from __future__ import annotations

import json
import shutil
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from core.logger import log_event, log_exception
from core.config_loader import load_config


def _optional_import_camoufox() -> Any:
    try:
        from camoufox.sync_api import Camoufox
    except Exception as exc:
        raise RuntimeError(
            "Camoufox is not installed. Run: pip install -U -r requirements.txt and camoufox fetch"
        ) from exc
    return Camoufox


def _load_config(config_path: Path) -> dict:
    return load_config(config_path)


def _session_path_from_config(config: dict[str, Any]) -> Path:
    return Path(config["session"]["path"])


def _persistent_profile_dir_from_config(config: dict[str, Any]) -> Path:
    session_cfg = config.get("session", {})
    profile_dir = str(session_cfg.get("persistent_profile_dir", "data/camoufox_profile"))
    return Path(profile_dir)


# Firefox/Camoufox directories that are written to continuously while the
# browser runs.  Copying them while the browser is open causes race-condition
# errors on Windows ("The system cannot find the file specified").  They are
# pure caches and not needed for the copied profile to work.
_VOLATILE_PROFILE_DIRS: frozenset[str] = frozenset({
    "cache2", "startupCache", "thumbnails", "shader-cache",
    "OfflineCache", "jumpListCache",
})


def safe_copytree(src: Path, dst: Path) -> None:
    """Copy a browser profile directory while the browser may be open.

    Two protections:
    1. Skips volatile cache subdirectories that the running browser rewrites
       constantly (``cache2``, ``startupCache``, etc.).
    2. Uses a resilient per-file copy that silently ignores ``FileNotFoundError``
       and ``PermissionError`` — on Windows, cache entries can vanish between
       the directory listing and the actual copy when a concurrent browser
       process is writing to the same profile.
    """

    def _copy_file(s: str, d: str) -> None:
        try:
            shutil.copy2(s, d)
        except (FileNotFoundError, PermissionError, OSError):
            pass  # file deleted or locked by the running browser — skip it

    def _ignore_volatile(directory: str, contents: list[str]) -> set[str]:
        if Path(directory) == src:
            return _VOLATILE_PROFILE_DIRS & set(contents)
        return set()

    shutil.copytree(str(src), str(dst), copy_function=_copy_file, ignore=_ignore_volatile)


def clear_profile_locks(profile_dir: Path) -> None:
    """Remove Firefox/Camoufox stale lock files left behind by crashed sessions.

    Firefox writes ``lock`` (symlink on POSIX), ``.parentlock``, and
    ``parent.lock`` into the profile when a browser instance starts.  If the
    process is killed or crashes those files are never cleaned up, causing the
    *next* launch to stall until the 180-second timeout fires.  Deleting them
    before every launch is safe — a running instance re-creates them
    immediately.
    """
    for name in ("lock", ".parentlock", "parent.lock"):
        target = profile_dir / name
        try:
            if target.is_symlink() or target.exists():
                target.unlink(missing_ok=True)
                log_event(f"Removed stale browser lock: {name}", level="DEBUG")
        except Exception:
            pass


def camoufox_kwargs(config: dict[str, Any]) -> dict[str, Any]:
    browser_cfg = config.get("browser", {})
    camoufox_cfg = config.get("camoufox", {})
    profile_dir = _persistent_profile_dir_from_config(config)
    profile_dir.mkdir(parents=True, exist_ok=True)

    kwargs: dict[str, Any] = {
        "headless": bool(browser_cfg.get("headless", False)),
        "humanize": bool(browser_cfg.get("humanize", True)),
        "locale": browser_cfg.get("locale", "en-US"),
        "geoip": camoufox_cfg.get("geoip", False),
        "os": camoufox_cfg.get("os", "windows"),
        "persistent_context": True,
        "user_data_dir": str(profile_dir),
    }

    max_width = int(browser_cfg.get("screen_max_width", 0) or 0)
    max_height = int(browser_cfg.get("screen_max_height", 0) or 0)
    if max_width > 0 and max_height > 0:
        try:
            from browserforge.fingerprints import Screen

            kwargs["screen"] = Screen(max_width=max_width, max_height=max_height)
        except Exception as exc:
            log_exception("Failed to set Camoufox screen constraint.", exc)

    window_width = int(browser_cfg.get("window_width", 0) or 0)
    window_height = int(browser_cfg.get("window_height", 0) or 0)
    if window_width > 0 and window_height > 0:
        kwargs["window"] = (window_width, window_height)

    proxy_cfg = camoufox_cfg.get("proxy", {})
    if isinstance(proxy_cfg, dict) and proxy_cfg.get("server"):
        kwargs["proxy"] = {
            "server": str(proxy_cfg["server"]),
            "username": str(proxy_cfg.get("username", "")),
            "password": str(proxy_cfg.get("password", "")),
        }

    return kwargs


