"""Playwright-backed browser automation helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config import get_state_dir, is_dry_run
from logger import SESSION_ID, log_event
from shell_guard import redact_secrets

_DEFAULT_TIMEOUT_MS = 10_000
_ALLOWED_URL_SCHEMES = ("http://", "https://")


def _downloads_dir() -> Path:
    path = get_state_dir() / "browser_downloads" / SESSION_ID
    path.mkdir(parents=True, exist_ok=True)
    return path


def _validate_http_url(url: str) -> str:
    cleaned = url.strip()
    if not cleaned:
        raise ValueError("URL is required.")
    if not cleaned.lower().startswith(_ALLOWED_URL_SCHEMES):
        raise ValueError("Only http(s) URLs are allowed.")
    return cleaned


@dataclass
class _BrowserSession:
    playwright: Any
    browser: Any
    context: Any
    page: Any


_SESSION: _BrowserSession | None = None


def _load_playwright() -> Any:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("playwright is required for browser automation.") from e
    return sync_playwright


def _ensure_browser_page() -> _BrowserSession:
    global _SESSION
    if _SESSION is not None:
        return _SESSION

    sync_playwright = _load_playwright()
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=False)
    context = browser.new_context(accept_downloads=True)
    page = context.new_page()
    _SESSION = _BrowserSession(playwright=pw, browser=browser, context=context, page=page)
    return _SESSION


def _page() -> Any:
    return _ensure_browser_page().page


def browser_navigate(url: str) -> dict[str, Any]:
    try:
        target_url = _validate_http_url(url)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    if is_dry_run():
        return {"ok": True, "dry_run": True, "url": target_url}
    try:
        page = _page()
        page.goto(target_url, wait_until="domcontentloaded")
        final_url = page.url
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"Failed to navigate: {e}"}
    log_event("browser_navigate", {"url": url, "final_url": final_url}, phase="executed")
    return {"ok": True, "url": final_url}


def browser_wait_for(selector: str, timeout_sec: float = 10.0) -> dict[str, Any]:
    try:
        page = _page()
        page.wait_for_selector(selector, timeout=max(100, int(timeout_sec * 1000)))
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"Failed waiting for selector: {e}"}
    return {"ok": True, "selector": selector, "timeout_sec": timeout_sec}


def browser_extract_text(selector: str | None = None, timeout_sec: float = 10.0) -> dict[str, Any]:
    target = selector or "body"
    try:
        page = _page()
        page.wait_for_selector(target, timeout=max(100, int(timeout_sec * 1000)))
        text = page.locator(target).inner_text()
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"Failed extracting text: {e}"}
    return {"ok": True, "selector": selector, "text": text}


def browser_click(selector: str, timeout_sec: float = 10.0) -> dict[str, Any]:
    if is_dry_run():
        return {"ok": True, "dry_run": True, "selector": selector}
    try:
        page = _page()
        page.click(selector, timeout=max(100, int(timeout_sec * 1000)))
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"Failed clicking selector: {e}"}
    log_event("browser_click", {"selector": selector}, phase="executed")
    return {"ok": True, "selector": selector}


def browser_fill(selector: str, text: str, timeout_sec: float = 10.0) -> dict[str, Any]:
    preview = redact_secrets(text)
    if is_dry_run():
        return {"ok": True, "dry_run": True, "selector": selector, "text_preview": preview}
    try:
        page = _page()
        page.fill(selector, text, timeout=max(100, int(timeout_sec * 1000)))
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"Failed filling selector: {e}"}
    log_event("browser_fill", {"selector": selector, "text_preview": preview}, phase="executed")
    return {"ok": True, "selector": selector, "text_preview": preview}


def browser_press(selector: str, key: str, timeout_sec: float = 10.0) -> dict[str, Any]:
    if is_dry_run():
        return {"ok": True, "dry_run": True, "selector": selector, "key": key}
    try:
        page = _page()
        page.press(selector, key, timeout=max(100, int(timeout_sec * 1000)))
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"Failed pressing key: {e}"}
    log_event("browser_press", {"selector": selector, "key": key}, phase="executed")
    return {"ok": True, "selector": selector, "key": key}


def browser_download(
    url: str | None = None,
    click_selector: str | None = None,
    timeout_sec: float = 10.0,
) -> dict[str, Any]:
    if not url and not click_selector:
        return {"ok": False, "error": "Provide url, click_selector, or both."}
    target_url: str | None = None
    if url is not None:
        try:
            target_url = _validate_http_url(url)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
    if is_dry_run():
        return {"ok": True, "dry_run": True, "url": target_url, "click_selector": click_selector}
    try:
        page = _page()
        timeout_ms = max(100, int(timeout_sec * 1000))
        if target_url and click_selector:
            page.goto(target_url, wait_until="domcontentloaded")
        with page.expect_download(timeout=timeout_ms) as download_info:
            if click_selector:
                page.click(click_selector, timeout=timeout_ms)
            elif target_url:
                page.goto(target_url, wait_until="domcontentloaded")
        download = download_info.value
        suggested = download.suggested_filename
        save_path = _downloads_dir() / suggested
        download.save_as(str(save_path))
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"Failed downloading file: {e}"}
    log_event("browser_download", {"path": str(save_path), "selector": click_selector, "url": target_url}, phase="executed")
    return {"ok": True, "path": str(save_path), "selector": click_selector, "url": target_url}
