"""Tests for the orchestration layer: ApprovalCard, plan building."""

from __future__ import annotations

from core import ApprovalCard, PlannedAction, _action_label, _action_risk, build_approval_card
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
