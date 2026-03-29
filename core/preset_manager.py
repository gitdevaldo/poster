from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


PRESETS_DIR = Path("templates/presets")


def _ensure_presets_dir() -> None:
    PRESETS_DIR.mkdir(parents=True, exist_ok=True)


def _normalize_filename(name: str) -> str:
    base = str(name or "").strip()
    base = re.sub(r"\.ya?ml$", "", base, flags=re.IGNORECASE)
    base = re.sub(r"[^a-zA-Z0-9._-]+", "-", base).strip("-_.").lower()
    if not base:
        base = "preset"
    return f"{base}.yaml"


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _load_raw_config(config_path: Path) -> dict[str, Any]:
    with config_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data if isinstance(data, dict) else {}


def _save_raw_config(config_path: Path, config: dict[str, Any]) -> None:
    with config_path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(config, f, sort_keys=False, allow_unicode=True)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _account_groups_path(raw_config: dict[str, Any], account_id: str) -> Path | None:
    accounts = raw_config.get("accounts", {})
    if not isinstance(accounts, dict):
        return None
    override = accounts.get(account_id)
    if not isinstance(override, dict):
        return None
    groups_cfg = override.get("groups", {})
    if not isinstance(groups_cfg, dict):
        return None
    source = str(groups_cfg.get("source", "")).strip()
    if not source:
        return None
    return Path(source)


def list_presets() -> list[dict[str, Any]]:
    _ensure_presets_dir()
    items: list[dict[str, Any]] = []
    for path in sorted(PRESETS_DIR.glob("*.yaml")):
        data = _read_yaml(path)
        items.append(
            {
                "filename": path.name,
                "name": str(data.get("name", path.stem)).strip() or path.stem,
                "account": str(data.get("account", "")).strip(),
                "created_at": data.get("created_at"),
                "updated_at": data.get("updated_at"),
            }
        )
    return items


def load_preset(filename: str) -> dict[str, Any] | None:
    _ensure_presets_dir()
    normalized = _normalize_filename(filename)
    path = PRESETS_DIR / normalized
    if not path.exists() or not path.is_file():
        return None
    data = _read_yaml(path)
    if not data:
        return None
    data["_filename"] = normalized
    return data


def save_preset(filename: str, data: dict[str, Any], *, update: bool = False) -> tuple[bool, str]:
    _ensure_presets_dir()
    normalized = _normalize_filename(filename)
    path = PRESETS_DIR / normalized
    now_iso = datetime.now(timezone.utc).isoformat()

    payload = dict(data)
    existing = _read_yaml(path) if path.exists() else {}
    payload["created_at"] = existing.get("created_at", now_iso) if update else now_iso
    payload["updated_at"] = now_iso
    if "name" not in payload or not str(payload.get("name", "")).strip():
        payload["name"] = normalized.replace(".yaml", "")

    try:
        with path.open("w", encoding="utf-8") as f:
            yaml.safe_dump(payload, f, sort_keys=False, allow_unicode=True)
        return True, normalized
    except Exception as exc:
        return False, str(exc)


def delete_preset(filename: str) -> bool:
    _ensure_presets_dir()
    path = PRESETS_DIR / _normalize_filename(filename)
    if not path.exists() or not path.is_file():
        return False
    try:
        path.unlink()
        return True
    except Exception:
        return False


def get_preset_status(config_path: Path) -> dict[str, Any]:
    raw = _load_raw_config(config_path)
    section = raw.get("presets", raw.get("preset", {}))
    if not isinstance(section, dict):
        return {"enabled": False, "name": "", "details": None}

    enabled = bool(section.get("enabled", False))
    name = str(section.get("name", "")).strip()
    details: dict[str, Any] | None = None
    if enabled and name:
        loaded = load_preset(name)
        if isinstance(loaded, dict):
            details = {
                "name": str(loaded.get("name", "")).strip(),
                "account": str(loaded.get("account", "")).strip(),
                "created_at": loaded.get("created_at"),
                "updated_at": loaded.get("updated_at"),
            }
    return {"enabled": enabled, "name": name, "details": details}


