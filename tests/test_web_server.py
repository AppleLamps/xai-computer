"""Tests for the local web API/session bridge."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from core import ApprovalCard, PlannedAction
from web_server import SessionManager, WebSession, WebSink, _card_to_dict, _validate_allowed_root_candidate


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


def test_web_sink_bypass_all_auto_approves() -> None:
    session = WebSession(bypass_approvals=True)
    sink = WebSink(session, timeout_sec=0.1)
    card = ApprovalCard(
        actions=[
            PlannedAction(index=1, tool_name="copy_file", arguments={"source": "a.txt", "destination": "b.txt"}),
        ]
    )

    sink.plan(card)

    assert sink.prompt_confirmation("Approve?") == "yes"
    assert any(e["kind"] == "auto_approved" for e in session.events)
    assert session.events[-1]["kind"] == "info"
    assert "BYPASS ALL is on" in session.events[-1]["payload"]["text"]


def test_web_sink_continuation_is_not_bypassed() -> None:
    session = WebSession(bypass_approvals=True)
    sink = WebSink(session, timeout_sec=5)
    card = ApprovalCard(
        actions=[
            PlannedAction(index=1, tool_name="continue_tool_loop", arguments={}),
        ],
        action_class="continuation",
        summary="Continue for up to 24 more model/tool round-trip(s)",
    )
    result: dict[str, str] = {}

    def worker() -> None:
        sink.plan(card)
        result["answer"] = sink.prompt_confirmation("Continue?")

    thread = threading.Thread(target=worker)
    thread.start()
    while not session.events:
        time.sleep(0.001)

    approval = session.events[-1]
    assert approval["kind"] == "approval"
    assert not any(e["kind"] == "auto_approved" for e in session.events)
    assert session.set_approval("yes", generation=approval["payload"]["card"]["generation"])
    thread.join(timeout=2)

    assert result["answer"] == "yes"


def test_bypass_all_is_session_scoped() -> None:
    assert WebSession(bypass_approvals=True).bypass_approvals is True
    assert WebSession().bypass_approvals is False


def test_stop_cancels_pending_approval() -> None:
    session = WebSession()
    sink = WebSink(session, timeout_sec=5)
    card = ApprovalCard(actions=[PlannedAction(index=1, tool_name="copy_file", arguments={})])
    result: dict[str, str] = {}

    def worker() -> None:
        sink.plan(card)
        result["answer"] = sink.prompt_confirmation("Approve?")

    thread = threading.Thread(target=worker)
    thread.start()
    while not session.events:
        time.sleep(0.001)

    assert session.request_stop() is False
    thread.join(timeout=2)

    assert result["answer"] == "cancel"
    assert session.stop_event.is_set()


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


def test_session_manager_stop_marks_done(
    tmp_path: Path,
    monkeypatch,
) -> None:
    manager = SessionManager()
    manager.store.sessions_dir = tmp_path
    session = manager.create_session()

    def fake_turn(messages: list[dict], user_text: str, sink: WebSink) -> None:
        while not sink.stop_event.is_set():
            time.sleep(0.001)

    monkeypatch.setattr("web_server.handle_user_turn", fake_turn)
    result = manager.start_turn(session, "wait")
    assert result["ok"] is True
    assert manager.stop_turn(session)["stopping"] is True
    while session.busy:
        time.sleep(0.001)

    done = [e for e in session.events if e["kind"] == "done"][-1]
    assert done["payload"]["canceled"] is True


def test_session_manager_restores_saved_session(tmp_path: Path) -> None:
    manager = SessionManager()
    manager.store.sessions_dir = tmp_path
    saved = manager.create_session()
    saved.messages.append({"role": "user", "content": "hello"})
    saved.messages.append({"role": "assistant", "content": "hi"})
    manager.save(saved)
    manager.sessions.clear()

    restored = manager.get_session(saved.session_id)

    assert restored.session_id == saved.session_id
    assert any(e["kind"] == "user" for e in restored.events)
    assert any(e["kind"] == "assistant" for e in restored.events)


def test_allowed_root_candidate_validation(tmp_path: Path) -> None:
    ok, value = _validate_allowed_root_candidate(str(tmp_path))
    assert ok is True
    assert value == tmp_path.resolve()

    ok, error = _validate_allowed_root_candidate(str(tmp_path / "missing"))
    assert ok is False
    assert "existing folder" in str(error)
