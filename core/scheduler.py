from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
import random
import re
import time
from pathlib import Path
import threading
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


def _load_posted_log(posted_log_path: Path) -> list[dict[str, Any]]:
    """Load posted log entries from disk."""
    if not posted_log_path.exists():
        return []
    try:
        raw = json.loads(posted_log_path.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            return [item for item in raw if isinstance(item, dict)]
    except Exception:
        pass
    return []


def _get_already_posted_group_ids(posted_log_path: Path) -> set[str]:
    """Return set of group IDs/URLs that have status 'posted' in the log."""
    entries = _load_posted_log(posted_log_path)
    posted: set[str] = set()
    for entry in entries:
        if str(entry.get("status", "")).strip().lower() == "posted" and not entry.get("dry_run", False):
            gid = str(entry.get("group_id", "")).strip()
            gurl = str(entry.get("group_url", "")).strip()
            if gid:
                posted.add(gid)
            if gurl:
                posted.add(gurl)
    return posted


def _append_posted_log(posted_log_path: Path, entry: dict[str, Any]) -> None:
    posted_log_path.parent.mkdir(parents=True, exist_ok=True)
    data = _load_posted_log(posted_log_path)

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
            loc = page.locator(selector)
            if loc.count() < 1:
                continue
            target = loc.first
            if not target.is_visible(timeout=500):
                continue
            target.click(timeout=timeout)
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


def _lock_background_scroll(page: Any) -> None:
    try:
        page.evaluate(
            """
            () => {
              const y = window.scrollY || document.documentElement.scrollTop || 0;
                            if (!window.__fbpost_scroll_lock_state) {
                                window.__fbpost_scroll_lock_state = {
                                    y,
                                    scrollTo: window.scrollTo,
                                    scrollBy: window.scrollBy,
                                };
                            }

                            const onWheel = (e) => e.preventDefault();
                            const onTouch = (e) => e.preventDefault();
                            const onKey = (e) => {
                                const blocked = ['PageUp', 'PageDown', 'ArrowUp', 'ArrowDown', 'Home', 'End', ' ', 'Spacebar'];
                                if (blocked.includes(e.key)) e.preventDefault();
                            };

                            window.__fbpost_scroll_lock_state.onWheel = onWheel;
                            window.__fbpost_scroll_lock_state.onTouch = onTouch;
                            window.__fbpost_scroll_lock_state.onKey = onKey;

                            window.addEventListener('wheel', onWheel, { passive: false, capture: true });
                            window.addEventListener('touchmove', onTouch, { passive: false, capture: true });
                            window.addEventListener('keydown', onKey, { passive: false, capture: true });

                            window.scrollTo = () => {};
                            window.scrollBy = () => {};

              const html = document.documentElement;
              const body = document.body;
              if (html) html.style.overflow = 'hidden';
                            if (body) {
                                body.style.overflow = 'hidden';
                                body.style.position = 'fixed';
                                body.style.top = `-${y}px`;
                                body.style.left = '0';
                                body.style.right = '0';
                                body.style.width = '100%';
                            }
            }
            """
        )
    except Exception:
        return


def _unlock_background_scroll(page: Any) -> None:
    try:
        page.evaluate(
            """
            () => {
                            const state = window.__fbpost_scroll_lock_state || {};
                            if (state.onWheel) window.removeEventListener('wheel', state.onWheel, { capture: true });
                            if (state.onTouch) window.removeEventListener('touchmove', state.onTouch, { capture: true });
                            if (state.onKey) window.removeEventListener('keydown', state.onKey, { capture: true });
                            if (typeof state.scrollTo === 'function') window.scrollTo = state.scrollTo;
                            if (typeof state.scrollBy === 'function') window.scrollBy = state.scrollBy;

              const html = document.documentElement;
              const body = document.body;
              if (html) html.style.overflow = '';
                            if (body) {
                                body.style.overflow = '';
                                body.style.position = '';
                                body.style.top = '';
                                body.style.left = '';
                                body.style.right = '';
                                body.style.width = '';
                            }
                            const y = Number(state.y || 0);
              window.scrollTo(0, Number.isFinite(y) ? y : 0);
                            window.__fbpost_scroll_lock_state = undefined;
            }
            """
        )
    except Exception:
        return


def _get_popup_root(page: Any, *, timeout: int = 3000) -> Any | None:
    """Locate the composer popup dialog root. Returns a Locator or None.

    Facebook pages often have multiple div[role='dialog'] elements in the DOM
    (cookie banners, notification popups, etc.).  We must NOT rely on
    ``locator.first`` because the first dialog in DOM order is frequently a
    *hidden* one — calling ``wait_for`` on it would time-out even though the
    composer popup is perfectly visible further down.

    Instead we poll *all* dialogs until we find a visible one that looks like
    the composer.
    """
    dialog_selector = "div[role='dialog']"
    deadline = time.monotonic() + (timeout / 1000.0)

    while time.monotonic() < deadline:
        try:
            loc = page.locator(dialog_selector)
            count = loc.count()
            for i in range(count):
                dialog = loc.nth(i)
                try:
                    if not dialog.is_visible(timeout=200):
                        continue
                except Exception:
                    continue

                # Best check: dialog contains a contenteditable textbox
                try:
                    tb = dialog.locator(
                        "div[contenteditable='true'][role='textbox'][data-lexical-editor='true']"
                    )
                    if tb.count() > 0:
                        return dialog
                except Exception:
                    pass

                # Fallback: dialog contains heading/text "Create post" / "Buat postingan"
                try:
                    text_content = dialog.inner_text(timeout=300).lower()
                    if "create post" in text_content or "buat postingan" in text_content:
                        return dialog
                except Exception:
                    pass
        except Exception:
            pass
        page.wait_for_timeout(200)

    return None


def _click_composer_trigger_by_text(page: Any) -> bool:
    """Fallback: find composer trigger by its text content ('Write something...' / 'Tulis sesuatu...')."""
    trigger_texts = ["Write something", "Tulis sesuatu"]
    for txt in trigger_texts:
        try:
            # The trigger is a span inside a div[role='button']
            span = page.locator(f"div[role='button'] span:has-text('{txt}')")
            if span.count() > 0:
                first_span = span.first
                if first_span.is_visible(timeout=1500):
                    # Click the parent button, not the span itself
                    btn = first_span.locator("xpath=ancestor::div[@role='button'][1]")
                    if btn.count() > 0:
                        btn.first.click(timeout=3000)
                        return True
                    # Fallback: click the span
                    first_span.click(timeout=3000)
                    return True
        except Exception:
            continue
    return False


def _open_group_composer(
    page: Any,
    composer_selectors: list[str],
) -> Any | None:
    """Click the composer trigger and return the popup dialog root locator, or None."""
    log_event("state_enter: pre_popup")

    # Check if popup is already open
    popup = _get_popup_root(page, timeout=1200)
    if popup is not None:
        log_event("state_exit: pre_popup_success (popup already open)")
        return popup

    attempts = 3
    for attempt_num in range(1, attempts + 1):
        # Try CSS selectors first
        clicked = _click_first_available(page, selectors=composer_selectors, timeout=5000)
        if not clicked:
            # Fallback: try text-based trigger
            clicked = _click_composer_trigger_by_text(page)

        if clicked:
            page.wait_for_timeout(1500)
            popup = _get_popup_root(page, timeout=5000)
            if popup is not None:
                log_event("state_exit: pre_popup_success", context={"attempt": attempt_num})
                return popup
        else:
            page.wait_for_timeout(1000)

    log_event("state_exit: pre_popup_failed (could not open composer)")
    return None


def _fill_post_text(page: Any, popup_root: Any, text: str) -> bool:
    """Fill the post textbox inside the popup dialog."""
    textbox_selector = "div[contenteditable='true'][role='textbox'][data-lexical-editor='true']"
    try:
        boxes = popup_root.locator(textbox_selector)
        for i in range(boxes.count()):
            box = boxes.nth(i)
            try:
                if not box.is_visible(timeout=500):
                    continue
                aria_label = (box.get_attribute("aria-label") or "").strip().lower()
                aria_placeholder = (box.get_attribute("aria-placeholder") or "").strip().lower()
                # Skip comment inputs explicitly.
                if "comment" in aria_label or "comment" in aria_placeholder:
                    continue
                box.click(timeout=5000)
                try:
                    box.fill(text, timeout=5000)
                except Exception:
                    page.keyboard.press("Control+a")
                    page.keyboard.insert_text(text)
                log_event("popup_textbox_found")
                return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def _click_enabled_post_button(page: Any, popup_root: Any, timeout_ms: int = 7000) -> bool:
    """Wait for the Post button inside the popup to become enabled, then click it."""
    post_button_selectors = [
        "div[role='button'][aria-label='Post']",
        "div[role='button'][aria-label='Kirim']",
        "div[data-testid='react-composer-post-button']",
    ]
    deadline = time.monotonic() + (max(1000, timeout_ms) / 1000.0)
    while time.monotonic() < deadline:
        for selector in post_button_selectors:
            try:
                loc = popup_root.locator(selector)
                if loc.count() < 1:
                    continue
                button = loc.first
                if not button.is_visible(timeout=250):
                    continue
                aria_disabled = str(button.get_attribute("aria-disabled") or "").strip().lower()
                if aria_disabled == "true":
                    continue
                log_event("popup_post_button_enabled")
                button.click(timeout=1200)
                log_event("popup_post_clicked")
                return True
            except Exception:
                continue
        page.wait_for_timeout(250)
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


def _attach_image_if_present(page: Any, popup_root: Any, image_paths: list[str]) -> bool:
    """Attach images via the file input inside the popup dialog."""
    if not image_paths:
        return True

    resolved_paths: list[str] = []
    for image_path in image_paths:
        path = Path(image_path)
        if not path.exists():
            return False
        resolved_paths.append(str(path.resolve()))

    file_input_selector = "input[type='file']"
    photo_button_selectors = [
        "div[role='button'][aria-label='Photo/video']",
        "div[role='button'][aria-label='Foto/video']",
    ]

    for _ in range(2):
        try:
            file_input = popup_root.locator(file_input_selector)
            if file_input.count() > 0:
                file_input.first.set_input_files(resolved_paths, timeout=7000)
                page.wait_for_timeout(2500)
                return True
        except Exception:
            pass
        # Some group composers require opening the media picker before the file input appears.
        for sel in photo_button_selectors:
            try:
                btn = popup_root.locator(sel)
                if btn.count() > 0 and btn.first.is_visible(timeout=500):
                    btn.first.click(timeout=2500)
                    page.wait_for_timeout(1200)
                    break
            except Exception:
                continue
    return False


def _post_to_group(
    page: Any,
    group: dict[str, Any],
    template: dict[str, Any],
    *,
    dry_run: bool,
    home_url: str,
    composer_selectors: list[str],
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

    # --- State A: click composer trigger ---
    popup_root = _open_group_composer(page, composer_selectors)
    if popup_root is None:
        log_event("popup_flow_failed", context={"group": group_name, "reason": "Could not open group composer"})
        return False, "Could not open group composer"

    # --- State B: all actions scoped to popup_root ---
    log_event("state_enter: popup", context={"group": group_name})
    _lock_background_scroll(page)
    try:
        page.wait_for_timeout(random.randint(500, 1200))

        if text and not _fill_post_text(page, popup_root, text):
            log_event("popup_flow_failed", context={"group": group_name, "reason": "Could not fill post textbox"})
            return False, "Could not fill post textbox"
        if text:
            log_event("Posting step: text inserted.", context={"group": group_name, "text_length": len(text)})

        if image_paths:
            image_attached = _attach_image_if_present(page, popup_root, image_paths)
            if image_attached:
                log_event("Posting step: images attached.", context={"group": group_name, "image_count": len(image_paths)})
            else:
                # Continue with text-only post when image upload UI is unavailable in this group.
                log_event(
                    "Posting step: image attachment failed, continuing without images.",
                    level="WARNING",
                    context={"group": group_name, "image_count": len(image_paths)},
                )

        posted = _click_enabled_post_button(page, popup_root, timeout_ms=7000)
        if not posted:
            log_event("popup_flow_failed", context={"group": group_name, "reason": "Post button not found or not enabled"})
            return False, "Post button not found"

        # Wait for the composer dialog to close, confirming the post was submitted.
        # The dialog closing is the definitive signal that Facebook accepted the post.
        dialog_closed = False
        try:
            popup_root.wait_for(state="hidden", timeout=30000)
            dialog_closed = True
        except Exception:
            # Fallback: even if wait_for fails, give it extra time
            page.wait_for_timeout(10000)

        if dialog_closed:
            log_event("Posting step: composer dialog closed (post confirmed).", context={"group": group_name})
        else:
            log_event(
                "Posting step: composer dialog did not close within timeout, assuming posted.",
                level="WARNING",
                context={"group": group_name},
            )

        page.wait_for_timeout(random.randint(2000, 4000))
        page.goto(home_url, wait_until="domcontentloaded")
        page.wait_for_timeout(random.randint(1500, 3000))
        return True, "Posted"
    finally:
        _unlock_background_scroll(page)


def _control_checkpoint(
    pause_event: threading.Event | None,
    stop_event: threading.Event | None,
) -> bool:
    if stop_event is not None and stop_event.is_set():
        return False
    if pause_event is not None:
        while pause_event.is_set():
            if stop_event is not None and stop_event.is_set():
                return False
            time.sleep(0.2)
    return True


def _sleep_controlled(
    seconds: float,
    *,
    pause_event: threading.Event | None,
    stop_event: threading.Event | None,
) -> bool:
    remaining = max(0.0, float(seconds))
    while remaining > 0:
        if not _control_checkpoint(pause_event, stop_event):
            return False
        chunk = min(1.0, remaining)
        time.sleep(chunk)
        remaining -= chunk
    return _control_checkpoint(pause_event, stop_event)


def _sleep_between_posts(
    min_delay: int,
    max_delay: int,
    *,
    dry_run: bool,
    pause_event: threading.Event | None,
    stop_event: threading.Event | None,
) -> bool:
    if dry_run:
        return _sleep_controlled(0.2, pause_event=pause_event, stop_event=stop_event)
    delay_seconds = random.randint(min_delay * 60, max_delay * 60)
    return _sleep_controlled(delay_seconds, pause_event=pause_event, stop_event=stop_event)


def run_scheduler(
    config_path: Path,
    run_once: bool,
    *,
    force_dry_run: bool | None = None,
    pause_event: threading.Event | None = None,
    stop_event: threading.Event | None = None,
) -> None:
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
    auto_skip = bool(posting_cfg.get("auto_skip", False))

    home_url = "https://www.facebook.com/"
    composer_selectors: list[str] = []

    default_composer_selectors = [
        # Primary: the composer card trigger area on Facebook group pages
        "div.xod5an3 div[role='button'][tabindex='0']",
        # Fallbacks for different FB UI variants
        "div[data-testid='group-composer-enter-composer']",
        "div[role='button'][aria-label*='Create post' i]",
        "div[role='button'][aria-label*='Buat postingan' i]",
    ]

    composer_selectors = list(dict.fromkeys(composer_selectors + default_composer_selectors))

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
    already_posted = _get_already_posted_group_ids(posted_log_path) if auto_skip else set()
    eligible_groups: list[dict[str, Any]] = []
    skipped_auto = 0
    for group in groups:
        url = str(group.get("url", "")).strip()
        gid = str(group.get("id", "")).strip() or _group_id_from_url(url)
        if url in blacklist or gid in blacklist:
            continue
        if _is_group_on_cooldown(group, cooldown_hours):
            continue
        if auto_skip and (gid in already_posted or url in already_posted):
            skipped_auto += 1
            continue
        eligible_groups.append(group)
    if auto_skip and skipped_auto > 0:
        log_event(f"Auto-skip: skipped {skipped_auto} already-posted groups.", context={"skipped": skipped_auto})

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
                if stop_requested or not _control_checkpoint(pause_event, stop_event):
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
                        captcha_keywords=captcha_keywords,
                        rate_limit_keywords=rate_limit_keywords,
                    )
                    if success:
                        break
                    if detail in {"CAPTCHA_DETECTED", "RATE_LIMIT_DETECTED", "GROUP_404"}:
                        break
                    if attempt < retry_max_attempts:
                        backoff_seconds = 0.2 if dry_run else retry_backoff_seconds * attempt
                        if not _sleep_controlled(backoff_seconds, pause_event=pause_event, stop_event=stop_event):
                            stop_requested = True
                            break

                if stop_requested or not _control_checkpoint(pause_event, stop_event):
                    break

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
                    rest_seconds = 0.2
                    if not dry_run:
                        low = max(1, rest_minutes - 10)
                        high = rest_minutes + 10
                        rest_seconds = random.randint(low, high) * 60
                    if not _sleep_controlled(rest_seconds, pause_event=pause_event, stop_event=stop_event):
                        stop_requested = True
                        break

                if not _sleep_between_posts(
                    min_delay,
                    max_delay,
                    dry_run=dry_run,
                    pause_event=pause_event,
                    stop_event=stop_event,
                ):
                    stop_requested = True
                    break

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
