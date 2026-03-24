from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
import random
import re
import time
from pathlib import Path
from typing import Any

from core.config_loader import load_config
from core.logger import log_event, log_exception
from core.post_queue import (
    load_queue_state,
    load_templates,
    plan_templates_for_session,
    save_queue_state,
)
from core.session_manager import (
    camoufox_kwargs,
    configure_page_window,
    ensure_logged_in_in_page,
    ensure_session,
    get_or_create_page,
    load_session_data,
    maybe_apply_session_cookies,
    save_session_from_page,
)


GROUP_LINK_RE = re.compile(r"facebook\.com/groups/([^/?#]+)", re.IGNORECASE)


def _load_config(config_path: Path) -> dict:
    return load_config(config_path)


def _load_groups(groups_path: Path) -> list[dict[str, Any]]:
    if not groups_path.exists():
        return []
    with groups_path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def _save_groups(groups_path: Path, groups: list[dict[str, Any]]) -> None:
    groups_path.parent.mkdir(parents=True, exist_ok=True)
    with groups_path.open("w", encoding="utf-8") as f:
        json.dump(groups, f, ensure_ascii=False, indent=2)


def _load_blacklist(blacklist_path: Path) -> set[str]:
    if not blacklist_path.exists():
        return set()
    items: set[str] = set()
    for line in blacklist_path.read_text(encoding="utf-8").splitlines():
        value = line.strip()
        if value and not value.startswith("#"):
            items.add(value)
    return items


def _group_id_from_url(url: str) -> str:
    match = GROUP_LINK_RE.search(url)
    return match.group(1) if match else ""


def _is_group_on_cooldown(group: dict[str, Any], cooldown_hours: int) -> bool:
    last_posted = group.get("last_posted")
    if not isinstance(last_posted, str) or not last_posted.strip():
        return False
    try:
        ts = datetime.fromisoformat(last_posted)
    except ValueError:
        return False

    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)

    return datetime.now(timezone.utc) - ts < timedelta(hours=cooldown_hours)


