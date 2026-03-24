from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from typing import Any


def get_log_file(base_dir: Path = Path("logs")) -> Path:
    base_dir.mkdir(parents=True, exist_ok=True)
    return base_dir / f"session_{datetime.now():%Y%m%d}.log"


def log_event(message: str, *, level: str = "INFO", context: dict[str, Any] | None = None) -> None:
    log_path = get_log_file()
    timestamp = datetime.now().isoformat(timespec="seconds")
    entry: dict[str, Any] = {
        "timestamp": timestamp,
        "level": level.upper(),
        "message": message.strip(),
    }
    if context:
        entry["context"] = context

    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def log_exception(message: str, exc: Exception, *, context: dict[str, Any] | None = None) -> None:
    payload = {} if context is None else dict(context)
    payload["error_type"] = type(exc).__name__
    payload["error"] = str(exc)
    log_event(message, level="ERROR", context=payload)
