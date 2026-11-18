"""
PVR + District ticket watcher -> Telegram alerts.

Polls PVR's showtimes API for the configured cinemas, keeps only 2D shows
(drops 3D / 3D IMAX / 4DX) for the movies you care about, and pings you on
Telegram the moment a new bookable show appears.

Setup:
  pip install requests
  set TELEGRAM_BOT_TOKEN=123456:ABC...     (from @BotFather)
  set TELEGRAM_CHAT_ID=123456789           (run: python pvr_watcher.py --chat-id)
  python pvr_watcher.py --test             (sends a test message + prints current shows)
  python pvr_watcher.py                    (runs forever)
"""

import argparse
import json
import re
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

# Load KEY=VALUE lines from a local .env (git-ignored) without overriding real env vars.
_env = Path(__file__).with_name(".env")
if _env.exists():
    for _line in _env.read_text(encoding="utf-8").splitlines():
        if "=" in _line and not _line.lstrip().startswith("#"):
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

# ----------------------------- CONFIG -----------------------------------------
CITY = "Chennai"

# theatreId -> label (ids from PVR's cinemas API)
CINEMAS = {
    "388": "PVR Palazzo - Nexus Vijaya Mall",
    "320": "INOX Luxe - Phoenix Marketcity Velachery",
}

# Movies to watch (case-insensitive substring of the film name).
# Leave empty [] to alert on ANY new 2D show (noisy).
MOVIES = os.environ.get("PVR_MOVIES", "AVENGERS").split(",")
MOVIES = [m.strip().upper() for m in MOVIES if m.strip()]

# Only watch these dates (YYYY-MM-DD). Empty = every date PVR has opened.
DATES = [d.strip() for d in os.environ.get("PVR_DATES", "").split(",") if d.strip()]

POLL_SECONDS = int(os.environ.get("PVR_POLL_SECONDS", "60"))

# Stop after this many seconds (0 = forever). GitHub Actions jobs max out at 6h.
MAX_RUNTIME = int(os.environ.get("PVR_MAX_RUNTIME", "0"))

# District (district.in) movie page for your city. Empty = skip District.
DISTRICT_URL = os.environ.get(
    "DISTRICT_URL",
    "https://www.district.in/movies/avengers-doomsday-movie-tickets-in-chennai-MV176735")
# Cinema-name keywords to match on District (case-insensitive).
DISTRICT_CINEMAS = [c.strip().upper() for c in os.environ.get(
    "DISTRICT_CINEMAS", "NEXUS VIJAYA,PHOENIX").split(",") if c.strip()]

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

SEEN_FILE = Path(__file__).with_name("pvr_seen.json")
# ------------------------------------------------------------------------------

API = "https://api3.pvrcinemas.com/api/v1/booking/content/csessions"
HEADERS = {
    "Content-Type": "application/json",
    "Authorization": "Bearer ",
    "chain": "PVR",
    "country": "INDIA",
    "appVersion": "1.0",
    "platform": "WEBSITE",
    "city": CITY,
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Origin": "https://www.pvrcinemas.com",
    "Referer": "https://www.pvrcinemas.com/",
}
NON_2D_MARKERS = ("3D", "4DX")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # emoji on Windows console
except Exception:
    pass

session = requests.Session()
session.headers.update(HEADERS)

district_session = requests.Session()
district_session.headers.update({"User-Agent": HEADERS["User-Agent"],
                                 "Accept": "text/html", "Accept-Language": "en-IN,en"})


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def fetch(cid, dated="NA"):
    body = {"city": CITY, "cid": cid, "lat": "0.00", "lng": "0.00",
            "dated": dated, "qr": "NO", "cineType": "", "cineTypeQR": ""}
    r = session.post(API, json=body, timeout=20)
    r.raise_for_status()
    data = r.json()
    if data.get("result") != "success":
        raise RuntimeError(data.get("msg"))
    return data.get("output") or {}


def is_2d(film, exp_session, show):
    text = " ".join(str(x or "") for x in (
        film.get("filmName"), film.get("format"), exp_session.get("experience"),
        show.get("filmFormat"), show.get("movieFormat"), show.get("screenType"),
    )).upper()
    return not any(m in text for m in NON_2D_MARKERS)


def wanted_movie(name):
    return not MOVIES or any(m in name.upper() for m in MOVIES)


