# FBPost Control Panel + AutoPoster

A local automation tool for digital product sellers to automate posting promotional content to Facebook groups.

## Tech Stack

- **Language:** Python 3.12
- **Browser Automation:** Camoufox (anti-detection Firefox) + Playwright
- **Web UI:** Custom async Python HTTP server (`BaseHTTPRequestHandler`)
- **Config/Data:** YAML (config & templates), JSON (state, sessions, logs)
- **Terminal UI:** `rich`
- **Scheduling:** Custom scheduling engine

## Project Layout

```
main.py                  # Entry point (CLI + Web UI)
config.yaml              # Global config and accounts
requirements.txt         # Python dependencies
core/                    # Application logic
  account_manager.py     # Multi-account CRUD
  config_loader.py       # YAML config parsing
  group_scraper.py       # Facebook group scraping
  logger.py              # Structured logging
  post_queue.py          # Template management and posting
  preset_manager.py      # Preset management
  scheduler.py           # Posting scheduler and delay engine
  session_manager.py     # Browser session/cookie persistence
  web_ui.py              # Control panel HTTP server + API
data/                    # Dynamic data (accounts, sessions, logs)
templates/               # Post content templates (YAML + images)
logs/                    # Global system logs and screenshots
```

## Running the App

The web UI is started via the "Start application" workflow:

```bash
python main.py --host 0.0.0.0 --port 5000
```

The control panel is available at port 5000.

## Key Features

- Anti-detection browser automation via Camoufox
- Multi-account support with isolated sessions and group lists
- Web control panel for managing accounts, groups, schedules, and templates
- Dry-run mode for testing without actually posting
- Real-time log streaming in the dashboard

## CLI Options

- `--setup` — Initialize login session and scrape groups
- `--scrape-only` — Refresh group inventory only
- `--validate-session` — Check if saved session is still valid
- `--run-once` — Execute one posting cycle
- `--live` — Override config to run live (not dry-run)
- `--dry-run` — Force dry-run mode

## Dependencies

Installed via `pip install -r requirements.txt`:
- `camoufox[geoip]` — Anti-detection browser
- `playwright` — Browser automation
- `pyyaml` — YAML parsing
- `rich` — Terminal formatting
- `Pillow` — Image handling
- `schedule` — Scheduling utilities
