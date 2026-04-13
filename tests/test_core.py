"""Tests for the orchestration layer: ApprovalCard, plan building, conversation flow."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from core import (
    ApprovalCard,
    PlannedAction,
    _action_label,
    _action_risk,
    _process_tool_calls,
    _tool_progress_label,
    build_approval_card,
)
from xai_client import ToolCallSpec


class TestPlannedAction:
    def test_auto_label_move(self) -> None:
        a = PlannedAction(index=1, tool_name="move_file",
                          arguments={"source": "a.txt", "destination": "b/"})
        assert "MOVE" in a.label
        assert "a.txt" in a.label

    def test_auto_label_create_folder(self) -> None:
        a = PlannedAction(index=1, tool_name="create_folder",
                          arguments={"path": "/new"})
        assert "CREATE FOLDER" in a.label

    def test_risk_organize_is_medium(self) -> None:
        assert _action_risk("organize_folder") == "medium"
        assert _action_risk("organize_desktop_by_type") == "medium"

    def test_risk_move_is_low(self) -> None:
        assert _action_risk("move_file") == "low"
        assert _action_risk("create_folder") == "low"


class TestApprovalCard:
    def test_build_from_tool_calls(self) -> None:
        tcs = [
            ToolCallSpec(id="1", name="move_file", arguments={"source": "a.txt", "destination": "b/"}),
            ToolCallSpec(id="2", name="create_folder", arguments={"path": "/new"}),
        ]
        card = build_approval_card(tcs)
        assert len(card.actions) == 2
        assert "1 file operation" in card.summary
        assert "1 folder" in card.summary
        assert card.risk_level == "low"

    def test_medium_risk_propagates(self) -> None:
        tcs = [
            ToolCallSpec(id="1", name="organize_folder", arguments={"path": "/dir"}),
        ]
        card = build_approval_card(tcs)
        assert card.risk_level == "medium"

    def test_empty_card(self) -> None:
        card = build_approval_card([])
        assert len(card.actions) == 0
        assert card.summary == "action(s) pending"


class TestToolProgressLabel:
    def test_list_directory(self) -> None:
        label = _tool_progress_label("list_directory", {"path": "C:\\Users"})
        assert "Listing" in label
        assert "C:\\Users" in label

    def test_search_files(self) -> None:
        label = _tool_progress_label("search_files", {"path": "/dir", "query": "*.txt"})
        assert "Searching" in label
        assert "*.txt" in label

    def test_read_text_file(self) -> None:
        label = _tool_progress_label("read_text_file", {"path": "/file.txt"})
        assert "Reading" in label

    def test_unknown_tool(self) -> None:
        label = _tool_progress_label("some_future_tool", {"x": 1})
        assert "Running some_future_tool" in label


class TestProcessToolCallsConversation:
    """Tests that _process_tool_calls shows progress for read-only tools."""

    def _make_sink(self) -> MagicMock:
        sink = MagicMock()
        sink.info = MagicMock()
        sink.error = MagicMock()
        sink.assistant = MagicMock()
        sink.plan = MagicMock()
        sink.progress = MagicMock()
        sink.prompt_confirmation = MagicMock(return_value="yes")
        return sink

    @patch("core.dispatch_tool", return_value={"ok": True, "files": []})
    def test_readonly_tool_shows_progress(self, mock_dispatch: MagicMock) -> None:
        """Read-only tool calls should emit a progress message."""
        sink = self._make_sink()
        messages: list = []
        tool_calls = [
            ToolCallSpec(id="c1", name="list_directory", arguments={"path": "/test"}),
        ]
        _process_tool_calls(messages, tool_calls, sink)
        sink.progress.assert_called_once()
        label = sink.progress.call_args[0][0]
        assert "Listing" in label
        assert "/test" in label

    @patch("core.dispatch_tool", return_value={"ok": True, "files": []})
    def test_multiple_readonly_tools_show_progress(self, mock_dispatch: MagicMock) -> None:
        """Each read-only tool call should emit its own progress message."""
        sink = self._make_sink()
        messages: list = []
        tool_calls = [
            ToolCallSpec(id="c1", name="list_directory", arguments={"path": "/a"}),
            ToolCallSpec(id="c2", name="analyze_directory", arguments={"path": "/b"}),
        ]
        _process_tool_calls(messages, tool_calls, sink)
        assert sink.progress.call_count == 2

    @patch("core.dispatch_tool", return_value={"ok": True})
    def test_whitespace_only_content_not_rendered(self, mock_dispatch: MagicMock) -> None:
        """assistant_content that is only whitespace should not trigger rendering."""
        sink = self._make_sink()
        messages: list = []
        tool_calls = [
            ToolCallSpec(id="c1", name="list_directory", arguments={"path": "/x"}),
        ]
        # Pass whitespace-only content — _run_turn strips it before calling sink.assistant
        _process_tool_calls(messages, tool_calls, sink, assistant_content="   \n  ")
        # assistant() should NOT have been called by _process_tool_calls
        # (that responsibility is in _run_turn, not here)
        sink.assistant.assert_not_called()


# ---------------------------------------------------------------------------
# Partial batch retry
# ---------------------------------------------------------------------------


class TestPartialBatchRetry:
    """When a mutating block has failures, the user is offered a retry pass."""

    def _make_sink(self, confirm_answers: list[str] | None = None) -> MagicMock:
        sink = MagicMock()
        answers = iter(confirm_answers or [])
        sink.prompt_confirmation.side_effect = lambda _: next(answers, "cancel")
        return sink

    @patch("core.dispatch_tool")
    def test_no_retry_prompt_when_all_succeed(self, mock_dispatch: MagicMock) -> None:
        mock_dispatch.return_value = {"ok": True}
        sink = self._make_sink(["yes"])  # approve original block
        messages: list = []
        tool_calls = [
            ToolCallSpec(id="t1", name="move_file", arguments={"source": "a.txt", "destination": "b/"}),
        ]
        _process_tool_calls(messages, tool_calls, sink)
        # plan() called once; no retry = only one plan() call
        assert sink.plan.call_count == 1

    @patch("core.dispatch_tool")
    def test_retry_prompt_shown_on_failure(self, mock_dispatch: MagicMock) -> None:
        """When a move fails, the retry approval card should be shown."""
        mock_dispatch.return_value = {"ok": False, "error": "access denied"}
        sink = self._make_sink(["yes", "cancel"])  # approve original, cancel retry
        messages: list = []
        tool_calls = [
            ToolCallSpec(id="t1", name="move_file", arguments={"source": "a.txt", "destination": "b/"}),
        ]
        _process_tool_calls(messages, tool_calls, sink)
        # plan() called twice: original card + retry card
        assert sink.plan.call_count == 2

    @patch("core.dispatch_tool")
    def test_run_command_not_offered_for_retry(self, mock_dispatch: MagicMock) -> None:
        """run_command failures should NOT trigger the retry flow."""
        mock_dispatch.return_value = {"ok": False, "error": "exit 1"}
        sink = self._make_sink(["yes"])  # approve original only
        messages: list = []
        tool_calls = [
            ToolCallSpec(id="t1", name="run_command", arguments={"command": "bad_cmd"}),
        ]
        _process_tool_calls(messages, tool_calls, sink)
        # plan() called exactly once — no retry card for run_command
        assert sink.plan.call_count == 1

    @patch("core.dispatch_tool")
    def test_retry_updates_message_in_place(self, mock_dispatch: MagicMock) -> None:
        """After a successful retry, the tool-result message in history reflects success."""
        call_count = {"n": 0}

        def side_effect(name: str, args: dict) -> dict:
            call_count["n"] += 1
            return {"ok": True} if call_count["n"] > 1 else {"ok": False, "error": "transient"}

        mock_dispatch.side_effect = side_effect
        sink = self._make_sink(["yes", "yes"])  # approve original + approve retry
        messages: list = []
        tool_calls = [
            ToolCallSpec(id="tid1", name="move_file", arguments={"source": "a.txt", "destination": "b/"}),
        ]
        _process_tool_calls(messages, tool_calls, sink)
        # The tool-result message should now show success
        tool_msg = next(m for m in messages if m.get("role") == "tool" and m.get("tool_call_id") == "tid1")
        import json
        content = json.loads(tool_msg["content"])
        assert content.get("ok") is True

    @patch("core.dispatch_tool")
    def test_no_retry_when_user_declines_original(self, mock_dispatch: MagicMock) -> None:
        """If user cancels the original block, retry prompt should never appear."""
        mock_dispatch.return_value = {"ok": False, "error": "irrelevant"}
        sink = self._make_sink(["cancel"])  # decline original
        messages: list = []
        tool_calls = [
            ToolCallSpec(id="t1", name="move_file", arguments={"source": "x.txt", "destination": "y/"}),
        ]
        _process_tool_calls(messages, tool_calls, sink)
        # plan() called once for original; retry never offered
        assert sink.plan.call_count == 1
