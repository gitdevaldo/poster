from __future__ import annotations

import argparse
import os
from pathlib import Path

from core.account_manager import add_account
from core.config_loader import ACCOUNT_ENV_VAR
from core.group_scraper import scrape_groups
from core.scheduler import run_scheduler
from core.session_manager import ensure_session, validate_session
from core.web_ui import run_web_ui


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Facebook Group Auto-Poster")
    parser.add_argument("--account", type=str, help="Run using an account id from config.yaml accounts section")
    parser.add_argument("--add", dest="add_account", type=str, help="Add a new account profile into config.yaml")
    parser.add_argument("--ui", action="store_true", help="Start local web UI")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="UI bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8080, help="UI bind port (default: 8080)")
    parser.add_argument("--setup", action="store_true", help="Initialize login session and scrape groups")
    parser.add_argument("--scrape-only", action="store_true", help="Only refresh group inventory")
    parser.add_argument(
        "--scrape-force",
        action="store_true",
        help="Bypass freshness check and force group scraping",
    )
    parser.add_argument("--validate-session", action="store_true", help="Validate saved session and exit")
    parser.add_argument("--force-relogin", action="store_true", help="Force interactive relogin during setup")
    parser.add_argument("--run-once", action="store_true", help="Run one posting cycle")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Run live posting (overrides config dry_run for this execution)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Force dry-run (no submit) for this execution",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    config_path = Path("config.yaml")

    if args.add_account:
        ok, message = add_account(config_path, args.add_account)
        print(message)
        return

    if args.ui:
        run_web_ui(config_path=config_path, host=args.host, port=args.port)
        return

    if args.account:
        os.environ[ACCOUNT_ENV_VAR] = args.account.strip()

    if args.validate_session:
        is_valid = validate_session(config_path)
        print("Session valid." if is_valid else "Session invalid.")
        return

    if args.setup:
        ensure_session(config_path, force_relogin=args.force_relogin)
        return

    if args.scrape_only:
        scrape_groups(config_path, force=args.scrape_force)
        return

    force_dry_run: bool | None = None
    if args.live:
        force_dry_run = False
    elif args.dry_run:
        force_dry_run = True

    if args.run_once:
        run_scheduler(config_path, run_once=True, force_dry_run=force_dry_run)
        return

    run_scheduler(config_path, run_once=False, force_dry_run=force_dry_run)


if __name__ == "__main__":
    main()
