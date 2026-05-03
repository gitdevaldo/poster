from __future__ import annotations

import json
import os
import threading
from collections import deque
from contextlib import contextmanager
from datetime import datetime, time as dt_time, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import yaml
from zoneinfo import ZoneInfo

from core.account_manager import (
    add_account,
    delete_account,
    get_active_account,
    list_accounts,
    list_groups_for_account,
    load_raw_config,
    save_raw_config,
    set_account_template,
    set_account_enabled,
    set_active_account,
    set_group_included,
    set_group_comment_included,
)
from core.auto_commenter import run_auto_commenter
from core.config_loader import ACCOUNT_ENV_VAR, load_config, load_config_lenient
from core.group_scraper import scrape_groups
from core.logger import get_log_file, set_log_source
from core.post_queue import load_templates
from core.preset_manager import (
    list_presets,
    load_preset,
    save_preset,
    delete_preset,
    get_current_state,
    apply_preset_values,
    disable_preset,
    get_preset_status,
)
from core.scheduler import run_scheduler
from core.session_manager import ensure_session, validate_session


@contextmanager
def _account_env(account_id: str | None):
    previous = os.environ.get(ACCOUNT_ENV_VAR)
    try:
        if account_id:
            os.environ[ACCOUNT_ENV_VAR] = account_id
        elif ACCOUNT_ENV_VAR in os.environ:
            del os.environ[ACCOUNT_ENV_VAR]
        yield
    finally:
        if previous is None:
            if ACCOUNT_ENV_VAR in os.environ:
                del os.environ[ACCOUNT_ENV_VAR]
        else:
            os.environ[ACCOUNT_ENV_VAR] = previous


def _parse_hhmm(value: str) -> tuple[bool, str, dt_time | None]:
    raw = str(value or "").strip()
    try:
        parsed = datetime.strptime(raw, "%H:%M").time()
        return True, "", parsed
    except Exception:
        return False, "Time must be in HH:MM format.", None


def _normalize_weekdays(values: Any) -> list[int]:
    if not isinstance(values, list):
        return []
    normalized: list[int] = []
    for item in values:
        try:
            val = int(item)
        except Exception:
            continue
        if 0 <= val <= 6 and val not in normalized:
            normalized.append(val)
    normalized.sort()
    return normalized


def _safe_zoneinfo(name: str) -> ZoneInfo | None:
    try:
        return ZoneInfo(name)
    except Exception:
        return None


_DEFAULT_TZ = "Asia/Jakarta"


def _normalize_schedule_rules(
    schedule_rules: dict[str, Any],
    default_timezone: str = _DEFAULT_TZ,
) -> tuple[bool, str, dict[str, Any]]:
    if not isinstance(schedule_rules, dict):
        return False, "Invalid schedule payload.", {}

    schedule_type = str(schedule_rules.get("type", "")).strip().lower()
    if schedule_type not in {"one_time", "daily", "weekly", "specific_datetime"}:
        return False, "Schedule type is invalid.", {}

    # Timezone is always Asia/Jakarta; ignore whatever the client sends.
    timezone_name = _DEFAULT_TZ
    tz = _safe_zoneinfo(timezone_name)
    if tz is None:
        return False, f"Invalid timezone '{timezone_name}'.", {}

    normalized: dict[str, Any] = {
        "enabled": True,
        "type": schedule_type,
        "timezone": timezone_name,
        "dry_run": False,  # always live
    }

    if schedule_type in {"one_time", "daily", "weekly"}:
        ok, err, parsed_time = _parse_hhmm(str(schedule_rules.get("time", "")).strip())
        if not ok or parsed_time is None:
            return False, err, {}
        normalized["time"] = parsed_time.strftime("%H:%M")
        if schedule_type == "one_time":
            run_at_local = _compute_one_time_run_at_local_iso(normalized["time"], timezone_name)
            if not run_at_local:
                return False, "Failed to compute one-time schedule time.", {}
            normalized["run_at"] = run_at_local

    if schedule_type == "weekly":
        weekdays = _normalize_weekdays(schedule_rules.get("weekdays", []))
        if not weekdays:
            return False, "Weekly schedule requires at least one day.", {}
        normalized["weekdays"] = weekdays

    if schedule_type == "specific_datetime":
        dt_raw = str(schedule_rules.get("specific_datetime", "")).strip()
        if not dt_raw:
            return False, "Specific date/time is required.", {}
        try:
            local_dt = datetime.fromisoformat(dt_raw)
        except Exception:
            return False, "Invalid specific date/time format.", {}
        if local_dt.tzinfo is None:
            local_dt = local_dt.replace(tzinfo=tz)
        else:
            local_dt = local_dt.astimezone(tz)
        local_iso = local_dt.replace(second=0, microsecond=0).isoformat()
        normalized["specific_datetime"] = local_iso
        normalized["run_at"] = local_iso

    # Optional comment-session window: time_to (HH:MM stop time)
    time_to_raw = str(schedule_rules.get("time_to", "")).strip()
    if time_to_raw:
        ok2, _, parsed_to = _parse_hhmm(time_to_raw)
        if ok2 and parsed_to is not None:
            normalized["time_to"] = parsed_to.strftime("%H:%M")

    return True, "", normalized


def _compute_next_run_utc(spec: dict[str, Any], now_utc: datetime | None = None) -> datetime | None:
    schedule_type = str(spec.get("type", "")).strip().lower()
    timezone_name = str(spec.get("timezone", "UTC")).strip() or "UTC"
    tz = _safe_zoneinfo(timezone_name)
    if tz is None:
        return None

    now_utc = now_utc or datetime.now(timezone.utc)
    now_local = now_utc.astimezone(tz)

    if schedule_type in {"one_time", "specific_datetime"}:
        raw = str(spec.get("run_at", "")).strip()
        if not raw and schedule_type == "specific_datetime":
            raw = str(spec.get("specific_datetime", "")).strip()
        if not raw and schedule_type == "one_time":
            raw = str(spec.get("specific_datetime", "")).strip()
        if not raw:
            return None
        try:
            dt_local = datetime.fromisoformat(raw)
            if dt_local.tzinfo is None:
                dt_local = dt_local.replace(tzinfo=tz)
            else:
                dt_local = dt_local.astimezone(tz)
        except Exception:
            return None
        if dt_local <= now_local:
            return None
        return dt_local.astimezone(timezone.utc)

    time_raw = str(spec.get("time", "")).strip()
    ok, _, run_time = _parse_hhmm(time_raw)
    if not ok or run_time is None:
        return None

    if schedule_type == "daily":
        candidate_local = datetime.combine(now_local.date(), run_time, tzinfo=tz)
        if candidate_local <= now_local:
            candidate_local = candidate_local + timedelta(days=1)
        return candidate_local.astimezone(timezone.utc)

    if schedule_type == "weekly":
        weekdays = _normalize_weekdays(spec.get("weekdays", []))
        if not weekdays:
            return None
        base_date = now_local.date()
        best_local: datetime | None = None
        for add_days in range(0, 15):
            d = base_date + timedelta(days=add_days)
            if d.weekday() not in weekdays:
                continue
            candidate = datetime.combine(d, run_time, tzinfo=tz)
            if candidate <= now_local:
                continue
            if best_local is None or candidate < best_local:
                best_local = candidate
        return best_local.astimezone(timezone.utc) if best_local else None

    return None


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _new_schedule_id() -> str:
    return f"sch-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}"


def _compute_one_time_run_at_local_iso(time_hhmm: str, timezone_name: str) -> str:
    tz = _safe_zoneinfo(timezone_name)
    if tz is None:
        return ""
    ok, _, run_time = _parse_hhmm(time_hhmm)
    if not ok or run_time is None:
        return ""
    now_local = datetime.now(timezone.utc).astimezone(tz)
    candidate = datetime.combine(now_local.date(), run_time, tzinfo=tz)
    if candidate <= now_local:
        candidate = candidate + timedelta(days=1)
    return candidate.replace(second=0, microsecond=0).isoformat()


def _ensure_schedule_record_defaults(
    record: dict[str, Any],
    *,
    default_timezone: str = "UTC",
    touch: bool = False,
) -> dict[str, Any]:
    normalized = dict(record) if isinstance(record, dict) else {}
    now_iso = _now_utc_iso()

    schedule_type = str(normalized.get("type", "")).strip().lower()
    timezone_name = str(normalized.get("timezone", "")).strip() or default_timezone or "UTC"

    normalized["id"] = str(normalized.get("id", "")).strip() or _new_schedule_id()
    normalized["enabled"] = bool(normalized.get("enabled", True))
    normalized["type"] = schedule_type
    normalized["timezone"] = timezone_name
    normalized["dry_run"] = bool(normalized.get("dry_run", False))

    if schedule_type in {"one_time", "daily", "weekly"}:
        ok, _, parsed_time = _parse_hhmm(str(normalized.get("time", "")).strip())
        normalized["time"] = parsed_time.strftime("%H:%M") if ok and parsed_time is not None else ""

    if schedule_type == "weekly":
        normalized["weekdays"] = _normalize_weekdays(normalized.get("weekdays", []))

    if schedule_type == "specific_datetime":
        normalized["specific_datetime"] = str(normalized.get("specific_datetime", "")).strip()

    if schedule_type in {"one_time", "specific_datetime"}:
        run_at = str(normalized.get("run_at", "")).strip()
        if not run_at and schedule_type == "one_time":
            run_at = _compute_one_time_run_at_local_iso(
                str(normalized.get("time", "")).strip(),
                timezone_name,
            )
        if not run_at and schedule_type == "specific_datetime":
            run_at = str(normalized.get("specific_datetime", "")).strip()
        normalized["run_at"] = run_at

    normalized["created_at"] = str(normalized.get("created_at", "")).strip() or now_iso
    if touch:
        normalized["updated_at"] = now_iso
    else:
        normalized["updated_at"] = str(normalized.get("updated_at", "")).strip() or now_iso
    normalized["last_run_at"] = str(normalized.get("last_run_at", "")).strip()
    normalized["last_result"] = str(normalized.get("last_result", "")).strip()
    normalized["last_status"] = str(normalized.get("last_status", "")).strip() or "pending"
    return normalized


def _extract_account_schedules(account: dict[str, Any]) -> list[dict[str, Any]]:
    schedules_raw = account.get("schedules")
    if isinstance(schedules_raw, list):
        return [dict(item) for item in schedules_raw if isinstance(item, dict)]
    legacy = account.get("schedule")
    if isinstance(legacy, dict):
        return [dict(legacy)]
    return []


def _save_account_schedules(config_path: Path, account_id: str, schedules: list[dict[str, Any]]) -> bool:
    try:
        config = load_raw_config(config_path)
        accounts = config.get("accounts", {})
        if not isinstance(accounts, dict):
            return False
        account = accounts.get(account_id)
        if not isinstance(account, dict):
            return False
        account["schedules"] = [dict(item) for item in schedules if isinstance(item, dict)]
        if "schedule" in account:
            del account["schedule"]
        accounts[account_id] = account
        config["accounts"] = accounts
        save_raw_config(config_path, config)
        return True
    except Exception:
        return False


def _get_account_schedules(config_path: Path, account_id: str | None) -> list[dict[str, Any]]:
    if not account_id:
        return []
    config = load_raw_config(config_path)
    accounts = config.get("accounts", {})
    if not isinstance(accounts, dict):
        return []
    account = accounts.get(account_id)
    if not isinstance(account, dict):
        return []
    records = _extract_account_schedules(account)
    normalized: list[dict[str, Any]] = []
    changed = "schedule" in account or not isinstance(account.get("schedules"), list)
    for item in records:
        if not isinstance(item, dict):
            changed = True
            continue
        timezone_name = str(item.get("timezone", "")).strip() or "UTC"
        normalized_item = _ensure_schedule_record_defaults(item, default_timezone=timezone_name, touch=False)
        if normalized_item != item:
            changed = True
        normalized.append(normalized_item)
    if changed:
        account["schedules"] = [dict(item) for item in normalized]
        if "schedule" in account:
            del account["schedule"]
        accounts[account_id] = account
        config["accounts"] = accounts
        save_raw_config(config_path, config)
    return normalized


def _find_schedule_record(
    schedules: list[dict[str, Any]],
    schedule_id: str,
) -> tuple[int, dict[str, Any] | None]:
    target = schedule_id.strip()
    if not target:
        return -1, None
    for idx, item in enumerate(schedules):
        if str(item.get("id", "")).strip() == target:
            return idx, item
    return -1, None


def _get_next_due_schedule(config_path: Path) -> tuple[str, dict[str, Any], datetime] | None:
    config = load_raw_config(config_path)
    accounts = config.get("accounts", {})
    if not isinstance(accounts, dict):
        return None

    best: tuple[str, dict[str, Any], datetime] | None = None
    config_changed = False
    for account_id, account in accounts.items():
        if not isinstance(account, dict):
            continue
        records = _extract_account_schedules(account)
        normalized_records: list[dict[str, Any]] = []
        account_changed = "schedule" in account or not isinstance(account.get("schedules"), list)
        for item in records:
            if not isinstance(item, dict):
                account_changed = True
                continue
            record = _ensure_schedule_record_defaults(
                item,
                default_timezone=str(item.get("timezone", "")).strip() or "UTC",
                touch=False,
            )
            if record != item:
                account_changed = True
            normalized_records.append(record)
            if not bool(record.get("enabled", True)):
                continue
            next_run = _compute_next_run_utc(record)
            if next_run is None:
                continue
            if best is None or next_run < best[2]:
                best = (str(account_id), record, next_run)
        if account_changed:
            account["schedules"] = [dict(item) for item in normalized_records]
            if "schedule" in account:
                del account["schedule"]
            accounts[account_id] = account
            config_changed = True
    if config_changed:
        config["accounts"] = accounts
        save_raw_config(config_path, config)
    return best


def _save_account_comment_schedules(config_path: Path, account_id: str, schedules: list[dict[str, Any]]) -> bool:
    try:
        config = load_raw_config(config_path)
        accounts = config.get("accounts", {})
        if not isinstance(accounts, dict):
            return False
        account = accounts.get(account_id)
        if not isinstance(account, dict):
            return False
        account["comment_schedules"] = [dict(item) for item in schedules if isinstance(item, dict)]
        accounts[account_id] = account
        config["accounts"] = accounts
        save_raw_config(config_path, config)
        return True
    except Exception:
        return False


def _get_account_comment_schedules(config_path: Path, account_id: str | None) -> list[dict[str, Any]]:
    if not account_id:
        return []
    config = load_raw_config(config_path)
    accounts = config.get("accounts", {})
    if not isinstance(accounts, dict):
        return []
    account = accounts.get(account_id)
    if not isinstance(account, dict):
        return []
    records = account.get("comment_schedules", [])
    if not isinstance(records, list):
        return []
    normalized: list[dict[str, Any]] = []
    changed = False
    for item in records:
        if not isinstance(item, dict):
            changed = True
            continue
        timezone_name = str(item.get("timezone", "")).strip() or "UTC"
        normalized_item = _ensure_schedule_record_defaults(item, default_timezone=timezone_name, touch=False)
        if normalized_item != item:
            changed = True
        normalized.append(normalized_item)
    if changed:
        account["comment_schedules"] = [dict(item) for item in normalized]
        accounts[account_id] = account
        config["accounts"] = accounts
        save_raw_config(config_path, config)
    return normalized


def _get_next_due_comment_schedule(config_path: Path) -> tuple[str, dict[str, Any], datetime] | None:
    config = load_raw_config(config_path)
    accounts = config.get("accounts", {})
    if not isinstance(accounts, dict):
        return None
    best: tuple[str, dict[str, Any], datetime] | None = None
    config_changed = False
    for account_id, account in accounts.items():
        if not isinstance(account, dict):
            continue
        records = account.get("comment_schedules", [])
        if not isinstance(records, list):
            continue
        normalized_records: list[dict[str, Any]] = []
        account_changed = False
        for item in records:
            if not isinstance(item, dict):
                account_changed = True
                continue
            record = _ensure_schedule_record_defaults(
                item,
                default_timezone=str(item.get("timezone", "")).strip() or "UTC",
                touch=False,
            )
            if record != item:
                account_changed = True
            normalized_records.append(record)
            if not bool(record.get("enabled", True)):
                continue
            next_run = _compute_next_run_utc(record)
            if next_run is None:
                continue
            if best is None or next_run < best[2]:
                best = (str(account_id), record, next_run)
        if account_changed:
            account["comment_schedules"] = [dict(item) for item in normalized_records]
            accounts[account_id] = account
            config_changed = True
    if config_changed:
        config["accounts"] = accounts
        save_raw_config(config_path, config)
    return best


def _to_schedule_ui_record(record: dict[str, Any]) -> dict[str, Any]:
    item = dict(record)
    run_at_raw = str(item.get("run_at", "")).strip()
    if run_at_raw:
        item["run_at_input"] = _iso_to_local_datetime_input(
            run_at_raw,
            str(item.get("timezone", "")),
        )
    if isinstance(item.get("specific_datetime"), str):
        item["specific_datetime_input"] = _iso_to_local_datetime_input(
            str(item.get("specific_datetime", "")),
            str(item.get("timezone", "")),
        )
    if not str(item.get("specific_datetime_input", "")).strip() and run_at_raw:
        item["specific_datetime_input"] = _iso_to_local_datetime_input(
            run_at_raw,
            str(item.get("timezone", "")),
        )
    if bool(item.get("enabled", True)):
        next_run = _compute_next_run_utc(item)
        item["next_run_at"] = next_run.isoformat() if next_run else ""
    else:
        item["next_run_at"] = ""
    return item


