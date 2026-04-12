"""Orchestration layer — decoupled from CLI rendering.

This module owns the conversation loop logic, tool dispatch, and confirmation
flow.  It communicates through an OutputSink protocol so the CLI (or a future
GUI) can plug in its own rendering.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from config import (
    get_allowed_roots,
    get_default_desktop_path,
    get_max_tool_loops,
    get_xai_api_key,
    get_xai_model,
    is_dry_run,
    is_verbose,
    web_search_enabled,
)
from logger import log_event
from schemas import MUTATING_TOOL_NAMES, SYSTEM_PROMPT, get_server_side_tools, get_tool_definitions
from safety import is_affirmative_confirmation
from tools import dispatch_tool
from xai_client import ChatCompletionResult, ToolCallSpec, chat_completion


# ---------------------------------------------------------------------------
# Output sink protocol (CLI / GUI implement this)
# ---------------------------------------------------------------------------


class OutputSink(Protocol):
    def info(self, text: str) -> None: ...
    def error(self, text: str) -> None: ...
    def assistant(self, text: str) -> None: ...
    def plan(self, card: ApprovalCard) -> None: ...
    def progress(self, text: str) -> None: ...
    def prompt_confirmation(self, prompt_text: str) -> str: ...


# ---------------------------------------------------------------------------
# Structured action plan
# ---------------------------------------------------------------------------


@dataclass
class PlannedAction:
    """One step in a pending action plan."""
    index: int
    tool_name: str
    arguments: dict[str, Any]
    label: str = ""          # human-readable one-liner
    risk: str = ""           # "low" or "medium"; auto-detected if empty

    def __post_init__(self) -> None:
        if not self.label:
            self.label = _action_label(self.tool_name, self.arguments)
        if not self.risk:
            self.risk = _action_risk(self.tool_name, self.arguments)


@dataclass
class ApprovalCard:
    """Everything the UI needs to render an approval prompt."""
    actions: list[PlannedAction] = field(default_factory=list)
    affected_root: str = ""
    dry_run: bool = False
    risk_level: str = "low"  # overall: max of individual risks
    summary: str = ""
    # Optional structured enrichment (populated by xai_structured.py when available)
    shell_explanation: dict[str, str] | None = None

    def __post_init__(self) -> None:
        if self.actions:
            risks = {a.risk for a in self.actions}
            if "high" in risks:
                max_risk = "high"
            elif "medium" in risks:
                max_risk = "medium"
            else:
                max_risk = "low"
            # Only escalate, never downgrade
            risk_order = {"low": 0, "medium": 1, "high": 2}
            if risk_order.get(max_risk, 0) > risk_order.get(self.risk_level, 0):
                self.risk_level = max_risk
        if not self.summary:
            self.summary = _build_summary(self.actions)


def _action_label(tool: str, args: dict[str, Any]) -> str:
    if tool == "move_file":
        return f"MOVE {args.get('source', '?')} -> {args.get('destination', '?')}"
    if tool == "rename_file":
        return f"RENAME {args.get('source', '?')} -> {args.get('new_name', '?')}"
    if tool == "create_folder":
        return f"CREATE FOLDER {args.get('path', '?')}"
    if tool == "organize_desktop_by_type":
        return f"ORGANIZE DESKTOP {args.get('desktop_path', '(default)')}"
    if tool == "organize_folder":
        return f"ORGANIZE FOLDER {args.get('path', '?')} by {args.get('mode', 'type')}"
    if tool == "write_file":
        fp = args.get("path", "?")
        ow = args.get("overwrite", False)
        sz = len(args.get("content", "").encode("utf-8", errors="replace"))
        label = f"WRITE {fp} ({sz} bytes)"
        if ow:
            label += " [OVERWRITE — .bak backup will be created]"
        else:
            label += " [NEW FILE]"
        return label
    if tool == "run_command":
        cmd = args.get("command", "?")
        cwd = args.get("working_dir", "(project root)")
        return f"RUN COMMAND: {cmd}  [in {cwd}]"
    return f"{tool}({json.dumps(args, ensure_ascii=False)})"


def _action_risk(tool: str, args: dict[str, Any] | None = None) -> str:
    if tool in ("organize_desktop_by_type", "organize_folder"):
        return "medium"
    if tool == "write_file":
        if args and args.get("overwrite"):
            return "medium"
        return "low"
    if tool == "run_command":
        # Classify the command to determine risk tier
        if args:
            from shell_guard import classify_command, get_extra_allowlist
            verdict = classify_command(args.get("command", ""), get_extra_allowlist())
            if verdict.tier == "risky":
                return "high"
            return "medium"  # safe-tier shell commands are still medium risk
        return "high"
    return "low"


def _build_summary(actions: list[PlannedAction]) -> str:
    move_count = sum(1 for a in actions if a.tool_name in ("move_file", "rename_file"))
    folder_count = sum(1 for a in actions if a.tool_name == "create_folder")
    org_count = sum(1 for a in actions if a.tool_name in ("organize_desktop_by_type", "organize_folder"))
    write_count = sum(1 for a in actions if a.tool_name == "write_file")
    cmd_count = sum(1 for a in actions if a.tool_name == "run_command")

    parts: list[str] = []
    if move_count:
        parts.append(f"{move_count} file operation(s)")
    if folder_count:
        parts.append(f"{folder_count} folder(s) to create")
    if write_count:
        parts.append(f"{write_count} file(s) to write")
    if org_count:
        parts.append(f"{org_count} organize operation(s)")
    if cmd_count:
        parts.append(f"{cmd_count} shell command(s)")
    return ", ".join(parts) if parts else "action(s) pending"


def _detect_affected_root(actions: list[PlannedAction]) -> str:
    """Best-effort: find the common root folder across all action paths."""
    paths: list[str] = []
    for a in actions:
        for key in ("source", "destination", "path", "desktop_path"):
            v = a.arguments.get(key)
            if v:
                paths.append(v)
    if not paths:
        return ""
    # Find shortest common prefix
    from pathlib import PurePath
    try:
        parts_list = [PurePath(p).parts for p in paths]
        common = []
        for i, part in enumerate(parts_list[0]):
            if all(len(ps) > i and ps[i] == part for ps in parts_list):
                common.append(part)
            else:
                break
        return str(PurePath(*common)) if common else paths[0]
    except Exception:
        return paths[0] if paths else ""


def build_approval_card(tool_calls: list[ToolCallSpec]) -> ApprovalCard:
    """Build a structured approval card from a batch of pending tool calls."""
    actions = [
        PlannedAction(
            index=i + 1,
            tool_name=tc.name,
            arguments=tc.arguments,
        )
        for i, tc in enumerate(tool_calls)
    ]

    # Try to enrich shell commands with structured explanations
    shell_explanation: dict[str, str] | None = None
    shell_actions = [tc for tc in tool_calls if tc.name == "run_command"]
    if shell_actions:
        cmd = shell_actions[0].arguments.get("command", "")
        tier = "risky"  # default; will be overridden below
        for a in actions:
            if a.tool_name == "run_command":
                tier = a.risk
                break
        try:
            from xai_structured import explain_shell_command
            shell_explanation = explain_shell_command(cmd, tier)
        except Exception:
            pass  # fallback: no explanation

    return ApprovalCard(
        actions=actions,
        affected_root=_detect_affected_root(actions),
        dry_run=is_dry_run(),
        shell_explanation=shell_explanation,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_WEB_SEARCH_ATTACHED: bool | None = None


def _build_assistant_tool_message(
    tool_calls: list[ToolCallSpec],
    assistant_content: str | None = None,
) -> dict[str, Any]:
    api_calls: list[dict[str, Any]] = []
    for tc in tool_calls:
        tc_id = tc.id or f"call_{uuid.uuid4().hex[:24]}"
        api_calls.append({
            "id": tc_id,
            "type": "function",
            "function": {
                "name": tc.name,
                "arguments": json.dumps(tc.arguments, ensure_ascii=False),
            },
        })
    return {"role": "assistant", "content": assistant_content, "tool_calls": api_calls}


def _tool_result_message(tool_call_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "content": json.dumps(payload, ensure_ascii=False, default=str),
    }


def _ensure_tool_call_ids(tool_calls: list[ToolCallSpec]) -> list[ToolCallSpec]:
    out: list[ToolCallSpec] = []
    for tc in tool_calls:
        tid = tc.id or f"call_{uuid.uuid4().hex[:24]}"
        out.append(ToolCallSpec(id=tid, name=tc.name, arguments=tc.arguments))
    return out


def _merge_tool_defs(base: list[dict[str, Any]], extra: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return list(base) + list(extra)


# ---------------------------------------------------------------------------
# Web search integration
# ---------------------------------------------------------------------------


def _chat_with_optional_web_tools(
    api_key: str,
    model: str,
    messages: list[dict[str, Any]],
    base_tools: list[dict[str, Any]],
    sink: OutputSink,
) -> ChatCompletionResult:
    global _WEB_SEARCH_ATTACHED
    if not web_search_enabled():
        return chat_completion(api_key, model, messages, tools=base_tools)
    if _WEB_SEARCH_ATTACHED is False:
        return chat_completion(api_key, model, messages, tools=base_tools)
    if _WEB_SEARCH_ATTACHED is True:
        return chat_completion(
            api_key, model, messages,
            tools=_merge_tool_defs(base_tools, get_server_side_tools()),
        )
    try:
        result = chat_completion(
            api_key, model, messages,
            tools=_merge_tool_defs(base_tools, get_server_side_tools()),
        )
        _WEB_SEARCH_ATTACHED = True
        return result
    except RuntimeError as e:
        _WEB_SEARCH_ATTACHED = False
        log_event("web_search_fallback", {"error": str(e)})
        sink.info(
            "[note] Built-in web_search unavailable; continuing without it. "
            "Set XAI_ENABLE_WEB_SEARCH=0 to silence."
        )
        return chat_completion(api_key, model, messages, tools=base_tools)


# ---------------------------------------------------------------------------
# Tool processing
# ---------------------------------------------------------------------------


def _process_tool_calls(
    messages: list[dict[str, Any]],
    tool_calls: list[ToolCallSpec],
    sink: OutputSink,
    assistant_content: str | None = None,
) -> None:
    tool_calls = _ensure_tool_call_ids(tool_calls)
    messages.append(_build_assistant_tool_message(tool_calls, assistant_content))

    i = 0
    n = len(tool_calls)
    while i < n:
        tc = tool_calls[i]
        if tc.name not in MUTATING_TOOL_NAMES:
            res = dispatch_tool(tc.name, tc.arguments)
            messages.append(_tool_result_message(tc.id, res))
            if res.get("ok") is False:
                log_event("tool_error", {"tool": tc.name, "error": res.get("error")}, phase="error")
            i += 1
            continue

        # Gather consecutive mutating calls into a confirmation block
        j = i
        block: list[ToolCallSpec] = []
        while j < n and tool_calls[j].name in MUTATING_TOOL_NAMES:
            block.append(tool_calls[j])
            j += 1

        card = build_approval_card(block)
        sink.plan(card)

        answer = sink.prompt_confirmation(
            'Approve? (yes / cancel): '
        )
        approved = is_affirmative_confirmation(answer)
        log_event(
            "user_confirmation",
            {"approved": approved, "tools": [b.name for b in block]},
            phase="confirmed" if approved else "skipped",
        )

        executed = 0
        for b in block:
            if approved:
                res = dispatch_tool(b.name, b.arguments)
                if res.get("ok"):
                    executed += 1
                    if is_verbose():
                        sink.progress(f"  Done: {b.name}")
            else:
                res = {"ok": False, "error": "user_declined", "declined": True}
            messages.append(_tool_result_message(b.id, res))
            if res.get("ok") is False and not res.get("declined"):
                log_event("tool_error", {"tool": b.name, "error": res.get("error")}, phase="error")

        if approved:
            # Try structured summary; fall back to plain text
            structured_summary = _try_structured_summary(block, executed)
            if structured_summary:
                sink.info(structured_summary)
            else:
                sink.info(f"Completed {executed}/{len(block)} operation(s).")

        i = j


def _try_structured_summary(block: list[ToolCallSpec], executed: int) -> str | None:
    """Attempt to get a structured execution summary. Returns formatted string or None."""
    try:
        from xai_structured import summarize_execution
        results = [{"ok": True}] * executed + [{"ok": False}] * (len(block) - executed)
        has_shell = any(b.name == "run_command" for b in block)
        summary = summarize_execution(results, dry_run=is_dry_run())
        if summary:
            parts = [f"Completed {summary['actions_completed']}/{len(block)} operation(s)."]
            if summary.get("one_line_summary"):
                parts.append(summary["one_line_summary"])
            return "  ".join(parts)
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Public conversation driver
# ---------------------------------------------------------------------------


def handle_user_turn(
    messages: list[dict[str, Any]],
    user_text: str,
    sink: OutputSink,
) -> None:
    """Process one user turn: send to model, handle tool calls, render reply."""
    api_key = get_xai_api_key()
    if not api_key:
        sink.error("Missing XAI_API_KEY.")
        return

    model = get_xai_model()
    tools = get_tool_definitions()

    messages.append({"role": "user", "content": user_text})
    log_event("user_message", {"length": len(user_text)}, user_request=user_text)

    max_steps = get_max_tool_loops()
    for _ in range(max_steps):
        try:
            result = _chat_with_optional_web_tools(api_key, model, messages, tools, sink)
        except RuntimeError as e:
            sink.error(f"[error] {e}")
            log_event("api_error", {"error": str(e)}, phase="error")
            messages.pop()
            return

        if result.tool_calls:
            _process_tool_calls(messages, result.tool_calls, sink, result.content)
            continue

        text = (result.content or "").strip()
        if text:
            sink.assistant(text)
        else:
            sink.assistant("(empty response)")
        log_event("assistant_done", {"has_content": bool(text)})
        return
    else:
        sink.error("[error] Tool loop limit reached; stopping this turn.")
        log_event("tool_loop_limit", {}, phase="error")
        messages.pop()


def get_startup_info() -> dict[str, Any]:
    """Return info dict for startup display."""
    return {
        "model": get_xai_model(),
        "desktop": str(get_default_desktop_path()),
        "allowed_roots": [str(p) for p in get_allowed_roots()],
        "dry_run": is_dry_run(),
        "web_search": web_search_enabled(),
        "max_tool_loops": get_max_tool_loops(),
        "verbose": is_verbose(),
    }
