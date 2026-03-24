from __future__ import annotations

import json
import shutil
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml


def load_raw_config(config_path: Path) -> dict[str, Any]:
    with config_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        return {}
    return data


def save_raw_config(config_path: Path, config: dict[str, Any]) -> None:
    with config_path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(config, f, sort_keys=False, allow_unicode=True)


def list_accounts(config_path: Path) -> dict[str, dict[str, Any]]:
    config = load_raw_config(config_path)
    accounts = config.get("accounts", {})
    if not isinstance(accounts, dict):
        return {}
    return {k: v for k, v in accounts.items() if isinstance(v, dict)}


def get_active_account(config_path: Path) -> str | None:
    config = load_raw_config(config_path)
    value = str(config.get("active_account", "")).strip()
    return value or None


def _build_account_override(account_id: str, config: dict[str, Any]) -> dict[str, Any]:
    account_root = f"data/accounts/{account_id}"
    default_template = str(config.get("posting", {}).get("template_file", "post_1.yaml"))

    return {
        "enabled": True,
        "session": {
            "path": f"{account_root}/session.json",
            "persistent_profile_dir": f"{account_root}/camoufox_profile",
        },
        "groups": {
            "source": f"{account_root}/groups.json",
            "blacklist": f"{account_root}/blacklist.txt",
        },
        "paths": {
            "queue_state": f"{account_root}/queue_state.json",
            "posted_log": f"{account_root}/logs/posted_log.json",
            "screenshots": f"{account_root}/logs/screenshots",
        },
        "posting": {
            "template_file": default_template,
        },
    }


def ensure_account_scaffold(account_override: dict[str, Any]) -> None:
    session_cfg = account_override.get("session", {}) if isinstance(account_override, dict) else {}
    groups_cfg = account_override.get("groups", {}) if isinstance(account_override, dict) else {}
    paths_cfg = account_override.get("paths", {}) if isinstance(account_override, dict) else {}

    session_path = Path(str(session_cfg.get("path", "")))
    profile_dir = Path(str(session_cfg.get("persistent_profile_dir", "")))
    groups_path = Path(str(groups_cfg.get("source", "")))
    blacklist_path = Path(str(groups_cfg.get("blacklist", "")))
    queue_state_path = Path(str(paths_cfg.get("queue_state", "")))
    posted_log_path = Path(str(paths_cfg.get("posted_log", "")))
    screenshots_dir = Path(str(paths_cfg.get("screenshots", "")))

    for directory in [
        profile_dir,
        screenshots_dir,
        session_path.parent,
        groups_path.parent,
        blacklist_path.parent,
        queue_state_path.parent,
        posted_log_path.parent,
    ]:
        if str(directory):
            directory.mkdir(parents=True, exist_ok=True)

    if str(groups_path) and not groups_path.exists():
        groups_path.write_text("[]\n", encoding="utf-8")

    if str(blacklist_path) and not blacklist_path.exists():
        blacklist_path.write_text("", encoding="utf-8")

    if str(queue_state_path) and not queue_state_path.exists():
        queue_state_path.write_text('{"sequential_index": 0, "round_robin_index": 0}\n', encoding="utf-8")

    if str(posted_log_path) and not posted_log_path.exists():
        posted_log_path.write_text("[]\n", encoding="utf-8")


def add_account(config_path: Path, account_id: str) -> tuple[bool, str]:
    normalized_id = account_id.strip()
    if not normalized_id:
        return False, "Account id cannot be empty."

    config = load_raw_config(config_path)
    accounts = config.get("accounts")
    if not isinstance(accounts, dict):
        accounts = {}
        config["accounts"] = accounts

    if normalized_id in accounts:
        existing = accounts.get(normalized_id)
        if isinstance(existing, dict):
            ensure_account_scaffold(existing)
        return True, f"Account '{normalized_id}' already exists (scaffold ensured)."

    account_override = _build_account_override(normalized_id, config)
    accounts[normalized_id] = account_override
    save_raw_config(config_path, config)
    ensure_account_scaffold(account_override)
    return True, f"Added account '{normalized_id}'."