def get_current_state(config_path: Path, account_id: str | None = None) -> dict[str, Any]:
    raw = _load_raw_config(config_path)
    active_account = str(account_id or raw.get("active_account", "")).strip()

    account_cfg: dict[str, Any] = {}
    accounts = raw.get("accounts", {})
    if isinstance(accounts, dict):
        candidate = accounts.get(active_account, {})
        if isinstance(candidate, dict):
            account_cfg = candidate

    merged_browser = _deep_merge(
        dict(raw.get("browser", {})) if isinstance(raw.get("browser"), dict) else {},
        dict(account_cfg.get("browser", {})) if isinstance(account_cfg.get("browser"), dict) else {},
    )
    merged_groups = _deep_merge(
        dict(raw.get("groups", {})) if isinstance(raw.get("groups"), dict) else {},
        dict(account_cfg.get("groups", {})) if isinstance(account_cfg.get("groups"), dict) else {},
    )
    merged_posting = _deep_merge(
        dict(raw.get("posting", {})) if isinstance(raw.get("posting"), dict) else {},
        dict(account_cfg.get("posting", {})) if isinstance(account_cfg.get("posting"), dict) else {},
    )

    # If preset mode is enabled, include currently active preset overlay so "current state"
    # reflects what the UI/runtime is using.
    preset_section = raw.get("presets", raw.get("preset", {}))
    if isinstance(preset_section, dict) and bool(preset_section.get("enabled", False)):
        preset_name = str(preset_section.get("name", "")).strip()
        if preset_name:
            active_preset = load_preset(preset_name)
            if isinstance(active_preset, dict):
                if isinstance(active_preset.get("browser"), dict):
                    merged_browser = _deep_merge(merged_browser, active_preset["browser"])
                if isinstance(active_preset.get("groups"), dict):
                    merged_groups = _deep_merge(merged_groups, active_preset["groups"])
                if isinstance(active_preset.get("posting"), dict):
                    merged_posting = _deep_merge(merged_posting, active_preset["posting"])

    groups_state: list[dict[str, Any]] = []
    if active_account:
        groups_path = _account_groups_path(raw, active_account)
        if groups_path and groups_path.exists():
            try:
                parsed = json.loads(groups_path.read_text(encoding="utf-8"))
                if isinstance(parsed, list):
                    groups_state = [item for item in parsed if isinstance(item, dict)]
            except Exception:
                groups_state = []

    return {
        "name": "",
        "account": active_account,
        "browser": merged_browser,
        "groups": merged_groups,
        "posting": merged_posting,
        "groups_state": groups_state,
    }


def apply_preset_values(config_path: Path, preset_filename: str) -> bool:
    preset = load_preset(preset_filename)
    if not preset:
        return False

    raw = _load_raw_config(config_path)
    account_id = str(preset.get("account", "")).strip() or str(raw.get("active_account", "")).strip()
    if not account_id:
        return False
    accounts = raw.get("accounts", {})
    if not isinstance(accounts, dict):
        return False
    account_override = accounts.get(account_id)
    if not isinstance(account_override, dict):
        try:
            from core.account_manager import add_account

            ok, _ = add_account(config_path, account_id)
            if not ok:
                return False
            raw = _load_raw_config(config_path)
        except Exception:
            return False

    # Apply group include/exclude snapshot to account groups file.
    groups_state = preset.get("groups_state", [])
    if isinstance(groups_state, list):
        groups_path = _account_groups_path(raw, account_id)
        if groups_path:
            groups_path.parent.mkdir(parents=True, exist_ok=True)
            groups_path.write_text(json.dumps(groups_state, ensure_ascii=False, indent=2), encoding="utf-8")

    raw["presets"] = {
        "enabled": True,
        "name": str(preset.get("_filename", _normalize_filename(preset_filename))).strip(),
    }
    # Keep backward compatibility key if already present
    if "preset" in raw and isinstance(raw.get("preset"), dict):
        raw["preset"] = {"enabled": True, "name": raw["presets"]["name"]}
    raw["active_account"] = account_id
    _save_raw_config(config_path, raw)
    return True


def disable_preset(config_path: Path) -> bool:
    try:
        raw = _load_raw_config(config_path)
        raw["presets"] = {"enabled": False, "name": ""}
        if "preset" in raw and isinstance(raw.get("preset"), dict):
            raw["preset"] = {"enabled": False, "name": ""}
        _save_raw_config(config_path, raw)
        return True
    except Exception:
        return False