@contextmanager
def scrape_profile_context(config: dict[str, Any]):
    """Context manager that provides a temporary copy of the posting profile.

    Before yielding, copies the main persistent profile to a sibling temp
    directory so the scrape/validation browser can open it while the posting
    browser still holds the lock on the original. The copy is unconditionally
    deleted in the finally block — whether the scrape succeeds or crashes.

    Yields camoufox kwargs with ``user_data_dir`` pointing at the temp copy.
    If the main profile does not exist yet, yields the regular kwargs unchanged
    (first-run case where no profile has been created).
    """
    main_profile = _persistent_profile_dir_from_config(config)
    kwargs = camoufox_kwargs(config)

    if not main_profile.exists():
        # No profile yet — let the browser create a fresh one normally.
        yield kwargs
        return

    tmp_profile = main_profile.parent / (main_profile.name + "_tmp_scrape")
    try:
        if tmp_profile.exists():
            shutil.rmtree(str(tmp_profile), ignore_errors=True)
        log_event("Copying persistent profile for scrape browser.", context={"source": str(main_profile), "dest": str(tmp_profile)})
        safe_copytree(main_profile, tmp_profile)
        kwargs["user_data_dir"] = str(tmp_profile)
        yield kwargs
    finally:
        if tmp_profile.exists():
            shutil.rmtree(str(tmp_profile), ignore_errors=True)
            log_event("Removed temporary scrape profile copy.")


def _home_url(config: dict[str, Any]) -> str:
    return "https://www.facebook.com/"


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        return {}
    return {}


def _normalize_same_site(value: Any) -> str | None:
    raw = str(value).strip().lower()
    if raw in {"lax"}:
        return "Lax"
    if raw in {"strict"}:
        return "Strict"
    if raw in {"none", "no_restriction", "no-restriction"}:
        return "None"
    return None


def _normalize_cookie_export(cookies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue

        name = str(cookie.get("name", "")).strip()
        value = str(cookie.get("value", ""))
        domain = str(cookie.get("domain", "")).strip()
        path = str(cookie.get("path", "/") or "/")
        if not name or not domain:
            continue

        item: dict[str, Any] = {
            "name": name,
            "value": value,
            "domain": domain,
            "path": path,
            "httpOnly": bool(cookie.get("httpOnly", False)),
            "secure": bool(cookie.get("secure", True)),
        }

        same_site = _normalize_same_site(cookie.get("sameSite"))
        if same_site:
            item["sameSite"] = same_site

        expiration = cookie.get("expirationDate")
        if isinstance(expiration, (int, float)):
            item["expires"] = int(expiration)

        normalized.append(item)

    return normalized


def load_session_data(session_path: Path) -> dict[str, Any]:
    if not session_path.exists():
        return {}

    try:
        raw_text = session_path.read_text(encoding="utf-8")
        parsed = json.loads(raw_text)
    except Exception:
        return {}

    # Preferred internal format: {"cookies": [...]}.
    if isinstance(parsed, dict):
        cookies = parsed.get("cookies", [])
        if isinstance(cookies, list):
            return {"cookies": _normalize_cookie_export([c for c in cookies if isinstance(c, dict)])}
        return {}

    # Browser export fallback format: [{cookie}, {cookie}, ...].
    if isinstance(parsed, list):
        return {"cookies": _normalize_cookie_export([c for c in parsed if isinstance(c, dict)])}

    return {}


def _try_import_cookie_export(session_path: Path) -> bool:
    candidates = sorted(session_path.parent.parent.glob("www.facebook.com_json_*.json"))
    if not candidates:
        return False

    latest = candidates[-1]
    data = load_session_data(latest)
    cookies = data.get("cookies", [])
    if not isinstance(cookies, list) or not cookies:
        return False

    session_path.parent.mkdir(parents=True, exist_ok=True)
    with session_path.open("w", encoding="utf-8") as f:
        json.dump({"cookies": cookies}, f, ensure_ascii=False, indent=2)

    log_event(
        "Imported cookies from exported browser session file.",
        context={"source": str(latest), "target": str(session_path), "cookies": len(cookies)},
    )
    return True


def apply_session_cookies(page: Any, session_data: dict[str, Any]) -> None:
    cookies = session_data.get("cookies", [])
    if not isinstance(cookies, list) or not cookies:
        return
    try:
        page.context.add_cookies(cookies)
    except Exception as exc:
        log_exception("Failed to apply saved cookies.", exc)


def should_apply_cookie_overlay(config: dict[str, Any]) -> bool:
    session_cfg = config.get("session", {})
    return bool(session_cfg.get("apply_cookie_overlay", False))


def maybe_apply_session_cookies(page: Any, session_data: dict[str, Any], config: dict[str, Any]) -> None:
    if not should_apply_cookie_overlay(config):
        return
    apply_session_cookies(page, session_data)


def save_session_from_page(page: Any, session_path: Path) -> None:
    session_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "cookies": page.context.cookies(),
    }
    with session_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def configure_page_window(page: Any, config: dict[str, Any]) -> None:
    browser_cfg = config.get("browser", {})
    fullscreen = bool(browser_cfg.get("fullscreen", False))
    if not fullscreen:
        return

    # Trigger native browser fullscreen mode once after page creation.
    try:
        page.bring_to_front()
    except Exception:
        pass

    try:
        page.keyboard.press("F11")
        page.wait_for_timeout(250)
    except Exception as exc:
        log_exception("Failed to apply fullscreen mode.", exc)


