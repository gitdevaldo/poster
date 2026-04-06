from __future__ import annotations

from contextvars import ContextVar
from datetime import datetime
import json
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_WIB = ZoneInfo("Asia/Jakarta")


_log_source: ContextVar[str] = ContextVar("_log_source", default="")


def set_log_source(source: str) -> None:
    _log_source.set(source)


def get_log_file(base_dir: Path = Path("logs")) -> Path:
    base_dir.mkdir(parents=True, exist_ok=True)
    return base_dir / f"session_{datetime.now(_WIB):%Y%m%d}.log"


def log_event(message: str, *, level: str = "INFO", context: dict[str, Any] | None = None) -> None:
    log_path = get_log_file()
    timestamp = datetime.now(_WIB).isoformat(timespec="seconds")
    entry: dict[str, Any] = {
        "timestamp": timestamp,
        "level": level.upper(),
        "message": message.strip(),
    }
    src = _log_source.get("")
    if src:
        entry["source"] = src
    if context:
        entry["context"] = context

    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def log_exception(message: str, exc: Exception, *, context: dict[str, Any] | None = None) -> None:
    payload = {} if context is None else dict(context)
    payload["error_type"] = type(exc).__name__
    payload["error"] = str(exc)
    log_event(message, level="ERROR", context=payload)
