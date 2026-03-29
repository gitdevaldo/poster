from __future__ import annotations

import os
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

from core.preset_manager import load_preset


ACCOUNT_ENV_VAR = "FBPOST_ACCOUNT"


def _load_raw_config(config_path: Path) -> dict[str, Any]:
    with config_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        return {}
    return data


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def get_active_account_id(explicit_account_id: str | None = None) -> str | None:
    if explicit_account_id and explicit_account_id.strip():
        return explicit_account_id.strip()

    env_account = os.environ.get(ACCOUNT_ENV_VAR, "").strip()
    return env_account or None


def load_config(config_path: Path, account_id: str | None = None) -> dict[str, Any]:
    raw = _load_raw_config(config_path)
    preset_sections: dict[str, dict[str, Any]] = {}
    preset_cfg = raw.get("presets", raw.get("preset", {}))
    if isinstance(preset_cfg, dict) and bool(preset_cfg.get("enabled", False)):
        preset_name = str(preset_cfg.get("name", "")).strip()
        if preset_name:
            preset = load_preset(preset_name)
            if isinstance(preset, dict):
                # Keep only relevant sections for merge.
                for key in ("browser", "groups", "posting"):
                    value = preset.get(key)
                    if isinstance(value, dict):
                        preset_sections[key] = value
                preset_account = str(preset.get("account", "")).strip()
                if preset_account:
                    raw["active_account"] = preset_account

    default_account = str(raw.get("active_account", "")).strip() or None
    active_account_id = get_active_account_id(account_id) or default_account

    base_config: dict[str, Any] = {k: deepcopy(v) for k, v in raw.items() if k != "accounts"}

    if not active_account_id:
        for key, value in preset_sections.items():
            base_part = base_config.get(key, {})
            if not isinstance(base_part, dict):
                base_part = {}
            base_config[key] = _deep_merge(base_part, value)
        return base_config

    accounts = raw.get("accounts", {})
    if not isinstance(accounts, dict):
        raise ValueError("Invalid accounts section in config.yaml")

    account_override = accounts.get(active_account_id)
    if not isinstance(account_override, dict):
        raise ValueError(f"Account '{active_account_id}' not found in config.yaml accounts section")

    if account_override.get("enabled", True) is False:
        raise PermissionError(f"Account '{active_account_id}' is disabled")

    merged = _deep_merge(base_config, account_override)
    for key, value in preset_sections.items():
        base_part = merged.get(key, {})
        if not isinstance(base_part, dict):
            base_part = {}
        merged[key] = _deep_merge(base_part, value)
    return merged