def get_or_create_page(browser_or_context: Any) -> Any:
    """Reuse existing page in persistent context to avoid extra windows/tabs."""
    try:
        pages = list(getattr(browser_or_context, "pages", []))
    except Exception:
        pages = []

    if pages:
        page = pages[0]
        # Keep one working page for deterministic automation behavior.
        for extra in pages[1:]:
            try:
                extra.close()
            except Exception:
                continue
        return page

    return browser_or_context.new_page()


def is_logged_in(page: Any) -> bool:
    url = page.url.lower()
    if "/login" in url:
        return False

    # Facebook home/feed with authenticated context usually resolves to these paths.
    return "facebook.com" in url


def ensure_logged_in_in_page(page: Any, config: dict[str, Any], session_path: Path) -> None:
    home_url = _home_url(config)
    auto_relogin = bool(config.get("session", {}).get("auto_relogin", True))

    page.goto(home_url, wait_until="domcontentloaded")
    page.wait_for_timeout(1200)

    if is_logged_in(page):
        save_session_from_page(page, session_path)
        return

    if not auto_relogin:
        raise RuntimeError("Session invalid and auto relogin is disabled.")

    print("Session invalid. Please login in this opened browser window, then press Enter to continue...")
    input()
    page.wait_for_timeout(1000)
    if not is_logged_in(page):
        raise RuntimeError("Login appears incomplete. Please finish login and retry.")

    save_session_from_page(page, session_path)
    log_event("Session refreshed successfully in current browser context.")


def ensure_session(
    config_path: Path,
    *,
    force_relogin: bool = False,
    validate_existing: bool = True,
) -> Path:
    config = _load_config(config_path)
    session_path = _session_path_from_config(config)
    session_path.parent.mkdir(parents=True, exist_ok=True)

    Camoufox = _optional_import_camoufox()
    kwargs = camoufox_kwargs(config)
    home_url = _home_url(config)

    if session_path.exists() and not force_relogin:
        if not validate_existing:
            log_event("Reusing existing session without pre-validation.")
            return session_path
        if validate_session(config_path):
            log_event("Session is valid and ready.")
            return session_path
        log_event("Existing session is invalid; relogin is required.", level="WARNING")

    if not session_path.exists() and not force_relogin:
        imported = _try_import_cookie_export(session_path)
        if imported:
            if not validate_existing:
                log_event("Imported browser session found; deferring validation to active browser flow.")
                return session_path
            if validate_session(config_path):
                log_event("Imported browser session is valid and ready.")
                return session_path

    if not session_path.exists() and not force_relogin and not validate_existing:
        log_event("No local session file yet; deferring login validation to active browser flow.")
        return session_path

    log_event("Launching Camoufox for manual login setup.")
    clear_profile_locks(Path(str(kwargs.get("user_data_dir", ""))))
    with Camoufox(**kwargs) as browser:
        page = get_or_create_page(browser)
        configure_page_window(page, config)
        page.goto(home_url, wait_until="domcontentloaded")
        print("Complete login in the opened browser window, then press Enter here to continue...")
        input()

        page.wait_for_timeout(1000)
        if not is_logged_in(page):
            raise RuntimeError("Login appears incomplete. Please finish login and run --setup again.")

        save_session_from_page(page, session_path)
        log_event("Session saved successfully.")

    return session_path


def validate_session(config_path: Path) -> bool:
    config = _load_config(config_path)
    session_path = _session_path_from_config(config)

    Camoufox = _optional_import_camoufox()
    home_url = _home_url(config)

    try:
        with scrape_profile_context(config) as kwargs:
            clear_profile_locks(Path(str(kwargs.get("user_data_dir", ""))))
            with Camoufox(**kwargs) as browser:
                page = get_or_create_page(browser)
                configure_page_window(page, config)
                page.goto(home_url, wait_until="domcontentloaded")
                page.wait_for_timeout(1500)
                valid = is_logged_in(page)
                if valid:
                    save_session_from_page(page, session_path)
                return valid
    except Exception as exc:
        log_exception("Session validation failed.", exc)
        return False

