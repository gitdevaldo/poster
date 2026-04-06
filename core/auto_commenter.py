"""Auto Commenter — monitors Facebook groups and comments on latest posts.

Flow (sequential):
  [navigate Group A → comment or skip → delay] → [navigate Group B → ...] → repeat

The browser stays open for the entire session.
Profile is copied once at start and deleted on stop.
comment_log.json tracks {group_id: last_commented_post_id} to avoid duplicates.
"""
from __future__ import annotations

import json
import random
import re
import shutil
import time
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator

from core.config_loader import load_config
from core.logger import log_event, log_exception


GROUP_LINK_RE = re.compile(r"facebook\.com/groups/([^/?#]+)", re.IGNORECASE)
POST_LINK_RE = re.compile(
    r"facebook\.com/(?:groups/[^/?#]+/)?(?:posts|permalink)/([^/?#&]+)",
    re.IGNORECASE,
)


def _persistent_profile_dir(config: dict[str, Any]) -> Path | None:
    session_cfg = config.get("session", {}) if isinstance(config.get("session"), dict) else {}
    profile_dir = str(session_cfg.get("persistent_profile_dir", "")).strip()
    return Path(profile_dir) if profile_dir else None


def _camoufox_launch_kwargs(config: dict[str, Any]) -> dict[str, Any]:
    browser_cfg = config.get("browser", {}) if isinstance(config.get("browser"), dict) else {}
    camoufox_cfg = config.get("camoufox", {}) if isinstance(config.get("camoufox"), dict) else {}

    kwargs: dict[str, Any] = {}
    kwargs["headless"] = bool(browser_cfg.get("headless", True))
    if "humanize" in browser_cfg:
        kwargs["humanize"] = bool(browser_cfg["humanize"])
    if "os" in camoufox_cfg:
        kwargs["os"] = camoufox_cfg["os"]
    if "geoip" in camoufox_cfg:
        kwargs["geoip"] = bool(camoufox_cfg["geoip"])
    proxy = camoufox_cfg.get("proxy")
    if isinstance(proxy, dict) and proxy:
        kwargs["proxy"] = proxy

    profile_dir = _persistent_profile_dir(config)
    if profile_dir:
        kwargs["user_data_dir"] = str(profile_dir)
        kwargs["persistent_context"] = True

    return kwargs


@contextmanager
def comment_profile_context(config: dict[str, Any]) -> Generator[dict[str, Any], None, None]:
    """Copy the persistent profile to a temp dir for the comment browser.

    Yields camoufox kwargs with user_data_dir pointing at the copy.
    The copy is deleted on exit.
    """
    kwargs = _camoufox_launch_kwargs(config)
    main_profile = _persistent_profile_dir(config)

    if not main_profile or not main_profile.exists():
        yield kwargs
        return

    tmp_profile = main_profile.parent / (main_profile.name + "_tmp_comment")
    try:
        if tmp_profile.exists():
            shutil.rmtree(str(tmp_profile), ignore_errors=True)
        log_event("Copying persistent profile for comment browser.")
        shutil.copytree(str(main_profile), str(tmp_profile))
        kwargs["user_data_dir"] = str(tmp_profile)
        yield kwargs
    finally:
        if tmp_profile.exists():
            shutil.rmtree(str(tmp_profile), ignore_errors=True)
            log_event("Removed temporary comment profile copy.")


