"""Windows desktop perception and actuation helpers."""

from __future__ import annotations

import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import get_allowed_roots, get_state_dir, is_dry_run
from logger import SESSION_ID, log_event
from safety import require_allowed_path_readonly
from shell_guard import redact_secrets

_BLOCKED_HOTKEYS: set[tuple[str, ...]] = {
    ("alt", "f4"),
    ("win", "r"),
    ("ctrl", "alt", "del"),
    ("win", "x"),
    ("win", "l"),
}


def _screenshots_dir() -> Path:
    path = get_state_dir() / "screenshots" / SESSION_ID
    path.mkdir(parents=True, exist_ok=True)
    return path


def _screenshot_path() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    return _screenshots_dir() / f"screenshot_{stamp}.png"


def _normalize_hotkey(keys: list[str]) -> tuple[str, ...]:
    return tuple(k.strip().casefold().replace("windows", "win") for k in keys if k.strip())


def classify_hotkey(keys: list[str]) -> dict[str, Any]:
    normalized = _normalize_hotkey(keys)
    if not normalized:
        return {"ok": False, "blocked": True, "error": "No hotkey keys provided.", "keys": []}
    if normalized in _BLOCKED_HOTKEYS:
        return {
            "ok": False,
            "blocked": True,
            "error": f"Hotkey blocked: {'+'.join(normalized)}",
            "keys": list(normalized),
        }
    return {"ok": True, "keys": list(normalized)}


def _load_mss() -> Any:
    try:
        from mss import mss
    except ImportError as e:  # pragma: no cover - exercised via error handling
        raise RuntimeError("mss is required for screenshots.") from e
    return mss


def _load_pillow() -> Any:
    try:
        from PIL import Image
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("Pillow is required for image processing.") from e
    return Image


def _load_ocr_engine() -> Any:
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("rapidocr-onnxruntime is required for OCR.") from e
    return RapidOCR


def _load_pyautogui() -> Any:
    try:
        import pyautogui
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("pyautogui is required for desktop input.") from e
    return pyautogui


def _write_capture_png(shot: Any, path: Path) -> None:
    from mss.tools import to_png

    to_png(shot.rgb, shot.size, output=str(path))


def _load_win32() -> tuple[Any, Any, Any, Any]:
    try:
        import win32con
        import win32gui
        import win32process
        import psutil
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("pywin32 and psutil are required for window management.") from e
    return win32con, win32gui, win32process, psutil


def _crop_region(path: Path, region: dict[str, int] | None) -> tuple[Path, bool]:
    if not region:
        return path, False
    Image = _load_pillow()
    with Image.open(path) as img:
        box = (
            int(region["x"]),
            int(region["y"]),
            int(region["x"] + region["width"]),
            int(region["y"] + region["height"]),
        )
        cropped = img.crop(box)
        with tempfile.NamedTemporaryFile(
            suffix=path.suffix or ".png",
            prefix=path.stem + "_crop_",
            dir=_screenshots_dir(),
            delete=False,
        ) as tmp:
            target = Path(tmp.name)
        cropped.save(target)
    return target, True


def _resolve_ocr_path(path: str) -> Path:
    roots = [_screenshots_dir(), get_state_dir() / "screenshots", get_state_dir(), *get_allowed_roots()]
    return require_allowed_path_readonly(Path(path), roots=roots)


def take_screenshot(region: dict[str, int] | None = None) -> dict[str, Any]:
    try:
        mss = _load_mss()
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}

    path = _screenshot_path()
    if is_dry_run():
        return {"ok": True, "dry_run": True, "path": str(path), "region": region}

    try:
        with mss() as sct:
            if region:
                monitor: dict[str, int] = {
                    "left": int(region["x"]),
                    "top": int(region["y"]),
                    "width": int(region["width"]),
                    "height": int(region["height"]),
                }
            else:
                monitors = getattr(sct, "monitors", None) or []
                if len(monitors) > 1:
                    monitor = monitors[1]
                elif monitors:
                    monitor = monitors[0]
                else:
                    return {"ok": False, "error": "No monitors available for screenshot capture."}
            shot = sct.grab(monitor)
            _write_capture_png(shot, path)
            width, height = shot.size
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"Failed to take screenshot: {e}"}

    log_event("take_screenshot", {"path": str(path), "width": width, "height": height}, phase="executed")
    return {"ok": True, "path": str(path), "width": width, "height": height}


def ocr_image(path: str, region: dict[str, int] | None = None) -> dict[str, Any]:
    try:
        fp = _resolve_ocr_path(path)
    except (OSError, ValueError, PermissionError) as e:
        return {"ok": False, "error": str(e)}

    if not fp.is_file():
        return {"ok": False, "error": f"Not a file: {fp}"}
    crop_path: Path | None = None
    try:
        crop_path, should_delete = _crop_region(fp, region)
        RapidOCR = _load_ocr_engine()
        engine = RapidOCR()
        result, _ = engine(str(crop_path))
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"Failed to OCR image: {e}"}
    finally:
        if crop_path is not None and 'should_delete' in locals() and should_delete:
            try:
                crop_path.unlink(missing_ok=True)
            except OSError:
                pass

    lines: list[dict[str, Any]] = []
    text_parts: list[str] = []
    for entry in result or []:
        if len(entry) < 3:
            continue
        box, text, score = entry
        lines.append({"text": text, "score": score, "box": box})
        text_parts.append(text)
    text = "\n".join(text_parts)
    log_event("ocr_image", {"path": str(fp), "lines": len(lines)}, phase="executed")
    return {"ok": True, "path": str(fp), "text": text, "lines": lines}


