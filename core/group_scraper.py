from __future__ import annotations

import json
from datetime import datetime, timezone
import re
from pathlib import Path
from typing import Any

from core.config_loader import load_config
from core.logger import log_event, log_exception
from core.session_manager import (
    clear_profile_locks,
    configure_page_window,
    ensure_logged_in_in_page,
    ensure_session,
    get_or_create_page,
    save_session_from_page,
    scrape_profile_context,
)


GROUP_LINK_RE = re.compile(r"facebook\.com/groups/([^/?#]+)", re.IGNORECASE)
LAST_ACTIVE_SUFFIX_RE = re.compile(r"\s*Last active\s+.*$", re.IGNORECASE)


def _load_config(config_path: Path) -> dict:
    return load_config(config_path)


def _load_existing_groups(groups_path: Path) -> list[dict[str, Any]]:
    if not groups_path.exists():
        return []
    try:
        with groups_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
    except Exception:
        return []
    return []


def _should_rescrape(groups_path: Path, rescrape_every_days: int) -> bool:
    if not groups_path.exists():
        return True
    if rescrape_every_days <= 0:
        return True

    modified = datetime.fromtimestamp(groups_path.stat().st_mtime, tz=timezone.utc)
    age = datetime.now(timezone.utc) - modified
    return age.days >= rescrape_every_days


def _normalize_group_name(name: str, group_id: str) -> str:
    cleaned = LAST_ACTIVE_SUFFIX_RE.sub("", str(name or "")).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        return f"Group {group_id}"
    return cleaned


def _is_placeholder_group_name(name: str, group_id: str) -> bool:
    return str(name).strip().lower() == f"group {group_id}".lower()


def _canonical_group_id(group_id: str, url: str) -> str:
    parsed_id = str(group_id or "").strip()
    match = GROUP_LINK_RE.search(str(url or ""))
    if match:
        url_id = match.group(1).strip()
        if url_id:
            return url_id
    return parsed_id


def _upsert_group(groups: dict[str, dict[str, str]], group_id: str, name: str, url: str) -> None:
    canonical_id = _canonical_group_id(group_id, url)
    if not canonical_id:
        return

    normalized_name = _normalize_group_name(name, canonical_id)
    normalized_url = str(url).strip() or f"https://www.facebook.com/groups/{group_id}"

    existing = groups.get(canonical_id)
    if existing is None:
        groups[canonical_id] = {
            "id": canonical_id,
            "name": normalized_name,
            "url": normalized_url,
        }
        return

    existing_name = str(existing.get("name", "")).strip()
    existing_placeholder = _is_placeholder_group_name(existing_name, canonical_id)
    new_placeholder = _is_placeholder_group_name(normalized_name, canonical_id)

    if existing_placeholder and not new_placeholder:
        existing["name"] = normalized_name
    elif not existing_name:
        existing["name"] = normalized_name

    if normalized_url:
        existing["url"] = normalized_url


def _extract_groups_from_graphql_payload(payload: Any) -> list[dict[str, str]]:
    found: dict[str, dict[str, str]] = {}

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            group_id = str(node.get("id", "")).strip()
            group_name = str(node.get("name", "")).strip()
            group_url = str(node.get("url", "")).strip()
            typename = str(node.get("__typename", "")).strip()

            looks_like_group = typename == "Group" or (group_id and group_url and "/groups/" in group_url)
            if looks_like_group:
                if not group_id and group_url:
                    match = GROUP_LINK_RE.search(group_url)
                    if match:
                        group_id = match.group(1)
                if group_id:
                    _upsert_group(found, group_id, group_name, group_url)

            for value in node.values():
                _walk(value)
            return

        if isinstance(node, list):
            for value in node:
                _walk(value)

    _walk(payload)
    return list(found.values())


