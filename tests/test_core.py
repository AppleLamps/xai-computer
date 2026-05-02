"""Tests for the orchestration layer: ApprovalCard, plan building, conversation flow."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from core import (
    ApprovalCard,
    PlannedAction,
    _action_label,
    _action_risk,
    _claims_clipboard_write,
    _format_execution_summary,
    _process_tool_calls,
    _run_turn,
    _requested_clipboard_write,
    _successful_tool_since,
    _tool_progress_label,
    build_approval_card,
)
from schemas import MUTATING_TOOL_NAMES
from xai_client import ChatCompletionResult, ToolCallSpec


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

    def test_risk_desktop_click_is_high(self) -> None:
        assert _action_risk("click", {"x": 1, "y": 2}) == "high"

    def test_risk_start_process_is_high(self) -> None:
        assert _action_risk("start_process", {"executable": "notepad.exe"}) == "high"

    def test_new_tool_risks_and_labels(self) -> None:
        assert _action_risk("copy_file", {"overwrite": False}) == "low"
        assert _action_risk("copy_file", {"overwrite": True}) == "medium"
        assert _action_risk("delete_file_to_recycle_bin", {"path": "a.txt"}) == "medium"
        assert _action_risk("read_clipboard", {}) == "medium"
        assert _action_risk("window_screenshot", {"window_id": 1}) == "medium"
        assert _action_risk("browser_screenshot", {}) == "medium"
        assert "COPY" in _action_label("copy_file", {"source": "a", "destination": "b"})
        assert "RECYCLE" in _action_label("delete_file_to_recycle_bin", {"path": "a"})

    def test_mutating_membership_for_sensitive_new_tools(self) -> None:
        assert "copy_file" in MUTATING_TOOL_NAMES
        assert "delete_file_to_recycle_bin" in MUTATING_TOOL_NAMES
        assert "read_clipboard" in MUTATING_TOOL_NAMES
        assert "window_screenshot" in MUTATING_TOOL_NAMES
        assert "browser_screenshot" in MUTATING_TOOL_NAMES
        assert "copy_to_clipboard" not in MUTATING_TOOL_NAMES


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

    def test_browser_actions_batch_summary(self) -> None:
        tcs = [
            ToolCallSpec(id="1", name="browser_click", arguments={"selector": "#go"}),
            ToolCallSpec(id="2", name="browser_fill", arguments={"selector": "#q", "text": "hello"}),
        ]
        card = build_approval_card(tcs)
        assert card.action_class == "browser_control"
        assert "browser action" in card.summary
        assert card.risk_level == "high"

    def test_sensitive_read_summary(self) -> None:
        card = build_approval_card([ToolCallSpec(id="1", name="read_clipboard", arguments={})])
        assert card.action_class == "sensitive_read"
        assert "sensitive read" in card.summary
        assert card.risk_level == "medium"


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

    def test_screenshot_label(self) -> None:
        label = _tool_progress_label("take_screenshot", {})
        assert "Capturing screenshot" in label

    def test_new_readonly_tool_labels(self) -> None:
        assert "Inspecting file info" in _tool_progress_label("get_file_info", {"path": "/x"})
        assert "recursively" in _tool_progress_label("recursive_find_files", {"path": "/x"})
        assert "file contents" in _tool_progress_label("search_file_contents", {"path": "/x"})


class TestExecutionSummary:
    def test_failed_action_summary_is_plain_and_specific(self) -> None:
        block = [ToolCallSpec(id="c1", name="run_command", arguments={"command": "bad"})]
        summary = _format_execution_summary(
            block,
            {"c1": {"ok": False, "error": "working directory outside allowed roots"}},
        )
        assert summary.startswith("Completed 0/1 operation(s).")
        assert "1 failed" in summary
        assert "working directory outside allowed roots" in summary

    def test_success_summary_is_concise(self) -> None:
        block = [ToolCallSpec(id="m1", name="move_file", arguments={})]
        assert _format_execution_summary(block, {"m1": {"ok": True}}) == (
            "Completed 1/1 operation(s)."
        )


class TestClipboardClaimGuard:
    def test_detects_clipboard_requests_and_claims(self) -> None:
        assert _requested_clipboard_write("Copy the summary to my clipboard.")
        assert not _requested_clipboard_write("Read my clipboard.")
        assert _claims_clipboard_write("Copying this to your clipboard now.")
        assert _claims_clipboard_write("Copied to your clipboard.")
        assert not _claims_clipboard_write("I cannot access the clipboard.")

    def test_successful_tool_since_detects_copy_result(self) -> None:
        messages = [
            {"role": "user", "content": "copy it"},
            {
                "role": "assistant",
                "content": "doing it",
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "copy_to_clipboard", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": '{"ok": true}'},
        ]
        assert _successful_tool_since(messages, 0, "copy_to_clipboard")


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

    def test_blocked_run_command_skips_approval(self) -> None:
        """Permanently blocked shell commands should never render an approval card."""
        sink = self._make_sink()
        messages: list = []
        tool_calls = [
            ToolCallSpec(id="c1", name="run_command", arguments={"command": "rm -rf /"}),
        ]
        _process_tool_calls(messages, tool_calls, sink)
        sink.plan.assert_not_called()
        sink.prompt_confirmation.assert_not_called()
        tool_msg = next(m for m in messages if m.get("role") == "tool" and m.get("tool_call_id") == "c1")
        import json
        content = json.loads(tool_msg["content"])
        assert content.get("blocked") is True

    @patch("core.dispatch_tool")
    def test_blocked_run_command_splits_mutating_batch(self, mock_dispatch: MagicMock) -> None:
        """A blocked shell command should not be bundled into adjacent approval blocks."""
        def side_effect(name: str, args: dict) -> dict:
            if name == "run_command":
                return {"ok": False, "blocked": True, "error": "Command blocked"}
            return {"ok": True}

        mock_dispatch.side_effect = side_effect
        sink = self._make_sink()
        sink.prompt_confirmation.side_effect = ["yes", "yes"]
        messages: list = []
        tool_calls = [
            ToolCallSpec(id="m1", name="move_file", arguments={"source": "a.txt", "destination": "b.txt"}),
            ToolCallSpec(id="c1", name="run_command", arguments={"command": "rm -rf /"}),
            ToolCallSpec(id="f1", name="create_folder", arguments={"path": "/new"}),
        ]
        _process_tool_calls(messages, tool_calls, sink)
        assert sink.plan.call_count == 2
        assert sink.prompt_confirmation.call_count == 2

    @patch("core.dispatch_tool", return_value={"ok": True})
    def test_batches_by_action_class(self, mock_dispatch: MagicMock) -> None:
        sink = self._make_sink()
        sink.prompt_confirmation.side_effect = ["yes", "yes"]
        messages: list = []
        tool_calls = [
            ToolCallSpec(id="b1", name="browser_navigate", arguments={"url": "https://example.com"}),
            ToolCallSpec(id="b2", name="browser_click", arguments={"selector": "#go"}),
            ToolCallSpec(id="f1", name="append_file", arguments={"path": "C:/tmp/out.txt", "content": "x"}),
        ]
        _process_tool_calls(messages, tool_calls, sink)
        assert sink.plan.call_count == 2

    @patch("core.dispatch_tool", return_value={"ok": True})
    def test_sensitive_new_tools_request_approval(self, mock_dispatch: MagicMock) -> None:
        sink = self._make_sink()
        messages: list = []
        tool_calls = [
            ToolCallSpec(id="clip", name="read_clipboard", arguments={"max_chars": 100}),
            ToolCallSpec(id="shot", name="window_screenshot", arguments={"window_id": 5}),
        ]
        _process_tool_calls(messages, tool_calls, sink)
        sink.plan.assert_called_once()
        card = sink.plan.call_args[0][0]
        assert isinstance(card, ApprovalCard)
        assert card.action_class == "sensitive_read"

    @patch("core.dispatch_tool", return_value={"ok": True})
    def test_copy_to_clipboard_does_not_request_approval(self, mock_dispatch: MagicMock) -> None:
        sink = self._make_sink()
        messages: list = []
        tool_calls = [ToolCallSpec(id="copy", name="copy_to_clipboard", arguments={"text": "hello"})]
        _process_tool_calls(messages, tool_calls, sink)
        sink.plan.assert_not_called()
        sink.progress.assert_called_once()

    def test_blocked_hotkey_skips_approval(self) -> None:
        sink = self._make_sink()
        messages: list = []
        tool_calls = [
            ToolCallSpec(id="hk1", name="press_hotkey", arguments={"keys": ["alt", "f4"]}),
        ]
        _process_tool_calls(messages, tool_calls, sink)
        sink.plan.assert_not_called()
        sink.prompt_confirmation.assert_not_called()
        tool_msg = next(m for m in messages if m.get("role") == "tool" and m.get("tool_call_id") == "hk1")
        import json
        content = json.loads(tool_msg["content"])
        assert content.get("blocked") is True


class TestToolActivityHooks:
    """tool_start / tool_end must fire once per dispatch call on both paths."""

    def _make_sink(self, with_hooks: bool = True) -> MagicMock:
        # `spec` limits attribute access so missing hooks truly appear absent.
        attrs = ["info", "error", "assistant", "plan", "progress",
                 "prompt_confirmation"]
        if with_hooks:
            attrs += ["tool_start", "tool_end"]
        sink = MagicMock(spec=attrs)
        sink.prompt_confirmation.return_value = "yes"
        return sink

    @patch("core.dispatch_tool", return_value={"ok": True, "files": []})
    def test_readonly_hooks_fire(self, _mock_dispatch: MagicMock) -> None:
        sink = self._make_sink()
        tool_calls = [
            ToolCallSpec(id="c1", name="list_directory", arguments={"path": "/x"}),
        ]
        _process_tool_calls([], tool_calls, sink)
        sink.tool_start.assert_called_once()
        sink.tool_end.assert_called_once()
        assert sink.tool_start.call_args[0][0] == "list_directory"
        assert sink.tool_end.call_args[0] == ("list_directory", True)

    @patch("core.dispatch_tool", return_value={"ok": True})
    def test_mutating_hooks_fire(self, _mock_dispatch: MagicMock) -> None:
        sink = self._make_sink()
        tool_calls = [
            ToolCallSpec(
                id="m1", name="move_file",
                arguments={"source": "a.txt", "destination": "b.txt"},
            ),
        ]
        _process_tool_calls([], tool_calls, sink)
        sink.tool_start.assert_called_once()
        sink.tool_end.assert_called_once()
        assert sink.tool_end.call_args[0] == ("move_file", True)

    @patch("core.dispatch_tool", return_value={"ok": False, "error": "x"})
    def test_tool_end_reports_failure(self, _mock_dispatch: MagicMock) -> None:
        sink = self._make_sink()
        tool_calls = [
            ToolCallSpec(id="c1", name="list_directory", arguments={"path": "/x"}),
        ]
        _process_tool_calls([], tool_calls, sink)
        assert sink.tool_end.call_args[0] == ("list_directory", False)

    @patch("core.dispatch_tool", return_value={"ok": True, "files": []})
    def test_sink_without_hooks_still_works(self, _mock_dispatch: MagicMock) -> None:
        """Duck-typed: a sink lacking tool_start/tool_end must not break dispatch."""
        sink = self._make_sink(with_hooks=False)
        tool_calls = [
            ToolCallSpec(id="c1", name="list_directory", arguments={"path": "/x"}),
        ]
        # Should complete without raising AttributeError.
        _process_tool_calls([], tool_calls, sink)
        sink.progress.assert_called_once()


class TestRunTurnRollback:
    def _make_sink(self) -> MagicMock:
        sink = MagicMock()
        sink.info = MagicMock()
        sink.error = MagicMock()
        sink.assistant = MagicMock()
        sink.plan = MagicMock()
        sink.progress = MagicMock()
        sink.prompt_confirmation = MagicMock(return_value="yes")
        return sink

    @patch("core.dispatch_tool", return_value={"ok": True})
    @patch("core.get_tool_definitions", return_value=[])
    @patch("core.get_xai_model", return_value="test-model")
    @patch("core.get_xai_api_key", return_value="test-key")
    @patch("core.get_max_tool_loops", return_value=3)
    @patch("core._chat_with_optional_web_tools")
    def test_api_failure_rolls_back_partial_turn(
        self,
        mock_chat: MagicMock,
        mock_loops: MagicMock,
        mock_key: MagicMock,
        mock_model: MagicMock,
        mock_tools: MagicMock,
        mock_dispatch: MagicMock,
    ) -> None:
        """A failed turn should discard the user message and any partial tool history."""
        first = ChatCompletionResult(
            message_role="assistant",
            content="I'll inspect that.",
            tool_calls=[ToolCallSpec(id="c1", name="list_directory", arguments={"path": "/tmp"})],
            raw={},
        )

        def side_effect(*args: object, **kwargs: object) -> ChatCompletionResult:
            if mock_chat.call_count == 1:
                return first
            raise RuntimeError("network down")

        mock_chat.side_effect = side_effect
        sink = self._make_sink()
        messages = [{"role": "assistant", "content": "previous"}]

        _run_turn(messages, "show me a folder", sink)

        assert messages == [{"role": "assistant", "content": "previous"}]
        sink.error.assert_called_once()


class TestRunTurnClipboardGuard:
    def _make_sink(self) -> MagicMock:
        sink = MagicMock()
        sink.info = MagicMock()
        sink.error = MagicMock()
        sink.assistant = MagicMock()
        sink.plan = MagicMock()
        sink.progress = MagicMock()
        sink.prompt_confirmation = MagicMock(return_value="yes")
        sink.cancel_stream = MagicMock()
        return sink

    @patch("core.get_tool_definitions", return_value=[])
    @patch("core.get_xai_model", return_value="test-model")
    @patch("core.get_xai_api_key", return_value="test-key")
    @patch("core.get_max_tool_loops", return_value=5)
    @patch("core._chat_with_optional_web_tools")
    @patch("core.dispatch_tool")
    def test_clipboard_claim_without_tool_is_retried(
        self,
        mock_dispatch: MagicMock,
        mock_chat: MagicMock,
        mock_loops: MagicMock,
        mock_key: MagicMock,
        mock_model: MagicMock,
        mock_tools: MagicMock,
    ) -> None:
        mock_dispatch.return_value = {"ok": True}
        mock_chat.side_effect = [
            ChatCompletionResult(
                message_role="assistant",
                content="I'll search first.",
                tool_calls=[ToolCallSpec(id="s1", name="search_file_contents", arguments={"path": "/tmp", "query": "x"})],
                raw={},
            ),
            ChatCompletionResult(
                message_role="assistant",
                content="Copying the clean summary to your clipboard now.",
                tool_calls=[],
                raw={},
            ),
            ChatCompletionResult(
                message_role="assistant",
                content="Now I will use the clipboard tool.",
                tool_calls=[ToolCallSpec(id="c1", name="copy_to_clipboard", arguments={"text": "clean summary"})],
                raw={},
            ),
            ChatCompletionResult(
                message_role="assistant",
                content="Done.",
                tool_calls=[],
                raw={},
            ),
        ]
        sink = self._make_sink()
        messages: list = []

        _run_turn(messages, "Search and copy the summary to my clipboard.", sink)

        assert mock_chat.call_count == 4
        assert mock_dispatch.call_args_list[-1].args[0] == "copy_to_clipboard"
        sink.info.assert_any_call(
            "[note] Clipboard copy was requested, but no clipboard tool ran. "
            "Asking the assistant to perform the clipboard step now."
        )
        sink.assistant.assert_called_with("Done.")


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