def _iso_to_local_datetime_input(value: str, timezone_name: str | None = None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        dt = datetime.fromisoformat(raw)
    except Exception:
        return ""
    tz = _safe_zoneinfo(str(timezone_name or "").strip()) if timezone_name else None
    if tz is not None:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        else:
            dt = dt.astimezone(tz)
    elif dt.tzinfo is not None:
        dt = dt.astimezone()
    return dt.strftime("%Y-%m-%dT%H:%M")


class _WebState:
    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path
        self.lock = threading.RLock()
        self.runner_thread: threading.Thread | None = None
        self.runner_account: str = ""
        self.runner_mode: str = ""
        self.runner_status: str = "idle"
        self.runner_message: str = ""
        self.pause_event = threading.Event()
        self.stop_event = threading.Event()
        self.schedule_thread: threading.Thread | None = None
        self.schedule_stop_event = threading.Event()
        self.schedule_wake_event = threading.Event()
        self.schedule_status: str = "idle"
        self.schedule_message: str = ""
        self.schedule_next_run_at: str = ""
        self.schedule_last_run_at: str = ""
        self.schedule_last_result: str = ""
        self.schedule_account: str = ""
        self.schedule_spec: dict[str, Any] = {}
        self.schedule_dry_run: bool = False
        self.commenter_thread: threading.Thread | None = None
        self.commenter_account: str = ""
        self.commenter_status: str = "idle"
        self.commenter_message: str = ""
        self.commenter_stop_event = threading.Event()
        self.commenter_pause_event = threading.Event()
        self.comment_schedule_thread: threading.Thread | None = None
        self.comment_schedule_stop_event = threading.Event()
        self.comment_schedule_wake_event = threading.Event()
        self.comment_schedule_status: str = "idle"
        self.comment_schedule_message: str = ""
        self.comment_schedule_next_run_at: str = ""
        self.comment_schedule_last_run_at: str = ""
        self.comment_schedule_last_result: str = ""
        self.comment_schedule_account: str = ""
        self.comment_schedule_spec: dict[str, Any] = {}
        self._ensure_schedule_thread_locked()
        self._ensure_comment_schedule_thread_locked()

    def _sync_runner_locked(self) -> None:
        if self.runner_thread is not None and not self.runner_thread.is_alive():
            self.runner_thread = None
            self.runner_account = ""
            self.runner_mode = ""
            self.runner_status = "idle"
            self.pause_event.clear()
            self.stop_event.clear()
        if self.schedule_thread is not None and not self.schedule_thread.is_alive():
            self.schedule_thread = None
        if self.commenter_thread is not None and not self.commenter_thread.is_alive():
            self.commenter_thread = None
            self.commenter_account = ""
            self.commenter_status = "idle"
            self.commenter_stop_event.clear()
            self.commenter_pause_event.clear()

    def _mark_schedule_changed_locked(self) -> None:
        self.schedule_wake_event.set()
        self._ensure_schedule_thread_locked()

    def _ensure_schedule_thread_locked(self) -> None:
        self._sync_runner_locked()
        if self.schedule_thread is not None and self.schedule_thread.is_alive():
            return

        self.schedule_stop_event.clear()
        self.schedule_wake_event.clear()

        def _schedule_worker() -> None:
            try:
                while not self.schedule_stop_event.is_set():
                    due = _get_next_due_schedule(self.config_path)
                    if due is None:
                        with self.lock:
                            self.schedule_status = "idle"
                            self.schedule_message = "No enabled schedules."
                            self.schedule_next_run_at = ""
                            self.schedule_account = ""
                            self.schedule_spec = {}
                            self.schedule_dry_run = False
                        self.schedule_wake_event.wait(2.0)
                        self.schedule_wake_event.clear()
                        continue

                    account_id, record, next_run_utc = due
                    schedule_id = str(record.get("id", "")).strip()
                    with self.lock:
                        self.schedule_status = "running"
                        self.schedule_message = f"Waiting for schedule '{schedule_id}' on '{account_id}'."
                        self.schedule_next_run_at = next_run_utc.isoformat()
                        self.schedule_account = account_id
                        self.schedule_spec = dict(record)
                        self.schedule_dry_run = bool(record.get("dry_run", False))

                    should_recompute = False
                    while not self.schedule_stop_event.is_set():
                        now_utc = datetime.now(timezone.utc)
                        if now_utc >= next_run_utc:
                            break
                        wait_seconds = max(0.2, min(1.0, (next_run_utc - now_utc).total_seconds()))
                        if self.schedule_wake_event.wait(wait_seconds):
                            self.schedule_wake_event.clear()
                            should_recompute = True
                            break
                    if self.schedule_stop_event.is_set():
                        break
                    if should_recompute:
                        continue

                    with self.lock:
                        self.schedule_status = "executing"
                        self.schedule_message = f"Running schedule '{schedule_id}' on '{account_id}'."

                    # Auto-reset posted_log before scheduled run so all groups are posted
                    try:
                        with _account_env(account_id):
                            from core.config_loader import load_config as _load_cfg
                            cfg = _load_cfg(self.config_path)
                            paths_cfg = cfg.get("paths", {}) if isinstance(cfg.get("paths"), dict) else {}
                            posted_log_path = Path(str(paths_cfg.get("posted_log", "data/posted_log.json")))
                            if posted_log_path.exists():
                                posted_log_path.unlink()
                    except Exception:
                        pass  # Non-fatal; continue with run

                    started = False
                    start_error = ""
                    run_live = not bool(record.get("dry_run", False))
                    while not self.schedule_stop_event.is_set():
                        with self.lock:
                            ok_start, msg_start = self.start_run(account_id, live=run_live)
                        if ok_start:
                            started = True
                            break
                        if "in progress" in msg_start.lower():
                            if self.schedule_wake_event.wait(1.0):
                                self.schedule_wake_event.clear()
                            continue
                        start_error = msg_start
                        break

                    if not started:
                        schedules = _get_account_schedules(self.config_path, account_id)
                        idx, current = _find_schedule_record(schedules, schedule_id)
                        if idx >= 0 and isinstance(current, dict):
                            updated = _ensure_schedule_record_defaults(
                                current,
                                default_timezone=str(current.get("timezone", "")).strip() or "UTC",
                                touch=True,
                            )
                            updated["last_run_at"] = _now_utc_iso()
                            updated["last_result"] = start_error or "Skipped (scheduler stopped)."
                            updated["last_status"] = "error"
                            if str(updated.get("type", "")).strip() in {"one_time", "specific_datetime"}:
                                updated["enabled"] = False
                            schedules[idx] = updated
                            _save_account_schedules(self.config_path, account_id, schedules)
                        with self.lock:
                            self.schedule_last_run_at = _now_utc_iso()
                            self.schedule_last_result = start_error or "Skipped (scheduler stopped)."
                            self.schedule_status = "running"
                            self.schedule_message = "Waiting for next scheduled run."
                        continue

                    while not self.schedule_stop_event.is_set():
                        with self.lock:
                            active = self.runner_thread is not None and self.runner_thread.is_alive()
                        if not active:
                            break
                        self.schedule_wake_event.wait(0.5)
                        self.schedule_wake_event.clear()

                    if self.schedule_stop_event.is_set():
                        with self.lock:
                            if self.runner_thread is not None and self.runner_account == account_id:
                                self.stop_run(account_id)
                        break

                    with self.lock:
                        self._sync_runner_locked()
                        run_result = self.runner_message or "success"
                    run_status = "success" if run_result == "success" else "error"

                    schedules = _get_account_schedules(self.config_path, account_id)
                    idx, current = _find_schedule_record(schedules, schedule_id)
                    if idx >= 0 and isinstance(current, dict):
                        updated = _ensure_schedule_record_defaults(
                            current,
                            default_timezone=str(current.get("timezone", "")).strip() or "UTC",
                            touch=True,
                        )
                        updated["last_run_at"] = _now_utc_iso()
                        updated["last_result"] = run_result
                        updated["last_status"] = run_status
                        if str(updated.get("type", "")).strip() in {"one_time", "specific_datetime"}:
                            updated["enabled"] = False
                        schedules[idx] = updated
                        _save_account_schedules(self.config_path, account_id, schedules)

                    with self.lock:
                        self.schedule_last_run_at = _now_utc_iso()
                        self.schedule_last_result = run_result
                        self.schedule_status = "running"
                        self.schedule_message = "Waiting for next scheduled run."
            except Exception as exc:  # pragma: no cover - defensive
                with self.lock:
                    self.schedule_status = "error"
                    self.schedule_message = f"{type(exc).__name__}: {exc}"
                    self.schedule_next_run_at = ""
            finally:
                with self.lock:
                    self.schedule_thread = None
                    if self.schedule_status != "error":
                        self.schedule_status = "stopped"
                        self.schedule_message = "Scheduler stopped."
                        self.schedule_next_run_at = ""

        self.schedule_thread = threading.Thread(
            target=_schedule_worker,
            name="web-ui-scheduler-loop",
            daemon=True,
        )
        self.schedule_thread.start()

    def _ensure_comment_schedule_thread_locked(self) -> None:
        if self.comment_schedule_thread is not None and self.comment_schedule_thread.is_alive():
            return
        self.comment_schedule_stop_event.clear()
        self.comment_schedule_wake_event.clear()

        def _comment_schedule_worker() -> None:
            try:
                while not self.comment_schedule_stop_event.is_set():
                    due = _get_next_due_comment_schedule(self.config_path)
                    if due is None:
                        with self.lock:
                            self.comment_schedule_status = "idle"
                            self.comment_schedule_message = "No enabled comment schedules."
                            self.comment_schedule_next_run_at = ""
                            self.comment_schedule_account = ""
                            self.comment_schedule_spec = {}
                        self.comment_schedule_wake_event.wait(2.0)
                        self.comment_schedule_wake_event.clear()
                        continue
                    account_id, record, next_run_utc = due
                    schedule_id = str(record.get("id", "")).strip()
                    with self.lock:
                        self.comment_schedule_status = "running"
                        self.comment_schedule_message = f"Waiting for schedule '{schedule_id}' on '{account_id}'."
                        self.comment_schedule_next_run_at = next_run_utc.isoformat()
                        self.comment_schedule_account = account_id
                        self.comment_schedule_spec = dict(record)
                    should_recompute = False
                    while not self.comment_schedule_stop_event.is_set():
                        now_utc = datetime.now(timezone.utc)
                        if now_utc >= next_run_utc:
                            break
                        wait_seconds = max(0.2, min(1.0, (next_run_utc - now_utc).total_seconds()))
                        if self.comment_schedule_wake_event.wait(wait_seconds):
                            self.comment_schedule_wake_event.clear()
                            should_recompute = True
                            break
                    if self.comment_schedule_stop_event.is_set():
                        break
                    if should_recompute:
                        continue
                    with self.lock:
                        self.comment_schedule_status = "executing"
                        self.comment_schedule_message = f"Running comment schedule '{schedule_id}' on '{account_id}'."
                    started = False
                    start_error = ""
                    while not self.comment_schedule_stop_event.is_set():
                        with self.lock:
                            ok_start, msg_start = self.start_commenter(account_id)
                        if ok_start:
                            started = True
                            break
                        if "already running" in msg_start.lower():
                            if self.comment_schedule_wake_event.wait(1.0):
                                self.comment_schedule_wake_event.clear()
                            continue
                        start_error = msg_start
                        break
                    if not started:
                        schedules = _get_account_comment_schedules(self.config_path, account_id)
                        idx, current = _find_schedule_record(schedules, schedule_id)
                        if idx >= 0 and isinstance(current, dict):
                            updated = _ensure_schedule_record_defaults(current, default_timezone=str(current.get("timezone", "")).strip() or "UTC", touch=True)
                            updated["last_run_at"] = _now_utc_iso()
                            updated["last_result"] = start_error or "Skipped."
                            updated["last_status"] = "error"
                            if str(updated.get("type", "")).strip() in {"one_time", "specific_datetime"}:
                                updated["enabled"] = False
                            schedules[idx] = updated
                            _save_account_comment_schedules(self.config_path, account_id, schedules)
                        with self.lock:
                            self.comment_schedule_last_run_at = _now_utc_iso()
                            self.comment_schedule_last_result = start_error or "Skipped."
                            self.comment_schedule_status = "running"
                            self.comment_schedule_message = "Waiting for next comment schedule."
                        continue
                    while not self.comment_schedule_stop_event.is_set():
                        with self.lock:
                            active = self.commenter_thread is not None and self.commenter_thread.is_alive()
                        if not active:
                            break
                        self.comment_schedule_wake_event.wait(0.5)
                        self.comment_schedule_wake_event.clear()
                    if self.comment_schedule_stop_event.is_set():
                        with self.lock:
                            if self.commenter_thread is not None and self.commenter_account == account_id:
                                self.stop_commenter(account_id)
                        break
                    with self.lock:
                        run_result = self.commenter_message or "success"
                    run_status = "success" if run_result == "success" else "error"
                    schedules = _get_account_comment_schedules(self.config_path, account_id)
                    idx, current = _find_schedule_record(schedules, schedule_id)
                    if idx >= 0 and isinstance(current, dict):
                        updated = _ensure_schedule_record_defaults(current, default_timezone=str(current.get("timezone", "")).strip() or "UTC", touch=True)
                        updated["last_run_at"] = _now_utc_iso()
                        updated["last_result"] = run_result
                        updated["last_status"] = run_status
                        if str(updated.get("type", "")).strip() in {"one_time", "specific_datetime"}:
                            updated["enabled"] = False
                        schedules[idx] = updated
                        _save_account_comment_schedules(self.config_path, account_id, schedules)
                    with self.lock:
                        self.comment_schedule_last_run_at = _now_utc_iso()
                        self.comment_schedule_last_result = run_result
                        self.comment_schedule_status = "running"
                        self.comment_schedule_message = "Waiting for next comment schedule."
            except Exception as exc:
                with self.lock:
                    self.comment_schedule_status = "error"
                    self.comment_schedule_message = f"{type(exc).__name__}: {exc}"
                    self.comment_schedule_next_run_at = ""
            finally:
                with self.lock:
                    self.comment_schedule_thread = None
                    if self.comment_schedule_status != "error":
                        self.comment_schedule_status = "stopped"
                        self.comment_schedule_message = "Comment scheduler stopped."
                        self.comment_schedule_next_run_at = ""

        self.comment_schedule_thread = threading.Thread(
            target=_comment_schedule_worker,
            name="web-ui-comment-scheduler-loop",
            daemon=True,
        )
        self.comment_schedule_thread.start()

    def snapshot_runner(self, selected_account: str | None = None) -> dict[str, Any]:
        self._sync_runner_locked()
        is_active = self.runner_thread is not None and self.runner_thread.is_alive()
        target = (selected_account or "").strip()
        is_selected = bool(target and target == self.runner_account)
        return {
            "is_active": is_active,
            "status": self.runner_status,
            "account_id": self.runner_account,
            "mode": self.runner_mode,
            "is_selected_account": is_selected,
            "message": self.runner_message,
        }

    def snapshot_schedule(self, selected_account: str | None = None) -> dict[str, Any]:
        self._sync_runner_locked()
        is_active = self.schedule_thread is not None and self.schedule_thread.is_alive()
        target = (selected_account or "").strip()
        is_selected = bool(target and target == self.schedule_account)
        account_schedules = _get_account_schedules(self.config_path, target)
        ui_records = [_to_schedule_ui_record(item) for item in account_schedules]
        ui_records.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
        return {
            "is_active": is_active,
            "status": self.schedule_status,
            "account_id": self.schedule_account,
            "is_selected_account": is_selected,
            "message": self.schedule_message,
            "next_run_at": self.schedule_next_run_at,
            "last_run_at": self.schedule_last_run_at,
            "last_result": self.schedule_last_result,
            "spec": self.schedule_spec,
            "dry_run": self.schedule_dry_run,
            "records": ui_records,
        }

    def start_run(self, account_id: str, *, live: bool) -> tuple[bool, str]:
        self._sync_runner_locked()
        if self.runner_thread is not None:
            return False, f"Another run is in progress for '{self.runner_account}'."

        account = account_id.strip()
        if not account:
            return False, "Account id is required."

        mode_label = "live" if live else "dry"
        self.pause_event.clear()
        self.stop_event.clear()
        self.runner_account = account
        self.runner_mode = mode_label
        self.runner_status = "running"
        self.runner_message = ""

        def _worker() -> None:
            set_log_source("poster")
            error_message = ""
            try:
                with _account_env(account):
                    run_scheduler(
                        self.config_path,
                        run_once=True,
                        force_dry_run=not live,
                        pause_event=self.pause_event,
                        stop_event=self.stop_event,
                    )
            except Exception as exc:  # pragma: no cover - defensive
                error_message = f"{type(exc).__name__}: {exc}"
            finally:
                with self.lock:
                    self._sync_runner_locked()
                    if error_message:
                        self.runner_message = error_message

        thread = threading.Thread(target=_worker, name=f"web-ui-runner-{account}", daemon=True)
        self.runner_thread = thread
        thread.start()
        return True, f"Started {mode_label} run for '{account}'."

    def pause_run(self, account_id: str) -> tuple[bool, str]:
        self._sync_runner_locked()
        if self.runner_thread is None:
            return False, "No active run to pause."
        if self.runner_account != account_id:
            return False, f"Run is active for '{self.runner_account}'."
        if self.runner_status == "paused":
            return True, "Run is already paused."
        self.pause_event.set()
        self.runner_status = "paused"
        return True, "Run paused."

    def resume_run(self, account_id: str) -> tuple[bool, str]:
        self._sync_runner_locked()
        if self.runner_thread is None:
            return False, "No active run to resume."
        if self.runner_account != account_id:
            return False, f"Run is active for '{self.runner_account}'."
        if self.runner_status != "paused":
            return False, "Run is not paused."
        self.pause_event.clear()
        self.runner_status = "running"
        return True, "Run resumed."

    def stop_run(self, account_id: str) -> tuple[bool, str]:
        self._sync_runner_locked()
        if self.runner_thread is None:
            return False, "No active run to stop."
        if self.runner_account != account_id:
            return False, f"Run is active for '{self.runner_account}'."
        self.stop_event.set()
        self.pause_event.clear()
        self.runner_status = "stopping"
        return True, "Stop requested. Waiting for current step to finish."

    def start_schedule(self, account_id: str, schedule_rules: dict[str, Any]) -> tuple[bool, str]:
        account = account_id.strip()
        if not account:
            return False, "Account id is required."

        effective_cfg = load_config(self.config_path, account)
        browser_cfg = effective_cfg.get("browser", {}) if isinstance(effective_cfg.get("browser"), dict) else {}
        default_tz = str(browser_cfg.get("timezone", "UTC")).strip() or "UTC"
        ok, error, normalized = _normalize_schedule_rules(schedule_rules, default_tz)
        if not ok:
            return False, error
        schedule_record = _ensure_schedule_record_defaults(normalized, default_timezone=default_tz, touch=True)
        schedule_record["id"] = _new_schedule_id()

        schedules = _get_account_schedules(self.config_path, account)
        schedules.append(schedule_record)
        if not _save_account_schedules(self.config_path, account, schedules):
            return False, "Failed to save schedule config."

        with self.lock:
            self.schedule_account = account
            self.schedule_spec = dict(schedule_record)
            self.schedule_dry_run = bool(schedule_record.get("dry_run", False))
            self.schedule_status = "running"
            self.schedule_message = f"Schedule '{schedule_record['id']}' saved."
            next_run = _compute_next_run_utc(schedule_record)
            self.schedule_next_run_at = next_run.isoformat() if next_run else ""
            self._mark_schedule_changed_locked()

        return True, f"Schedule '{schedule_record['id']}' saved for '{account}'."

    def stop_schedule(self, account_id: str, schedule_id: str = "") -> tuple[bool, str]:
        account = account_id.strip()
        if not account:
            return False, "Account id is required."

        schedules = _get_account_schedules(self.config_path, account)
        if not schedules:
            return False, "No schedules found for this account."

        target_id = schedule_id.strip()
        changed = 0
        if target_id:
            idx, current = _find_schedule_record(schedules, target_id)
            if idx < 0 or not isinstance(current, dict):
                return False, f"Schedule '{target_id}' not found."
            updated = _ensure_schedule_record_defaults(
                current,
                default_timezone=str(current.get("timezone", "")).strip() or "UTC",
                touch=True,
            )
            if bool(updated.get("enabled", True)):
                updated["enabled"] = False
                updated["last_status"] = "stopped"
                schedules[idx] = updated
                changed = 1
        else:
            for idx, current in enumerate(schedules):
                if not isinstance(current, dict):
                    continue
                updated = _ensure_schedule_record_defaults(
                    current,
                    default_timezone=str(current.get("timezone", "")).strip() or "UTC",
                    touch=True,
                )
                if bool(updated.get("enabled", True)):
                    updated["enabled"] = False
                    updated["last_status"] = "stopped"
                    schedules[idx] = updated
                    changed += 1
            if changed == 0:
                return False, "No enabled schedules to stop."

        if not _save_account_schedules(self.config_path, account, schedules):
            return False, "Failed to update schedule config."

        with self.lock:
            if target_id:
                self.schedule_message = f"Schedule '{target_id}' disabled."
            else:
                self.schedule_message = f"Disabled {changed} schedule(s) for '{account}'."
            if self.runner_thread is not None and self.runner_account == account:
                self.stop_event.set()
                self.pause_event.clear()
            self._mark_schedule_changed_locked()

        return True, self.schedule_message

    def delete_schedule(self, account_id: str, schedule_id: str) -> tuple[bool, str]:
        account = account_id.strip()
        if not account:
            return False, "Account id is required."
        target_id = schedule_id.strip()
        if not target_id:
            return False, "Schedule id is required."

        schedules = _get_account_schedules(self.config_path, account)
        if not schedules:
            return False, "No schedules found for this account."

        idx, _ = _find_schedule_record(schedules, target_id)
        if idx < 0:
            return False, f"Schedule '{target_id}' not found."

        del schedules[idx]
        if not _save_account_schedules(self.config_path, account, schedules):
            return False, "Failed to delete schedule config."

        with self.lock:
            self.schedule_message = f"Schedule '{target_id}' deleted."
            self._mark_schedule_changed_locked()

        return True, self.schedule_message

    def enable_schedule(self, account_id: str, schedule_id: str) -> tuple[bool, str]:
        account = account_id.strip()
        if not account:
            return False, "Account id is required."
        target_id = schedule_id.strip()
        if not target_id:
            return False, "Schedule id is required."
        schedules = _get_account_schedules(self.config_path, account)
        if not schedules:
            return False, "No schedules found."
        idx, current = _find_schedule_record(schedules, target_id)
        if idx < 0 or not isinstance(current, dict):
            return False, f"Schedule '{target_id}' not found."
        updated = _ensure_schedule_record_defaults(
            current, default_timezone=str(current.get("timezone", _DEFAULT_TZ)) or _DEFAULT_TZ, touch=True
        )
        updated["enabled"] = True
        updated["last_status"] = "pending"
        schedules[idx] = updated
        if not _save_account_schedules(self.config_path, account, schedules):
            return False, "Failed to update schedule config."
        with self.lock:
            self.schedule_message = f"Schedule '{target_id}' enabled."
            self._mark_schedule_changed_locked()
        return True, self.schedule_message

    def start_comment_schedule(self, account_id: str, schedule_rules: dict[str, Any]) -> tuple[bool, str]:
        account = account_id.strip()
        if not account:
            return False, "Account id is required."
        effective_cfg = load_config(self.config_path, account)
        browser_cfg = effective_cfg.get("browser", {}) if isinstance(effective_cfg.get("browser"), dict) else {}
        default_tz = str(browser_cfg.get("timezone", "UTC")).strip() or "UTC"
        rules = {k: v for k, v in (schedule_rules or {}).items() if k != "dry_run"}
        ok, error, normalized = _normalize_schedule_rules(rules, default_tz)
        if not ok:
            return False, error
        schedule_record = _ensure_schedule_record_defaults(normalized, default_timezone=default_tz, touch=True)
        schedule_record["id"] = _new_schedule_id()
        schedules = _get_account_comment_schedules(self.config_path, account)
        schedules.append(schedule_record)
        if not _save_account_comment_schedules(self.config_path, account, schedules):
            return False, "Failed to save comment schedule config."
        with self.lock:
            self.comment_schedule_account = account
            self.comment_schedule_spec = dict(schedule_record)
            self.comment_schedule_status = "running"
            self.comment_schedule_message = f"Comment schedule '{schedule_record['id']}' saved."
            next_run = _compute_next_run_utc(schedule_record)
            self.comment_schedule_next_run_at = next_run.isoformat() if next_run else ""
            self._ensure_comment_schedule_thread_locked()
        return True, f"Comment schedule '{schedule_record['id']}' saved for '{account}'."

    def stop_comment_schedule(self, account_id: str, schedule_id: str = "") -> tuple[bool, str]:
        account = account_id.strip()
        if not account:
            return False, "Account id is required."
        schedules = _get_account_comment_schedules(self.config_path, account)
        if not schedules:
            return False, "No comment schedules found for this account."
        target_id = schedule_id.strip()
        changed = 0
        if target_id:
            idx, current = _find_schedule_record(schedules, target_id)
            if idx < 0 or not isinstance(current, dict):
                return False, f"Comment schedule '{target_id}' not found."
            updated = _ensure_schedule_record_defaults(current, default_timezone=str(current.get("timezone", "")).strip() or "UTC", touch=True)
            if bool(updated.get("enabled", True)):
                updated["enabled"] = False
                updated["last_status"] = "stopped"
                schedules[idx] = updated
                changed = 1
        else:
            for idx, current in enumerate(schedules):
                if not isinstance(current, dict):
                    continue
                updated = _ensure_schedule_record_defaults(current, default_timezone=str(current.get("timezone", "")).strip() or "UTC", touch=True)
                if bool(updated.get("enabled", True)):
                    updated["enabled"] = False
                    updated["last_status"] = "stopped"
                    schedules[idx] = updated
                    changed += 1
            if changed == 0:
                return False, "No enabled comment schedules to stop."
        if not _save_account_comment_schedules(self.config_path, account, schedules):
            return False, "Failed to update comment schedule config."
        with self.lock:
            self.comment_schedule_message = f"Stopped {changed} comment schedule(s)."
            self.comment_schedule_wake_event.set()
        return True, self.comment_schedule_message

    def delete_comment_schedule(self, account_id: str, schedule_id: str) -> tuple[bool, str]:
        account = account_id.strip()
        if not account:
            return False, "Account id is required."
        target_id = schedule_id.strip()
        if not target_id:
            return False, "Schedule id is required."
        schedules = _get_account_comment_schedules(self.config_path, account)
        if not schedules:
            return False, "No comment schedules found for this account."
        idx, _ = _find_schedule_record(schedules, target_id)
        if idx < 0:
            return False, f"Comment schedule '{target_id}' not found."
        del schedules[idx]
        if not _save_account_comment_schedules(self.config_path, account, schedules):
            return False, "Failed to delete comment schedule config."
        with self.lock:
            self.comment_schedule_message = f"Comment schedule '{target_id}' deleted."
            self.comment_schedule_wake_event.set()
        return True, self.comment_schedule_message

    def enable_comment_schedule(self, account_id: str, schedule_id: str) -> tuple[bool, str]:
        account = account_id.strip()
        if not account:
            return False, "Account id is required."
        target_id = schedule_id.strip()
        if not target_id:
            return False, "Schedule id is required."
        schedules = _get_account_comment_schedules(self.config_path, account)
        if not schedules:
            return False, "No comment schedules found."
        idx, current = _find_schedule_record(schedules, target_id)
        if idx < 0 or not isinstance(current, dict):
            return False, f"Comment schedule '{target_id}' not found."
        updated = _ensure_schedule_record_defaults(
            current, default_timezone=str(current.get("timezone", _DEFAULT_TZ)) or _DEFAULT_TZ, touch=True
        )
        updated["enabled"] = True
        updated["last_status"] = "pending"
        schedules[idx] = updated
        if not _save_account_comment_schedules(self.config_path, account, schedules):
            return False, "Failed to update comment schedule config."
        with self.lock:
            self.comment_schedule_message = f"Comment schedule '{target_id}' enabled."
            self.comment_schedule_wake_event.set()
        return True, self.comment_schedule_message

    def snapshot_comment_schedule(self, selected_account: str | None = None) -> dict[str, Any]:
        is_active = self.comment_schedule_thread is not None and self.comment_schedule_thread.is_alive()
        target = (selected_account or "").strip()
        is_selected = bool(target and target == self.comment_schedule_account)
        account_schedules = _get_account_comment_schedules(self.config_path, target)
        ui_records = [_to_schedule_ui_record(item) for item in account_schedules]
        ui_records.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
        return {
            "is_active": is_active,
            "status": self.comment_schedule_status,
            "account_id": self.comment_schedule_account,
            "is_selected_account": is_selected,
            "message": self.comment_schedule_message,
            "next_run_at": self.comment_schedule_next_run_at,
            "last_run_at": self.comment_schedule_last_run_at,
            "last_result": self.comment_schedule_last_result,
            "spec": self.comment_schedule_spec,
            "records": ui_records,
        }

    def start_commenter(self, account_id: str) -> tuple[bool, str]:
        self._sync_runner_locked()
        if self.commenter_thread is not None:
            return False, f"Auto commenter is already running for '{self.commenter_account}'."

        account = account_id.strip()
        if not account:
            return False, "Account id is required."

        self.commenter_stop_event.clear()
        self.commenter_pause_event.clear()
        self.commenter_account = account
        self.commenter_status = "running"
        self.commenter_message = ""

        def _commenter_worker() -> None:
            set_log_source("commenter")
            error_message = ""
            try:
                with _account_env(account):
                    run_auto_commenter(
                        self.config_path,
                        stop_event=self.commenter_stop_event,
                        pause_event=self.commenter_pause_event,
                    )
            except Exception as exc:
                error_message = f"{type(exc).__name__}: {exc}"
            finally:
                with self.lock:
                    self._sync_runner_locked()
                    if error_message:
                        self.commenter_message = error_message
                        self.commenter_status = "error"

        thread = threading.Thread(
            target=_commenter_worker,
            name=f"web-ui-commenter-{account}",
            daemon=True,
        )
        self.commenter_thread = thread
        thread.start()
        return True, f"Auto commenter started for '{account}'."

    def stop_commenter(self, account_id: str) -> tuple[bool, str]:
        self._sync_runner_locked()
        if self.commenter_thread is None:
            return False, "No active commenter to stop."
        account = account_id.strip()
        if account and self.commenter_account != account:
            return False, f"Commenter is running for '{self.commenter_account}'."
        self.commenter_stop_event.set()
        self.commenter_pause_event.clear()
        self.commenter_status = "stopping"
        return True, "Commenter stop requested. Waiting for current step to finish."

    def snapshot_commenter(self, selected_account: str | None = None) -> dict[str, Any]:
        self._sync_runner_locked()
        is_active = self.commenter_thread is not None and self.commenter_thread.is_alive()
        target = (selected_account or "").strip()
        is_selected = bool(target and target == self.commenter_account)
        return {
            "is_active": is_active,
            "status": self.commenter_status,
            "account_id": self.commenter_account,
            "is_selected_account": is_selected,
            "message": self.commenter_message,
        }


def _build_state(
  config_path: Path,
  selected_account: str | None = None,
  runner_state: dict[str, Any] | None = None,
  schedule_state: dict[str, Any] | None = None,
  commenter_state: dict[str, Any] | None = None,
  comment_schedule_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    accounts = list_accounts(config_path)
    active = get_active_account(config_path) or ""
    preset_status = get_preset_status(config_path)
    preset_details = preset_status.get("details") if isinstance(preset_status.get("details"), dict) else {}
    preset_account = str(preset_details.get("account", "")).strip() if isinstance(preset_details, dict) else ""
    if bool(preset_status.get("enabled")) and preset_account:
        account_id = preset_account
    else:
        account_id = selected_account or active or (next(iter(accounts.keys()), ""))
    groups = list_groups_for_account(config_path, account_id) if account_id else []
    try:
        effective_cfg = load_config(config_path, account_id or None)
    except (PermissionError, ValueError):
        # Account is disabled or not found — use lenient loader that skips enabled check
        effective_cfg = load_config_lenient(config_path, account_id or None)

    account_items: list[dict[str, Any]] = []
    for aid, override in accounts.items():
        account_items.append({
            "id": aid,
            "enabled": bool(override.get("enabled", True)),
            "is_active": aid == active,
        })

    group_items: list[dict[str, Any]] = []
    for g in groups:
        gid = str(g.get("id", "")).strip()
        group_items.append({
            "id": gid,
            "name": str(g.get("name", "")),
            "url": str(g.get("url", "")),
            "included": bool(g.get("active", True)),
            "comment_active": bool(g.get("comment_active", False)),
        })

    posting_cfg = dict(effective_cfg.get("posting", {})) if isinstance(effective_cfg.get("posting"), dict) else {}
    schedule_records = _get_account_schedules(config_path, account_id)
    schedule_records_ui = [_to_schedule_ui_record(item) for item in schedule_records]
    schedule_records_ui.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
    latest_schedule = schedule_records_ui[0] if schedule_records_ui else {}

    templates: list[dict[str, Any]] = []
    for tpl in load_templates(Path("templates")):
        template_file = str(tpl.get("template_file", "")).strip()
        if not template_file:
            continue
        text_value = str(tpl.get("text", "")).strip()
        tags_value = tpl.get("tags", [])
        tags: list[str] = []
        if isinstance(tags_value, list):
            tags = [str(tag).strip() for tag in tags_value if str(tag).strip()]
        image_items: list[str] = []
        single_image = str(tpl.get("image", "")).strip()
        if single_image:
            image_items.append(single_image)
        images_value = tpl.get("images", [])
        if isinstance(images_value, list):
            image_items.extend(str(img).strip() for img in images_value if str(img).strip())

        title = str(tpl.get("title", "")).strip() or template_file
        templates.append({
            "template_file": template_file,
            "title": title,
            "text": text_value,
            "tags": tags,
            "images": image_items,
        })

    presets_list = list_presets()

    return {
        "active_account": active,
        "selected_account": account_id,
        "accounts": account_items,
        "groups": group_items,
        "posting": posting_cfg,
      "global_groups": dict(effective_cfg.get("groups", {})) if isinstance(effective_cfg.get("groups"), dict) else {},
      "global_posting": {
        **(dict(effective_cfg.get("posting", {})) if isinstance(effective_cfg.get("posting"), dict) else {}),
        **{k: v for k, v in (dict(effective_cfg.get("error_handling", {})) if isinstance(effective_cfg.get("error_handling"), dict) else {}).items() if k in ("pause_on_captcha", "stop_on_rate_limit")},
      },
        "browser": dict(effective_cfg.get("browser", {})) if isinstance(effective_cfg.get("browser"), dict) else {},
        "templates": templates,
      "run_control": runner_state or {
        "is_active": False,
        "status": "idle",
        "account_id": "",
        "mode": "",
        "is_selected_account": False,
        "message": "",
      },
      "schedule_control": schedule_state or {
        "is_active": False,
        "status": "idle",
        "account_id": "",
        "is_selected_account": False,
        "message": "",
        "next_run_at": "",
        "last_run_at": "",
        "last_result": "",
        "spec": {},
        "dry_run": False,
        "records": schedule_records_ui,
      },
      "preset": preset_status,
      "presets": presets_list,
      "saved_schedule": latest_schedule,
      "saved_schedules": schedule_records_ui,
      "global_commenting": dict(effective_cfg.get("commenting", {})) if isinstance(effective_cfg.get("commenting"), dict) else {},
      "commenter_control": commenter_state or {
        "is_active": False,
        "status": "idle",
        "account_id": "",
        "is_selected_account": False,
        "message": "",
      },
      "comment_schedule_control": comment_schedule_state or {
        "is_active": False,
        "status": "idle",
        "account_id": "",
        "is_selected_account": False,
        "message": "",
        "next_run_at": "",
        "last_run_at": "",
        "last_result": "",
        "spec": {},
        "records": [_to_schedule_ui_record(item) for item in _get_account_comment_schedules(config_path, account_id)],
      },
    }


def _update_browser_rules(config_path: Path, browser_rules: dict[str, Any]) -> tuple[bool, str]:
    config = load_raw_config(config_path)
    browser_cfg = dict(config.get("browser", {})) if isinstance(config.get("browser"), dict) else {}

    bool_fields = {"headless", "humanize", "fullscreen"}
    int_fields = {"screen_max_width", "screen_max_height", "window_width", "window_height"}
    str_fields = {"locale", "timezone"}

    for key in bool_fields:
        if key in browser_rules:
            browser_cfg[key] = bool(browser_rules.get(key))

    for key in int_fields:
        if key in browser_rules:
            try:
                value = int(browser_rules.get(key))
            except Exception:
                return False, f"Invalid integer for '{key}'."
            if value <= 0:
                return False, f"'{key}' must be greater than 0."
            browser_cfg[key] = value

    for key in str_fields:
        if key in browser_rules:
            value = str(browser_rules.get(key, "")).strip()
            if not value:
                return False, f"'{key}' cannot be empty."
            browser_cfg[key] = value

    config["browser"] = browser_cfg
    save_raw_config(config_path, config)
    return True, "Global browser rules updated."


def _update_groups_rules(config_path: Path, groups_rules: dict[str, Any]) -> tuple[bool, str]:
    config = load_raw_config(config_path)
    groups_cfg = dict(config.get("groups", {})) if isinstance(config.get("groups"), dict) else {}

    int_fields = {
        "rescrape_every_days",
        "idle_rounds_to_stop",
        "scroll_wait_ms",
    }

    scrape_cfg = dict(groups_cfg.get("scrape", {})) if isinstance(groups_cfg.get("scrape"), dict) else {}
    for key in int_fields:
        if key not in groups_rules:
            continue
        try:
            value = int(groups_rules.get(key))
        except Exception:
            return False, f"Invalid integer for '{key}'."
        if value < 0:
            return False, f"'{key}' must be 0 or greater."
        if key == "rescrape_every_days":
            groups_cfg[key] = max(1, value)
        else:
            scrape_cfg[key] = value

    groups_cfg["scrape"] = scrape_cfg
    config["groups"] = groups_cfg
    save_raw_config(config_path, config)
    return True, "Global group rules updated."


def _update_posting_rules(config_path: Path, posting_rules: dict[str, Any]) -> tuple[bool, str]:
    config = load_raw_config(config_path)
    posting_cfg = dict(config.get("posting", {})) if isinstance(config.get("posting"), dict) else {}

    int_fields = {
        "min_delay_minutes",
        "max_delay_minutes",
        "rest_every_n_posts",
        "rest_duration_minutes",
    }
    bool_fields = {"dry_run", "auto_skip"}
    error_bool_fields = {"pause_on_captcha", "stop_on_rate_limit"}
    str_fields = {"template_file"}

    for key in int_fields:
        if key not in posting_rules:
            continue
        try:
            value = int(posting_rules.get(key))
        except Exception:
            return False, f"Invalid integer for '{key}'."
        if value < 0:
            return False, f"'{key}' must be 0 or greater."
        posting_cfg[key] = value

    for key in bool_fields:
        if key in posting_rules:
            posting_cfg[key] = bool(posting_rules.get(key))

    for key in str_fields:
        if key in posting_rules:
            value = str(posting_rules.get(key, "")).strip()
            if not value:
                return False, f"'{key}' cannot be empty."
            posting_cfg[key] = value

    min_delay = int(posting_cfg.get("min_delay_minutes", 0))
    max_delay = int(posting_cfg.get("max_delay_minutes", 0))
    if min_delay > max_delay:
        return False, "'min_delay_minutes' cannot be greater than 'max_delay_minutes'."

    config["posting"] = posting_cfg

    error_cfg = dict(config.get("error_handling", {})) if isinstance(config.get("error_handling"), dict) else {}
    for key in error_bool_fields:
        if key in posting_rules:
            error_cfg[key] = bool(posting_rules.get(key))
    config["error_handling"] = error_cfg

    save_raw_config(config_path, config)
    return True, "Global posting rules updated."


def _update_commenting_rules(config_path: Path, commenting_rules: dict[str, Any]) -> tuple[bool, str]:
    config = load_raw_config(config_path)
    commenting_cfg = dict(config.get("commenting", {})) if isinstance(config.get("commenting"), dict) else {}

    int_fields = {"min_delay_minutes", "max_delay_minutes"}
    str_fields = {"template_file"}

    for key in int_fields:
        if key not in commenting_rules:
            continue
        try:
            value = int(commenting_rules.get(key))
        except Exception:
            return False, f"Invalid integer for '{key}'."
        if value < 0:
            return False, f"'{key}' must be 0 or greater."
        commenting_cfg[key] = value

    for key in str_fields:
        if key in commenting_rules:
            value = str(commenting_rules.get(key, "")).strip()
            if not value:
                return False, f"'{key}' cannot be empty."
            commenting_cfg[key] = value

    min_delay = int(commenting_cfg.get("min_delay_minutes", 0))
    max_delay = int(commenting_cfg.get("max_delay_minutes", 0))
    if min_delay > max_delay:
        return False, "'min_delay_minutes' cannot be greater than 'max_delay_minutes'."

    config["commenting"] = commenting_cfg
    save_raw_config(config_path, config)
    return True, "Comment settings updated."


def _normalize_template_data(template_data: dict[str, Any]) -> dict[str, Any]:
    title = str(template_data.get("title", "")).strip()
    text = str(template_data.get("text", "")).strip()

    tags_raw = template_data.get("tags", [])
    tags: list[str] = []
    if isinstance(tags_raw, list):
        tags = [str(tag).strip() for tag in tags_raw if str(tag).strip()]
    elif isinstance(tags_raw, str):
        tags = [item.strip() for item in tags_raw.split(",") if item.strip()]

    images_raw = template_data.get("images", [])
    images: list[str] = []
    if isinstance(images_raw, list):
        images = [str(path).strip() for path in images_raw if str(path).strip()]
    elif isinstance(images_raw, str):
        images = [item.strip() for item in images_raw.splitlines() if item.strip()]

    payload: dict[str, Any] = {
        "title": title,
        "text": text,
        "images": images,
        "tags": tags,
    }
    return payload


def _next_template_filename(template_dir: Path) -> str:
    used_indices: set[int] = set()
    for path in template_dir.glob("post_*.yaml"):
        stem = path.stem
        if not stem.startswith("post_"):
            continue
        suffix = stem[5:]
        if suffix.isdigit():
            used_indices.add(int(suffix))

    next_index = 1
    while next_index in used_indices:
        next_index += 1
    return f"post_{next_index}.yaml"


def _save_template_file(template_path: Path, template_data: dict[str, Any]) -> None:
    template_path.parent.mkdir(parents=True, exist_ok=True)
    with template_path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(template_data, f, sort_keys=False, allow_unicode=True)


_ACTIVITY_LOG_PREFIXES = (
    "state_enter:",
    "state_exit:",
    "popup_textbox_found",
    "popup_post_button_enabled",
    "popup_post_clicked",
)

_MESSAGE_RENAME_MAP = {
    "popup_flow_failed": "Failed to open post composer",
}

_CONTEXT_LABEL_MAP = {
    "group": "Group",
    "text_length": "Text length",
    "image_count": "Images",
    "image_path": "Image",
    "reason": "Reason",
    "skipped": "Skipped",
    "attempt": "Attempt",
    "error_type": "Error",
    "error": "Detail",
    "account": "Account",
    "template": "Template",
    "removed_groups": "Removed",
    "removed_count": "Removed",
    "scraped_count": "Scraped",
    "merged_count": "Total",
}


_CONTEXT_SKIP_KEYS = {"url", "error_raw", "traceback", "groups_path", "fallback_url"}


def _format_context(context: dict) -> str:
    parts = []
    for key in _CONTEXT_LABEL_MAP:
        if key in context:
            val = context[key]
            if val is None or val == "":
                continue
            label = _CONTEXT_LABEL_MAP[key]
            parts.append(f"{label}: {val}")
    for key, val in context.items():
        if key not in _CONTEXT_LABEL_MAP and key not in _CONTEXT_SKIP_KEYS and val is not None and val != "":
            parts.append(f"{key}: {val}")
    return "  —  ".join(parts)


def _is_activity_log(message: str) -> bool:
    msg_lower = message.lower()
    for prefix in _ACTIVITY_LOG_PREFIXES:
        if msg_lower.startswith(prefix.lower()):
            return True
    return False


def _read_live_logs(limit: int = 200) -> list[dict[str, str]]:
  log_path = get_log_file()
  if not log_path.exists():
    return []

  capped_limit = max(20, min(int(limit), 500))
  rows: deque[dict[str, str]] = deque(maxlen=capped_limit)

  try:
    with log_path.open("r", encoding="utf-8") as f:
      for raw_line in f:
        line = raw_line.strip()
        if not line:
          continue
        try:
          payload = json.loads(line)
          timestamp = str(payload.get("timestamp", "")).strip()
          level = str(payload.get("level", "INFO")).strip().upper() or "INFO"
          message = str(payload.get("message", "")).strip()
          if _is_activity_log(message):
            continue
          message = _MESSAGE_RENAME_MAP.get(message, message)
          context = payload.get("context")
          if isinstance(context, dict) and context:
            ctx_str = _format_context(context)
            if ctx_str:
              message = f"{message}  —  {ctx_str}"
          source = str(payload.get("source", "")).strip()
          rows.append({
            "timestamp": timestamp,
            "level": level,
            "message": message,
            "source": source,
          })
        except Exception:
          rows.append({
            "timestamp": "",
            "level": "INFO",
            "message": line,
            "source": "",
          })
  except Exception:
    return []

  return list(rows)


def _render_page() -> str:
    return r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>FBPost — Control Panel</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;600;700;800;900&family=Plus+Jakarta+Sans:wght@400;500;600&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg:        #FFFDF5;
      --fg:        #1E293B;
      --muted:     #F1F5F9;
      --muted-fg:  #64748B;
      --card:      #FFFFFF;
      --border:    #E2E8F0;
      --accent:    #8B5CF6;
      --pink:      #F472B6;
      --yellow:    #FBBF24;
      --green:     #34D399;
      --red:       #F87171;

      --pop:   4px 4px 0px 0px var(--fg);
      --lift:  6px 6px 0px 0px var(--fg);
      --press: 2px 2px 0px 0px var(--fg);

      --r-sm:   8px;
      --r-md:   16px;
      --r-lg:   24px;
      --r-pill: 9999px;

      --heading: 'Outfit', system-ui, sans-serif;
      --body:    'Plus Jakarta Sans', system-ui, sans-serif;
      --bounce:  cubic-bezier(0.34, 1.56, 0.64, 1);
    }

    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

    body {
      font-family: var(--body);
      font-size: 14px;
      color: var(--fg);
      background-color: var(--bg);
      background-image: radial-gradient(circle, #CBD5E1 1.2px, transparent 1.2px);
      background-size: 22px 22px;
      min-height: 100vh;
    }

    /* ── Floating deco shapes ───────────── */
    .deco { position: fixed; inset: 0; pointer-events: none; z-index: 0; overflow: hidden; }
    .deco-shape { position: absolute; opacity: .10; }
    .ds1 { width:320px;height:320px; background:var(--yellow); border-radius:50%; top:-90px; left:-70px; }
    .ds2 { width:190px;height:190px; background:var(--pink);   top:50px; right:160px; transform:rotate(18deg); }
    .ds3 { width:140px;height:140px; background:var(--accent); border-radius:50%; bottom:12%; left:6%; }
    .ds4 { width:240px;height:240px; background:var(--green);  border-radius:40% 60% 60% 40%; bottom:-60px; right:-50px; }

    /* ── Page wrapper ───────────────────── */
    .page {
      position: relative; z-index: 1;
      width: min(1380px, 97vw);
      margin: 0 auto;
      padding: 26px 0 60px;
      display: flex;
      flex-direction: column;
      gap: 20px;
    }

    /* ── Header ─────────────────────────── */
    .header {
      background: var(--card);
      border: 2px solid var(--fg);
      border-radius: var(--r-lg);
      padding: 14px 24px;
      box-shadow: 8px 8px 0 var(--border);
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 14px;
      flex-wrap: wrap;
    }

    .logo-row { display: flex; align-items: center; gap: 13px; }
    .logo-icon {
      width: 46px; height: 46px;
      background: var(--accent);
      border: 2px solid var(--fg);
      border-radius: var(--r-md);
      box-shadow: var(--pop);
      display: flex; align-items: center; justify-content: center;
      font-size: 22px; flex-shrink: 0;
    }
    .logo-name { font-family: var(--heading); font-weight: 900; font-size: 22px; letter-spacing: -.4px; }
    .logo-name em { color: var(--accent); font-style: normal; }
    .logo-sub { font-size: 12px; color: var(--muted-fg); font-weight: 500; margin-top: 1px; }

    .live-badge {
      display: inline-flex; align-items: center; gap: 7px;
      background: var(--card);
      border: 2px solid var(--fg);
      border-radius: var(--r-pill);
      padding: 5px 14px;
      font-family: var(--heading);
      font-weight: 700; font-size: 12px;
      box-shadow: var(--pop);
    }
    .live-dot {
      width: 8px; height: 8px; border-radius: 50%;
      background: var(--green);
      animation: blink 2s ease-in-out infinite;
    }
    @keyframes blink { 0%,100%{opacity:1} 50%{opacity:.25} }

    /* ── Grid ───────────────────────────── */
    .main-grid {
      display: grid;
      grid-template-columns: 40% 60%;
      gap: 20px;
      align-items: start;
    }

    /* ── Card ───────────────────────────── */
    .card {
      background: var(--card);
      border: 2px solid var(--fg);
      border-radius: var(--r-lg);
      padding: 22px;
      box-shadow: 8px 8px 0 var(--border);
    }

    .card-title {
      font-family: var(--heading);
      font-weight: 800;
      font-size: 17px;
      display: flex;
      align-items: center;
      gap: 10px;
      margin-bottom: 16px;
    }

    .t-icon {
      width: 30px; height: 30px;
      border: 2px solid var(--fg);
      border-radius: var(--r-sm);
      display: flex; align-items: center; justify-content: center;
      font-size: 15px; flex-shrink: 0;
      box-shadow: 3px 3px 0 var(--fg);
    }
    .ti-violet { background: var(--accent); }
    .ti-pink   { background: var(--pink);   }
    .ti-yellow { background: var(--yellow); }
    .ti-green  { background: var(--green);  }

    /* ── Stat boxes ─────────────────────── */
    .stats {
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 10px;
      margin-bottom: 16px;
    }
    .stat {
      background: var(--muted);
      border: 2px solid var(--fg);
      border-radius: var(--r-md);
      padding: 11px 8px;
      text-align: center;
      box-shadow: 4px 4px 0 var(--fg);
      transition: transform .22s var(--bounce), box-shadow .22s var(--bounce);
      cursor: default;
    }
    .stat:hover { transform: translate(-2px,-2px); box-shadow: var(--lift); }
    .stat-n {
      font-family: var(--heading); font-weight: 900; font-size: 28px;
      line-height: 1; color: var(--accent);
    }
    .stat-l { font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing:.05em; color: var(--muted-fg); margin-top: 2px; }

    /* ── Active badge ───────────────────── */
    .active-badge {
      display: inline-flex; align-items: center; gap: 5px;
      background: var(--yellow);
      border: 2px solid var(--fg);
      border-radius: var(--r-pill);
      padding: 4px 12px;
      font-family: var(--heading); font-weight: 800; font-size: 12px;
      box-shadow: 3px 3px 0 var(--fg);
    }

    /* ── Form row ───────────────────────── */
    .frow { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 12px; align-items: center; }

    input[type=text], input[type=datetime-local], input[type=time], select, textarea {
      height: 40px; padding: 0 13px;
      border: 2px solid var(--fg); border-radius: var(--r-md);
      background: var(--card); color: var(--fg);
      font-family: var(--body); font-size: 13px;
      outline: none;
      box-shadow: 4px 4px 0 transparent;
      transition: box-shadow .18s var(--bounce), border-color .15s;
    }
    input[type=text]:focus, input[type=datetime-local]:focus, input[type=time]:focus, select:focus, textarea:focus {
      border-color: var(--accent);
      box-shadow: 4px 4px 0 var(--accent);
    }
    input[type=text] { font-family: 'Courier New', monospace; font-size: 12px; min-width: 180px; }
    textarea {
      height: auto;
      min-height: 140px;
      padding: 10px 12px;
      resize: vertical;
      width: 100%;
      font-family: var(--body);
      line-height: 1.45;
    }
    select { cursor: pointer; }

    /* ── Buttons ────────────────────────── */
    button {
      display: inline-flex; align-items: center; gap: 5px;
      cursor: pointer;
      font-family: var(--heading); font-weight: 700; font-size: 13px;
      padding: 8px 16px;
      border: 2px solid var(--fg); border-radius: var(--r-pill);
      background: var(--card); color: var(--fg);
      box-shadow: var(--pop);
      transition: transform .22s var(--bounce), box-shadow .22s var(--bounce), background .15s;
      white-space: nowrap;
    }
    button:hover:not(:disabled) { transform: translate(-2px,-2px); box-shadow: var(--lift); }
    button:active:not(:disabled) { transform: translate(2px,2px); box-shadow: var(--press); }
    button:disabled { opacity: .42; cursor: not-allowed; transform: none !important; }

    .btn-primary { background: var(--accent); color: #fff; }
    .btn-primary:hover:not(:disabled) { background: #7c3aed; }
    .btn-green   { background: var(--green); }
    .btn-green:hover:not(:disabled) { background: #10b981; }
    .btn-yellow  { background: var(--yellow); }
    .btn-red     { background: var(--red); color: #fff; }
    .btn-red:hover:not(:disabled) { background: #ef4444; }
    .btn-violet  { background: #7c3aed; color: #fff; }
    .btn-violet:hover:not(:disabled) { background: #6d28d9; }

    .sm-btn {
      padding: 5px 11px; font-size: 12px; border-radius: var(--r-pill);
      box-shadow: 3px 3px 0 var(--fg);
    }
    .sm-btn:hover:not(:disabled) { transform: translate(-1px,-1px); box-shadow: 4px 4px 0 var(--fg); }
    .sm-btn:active:not(:disabled) { transform: translate(1px,1px); box-shadow: 1px 1px 0 var(--fg); }

    /* ── Table ──────────────────────────── */
    .tbl-wrap {
      border: 2px solid var(--fg); border-radius: var(--r-md);
      box-shadow: 4px 4px 0 var(--fg);
      overflow-x: auto;
    }
    table { width: 100%; border-collapse: collapse; }
    thead { background: var(--muted); }
    th {
      padding: 9px 13px; text-align: left;
      font-family: var(--heading); font-size: 11px; font-weight: 800;
      text-transform: uppercase; letter-spacing: .07em; color: var(--muted-fg);
      border-bottom: 2px solid var(--fg); white-space: nowrap;
    }
    td { padding: 9px 13px; border-bottom: 1px solid var(--border); font-size: 13px; vertical-align: middle; }
    tbody tr:last-child td { border-bottom: none; }
    tbody tr:hover td { background: #FFFBEC; }
    .mono { font-family: 'Courier New', monospace; font-size: 12px; color: var(--muted-fg); }

    /* ── Pills ──────────────────────────── */
    .pill {
      display: inline-flex; align-items: center; gap: 4px;
      padding: 3px 9px;
      border: 2px solid var(--fg); border-radius: var(--r-pill);
      font-family: var(--heading); font-weight: 700; font-size: 11px;
      box-shadow: 2px 2px 0 var(--fg);
      white-space: nowrap;
    }
    .p-green  { background: var(--green); }
    .p-orange { background: #FED7AA; }
    .p-violet { background: #EDE9FE; color: #5b21b6; }
    .p-yellow { background: var(--yellow); }
    .p-gray   { background: var(--muted); color: var(--muted-fg); }

    /* ── Toolbar ────────────────────────── */
    .toolbar {
      background: var(--muted);
      border: 2px solid var(--fg); border-radius: var(--r-md);
      padding: 14px 16px; margin-bottom: 20px;
      box-shadow: 4px 4px 0 var(--fg);
    }
    .toolbar-lbl {
      font-family: var(--heading); font-size: 11px; font-weight: 800;
      text-transform: uppercase; letter-spacing: .07em; color: var(--muted-fg);
      margin-bottom: 10px;
    }
    .toolbar-btns { display: flex; flex-wrap: wrap; gap: 8px; justify-content: center; }

    .pager {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      margin-top: 12px;
    }
    .pager-info {
      font-family: var(--heading);
      font-size: 12px;
      font-weight: 700;
      color: var(--muted-fg);
    }

    .settings-box {
      margin-top: 14px;
      border: 2px solid var(--fg);
      border-radius: var(--r-md);
      background: var(--muted);
      padding: 12px;
      box-shadow: 4px 4px 0 var(--fg);
    }
    .log-wrap {
      max-height: 220px;
      overflow: auto;
    }
    .log-table th, .log-table td {
      padding: 7px 9px;
      font-size: 11px;
    }
    .auto-scroll-ctrl {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      font-family: var(--heading);
      font-size: 11px;
      font-weight: 700;
      color: var(--muted-fg);
    }
    .field {
      display: flex;
      flex-direction: column;
      gap: 6px;
      min-width: 180px;
      flex: 1;
    }
    .preset-inline {
      display: flex;
      align-items: center;
      gap: 8px;
      margin-bottom: 0;
      flex-wrap: nowrap;
    }
    .preset-inline > select {
      flex: 1 1 auto;
      min-width: 150px;
    }
    .preset-inline > button {
      padding: 8px 10px;
      min-width: 40px;
      justify-content: center;
      flex: 0 0 auto;
    }
    .schedule-records {
      margin-top: 10px;
      border: 2px solid var(--fg);
      border-radius: var(--r-md);
      background: var(--card);
      padding: 8px;
      max-height: 170px;
      overflow: auto;
      display: grid;
      gap: 7px;
    }
    .schedule-item {
      border: 2px solid var(--fg);
      border-radius: var(--r-sm);
      background: var(--muted);
      padding: 6px 8px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
    }
    .schedule-item .meta {
      min-width: 0;
      display: flex;
      flex-direction: column;
      gap: 2px;
      color: var(--muted-fg);
      font-size: 11px;
    }
    .schedule-item .meta strong {
      color: var(--fg);
      font-size: 12px;
    }
    .schedule-item .actions {
      display: flex;
      gap: 6px;
      flex-shrink: 0;
    }
    .schedule-item .actions button {
      padding: 5px 9px;
      font-size: 11px;
    }

    /* ── Divider ────────────────────────── */
    .divider {
      display: flex; align-items: center; gap: 10px;
      margin: 20px 0 14px;
    }
    .divider-label {
      font-family: var(--heading); font-weight: 800; font-size: 15px;
      display: flex; align-items: center; gap: 8px; white-space: nowrap;
    }
    .divider::before, .divider::after {
      content: ''; flex: 1; height: 2px;
      background: repeating-linear-gradient(90deg, var(--fg) 0 5px, transparent 5px 11px);
    }

    /* ── Empty state ────────────────────── */
    .empty { text-align: center; padding: 34px 16px; color: var(--muted-fg); }
    .empty-ico { font-size: 38px; display: block; margin-bottom: 8px; }
    .empty-txt { font-family: var(--heading); font-weight: 700; font-size: 14px; }

    .modal-backdrop {
      position: fixed;
      inset: 0;
      background: rgba(30, 41, 59, 0.55);
      display: none;
      align-items: center;
      justify-content: center;
      z-index: 10000;
      padding: 16px;
    }
    .modal-backdrop.show { display: flex; }
    .modal-card {
      width: min(760px, 100%);
      max-height: 90vh;
      overflow: auto;
      background: var(--card);
      border: 2px solid var(--fg);
      border-radius: var(--r-lg);
      box-shadow: 8px 8px 0 var(--fg);
      padding: 18px;
    }
    .modal-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      margin-bottom: 12px;
    }
    .preview-box {
      border: 2px solid var(--fg);
      border-radius: var(--r-md);
      background: var(--muted);
      padding: 12px;
      margin-top: 10px;
      font-size: 13px;
    }
    .preview-text {
      white-space: pre-wrap;
      max-height: 210px;
      overflow: auto;
      margin-top: 8px;
    }
    .mini-lbl {
      font-family: var(--heading);
      font-size: 11px;
      font-weight: 800;
      color: var(--muted-fg);
      text-transform: uppercase;
      letter-spacing: .06em;
      margin-bottom: 6px;
    }
    .img-list {
      display: grid;
      gap: 6px;
      margin-bottom: 10px;
    }
    .img-item {
      display: flex;
      align-items: center;
      gap: 8px;
      border: 2px solid var(--fg);
      border-radius: var(--r-md);
      background: var(--card);
      padding: 6px 8px;
    }
    .img-item .mono {
      flex: 1;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    /* ── Toast ──────────────────────────── */
    #toast {
      position: fixed; bottom: 20px; right: 20px; z-index: 9999;
      max-width: min(380px, calc(100vw - 32px));
      background: var(--card);
      border: 2px solid var(--fg); border-radius: var(--r-lg);
      padding: 12px 16px;
      font-family: var(--heading); font-weight: 700; font-size: 14px;
      display: flex; align-items: center; gap: 10px;
      box-shadow: var(--pop);
      transform: translateY(20px) scale(.95);
      opacity: 0; pointer-events: none;
      transition: opacity .22s var(--bounce), transform .22s var(--bounce);
    }
    #toast.show { transform: translateY(0) scale(1); opacity: 1; }
    #toast.ok  { border-left: 6px solid var(--green); }
    #toast.err { border-left: 6px solid var(--red); }

    /* ── Tabs ───────────────────────────── */
    .tabs-bar {
      display: flex;
      align-items: flex-end;
      gap: 6px;
      padding: 0 16px 0;
      margin: 8px auto -18px;
      max-width: 1340px;
      width: 100%;
      position: relative;
      z-index: 2;
    }
    .tab-btn {
      font-family: var(--heading);
      font-weight: 800;
      font-size: 13px;
      padding: 9px 18px;
      border-radius: var(--r-md) var(--r-md) 0 0;
      border: 2px solid var(--fg);
      border-bottom: none;
      background: var(--muted);
      color: var(--muted-fg);
      cursor: pointer;
      transition: background .15s, color .15s;
    }
    .tab-btn.active {
      background: var(--fg);
      color: var(--card);
    }
    .tab-btn:hover:not(.active) {
      background: var(--border);
      color: var(--fg);
    }
    .tab-panel {
      display: block;
    }
    .tab-panel.hidden {
      display: none;
    }

    @media (prefers-reduced-motion: reduce) {
      *, button { transition: none !important; animation: none !important; }
    }
    @media (max-width: 860px) {
      .main-grid { grid-template-columns: 1fr; }
      th:nth-child(3), td:nth-child(3) { display: none; }
    }
  </style>
</head>
<body>

<div class="deco" aria-hidden="true">
  <div class="deco-shape ds1"></div>
  <div class="deco-shape ds2"></div>
  <div class="deco-shape ds3"></div>
  <div class="deco-shape ds4"></div>
</div>

<div class="page">

  <!-- Header -->
  <header class="header">
    <div class="logo-row">
      <div class="logo-icon">📘</div>
      <div>
        <div class="logo-name"><em>FB</em>Post</div>
        <div class="logo-sub">Control Panel · Realtime API</div>
      </div>
    </div>
    <div style="display:inline-flex;align-items:center;gap:12px">
      <div class="live-badge">
        <span class="live-dot"></span>
        <span id="lastUpdated">Connecting…</span>
      </div>
      <span id="postProgress" style="background:#10b981;color:#fff;padding:2px 8px;border-radius:4px;font-size:12px;font-family:var(--heading);font-weight:700;display:none"></span>
    </div>
  </header>

  <div class="tabs-bar">
    <button class="tab-btn active" data-tab="autopost">📮 Auto Post</button>
    <button class="tab-btn" data-tab="autocomment">💬 Auto Comment</button>
  </div>

<div class="main-grid">

  <!-- ═══ SHARED SIDEBAR — always visible regardless of active tab ═══ -->
  <aside>
    <div class="card" style="margin-bottom:14px">
      <div class="card-title" style="justify-content:space-between">
        <div style="display:flex;align-items:center;gap:8px">
          <div class="t-icon ti-yellow">📋</div>
          Presets
        </div>
        <span id="presetUnsavedBadge" class="pill p-orange" style="display:none">Unsaved changes</span>
      </div>
      <div class="preset-inline">
        <select id="presetSelect" title="Select preset" aria-label="Select preset"></select>
        <button id="saveNewPresetBtn" class="btn-primary" type="button" title="Save new preset" aria-label="Save new preset">💾</button>
        <button id="updatePresetBtn" class="btn-yellow" type="button" title="Update selected preset" aria-label="Update selected preset">📝</button>
        <button id="deletePresetBtn" class="btn-red" type="button" title="Delete selected preset" aria-label="Delete selected preset">🗑️</button>
      </div>
      <div id="presetInfo" class="mono" style="display:none"></div>
    </div>

    <div class="card">
      <div class="card-title" style="justify-content:space-between">
        <div style="display:flex;align-items:center;gap:8px">
          <div class="t-icon ti-violet">👤</div>
          Accounts
        </div>
        <span class="active-badge">⚡ <span id="activeName">—</span></span>
      </div>
      <div class="stats">
        <div class="stat"><div class="stat-n" id="statTotal">0</div><div class="stat-l">Total</div></div>
        <div class="stat"><div class="stat-n" style="color:var(--green)" id="statEnabled">0</div><div class="stat-l">On</div></div>
        <div class="stat"><div class="stat-n" style="color:var(--pink)"  id="statDisabled">0</div><div class="stat-l">Off</div></div>
      </div>
      <div class="frow">
        <select id="accountSelect" style="flex:1;min-width:0"></select>
      </div>
      <div class="frow">
        <input id="newAccountId" type="text" placeholder="new-account-id" style="flex:1;min-width:0">
        <button id="addAccountBtn" class="btn-primary" type="button">＋ Add</button>
      </div>
      <div class="tbl-wrap">
        <table>
          <thead><tr><th>Account</th><th>State</th><th>Actions</th></tr></thead>
          <tbody id="accountsBody">
            <tr><td colspan="3"><div class="empty"><span class="empty-ico">⏳</span><span class="empty-txt">Loading…</span></div></td></tr>
          </tbody>
        </table>
      </div>
      <div class="settings-box" style="padding:8px">
        <div class="mini-lbl">Global Settings</div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:5px;margin-top:6px">
          <button id="openBrowserRulesBtn" class="btn-primary" type="button" style="width:100%">🌐 Browser</button>
          <button id="openGroupsRulesBtn" class="btn-yellow" type="button" style="width:100%">👥 Groups</button>
          <button id="openPostingRulesBtn" class="btn-green" type="button" style="width:100%">📝 Posting</button>
          <button id="openCommentRulesBtn" class="btn-violet" type="button" style="width:100%">💬 Comment</button>
        </div>
      </div>
    </div>
    <!-- ─── Live Log (shared, tab-aware) ─── -->
    <div class="card" style="margin-top:0">
      <div class="frow" style="justify-content:space-between;margin-bottom:8px">
        <div class="card-title" style="margin:0">
          <div class="t-icon ti-green">📝</div>
          <span id="liveLogCardTitle">Live Log — Auto Post</span>
        </div>
        <div style="display:flex;gap:8px;align-items:center">
          <label class="auto-scroll-ctrl" for="logAutoScroll" id="logAutoScrollLabel">
            <input id="logAutoScroll" type="checkbox" checked>
            Auto Scroll
          </label>
          <label class="auto-scroll-ctrl" for="logAutoScroll2" id="logAutoScroll2Label" style="display:none">
            <input id="logAutoScroll2" type="checkbox" checked>
            Auto Scroll
          </label>
        </div>
      </div>
      <div id="liveLogWrap" class="tbl-wrap log-wrap">
        <table class="log-table">
          <thead><tr><th style="width:90px">Time</th><th style="width:60px">Level</th><th>Message</th></tr></thead>
          <tbody id="liveLogsBody">
            <tr><td colspan="3"><div class="empty"><span class="empty-ico">📝</span><span class="empty-txt">Waiting logs…</span></div></td></tr>
          </tbody>
        </table>
      </div>
      <div id="cmLiveLogWrap" class="tbl-wrap log-wrap" style="display:none">
        <table class="log-table">
          <thead><tr><th style="width:90px">Time</th><th style="width:60px">Level</th><th>Message</th></tr></thead>
          <tbody id="cmLiveLogsBody">
            <tr><td colspan="3"><div class="empty"><span class="empty-ico">📝</span><span class="empty-txt">Waiting logs…</span></div></td></tr>
          </tbody>
        </table>
      </div>
    </div>
  </aside>

  <!-- ═══ TAB PANELS — right column, switches with tab ═══ -->
  <div style="min-width:0">

  <!-- ─── AUTO POST TAB ─── -->
  <div id="tab-autopost" class="tab-panel">

    <!-- Actions + Groups -->
    <section>
      <div class="card">
        <div class="card-title">
          <div class="t-icon ti-pink">⚙️</div>
          <span id="selectedTitle">Select an account</span>
        </div>

        <div class="toolbar">
          <div class="toolbar-btns" id="selectedActions">
            <button type="button" data-action="scrape_groups" class="btn-yellow">🕷️ Scrape Groups</button>
            <button type="button" data-action="run_once_dry">🧪 Run Dry</button>
            <button type="button" data-action="run_once_live" class="btn-green">🚀 Run Live</button>
          </div>
        </div>

        <div class="settings-box" style="margin-top:10px;padding:8px">
          <div class="mini-lbl">Scheduler</div>
          <div class="frow" style="gap:5px;flex-wrap:nowrap;margin-top:6px">
            <select id="schType" style="flex:0 0 auto">
              <option value="specific_datetime">One Time</option>
              <option value="daily">Daily</option>
              <option value="weekly">Weekly</option>
            </select>
            <div id="schDateTimeWrap" style="flex:1;min-width:0">
              <input id="schDateTime" type="datetime-local" style="width:100%">
            </div>
            <div id="schTimeOnlyWrap" style="flex:1;min-width:0;display:none">
              <input id="schTimeOnly" type="time" value="09:00" style="width:100%">
            </div>
            <button id="startScheduleBtn" class="btn-primary" type="button" style="flex:0 0 auto;white-space:nowrap">⏰ Add</button>
            <button id="stopScheduleBtn" class="btn-red" type="button" style="flex:0 0 auto;white-space:nowrap">■ Off</button>
          </div>
          <div class="frow" id="schWeekdaysWrap" style="display:none;flex-wrap:wrap;gap:6px;margin-top:6px">
            <label><input type="checkbox" class="schDay" value="0"> Mon</label>
            <label><input type="checkbox" class="schDay" value="1"> Tue</label>
            <label><input type="checkbox" class="schDay" value="2"> Wed</label>
            <label><input type="checkbox" class="schDay" value="3"> Thu</label>
            <label><input type="checkbox" class="schDay" value="4"> Fri</label>
            <label><input type="checkbox" class="schDay" value="5"> Sat</label>
            <label><input type="checkbox" class="schDay" value="6"> Sun</label>
          </div>
          <div id="scheduleInfo" class="mono" style="margin-top:6px">No schedule active.</div>
          <div id="scheduleRecords" class="schedule-records">
            <div class="mono">No schedules saved.</div>
          </div>
        </div>

        <div class="divider">
          <div class="divider-label">
            <div class="t-icon ti-violet" style="width:24px;height:24px;font-size:13px;box-shadow:2px 2px 0 var(--fg)">👥</div>
            Groups
          </div>
          <span class="pill p-violet" id="groupCount" style="margin-left:auto">0 groups</span>
        </div>

        <div class="frow">
          <input id="groupFilter" type="text" placeholder="filter by id or name…" style="min-width:170px;flex:1;max-width:260px">
          <select id="groupStatusFilter">
            <option value="all">All statuses</option>
            <option value="included">Included only</option>
            <option value="excluded">Excluded only</option>
          </select>
          <select id="groupPerPage">
            <option value="20">20 / page</option>
            <option value="40">40 / page</option>
            <option value="60">60 / page</option>
            <option value="all">All</option>
          </select>
        </div>

        <div class="tbl-wrap">
          <table>
            <thead><tr><th>ID</th><th>Name</th><th>URL</th><th>Status</th><th>Toggle</th></tr></thead>
            <tbody id="groupsBody">
              <tr><td colspan="5"><div class="empty"><span class="empty-ico">👥</span><span class="empty-txt">No groups loaded yet</span></div></td></tr>
            </tbody>
          </table>
        </div>

        <div class="pager">
          <button id="groupPrevPage" type="button">← Prev</button>
          <span id="groupPageInfo" class="pager-info">Page 1 / 1</span>
          <button id="groupNextPage" type="button">Next →</button>
        </div>

      </div>
    </section>

  </div><!-- end tab-autopost -->

  <!-- ─── AUTO COMMENT TAB ─── -->
  <div id="tab-autocomment" class="tab-panel hidden">
    <div class="card">
      <div class="card-title">
        <div class="t-icon ti-pink">💬</div>
        <span id="cmSelectedTitle">Select an account to start commenting</span>
      </div>
      <div class="toolbar">
        <div class="toolbar-btns" id="cmActions">
          <button type="button" id="startCommenterBtn" class="btn-green">▶️ Start Commenter</button>
          <button type="button" id="stopCommenterBtn" class="btn-red">■ Stop Commenter</button>
        </div>
      </div>
      <div id="cmStatusBox" class="settings-box" style="margin-top:10px">
        <div class="mini-lbl">Commenter Status</div>
        <div id="cmStatusInfo" class="mono" style="margin-top:4px">Idle — no commenter running.</div>
      </div>

      <div class="settings-box" style="margin-top:10px;padding:8px">
        <div class="mini-lbl">Scheduler</div>
        <div class="frow" style="gap:5px;flex-wrap:nowrap;margin-top:6px">
          <select id="cmSchType" style="flex:0 0 auto">
            <option value="specific_datetime">One Time</option>
            <option value="daily">Daily</option>
            <option value="weekly">Weekly</option>
          </select>
          <div id="cmSchDateFromWrap" style="flex:1;min-width:0">
            <input id="cmSchDateFrom" type="datetime-local" style="width:100%">
          </div>
          <div id="cmSchTimeFromWrap" style="flex:1;min-width:0;display:none">
            <input id="cmSchTimeFrom" type="time" value="09:00" style="width:100%">
          </div>
          <input id="cmSchTimeTo" type="time" value="22:00" style="flex:0 0 auto;min-width:80px">
          <button id="cmStartScheduleBtn" class="btn-primary" type="button" style="flex:0 0 auto;white-space:nowrap">⏰ Add</button>
          <button id="cmStopScheduleBtn" class="btn-red" type="button" style="flex:0 0 auto;white-space:nowrap">■ Off</button>
        </div>
        <div class="frow" id="cmSchWeekdaysWrap" style="display:none;flex-wrap:wrap;gap:6px;margin-top:6px">
          <label><input type="checkbox" class="cmSchDay" value="0"> Mon</label>
          <label><input type="checkbox" class="cmSchDay" value="1"> Tue</label>
          <label><input type="checkbox" class="cmSchDay" value="2"> Wed</label>
          <label><input type="checkbox" class="cmSchDay" value="3"> Thu</label>
          <label><input type="checkbox" class="cmSchDay" value="4"> Fri</label>
          <label><input type="checkbox" class="cmSchDay" value="5"> Sat</label>
          <label><input type="checkbox" class="cmSchDay" value="6"> Sun</label>
        </div>
        <div id="cmScheduleInfo" class="mono" style="margin-top:6px">No comment schedule active.</div>
        <div id="cmScheduleRecords" class="schedule-records">
          <div class="mono">No comment schedules saved.</div>
        </div>
      </div>
    </div>

    <!-- ─── Comment Groups ─── -->
    <div class="divider">
      <div class="divider-label">
        <div class="t-icon ti-violet" style="width:24px;height:24px;font-size:13px;box-shadow:2px 2px 0 var(--fg)">👥</div>
        Comment Groups
      </div>
      <span class="pill p-violet" id="cmGroupCount" style="margin-left:auto">0 groups</span>
    </div>

    <div class="frow">
      <input id="cmGroupFilter" type="text" placeholder="filter by id or name…" style="min-width:170px;flex:1;max-width:260px">
      <select id="cmGroupStatusFilter">
        <option value="all">All statuses</option>
        <option value="enabled">Comment-enabled only</option>
        <option value="disabled">Comment-disabled only</option>
      </select>
      <select id="cmGroupPerPage">
        <option value="20">20 / page</option>
        <option value="40">40 / page</option>
        <option value="all">All</option>
      </select>
    </div>

    <div class="tbl-wrap">
      <table>
        <thead><tr><th>ID</th><th>Name</th><th>URL</th><th>Comment</th><th>Toggle</th></tr></thead>
        <tbody id="cmGroupsBody">
          <tr><td colspan="5"><div class="empty"><span class="empty-ico">👥</span><span class="empty-txt">No groups loaded yet</span></div></td></tr>
        </tbody>
      </table>
    </div>

    <div class="pager">
      <button id="cmGroupPrevPage" type="button">← Prev</button>
      <span id="cmGroupPageInfo" class="pager-info">Page 1 / 1</span>
      <button id="cmGroupNextPage" type="button">Next →</button>
    </div>
  </div><!-- end tab-autocomment -->

  </div><!-- end tab-panels col -->
</div><!-- end main-grid -->

<div id="toast" role="alert" aria-live="polite"></div>

<div id="templateModal" class="modal-backdrop" aria-hidden="true">
  <div class="modal-card" role="dialog" aria-modal="true" aria-labelledby="templateModalTitle">
    <div class="modal-head">
      <div class="card-title" style="margin:0">
        <div class="t-icon ti-yellow">🧩</div>
        <span id="templateModalTitle">Choose Template</span>
      </div>
      <button id="closeTemplateModal" type="button">✕ Close</button>
    </div>

    <div class="frow">
      <select id="templateSelect" style="flex:1;min-width:240px"></select>
      <button id="applyTemplateBtn" class="btn-primary" type="button">✅ Use Template</button>
    </div>

    <div class="preview-box">
      <div><strong>Title:</strong> <span id="templatePreviewTitle">—</span></div>
      <div style="margin-top:6px"><strong>File:</strong> <span class="mono" id="templatePreviewFile">—</span></div>
      <div style="margin-top:6px"><strong>Tags:</strong> <span id="templatePreviewTags">—</span></div>
      <div style="margin-top:6px"><strong>Images:</strong> <span id="templatePreviewImages">0</span></div>
      <div class="preview-text" id="templatePreviewText">No preview.</div>
    </div>

    <div class="frow" style="margin-top:12px">
      <button id="openEditTemplateBtn" class="btn-yellow" type="button">✏️ Edit Post</button>
      <button id="openAddTemplateBtn" class="btn-primary" type="button">➕ Add Post</button>
      <button id="deleteTemplateBtn" class="btn-red" type="button">🗑️ Delete</button>
    </div>
  </div>
</div>

<div id="templateEditorModal" class="modal-backdrop" aria-hidden="true">
  <div class="modal-card" role="dialog" aria-modal="true" aria-labelledby="templateEditorModalTitle">
    <div class="modal-head">
      <div class="card-title" style="margin:0">
        <div class="t-icon ti-yellow">📝</div>
        <span id="templateEditorModalTitle">Edit Template Content</span>
      </div>
      <button id="closeTemplateEditorModal" type="button">✕ Close</button>
    </div>

    <div class="preview-box" style="margin-top:0">
      <div class="mini-lbl">Template Content</div>
      <div class="frow" style="margin-bottom:10px">
        <input id="tplEditTitle" type="text" placeholder="Template title (display only)" style="flex:1;min-width:220px">
      </div>
      <div class="frow" style="margin-bottom:10px">
        <input id="tplEditTags" type="text" placeholder="tags comma separated (example: promo, vps, github)" style="flex:1;min-width:220px">
      </div>
      <div class="frow" style="margin-bottom:10px">
        <textarea id="tplEditText" placeholder="Post text..."></textarea>
      </div>

      <div class="mini-lbl">Images</div>
      <div id="tplImagesList" class="img-list"></div>
      <div class="frow" style="margin-bottom:10px">
        <input id="tplNewImage" type="text" placeholder="templates/images/your-image.png" style="flex:1;min-width:220px">
        <button id="tplAddImageBtn" class="btn-green" type="button">+ Add Image</button>
        <input id="tplImageFileInput" type="file" accept="image/*" style="display:none" multiple>
        <button id="tplBrowseImageBtn" class="btn-primary" type="button">📁 Browse</button>
      </div>

      <div class="frow">
        <button id="saveTemplateBtn" class="btn-yellow" type="button">💾 Save Selected Template</button>
        <button id="createTemplateBtn" class="btn-primary" type="button">➕ Create New Post</button>
      </div>
    </div>
  </div>
</div>

<div id="browserRulesModal" class="modal-backdrop" aria-hidden="true">
  <div class="modal-card" role="dialog" aria-modal="true" aria-labelledby="browserRulesModalTitle">
    <div class="modal-head">
      <div class="card-title" style="margin:0">
        <div class="t-icon ti-violet">🌐</div>
        <span id="browserRulesModalTitle">Global Browser Rules</span>
      </div>
      <button id="closeBrowserRulesModal" type="button">✕ Close</button>
    </div>

    <div class="preview-box" style="margin-top:0">
      <div class="frow">
        <div class="field">
          <label class="mini-lbl" for="brHeadless">Headless</label>
          <select id="brHeadless">
            <option value="false">false</option>
            <option value="true">true</option>
          </select>
        </div>
        <div class="field">
          <label class="mini-lbl" for="brHumanize">Humanize</label>
          <select id="brHumanize">
            <option value="true">true</option>
            <option value="false">false</option>
          </select>
        </div>
        <div class="field">
          <label class="mini-lbl" for="brFullscreen">Fullscreen</label>
          <select id="brFullscreen">
            <option value="true">true</option>
            <option value="false">false</option>
          </select>
        </div>
      </div>

      <div class="frow">
        <div class="field">
          <label class="mini-lbl" for="brLocale">Locale</label>
          <input id="brLocale" type="text" placeholder="example: id-ID">
        </div>
        <div class="field">
          <label class="mini-lbl" for="brTimezone">Timezone</label>
          <input id="brTimezone" type="text" placeholder="example: Asia/Jakarta">
        </div>
      </div>

      <div class="frow">
        <div class="field">
          <label class="mini-lbl" for="brScreenMaxWidth">Screen Max Width</label>
          <input id="brScreenMaxWidth" type="text" placeholder="1536">
        </div>
        <div class="field">
          <label class="mini-lbl" for="brScreenMaxHeight">Screen Max Height</label>
          <input id="brScreenMaxHeight" type="text" placeholder="864">
        </div>
      </div>
      <div class="frow">
        <div class="field">
          <label class="mini-lbl" for="brWindowWidth">Window Width</label>
          <input id="brWindowWidth" type="text" placeholder="1536">
        </div>
        <div class="field">
          <label class="mini-lbl" for="brWindowHeight">Window Height</label>
          <input id="brWindowHeight" type="text" placeholder="864">
        </div>
      </div>

      <div class="frow">
        <button id="saveBrowserRulesBtn" class="btn-primary" type="button">💾 Save Browser Rules</button>
      </div>
    </div>
  </div>
</div>

<div id="groupsRulesModal" class="modal-backdrop" aria-hidden="true">
  <div class="modal-card" role="dialog" aria-modal="true" aria-labelledby="groupsRulesModalTitle">
    <div class="modal-head">
      <div class="card-title" style="margin:0">
        <div class="t-icon ti-yellow">👥</div>
        <span id="groupsRulesModalTitle">Global Groups Rules</span>
      </div>
      <button id="closeGroupsRulesModal" type="button">✕ Close</button>
    </div>
    <div class="preview-box" style="margin-top:0">
      <div class="frow">
        <div class="field">
          <label class="mini-lbl" for="grRescrapeDays">Rescrape Every Days</label>
          <input id="grRescrapeDays" type="text" placeholder="7">
        </div>
      </div>
      <div class="frow">
        <div class="field">
          <label class="mini-lbl" for="grIdleRounds">Idle Rounds To Stop</label>
          <input id="grIdleRounds" type="text" placeholder="4">
        </div>
        <div class="field">
          <label class="mini-lbl" for="grScrollWaitMs">Scroll Wait (ms)</label>
          <input id="grScrollWaitMs" type="text" placeholder="1400">
        </div>
      </div>
      <div class="frow">
        <button id="saveGroupsRulesBtn" class="btn-primary" type="button">💾 Save Groups Rules</button>
      </div>
    </div>
  </div>
</div>

<div id="postingRulesModal" class="modal-backdrop" aria-hidden="true">
  <div class="modal-card" role="dialog" aria-modal="true" aria-labelledby="postingRulesModalTitle">
    <div class="modal-head">
      <div class="card-title" style="margin:0">
        <div class="t-icon ti-green">📝</div>
        <span id="postingRulesModalTitle">Global Posting Rules</span>
      </div>
      <button id="closePostingRulesModal" type="button">✕ Close</button>
    </div>
    <div class="preview-box" style="margin-top:0">
      <div class="frow">
        <div class="field">
          <label class="mini-lbl" for="prTemplateFile">Template File</label>
          <input id="prTemplateFile" type="text" placeholder="post_1.yaml">
        </div>
      </div>
      <div class="frow">
        <div class="field">
          <label class="mini-lbl" for="prMinDelay">Min Delay (minutes)</label>
          <input id="prMinDelay" type="text" placeholder="3">
        </div>
        <div class="field">
          <label class="mini-lbl" for="prMaxDelay">Max Delay (minutes)</label>
          <input id="prMaxDelay" type="text" placeholder="5">
        </div>
      </div>
      <div class="frow">
        <div class="field">
          <label class="mini-lbl" for="prRestEvery">Rest Every N Posts</label>
          <input id="prRestEvery" type="text" placeholder="10">
        </div>
        <div class="field">
          <label class="mini-lbl" for="prRestDuration">Rest Duration (minutes)</label>
          <input id="prRestDuration" type="text" placeholder="30">
        </div>
        <div class="field">
          <label class="mini-lbl" for="prDryRun">Dry Run</label>
          <select id="prDryRun">
            <option value="true">true</option>
            <option value="false">false</option>
          </select>
        </div>
        <div class="field">
          <label class="mini-lbl" for="prAutoSkip">Auto Skip Posted</label>
          <select id="prAutoSkip">
            <option value="true">true</option>
            <option value="false">false</option>
          </select>
        </div>
        <div class="field">
          <label class="mini-lbl" for="prPauseOnCaptcha">Stop On CAPTCHA</label>
          <select id="prPauseOnCaptcha">
            <option value="false">false</option>
            <option value="true">true</option>
          </select>
        </div>
        <div class="field">
          <label class="mini-lbl" for="prStopOnRateLimit">Stop On Rate Limit</label>
          <select id="prStopOnRateLimit">
            <option value="false">false</option>
            <option value="true">true</option>
          </select>
        </div>
      </div>
      <div class="frow">
        <button id="savePostingRulesBtn" class="btn-primary" type="button">💾 Save Posting Rules</button>
      </div>
    </div>
  </div>
</div>

<div id="commentRulesModal" class="modal-backdrop" aria-hidden="true">
  <div class="modal-card" role="dialog" aria-modal="true" aria-labelledby="commentRulesModalTitle">
    <div class="modal-head">
      <div class="card-title" style="margin:0">
        <div class="t-icon ti-violet">💬</div>
        <span id="commentRulesModalTitle">Global Comment Rules</span>
      </div>
      <button id="closeCommentRulesModal" type="button">✕ Close</button>
    </div>
    <div class="preview-box" style="margin-top:0">
      <p class="mono" style="margin:0 0 12px;font-size:12px;color:var(--muted-fg)">
        To set the comment template, go to the Auto Comment tab and use the 🧩 button in the account actions row.
      </p>
      <div class="frow">
        <div class="field">
          <label class="mini-lbl" for="cmMinDelay">Min Delay Between Groups (minutes)</label>
          <input id="cmMinDelay" type="text" placeholder="1" style="color:var(--fg)">
        </div>
        <div class="field">
          <label class="mini-lbl" for="cmMaxDelay">Max Delay Between Groups (minutes)</label>
          <input id="cmMaxDelay" type="text" placeholder="3" style="color:var(--fg)">
        </div>
      </div>
      <div class="frow" style="margin-top:12px">
        <button id="saveCommentSettingsBtn" class="btn-primary" type="button">💾 Save Comment Rules</button>
      </div>
    </div>
  </div>
</div>

<div id="runLiveModal" class="modal-backdrop" aria-hidden="true">
  <div class="modal-card" role="dialog" aria-modal="true" style="max-width:420px">
    <div class="modal-head">
      <div class="card-title" style="margin:0">
        <div class="t-icon ti-green">🚀</div>
        <span>Run Live Posting</span>
      </div>
      <button id="closeRunLiveModal" type="button">✕ Close</button>
    </div>
    <div class="preview-box" style="margin-top:0;text-align:center">
      <p style="margin:0 0 16px;font-size:15px"><strong>Reset posted history?</strong></p>
      <p style="margin:0 0 20px;color:var(--muted-fg);font-size:13px">
        Choose whether to clear the posted log before running.
      </p>
      <div class="frow" style="justify-content:center;gap:12px">
        <button id="runLiveResetBtn" class="btn-yellow" type="button">🔄 Reset & Post All</button>
        <button id="runLiveKeepBtn" class="btn-green" type="button">▶️ Post New Only</button>
      </div>
    </div>
  </div>
</div>

<div id="presetSaveModal" class="modal-backdrop" aria-hidden="true">
  <div class="modal-card" role="dialog" aria-modal="true" style="max-width:420px">
    <div class="modal-head">
      <div class="card-title" style="margin:0">
        <div class="t-icon ti-yellow">💾</div>
        <span>Save Preset</span>
      </div>
      <button id="closePresetSaveModal" type="button">✕ Close</button>
    </div>
    <div class="preview-box" style="margin-top:0">
      <div class="field">
        <label class="mini-lbl" for="presetNameInput">Preset Name</label>
        <input id="presetNameInput" type="text" placeholder="marketing-post">
      </div>
      <div class="frow" style="margin-top:12px">
        <button id="confirmSavePresetBtn" class="btn-primary" type="button">💾 Save</button>
      </div>
    </div>
  </div>
</div>

<div id="unsavedChangesModal" class="modal-backdrop" aria-hidden="true">
  <div class="modal-card" role="dialog" aria-modal="true" style="max-width:460px">
    <div class="modal-head">
      <div class="card-title" style="margin:0">
        <div class="t-icon ti-yellow">⚠️</div>
        <span>Unsaved Changes</span>
      </div>
      <button id="closeUnsavedModal" type="button">✕ Close</button>
    </div>
    <div class="preview-box" style="margin-top:0">
      <p style="margin:0 0 12px">You changed settings/groups but haven't saved them into a preset yet.</p>
      <div class="frow" style="justify-content:center;gap:10px">
        <button id="saveUnsavedToPresetBtn" class="btn-primary" type="button">💾 Save as Preset</button>
        <button id="saveUnsavedToConfigBtn" class="btn-yellow" type="button">➡ Continue (Config only)</button>
        <button id="discardUnsavedBtn" class="btn-red" type="button">✖ Cancel</button>
      </div>
    </div>
  </div>
</div>

<div id="presetActionModal" class="modal-backdrop" aria-hidden="true">
  <div class="modal-card" role="dialog" aria-modal="true" style="max-width:460px">
    <div class="modal-head">
      <div class="card-title" style="margin:0">
        <div class="t-icon ti-violet">📋</div>
        <span id="presetActionTitle">Preset Action</span>
      </div>
      <button id="closePresetActionModal" type="button">✕ Close</button>
    </div>
    <div class="preview-box" style="margin-top:0">
      <p id="presetActionMessage" style="margin:0 0 12px">Confirm preset action?</p>
      <div class="frow" style="justify-content:center;gap:10px">
        <button id="confirmPresetActionBtn" class="btn-primary" type="button">Confirm</button>
      </div>
    </div>
  </div>
</div>

<script>
  let selectedAccount = '';
  let isBusy = false;
  let groupsSnapshot = [];
  let logsSnapshot = [];
  let templatesSnapshot = [];
  let lastTemplatesSnapshot = [];
  let lastCommentingConfig = {};
  let browserRulesSnapshot = {};
  let globalGroupsSnapshot = {};
  let globalPostingSnapshot = {};
  let templateEditImages = [];
  let templateEditorMode = 'edit';
  let groupPage = 1;
  let toastTimer = null;
  let presetsSnapshot = [];
  let presetStatusSnapshot = { enabled: false, name: '' };
  let hasUnsavedChanges = false;
  let suppressUnsavedMark = false;
  let pendingUnsavedAction = null;
  let runPendingAfterPresetSave = false;
  let pendingPresetAction = null;
  let lastPresetSelectionValue = '';
  let lastScheduleSpecSignature = '';

  function esc(v) {
    const d = document.createElement('div');
    d.innerText = String(v ?? '');
    return d.innerHTML;
  }

  function toast(msg, isErr = false) {
    const el = document.getElementById('toast');
    el.innerHTML = (isErr ? '❌ ' : '✅ ') + esc(msg);
    el.className = 'show ' + (isErr ? 'err' : 'ok');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.className = ''; }, 3200);
  }

  function setUpdated() {
    document.getElementById('lastUpdated').textContent = new Date().toLocaleTimeString();
  }

  function markUnsaved() {
    if (suppressUnsavedMark) return;
    hasUnsavedChanges = true;
    document.getElementById('presetUnsavedBadge').style.display = 'inline-block';
  }

  function clearUnsaved() {
    hasUnsavedChanges = false;
    document.getElementById('presetUnsavedBadge').style.display = 'none';
  }

  function showUnsavedModal(nextAction = null) {
    pendingUnsavedAction = nextAction;
    document.getElementById('unsavedChangesModal').classList.add('show');
  }

  function showPresetActionModal(title, message, onConfirm, confirmLabel = 'Confirm') {
    pendingPresetAction = onConfirm;
    document.getElementById('presetActionTitle').textContent = title;
    document.getElementById('presetActionMessage').textContent = message;
    document.getElementById('confirmPresetActionBtn').textContent = confirmLabel;
    document.getElementById('presetActionModal').classList.add('show');
  }

  function closePresetActionModal() {
    pendingPresetAction = null;
    document.getElementById('presetActionModal').classList.remove('show');
  }

  async function runPendingUnsavedAction() {
    if (typeof pendingUnsavedAction !== 'function') {
      pendingUnsavedAction = null;
      return;
    }
    const fn = pendingUnsavedAction;
    pendingUnsavedAction = null;
    await fn();
  }

  function setBusy(b) {
    isBusy = b;
    document.querySelectorAll('button').forEach(btn => btn.disabled = !!b);
  }

  function resetGroupPaging() {
    groupPage = 1;
  }

  function formatLogTime(ts) {
    const value = String(ts || '').trim();
    if (!value) return '-';
    const part = value.split('T')[1] || value;
    return part.replace('Z', '').replace(/[+-]\d{2}:\d{2}$/, '');
  }

  function renderLiveLogs(rows) {
    const tbody = document.getElementById('liveLogsBody');
    if (!rows.length) {
      tbody.innerHTML = '<tr><td colspan="3"><div class="empty"><span class="empty-ico">🫙</span><span class="empty-txt">No logs yet</span></div></td></tr>';
      updatePostProgress(null);
      return;
    }

    tbody.innerHTML = rows.map(item => {
      const level = String(item.level || 'INFO').toUpperCase();
      return `<tr>
        <td class="mono">${esc(formatLogTime(item.timestamp))}</td>
        <td><span class="pill ${level === 'ERROR' ? 'p-orange' : 'p-gray'}">${esc(level)}</span></td>
        <td>${esc(item.message || '')}</td>
      </tr>`;
    }).join('');

    // Extract posting progress from recent logs
    updatePostProgressFromLogs(rows);

    const autoScroll = document.getElementById('logAutoScroll').checked;
    if (autoScroll) {
      const wrap = document.getElementById('liveLogWrap');
      wrap.scrollTop = wrap.scrollHeight;
    }
  }

  function updatePostProgressFromLogs(rows) {
    // Look for "Processing group X/Y" pattern in recent logs (last 20)
    const recentLogs = rows.slice(-20);
    let latestProgress = null;
    
    for (let i = recentLogs.length - 1; i >= 0; i--) {
      const msg = recentLogs[i].message || '';
      // Match "Processing group 5/10: Group Name"
      const match = msg.match(/Processing group (\d+)\/(\d+)/);
      if (match) {
        latestProgress = { current: parseInt(match[1]), total: parseInt(match[2]) };
        break;
      }
      // Check for completion messages
      if (msg.includes('Posting session complete') || msg.includes('No eligible groups')) {
        latestProgress = null;
        break;
      }
    }
    
    updatePostProgress(latestProgress);
  }

  function updatePostProgress(progress) {
    const el = document.getElementById('postProgress');
    if (!progress) {
      el.style.display = 'none';
      return;
    }
    el.style.display = 'inline';
    el.textContent = `📮 Post ${progress.current}/${progress.total}`;
  }

  async function loadLogs() {
    try {
      const res = await fetch('/api/logs?limit=200', { cache: 'no-store' });
      const data = await res.json();
      logsSnapshot = data.logs || [];
      renderLiveLogs(logsSnapshot);
      syncCmLogs(data.commenter_logs || []);
    } catch {
      // Keep last rendered logs if fetch temporarily fails.
    }
  }

  function initLogAutoScrollSetting() {
    const key = 'fbpost.logAutoScroll';
    const input = document.getElementById('logAutoScroll');
    const saved = localStorage.getItem(key);
    input.checked = saved === null ? true : saved === 'true';
    input.addEventListener('change', () => {
      localStorage.setItem(key, input.checked ? 'true' : 'false');
      if (input.checked) {
        const wrap = document.getElementById('liveLogWrap');
        wrap.scrollTop = wrap.scrollHeight;
      }
    });
  }

  function renderTemplateImagesEditor() {
    const wrap = document.getElementById('tplImagesList');
    if (!templateEditImages.length) {
      wrap.innerHTML = '<div class="mono">No images added.</div>';
      return;
    }
    wrap.innerHTML = templateEditImages.map((img, idx) =>
      `<div class="img-item"><span class="mono">${esc(img)}</span><button class="sm-btn btn-red" type="button" onclick="removeTemplateImage(${idx})">Remove</button></div>`
    ).join('');
  }

  function removeTemplateImage(index) {
    templateEditImages = templateEditImages.filter((_, i) => i !== index);
    renderTemplateImagesEditor();
  }

  function buildTemplatePayloadFromEditor() {
    const tagsRaw = (document.getElementById('tplEditTags').value || '').trim();
    const tags = tagsRaw ? tagsRaw.split(',').map(t => t.trim()).filter(Boolean) : [];
    return {
      title: (document.getElementById('tplEditTitle').value || '').trim(),
      text: (document.getElementById('tplEditText').value || ''),
      tags,
      images: templateEditImages.slice(),
    };
  }

  function fillTemplateEditor(item) {
    document.getElementById('tplEditTitle').value = item ? (item.title || '') : '';
    document.getElementById('tplEditTags').value = item && (item.tags || []).length ? item.tags.join(', ') : '';
    document.getElementById('tplEditText').value = item ? (item.text || '') : '';
    templateEditImages = item ? (item.images || []).slice() : [];
    renderTemplateImagesEditor();
  }

  async function callAction(
    action,
    accountId,
    groupId = '',
    templateFile = '',
    templateData = {},
    browserRules = {},
    groupsRules = {},
    postingRules = {}
  ) {
    if (isBusy) return;
    setBusy(true);
    try {
      const res = await fetch('/api/action', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          action,
          account_id: accountId,
          group_id: groupId,
          template_file: templateFile,
          template_data: templateData,
          browser_rules: browserRules,
          groups_rules: groupsRules,
          posting_rules: postingRules,
        })
      });
      const data = await res.json();
      data.ok ? toast(data.message || 'Done!') : toast(data.error || 'Failed.', true);
      if (data.ok) {
        const dirtyActions = new Set([
          'add_account',
          'delete_account',
          'enable_account',
          'disable_account',
          'set_active',
          'include_group',
          'exclude_group',
          'set_template',
          'update_browser_rules',
          'update_groups_rules',
          'update_posting_rules',
        ]);
        const cleanActions = new Set([
          'save_preset',
          'update_preset',
          'apply_preset',
          'disable_preset',
          'delete_preset',
        ]);
        if (dirtyActions.has(action)) markUnsaved();
        if (cleanActions.has(action)) clearUnsaved();
      }
      return data;
    } catch (e) {
      toast(String(e), true);
      return { ok: false, error: String(e) };
    } finally {
      setBusy(false);
      await loadState();
    }
  }

  function closeTemplateModal() {
    document.getElementById('templateModal').classList.remove('show');
    templateModalContext = 'post';
    document.getElementById('templateModalTitle').textContent = 'Choose Template';
    document.getElementById('applyTemplateBtn').textContent = '✅ Use Template';
  }

  function openBrowserRulesModal() {
    const r = browserRulesSnapshot || {};
    document.getElementById('brHeadless').value = String(!!r.headless);
    document.getElementById('brHumanize').value = String(r.humanize !== false);
    document.getElementById('brFullscreen').value = String(r.fullscreen !== false);
    document.getElementById('brLocale').value = String(r.locale || '');
    document.getElementById('brTimezone').value = String(r.timezone || '');
    document.getElementById('brScreenMaxWidth').value = String(r.screen_max_width || '');
    document.getElementById('brScreenMaxHeight').value = String(r.screen_max_height || '');
    document.getElementById('brWindowWidth').value = String(r.window_width || '');
    document.getElementById('brWindowHeight').value = String(r.window_height || '');
    document.getElementById('browserRulesModal').classList.add('show');
  }

  function closeBrowserRulesModal() {
    document.getElementById('browserRulesModal').classList.remove('show');
  }

  function openGroupsRulesModal() {
    const g = globalGroupsSnapshot || {};
    const s = g.scrape || {};
    document.getElementById('grRescrapeDays').value = String(g.rescrape_every_days || '');
    document.getElementById('grIdleRounds').value = String(s.idle_rounds_to_stop || '');
    document.getElementById('grScrollWaitMs').value = String(s.scroll_wait_ms || '');
    document.getElementById('groupsRulesModal').classList.add('show');
  }

  function closeGroupsRulesModal() {
    document.getElementById('groupsRulesModal').classList.remove('show');
  }

  function openPostingRulesModal() {
    const p = globalPostingSnapshot || {};
    document.getElementById('prTemplateFile').value = String(p.template_file || '');
    document.getElementById('prMinDelay').value = String(p.min_delay_minutes || '');
    document.getElementById('prMaxDelay').value = String(p.max_delay_minutes || '');
    document.getElementById('prRestEvery').value = String(p.rest_every_n_posts || '');
    document.getElementById('prRestDuration').value = String(p.rest_duration_minutes || '');
    document.getElementById('prDryRun').value = String(p.dry_run !== false);
    document.getElementById('prAutoSkip').value = String(!!p.auto_skip);
    document.getElementById('prPauseOnCaptcha').value = String(!!p.pause_on_captcha);
    document.getElementById('prStopOnRateLimit').value = String(!!p.stop_on_rate_limit);
    document.getElementById('postingRulesModal').classList.add('show');
  }

  function closePostingRulesModal() {
    document.getElementById('postingRulesModal').classList.remove('show');
  }

  function openCommentRulesModal() {
    const cfg = lastCommentingConfig || {};
    const templates = lastTemplatesSnapshot || [];
    // Pre-fill delay inputs from last known state
    const minEl = document.getElementById('cmMinDelay');
    const maxEl = document.getElementById('cmMaxDelay');
    if (minEl) minEl.value = cfg.min_delay_minutes ?? 1;
    if (maxEl) maxEl.value = cfg.max_delay_minutes ?? 3;
    document.getElementById('commentRulesModal').classList.add('show');
  }

  function closeCommentRulesModal() {
    document.getElementById('commentRulesModal').classList.remove('show');
  }

  function closeTemplateEditorModal() {
    document.getElementById('templateEditorModal').classList.remove('show');
    // Clear editor state on close to prevent stale data
    fillTemplateEditor(null);
  }

  function openTemplateEditorModal(mode) {
    templateEditorMode = mode === 'add' ? 'add' : 'edit';
    const titleEl = document.getElementById('templateEditorModalTitle');
    const saveBtn = document.getElementById('saveTemplateBtn');
    const createBtn = document.getElementById('createTemplateBtn');

    if (templateEditorMode === 'edit') {
      titleEl.textContent = 'Edit Template Content';
      saveBtn.style.display = '';
      createBtn.style.display = 'none';
      const selectedTemplate = (document.getElementById('templateSelect').value || '').trim();
      const item = templatesSnapshot.find(t => t.template_file === selectedTemplate) || null;
      fillTemplateEditor(item);
    } else {
      titleEl.textContent = 'Add New Post';
      saveBtn.style.display = 'none';
      createBtn.style.display = '';
      fillTemplateEditor(null);
    }

    document.getElementById('templateEditorModal').classList.add('show');
  }

  function updateTemplatePreview(templateFile) {
    const item = templatesSnapshot.find(t => t.template_file === templateFile);
    if (!item) {
      document.getElementById('templatePreviewTitle').textContent = '—';
      document.getElementById('templatePreviewFile').textContent = '—';
      document.getElementById('templatePreviewTags').textContent = '—';
      document.getElementById('templatePreviewImages').textContent = '0';
      document.getElementById('templatePreviewText').textContent = 'No preview.';
      return;
    }

    document.getElementById('templatePreviewTitle').textContent = item.title || item.template_file;
    document.getElementById('templatePreviewFile').textContent = item.template_file || '—';
    document.getElementById('templatePreviewTags').textContent = (item.tags || []).length ? item.tags.join(', ') : '—';
    document.getElementById('templatePreviewImages').textContent = String((item.images || []).length);
    document.getElementById('templatePreviewText').textContent = item.text || '(Template has no text)';
  }

  function renderTemplatePicker(data) {
    templatesSnapshot = data.templates || [];
    const activeTemplate = String((data.posting || {}).template_file || '').trim();
    const sel = document.getElementById('templateSelect');
    const currentSelection = sel.value || '';

    if (!templatesSnapshot.length) {
      sel.innerHTML = '<option value="">No templates found</option>';
      updateTemplatePreview('');
      return;
    }

    sel.innerHTML = templatesSnapshot.map(t => {
      const title = t.title || t.template_file;
      const isActive = t.template_file === activeTemplate;
      const label = isActive ? `★ ${esc(title)} (${esc(t.template_file)})` : `${esc(title)} (${esc(t.template_file)})`;
      return `<option value="${esc(t.template_file)}">${label}</option>`;
    }).join('');

    // Preserve user's current selection if still valid, otherwise fall back to active template
    if (currentSelection && templatesSnapshot.some(t => t.template_file === currentSelection)) {
      sel.value = currentSelection;
    } else if (activeTemplate && templatesSnapshot.some(t => t.template_file === activeTemplate)) {
      sel.value = activeTemplate;
    }
    updateTemplatePreview(sel.value || '');
  }

  async function openTemplateModal(accountId = '') {
    const target = (accountId || '').trim();
    if (target && target !== selectedAccount) {
      selectedAccount = target;
      resetGroupPaging();
      await loadState();
    }
    if (!selectedAccount) {
      toast('Please select an account first.', true);
      return;
    }
    const isComment = activeTab === 'autocomment';
    templateModalContext = isComment ? 'comment' : 'post';
    document.getElementById('templateModalTitle').textContent = isComment ? 'Choose Comment Template' : 'Choose Post Template';
    document.getElementById('applyTemplateBtn').textContent  = isComment ? '💬 Use for Comment'   : '✅ Use Template';
    const sel = document.getElementById('templateSelect');
    if (isComment && selectedCommentTemplate && templatesSnapshot.some(t => t.template_file === selectedCommentTemplate)) {
      sel.value = selectedCommentTemplate;
    }
    document.getElementById('templateModal').classList.add('show');
    updateTemplatePreview(sel ? sel.value : '');
  }

  function renderAccounts(data) {
    const rows = data.accounts || [];
    const on = rows.filter(x => x.enabled).length;
    document.getElementById('statTotal').textContent    = rows.length;
    document.getElementById('statEnabled').textContent  = on;
    document.getElementById('statDisabled').textContent = Math.max(0, rows.length - on);
    document.getElementById('activeName').textContent   = data.active_account || '—';

    const sel = document.getElementById('accountSelect');
    sel.innerHTML = rows.map(a =>
      `<option value="${esc(a.id)}">${esc(a.id)}${a.enabled ? '' : ' (off)'}</option>`
    ).join('');
    if (selectedAccount) sel.value = selectedAccount;

    const tbody = document.getElementById('accountsBody');
    if (!rows.length) {
      tbody.innerHTML = '<tr><td colspan="3"><div class="empty"><span class="empty-ico">🫙</span><span class="empty-txt">No accounts yet</span></div></td></tr>';
      return;
    }
    tbody.innerHTML = rows.map(a => {
      const sp  = a.enabled
        ? `<span class="pill p-green">● On</span>`
        : `<span class="pill p-orange">● Off</span>`;
      const btns = [
        `<button class="sm-btn btn-primary" type="button" onclick="openTemplateModal('${esc(a.id)}')" title="Set template" aria-label="Set template">🧩</button>`,
        `<button class="sm-btn" type="button" onclick="callAction('setup_session','${esc(a.id)}')" title="Login / Setup session" aria-label="Login">🔐</button>`,
        `<button class="sm-btn" type="button" onclick="callAction('test_session','${esc(a.id)}')" title="Test session validity" aria-label="Test session">🔍</button>`,
        `<button class="sm-btn btn-yellow" type="button" onclick="setActiveAccountFromList('${esc(a.id)}')" title="Set as active account" aria-label="Set as active account">⚡</button>`,
        a.enabled
          ? `<button class="sm-btn" type="button" onclick="callAction('disable_account','${esc(a.id)}')" title="Disable account" aria-label="Disable account">⏸</button>`
          : `<button class="sm-btn btn-green" type="button" onclick="callAction('enable_account','${esc(a.id)}')" title="Enable account" aria-label="Enable account">▶</button>`,
      ];
      if (!a.is_active) btns.push(`<button class="sm-btn btn-red" type="button" onclick="callAction('delete_account','${esc(a.id)}')" title="Delete account" aria-label="Delete account">🗑</button>`);
      return `<tr>
        <td>
          <a href="#" class="mono" style="color:var(--accent);font-weight:700;text-decoration:none"
             onclick="selectAccount('${esc(a.id)}');return false;">${esc(a.id)}</a>
        </td>
        <td>${sp}</td>
        <td><div style="display:flex;gap:5px;flex-wrap:wrap">${btns.join('')}</div></td>
      </tr>`;
    }).join('');
  }

  function renderGroups(data) {
    const raw = data.groups || [];
    const txt = (document.getElementById('groupFilter').value || '').trim().toLowerCase();
    const sf  = document.getElementById('groupStatusFilter').value || 'all';
    const perPageRaw = document.getElementById('groupPerPage').value || '20';
    const showAll = perPageRaw === 'all';
    const perPage = showAll ? 0 : Math.max(1, parseInt(perPageRaw, 10) || 20);
    const rows = raw.filter(g => {
      const m  = !txt || String(g.id).toLowerCase().includes(txt) || String(g.name).toLowerCase().includes(txt);
      const inc = !!g.included;
      return m && (sf === 'all' || (sf === 'included' && inc) || (sf === 'excluded' && !inc));
    });

    document.getElementById('groupCount').textContent = `${rows.length} / ${raw.length}`;

    const totalPages = showAll ? 1 : Math.max(1, Math.ceil(rows.length / perPage));
    if (groupPage > totalPages) groupPage = totalPages;
    if (groupPage < 1) groupPage = 1;

    const pageRows = showAll
      ? rows
      : rows.slice((groupPage - 1) * perPage, groupPage * perPage);

    const prevBtn = document.getElementById('groupPrevPage');
    const nextBtn = document.getElementById('groupNextPage');
    const info = document.getElementById('groupPageInfo');

    if (!rows.length) {
      info.textContent = 'Page 0 / 0';
      prevBtn.disabled = true;
      nextBtn.disabled = true;
    } else {
      info.textContent = `Page ${groupPage} / ${totalPages}`;
      prevBtn.disabled = groupPage <= 1;
      nextBtn.disabled = groupPage >= totalPages;
    }

    const tbody = document.getElementById('groupsBody');
    if (!pageRows.length) {
      tbody.innerHTML = '<tr><td colspan="5"><div class="empty"><span class="empty-ico">🫙</span><span class="empty-txt">No groups match filter</span></div></td></tr>';
      return;
    }
    tbody.innerHTML = pageRows.map(g => {
      const inc = !!g.included;
      return `<tr>
        <td class="mono">${esc(g.id)}</td>
        <td style="font-weight:600">${esc(g.name)}</td>
        <td><a href="${esc(g.url)}" target="_blank" style="color:var(--accent);font-size:12px;font-weight:700">↗ Open</a></td>
        <td><span class="pill ${inc ? 'p-green' : 'p-orange'}">${inc ? '✓ In' : '✕ Out'}</span></td>
        <td>
          <button class="sm-btn ${inc ? 'btn-red' : 'btn-green'}" type="button"
            onclick="toggleGroupInclude('${inc ? 'exclude' : 'include'}_group','${esc(g.id)}')">
            ${inc ? 'Exclude' : 'Include'}
          </button>
        </td>
      </tr>`;
    }).join('');
  }

  function renderQuickActions(data) {
    const runner = data.run_control || {};
    const isActive = !!runner.is_active;
    const isSelectedRunner = !!runner.is_selected_account;
    const status = String(runner.status || 'idle');
    const holder = document.getElementById('selectedActions');

    const buttons = [
      '<button type="button" data-action="scrape_groups" class="btn-yellow">🕷️ Scrape Groups</button>',
    ];

    if (!isActive || !isSelectedRunner) {
      buttons.push('<button type="button" data-action="run_once_dry">🧪 Run Dry</button>');
      buttons.push('<button type="button" data-action="run_once_live" class="btn-green">🚀 Run Live</button>');
    } else if (status === 'paused') {
      buttons.push('<button type="button" data-action="resume_run" class="btn-green">▶ Resume</button>');
      buttons.push('<button type="button" data-action="stop_run" class="btn-red">■ Stop</button>');
    } else if (status === 'stopping') {
      buttons.push('<button type="button" disabled class="btn-yellow">⏳ Stopping…</button>');
    } else {
      buttons.push('<button type="button" data-action="pause_run" class="btn-yellow">⏸ Pause</button>');
      buttons.push('<button type="button" data-action="stop_run" class="btn-red">■ Stop</button>');
    }

    if (isActive && !isSelectedRunner && runner.account_id) {
      buttons.push(`<button type="button" disabled>Running on ${esc(runner.account_id)}</button>`);
    }

    holder.innerHTML = buttons.join('');
  }

  function updateScheduleTypeVisibility() {
    const type = (document.getElementById('schType').value || '').trim();
    const weekWrap = document.getElementById('schWeekdaysWrap');
    const dateTimeWrap = document.getElementById('schDateTimeWrap');
    const timeOnlyWrap = document.getElementById('schTimeOnlyWrap');
    const isOneTime = (type === 'specific_datetime');
    if (dateTimeWrap) dateTimeWrap.style.display = isOneTime ? '' : 'none';
    if (timeOnlyWrap) timeOnlyWrap.style.display = isOneTime ? 'none' : '';
    if (weekWrap) weekWrap.style.display = (type === 'weekly') ? 'flex' : 'none';
  }

  async function disableSchedule(scheduleId) {
    if (!selectedAccount) { toast('Please select an account first.', true); return; }
    if (!scheduleId) { toast('Invalid schedule id.', true); return; }
    await callAction('stop_schedule', selectedAccount, '', '', { schedule_id: scheduleId });
  }

  async function enableSchedule(scheduleId) {
    if (!selectedAccount) { toast('Please select an account first.', true); return; }
    if (!scheduleId) { toast('Invalid schedule id.', true); return; }
    await callAction('enable_schedule', selectedAccount, '', '', { schedule_id: scheduleId });
  }

  async function deleteSchedule(scheduleId) {
    if (!selectedAccount) { toast('Please select an account first.', true); return; }
    if (!scheduleId) { toast('Invalid schedule id.', true); return; }
    if (!confirm(`Delete schedule "${scheduleId}"? This cannot be undone.`)) return;
    await callAction('delete_schedule', selectedAccount, '', '', { schedule_id: scheduleId });
  }

  function _fmtSchType(type) {
    if (type === 'specific_datetime') return 'One Time';
    if (type === 'daily') return 'Daily';
    if (type === 'weekly') return 'Weekly';
    return String(type || 'unknown');
  }

  function _schTimeInfo(item) {
    const type = String(item.type || '');
    if (type === 'specific_datetime') {
      const raw = item.run_at || item.specific_datetime || '';
      if (raw) { const m = raw.match(/^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2})/); return m ? m[1].replace('T', ' ') : raw.slice(0, 16); }
      return '-';
    }
    const t = item.time || '';
    if (type === 'weekly') {
      const days = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
      const dnames = (item.weekdays || []).map(d => days[d] || String(d)).join(', ');
      return `${dnames} ${t}`;
    }
    return t;
  }

  function renderScheduleState(data) {
    const sch = data.schedule_control || {};
    const savedList = (data.saved_schedules || sch.records || []).slice();
    const info = document.getElementById('scheduleInfo');
    const startBtn = document.getElementById('startScheduleBtn');
    const stopBtn = document.getElementById('stopScheduleBtn');
    const listEl = document.getElementById('scheduleRecords');
    const active = !!sch.is_active;
    const runningId = String(sch.running_schedule_id || sch.spec?.id || '');
    startBtn.disabled = false;
    const hasEnabled = savedList.some(item => !!item.enabled);
    stopBtn.disabled = !hasEnabled;
    if (!active) {
      info.textContent = sch.message || (hasEnabled ? 'Schedules saved and waiting.' : 'No schedule active.');
    } else {
      const next = sch.next_run_at ? new Date(sch.next_run_at).toLocaleString() : '-';
      const last = sch.last_run_at ? new Date(sch.last_run_at).toLocaleString() : '-';
      const result = sch.last_result ? ` | Result: ${sch.last_result}` : '';
      info.textContent = `Status: ${sch.status} | Next: ${next} | Last: ${last}${result}`;
    }

    if (!savedList.length) {
      listEl.innerHTML = '<div class="mono">No schedules saved.</div>';
    } else {
      const rows = savedList.map(item => {
        const id = String(item.id || '');
        const enabled = !!item.enabled;
        const isRunning = active && runningId && runningId === id;
        const typeLabel = _fmtSchType(item.type);
        const timeInfo = _schTimeInfo(item);
        const next = item.next_run_at ? new Date(item.next_run_at).toLocaleString() : '-';
        const status = String(item.last_status || 'pending');
        const badgeClass = isRunning ? 'p-orange' : (enabled ? 'p-green' : 'p-gray');
        const badgeLabel = isRunning ? '▶ Running' : (enabled ? 'Enabled' : 'Disabled');
        const toggleBtn = enabled
          ? `<button class="btn-red" type="button" onclick="disableSchedule('${esc(id)}')" title="Disable">Disable</button>`
          : `<button class="btn-green" type="button" onclick="enableSchedule('${esc(id)}')" title="Enable">Enable</button>`;
        return `
          <div class="schedule-item">
            <div class="meta">
              <strong>${esc(typeLabel)}</strong>
              <span>${esc(timeInfo)}</span>
              <span>Next: ${esc(next)} · Status: ${esc(status)}</span>
            </div>
            <div class="actions">
              <span class="pill ${badgeClass}">${badgeLabel}</span>
              ${toggleBtn}
              <button class="btn-red" type="button" onclick="deleteSchedule('${esc(id)}')" title="Delete">🗑️</button>
            </div>
          </div>
        `;
      });
      listEl.innerHTML = rows.join('');
    }

    // Prefill schedule inputs from the latest spec (only when it changes).
    const isSelectedSchedule = !!sch.is_selected_account;
    let spec = {};
    if (isSelectedSchedule && sch && typeof sch.spec === 'object' && sch.spec.type) {
      spec = sch.spec;
    }
    const signature = `${selectedAccount || ''}::${JSON.stringify(spec || {})}`;
    if (Object.keys(spec).length > 0 && signature !== lastScheduleSpecSignature) {
      const typeEl = document.getElementById('schType');
      if (typeEl && document.activeElement !== typeEl) typeEl.value = String(spec.type || 'specific_datetime');
      if (spec.type === 'specific_datetime') {
        const dtEl = document.getElementById('schDateTime');
        const raw = spec.run_at || spec.specific_datetime || '';
        if (dtEl && raw) { const m = raw.match(/^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2})/); if (m) dtEl.value = m[1]; }
      } else {
        const tEl = document.getElementById('schTimeOnly');
        if (tEl && spec.time && document.activeElement !== tEl) tEl.value = spec.time;
      }
      document.querySelectorAll('.schDay').forEach(el => {
        const day = parseInt(el.value || '-1', 10);
        el.checked = Array.isArray(spec.weekdays) ? spec.weekdays.includes(day) : false;
      });
      lastScheduleSpecSignature = signature;
    }
    updateScheduleTypeVisibility();
  }

  function renderPresets(data) {
    presetsSnapshot = data.presets || [];
    presetStatusSnapshot = data.preset || { enabled: false, name: '' };
    const sel = document.getElementById('presetSelect');

    const options = [
      '<option value="">None (Use Config)</option>',
      ...presetsSnapshot.map(p => `<option value="${esc(p.filename)}">${esc(p.name || p.filename)}</option>`)
    ];
    sel.innerHTML = options.join('');
    if (presetStatusSnapshot.enabled && presetStatusSnapshot.name) {
      sel.value = presetStatusSnapshot.name;
    } else {
      sel.value = '';
    }
    lastPresetSelectionValue = sel.value;

    const info = document.getElementById('presetInfo');
    if (presetStatusSnapshot.enabled && presetStatusSnapshot.name) {
      const pretty = (presetStatusSnapshot.details && presetStatusSnapshot.details.name)
        ? presetStatusSnapshot.details.name
        : presetStatusSnapshot.name;
      info.textContent = `Active: ${pretty} (${presetStatusSnapshot.name})`;
    } else {
      info.textContent = 'Using config.yaml';
    }

    const updateBtn = document.getElementById('updatePresetBtn');
    const deleteBtn = document.getElementById('deletePresetBtn');
    updateBtn.textContent = '📝';
    deleteBtn.textContent = '🗑️';
    updateBtn.disabled = !sel.value;
    deleteBtn.disabled = !sel.value;
    if (sel.value) {
      updateBtn.title = `Update preset: ${sel.value}`;
      updateBtn.setAttribute('aria-label', `Update preset: ${sel.value}`);
      deleteBtn.title = `Delete preset: ${sel.value}`;
      deleteBtn.setAttribute('aria-label', `Delete preset: ${sel.value}`);
    } else {
      updateBtn.title = 'Update selected preset';
      updateBtn.setAttribute('aria-label', 'Update selected preset');
      deleteBtn.title = 'Delete selected preset';
      deleteBtn.setAttribute('aria-label', 'Delete selected preset');
    }
  }

  async function loadState() {
    const q = selectedAccount ? `?account=${encodeURIComponent(selectedAccount)}` : '';
    try {
      const res = await fetch('/api/state' + q, { cache: 'no-store' });
      const data = await res.json();
      selectedAccount = data.selected_account || data.active_account || '';
      groupsSnapshot  = data.groups || [];
      browserRulesSnapshot = data.browser || {};
      globalGroupsSnapshot = data.global_groups || {};
      globalPostingSnapshot = data.global_posting || {};
      renderTemplatePicker(data);
      document.getElementById('selectedTitle').textContent =
        selectedAccount ? `Actions — ${selectedAccount}` : 'Select an account';
      suppressUnsavedMark = true;
      renderPresets(data);
      renderQuickActions(data);
      renderScheduleState(data);
      renderAccounts(data);
      renderGroups({ groups: groupsSnapshot });
      suppressUnsavedMark = false;
      renderCommenterState(data);
      renderCommentScheduleState(data);
      renderCommentGroups(groupsSnapshot);
      renderCommentSettings(data);
      setUpdated();
    } catch {
      document.getElementById('lastUpdated').textContent = 'Error';
    }
  }

  function selectAccount(id) {
    selectedAccount = id;
    resetGroupPaging();
    lastScheduleSpecSignature = '';
    loadState();
  }

  async function setActiveAccountFromList(id) {
    await callAction('set_active', id);
  }

  async function toggleGroupInclude(action, groupId) {
    await callAction(action, selectedAccount, groupId);
  }

  document.getElementById('addAccountBtn').addEventListener('click', async () => {
    const inp = document.getElementById('newAccountId');
    const id  = (inp.value || '').trim();
    if (!id) { toast('Account id is required.', true); return; }
    await callAction('add_account', id);
    inp.value = '';
  });

  document.getElementById('selectedActions').addEventListener('click', async ev => {
    const btn = ev.target.closest('button[data-action]');
    if (!btn) return;
    if (!selectedAccount) { toast('Please select an account first.', true); return; }
    
    const action = btn.dataset.action;

    // Ask to save/discard unsaved edits before running actions.
    const safeActions = new Set(['test_session', 'setup_session', 'scrape_groups', 'run_once_dry', 'run_once_live']);
    if (hasUnsavedChanges && safeActions.has(action)) {
      showUnsavedModal(async () => {
        if (action === 'run_once_live') {
          document.getElementById('runLiveModal').classList.add('show');
          return;
        }
        await callAction(action, selectedAccount);
      });
      return;
    }
    
    // Special handling for run_once_live - show modal to ask about resetting posted log
    if (action === 'run_once_live') {
      document.getElementById('runLiveModal').classList.add('show');
      return;
    }
    
    await callAction(action, selectedAccount);
  });

  document.getElementById('closeRunLiveModal').addEventListener('click', () => {
    document.getElementById('runLiveModal').classList.remove('show');
  });
  document.getElementById('runLiveModal').addEventListener('click', ev => {
    if (ev.target && ev.target.id === 'runLiveModal') {
      document.getElementById('runLiveModal').classList.remove('show');
    }
  });
  document.getElementById('runLiveResetBtn').addEventListener('click', async () => {
    document.getElementById('runLiveModal').classList.remove('show');
    await callAction('clear_posted_log', selectedAccount);
    await callAction('run_once_live', selectedAccount);
  });
  document.getElementById('runLiveKeepBtn').addEventListener('click', async () => {
    document.getElementById('runLiveModal').classList.remove('show');
    await callAction('run_once_live', selectedAccount);
  });

  document.getElementById('closeTemplateModal').addEventListener('click', closeTemplateModal);
  document.getElementById('templateModal').addEventListener('click', ev => {
    if (ev.target && ev.target.id === 'templateModal') closeTemplateModal();
  });
  document.getElementById('closeTemplateEditorModal').addEventListener('click', closeTemplateEditorModal);
  document.getElementById('templateEditorModal').addEventListener('click', ev => {
    if (ev.target && ev.target.id === 'templateEditorModal') closeTemplateEditorModal();
  });
  document.getElementById('openEditTemplateBtn').addEventListener('click', () => {
    const selectedTemplate = (document.getElementById('templateSelect').value || '').trim();
    if (!selectedTemplate) {
      toast('Select a template first.', true);
      return;
    }
    openTemplateEditorModal('edit');
  });
  document.getElementById('openAddTemplateBtn').addEventListener('click', () => {
    openTemplateEditorModal('add');
  });
  document.getElementById('templateSelect').addEventListener('change', ev => {
    updateTemplatePreview((ev.target.value || '').trim());
  });
  document.getElementById('tplAddImageBtn').addEventListener('click', () => {
    const inp = document.getElementById('tplNewImage');
    const value = (inp.value || '').trim();
    if (!value) {
      toast('Image path is required.', true);
      return;
    }
    templateEditImages.push(value);
    inp.value = '';
    renderTemplateImagesEditor();
  });
  document.getElementById('tplBrowseImageBtn').addEventListener('click', () => {
    document.getElementById('tplImageFileInput').click();
  });
  document.getElementById('tplImageFileInput').addEventListener('change', async (ev) => {
    const files = ev.target.files;
    if (!files || !files.length) return;
    for (const file of files) {
      const formData = new FormData();
      formData.append('image', file);
      try {
        const res = await fetch('/api/upload-image', { method: 'POST', body: formData });
        const data = await res.json();
        if (data.ok) {
          templateEditImages.push(data.path);
          renderTemplateImagesEditor();
          toast('Image uploaded: ' + data.path);
        } else {
          toast(data.error || 'Upload failed', true);
        }
      } catch (err) {
        toast('Upload error: ' + err.message, true);
      }
    }
    ev.target.value = '';
  });
  document.getElementById('applyTemplateBtn').addEventListener('click', async () => {
    const selectedTemplate = (document.getElementById('templateSelect').value || '').trim();
    if (!selectedTemplate) {
      toast('Template is required.', true);
      return;
    }
    if (templateModalContext === 'comment') {
      const minEl = document.getElementById('cmMinDelay');
      const maxEl = document.getElementById('cmMaxDelay');
      const min = parseInt((minEl ? minEl.value : '1') || '1', 10) || 1;
      const max = parseInt((maxEl ? maxEl.value : '3') || '3', 10) || 3;
      await callAction('update_commenting_rules', selectedAccount || '', '', '', {
        template_file: selectedTemplate,
        min_delay_minutes: min,
        max_delay_minutes: max,
      });
      closeTemplateModal();
      return;
    }
    if (!selectedAccount) {
      toast('Please select an account first.', true);
      return;
    }
    await callAction('set_template', selectedAccount, '', selectedTemplate);
    closeTemplateModal();
  });
  document.getElementById('saveTemplateBtn').addEventListener('click', async () => {
    const selectedTemplate = (document.getElementById('templateSelect').value || '').trim();
    if (!selectedTemplate) {
      toast('Select a template first.', true);
      return;
    }
    const payload = buildTemplatePayloadFromEditor();
    await callAction('save_template', selectedAccount, '', selectedTemplate, payload);
    updateTemplatePreview(selectedTemplate);
    closeTemplateEditorModal();
  });
  document.getElementById('createTemplateBtn').addEventListener('click', async () => {
    const payload = buildTemplatePayloadFromEditor();
    if (!String(payload.text || '').trim()) {
      toast('Post text is required to create a new post.', true);
      return;
    }
    const result = await callAction('create_template', selectedAccount, '', '', payload);
    if (!result || !result.ok) {
      return;
    }
    // Get the new template filename from the result message
    const match = (result.message || '').match(/Created template '([^']+)'/);
    const newFile = match ? match[1] : null;
    if (newFile && selectedAccount) {
      // Auto-set the new template as active for this account
      await callAction('set_template', selectedAccount, '', newFile);
    }
    closeTemplateEditorModal();
  });
  document.getElementById('deleteTemplateBtn').addEventListener('click', async () => {
    const selectedTemplate = (document.getElementById('templateSelect').value || '').trim();
    if (!selectedTemplate) {
      toast('Select a template first.', true);
      return;
    }
    if (!confirm(`Delete template "${selectedTemplate}"? This cannot be undone.`)) {
      return;
    }
    await callAction('delete_template', selectedAccount, '', selectedTemplate);
  });

  document.getElementById('groupFilter').addEventListener('input', () => {
    resetGroupPaging();
    renderGroups({ groups: groupsSnapshot });
  });
  document.getElementById('groupStatusFilter').addEventListener('change', () => {
    resetGroupPaging();
    renderGroups({ groups: groupsSnapshot });
  });
  document.getElementById('groupPerPage').addEventListener('change', () => {
    resetGroupPaging();
    renderGroups({ groups: groupsSnapshot });
  });
  document.getElementById('groupPrevPage').addEventListener('click', () => {
    groupPage = Math.max(1, groupPage - 1);
    renderGroups({ groups: groupsSnapshot });
  });
  document.getElementById('groupNextPage').addEventListener('click', () => {
    groupPage += 1;
    renderGroups({ groups: groupsSnapshot });
  });
  document.getElementById('schType').addEventListener('change', updateScheduleTypeVisibility);
  document.getElementById('startScheduleBtn').addEventListener('click', async () => {
    if (!selectedAccount) { toast('Please select an account first.', true); return; }
    const type = (document.getElementById('schType').value || '').trim();
    let timeVal = '';
    let specificVal = '';
    if (type === 'specific_datetime') {
      specificVal = (document.getElementById('schDateTime').value || '').trim();
      if (!specificVal) { toast('Please set a date and time.', true); return; }
    } else {
      timeVal = (document.getElementById('schTimeOnly').value || '').trim();
      if (!timeVal) { toast('Please set a time.', true); return; }
    }
    const weekdays = Array.from(document.querySelectorAll('.schDay'))
      .filter(el => el.checked)
      .map(el => parseInt(el.value || '-1', 10))
      .filter(v => !Number.isNaN(v) && v >= 0 && v <= 6);
    if (type === 'weekly' && weekdays.length === 0) {
      toast('Please select at least one day for a weekly schedule.', true); return;
    }
    await callAction('start_schedule', selectedAccount, '', '', {
      type, time: timeVal, specific_datetime: specificVal, weekdays, dry_run: false,
    });
  });
  document.getElementById('stopScheduleBtn').addEventListener('click', async () => {
    if (!selectedAccount) {
      toast('Please select an account first.', true);
      return;
    }
    await callAction('stop_schedule', selectedAccount, '', '', { schedule_id: '' });
  });
  document.getElementById('presetSelect').addEventListener('change', async ev => {
    const filename = (ev.target.value || '').trim();
    const revertSelection = () => {
      ev.target.value = lastPresetSelectionValue;
    };
    if (!filename) {
      showPresetActionModal(
        'Disable Preset',
        'Switch to config.yaml values only?',
        async () => {
          await callAction('disable_preset', selectedAccount || '');
        },
        'Use Config'
      );
      revertSelection();
      return;
    }
    showPresetActionModal(
      'Apply Preset',
      `Apply preset "${filename}" now?`,
      async () => {
        await callAction('apply_preset', selectedAccount || '', '', '', { preset_filename: filename });
      },
      'Apply'
    );
    revertSelection();
  });
  document.getElementById('saveNewPresetBtn').addEventListener('click', () => {
    runPendingAfterPresetSave = false;
    document.getElementById('presetNameInput').value = '';
    document.getElementById('presetSaveModal').classList.add('show');
  });
  document.getElementById('updatePresetBtn').addEventListener('click', async () => {
    const filename = (document.getElementById('presetSelect').value || '').trim();
    if (!filename) {
      toast('Select a preset first.', true);
      return;
    }
    await callAction('update_preset', selectedAccount || '', '', '', { preset_filename: filename });
  });
  document.getElementById('deletePresetBtn').addEventListener('click', async () => {
    const filename = (document.getElementById('presetSelect').value || '').trim();
    if (!filename) {
      toast('Select a preset first.', true);
      return;
    }
    showPresetActionModal(
      'Delete Preset',
      `Delete preset "${filename}"? This cannot be undone.`,
      async () => {
        await callAction('delete_preset', selectedAccount || '', '', '', { preset_filename: filename });
      },
      'Delete'
    );
  });
  document.getElementById('closePresetSaveModal').addEventListener('click', () => {
    runPendingAfterPresetSave = false;
    document.getElementById('presetSaveModal').classList.remove('show');
  });
  document.getElementById('presetSaveModal').addEventListener('click', ev => {
    if (ev.target && ev.target.id === 'presetSaveModal') {
      runPendingAfterPresetSave = false;
      document.getElementById('presetSaveModal').classList.remove('show');
    }
  });
  document.getElementById('confirmSavePresetBtn').addEventListener('click', async () => {
    const name = (document.getElementById('presetNameInput').value || '').trim();
    if (!name) {
      toast('Preset name is required.', true);
      return;
    }
    const result = await callAction('save_preset', selectedAccount || '', '', '', { preset_name: name });
    if (result && result.ok) {
      document.getElementById('presetSaveModal').classList.remove('show');
      if (runPendingAfterPresetSave) {
        runPendingAfterPresetSave = false;
        await runPendingUnsavedAction();
      } else {
        await loadState();
      }
    }
  });
  document.getElementById('closeUnsavedModal').addEventListener('click', () => {
    pendingUnsavedAction = null;
    runPendingAfterPresetSave = false;
    document.getElementById('unsavedChangesModal').classList.remove('show');
  });
  document.getElementById('unsavedChangesModal').addEventListener('click', ev => {
    if (ev.target && ev.target.id === 'unsavedChangesModal') {
      pendingUnsavedAction = null;
      runPendingAfterPresetSave = false;
      document.getElementById('unsavedChangesModal').classList.remove('show');
    }
  });
  document.getElementById('saveUnsavedToPresetBtn').addEventListener('click', () => {
    runPendingAfterPresetSave = true;
    document.getElementById('unsavedChangesModal').classList.remove('show');
    document.getElementById('saveNewPresetBtn').click();
  });
  document.getElementById('saveUnsavedToConfigBtn').addEventListener('click', async () => {
    // Current edits are already written to config through rule/group/account actions.
    // This path just proceeds without creating/updating a preset.
    clearUnsaved();
    document.getElementById('unsavedChangesModal').classList.remove('show');
    toast('Proceeding with current config values.');
    await runPendingUnsavedAction();
  });
  document.getElementById('discardUnsavedBtn').addEventListener('click', async () => {
    // Keep unsaved flag because this action means "cancel moving forward".
    document.getElementById('unsavedChangesModal').classList.remove('show');
    pendingUnsavedAction = null;
    runPendingAfterPresetSave = false;
  });
  document.getElementById('closePresetActionModal').addEventListener('click', closePresetActionModal);
  document.getElementById('presetActionModal').addEventListener('click', ev => {
    if (ev.target && ev.target.id === 'presetActionModal') closePresetActionModal();
  });
  document.getElementById('confirmPresetActionBtn').addEventListener('click', async () => {
    const fn = pendingPresetAction;
    closePresetActionModal();
    if (typeof fn === 'function') await fn();
  });
  document.getElementById('openBrowserRulesBtn').addEventListener('click', openBrowserRulesModal);
  document.getElementById('openGroupsRulesBtn').addEventListener('click', openGroupsRulesModal);
  document.getElementById('openPostingRulesBtn').addEventListener('click', openPostingRulesModal);
  document.getElementById('closeBrowserRulesModal').addEventListener('click', closeBrowserRulesModal);
  document.getElementById('browserRulesModal').addEventListener('click', ev => {
    if (ev.target && ev.target.id === 'browserRulesModal') closeBrowserRulesModal();
  });
  document.getElementById('closeGroupsRulesModal').addEventListener('click', closeGroupsRulesModal);
  document.getElementById('groupsRulesModal').addEventListener('click', ev => {
    if (ev.target && ev.target.id === 'groupsRulesModal') closeGroupsRulesModal();
  });
  document.getElementById('closePostingRulesModal').addEventListener('click', closePostingRulesModal);
  document.getElementById('postingRulesModal').addEventListener('click', ev => {
    if (ev.target && ev.target.id === 'postingRulesModal') closePostingRulesModal();
  });
  document.getElementById('saveBrowserRulesBtn').addEventListener('click', async () => {
    const payload = {
      headless: document.getElementById('brHeadless').value === 'true',
      humanize: document.getElementById('brHumanize').value === 'true',
      fullscreen: document.getElementById('brFullscreen').value === 'true',
      locale: (document.getElementById('brLocale').value || '').trim(),
      timezone: (document.getElementById('brTimezone').value || '').trim(),
      screen_max_width: parseInt((document.getElementById('brScreenMaxWidth').value || '').trim(), 10),
      screen_max_height: parseInt((document.getElementById('brScreenMaxHeight').value || '').trim(), 10),
      window_width: parseInt((document.getElementById('brWindowWidth').value || '').trim(), 10),
      window_height: parseInt((document.getElementById('brWindowHeight').value || '').trim(), 10),
    };

    if (!payload.locale || !payload.timezone) {
      toast('Locale and timezone are required.', true);
      return;
    }
    if ([payload.screen_max_width, payload.screen_max_height, payload.window_width, payload.window_height].some(Number.isNaN)) {
      toast('Screen/window sizes must be valid integers.', true);
      return;
    }

    const result = await callAction('update_browser_rules', selectedAccount || '', '', '', {}, payload);
    if (result && result.ok) {
      closeBrowserRulesModal();
    }
  });
  document.getElementById('saveGroupsRulesBtn').addEventListener('click', async () => {
    const payload = {
      rescrape_every_days: parseInt((document.getElementById('grRescrapeDays').value || '').trim(), 10),
      idle_rounds_to_stop: parseInt((document.getElementById('grIdleRounds').value || '').trim(), 10),
      scroll_wait_ms: parseInt((document.getElementById('grScrollWaitMs').value || '').trim(), 10),
    };
    if (Object.values(payload).some(Number.isNaN)) {
      toast('Groups rules values must be valid integers.', true);
      return;
    }
    const result = await callAction('update_groups_rules', selectedAccount || '', '', '', {}, {}, payload, {});
    if (result && result.ok) {
      closeGroupsRulesModal();
    }
  });
  document.getElementById('savePostingRulesBtn').addEventListener('click', async () => {
    const payload = {
      template_file: (document.getElementById('prTemplateFile').value || '').trim(),
      min_delay_minutes: parseInt((document.getElementById('prMinDelay').value || '').trim(), 10),
      max_delay_minutes: parseInt((document.getElementById('prMaxDelay').value || '').trim(), 10),
      rest_every_n_posts: parseInt((document.getElementById('prRestEvery').value || '').trim(), 10),
      rest_duration_minutes: parseInt((document.getElementById('prRestDuration').value || '').trim(), 10),
      dry_run: document.getElementById('prDryRun').value === 'true',
      auto_skip: document.getElementById('prAutoSkip').value === 'true',
      pause_on_captcha: document.getElementById('prPauseOnCaptcha').value === 'true',
      stop_on_rate_limit: document.getElementById('prStopOnRateLimit').value === 'true',
    };
    if (!payload.template_file) {
      toast('Template file is required.', true);
      return;
    }
    if ([
      payload.min_delay_minutes,
      payload.max_delay_minutes,
      payload.rest_every_n_posts,
      payload.rest_duration_minutes,
    ].some(Number.isNaN)) {
      toast('Posting rules numeric values must be valid integers.', true);
      return;
    }
    const result = await callAction('update_posting_rules', selectedAccount || '', '', '', {}, {}, {}, payload);
    if (result && result.ok) {
      closePostingRulesModal();
    }
  });
  document.getElementById('accountSelect').addEventListener('change', ev => {
    const v = (ev.target.value || '').trim();
    if (v) {
      resetGroupPaging();
      selectAccount(v);
    }
  });

  window.addEventListener('beforeunload', ev => {
    if (!hasUnsavedChanges) return;
    ev.preventDefault();
    ev.returnValue = 'You have unsaved preset changes.';
  });

  // ─── Auto Comment Tab ────────────────────────────────────
  let activeTab = 'autopost';
  let cmGroupPage = 1;
  let cmGroupsSnapshot = [];
  let templateModalContext = 'post'; // 'post' or 'comment'
  let selectedCommentTemplate = '';

  function switchTab(name) {
    activeTab = name;
    document.querySelectorAll('.tab-btn').forEach(btn => {
      btn.classList.toggle('active', btn.dataset.tab === name);
    });
    document.querySelectorAll('.tab-panel').forEach(panel => {
      panel.classList.toggle('hidden', panel.id !== 'tab-' + name);
    });
    const isComment = name === 'autocomment';
    const titleEl = document.getElementById('liveLogCardTitle');
    if (titleEl) titleEl.textContent = isComment ? 'Live Log — Auto Comment' : 'Live Log — Auto Post';
    const postWrap = document.getElementById('liveLogWrap');
    const cmWrap = document.getElementById('cmLiveLogWrap');
    if (postWrap) postWrap.style.display = isComment ? 'none' : '';
    if (cmWrap) cmWrap.style.display = isComment ? '' : 'none';
    const postScrollLabel = document.getElementById('logAutoScrollLabel');
    const cmScrollLabel = document.getElementById('logAutoScroll2Label');
    if (postScrollLabel) postScrollLabel.style.display = isComment ? 'none' : '';
    if (cmScrollLabel) cmScrollLabel.style.display = isComment ? '' : 'none';
  }

  document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', () => switchTab(btn.dataset.tab));
  });

  function renderCommentSettings(data) {
    const cfg = data.global_commenting || {};
    const templates = data.templates || [];
    const currentFile = String(cfg.template_file || '').trim();

    // Keep cached copies for the modal pre-fill
    lastCommentingConfig = cfg;
    lastTemplatesSnapshot = templates;

    // Update selectedCommentTemplate (but don't overwrite if modal is open in comment mode)
    if (templateModalContext !== 'comment') {
      selectedCommentTemplate = currentFile || (templates[0] && templates[0].template_file) || '';
    }

    const minEl = document.getElementById('cmMinDelay');
    const maxEl = document.getElementById('cmMaxDelay');
    if (document.activeElement !== minEl) minEl.value = cfg.min_delay_minutes ?? 1;
    if (document.activeElement !== maxEl) maxEl.value = cfg.max_delay_minutes ?? 3;
  }

  function renderCommenterState(data) {
    const ctrl = data.commenter_control || {};
    const isActive = ctrl.is_active || false;
    const status = (ctrl.status || 'idle').toLowerCase();
    const msg = ctrl.message || '';
    const acct = ctrl.account_id || '';

    const statusInfo = document.getElementById('cmStatusInfo');
    const title = document.getElementById('cmSelectedTitle');
    const startBtn = document.getElementById('startCommenterBtn');
    const stopBtn = document.getElementById('stopCommenterBtn');

    if (title) {
      if (isActive) {
        title.textContent = `Commenter running — ${acct}`;
      } else if (selectedAccount) {
        title.textContent = `Commenter ready — ${selectedAccount}`;
      } else {
        title.textContent = 'Select an account to start commenting';
      }
    }

    let statusText = '';
    if (status === 'running') statusText = `▶️ Running — ${acct}`;
    else if (status === 'stopping') statusText = `⏳ Stopping — ${acct}`;
    else if (status === 'error') statusText = `❌ Error — ${msg}`;
    else statusText = `Idle — no commenter running.`;
    if (statusInfo) statusInfo.textContent = statusText;

    if (startBtn) startBtn.disabled = isActive;
    if (stopBtn) stopBtn.disabled = !isActive;
  }

  function cmUpdateScheduleTypeVisibility() {
    const type = (document.getElementById('cmSchType').value || '').trim();
    const weekWrap = document.getElementById('cmSchWeekdaysWrap');
    const dateFromWrap = document.getElementById('cmSchDateFromWrap');
    const timeFromWrap = document.getElementById('cmSchTimeFromWrap');
    const isOneTime = (type === 'specific_datetime');
    if (dateFromWrap) dateFromWrap.style.display = isOneTime ? '' : 'none';
    if (timeFromWrap) timeFromWrap.style.display = isOneTime ? 'none' : '';
    if (weekWrap) weekWrap.style.display = (type === 'weekly') ? 'flex' : 'none';
  }

  async function disableCommentSchedule(scheduleId) {
    if (!selectedAccount) { toast('Please select an account first.', true); return; }
    if (!scheduleId) { toast('Invalid schedule id.', true); return; }
    await callAction('stop_comment_schedule', selectedAccount, '', '', { schedule_id: scheduleId });
  }

  async function enableCommentSchedule(scheduleId) {
    if (!selectedAccount) { toast('Please select an account first.', true); return; }
    if (!scheduleId) { toast('Invalid schedule id.', true); return; }
    await callAction('enable_comment_schedule', selectedAccount, '', '', { schedule_id: scheduleId });
  }

  async function deleteCommentSchedule(scheduleId) {
    if (!selectedAccount) { toast('Please select an account first.', true); return; }
    if (!scheduleId) { toast('Invalid schedule id.', true); return; }
    if (!confirm(`Delete comment schedule "${scheduleId}"? This cannot be undone.`)) return;
    await callAction('delete_comment_schedule', selectedAccount, '', '', { schedule_id: scheduleId });
  }

  let lastCmScheduleSpecSignature = '';

  function renderCommentScheduleState(data) {
    const sch = data.comment_schedule_control || {};
    const savedList = (sch.records || []).slice();
    const info = document.getElementById('cmScheduleInfo');
    const startBtn = document.getElementById('cmStartScheduleBtn');
    const stopBtn = document.getElementById('cmStopScheduleBtn');
    const listEl = document.getElementById('cmScheduleRecords');
    if (!info) return;
    const active = !!sch.is_active;
    const runningId = String(sch.running_schedule_id || sch.spec?.id || '');
    const hasEnabled = savedList.some(item => !!item.enabled);
    if (startBtn) startBtn.disabled = false;
    if (stopBtn) stopBtn.disabled = !hasEnabled;
    if (!active) {
      info.textContent = sch.message || (hasEnabled ? 'Comment schedules saved and waiting.' : 'No comment schedule active.');
    } else {
      const next = sch.next_run_at ? new Date(sch.next_run_at).toLocaleString() : '-';
      const last = sch.last_run_at ? new Date(sch.last_run_at).toLocaleString() : '-';
      info.textContent = `Status: ${sch.status} | Next: ${next} | Last: ${last}`;
    }
    if (!savedList.length) {
      listEl.innerHTML = '<div class="mono">No comment schedules saved.</div>';
    } else {
      const rows = savedList.map(item => {
        const id = String(item.id || '');
        const enabled = !!item.enabled;
        const isRunning = active && runningId && runningId === id;
        const typeLabel = _fmtSchType(item.type);
        const timeInfo = _schTimeInfo(item);
        const timeTo = item.time_to ? ` → ${item.time_to}` : '';
        const next = item.next_run_at ? new Date(item.next_run_at).toLocaleString() : '-';
        const status = String(item.last_status || 'pending');
        const badgeClass = isRunning ? 'p-orange' : (enabled ? 'p-green' : 'p-gray');
        const badgeLabel = isRunning ? '▶ Running' : (enabled ? 'Enabled' : 'Disabled');
        const toggleBtn = enabled
          ? `<button class="btn-red" type="button" onclick="disableCommentSchedule('${esc(id)}')" title="Disable">Disable</button>`
          : `<button class="btn-green" type="button" onclick="enableCommentSchedule('${esc(id)}')" title="Enable">Enable</button>`;
        return `
          <div class="schedule-item">
            <div class="meta">
              <strong>${esc(typeLabel)}</strong>
              <span>${esc(timeInfo)}${esc(timeTo)}</span>
              <span>Next: ${esc(next)} · Status: ${esc(status)}</span>
            </div>
            <div class="actions">
              <span class="pill ${badgeClass}">${badgeLabel}</span>
              ${toggleBtn}
              <button class="btn-red" type="button" onclick="deleteCommentSchedule('${esc(id)}')" title="Delete">🗑️</button>
            </div>
          </div>
        `;
      });
      listEl.innerHTML = rows.join('');
    }
    // Pre-fill inputs from active spec (only on change).
    const isSelectedSchedule = !!sch.is_selected_account;
    let spec = {};
    if (isSelectedSchedule && sch.spec && typeof sch.spec === 'object' && sch.spec.type) {
      spec = sch.spec;
    }
    const sig = `${selectedAccount || ''}::${JSON.stringify(spec)}`;
    if (sig !== lastCmScheduleSpecSignature) {
      lastCmScheduleSpecSignature = sig;
      if (spec.type) {
        const typeEl = document.getElementById('cmSchType');
        if (typeEl && document.activeElement !== typeEl) typeEl.value = spec.type;
        if (spec.type === 'specific_datetime') {
          const dtEl = document.getElementById('cmSchDateFrom');
          const raw = spec.run_at || spec.specific_datetime || '';
          if (dtEl && raw) { const m = raw.match(/^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2})/); if (m) dtEl.value = m[1]; }
        } else {
          const fromEl = document.getElementById('cmSchTimeFrom');
          if (fromEl && spec.time && document.activeElement !== fromEl) fromEl.value = spec.time;
        }
        if (spec.time_to) {
          const toEl = document.getElementById('cmSchTimeTo');
          if (toEl && document.activeElement !== toEl) toEl.value = spec.time_to;
        }
        document.querySelectorAll('.cmSchDay').forEach(el => {
          const day = parseInt(el.value || '-1', 10);
          el.checked = Array.isArray(spec.weekdays) ? spec.weekdays.includes(day) : false;
        });
        cmUpdateScheduleTypeVisibility();
      }
    }
  }

  function cmResetGroupPaging() {
    cmGroupPage = 1;
  }

  function renderCommentGroups(groups) {
    cmGroupsSnapshot = groups || [];
    const filter = (document.getElementById('cmGroupFilter').value || '').toLowerCase().trim();
    const statusFilter = (document.getElementById('cmGroupStatusFilter').value || 'all');
    const perPageRaw = (document.getElementById('cmGroupPerPage').value || '20');
    const perPage = perPageRaw === 'all' ? Infinity : parseInt(perPageRaw, 10);

    let filtered = cmGroupsSnapshot;
    if (filter) {
      filtered = filtered.filter(g =>
        String(g.id || '').toLowerCase().includes(filter) ||
        String(g.name || '').toLowerCase().includes(filter)
      );
    }
    if (statusFilter === 'enabled') filtered = filtered.filter(g => g.comment_active);
    if (statusFilter === 'disabled') filtered = filtered.filter(g => !g.comment_active);

    const total = filtered.length;
    const totalPages = perPage === Infinity ? 1 : Math.max(1, Math.ceil(total / perPage));
    cmGroupPage = Math.min(cmGroupPage, totalPages);
    const start = perPage === Infinity ? 0 : (cmGroupPage - 1) * perPage;
    const page = perPage === Infinity ? filtered : filtered.slice(start, start + perPage);

    document.getElementById('cmGroupCount').textContent = `${total} groups`;
    document.getElementById('cmGroupPageInfo').textContent = `Page ${cmGroupPage} / ${totalPages}`;

    const tbody = document.getElementById('cmGroupsBody');
    if (!page.length) {
      tbody.innerHTML = '<tr><td colspan="5"><div class="empty"><span class="empty-ico">👥</span><span class="empty-txt">No groups match filter</span></div></td></tr>';
      return;
    }
    tbody.innerHTML = page.map(g => {
      const active = g.comment_active;
      const pill = active
        ? '<span class="pill p-green">💬 On</span>'
        : '<span class="pill p-gray">Off</span>';
      const toggleBtn = active
        ? `<button class="btn-red" style="padding:4px 10px;font-size:11px" onclick="toggleCommentGroup('exclude_comment_group','${esc(g.id)}')">Exclude</button>`
        : `<button class="btn-green" style="padding:4px 10px;font-size:11px" onclick="toggleCommentGroup('include_comment_group','${esc(g.id)}')">Include</button>`;
      return `<tr>
        <td class="mono" style="font-size:11px">${esc(g.id)}</td>
        <td>${esc(g.name)}</td>
        <td class="mono" style="font-size:11px;word-break:break-all">${esc(g.url)}</td>
        <td>${pill}</td>
        <td>${toggleBtn}</td>
      </tr>`;
    }).join('');
  }

  async function toggleCommentGroup(action, groupId) {
    if (!selectedAccount) { toast('Select an account first.', true); return; }
    await callAction(action, selectedAccount, groupId);
  }

  document.getElementById('cmGroupFilter').addEventListener('input', () => {
    cmResetGroupPaging();
    renderCommentGroups(groupsSnapshot);
  });
  document.getElementById('cmGroupStatusFilter').addEventListener('change', () => {
    cmResetGroupPaging();
    renderCommentGroups(groupsSnapshot);
  });
  document.getElementById('cmGroupPerPage').addEventListener('change', () => {
    cmResetGroupPaging();
    renderCommentGroups(groupsSnapshot);
  });
  document.getElementById('cmGroupPrevPage').addEventListener('click', () => {
    cmGroupPage = Math.max(1, cmGroupPage - 1);
    renderCommentGroups(groupsSnapshot);
  });
  document.getElementById('cmGroupNextPage').addEventListener('click', () => {
    cmGroupPage += 1;
    renderCommentGroups(groupsSnapshot);
  });

  document.getElementById('openCommentRulesBtn').addEventListener('click', openCommentRulesModal);
  document.getElementById('closeCommentRulesModal').addEventListener('click', closeCommentRulesModal);
  document.getElementById('commentRulesModal').addEventListener('click', ev => {
    if (ev.target && ev.target.id === 'commentRulesModal') closeCommentRulesModal();
  });

  document.getElementById('startCommenterBtn').addEventListener('click', async () => {
    if (!selectedAccount) { toast('Select an account from the sidebar first.', true); return; }
    await callAction('start_commenter', selectedAccount);
  });

  document.getElementById('stopCommenterBtn').addEventListener('click', async () => {
    await callAction('stop_commenter', selectedAccount || '');
  });

  document.getElementById('cmSchType').addEventListener('change', cmUpdateScheduleTypeVisibility);
  document.getElementById('cmStartScheduleBtn').addEventListener('click', async () => {
    if (!selectedAccount) { toast('Please select an account first.', true); return; }
    const type = (document.getElementById('cmSchType').value || '').trim();
    let timeVal = '';
    let specificVal = '';
    if (type === 'specific_datetime') {
      specificVal = (document.getElementById('cmSchDateFrom').value || '').trim();
      if (!specificVal) { toast('Please set a date and time.', true); return; }
      // time is derived from the datetime-local value
      timeVal = specificVal.includes('T') ? specificVal.split('T')[1].slice(0, 5) : '09:00';
    } else {
      timeVal = (document.getElementById('cmSchTimeFrom').value || '').trim();
      if (!timeVal) { toast('Please set a start time.', true); return; }
    }
    const timeTo = (document.getElementById('cmSchTimeTo').value || '').trim();
    const weekdays = Array.from(document.querySelectorAll('.cmSchDay'))
      .filter(el => el.checked)
      .map(el => parseInt(el.value || '-1', 10))
      .filter(v => !Number.isNaN(v) && v >= 0 && v <= 6);
    if (type === 'weekly' && weekdays.length === 0) {
      toast('Please select at least one day for a weekly schedule.', true); return;
    }
    await callAction('start_comment_schedule', selectedAccount, '', '', {
      type, time: timeVal, specific_datetime: specificVal, weekdays, time_to: timeTo, dry_run: false,
    });
  });
  document.getElementById('cmStopScheduleBtn').addEventListener('click', async () => {
    if (!selectedAccount) { toast('Please select an account first.', true); return; }
    await callAction('stop_comment_schedule', selectedAccount, '', '', { schedule_id: '' });
  });

  document.getElementById('saveCommentSettingsBtn').addEventListener('click', async () => {
    const minDelay = parseInt((document.getElementById('cmMinDelay').value || '').trim(), 10);
    const maxDelay = parseInt((document.getElementById('cmMaxDelay').value || '').trim(), 10);
    if (isNaN(minDelay) || isNaN(maxDelay)) { toast('Delay values must be integers.', true); return; }
    if (minDelay > maxDelay) { toast('Min delay cannot exceed max delay.', true); return; }
    const payload = { min_delay_minutes: minDelay, max_delay_minutes: maxDelay };
    if (selectedCommentTemplate) payload.template_file = selectedCommentTemplate;
    await callAction('update_commenting_rules', selectedAccount || '', '', '', payload);
    closeCommentRulesModal();
  });

  function initCmLogAutoScroll() {
    const key = 'fbpost.cmLogAutoScroll';
    const input = document.getElementById('logAutoScroll2');
    if (!input) return;
    const saved = localStorage.getItem(key);
    input.checked = saved === null ? true : saved === 'true';
    input.addEventListener('change', () => {
      localStorage.setItem(key, input.checked ? 'true' : 'false');
      if (input.checked) {
        const wrap = document.getElementById('cmLiveLogWrap');
        if (wrap) wrap.scrollTop = wrap.scrollHeight;
      }
    });
  }

  function syncCmLogs(rows) {
    const tbody = document.getElementById('cmLiveLogsBody');
    if (!tbody) return;
    if (!rows.length) {
      tbody.innerHTML = '<tr><td colspan="3"><div class="empty"><span class="empty-ico">🫙</span><span class="empty-txt">No logs yet</span></div></td></tr>';
      return;
    }
    tbody.innerHTML = rows.map(item => {
      const level = String(item.level || 'INFO').toUpperCase();
      return `<tr>
        <td class="mono">${esc(formatLogTime(item.timestamp))}</td>
        <td><span class="pill ${level === 'ERROR' ? 'p-orange' : 'p-gray'}">${esc(level)}</span></td>
        <td>${esc(item.message || '')}</td>
      </tr>`;
    }).join('');
    const input = document.getElementById('logAutoScroll2');
    if (input && input.checked) {
      const wrap = document.getElementById('cmLiveLogWrap');
      if (wrap) wrap.scrollTop = wrap.scrollHeight;
    }
  }

  updateScheduleTypeVisibility();
  lastScheduleSpecSignature = '';
  initLogAutoScrollSetting();
  initCmLogAutoScroll();
  loadState();
  loadLogs();
  setInterval(() => {
    loadState();
    loadLogs();
  }, 3000);