def _extract_groups_from_page(
    page: Any,
    max_scrolls: int = 30,
    idle_rounds_to_stop: int = 4,
    scroll_wait_ms: int = 1400,
    min_scroll_rounds_before_stop: int = 8,
) -> list[dict[str, str]]:
    groups: dict[str, dict[str, str]] = {}
    graphql_hits = 0
    bulk_route_hits = 0

    def _on_response(response: Any) -> None:
        nonlocal graphql_hits, bulk_route_hits
        try:
            req = response.request
            url = str(response.url)
            method = str(getattr(req, "method", "")).upper()
            if method != "POST":
                return
            if "/api/graphql/" in url:
                graphql_hits += 1
                try:
                    payload = response.json()
                    graphql_groups = _extract_groups_from_graphql_payload(payload)
                    for item in graphql_groups:
                        _upsert_group(
                            groups,
                            str(item.get("id", "")).strip(),
                            str(item.get("name", "")).strip(),
                            str(item.get("url", "")).strip(),
                        )
                except Exception:
                    pass
            elif "/ajax/bulk-route-definitions/" in url:
                bulk_route_hits += 1
        except Exception:
            return

    page.on("response", _on_response)
    idle_rounds = 0
    graphql_idle_rounds = 0

    for scroll_index in range(max_scrolls):
        before_count = len(groups)
        graphql_before = graphql_hits
        links = page.eval_on_selector_all(
            "a[href*='/groups/']",
            """
            (nodes) => nodes.map((node) => ({
              href: node.href || '',
                            text: (node.textContent || '').trim(),
                            ariaLabel: (node.getAttribute('aria-label') || '').trim(),
                            title: (node.getAttribute('title') || '').trim()
            }))
            """,
        )

        if isinstance(links, list):
            for link in links:
                if not isinstance(link, dict):
                    continue
                href = str(link.get("href", "")).strip()
                text = str(link.get("text", "")).strip()
                aria_label = str(link.get("ariaLabel", "")).strip()
                title = str(link.get("title", "")).strip()
                match = GROUP_LINK_RE.search(href)
                if not match:
                    continue
                group_id = match.group(1)
                candidate_name = text or aria_label or title or f"Group {group_id}"
                _upsert_group(groups, group_id, candidate_name, href)

        page.mouse.wheel(0, 2500)
        page.wait_for_timeout(scroll_wait_ms)

        new_groups = len(groups) - before_count
        graphql_delta = graphql_hits - graphql_before

        if new_groups == 0:
            idle_rounds += 1
        else:
            idle_rounds = 0

        if graphql_delta == 0:
            graphql_idle_rounds += 1
        else:
            graphql_idle_rounds = 0

        if (scroll_index + 1) % 5 == 0:
            log_event(
                "Group scrape scroll progress.",
                context={
                    "scroll_round": scroll_index + 1,
                    "groups_found": len(groups),
                    "graphql_hits": graphql_hits,
                    "bulk_route_hits": bulk_route_hits,
                    "idle_rounds": idle_rounds,
                    "graphql_idle_rounds": graphql_idle_rounds,
                },
            )

        # Avoid stopping too early: require minimum scroll rounds first.
        if scroll_index + 1 < min_scroll_rounds_before_stop:
            continue

        # If GraphQL was observed, require it to be idle too; otherwise rely on DOM idle rounds.
        if idle_rounds >= idle_rounds_to_stop and (graphql_hits == 0 or graphql_idle_rounds >= 2):
            break

    log_event(
        "Group scrape network summary.",
        context={
            "groups_found": len(groups),
            "graphql_hits": graphql_hits,
            "bulk_route_hits": bulk_route_hits,
            "idle_rounds": idle_rounds,
            "graphql_idle_rounds": graphql_idle_rounds,
        },
    )

    return list(groups.values())


