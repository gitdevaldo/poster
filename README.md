# FBPost Control Panel + AutoPoster

Local Facebook group posting automation powered by Camoufox, with:

- Persistent browser profile/session handling
- Group scrape and merge workflow
- Template-based posting scheduler
- Multi-account support in one `config.yaml`
- Realtime web UI (no browser refresh required for actions)

## Requirements

- Python 3.10+
- System Python (no virtual environment required)
- Camoufox binaries

Install:

```bash
pip install -U -r requirements.txt
camoufox fetch
```

## Quick Start

1. First login/session setup:

```bash
python main.py --setup
```

2. Safe dry run:

```bash
python main.py --run-once --dry-run
```

3. Live run:

```bash
python main.py --run-once --live
```

## Web UI

Start local UI:

```bash
python main.py --ui --host 127.0.0.1 --port 8080
```

Open:

- `http://127.0.0.1:8080`

UI actions include:

- Add / delete account
- Enable / disable account
- Set active account
- Include / exclude groups
- Force scrape groups
- Test / setup session
- Run once (dry/live)

The UI communicates via API endpoints:

- `GET /api/state`
- `POST /api/action`

## Multi-Account

Accounts are stored in `config.yaml` under `accounts:` with one global default section.

- Default active account is `active_account`.
- Account-specific data/profile paths are isolated under `data/accounts/<account-id>/`.
- Disabled accounts cannot run posting/scraping/session actions.

Add account scaffold automatically:

```bash
python main.py --add account-2
```

Run a specific account:

```bash
python main.py --account account-2 --run-once --dry-run
python main.py --account account-2 --run-once --live
```

## Key Commands

```bash
python main.py --validate-session
python main.py --scrape-only
python main.py --scrape-only --scrape-force
python main.py --run-once --dry-run
python main.py --run-once --live
python main.py --ui --host 127.0.0.1 --port 8080
```

## Data Paths

Main account example:

- Session: `data/accounts/account-1/session.json`
- Profile: `data/accounts/account-1/camoufox_profile/`
- Groups: `data/accounts/account-1/groups.json`
- Posted log: `data/accounts/account-1/logs/posted_log.json`

## Notes

- Group include/exclude is saved as `active: true/false` in each account's `groups.json`.
- Rescrape merge preserves existing `active` status for matched groups.

## Reference

- PRD markdown: `fb-autoposter-prd.md`
