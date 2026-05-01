"""Tests for GUI session persistence storage helpers."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from schemas import SYSTEM_PROMPT
from session_store import SessionStore


def test_save_creates_session_file(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "Hello there"},
    ]

    result = store.save_session(
        session_id="abc123",
        created="2026-01-01T00:00:00Z",
        messages=messages,
        token_totals={"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
    )

    assert result["ok"] is True
    data = json.loads((tmp_path / "abc123.json").read_text(encoding="utf-8"))
    assert data["title"] == "Hello there"
    assert data["token_totals"]["total_tokens"] == 3


def test_save_skips_empty_session(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    result = store.save_session(
        session_id="empty",
        created="2026-01-01T00:00:00Z",
        messages=[{"role": "system", "content": SYSTEM_PROMPT}],
        token_totals={},
    )

    assert result["ok"] is True
    assert result["skipped"] is True
    assert list(tmp_path.glob("*.json")) == []


def test_load_inserts_missing_system_prompt(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    path = tmp_path / "old.json"
    path.write_text(json.dumps({"messages": [{"role": "user", "content": "hi"}]}), encoding="utf-8")

    result = store.load_session(path)

    assert result["ok"] is True
    assert result["data"]["messages"][0]["role"] == "system"


def test_list_sessions_skips_corrupt_json(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "good.json").write_text(
        json.dumps({"id": "good", "messages": [{"role": "system", "content": SYSTEM_PROMPT}]}),
        encoding="utf-8",
    )

    sessions = store.list_sessions()

    assert [s["id"] for s in sessions] == ["good"]


def test_save_failure_is_reported(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "Hello"},
    ]

    with patch.object(Path, "write_text", side_effect=OSError("disk full")):
        result = store.save_session(
            session_id="abc123",
            created="2026-01-01T00:00:00Z",
            messages=messages,
            token_totals={},
        )

    assert result["ok"] is False
    assert "disk full" in result["error"]