def set_account_enabled(config_path: Path, account_id: str, enabled: bool) -> tuple[bool, str]:
    config = load_raw_config(config_path)
    accounts = config.get("accounts", {})
    if not isinstance(accounts, dict) or account_id not in accounts or not isinstance(accounts[account_id], dict):
        return False, f"Account '{account_id}' not found."

    account = deepcopy(accounts[account_id])
    account["enabled"] = bool(enabled)
    accounts[account_id] = account
    config["accounts"] = accounts
    save_raw_config(config_path, config)
    return True, f"Account '{account_id}' {'enabled' if enabled else 'disabled'}."


def set_active_account(config_path: Path, account_id: str) -> tuple[bool, str]:
    config = load_raw_config(config_path)
    accounts = config.get("accounts", {})
    if not isinstance(accounts, dict) or account_id not in accounts:
        return False, f"Account '{account_id}' not found."

    config["active_account"] = account_id
    save_raw_config(config_path, config)
    return True, f"Active account set to '{account_id}'."


def _account_root_dir(account_override: dict[str, Any]) -> Path | None:
    session_cfg = account_override.get("session", {}) if isinstance(account_override, dict) else {}
    session_path = str(session_cfg.get("path", "")).strip()
    if not session_path:
        return None

    # Expected format: data/accounts/<id>/session.json
    p = Path(session_path)
    if len(p.parts) >= 4:
        return Path(*p.parts[:3])
    return p.parent


def delete_account(config_path: Path, account_id: str) -> tuple[bool, str]:
    config = load_raw_config(config_path)
    accounts = config.get("accounts", {})
    if not isinstance(accounts, dict) or account_id not in accounts:
        return False, f"Account '{account_id}' not found."

    if str(config.get("active_account", "")).strip() == account_id:
        return False, f"Cannot delete active account '{account_id}'. Set another active account first."

    override = accounts.get(account_id)
    root_dir = _account_root_dir(override) if isinstance(override, dict) else None

    del accounts[account_id]
    config["accounts"] = accounts
    save_raw_config(config_path, config)

    if root_dir is not None and root_dir.exists():
        shutil.rmtree(root_dir, ignore_errors=True)

    return True, f"Deleted account '{account_id}'."


def _groups_path_for_account(config_path: Path, account_id: str) -> Path | None:
    accounts = list_accounts(config_path)
    override = accounts.get(account_id)
    if not isinstance(override, dict):
        return None
    groups_cfg = override.get("groups", {})
    if not isinstance(groups_cfg, dict):
        return None
    source = str(groups_cfg.get("source", "")).strip()
    return Path(source) if source else None


def list_groups_for_account(config_path: Path, account_id: str) -> list[dict[str, Any]]:
    groups_path = _groups_path_for_account(config_path, account_id)
    if groups_path is None or not groups_path.exists():
        return []

    try:
        raw = json.loads(groups_path.read_text(encoding="utf-8"))
    except Exception:
        return []

    if not isinstance(raw, list):
        return []
    return [g for g in raw if isinstance(g, dict)]


def set_group_included(config_path: Path, account_id: str, group_id: str, included: bool) -> tuple[bool, str]:
    groups_path = _groups_path_for_account(config_path, account_id)
    if groups_path is None:
        return False, f"Account '{account_id}' not found."

    if not groups_path.exists():
        return False, f"Groups file not found for account '{account_id}'."

    try:
        raw = json.loads(groups_path.read_text(encoding="utf-8"))
    except Exception:
        return False, f"Invalid groups data for account '{account_id}'."

    if not isinstance(raw, list):
        return False, f"Invalid groups data for account '{account_id}'."

    target_id = str(group_id).strip()
    updated = False
    for item in raw:
        if not isinstance(item, dict):
            continue
        if str(item.get("id", "")).strip() == target_id:
            item["active"] = bool(included)
            updated = True
            break

    if not updated:
        return False, f"Group '{target_id}' not found."

    groups_path.parent.mkdir(parents=True, exist_ok=True)
    groups_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    return True, f"Group '{target_id}' {'included' if included else 'excluded'}."
