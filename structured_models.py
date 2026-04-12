"""Pydantic models for xAI structured output responses.

These models define the schemas sent to the xAI API via chat.parse().
Every field has a description so the schema is self-documenting to the model.
These are used for richer UI rendering — never for safety decisions.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class PlannedActionExplanation(BaseModel):
    """Model-generated explanation of one planned action."""
    tool_name: str = Field(description="The tool being called (e.g. move_file, create_folder).")
    display_label: str = Field(description="Short human-readable label for the action.")
    reason: str = Field(description="Why this action is being taken, in one sentence.")
    risk: Literal["low", "medium", "high"] = Field(
        description="Risk level: low for single-file ops, medium for bulk ops, high for shell commands."
    )


class ActionPlanExplanation(BaseModel):
    """Structured explanation of a proposed action plan."""
    summary: str = Field(description="One-line summary of what the plan does overall.")
    actions: list[PlannedActionExplanation] = Field(
        description="List of planned actions with explanations."
    )
    overall_risk: Literal["low", "medium", "high"] = Field(
        description="The highest risk level among all actions."
    )


class ShellCommandExplanation(BaseModel):
    """Structured explanation of a proposed shell command."""
    command: str = Field(description="The exact command string.")
    what_it_does: str = Field(description="Plain-language explanation of what this command does.")
    side_effects: str = Field(
        description="What side effects this command has (file changes, network, etc.), or 'None' if read-only."
    )
    risk_reason: str = Field(
        description="Why this command is at its risk level."
    )


class ExecutionSummary(BaseModel):
    """Structured summary of completed execution results."""
    actions_completed: int = Field(description="Number of actions that succeeded.")
    actions_skipped: int = Field(description="Number of actions that were skipped or failed.")
    collisions_handled: int = Field(
        description="Number of file name collisions that were resolved with suffixes."
    )
    undo_available: bool = Field(description="Whether undo is available for these actions.")
    dry_run: bool = Field(description="Whether this was a dry-run (no real changes).")
    one_line_summary: str = Field(description="One sentence summarizing what happened.")
