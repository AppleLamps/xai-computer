"""Tests for desktop perception and input tools."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import desktop_tools
from config import set_dry_run


class _FakeMSS:
    monitors = [None, {"left": 0, "top": 0, "width": 64, "height": 32}]

    def __enter__(self) -> "_FakeMSS":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return False

    def grab(self, monitor: object) -> object:
        return SimpleNamespace(rgb=b"rgb", size=(64, 32))


class TestScreenshot:
    def test_take_screenshot_creates_png_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(desktop_tools, "_load_mss", lambda: _FakeMSS)
        monkeypatch.setattr(desktop_tools, "_write_capture_png", lambda shot, path: Path(path).write_bytes(b"png"))
        result = desktop_tools.take_screenshot()
        assert result["ok"] is True
        assert Path(result["path"]).exists()
        assert result["width"] == 64
        assert result["height"] == 32

    def test_get_screen_info_returns_monitors_and_cursor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class FakeScreenMSS:
            monitors = [
                {"left": 0, "top": 0, "width": 128, "height": 64},
                {"left": 0, "top": 0, "width": 128, "height": 64},
            ]

            def __enter__(self) -> "FakeScreenMSS":
                return self

            def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
                return False

        monkeypatch.setattr(desktop_tools, "_load_mss", lambda: FakeScreenMSS)
        monkeypatch.setattr(
            desktop_tools,
            "_load_pyautogui",
            lambda: SimpleNamespace(position=lambda: SimpleNamespace(x=12, y=34)),
        )
        result = desktop_tools.get_screen_info()
        assert result["ok"] is True
        assert result["monitor_count"] == 1
        assert result["cursor"] == {"x": 12, "y": 34}

    def test_window_screenshot_captures_window_bounds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        win32gui = SimpleNamespace(
            IsWindow=lambda hwnd: hwnd == 22,
            IsIconic=lambda hwnd: False,
            GetWindowRect=lambda hwnd: (10, 20, 110, 70),
        )
        monkeypatch.setattr(desktop_tools, "_load_win32", lambda: (None, win32gui, None, None))
        monkeypatch.setattr(
            desktop_tools,
            "take_screenshot",
            lambda region: {"ok": True, "path": "shot.png", "width": region["width"], "height": region["height"]},
        )
        result = desktop_tools.window_screenshot(22)
        assert result["ok"] is True
        assert result["window_id"] == 22
        assert result["region"] == {"x": 10, "y": 20, "width": 100, "height": 50}


class TestOCR:
    def test_ocr_image_parses_lines(self, sample_files: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        image = sample_files / "screen.png"
        image.write_bytes(b"fake")
        monkeypatch.setattr(desktop_tools, "get_allowed_roots", lambda: [sample_files])

        class FakeOCR:
            def __call__(self, path: str) -> tuple[list[tuple[list[list[int]], str, float]], None]:
                return [([[0, 0], [1, 0], [1, 1], [0, 1]], "hello", 0.99)], None

        monkeypatch.setattr(desktop_tools, "_load_ocr_engine", lambda: FakeOCR)
        result = desktop_tools.ocr_image(str(image))
        assert result["ok"] is True
        assert result["text"] == "hello"
        assert len(result["lines"]) == 1

    def test_ocr_crop_does_not_leave_sidecar_file(self, sample_files: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        image = sample_files / "screen.png"
        image.write_bytes(b"fake")
        monkeypatch.setattr(desktop_tools, "get_allowed_roots", lambda: [sample_files])

        class FakeImageCtx:
            def __enter__(self) -> "FakeImageCtx":
                return self

            def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
                return False

            def crop(self, box: tuple[int, int, int, int]) -> "FakeImageCtx":
                return self

            def save(self, target: Path) -> None:
                Path(target).write_bytes(b"crop")

        class FakeOCR:
            def __call__(self, path: str) -> tuple[list[tuple[list[list[int]], str, float]], None]:
                return [([[0, 0], [1, 0], [1, 1], [0, 1]], "hello", 0.99)], None

        monkeypatch.setattr(desktop_tools, "_load_pillow", lambda: SimpleNamespace(open=lambda p: FakeImageCtx()))
        monkeypatch.setattr(desktop_tools, "_load_ocr_engine", lambda: FakeOCR)
        result = desktop_tools.ocr_image(str(image), region={"x": 0, "y": 0, "width": 10, "height": 10})
        assert result["ok"] is True
        assert not any(p.name.startswith("screen_crop_") for p in desktop_tools._screenshots_dir().iterdir())


class TestWindowsAndInput:
    def test_list_windows_shape(self, monkeypatch: pytest.MonkeyPatch) -> None:
        win32con = SimpleNamespace(GW_OWNER=4)
        win32gui = SimpleNamespace(
            GetForegroundWindow=lambda: 10,
            EnumWindows=lambda cb, _: cb(10, None),
            IsWindowVisible=lambda hwnd: True,
            GetWindow=lambda hwnd, flag: 0,
            GetWindowText=lambda hwnd: "My App",
            GetWindowRect=lambda hwnd: (0, 0, 100, 100),
            IsIconic=lambda hwnd: False,
        )
        win32process = SimpleNamespace(GetWindowThreadProcessId=lambda hwnd: (1, 123))
        psutil = SimpleNamespace(Process=lambda pid: SimpleNamespace(name=lambda: "app.exe"))
        monkeypatch.setattr(desktop_tools, "_load_win32", lambda: (win32con, win32gui, win32process, psutil))
        result = desktop_tools.list_windows()
        assert result["ok"] is True
        assert result["windows"][0]["window_id"] == 10

    def test_type_text_dry_run(self) -> None:
        set_dry_run(True)
        try:
            result = desktop_tools.type_text("secret sk-1234567890abcdefghijklmnop")
            assert result["ok"] is True
            assert result["dry_run"] is True
            assert "[REDACTED]" in result["text_preview"]
        finally:
            set_dry_run(False)

    def test_press_hotkey_blocks_dangerous_combo(self) -> None:
        result = desktop_tools.press_hotkey(["alt", "f4"])
        assert result["ok"] is False
        assert result["blocked"] is True
