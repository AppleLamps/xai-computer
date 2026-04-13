"""Tests for browser automation helpers."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import browser_tools
from config import set_dry_run


@pytest.fixture(autouse=True)
def _reset_browser_session() -> None:
    browser_tools._SESSION = None


class TestBrowserSession:
    def test_browser_navigate_uses_current_page(self, monkeypatch: pytest.MonkeyPatch) -> None:
        page = MagicMock()
        page.url = "https://example.com"
        monkeypatch.setattr(browser_tools, "_page", lambda: page)
        result = browser_tools.browser_navigate("https://example.com")
        assert result["ok"] is True
        page.goto.assert_called_once()

    def test_browser_extract_text_defaults_to_body(self, monkeypatch: pytest.MonkeyPatch) -> None:
        locator = MagicMock(inner_text=lambda: "hello")
        page = MagicMock(locator=lambda selector: locator)
        monkeypatch.setattr(browser_tools, "_page", lambda: page)
        result = browser_tools.browser_extract_text()
        assert result["ok"] is True
        assert result["text"] == "hello"
        page.wait_for_selector.assert_called_once_with("body", timeout=10000)

    def test_browser_fill_redacts_preview(self, monkeypatch: pytest.MonkeyPatch) -> None:
        page = MagicMock()
        monkeypatch.setattr(browser_tools, "_page", lambda: page)
        result = browser_tools.browser_fill("input", "token=sk-1234567890abcdefghijklmnop")
        assert result["ok"] is True
        assert "[REDACTED]" in result["text_preview"]

    def test_browser_download_saves_file(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(browser_tools, "_downloads_dir", lambda: tmp_path)

        class DownloadCtx:
            def __enter__(self) -> "DownloadCtx":
                self.value = SimpleNamespace(
                    suggested_filename="file.txt",
                    save_as=lambda p: Path(p).write_text("downloaded", encoding="utf-8"),
                )
                return self

            def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
                return False

        page = MagicMock()
        page.expect_download.return_value = DownloadCtx()
        monkeypatch.setattr(browser_tools, "_page", lambda: page)
        result = browser_tools.browser_download(click_selector="a.download")
        assert result["ok"] is True
        assert Path(result["path"]).exists()

    def test_browser_download_direct_url_only_navigates_once(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(browser_tools, "_downloads_dir", lambda: tmp_path)

        class DownloadCtx:
            def __enter__(self) -> "DownloadCtx":
                self.value = SimpleNamespace(
                    suggested_filename="file.txt",
                    save_as=lambda p: Path(p).write_text("downloaded", encoding="utf-8"),
                )
                return self

            def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
                return False

        page = MagicMock()
        page.expect_download.return_value = DownloadCtx()
        monkeypatch.setattr(browser_tools, "_page", lambda: page)
        result = browser_tools.browser_download(url="https://example.com/file.txt")
        assert result["ok"] is True
        assert page.goto.call_count == 1

    def test_browser_navigate_dry_run(self) -> None:
        set_dry_run(True)
        try:
            result = browser_tools.browser_navigate("https://example.com")
            assert result["ok"] is True
            assert result["dry_run"] is True
        finally:
            set_dry_run(False)