</script>
</body>
</html>
"""


def _execute_account_action(
    config_path: Path,
    action: str,
    account_id: str,
    group_id: str = "",
    template_file: str = "",
    template_data: dict[str, Any] | None = None,
    browser_rules: dict[str, Any] | None = None,
    groups_rules: dict[str, Any] | None = None,
    posting_rules: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    if action == "add_account":
        return add_account(config_path, account_id)
    if action == "delete_account":
        return delete_account(config_path, account_id)
    if action == "enable_account":
        return set_account_enabled(config_path, account_id, True)
    if action == "disable_account":
        return set_account_enabled(config_path, account_id, False)
    if action == "set_active":
        return set_active_account(config_path, account_id)
    if action == "exclude_group":
        return set_group_included(config_path, account_id, group_id, False)
    if action == "include_group":
        return set_group_included(config_path, account_id, group_id, True)
    if action == "set_template":
        template_names = {str(item.get("template_file", "")).strip() for item in load_templates(Path("templates"))}
        normalized_template = template_file.strip()
        if normalized_template not in template_names:
            return False, f"Template '{normalized_template}' not found."
        return set_account_template(config_path, account_id, normalized_template)
    if action == "save_template":
      normalized_template = template_file.strip()
      if not normalized_template:
        return False, "Template file is required."
      template_names = {str(item.get("template_file", "")).strip() for item in load_templates(Path("templates"))}
      if normalized_template not in template_names:
        return False, f"Template '{normalized_template}' not found."
      payload = _normalize_template_data(template_data or {})
      _save_template_file(Path("templates") / normalized_template, payload)
      return True, f"Template '{normalized_template}' updated."
    if action == "create_template":
      payload = _normalize_template_data(template_data or {})
      template_dir = Path("templates")
      new_file = _next_template_filename(template_dir)
      _save_template_file(template_dir / new_file, payload)
      return True, f"Created template '{new_file}'."
    if action == "delete_template":
      normalized_template = template_file.strip()
      if not normalized_template:
        return False, "Template file is required."
      template_path = Path("templates") / normalized_template
      if not template_path.exists():
        return False, f"Template '{normalized_template}' not found."
      template_path.unlink()
      return True, f"Deleted template '{normalized_template}'."
    if action == "update_browser_rules":
      return _update_browser_rules(config_path, browser_rules or {})
    if action == "update_groups_rules":
      return _update_groups_rules(config_path, groups_rules or {})
    if action == "update_posting_rules":
      return _update_posting_rules(config_path, posting_rules or {})
    if action == "update_commenting_rules":
      return _update_commenting_rules(config_path, template_data or {})
    if action == "include_comment_group":
      return set_group_comment_included(config_path, account_id, group_id, True)
    if action == "exclude_comment_group":
      return set_group_comment_included(config_path, account_id, group_id, False)

    # Preset actions
    if action == "list_presets":
      presets = list_presets()
      status = get_preset_status(config_path)
      return True, json.dumps({"presets": presets, "status": status})
    if action == "save_preset":
      preset_name = str(template_data.get("preset_name", "")).strip() if template_data else ""
      if not preset_name:
        return False, "Preset name is required."
      current_state = get_current_state(config_path, account_id)
      current_state["name"] = preset_name
      ok, normalized = save_preset(preset_name, current_state, update=False)
      if not ok:
        return False, f"Failed to save preset: {normalized}"
      if apply_preset_values(config_path, normalized):
        return True, f"Preset '{preset_name}' saved and applied."
      return True, f"Preset '{preset_name}' saved."
    if action == "update_preset":
      preset_filename = str(template_data.get("preset_filename", "")).strip() if template_data else ""
      if not preset_filename:
        return False, "Preset filename is required."
      existing = load_preset(preset_filename)
      if not existing:
        return False, f"Preset '{preset_filename}' not found."
      current_state = get_current_state(config_path, account_id)
      current_state["name"] = existing.get("name", preset_filename.replace(".yaml", ""))
      ok, normalized = save_preset(preset_filename, current_state, update=True)
      if not ok:
        return False, f"Failed to update preset: {normalized}"
      if apply_preset_values(config_path, normalized):
        return True, f"Preset '{normalized}' updated and applied."
      return True, f"Preset '{normalized}' updated."
    if action == "delete_preset":
      preset_filename = str(template_data.get("preset_filename", "")).strip() if template_data else ""
      if not preset_filename:
        return False, "Preset filename is required."
      if delete_preset(preset_filename):
        # If this was the active preset, disable preset mode
        status = get_preset_status(config_path)
        if status.get("name") == preset_filename:
          disable_preset(config_path)
        return True, f"Preset '{preset_filename}' deleted."
      return False, f"Failed to delete preset '{preset_filename}'."
    if action == "apply_preset":
      preset_filename = str(template_data.get("preset_filename", "")).strip() if template_data else ""
      if not preset_filename:
        return False, "Preset filename is required."
      if apply_preset_values(config_path, preset_filename):
        return True, f"Preset '{preset_filename}' applied."
      return False, f"Failed to apply preset '{preset_filename}'."
    if action == "disable_preset":
      if disable_preset(config_path):
        return True, "Preset disabled. Using config.yaml values."
      return False, "Failed to disable preset."

    with _account_env(account_id):
        if action == "clear_posted_log":
            from core.config_loader import load_config_lenient as _load_cfg_lenient
            cfg = _load_cfg_lenient(config_path)
            paths_cfg = cfg.get("paths", {})
            posted_log_path = Path(str(paths_cfg.get("posted_log", "data/posted_log.json")))
            if posted_log_path.exists():
                posted_log_path.unlink()
            return True, "Posted history cleared."
        if action == "test_session":
            ok = validate_session(config_path)
            return (ok, "Session valid ✓" if ok else "Session invalid ✗")
        if action == "setup_session":
            ensure_session(config_path, force_relogin=False)
            return True, "Setup session flow finished."
        if action == "scrape_groups":
            scrape_groups(config_path, force=True)
            return True, "Group scrape completed."
        if action == "run_once_dry":
            run_scheduler(config_path, run_once=True, force_dry_run=True)
            return True, "Dry run completed."
        if action == "run_once_live":
            run_scheduler(config_path, run_once=True, force_dry_run=False)
            return True, "Live run completed."

    return False, f"Unknown action '{action}'."


def run_web_ui(*, config_path: Path, host: str, port: int) -> None:
    state = _WebState(config_path)

    class Handler(BaseHTTPRequestHandler):
        def _send_html(self, body: str) -> None:
            encoded = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
          parsed = urlparse(self.path)
          qs = parse_qs(parsed.query)
          if parsed.path == "/api/state":
            selected_account = (qs.get("account") or [""])[0].strip() or None
            with state.lock:
              runner_state = state.snapshot_runner(selected_account)
              schedule_state = state.snapshot_schedule(selected_account)
              commenter_state = state.snapshot_commenter(selected_account)
              comment_schedule_state = state.snapshot_comment_schedule(selected_account)
            payload = _build_state(state.config_path, selected_account, runner_state, schedule_state, commenter_state, comment_schedule_state)
            self._send_json({"ok": True, "data": payload, **payload})
            return
          if parsed.path == "/api/logs":
            limit_raw = (qs.get("limit") or ["200"])[0].strip()
            try:
              limit = int(limit_raw)
            except Exception:
              limit = 200
            all_logs = _read_live_logs(limit)
            poster_logs = [r for r in all_logs if r.get("source") != "commenter"]
            commenter_logs = [r for r in all_logs if r.get("source") == "commenter"]
            self._send_json({
              "ok": True,
              "logs": poster_logs,
              "commenter_logs": commenter_logs,
            })
            return
          if parsed.path == "/":
            self._send_html(_render_page())
            return
          self.send_error(404)

        def do_POST(self) -> None:  # noqa: N802
            if self.path == "/api/upload-image":
                self._handle_image_upload()
                return
            if self.path != "/api/action":
                self.send_error(404)
                return
            content_len = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(content_len)
            try:
                payload    = json.loads(raw.decode("utf-8", errors="ignore") or "{}")
                action     = str(payload.get("action", "")).strip()
                account_id = str(payload.get("account_id", "")).strip()
                group_id   = str(payload.get("group_id", "")).strip()
                template_file = str(payload.get("template_file", "")).strip()
                template_data = payload.get("template_data")
                if not isinstance(template_data, dict):
                  template_data = {}
                browser_rules = payload.get("browser_rules")
                if not isinstance(browser_rules, dict):
                  browser_rules = {}
                groups_rules = payload.get("groups_rules")
                if not isinstance(groups_rules, dict):
                  groups_rules = {}
                posting_rules = payload.get("posting_rules")
                if not isinstance(posting_rules, dict):
                  posting_rules = {}

                global_actions = {
                  "update_browser_rules",
                  "update_groups_rules",
                  "update_posting_rules",
                  "update_commenting_rules",
                  "list_presets",
                  "save_preset",
                  "update_preset",
                  "delete_preset",
                  "apply_preset",
                  "disable_preset",
                  "start_schedule",
                  "stop_schedule",
                  "delete_schedule",
                  "start_commenter",
                  "stop_commenter",
                  "start_comment_schedule",
                  "stop_comment_schedule",
                  "delete_comment_schedule",
                  "enable_schedule",
                  "enable_comment_schedule",
                }
                if not account_id and action not in global_actions:
                    self._send_json({"ok": False, "error": "Account id is required."}, status=400)
                    return

                if not account_id:
                  account_id = get_active_account(state.config_path) or ""

                with state.lock:
                  if action == "run_once_live":
                    ok, result = state.start_run(account_id, live=True)
                  elif action == "run_once_dry":
                    ok, result = state.start_run(account_id, live=False)
                  elif action == "start_schedule":
                    ok, result = state.start_schedule(account_id, template_data if isinstance(template_data, dict) else {})
                  elif action == "stop_schedule":
                    schedule_id = str(template_data.get("schedule_id", "")).strip() if isinstance(template_data, dict) else ""
                    ok, result = state.stop_schedule(account_id, schedule_id)
                  elif action == "delete_schedule":
                    schedule_id = str(template_data.get("schedule_id", "")).strip() if isinstance(template_data, dict) else ""
                    ok, result = state.delete_schedule(account_id, schedule_id)
                  elif action == "pause_run":
                    ok, result = state.pause_run(account_id)
                  elif action == "resume_run":
                    ok, result = state.resume_run(account_id)
                  elif action == "stop_run":
                    ok, result = state.stop_run(account_id)
                  elif action == "start_commenter":
                    ok, result = state.start_commenter(account_id)
                  elif action == "stop_commenter":
                    ok, result = state.stop_commenter(account_id)
                  elif action == "start_comment_schedule":
                    ok, result = state.start_comment_schedule(account_id, template_data if isinstance(template_data, dict) else {})
                  elif action == "stop_comment_schedule":
                    schedule_id = str(template_data.get("schedule_id", "")).strip() if isinstance(template_data, dict) else ""
                    ok, result = state.stop_comment_schedule(account_id, schedule_id)
                  elif action == "delete_comment_schedule":
                    schedule_id = str(template_data.get("schedule_id", "")).strip() if isinstance(template_data, dict) else ""
                    ok, result = state.delete_comment_schedule(account_id, schedule_id)
                  elif action == "enable_schedule":
                    schedule_id = str(template_data.get("schedule_id", "")).strip() if isinstance(template_data, dict) else ""
                    ok, result = state.enable_schedule(account_id, schedule_id)
                  elif action == "enable_comment_schedule":
                    schedule_id = str(template_data.get("schedule_id", "")).strip() if isinstance(template_data, dict) else ""
                    ok, result = state.enable_comment_schedule(account_id, schedule_id)
                  else:
                    ok, result = _execute_account_action(
                      state.config_path,
                      action,
                      account_id,
                      group_id,
                      template_file,
                      template_data,
                      browser_rules,
                      groups_rules,
                      posting_rules,
                    )

                  runner_state = state.snapshot_runner(account_id or None)
                  schedule_state = state.snapshot_schedule(account_id or None)
                  commenter_state = state.snapshot_commenter(account_id or None)
                  comment_schedule_state = state.snapshot_comment_schedule(account_id or None)

                state_payload = _build_state(state.config_path, account_id or None, runner_state, schedule_state, commenter_state, comment_schedule_state)
                if ok:
                    self._send_json({"ok": True, "message": result, "state": state_payload})
                else:
                    self._send_json({"ok": False, "error": result, "state": state_payload}, status=400)
            except Exception as exc:
                self._send_json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=500)

        def _handle_image_upload(self) -> None:
            import re as _re
            content_type = self.headers.get("Content-Type", "")
            if "multipart/form-data" not in content_type:
                self._send_json({"ok": False, "error": "Invalid content type"}, status=400)
                return
            try:
                content_len = int(self.headers.get("Content-Length", "0") or "0")
                body = self.rfile.read(content_len)
                boundary = ""
                for part in content_type.split(";"):
                    part = part.strip()
                    if part.startswith("boundary="):
                        boundary = part[9:].strip('"')
                        break
                if not boundary:
                    self._send_json({"ok": False, "error": "Missing boundary"}, status=400)
                    return
                delimiter = f"--{boundary}".encode()
                parts = body.split(delimiter)
                file_data: bytes | None = None
                filename = ""
                for part in parts:
                    if b"Content-Disposition:" not in part and b"content-disposition:" not in part:
                        continue
                    header_end = part.find(b"\r\n\r\n")
                    if header_end < 0:
                        continue
                    header_block = part[:header_end].decode("utf-8", errors="ignore")
                    if 'name="image"' not in header_block:
                        continue
                    fn_match = _re.search(r'filename="([^"]+)"', header_block)
                    if fn_match:
                        filename = fn_match.group(1)
                    file_data = part[header_end + 4:]
                    if file_data.endswith(b"\r\n"):
                        file_data = file_data[:-2]
                    break
                if file_data is None or not filename:
                    self._send_json({"ok": False, "error": "No file selected"}, status=400)
                    return
                safe_name = _re.sub(r'[^\w.\-]', '_', os.path.basename(filename))
                if not safe_name:
                    self._send_json({"ok": False, "error": "Invalid filename"}, status=400)
                    return
                images_dir = Path("templates") / "images"
                images_dir.mkdir(parents=True, exist_ok=True)
                dest = images_dir / safe_name
                counter = 1
                while dest.exists():
                    stem = Path(safe_name).stem
                    suffix = Path(safe_name).suffix
                    dest = images_dir / f"{stem}_{counter}{suffix}"
                    counter += 1
                dest.write_bytes(file_data)
                self._send_json({"ok": True, "path": str(dest).replace(os.sep, "/")})
            except Exception as exc:
                self._send_json({"ok": False, "error": f"Upload failed: {exc}"}, status=500)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
            return

    server = ThreadingHTTPServer((host, int(port)), Handler)
    actual_port = server.server_port
    print(f"UI running at http://{host}:{actual_port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    finally:
        server.server_close()