def _merge_groups(existing: list[dict[str, Any]], scraped: list[dict[str, str]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    now = datetime.now(timezone.utc).isoformat()
    merged_by_id: dict[str, dict[str, Any]] = {}
    existing_by_url_key: dict[str, dict[str, Any]] = {}
    existing_by_name_key: dict[str, dict[str, Any]] = {}
    existing_active_by_id: dict[str, bool] = {}
    existing_active_by_url_key: dict[str, bool] = {}

    def _url_key(value: str) -> str:
        match = GROUP_LINK_RE.search(str(value or "").strip())
        return match.group(1).strip().lower() if match else ""

    def _name_key(value: str) -> str:
        cleaned = re.sub(r"\s+", " ", str(value or "").strip().lower())
        return cleaned

    for item in existing:
        group_id = _canonical_group_id(str(item.get("id", "")).strip(), str(item.get("url", "")).strip())
        if not group_id:
            continue
        item["id"] = group_id
        existing_name = str(item.get("name", "")).strip()
        item["name"] = _normalize_group_name(existing_name, group_id)
        merged_by_id[group_id] = item
        key = _url_key(str(item.get("url", "")))
        if key:
            existing_by_url_key[key] = item
            existing_active_by_url_key[key] = bool(item.get("active", True))
        name_key = _name_key(str(item.get("name", "")))
        if name_key:
            existing_by_name_key[name_key] = item
        existing_active_by_id[group_id] = bool(item.get("active", True))

    def _preserved_active(group_id: str, group_url: str) -> bool | None:
        if group_id in existing_active_by_id:
            return existing_active_by_id[group_id]
        key = _url_key(group_url)
        if key and key in existing_active_by_url_key:
            return existing_active_by_url_key[key]
        return None

    # Track which IDs were confirmed present in the fresh scrape.
    scraped_ids: set[str] = set()

    for item in scraped:
        group_id = _canonical_group_id(str(item.get("id", "")).strip(), str(item.get("url", "")).strip())
        if not group_id:
            continue

        # Preserve existing state when the same group appears with a changed id format.
        if group_id not in merged_by_id:
            key = _url_key(str(item.get("url", "")))
            existing_match = existing_by_url_key.get(key)
            if not isinstance(existing_match, dict):
                scraped_name_key = _name_key(str(item.get("name", "")))
                existing_match = existing_by_name_key.get(scraped_name_key)
            if isinstance(existing_match, dict):
                old_id = str(existing_match.get("id", "")).strip()
                if old_id and old_id in merged_by_id:
                    del merged_by_id[old_id]
                existing_match["id"] = group_id
                merged_by_id[group_id] = existing_match

        if group_id in merged_by_id:
            merged = merged_by_id[group_id]
            merged["name"] = item.get("name") or merged.get("name")
            merged["url"] = item.get("url") or merged.get("url")
            if "active" not in merged:
                prior_active = _preserved_active(group_id, str(merged.get("url", "")))
                merged["active"] = True if prior_active is None else prior_active
            merged["updated_at"] = now
        else:
            prior_active = _preserved_active(group_id, str(item.get("url", "")))
            merged_by_id[group_id] = {
                "id": group_id,
                "name": item.get("name") or f"Group {group_id}",
                "url": item.get("url") or f"https://www.facebook.com/groups/{group_id}",
                "last_posted": None,
                "active": True if prior_active is None else prior_active,
                "updated_at": now,
            }

        scraped_ids.add(group_id)

    # Remove groups that were in the existing list but not found in the fresh scrape.
    # These are groups the user has left or that are no longer accessible.
    removed: list[dict[str, Any]] = []
    if scraped_ids:
        stale_ids = [gid for gid in list(merged_by_id) if gid not in scraped_ids]
        for gid in stale_ids:
            removed.append(merged_by_id.pop(gid))

    merged_list = list(merged_by_id.values())
    merged_list.sort(key=lambda x: str(x.get("name", "")).lower())
    return merged_list, removed


def _merge_scraped_lists(primary: list[dict[str, str]], fallback: list[dict[str, str]]) -> list[dict[str, str]]:
    merged: dict[str, dict[str, str]] = {}
    for item in primary + fallback:
        group_id = str(item.get("id", "")).strip()
        if not group_id:
            continue
        if group_id in merged:
            merged[group_id]["name"] = item.get("name") or merged[group_id].get("name") or f"Group {group_id}"
            merged[group_id]["url"] = item.get("url") or merged[group_id].get("url") or f"https://www.facebook.com/groups/{group_id}"
        else:
            merged[group_id] = {
                "id": group_id,
                "name": item.get("name") or f"Group {group_id}",
                "url": item.get("url") or f"https://www.facebook.com/groups/{group_id}",
            }
    return list(merged.values())


def scrape_groups(config_path: Path, force: bool = False) -> Path:
    config = _load_config(config_path)
    groups_path = Path(config["groups"]["source"])
    groups_path.parent.mkdir(parents=True, exist_ok=True)
    rescrape_every_days = int(config.get("groups", {}).get("rescrape_every_days", 7))

    if not force and not _should_rescrape(groups_path, rescrape_every_days):
        log_event(
            "Skipping group scrape because source is still fresh.",
            context={"groups_path": str(groups_path), "rescrape_every_days": rescrape_every_days},
        )
        return groups_path

    if force:
        log_event(
            "Force group scrape requested; bypassing freshness gate.",
            context={"groups_path": str(groups_path), "rescrape_every_days": rescrape_every_days},
        )

    session_path = ensure_session(config_path, validate_existing=False)

    try:
        from camoufox.sync_api import Camoufox
    except Exception as exc:
        raise RuntimeError(
            "Camoufox is not installed. Run: pip install -U -r requirements.txt and camoufox fetch"
        ) from exc

    group_list_url = "https://www.facebook.com/groups/joins/?nav_source=tab&ordering=viewer_added"
    scrape_cfg = config.get("groups", {}).get("scrape", {})
    max_scrolls = int(scrape_cfg.get("max_scrolls", 30))
    idle_rounds_to_stop = int(scrape_cfg.get("idle_rounds_to_stop", 4))
    scroll_wait_ms = int(scrape_cfg.get("scroll_wait_ms", 1400))
    min_scroll_rounds_before_stop = int(scrape_cfg.get("min_scroll_rounds_before_stop", 8))
    min_expected_groups = int(scrape_cfg.get("min_expected_groups", 10))
    group_feed_url = "https://www.facebook.com/groups/feed/"

    log_event("Scraping joined Facebook groups.")
    scraped_groups: list[dict[str, str]] = []

    try:
        with scrape_profile_context(config) as kwargs:
            clear_profile_locks(Path(str(kwargs.get("user_data_dir", ""))))
            with Camoufox(**kwargs) as browser:
                page = get_or_create_page(browser)
                configure_page_window(page, config)
                ensure_logged_in_in_page(page, config, session_path)
                page.goto(group_list_url, wait_until="domcontentloaded")
                page.wait_for_timeout(2500)
                scraped_groups = _extract_groups_from_page(
                    page,
                    max_scrolls=max_scrolls,
                    idle_rounds_to_stop=idle_rounds_to_stop,
                    scroll_wait_ms=scroll_wait_ms,
                    min_scroll_rounds_before_stop=min_scroll_rounds_before_stop,
                )

                if len(scraped_groups) < min_expected_groups:
                    log_event(
                        "Joined-groups scrape returned low count; running feed fallback scrape.",
                        context={
                            "joined_groups_count": len(scraped_groups),
                            "min_expected_groups": min_expected_groups,
                        },
                    )
                    page.goto(group_feed_url, wait_until="domcontentloaded")
                    page.wait_for_timeout(2500)
                    fallback_groups = _extract_groups_from_page(
                        page,
                        max_scrolls=max_scrolls,
                        idle_rounds_to_stop=idle_rounds_to_stop,
                        scroll_wait_ms=scroll_wait_ms,
                        min_scroll_rounds_before_stop=min_scroll_rounds_before_stop,
                    )
                    scraped_groups = _merge_scraped_lists(scraped_groups, fallback_groups)

                save_session_from_page(page, session_path)
    except Exception as exc:
        log_exception("Failed during group scraping.", exc)
        raise

    existing_groups = _load_existing_groups(groups_path)
    merged, removed = _merge_groups(existing_groups, scraped_groups)
    with groups_path.open("w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)

    if removed:
        removed_names = [str(g.get("name") or g.get("id", "?")) for g in removed]
        log_event(
            f"Removed {len(removed)} group(s) no longer found in scrape (left or unavailable).",
            context={"removed_groups": ", ".join(removed_names)},
        )

    log_event(
        "Group scraping completed.",
        context={
            "scraped_count": len(scraped_groups),
            "merged_count": len(merged),
            "removed_count": len(removed),
        },
    )
    return groups_path