def _append_posted_log(posted_log_path: Path, entry: dict[str, Any]) -> None:
    posted_log_path.parent.mkdir(parents=True, exist_ok=True)
    data: list[dict[str, Any]] = []
    if posted_log_path.exists():
        try:
            raw = json.loads(posted_log_path.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                data = [item for item in raw if isinstance(item, dict)]
        except Exception:
            data = []

    data.append(entry)
    with posted_log_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _detect_keyword_block(page: Any, keywords: list[str]) -> bool:
    if not keywords:
        return False
    try:
        content = page.content().lower()
    except Exception:
        return False
    return any(word.lower() in content for word in keywords)


def _detect_captcha_block(page: Any, keywords: list[str]) -> bool:
    """Detect a real captcha/challenge page and avoid broad false positives."""
    keyword_hits = False
    visible_text = ""
    try:
        visible_text = page.locator("body").inner_text(timeout=1200).lower()
        keyword_hits = any(word.lower() in visible_text for word in keywords)
    except Exception:
        keyword_hits = False

    url = ""
    try:
        url = str(page.url).lower()
    except Exception:
        url = ""

    url_indicates_challenge = any(token in url for token in ["checkpoint", "captcha", "challenge"])

    selector_matches = [
        "iframe[src*='captcha']",
        "iframe[title*='captcha' i]",
        "input[name*='captcha' i]",
        "img[alt*='captcha' i]",
        "div[id*='captcha' i]",
        "form[action*='checkpoint']",
    ]
    has_captcha_ui = False
    for selector in selector_matches:
        try:
            if page.locator(selector).count() > 0:
                has_captcha_ui = True
                break
        except Exception:
            continue

    return has_captcha_ui or (url_indicates_challenge and keyword_hits)


def _save_failure_screenshot(page: Any, screenshots_dir: Path, group_id: str, reason: str) -> str | None:
    screenshots_dir.mkdir(parents=True, exist_ok=True)
    safe_reason = re.sub(r"[^a-zA-Z0-9_-]+", "_", reason).strip("_")[:40] or "error"
    filename = f"{datetime.now(timezone.utc):%Y%m%d_%H%M%S}_{group_id}_{safe_reason}.png"
    path = screenshots_dir / filename
    try:
        page.screenshot(path=str(path), full_page=True)
        return str(path)
    except Exception:
        return None


def _human_type(page: Any, text: str) -> None:
    for char in text:
        page.keyboard.type(char, delay=random.randint(30, 120))


def _click_first_available(page: Any, selectors: list[str], timeout: int = 3000) -> bool:
    for selector in selectors:
        try:
            page.locator(selector).first.click(timeout=timeout)
            return True
        except Exception:
            continue
    return False


def _is_any_selector_visible(page: Any, selectors: list[str], timeout: int = 1200) -> bool:
    for selector in selectors:
        try:
            if page.locator(selector).first.is_visible(timeout=timeout):
                return True
        except Exception:
            continue
    return False


def _open_group_composer(page: Any, composer_selectors: list[str], textbox_selectors: list[str]) -> bool:
    if _is_any_selector_visible(page, textbox_selectors, timeout=1200):
        return True

    attempts = 4
    for _ in range(attempts):
        if _click_first_available(page, selectors=composer_selectors, timeout=5000):
            page.wait_for_timeout(1200)
            if _is_any_selector_visible(page, textbox_selectors, timeout=4000):
                return True
        else:
            page.wait_for_timeout(1000)

    return _is_any_selector_visible(page, textbox_selectors, timeout=2500)


def _fill_post_text(page: Any, text: str, selectors: list[str]) -> bool:

    for selector in selectors:
        try:
            box = page.locator(selector).first
            box.click(timeout=5000)
            try:
                box.fill(text, timeout=5000)
            except Exception:
                page.keyboard.press("Control+a")
                page.keyboard.insert_text(text)
            return True
        except Exception:
            continue
    return False


def _collect_template_images(template: dict[str, Any]) -> list[str]:
    images: list[str] = []

    single_image = template.get("image")
    if isinstance(single_image, str) and single_image.strip():
        images.append(single_image.strip())

    image_list = template.get("images")
    if isinstance(image_list, list):
        for item in image_list:
            if isinstance(item, str) and item.strip():
                images.append(item.strip())

    # Deduplicate while preserving order.
    unique_images: list[str] = []
    seen: set[str] = set()
    for img in images:
        if img not in seen:
            seen.add(img)
            unique_images.append(img)
    return unique_images


def _attach_image_if_present(page: Any, image_paths: list[str], file_input_selector: str) -> bool:
    if not image_paths:
        return True

    resolved_paths: list[str] = []
    for image_path in image_paths:
        path = Path(image_path)
        if not path.exists():
            return False
        resolved_paths.append(str(path.resolve()))

    for _ in range(2):
        try:
            page.locator(file_input_selector).first.set_input_files(resolved_paths, timeout=7000)
            page.wait_for_timeout(2500)
            return True
        except Exception:
            # Some group composers require opening the media picker before the file input appears.
            _click_first_available(
                page,
                selectors=[
                    "div[role='button']:has-text('Photo/video')",
                    "div[role='button']:has-text('Foto/video')",
                    "span:has-text('Photo/video')",
                    "span:has-text('Foto/video')",
                ],
                timeout=2500,
            )
            page.wait_for_timeout(1200)
    return False


def _post_to_group(
    page: Any,
    group: dict[str, Any],
    template: dict[str, Any],
    *,
    dry_run: bool,
    home_url: str,
    composer_selectors: list[str],
    textbox_selectors: list[str],
    post_button_selectors: list[str],
    file_input_selector: str,
    captcha_keywords: list[str],
    rate_limit_keywords: list[str],
) -> tuple[bool, str]:
    group_name = str(group.get("name", "Unknown Group"))
    group_url = str(group.get("url", ""))
    text = str(template.get("text", "")).strip()
    image_paths = _collect_template_images(template)

    if not group_url:
        return False, "Missing group URL"

    if dry_run:
        return True, f"Dry run: skipped posting for {group_name}"

    log_event("Posting step: open group page.", context={"group": group_name, "url": group_url})
    response = page.goto(group_url, wait_until="domcontentloaded")
    if response is not None and response.status == 404:
        return False, "GROUP_404"

    page.wait_for_timeout(random.randint(2000, 4000))

    if _detect_captcha_block(page, captcha_keywords):
        return False, "CAPTCHA_DETECTED"
    if _detect_keyword_block(page, rate_limit_keywords):
        return False, "RATE_LIMIT_DETECTED"

    composer_open = _open_group_composer(page, composer_selectors, textbox_selectors)
    if not composer_open:
        return False, "Could not open group composer"

    log_event("Posting step: composer opened.", context={"group": group_name})
    page.wait_for_timeout(random.randint(500, 1200))

    if text and not _fill_post_text(page, text, textbox_selectors):
        return False, "Could not fill post textbox"
    if text:
        log_event("Posting step: text inserted.", context={"group": group_name, "text_length": len(text)})

    if image_paths and not _attach_image_if_present(page, image_paths, file_input_selector):
        return False, "Image attachment failed"
    if image_paths:
        log_event("Posting step: images attached.", context={"group": group_name, "image_count": len(image_paths)})

    posted = _click_first_available(page, selectors=post_button_selectors, timeout=4000)
    if not posted:
        return False, "Post button not found"

    page.wait_for_timeout(5000)
    page.goto(home_url, wait_until="domcontentloaded")
    page.wait_for_timeout(random.randint(600, 1200))
    return True, "Posted"


def _sleep_between_posts(min_delay: int, max_delay: int, *, dry_run: bool) -> None:
    if dry_run:
        time.sleep(0.2)
        return
    delay_seconds = random.randint(min_delay * 60, max_delay * 60)
    time.sleep(delay_seconds)


def run_scheduler(config_path: Path, run_once: bool, *, force_dry_run: bool | None = None) -> None:
    config = _load_config(config_path)
    groups_path = Path(config["groups"]["source"])
    blacklist_path = Path(config["groups"]["blacklist"])
    paths_cfg = config.get("paths", {})
    posted_log_path = Path(str(paths_cfg.get("posted_log", "data/posted_log.json")))
    queue_state_path = Path(str(paths_cfg.get("queue_state", "data/queue_state.json")))
    screenshots_dir = Path(str(paths_cfg.get("screenshots", "logs/screenshots")))

    posting_cfg = config.get("posting", {})
    min_delay = int(posting_cfg.get("min_delay_minutes", 3))
    max_delay = int(posting_cfg.get("max_delay_minutes", 5))
    max_posts = int(posting_cfg.get("max_posts_per_session", 15))
    cooldown_hours = int(posting_cfg.get("cooldown_hours", 24))
    rest_every = int(posting_cfg.get("rest_every_n_posts", 10))
    rest_minutes = int(posting_cfg.get("rest_duration_minutes", 30))
    rotation_mode = str(posting_cfg.get("rotation_mode", "single"))
    selected_template_file = str(posting_cfg.get("template_file", "")).strip()

    facebook_cfg = config.get("facebook", {})
    selectors_cfg = facebook_cfg.get("selectors", {})
    home_url = str(facebook_cfg.get("home_url", "https://www.facebook.com/"))
    composer_selectors = [str(s) for s in selectors_cfg.get("composer_buttons", [])]
    textbox_selectors = [str(s) for s in selectors_cfg.get("textboxes", [])]
    post_button_selectors = [str(s) for s in selectors_cfg.get("post_buttons", [])]
    file_input_selector = str(selectors_cfg.get("file_input", "input[type='file']"))

    default_composer_selectors = [
        "div[role='button'][aria-label*='Create post' i]",
        "div[role='button']:has-text('Create post')",
        "div[role='button']:has-text(\"What's on your mind\")",
        "div[role='button']:has-text('Apa yang Anda pikirkan')",
    ]
    default_textbox_selectors = [
        "div[role='dialog'] div[role='textbox']",
        "div[contenteditable='true'][data-lexical-editor='true']",
    ]
    default_post_button_selectors = [
        "div[role='button'][aria-label='Post']",
        "div[role='button'][aria-label='Post to group']",
        "div[role='button']:has-text('Post to group')",
    ]

    composer_selectors = list(dict.fromkeys(composer_selectors + default_composer_selectors))
    textbox_selectors = list(dict.fromkeys(textbox_selectors + default_textbox_selectors))
    post_button_selectors = list(dict.fromkeys(post_button_selectors + default_post_button_selectors))

    error_cfg = config.get("error_handling", {})
    retry_max_attempts = max(1, int(error_cfg.get("retry_max_attempts", 2)))
    retry_backoff_seconds = max(1, int(error_cfg.get("retry_backoff_seconds", 3)))
    screenshot_on_failure = bool(error_cfg.get("screenshot_on_failure", True))
    mark_inactive_on_404 = bool(error_cfg.get("mark_inactive_on_404", True))
    pause_on_captcha = bool(error_cfg.get("pause_on_captcha", True))
    stop_on_rate_limit = bool(error_cfg.get("stop_on_rate_limit", True))
    captcha_keywords = [str(s) for s in error_cfg.get("captcha_keywords", [])]
    rate_limit_keywords = [str(s) for s in error_cfg.get("rate_limit_keywords", [])]

    configured_dry_run = bool(posting_cfg.get("dry_run", True))
    dry_run = configured_dry_run if force_dry_run is None else force_dry_run

    if run_once:
        max_posts = min(max_posts, 1)

    groups = [g for g in _load_groups(groups_path) if bool(g.get("active", True))]
    blacklist = _load_blacklist(blacklist_path)
    eligible_groups: list[dict[str, Any]] = []
    for group in groups:
        url = str(group.get("url", "")).strip()
        gid = str(group.get("id", "")).strip() or _group_id_from_url(url)
        if url in blacklist or gid in blacklist:
            continue
        if _is_group_on_cooldown(group, cooldown_hours):
            continue
        eligible_groups.append(group)

    templates = load_templates()
    if selected_template_file:
        templates = [t for t in templates if str(t.get("template_file", "")).strip() == selected_template_file]

    if not eligible_groups:
        log_event("No eligible groups found. Check groups.json, cooldowns, and blacklist.")
        return

    if not templates:
        log_event("No templates found in templates/. Add post_*.yaml files.")
        return

    queue_state = load_queue_state(queue_state_path)
    planned_templates, next_queue_state = plan_templates_for_session(
        rotation_mode,
        len(eligible_groups),
        templates,
        queue_state,
    )

    if not planned_templates:
        log_event("No template plan could be generated.", level="ERROR")
        return

    session_path = ensure_session(config_path, validate_existing=False)
    session_data = load_session_data(session_path)

    try:
        from camoufox.sync_api import Camoufox
    except Exception as exc:
        raise RuntimeError(
            "Camoufox is not installed. Run: pip install -U -r requirements.txt and camoufox fetch"
        ) from exc

    kwargs = camoufox_kwargs(config)

    posted_count = 0
    stop_requested = False
    log_event(
        "Scheduler started.",
        context={
            "rotation_mode": rotation_mode,
            "eligible_groups": len(eligible_groups),
            "max_posts": max_posts,
            "dry_run": dry_run,
        },
    )

    try:
        with Camoufox(**kwargs) as browser:
            page = get_or_create_page(browser)
            configure_page_window(page, config)
            maybe_apply_session_cookies(page, session_data, config)
            ensure_logged_in_in_page(page, config, session_path)

            for index, group in enumerate(eligible_groups):
                if posted_count >= max_posts:
                    break
                if stop_requested:
                    break

                template = planned_templates[index]
                success = False
                detail = "Unknown error"
                for attempt in range(1, retry_max_attempts + 1):
                    success, detail = _post_to_group(
                        page,
                        group,
                        template,
                        dry_run=dry_run,
                        home_url=home_url,
                        composer_selectors=composer_selectors,
                        textbox_selectors=textbox_selectors,
                        post_button_selectors=post_button_selectors,
                        file_input_selector=file_input_selector,
                        captcha_keywords=captcha_keywords,
                        rate_limit_keywords=rate_limit_keywords,
                    )
                    if success:
                        break
                    if detail in {"CAPTCHA_DETECTED", "RATE_LIMIT_DETECTED", "GROUP_404"}:
                        break
                    if attempt < retry_max_attempts:
                        if dry_run:
                            time.sleep(0.2)
                        else:
                            time.sleep(retry_backoff_seconds * attempt)

                now_iso = datetime.now(timezone.utc).isoformat()
                template_file = str(template.get("template_file", "unknown"))
                group_name = str(group.get("name", "Unknown Group"))
                group_url = str(group.get("url", ""))
                group_id = str(group.get("id", "")) or _group_id_from_url(group_url)

                if success:
                    posted_count += 1
                    group["last_posted"] = now_iso
                    group["updated_at"] = now_iso
                    log_event(
                        "Post success.",
                        context={
                            "group": group_name,
                            "url": group_url,
                            "template": template_file,
                            "detail": detail,
                            "dry_run": dry_run,
                        },
                    )
                    _append_posted_log(
                        posted_log_path,
                        {
                            "timestamp": now_iso,
                            "group_id": group_id,
                            "group_name": group_name,
                            "group_url": group_url,
                            "template": template_file,
                            "status": "posted",
                            "dry_run": dry_run,
                            "detail": detail,
                        },
                    )
                else:
                    log_event(
                        "Post skipped/failed.",
                        level="WARNING",
                        context={
                            "group": group_name,
                            "url": group_url,
                            "template": template_file,
                            "detail": detail,
                            "dry_run": dry_run,
                        },
                    )

                    screenshot_path = None
                    if screenshot_on_failure and not dry_run:
                        screenshot_path = _save_failure_screenshot(page, screenshots_dir, group_id or "group", detail)

                    if detail == "GROUP_404" and mark_inactive_on_404:
                        group["active"] = False
                        group["updated_at"] = now_iso

                    if detail == "CAPTCHA_DETECTED" and pause_on_captcha:
                        stop_requested = True

                    if detail == "RATE_LIMIT_DETECTED" and stop_on_rate_limit:
                        stop_requested = True

                    _append_posted_log(
                        posted_log_path,
                        {
                            "timestamp": now_iso,
                            "group_id": group_id,
                            "group_name": group_name,
                            "group_url": group_url,
                            "template": template_file,
                            "status": "failed",
                            "dry_run": dry_run,
                            "detail": detail,
                            "screenshot": screenshot_path,
                        },
                    )

                if posted_count > 0 and rest_every > 0 and posted_count % rest_every == 0:
                    if dry_run:
                        time.sleep(0.2)
                    else:
                        low = max(1, rest_minutes - 10)
                        high = rest_minutes + 10
                        time.sleep(random.randint(low, high) * 60)

                _sleep_between_posts(min_delay, max_delay, dry_run=dry_run)

            save_session_from_page(page, session_path)
    except Exception as exc:
        log_exception("Scheduler failed unexpectedly.", exc)
        raise

    save_queue_state(queue_state_path, next_queue_state)
    _save_groups(groups_path, groups)

    log_event(
        "Scheduler cycle completed.",
        context={
            "posted_count": posted_count,
            "dry_run": dry_run,
            "rotation_mode": rotation_mode,
        },
    )
