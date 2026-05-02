"""Tests for the local web API/session bridge."""

from __future__ import annotations

import threading
from pathlib import Path

from core import ApprovalCard, PlannedAction
from web_server import SessionManager, WebSession, WebSink, _card_to_dict


def test_card_serialization_keeps_safe_labels() -> None:
    card = ApprovalCard(
        actions=[
            PlannedAction(
                index=1,
                tool_name="copy_file",
                arguments={"source": "a.txt", "destination": "b.txt"},
            )
        ]
    )

    payload = _card_to_dict(card, generation=3)

    assert payload["generation"] == 3
    assert payload["summary"] == "1 file copy operation(s)"
    assert payload["actions"][0]["label"] == "COPY a.txt -> b.txt"


def test_web_sink_waits_for_approval() -> None:
    session = WebSession()
    sink = WebSink(session, timeout_sec=5)
    card = ApprovalCard(
        actions=[
            PlannedAction(index=1, tool_name="read_clipboard", arguments={}),
        ]
    )
    result: dict[str, str] = {}

    def worker() -> None:
        sink.plan(card)
        result["answer"] = sink.prompt_confirmation("Approve?")

    thread = threading.Thread(target=worker)
    thread.start()
    while not session.events:
        pass

    approval = session.events[-1]
    assert approval["kind"] == "approval"
    assert session.set_approval("yes", generation=approval["payload"]["card"]["generation"])
    thread.join(timeout=2)

    assert result["answer"] == "yes"


def test_session_manager_starts_turn_and_saves(
    tmp_path: Path,
    monkeypatch,
) -> None:
    manager = SessionManager()
    manager.store.sessions_dir = tmp_path
    session = manager.create_session()

    def fake_turn(messages: list[dict], user_text: str, sink: WebSink) -> None:
        messages.append({"role": "user", "content": user_text})
        sink.assistant("hello from web")

    monkeypatch.setattr("web_server.handle_user_turn", fake_turn)

    result = manager.start_turn(session, "hello")

    assert result["ok"] is True
    while session.busy:
        pass
    assert any(e["kind"] == "assistant" for e in session.events)
    assert list(tmp_path.glob("*.json"))