def _load_comment_log(log_path: Path) -> dict[str, str]:
    """Load {group_id: last_commented_post_id} from disk."""
    if not log_path.exists():
        return {}
    try:
        raw = json.loads(log_path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return {str(k): str(v) for k, v in raw.items()}
    except Exception:
        pass
    return {}


def _save_comment_log(log_path: Path, data: dict[str, str]) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _get_latest_post_id(page: Any) -> str | None:
    """Find the ID of the top/first post on the current group page.

    Extracts from the first /posts/ or /permalink/ link in the feed.
    Returns the post ID string, or None if not found.
    """
    try:
        links = page.eval_on_selector_all(
            "a[href*='/posts/'], a[href*='/permalink/']",
            "(nodes) => nodes.map(n => n.href || '')",
        )
    except Exception:
        return None

    if not isinstance(links, list):
        return None

    for href in links:
        href = str(href or "").strip()
        match = POST_LINK_RE.search(href)
        if match:
            post_id = match.group(1).strip()
            if post_id and (post_id.isdigit() or re.match(r"[a-zA-Z0-9_-]+", post_id)):
                return post_id
    return None


def _find_comment_input(page: Any) -> Any | None:
    """Find the first visible comment input on the page."""
    selectors = [
        "div[contenteditable='true'][role='textbox'][aria-label*='comment' i]",
        "div[contenteditable='true'][role='textbox'][aria-placeholder*='comment' i]",
        "div[contenteditable='true'][role='textbox'][aria-label*='Leave a comment' i]",
        "div[contenteditable='true'][role='textbox'][aria-label*='Comment as' i]",
        "div[contenteditable='true'][role='textbox'][aria-label*='Tulis komentar' i]",
        "div[contenteditable='true'][role='textbox'][aria-label*='Tinggalkan komentar' i]",
    ]
    for selector in selectors:
        try:
            loc = page.locator(selector)
            count = loc.count()
            for i in range(min(count, 5)):
                try:
                    el = loc.nth(i)
                    if el.is_visible(timeout=500):
                        return el
                except Exception:
                    continue
        except Exception:
            continue
    return None


def _scroll_to_reveal_comments(page: Any) -> None:
    """Scroll down to make the first post's comment area accessible."""
    try:
        page.mouse.wheel(0, 500)
        page.wait_for_timeout(1000)
    except Exception:
        pass


def _collect_template_images(template: dict[str, Any]) -> list[str]:
    """Return resolved image paths from a template dict (mirrors scheduler logic)."""
    images: list[str] = []
    single = template.get("image")
    if isinstance(single, str) and single.strip():
        images.append(single.strip())
    image_list = template.get("images")
    if isinstance(image_list, list):
        for item in image_list:
            if isinstance(item, str) and item.strip():
                images.append(item.strip())
    seen: set[str] = set()
    unique: list[str] = []
    for img in images:
        if img not in seen:
            seen.add(img)
            unique.append(img)
    return unique


def _attach_comment_image(page: Any, image_paths: list[str]) -> bool:
    """Attach images to a comment by setting them directly on the hidden file
    input — same approach as autopost. No button click, no OS dialog."""
    if not image_paths:
        return True

    resolved: list[str] = []
    for p in image_paths:
        path = Path(p)
        if not path.exists():
            log_event(f"Comment image not found: {p}", level="WARNING")
            return False
        resolved.append(str(path.resolve()))

    photo_btn_selectors = [
        "div[aria-label='Photo/video'][role='button']",
        "div[aria-label='Foto/video'][role='button']",
        "div[aria-label*='Photo'][role='button']",
        "div[aria-label*='Foto'][role='button']",
        "div[aria-label*='photo' i][role='button']",
        "div[aria-label*='image' i][role='button']",
    ]

    # Two attempts: first try the hidden file input directly; if missing,
    # click the photo button once to reveal it, then try again.
    for attempt in range(2):
        try:
            file_input = page.locator("input[type='file']")
            if file_input.count() > 0:
                file_input.first.set_input_files(resolved, timeout=7000)
                page.wait_for_timeout(2500)
                return True
        except Exception:
            pass

        if attempt == 0:
            for sel in photo_btn_selectors:
                try:
                    btn = page.locator(sel)
                    if btn.count() > 0 and btn.first.is_visible(timeout=500):
                        btn.first.click(timeout=2500)
                        page.wait_for_timeout(1000)
                        break
                except Exception:
                    continue

    log_event("Could not attach image to comment.", level="WARNING")
    return False


def _do_comment(page: Any, text: str, image_paths: list[str] | None = None) -> bool:
    """Find the comment input for the top post and submit a comment.

    1. Click the comment box.
    2. insert_text the full template (fast, no per-char delay).
       Multi-line text is split on \\n; segments joined with Shift+Enter.
    3. If images: set_input_files on the hidden file input (same as autopost).
       Re-focus the box after the preview renders, then Enter to submit.
    """
    _scroll_to_reveal_comments(page)
    page.wait_for_timeout(600)

    comment_input = _find_comment_input(page)
    if comment_input is None:
        log_event("Comment input not found on page.", level="WARNING")
        return False

    try:
        comment_input.click(timeout=4000)
        page.wait_for_timeout(random.randint(400, 800))

        # Insert full text in one shot; handle newlines with Shift+Enter
        segments = text.split("\n")
        for i, seg in enumerate(segments):
            if seg:
                page.keyboard.insert_text(seg)
            if i < len(segments) - 1:
                page.keyboard.press("Shift+Enter")

        page.wait_for_timeout(random.randint(400, 700))

        # Attach images via set_input_files (no OS dialog, same as autopost)
        if image_paths:
            _attach_comment_image(page, image_paths)
            page.wait_for_timeout(random.randint(2000, 3000))
            comment_input.click(timeout=4000)
            page.wait_for_timeout(random.randint(300, 500))

        page.keyboard.press("Enter")
        page.wait_for_timeout(random.randint(1500, 3000))
        return True
    except Exception as exc:
        log_exception("Error while typing/submitting comment.", exc)
        return False


def _control_checkpoint(
    pause_event: threading.Event | None,
    stop_event: threading.Event | None,
) -> bool:
    """Returns False if should stop, handles pause by blocking until resumed."""
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
    """Sleep for `seconds` while checking for stop/pause every second.

    Returns False if stopped before the full duration elapsed.
    """
    remaining = max(0.0, float(seconds))
    while remaining > 0:
        if not _control_checkpoint(pause_event, stop_event):
            return False
        chunk = min(1.0, remaining)
        time.sleep(chunk)
        remaining -= chunk
    return _control_checkpoint(pause_event, stop_event)


def run_auto_commenter(
    config_path: Path,
    *,
    stop_event: threading.Event | None = None,
    pause_event: threading.Event | None = None,
) -> None:
    """Main auto commenter loop.

    Cycles through comment-enabled groups, comments on new posts,
    with a configurable delay between each group visit.
    The browser stays open for the entire session.
    """
    config = load_config(config_path)

    commenting_cfg = config.get("commenting", {})
    if not isinstance(commenting_cfg, dict):
        commenting_cfg = {}

    min_delay = int(commenting_cfg.get("min_delay_minutes", 1))
    max_delay = int(commenting_cfg.get("max_delay_minutes", 3))
    if min_delay < 0:
        min_delay = 1
    if max_delay < min_delay:
        max_delay = min_delay

    selected_template_file = str(commenting_cfg.get("template_file", "")).strip()

    groups_cfg = config.get("groups", {}) if isinstance(config.get("groups"), dict) else {}
    groups_source = str(groups_cfg.get("source", "")).strip()
    if not groups_source:
        log_event("No groups source configured.", level="ERROR")
        return

    groups_path = Path(groups_source)
    session_cfg = config.get("session", {}) if isinstance(config.get("session"), dict) else {}
    session_path_str = str(session_cfg.get("path", "data/session.json")).strip()
    session_path_obj = Path(session_path_str)

    all_groups: list[dict[str, Any]] = []
    if groups_path.exists():
        try:
            raw = json.loads(groups_path.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                all_groups = [g for g in raw if isinstance(g, dict)]
        except Exception:
            pass

    comment_groups = [g for g in all_groups if bool(g.get("comment_active", False))]
    if not comment_groups:
        log_event(
            "No groups enabled for Auto Comment. Toggle groups in the Auto Comment tab.",
            level="WARNING",
        )
        return

    from core.post_queue import load_templates as _load_templates
    all_templates = _load_templates(Path("templates"))
    template: dict[str, Any] | None = None
    if selected_template_file:
        for t in all_templates:
            if str(t.get("template_file", "")).strip() == selected_template_file:
                template = t
                break
        if template is None:
            log_event(
                f"Configured comment template '{selected_template_file}' not found — falling back to first available.",
                level="WARNING",
            )
    if template is None and all_templates:
        template = all_templates[0]

    if template is None:
        log_event("No comment templates found in templates/ folder.", level="ERROR")
        return

    actual_template_file = str(template.get("template_file", "(unknown)"))
    comment_text = str(template.get("text", "")).strip()
    comment_images = _collect_template_images(template)

    if not comment_text:
        log_event("Comment template has no text content.", level="ERROR")
        return

    account_root = groups_path.parent
    comment_log_path = account_root / "comment_log.json"
    comment_log = _load_comment_log(comment_log_path)

    log_event(
        "Auto commenter started.",
        context={
            "Template": actual_template_file,
            "groups": len(comment_groups),
            "images": len(comment_images),
            "delay_min_min": min_delay,
            "delay_max_min": max_delay,
        },
    )

    try:
        from camoufox.sync_api import Camoufox
    except Exception as exc:
        raise RuntimeError("Camoufox is not installed or import failed.") from exc

    try:
        with comment_profile_context(config) as launch_kwargs:
            with Camoufox(**launch_kwargs) as browser:
                page = browser.new_page()

                try:
                    session_data: dict[str, Any] = {}
                    if session_path_obj.exists():
                        try:
                            session_data = json.loads(session_path_obj.read_text(encoding="utf-8"))
                            if not isinstance(session_data, dict):
                                session_data = {}
                        except Exception:
                            session_data = {}

                    cookies = session_data.get("cookies", [])
                    if isinstance(cookies, list) and cookies:
                        try:
                            page.context.add_cookies(cookies)
                            log_event("Applied session cookies to comment browser.")
                        except Exception as exc:
                            log_exception("Failed to apply session cookies.", exc)

                    round_num = 0
                    while _control_checkpoint(pause_event, stop_event):
                        round_num += 1
                        log_event(f"Auto comment round {round_num} — {len(comment_groups)} group(s).")

                        for group in comment_groups:
                            if not _control_checkpoint(pause_event, stop_event):
                                break

                            group_id = str(group.get("id", "")).strip()
                            group_name = str(group.get("name", "Unknown Group"))
                            group_url = str(group.get("url", "")).strip()
                            if not group_url:
                                continue

                            log_event(
                                "Auto comment: visiting group.",
                                context={"group": group_name},
                            )

                            try:
                                page.goto(group_url, wait_until="domcontentloaded", timeout=30000)
                                page.wait_for_timeout(random.randint(2500, 4500))
                            except Exception as exc:
                                log_exception("Failed to navigate to group.", exc)
                                continue

                            post_id = _get_latest_post_id(page)

                            if not post_id:
                                log_event(
                                    "Auto comment: no post ID found on group page, skipping.",
                                    context={"group": group_name},
                                )
                                continue

                            if comment_log.get(group_id) == post_id:
                                log_event(
                                    "Auto comment: already commented on latest post, skipping.",
                                    context={"group": group_name, "post_id": post_id},
                                )
                                continue

                            log_event(
                                "Auto comment: commenting on new post.",
                                context={"group": group_name, "post_id": post_id},
                            )
                            success = _do_comment(page, comment_text, comment_images or None)
                            if success:
                                comment_log[group_id] = post_id
                                _save_comment_log(comment_log_path, comment_log)
                                log_event(
                                    "Auto comment: comment posted successfully.",
                                    context={"group": group_name, "post_id": post_id},
                                )
                            else:
                                log_event(
                                    "Auto comment: failed to post comment.",
                                    level="WARNING",
                                    context={"group": group_name},
                                )

                            # Delay only after attempting a comment (not when skipping)
                            if not _control_checkpoint(pause_event, stop_event):
                                break

                            delay_seconds = random.randint(min_delay * 60, max_delay * 60)
                            log_event(
                                f"Auto comment: waiting {delay_seconds // 60}m {delay_seconds % 60}s before next.",
                                context={"group": group_name},
                            )
                            if not _sleep_controlled(
                                delay_seconds,
                                pause_event=pause_event,
                                stop_event=stop_event,
                            ):
                                break

                        if stop_event is not None and stop_event.is_set():
                            break

                except Exception as inner_exc:
                    log_exception("Auto commenter inner error.", inner_exc)
                    raise

    except Exception as exc:
        log_exception("Auto commenter failed.", exc)
        raise

    log_event("Auto commenter stopped.")