def parse_shows(cid, output):
    """Yield bookable 2D shows for wanted movies."""
    for ms in output.get("cinemaMovieSessions") or []:
        films = {f["filmId"]: f for f in (ms.get("movieRe") or {}).get("films") or []}
        for es in ms.get("experienceSessions") or []:
            for sh in es.get("shows") or []:
                film = films.get(sh.get("movieId"), {})
                name = film.get("filmName") or (ms.get("movieRe") or {}).get("filmName", "?")
                if not wanted_movie(name) or not is_2d(film, es, sh):
                    continue
                status_txt = str(sh.get("statusTxt", "")).upper()
                if sh.get("status") == 0 or any(w in status_txt for w in ("SOLD", "HOUSEFULL", "LAPSED")):
                    continue
                if sh.get("showTimeStamp", 0) / 1000 < time.time():
                    continue  # already started
                yield {
                    "key": f"{cid}:{sh.get('sessionId')}",
                    "source": "PVR",
                    "link": "https://www.pvrcinemas.com/",
                    "cinema": CINEMAS.get(cid, cid),
                    "movie": name,
                    "date": sh.get("showDate"),
                    "time": sh.get("showTime"),
                    "screen": sh.get("screenName"),
                    "exp": es.get("experience") or film.get("format") or "2D",
                    "status": sh.get("statusTxt"),
                }


def district_page(fmt=None, date=None):
    """Fetch a District movie page and return the JSON state Next.js embeds in it."""
    params = {}
    if fmt:
        params = {"frmtid": fmt.lower(), "fromdate": date}
    r = district_session.get(DISTRICT_URL, params=params, timeout=30)
    r.raise_for_status()
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
    if not m:
        raise RuntimeError("District page layout changed (no __NEXT_DATA__)")
    return json.loads(m.group(1))["props"]["pageProps"]["data"]["serverState"]


def check_district():
    state = district_page()
    content = next(iter((state.get("movieContents") or {}).values()), {})
    movie = content.get("name", "?")
    shows = []
    for grp in content.get("languageFormatGroups") or []:
        fmt, lang = grp["fmtGrpId"], grp.get("language", "")
        dates = None
        todo = DATES[:] if DATES else [None]
        while todo:
            dt = todo.pop(0)
            st = district_page(fmt, dt or datetime.now().strftime("%Y-%m-%d"))
            for key, ms in (st.get("movieSessions") or {}).items():
                if dates is None and not DATES:  # discover open dates from the first page
                    dates = (ms.get("data") or {}).get("sessionDates") or []
                    todo = [d for d in dates if d != ms.get("searchDate")]
                page = ms.get("pageData") or {}
                for cin in (page.get("nearbyCinemas") or []) + (page.get("farCinemas") or []):
                    name = (cin.get("cinemaInfo") or {}).get("name") or cin.get("name", "")
                    if DISTRICT_CINEMAS and not any(k in name.upper() for k in DISTRICT_CINEMAS):
                        continue
                    for s in cin.get("sessions") or []:
                        scrn = str(s.get("scrnFmt") or "")
                        if any(m in scrn.upper() for m in NON_2D_MARKERS):
                            continue
                        status = str(s.get("seatStatus") or "")
                        if not s.get("avail") or s.get("disableClick") or "SOLD" in status.upper():
                            continue
                        # showTime is UTC -> IST
                        t = datetime.fromisoformat(s["showTime"]) + timedelta(hours=5, minutes=30)
                        if t < datetime.utcnow() + timedelta(hours=5, minutes=30):
                            continue
                        shows.append({
                            "key": f"district:{s.get('encSessionId') or s.get('sid')}",
                            "source": "District",
                            "link": f"{DISTRICT_URL}?frmtid={fmt.lower()}&fromdate={t:%Y-%m-%d}",
                            "cinema": name,
                            "movie": f"{movie} ({lang})",
                            "date": f"{t:%Y-%m-%d}",
                            "time": f"{t:%I:%M %p}",
                            "screen": s.get("audi"),
                            "exp": scrn or "2D",
                            "status": status or "Available",
                        })
            time.sleep(0.5)
    return shows


