"""Orchestration layer — decoupled from CLI rendering.

This module owns the conversation loop logic, tool dispatch, and confirmation
flow.  It communicates through an OutputSink protocol so the CLI (or a future
GUI) can plug in its own rendering.
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from shell_guard import redact_secrets
from config import (
    get_allowed_roots,
    get_coding_model,
    get_default_desktop_path,
    get_max_tool_loops,
    get_xai_api_key,
    get_xai_model,
    is_dry_run,
    is_verbose,
    set_runtime_model,
    user_has_set_model,
    web_search_enabled,
)
from logger import log_event
from schemas import MUTATING_TOOL_NAMES, SYSTEM_PROMPT, get_server_side_tools, get_tool_definitions
from safety import is_affirmative_confirmation
from tools import dispatch_tool
from xai_client import ChatCompletionResult, ToolCallSpec, chat_completion, chat_completion_stream


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
    action_class: str = ""
    label: str = ""          # human-readable one-liner
    risk: str = ""           # "low", "medium", or "high"; auto-detected if empty

    def __post_init__(self) -> None:
        if not self.action_class:
            self.action_class = _action_class(self.tool_name)
        if not self.label:
            self.label = _action_label(self.tool_name, self.arguments)
        if not self.risk:
            self.risk = _action_risk(self.tool_name, self.arguments)


@dataclass
class ApprovalCard:
    """Everything the UI needs to render an approval prompt."""
    actions: list[PlannedAction] = field(default_factory=list)
    action_class: str = ""
    affected_root: str = ""
    dry_run: bool = False
    risk_level: str = "low"  # overall: max of individual risks
    summary: str = ""
    # Optional structured enrichment (populated by xai_structured.py when available)
    shell_explanation: dict[str, str] | None = None

    def __post_init__(self) -> None:
        if self.actions and not self.action_class:
            self.action_class = self.actions[0].action_class
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


def _action_class(tool: str) -> str:
    if tool in (
        "move_file",
        "rename_file",
        "create_folder",
        "organize_desktop_by_type",
        "organize_folder",
        "copy_file",
        "delete_file_to_recycle_bin",
        "write_file",
        "replace_in_file",
        "append_file",
        "apply_patch",
    ):
        return "filesystem_write"
    if tool in ("start_process", "stop_process"):
        return "process_control"
    if tool == "focus_window":
        return "window_control"
    if tool in ("read_clipboard", "window_screenshot"):
        return "sensitive_read"
    if tool in ("move_mouse", "click", "scroll", "type_text", "press_hotkey"):
        return "desktop_input"
    if tool in ("browser_navigate", "browser_click", "browser_fill", "browser_press", "browser_download", "browser_screenshot"):
        return "browser_control"
    if tool == "run_command":
        return "shell"
    return "read_only"


def _action_label(tool: str, args: dict[str, Any]) -> str:
    if tool == "move_file":
        return f"MOVE {args.get('source', '?')} -> {args.get('destination', '?')}"
    if tool == "copy_file":
        suffix = " [OVERWRITE]" if args.get("overwrite") else ""
        return f"COPY {args.get('source', '?')} -> {args.get('destination', '?')}{suffix}"
    if tool == "delete_file_to_recycle_bin":
        return f"RECYCLE FILE {args.get('path', '?')}"
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
    if tool == "focus_window":
        return f"FOCUS WINDOW id={args.get('window_id', '?')}"
    if tool == "start_process":
        exe = args.get("executable", "?")
        raw_args = " ".join(args.get("args", []) or [])
        cwd = args.get("working_dir", "(project root)")
        return f"START PROCESS: {exe} {raw_args}".strip() + f"  [in {cwd}]"
    if tool == "stop_process":
        force = " [FORCE]" if args.get("force") else ""
        return f"STOP PROCESS pid={args.get('pid', '?')}{force}"
    if tool == "move_mouse":
        return f"MOVE MOUSE to ({args.get('x', '?')}, {args.get('y', '?')})"
    if tool == "click":
        return (
            f"CLICK {args.get('button', 'left')} at ({args.get('x', '?')}, {args.get('y', '?')})"
            f" x{args.get('clicks', 1)}"
        )
    if tool == "scroll":
        return f"SCROLL {args.get('amount', '?')} at ({args.get('x', 'current')}, {args.get('y', 'current')})"
    if tool == "type_text":
        preview = redact_secrets(args.get("text", ""))
        if len(preview) > 60:
            preview = preview[:57] + "..."
        return f"TYPE TEXT ({len(args.get('text', ''))} chars): {preview!r}"
    if tool == "press_hotkey":
        return f"PRESS HOTKEY: {'+'.join(args.get('keys', []))}"
    if tool == "browser_navigate":
        return f"BROWSER NAVIGATE: {args.get('url', '?')}"
    if tool == "browser_click":
        return f"BROWSER CLICK selector={args.get('selector', '?')!r}"
    if tool == "browser_fill":
        preview = redact_secrets(args.get("text", ""))
        if len(preview) > 60:
            preview = preview[:57] + "..."
        return f"BROWSER FILL selector={args.get('selector', '?')!r} text={preview!r}"
    if tool == "browser_press":
        return f"BROWSER PRESS selector={args.get('selector', '?')!r} key={args.get('key', '?')!r}"
    if tool == "browser_download":
        domain = args.get("url", "(current page)")
        selector = args.get("click_selector")
        suffix = f" selector={selector!r}" if selector else ""
        return f"BROWSER DOWNLOAD {domain}{suffix}"
    if tool == "browser_screenshot":
        selector = args.get("selector")
        suffix = f" selector={selector!r}" if selector else " page"
        return f"BROWSER SCREENSHOT{suffix} full_page={bool(args.get('full_page'))}"
    if tool == "read_clipboard":
        return f"READ CLIPBOARD (max {args.get('max_chars', 5000)} chars)"
    if tool == "window_screenshot":
        return f"WINDOW SCREENSHOT id={args.get('window_id', '?')}"
    if tool == "replace_in_file":
        return f"REPLACE IN FILE {args.get('path', '?')} [all={bool(args.get('replace_all'))}]"
    if tool == "append_file":
        size = len(args.get("content", "").encode("utf-8", errors="replace"))
        return f"APPEND FILE {args.get('path', '?')} ({size} bytes)"
    if tool == "apply_patch":
        diff = args.get("unified_diff", "")
        hunk_count = diff.count("@@")
        return f"APPLY PATCH {args.get('path', '?')} ({hunk_count} hunk(s))"
    return f"{tool}({json.dumps(args, ensure_ascii=False)})"


def _action_risk(tool: str, args: dict[str, Any] | None = None) -> str:
    if tool in ("organize_desktop_by_type", "organize_folder"):
        return "medium"
    if tool == "write_file":
        if args and args.get("overwrite"):
            return "medium"
        return "low"
    if tool == "copy_file":
        return "medium" if args and args.get("overwrite") else "low"
    if tool in ("delete_file_to_recycle_bin", "read_clipboard", "window_screenshot", "browser_screenshot"):
        return "medium"
    if tool == "run_command":
        # Classify the command to determine risk tier
        if args:
            from shell_guard import classify_command, get_extra_allowlist
            verdict = classify_command(args.get("command", ""), get_extra_allowlist())
            if verdict.tier == "risky":
                return "high"
            return "medium"  # safe-tier shell commands are still medium risk
        return "high"
    if tool == "start_process":
        return "high"
    if tool in ("focus_window", "browser_navigate", "replace_in_file", "append_file", "apply_patch"):
        return "medium"
    if tool in ("click", "type_text", "press_hotkey", "stop_process", "browser_click", "browser_fill", "browser_press", "browser_download"):
        return "high"
    return "low"


def _dispatch_with_activity(
    sink: Any,
    name: str,
    arguments: dict[str, Any],
    label: str,
) -> dict[str, Any]:
    """Run dispatch_tool while notifying the sink via optional tool_start/tool_end hooks."""
    start = getattr(sink, "tool_start", None)
    end = getattr(sink, "tool_end", None)
    if start:
        start(name, label)
    ok = False
    try:
        res = dispatch_tool(name, arguments)
        ok = isinstance(res, dict) and bool(res.get("ok"))
        return res
    finally:
        if end:
            end(name, ok)


def _tool_progress_label(tool: str, args: dict[str, Any]) -> str:
    """Short, user-friendly label for read-only tool calls shown as progress."""
    path = args.get("path") or args.get("desktop_path") or ""
    if tool == "list_directory":
        return f"Listing {path}"
    if tool == "analyze_directory":
        return f"Analyzing {path}"
    if tool == "directory_tree":
        return f"Scanning tree at {path}"
    if tool == "search_files":
        return f"Searching for '{args.get('query', '?')}' in {path}"
    if tool == "get_file_info":
        return f"Inspecting file info for {path}"
    if tool == "recursive_find_files":
        return f"Searching recursively in {path}"
    if tool == "search_file_contents":
        return f"Searching file contents in {path}"
    if tool == "recent_files":
        return f"Finding recent files in {path}"
    if tool == "largest_files":
        return f"Finding largest files in {path}"
    if tool == "file_type_summary":
        return f"Summarizing file types in {path}"
    if tool == "read_text_file":
        return f"Reading {path}"
    if tool == "preview_plan_for_desktop_cleanup":
        return f"Previewing desktop cleanup{' at ' + path if path else ''}"
    if tool == "preview_organize_folder":
        mode = args.get("mode", "type")
        return f"Previewing organize-by-{mode} for {path}"
    if tool == "open_url":
        return f"Opening {args.get('url', '?')}"
    if tool == "take_screenshot":
        return "Capturing screenshot"
    if tool == "get_screen_info":
        return "Inspecting screen info"
    if tool == "window_screenshot":
        return f"Capturing window screenshot id={args.get('window_id', '?')}"
    if tool == "ocr_image":
        return f"Running OCR on {path}"
    if tool == "list_windows":
        return "Listing windows"
    if tool == "get_active_window":
        return "Inspecting active window"
    if tool == "list_processes":
        query = args.get("query")
        return f"Listing processes{' for ' + query if query else ''}"
    if tool == "read_file_range":
        return f"Reading lines {args.get('start_line', '?')}-{args.get('end_line', '?')} from {path}"
    if tool == "wait_seconds":
        return f"Waiting {args.get('seconds', '?')}s"
    if tool == "wait_for_window":
        return f"Waiting for window '{args.get('title_query', '?')}'"
    if tool == "wait_for_file":
        return f"Waiting for file {path}"
    if tool == "wait_for_process_exit":
        return f"Waiting for process {args.get('pid', '?')} to exit"
    if tool == "browser_extract_text":
        return f"Extracting text from selector {args.get('selector', 'body')!r}"
    if tool == "browser_screenshot":
        return f"Capturing browser screenshot for {args.get('selector') or 'page'}"
    if tool == "browser_wait_for":
        return f"Waiting for browser selector {args.get('selector', '?')!r}"
    if tool == "copy_to_clipboard":
        return "Copying text to clipboard"
    if tool == "read_clipboard":
        return "Reading clipboard"
    return f"Running {tool}"


def _build_summary(actions: list[PlannedAction]) -> str:
    move_count = sum(1 for a in actions if a.tool_name in ("move_file", "rename_file"))
    folder_count = sum(1 for a in actions if a.tool_name == "create_folder")
    org_count = sum(1 for a in actions if a.tool_name in ("organize_desktop_by_type", "organize_folder"))
    copy_count = sum(1 for a in actions if a.tool_name == "copy_file")
    recycle_count = sum(1 for a in actions if a.tool_name == "delete_file_to_recycle_bin")
    write_count = sum(1 for a in actions if a.tool_name == "write_file")
    edit_count = sum(1 for a in actions if a.tool_name in ("replace_in_file", "append_file", "apply_patch"))
    cmd_count = sum(1 for a in actions if a.tool_name == "run_command")
    process_count = sum(1 for a in actions if a.action_class == "process_control")
    window_count = sum(1 for a in actions if a.action_class == "window_control")
    sensitive_count = sum(1 for a in actions if a.action_class == "sensitive_read")
    input_count = sum(1 for a in actions if a.action_class == "desktop_input")
    browser_count = sum(1 for a in actions if a.action_class == "browser_control")

    parts: list[str] = []
    if move_count:
        parts.append(f"{move_count} file operation(s)")
    if folder_count:
        parts.append(f"{folder_count} folder(s) to create")
    if copy_count:
        parts.append(f"{copy_count} file copy operation(s)")
    if recycle_count:
        parts.append(f"{recycle_count} file(s) to recycle")
    if write_count:
        parts.append(f"{write_count} file(s) to write")
    if edit_count:
        parts.append(f"{edit_count} file edit(s)")
    if org_count:
        parts.append(f"{org_count} organize operation(s)")
    if cmd_count:
        parts.append(f"{cmd_count} shell command(s)")
    if process_count:
        parts.append(f"{process_count} process action(s)")
    if window_count:
        parts.append(f"{window_count} window action(s)")
    if sensitive_count:
        parts.append(f"{sensitive_count} sensitive read(s)")
    if input_count:
        parts.append(f"{input_count} desktop input action(s)")
    if browser_count:
        parts.append(f"{browser_count} browser action(s)")
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
        action_class=actions[0].action_class if actions else "",
        affected_root=_detect_affected_root(actions),
        dry_run=is_dry_run(),
        shell_explanation=shell_explanation,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_WEB_SEARCH_ATTACHED: bool | None = None


def _runtime_system_prompt() -> str:
    roots = "\n".join(f"- {p}" for p in get_allowed_roots())
    return (
        f"{SYSTEM_PROMPT}\n\n"
        "Runtime context:\n"
        f"- Default Desktop path: {get_default_desktop_path()}\n"
        f"- Allowed local roots:\n{roots}\n\n"
        "Tool-selection guidance:\n"
        "- For Desktop questions, use the default Desktop path above directly.\n"
        "- Prefer dedicated read-only tools such as list_directory, recent_files, "
        "largest_files, search_files, recursive_find_files, search_file_contents, "
        "get_file_info, and directory_tree for inspection tasks.\n"
        "- Do not use run_command just to discover common paths, list files, or sort "
        "recent files when a dedicated read-only tool can do the job.\n"
        "- Use copy_file and delete_file_to_recycle_bin for file copy/delete tasks; "
        "never use shell commands for delete/copy when a dedicated tool applies.\n"
        "- Use get_screen_info, window_screenshot, browser_screenshot, and clipboard "
        "tools for visual or clipboard context instead of shell workarounds.\n"
        "- When reporting directory contents, use list_directory.files and "
        "list_directory.folders plus their explicit file_count/folder_count fields. "
        "Do not count mixed entries yourself unless no count field is available."
    )


def _ensure_runtime_system_prompt(messages: list[dict[str, Any]]) -> None:
    prompt = _runtime_system_prompt()
    if messages and messages[0].get("role") == "system":
        messages[0]["content"] = prompt
    else:
        messages.insert(0, {"role": "system", "content": prompt})


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


def _requested_clipboard_write(user_text: str) -> bool:
    lower = user_text.casefold()
    return "clipboard" in lower and bool(re.search(r"\b(copy|put|place|save|send)\b", lower))


def _claims_clipboard_write(text: str) -> bool:
    lower = text.casefold()
    if "clipboard" not in lower:
        return False
    return bool(
        re.search(r"\b(copied|copying|copy|placed|saved|sent)\b", lower)
        or "to your clipboard" in lower
        or "on your clipboard" in lower
    )


def _successful_tool_since(messages: list[dict[str, Any]], start: int, tool_name: str) -> bool:
    call_ids: set[str] = set()
    for msg in messages[start:]:
        if msg.get("role") == "assistant":
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function") or {}
                if fn.get("name") == tool_name and tc.get("id"):
                    call_ids.add(str(tc["id"]))
        elif msg.get("role") == "tool" and msg.get("tool_call_id") in call_ids:
            try:
                payload = json.loads(str(msg.get("content") or "{}"))
            except json.JSONDecodeError:
                payload = {}
            if payload.get("ok") is True:
                return True
    return False


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
    *,
    on_delta: Any = None,
    stop_event: Any = None,
) -> ChatCompletionResult:
    global _WEB_SEARCH_ATTACHED

    def _call(tools: list[dict[str, Any]]) -> ChatCompletionResult:
        if on_delta is not None:
            return chat_completion_stream(
                api_key, model, messages, tools=tools,
                on_delta=on_delta, stop_event=stop_event,
            )
        return chat_completion(api_key, model, messages, tools=tools)

    if not web_search_enabled():
        return _call(base_tools)
    if _WEB_SEARCH_ATTACHED is False:
        return _call(base_tools)
    if _WEB_SEARCH_ATTACHED is True:
        return _call(_merge_tool_defs(base_tools, get_server_side_tools()))
    try:
        result = _call(_merge_tool_defs(base_tools, get_server_side_tools()))
        _WEB_SEARCH_ATTACHED = True
        return result
    except RuntimeError as e:
        _WEB_SEARCH_ATTACHED = False
        log_event("web_search_fallback", {"error": str(e)})
        sink.info(
            "[note] Built-in web_search unavailable; continuing without it. "
            "Set XAI_ENABLE_WEB_SEARCH=0 to silence."
        )
        return _call(base_tools)


# ---------------------------------------------------------------------------
# Tool processing
# ---------------------------------------------------------------------------


def _process_tool_calls(
    messages: list[dict[str, Any]],
    tool_calls: list[ToolCallSpec],
    sink: OutputSink,
    assistant_content: str | None = None,
) -> None:
    def _preflight_mutating_tool(call: ToolCallSpec) -> tuple[bool, dict[str, Any] | None]:
        if call.name == "run_command":
            from shell_guard import classify_command, get_extra_allowlist

            verdict = classify_command(call.arguments.get("command", ""), get_extra_allowlist())
            if verdict.tier == "blocked":
                return True, None
        if call.name == "press_hotkey":
            from desktop_tools import classify_hotkey

            verdict = classify_hotkey(call.arguments.get("keys", []))
            if verdict.get("blocked"):
                return True, verdict
        return False, None

    def _is_retryable(call: ToolCallSpec) -> bool:
        return _action_class(call.name) == "filesystem_write"

    tool_calls = _ensure_tool_call_ids(tool_calls)
    messages.append(_build_assistant_tool_message(tool_calls, assistant_content))

    i = 0
    n = len(tool_calls)
    while i < n:
        tc = tool_calls[i]
        if tc.name not in MUTATING_TOOL_NAMES:
            label = _tool_progress_label(tc.name, tc.arguments)
            sink.progress(f"  ↳ {label}")
            res = _dispatch_with_activity(sink, tc.name, tc.arguments, label)
            messages.append(_tool_result_message(tc.id, res))
            if res.get("ok") is False and not res.get("blocked"):
                log_event("tool_error", {"tool": tc.name, "error": res.get("error")}, phase="error")
            i += 1
            continue

        blocked, blocked_result = _preflight_mutating_tool(tc)
        if blocked:
            res = blocked_result if blocked_result is not None else dispatch_tool(tc.name, tc.arguments)
            messages.append(_tool_result_message(tc.id, res))
            i += 1
            continue

        # Gather consecutive mutating calls into a confirmation block
        j = i
        block: list[ToolCallSpec] = []
        action_class = _action_class(tc.name)
        while j < n and tool_calls[j].name in MUTATING_TOOL_NAMES:
            if _action_class(tool_calls[j].name) != action_class:
                break
            if _preflight_mutating_tool(tool_calls[j])[0]:
                break
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
        results: dict[str, dict[str, Any]] = {}
        for b in block:
            if approved:
                res = _dispatch_with_activity(
                    sink, b.name, b.arguments, _tool_progress_label(b.name, b.arguments)
                )
                if res.get("ok"):
                    executed += 1
                    if is_verbose():
                        sink.progress(f"  Done: {b.name}")
            else:
                res = {"ok": False, "error": "user_declined", "declined": True}
            results[b.id] = res
            messages.append(_tool_result_message(b.id, res))
            if res.get("ok") is False and not res.get("declined") and not res.get("blocked"):
                log_event("tool_error", {"tool": b.name, "error": res.get("error")}, phase="error")

        if approved:
            # Offer one retry pass for failed idempotent operations (not run_command).
            failed_block = [
                b for b in block
                if _is_retryable(b)
                and not results[b.id].get("ok")
                and not results[b.id].get("declined")
                and not results[b.id].get("blocked")
            ]
            if failed_block:
                sink.info(
                    f"  ⚠  {len(failed_block)}/{len(block)} operation(s) failed — "
                    "would you like to retry them?"
                )
                retry_card = build_approval_card(failed_block)
                retry_card.summary = f"Retry {len(failed_block)} failed operation(s)"
                sink.plan(retry_card)
                retry_answer = sink.prompt_confirmation("Retry failed operations? (yes / cancel): ")
                if is_affirmative_confirmation(retry_answer):
                    log_event("retry_batch", {"count": len(failed_block)}, phase="retry")
                    for b in failed_block:
                        res = _dispatch_with_activity(
                            sink, b.name, b.arguments, _tool_progress_label(b.name, b.arguments)
                        )
                        was_ok = results[b.id].get("ok", False)
                        results[b.id] = res
                        if res.get("ok") and not was_ok:
                            executed += 1
                        # Update the tool-result message already in history.
                        for msg in reversed(messages):
                            if msg.get("role") == "tool" and msg.get("tool_call_id") == b.id:
                                msg["content"] = json.dumps(res, ensure_ascii=False, default=str)
                                break
                        if res.get("ok") is False:
                            log_event("tool_error", {"tool": b.name, "error": res.get("error")}, phase="error")

        if approved:
            sink.info(_format_execution_summary(block, results))

        i = j


def _format_execution_summary(
    block: list[ToolCallSpec],
    results: dict[str, dict[str, Any]],
) -> str:
    completed = sum(1 for b in block if results.get(b.id, {}).get("ok"))
    failed = [
        results.get(b.id, {})
        for b in block
        if not results.get(b.id, {}).get("ok")
        and not results.get(b.id, {}).get("declined")
    ]
    parts = [f"Completed {completed}/{len(block)} operation(s)."]
    if failed:
        parts.append(f"{len(failed)} failed.")
        first_error = str(failed[0].get("error") or "").strip()
        if first_error:
            if len(first_error) > 160:
                first_error = first_error[:157] + "..."
            parts.append(f"First error: {first_error}")
    return "  ".join(parts)


# ---------------------------------------------------------------------------
# Coding model auto-routing
# ---------------------------------------------------------------------------

_CODING_PHRASES: list[str] = [
    "write a ", "write the ", "write an ", "write me ",
    "build a ", "build me ", "build the ",
    "create a script", "create a file", "create a webpage", "create a website",
    "generate html", "generate css", "generate javascript", "generate python",
    "generate a ", "generate the ",
    "code a ", "code the ",
    "make a website", "make a script", "make a page", "make a webpage",
    "scaffold",
]
_CODING_EXTENSIONS: list[str] = [".html", ".css", ".js", ".py", ".ts", ".jsx", ".tsx"]


def _detect_coding_intent(message: str) -> bool:
    """Deterministic check for coding-related user messages. No LLM, no ML."""
    lower = message.casefold()
    for phrase in _CODING_PHRASES:
        if phrase in lower:
            return True
    for ext in _CODING_EXTENSIONS:
        if ext in lower:
            return True
    return False


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

    # Auto-route to coding model if configured and applicable
    coding_model = get_coding_model()
    routed = False
    saved_model: str | None = None

    if (
        coding_model
        and not user_has_set_model()
        and _detect_coding_intent(user_text)
    ):
        saved_model = get_xai_model()
        set_runtime_model(coding_model, user_initiated=False)
        routed = True
        sink.info(f"Using coding model ({coding_model}) for this request.")
        log_event("coding_model_routed", {"coding_model": coding_model})

    try:
        _run_turn(messages, user_text, sink)
    finally:
        if routed and saved_model is not None:
            set_runtime_model(saved_model, user_initiated=False)


def _run_turn(
    messages: list[dict[str, Any]],
    user_text: str,
    sink: OutputSink,
) -> None:
    """Inner turn logic — separated so handle_user_turn can wrap with model routing."""
    api_key = get_xai_api_key()
    model = get_xai_model()
    tools = get_tool_definitions()
    original_messages = [dict(m) for m in messages]
    _ensure_runtime_system_prompt(messages)

    # Streaming support — duck-typed so CLI sinks (which lack these) still work.
    on_delta = getattr(sink, "stream_delta", None)
    stop_event = getattr(sink, "stop_event", None)
    if not isinstance(stop_event, threading.Event):
        stop_event = None

    turn_start = len(messages)
    messages.append({"role": "user", "content": user_text})
    log_event("user_message", {"length": len(user_text)}, user_request=user_text)

    max_steps = get_max_tool_loops()
    clipboard_retry_requested = False
    for _ in range(max_steps):
        # Signal to the sink that a new LLM response is starting.
        getattr(sink, "start_stream", lambda: None)()
        try:
            result = _chat_with_optional_web_tools(
                api_key, model, messages, tools, sink,
                on_delta=on_delta, stop_event=stop_event,
            )
        except RuntimeError as e:
            getattr(sink, "cancel_stream", lambda: None)()
            sink.error(f"[error] {e}")
            log_event("api_error", {"error": str(e)}, phase="error")
            messages[:] = original_messages
            return

        # Report token usage to the sink if it tracks such things.
        if result.usage:
            _usage_cb = getattr(sink, "usage", None)
            if _usage_cb:
                _usage_cb(result.usage, model)

        # Honour stop requests between LLM calls.
        if stop_event and stop_event.is_set():
            getattr(sink, "cancel_stream", lambda: None)()
            return

        if result.tool_calls:
            # Show the model's plan/progress text before processing tools
            if result.content and result.content.strip():
                sink.assistant(result.content.strip())
            else:
                # No text preamble — discard any dangling stream header.
                getattr(sink, "cancel_stream", lambda: None)()
            _process_tool_calls(messages, result.tool_calls, sink, result.content)
            continue

        text = (result.content or "").strip()
        if text:
            missing_clipboard_write = (
                _requested_clipboard_write(user_text)
                and _claims_clipboard_write(text)
                and not _successful_tool_since(messages, turn_start, "copy_to_clipboard")
            )
            if missing_clipboard_write and not clipboard_retry_requested:
                clipboard_retry_requested = True
                getattr(sink, "cancel_stream", lambda: None)()
                sink.info(
                    "[note] Clipboard copy was requested, but no clipboard tool ran. "
                    "Asking the assistant to perform the clipboard step now."
                )
                messages.append({"role": "assistant", "content": text})
                messages.append({
                    "role": "system",
                    "content": (
                        "Correction: the user requested a clipboard copy, but no successful "
                        "copy_to_clipboard tool call has run in this turn. You must now call "
                        "copy_to_clipboard with the exact clean summary text, or clearly state "
                        "that you cannot complete the clipboard step. Do not claim the clipboard "
                        "was changed unless the tool result says ok=true."
                    ),
                })
                log_event("clipboard_missing_tool_retry", {"assistant_text_length": len(text)}, phase="retry")
                continue
            if missing_clipboard_write:
                getattr(sink, "cancel_stream", lambda: None)()
                sink.error("[warning] Clipboard copy was requested, but no successful clipboard tool call ran.")
                log_event("clipboard_missing_tool_warning", {"assistant_text_length": len(text)}, phase="error")
                return
            sink.assistant(text)
        else:
            sink.assistant("(empty response)")
        log_event("assistant_done", {"has_content": bool(text)})
        return
    else:
        sink.error("[error] Tool loop limit reached; stopping this turn.")
        log_event("tool_loop_limit", {}, phase="error")
        messages[:] = original_messages


def get_startup_info() -> dict[str, Any]:
    """Return info dict for startup display."""
    return {
        "model": get_xai_model(),
        "coding_model": get_coding_model(),
        "desktop": str(get_default_desktop_path()),
        "allowed_roots": [str(p) for p in get_allowed_roots()],
        "dry_run": is_dry_run(),
        "web_search": web_search_enabled(),
        "max_tool_loops": get_max_tool_loops(),
        "verbose": is_verbose(),
    }
