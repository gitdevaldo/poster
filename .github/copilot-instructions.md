# Project Guidelines

Facebook Group Auto-Poster: multi-account automation for posting to Facebook groups using Camoufox (anti-detection Firefox).

## Architecture

```
poster/
├── main.py                    # CLI entry point (argparse)
├── config.yaml                # Global config + per-account overrides
├── core/
│   ├── session_manager.py     # Camoufox browser lifecycle, login, session persistence
│   ├── group_scraper.py       # Scrapes joined groups from Facebook
│   ├── post_queue.py          # Template loading and rotation (single/sequential/round-robin/random)
│   ├── scheduler.py           # Main posting loop with delays and cooldowns
│   ├── account_manager.py     # Multi-account CRUD operations
│   ├── web_ui.py              # Built-in HTTP control panel (no framework)
│   ├── config_loader.py       # YAML config loading with account merging
│   └── logger.py              # Structured logging
├── templates/                 # Post templates (post_*.yaml) + images/
└── data/accounts/<id>/        # Per-account session, groups, logs
```

**Automation flow:**
1. `session_manager` → Camoufox browser with persistent profile, handles login/session
2. `group_scraper` → Extracts joined groups, merges with existing inventory
3. `post_queue` → Selects template based on rotation mode
4. `scheduler` → Posts to groups with random delays, cooldowns, rest breaks

**Multi-account:** Each account under `config.yaml > accounts:` has isolated data paths. Active account set via `--account` flag or `active_account` config.

## Commands

```bash
# Install
pip install -U -r requirements.txt
camoufox fetch

# Session setup (opens browser for manual login)
python main.py --setup
python main.py --setup --force-relogin
python main.py --validate-session

# Group scraping
python main.py --scrape-only
python main.py --scrape-only --scrape-force

# Posting
python main.py --run-once --dry-run      # Safe test
python main.py --run-once --live         # Live posting

# Multi-account
python main.py --account account-2 --run-once --dry-run
python main.py --add new-account-id

# Web UI control panel
python main.py --ui --host 127.0.0.1 --port 8080

# Validation (no formal tests exist)
python -m compileall main.py core
```

## Code Conventions

**Type hints:** All functions use Python type hints. Use `from __future__ import annotations` at top of modules.

**Config access:** Always go through `core.config_loader.load_config()` which handles account merging. Never read `config.yaml` directly.

**Camoufox browser:** Use `camoufox_kwargs()` from `session_manager` to build browser launch parameters. Browser options come from `config.yaml > browser:` and `camoufox:` sections.

**Template format** (`templates/post_*.yaml`):
```yaml
text: |
  Post content here...
image: templates/images/optional.jpg
tags: [for, logging, only]
```

**Rotation modes:** `single` (first template only), `sequential` (cycle templates per session), `round-robin` (cycle per group), `random`.

## Key Constraints

- **System Python only:** No virtual environments. Use system Python directly.
- **Config-driven behavior:** All scheduler/browser options must come from `config.yaml`. No hardcoding runtime overrides.
- **No scroll after composer:** During posting flow, do not add background scroll/motion after the composer popup opens (causes Facebook detection).
- **Preserve group metadata:** When merging scraped groups, keep existing `active` status and user data.
- **Session files are local:** Never commit `data/` contents. Sessions are machine-specific.

## PRD Reference

See `fb-autoposter-prd.md` for full product spec including:
- Anti-detection strategy (human-like delays, volume limits)
- Error handling patterns
- Recommended delay configurations