def check_all():
    shows, errors = [], []
    sources = [("PVR", check_pvr)] + ([("District", check_district)] if DISTRICT_URL else [])
    for name, fn in sources:
        try:
            shows.extend(fn())
        except Exception as e:
            log(f"{name} check failed: {e}")
            errors.append(f"{name}: {e}")
    if len(errors) == len(sources):
        raise RuntimeError("; ".join(errors))
    return list({s["key"]: s for s in shows}.values())  # drop duplicates


def check_pvr():
    shows = []
    for cid in CINEMAS:
        if DATES:  # fast path: dated calls are ~0.1s, the "NA" discovery call can take ~40s
            for dt in DATES:
                try:
                    shows.extend(parse_shows(cid, fetch(cid, dt)))
                except RuntimeError:
                    pass  # PVR returns an error for dates not open for booking yet
            continue
        first = fetch(cid)
        dates = [d["dt"] for d in first.get("days") or [] if d.get("enable")]
        if DATES:
            dates = [d for d in dates if d in DATES]
        for i, dt in enumerate(dates):
            out = first if i == 0 and first.get("days", [{}])[0].get("dt") == dt else fetch(cid, dt)
            shows.extend(parse_shows(cid, out))
            time.sleep(0.5)
    return shows


def telegram(text):
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        log("Telegram not configured; message was:\n" + text)
        return
    r = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT_ID, "text": text,
              "parse_mode": "HTML", "disable_web_page_preview": True},
        timeout=20)
    if not r.ok:
        log(f"Telegram error {r.status_code}: {r.text}")


def format_alert(new):
    lines = ["🎬 <b>2D tickets available!</b>"]
    by_group = {}
    for s in new:
        by_group.setdefault((s["date"], s["source"], s["movie"], s["cinema"], s["link"]), []).append(s)
    for (date, source, movie, cinema, link), items in sorted(by_group.items()):
        times = ", ".join(f"{s['time']} ({s['screen']}, {s['exp']}, {s['status']})" for s in items)
        lines.append(f"\n<b>{movie}</b>\n📍 {cinema}\n📅 {date}\n🕒 {times}\n🔗 {source}: {link}")
    return "\n".join(lines)[:4000]


def load_seen():
    try:
        return set(json.loads(SEEN_FILE.read_text()))
    except Exception:
        return set()


def save_seen(seen):
    SEEN_FILE.write_text(json.dumps(sorted(seen)))


def get_chat_id():
    if not TELEGRAM_BOT_TOKEN:
        sys.exit("Set TELEGRAM_BOT_TOKEN first, then send any message to your bot.")
    r = requests.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates", timeout=20).json()
    ids = {u["message"]["chat"]["id"] for u in r.get("result", []) if "message" in u}
    print("Chat IDs found:", ids or "none - send /start to your bot and retry")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="one check, print shows, send test msg")
    ap.add_argument("--chat-id", action="store_true", help="print your Telegram chat id")
    args = ap.parse_args()

    if args.chat_id:
        return get_chat_id()

    log(f"Watching {list(CINEMAS.values())} | movies={MOVIES or 'ANY'} | "
        f"dates={DATES or 'ALL'} | every {POLL_SECONDS}s")

    if args.test:
        shows = check_all()
        for s in shows:
            print(f"  {s['date']} {s['time']:>8}  {s['movie']}  @ {s['cinema']}  [{s['exp']}, {s['status']}]")
        log(f"{len(shows)} matching 2D shows right now")
        telegram("✅ PVR watcher test OK\n\n" + (format_alert(shows[:15]) if shows else "No matching shows yet."))
        return

    first_run = not SEEN_FILE.exists()
    seen = load_seen()
    save_seen(seen)
    if first_run:  # announce once, not on every GitHub Actions restart
        telegram(f"👀 PVR watcher started: {', '.join(MOVIES) or 'any movie'} (2D) at "
                 + ", ".join(CINEMAS.values()))
    errors = 0
    started = time.time()
    while not MAX_RUNTIME or time.time() - started < MAX_RUNTIME:
        try:
            shows = check_all()
            new = [s for s in shows if s["key"] not in seen]
            if new:
                log(f"{len(new)} NEW shows -> alerting")
                telegram(format_alert(new))
                seen.update(s["key"] for s in new)
                save_seen(seen)
            else:
                log(f"no new shows ({len(shows)} known)")
            errors = 0
        except Exception as e:
            errors += 1
            log(f"error: {e}")
            if errors == 5:
                telegram(f"⚠️ PVR watcher failing repeatedly: {e}")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
