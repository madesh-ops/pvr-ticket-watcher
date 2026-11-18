# PVR ticket watcher

Polls PVR's showtimes API **and District (district.in)** every 60s for **PVR Palazzo (Nexus Vijaya Mall)** and
**INOX Luxe (Phoenix Marketcity, Velachery)** in Chennai, keeps only bookable **2D**
shows of the chosen movie, and alerts on Telegram when a new one appears.

## Settings (environment variables)
| Variable | Default | Meaning |
|---|---|---|
| `PVR_MOVIES` | `AVENGERS` | Movie name keywords (comma-separated) |
| `PVR_DATES` | all open dates | Only these dates, e.g. `2026-12-18,2026-12-19` |
| `DISTRICT_URL` | Avengers: Doomsday, Chennai | District movie page for your city; empty to skip District |
| `DISTRICT_CINEMAS` | `NEXUS VIJAYA,PHOENIX` | Cinema-name keywords to match on District |

## Run on GitHub Actions
> ⚠️ PVR blocks GitHub's servers (HTTP 403), so the workflow is disabled. Run it locally instead.

1. Add repo secrets `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
2. Optional repo variables: `PVR_MOVIES` (default `AVENGERS`), `PVR_DATES` (e.g. `2026-12-18,2026-12-19`).
3. Actions → *PVR ticket watcher* → **Run workflow**. Each run lasts ~5h50m then starts the next.

## Run locally
```
pip install -r requirements.txt
set TELEGRAM_BOT_TOKEN=...
set TELEGRAM_CHAT_ID=...        # python pvr_watcher.py --chat-id
python pvr_watcher.py --test
python pvr_watcher.py
```