def list_windows() -> dict[str, Any]:
    try:
        win32con, win32gui, win32process, psutil = _load_win32()
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}

    foreground = win32gui.GetForegroundWindow()
    windows: list[dict[str, Any]] = []

    def callback(hwnd: int, _lparam: Any) -> bool:
        if not win32gui.IsWindowVisible(hwnd):
            return True
        if win32gui.GetWindow(hwnd, win32con.GW_OWNER):
            return True
        title = win32gui.GetWindowText(hwnd).strip()
        if not title:
            return True
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        if right <= left or bottom <= top:
            return True
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        try:
            process_name = psutil.Process(pid).name()
        except Exception:  # noqa: BLE001
            process_name = "unknown"
        windows.append(
            {
                "window_id": hwnd,
                "title": title,
                "process_name": process_name,
                "bounds": {"x": left, "y": top, "width": right - left, "height": bottom - top},
                "is_foreground": hwnd == foreground,
                "is_minimized": bool(win32gui.IsIconic(hwnd)),
            }
        )
        return True

    try:
        win32gui.EnumWindows(callback, None)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"Failed to enumerate windows: {e}"}

    return {"ok": True, "windows": windows, "count": len(windows)}


def get_active_window() -> dict[str, Any]:
    result = list_windows()
    if not result.get("ok"):
        return result
    active = next((w for w in result["windows"] if w.get("is_foreground")), None)
    if not active:
        return {"ok": False, "error": "No active window found."}
    return {"ok": True, "window": active}


def wait_for_window(title_query: str, timeout_sec: float = 10.0) -> dict[str, Any]:
    end = time.time() + max(0.1, timeout_sec)
    query = title_query.casefold()
    while time.time() < end:
        result = list_windows()
        if result.get("ok"):
            for window in result["windows"]:
                if query in window["title"].casefold():
                    return {"ok": True, "window": window}
        time.sleep(0.1)
    return {"ok": False, "error": f"No window matched '{title_query}' within {timeout_sec}s."}


def focus_window(window_id: int) -> dict[str, Any]:
    try:
        win32con, win32gui, _, _ = _load_win32()
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}

    if is_dry_run():
        return {"ok": True, "dry_run": True, "window_id": window_id}

    try:
        if win32gui.IsIconic(window_id):
            win32gui.ShowWindow(window_id, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(window_id)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"Failed to focus window {window_id}: {e}"}
    log_event("focus_window", {"window_id": window_id}, phase="executed")
    return {"ok": True, "window_id": window_id}


def move_mouse(x: int, y: int, duration_ms: int = 0) -> dict[str, Any]:
    if is_dry_run():
        return {"ok": True, "dry_run": True, "x": x, "y": y, "duration_ms": duration_ms}
    try:
        pyautogui = _load_pyautogui()
        pyautogui.moveTo(x, y, duration=max(0, duration_ms) / 1000)
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"Failed to move mouse: {e}"}
    log_event("move_mouse", {"x": x, "y": y, "duration_ms": duration_ms}, phase="executed")
    return {"ok": True, "x": x, "y": y, "duration_ms": duration_ms}


def click(x: int, y: int, button: str = "left", clicks: int = 1) -> dict[str, Any]:
    if is_dry_run():
        return {"ok": True, "dry_run": True, "x": x, "y": y, "button": button, "clicks": clicks}
    try:
        pyautogui = _load_pyautogui()
        pyautogui.moveTo(x, y)
        pyautogui.click(x=x, y=y, button=button, clicks=clicks)
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"Failed to click: {e}"}
    log_event("click", {"x": x, "y": y, "button": button, "clicks": clicks}, phase="executed")
    return {"ok": True, "x": x, "y": y, "button": button, "clicks": clicks}


def scroll(amount: int, x: int | None = None, y: int | None = None) -> dict[str, Any]:
    if is_dry_run():
        return {"ok": True, "dry_run": True, "amount": amount, "x": x, "y": y}
    try:
        pyautogui = _load_pyautogui()
        if x is not None and y is not None:
            pyautogui.moveTo(x, y)
        pyautogui.scroll(amount)
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"Failed to scroll: {e}"}
    log_event("scroll", {"amount": amount, "x": x, "y": y}, phase="executed")
    return {"ok": True, "amount": amount, "x": x, "y": y}


def type_text(text: str) -> dict[str, Any]:
    preview = redact_secrets(text)
    if is_dry_run():
        return {"ok": True, "dry_run": True, "text_preview": preview, "length": len(text)}
    try:
        pyautogui = _load_pyautogui()
        pyautogui.write(text)
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"Failed to type text: {e}"}
    log_event("type_text", {"text_preview": preview, "length": len(text)}, phase="executed")
    return {"ok": True, "text_preview": preview, "length": len(text)}


def press_hotkey(keys: list[str]) -> dict[str, Any]:
    verdict = classify_hotkey(keys)
    if not verdict.get("ok"):
        return verdict
    normalized = verdict["keys"]
    if is_dry_run():
        return {"ok": True, "dry_run": True, "keys": normalized}
    try:
        pyautogui = _load_pyautogui()
        pyautogui.hotkey(*normalized)
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"Failed to press hotkey: {e}"}
    log_event("press_hotkey", {"keys": normalized}, phase="executed")
    return {"ok": True, "keys": normalized}
