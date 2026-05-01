"""Tests for GUI session persistence (save, list, load round-trip)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

try:
    import tkinter as tk
    _root_probe = tk.Tk()
    _root_probe.withdraw()
    _root_probe.destroy()
    _TK_AVAILABLE = True
except Exception:
    _TK_AVAILABLE = False

pytestmark = pytest.mark.skipif(not _TK_AVAILABLE, reason="Tkinter unavailable")


@pytest.fixture
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("gui.get_state_dir", lambda: tmp_path)
    # Keep tool env sane during AssistantApp init.
    monkeypatch.setenv("XAI_API_KEY", "test-key")
    import gui  # imported lazily so the monkeypatch applies
    instance = gui.AssistantApp()
    instance.root.withdraw()
    yield instance
    try:
        instance.root.destroy()
    except Exception:
        pass


class TestSessionRoundTrip:
    def test_save_creates_file_with_expected_schema(self, app, tmp_path: Path) -> None:
        app._messages.append({"role": "user", "content": "Hello there"})
        app._messages.append({"role": "assistant", "content": "Hi!"})
        app._save_session()

        files = list((tmp_path / "sessions").glob("*.json"))
        assert len(files) == 1
        data = json.loads(files[0].read_text(encoding="utf-8"))
        assert data["id"] == app._session_id
        assert data["title"] == "Hello there"
        assert len(data["messages"]) == 3  # system + user + assistant

    def test_save_skipped_when_only_system_prompt(self, app, tmp_path: Path) -> None:
        app._save_session()
        assert list((tmp_path / "sessions").glob("*.json")) == []

    def test_title_truncated(self, app) -> None:
        long = "x" * 120
        app._messages.append({"role": "user", "content": long})
        assert len(app._session_title()) <= app._SESSION_TITLE_MAX

    def test_load_restores_messages_and_switches_id(self, app) -> None:
        app._messages.append({"role": "user", "content": "first turn"})
        app._messages.append({"role": "assistant", "content": "response"})
        app._save_session()
        original_id = app._session_id
        original_path = app._session_path(original_id)

        # Start a fresh session and append new messages.
        app._on_clear()
        assert app._session_id != original_id
        app._messages.append({"role": "user", "content": "new"})
        app._save_session()

        # Load the old one back.
        app._load_session(original_path)
        assert app._session_id == original_id
        assert any(
            m.get("role") == "user" and m.get("content") == "first turn"
            for m in app._messages
        )

    def test_list_sessions_sorted_by_mtime(self, app, tmp_path: Path) -> None:
        app._messages.append({"role": "user", "content": "A"})
        app._save_session()
        first = app._session_id

        app._on_clear()
        app._messages.append({"role": "user", "content": "B"})
        app._save_session()
        second = app._session_id

        ids = [e["id"] for e in app._list_sessions()]
        # Most recently saved first.
        assert ids[0] == second
        assert first in ids
