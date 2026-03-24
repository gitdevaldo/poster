from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

import yaml


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        return {}
    return data


def load_templates(template_dir: Path = Path("templates")) -> list[dict[str, Any]]:
    templates: list[dict[str, Any]] = []
    for file_path in sorted(template_dir.glob("post_*.yaml")):
        data = _load_yaml(file_path)
        if isinstance(data, dict):
            data["template_file"] = file_path.name
            templates.append(data)
    return templates


def select_template(templates: list[dict[str, Any]], index: int = 0) -> dict[str, Any] | None:
    if not templates:
        return None
    return templates[index % len(templates)]


def load_queue_state(state_path: Path) -> dict[str, int]:
    if not state_path.exists():
        return {"sequential_index": 0, "round_robin_index": 0}

    try:
        with state_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {"sequential_index": 0, "round_robin_index": 0}

    if not isinstance(data, dict):
        return {"sequential_index": 0, "round_robin_index": 0}

    sequential_index = int(data.get("sequential_index", 0))
    round_robin_index = int(data.get("round_robin_index", 0))
    return {
        "sequential_index": max(0, sequential_index),
        "round_robin_index": max(0, round_robin_index),
    }


def save_queue_state(state_path: Path, state: dict[str, int]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with state_path.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def plan_templates_for_session(
    mode: str,
    groups_count: int,
    templates: list[dict[str, Any]],
    state: dict[str, int],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    if not templates or groups_count <= 0:
        return ([], state)

    mode_key = mode.strip().lower()
    template_count = len(templates)
    next_state = dict(state)

    if mode_key == "single":
        selected = [templates[0] for _ in range(groups_count)]
        return selected, next_state

    if mode_key == "sequential":
        index = state.get("sequential_index", 0) % template_count
        selected = [templates[index] for _ in range(groups_count)]
        next_state["sequential_index"] = (index + 1) % template_count
        return selected, next_state

    if mode_key == "round-robin":
        start_index = state.get("round_robin_index", 0) % template_count
        selected: list[dict[str, Any]] = []
        for offset in range(groups_count):
            selected.append(templates[(start_index + offset) % template_count])
        next_state["round_robin_index"] = (start_index + groups_count) % template_count
        return selected, next_state

    # Fallback mode is random.
    selected_random = [random.choice(templates) for _ in range(groups_count)]
    return selected_random, next_state
