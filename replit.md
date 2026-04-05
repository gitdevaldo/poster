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
config.yaml              # Global config and accounts (includes commenting: section)
requirements.txt         # Python dependencies
core/                    # Application logic
  account_manager.py     # Multi-account CRUD + group comment toggles
  auto_commenter.py      # Auto Comment engine (sequential, browser stays open)
  config_loader.py       # YAML config parsing
  group_scraper.py       # Facebook group scraping
  logger.py              # Structured logging
  post_queue.py          # Template management and posting
  preset_manager.py      # Preset management
  scheduler.py           # Posting scheduler and delay engine
  session_manager.py     # Browser session/cookie persistence
  web_ui.py              # Control panel HTTP server + API (tabs: Auto Post, Auto Comment)
data/                    # Dynamic data (accounts, sessions, logs)
  accounts/<id>/
    comment_log.json     # {group_id: last_commented_post_id} for duplicate prevention
templates/               # Post content templates (YAML + images), shared by post + comment
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
- Web control panel with two tabs: Auto Post and Auto Comment
- **Auto Post:** Template-based posting, dry-run mode, scheduler, preset management
- **Auto Comment:** Continuous background commenter — visits each comment-enabled group, comments on the latest post (skips if already commented), configurable delay between groups
- Comment duplicate prevention via `comment_log.json` (`{group_id: last_post_id}`)
- Each tab shares accounts and live logs; Auto Comment has its own group toggles (`comment_active` field) and comment settings
- Real-time log streaming in both tabs

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
