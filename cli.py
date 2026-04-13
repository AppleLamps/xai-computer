"""CLI rendering layer and slash-command handling.

Implements the OutputSink protocol from core.py and handles all terminal I/O.
"""

from __future__ import annotations

from typing import Any

from config import (
    MODELS,
    get_last_working_folder,
    get_xai_model,
    is_dry_run,
    is_verbose,
    set_dry_run,
    set_runtime_model,
    set_verbose,
)
from core import ApprovalCard, get_startup_info, handle_user_turn
from logger import SESSION_ID
from schemas import SYSTEM_PROMPT
from tools import (
    analyze_directory,
    directory_tree,
    largest_files,
    recent_files,
)
from undo import get_history, undo_last, undo_n


# ---------------------------------------------------------------------------
# Terminal output sink
# ---------------------------------------------------------------------------


class TerminalSink:
    """Implements OutputSink for the terminal."""

    def info(self, text: str) -> None:
        print(text)

    def error(self, text: str) -> None:
        print(text)

    def assistant(self, text: str) -> None:
        print(f"\nAssistant:\n{text}\n")

    def plan(self, card: ApprovalCard) -> None:
        """Render a structured approval card."""
        dry_tag = " [DRY RUN]" if card.dry_run else ""
        risk_tag = f"  Risk: {card.risk_level.upper()}" if card.risk_level else ""

        print(f"\n{'=' * 60}")
        print(f"  APPROVAL REQUIRED{dry_tag}")
        print(f"{'=' * 60}")
        if card.affected_root:
            print(f"  Scope: {card.affected_root}")
        print(f"  {card.summary}{risk_tag}")
        print(f"{'-' * 60}")
        for action in card.actions:
            risk_marker = " [!]" if action.risk == "medium" else ""
            print(f"  {action.index}. {action.label}{risk_marker}")
        print(f"{'=' * 60}\n")

    def progress(self, text: str) -> None:
        print(text)

    def prompt_confirmation(self, prompt_text: str) -> str:
        return input(prompt_text)


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------

SLASH_COMMANDS: dict[str, str] = {
    "/help": "Show available commands",
    "/status": "Show current session status",
    "/history": "Show undo history for this session",
    "/undo": "Undo the last reversible action  (or /undo N to undo last N)",
    "/dry-on": "Enable dry-run mode (simulate only)",
    "/dry-off": "Disable dry-run mode (execute normally)",
    "/model": "Show or switch model (/model fast, /model quality)",
    "/mode": "Switch output mode (/mode concise, /mode verbose)",
    "/analyze": "Analyze a directory (/analyze <path>)",
    "/tree": "Show directory tree (/tree <path> [depth])",
    "/recent": "Show recent files (/recent <path> [limit])",
    "/largest": "Show largest files (/largest <path> [limit])",
    "/quit": "Exit the assistant",
}


def _handle_help(sink: TerminalSink) -> None:
    sink.info("\nAvailable commands:")
    for cmd, desc in SLASH_COMMANDS.items():
        sink.info(f"  {cmd:12s}  {desc}")
    sink.info("")


def _handle_status(sink: TerminalSink) -> None:
    info = get_startup_info()
    dry_label = "ON" if info["dry_run"] else "OFF"
    web_label = "ON" if info["web_search"] else "OFF"
    mode_label = "verbose" if info["verbose"] else "concise"
    lwf = get_last_working_folder()
    lwf_label = str(lwf) if lwf else "(none)"
    cm = info.get("coding_model")
    cm_label = f"{cm} (auto-routing enabled)" if cm else "not configured"
    sink.info(f"\n  Session:          {SESSION_ID}")
    sink.info(f"  Model:            {info['model']}")
    sink.info(f"  Coding model:     {cm_label}")
    sink.info(f"  Output mode:      {mode_label}")
    sink.info(f"  Dry run:          {dry_label}")
    sink.info(f"  Web search:       {web_label}")
    sink.info(f"  Desktop:          {info['desktop']}")
    sink.info(f"  Last folder:      {lwf_label}")
    sink.info(f"  Allowed roots:    {', '.join(info['allowed_roots'])}")
    sink.info(f"  Max tool loops:   {info['max_tool_loops']}")
    sink.info("")


def _handle_history(sink: TerminalSink) -> None:
    records = get_history(20)
    if not records:
        sink.info("No actions in this session's undo history.")
        return
    sink.info(f"\nUndo history (newest first, session {SESSION_ID}):")
    for i, r in enumerate(records, 1):
        action = r.get("action", "?")
        undone = " [UNDONE]" if r.get("undone") else ""
        ts = r.get("ts", "")[:19]
        if action in ("move_file", "rename_file", "organize_move"):
            sink.info(f"  {i}. [{ts}] {action}: {r.get('source', '?')} -> {r.get('destination', '?')}{undone}")
        elif action == "create_folder":
            sink.info(f"  {i}. [{ts}] {action}: {r.get('path', '?')}{undone}")
        else:
            sink.info(f"  {i}. [{ts}] {action}{undone}")
    sink.info("")


def _handle_undo(sink: TerminalSink, args: str = "") -> None:
    count_str = args.strip()
    if count_str:
        try:
            n = int(count_str)
        except ValueError:
            sink.info(f"Usage: /undo [N]  — undo the last N actions (default: 1)")
            return
        if n < 1:
            sink.info("N must be a positive integer.")
            return
        results = undo_n(n)
        for result in results:
            if result.get("ok"):
                action = result.get("action", "")
                note = result.get("note", "")
                if action == "create_folder":
                    sink.info(f"Undone: removed empty folder {result.get('removed', '?')}")
                else:
                    sink.info(f"Undone: {result.get('from', '?')} -> {result.get('restored_to', '?')}{note}")
            else:
                sink.info(f"Cannot undo: {result.get('error', 'unknown reason')}")
        return

    result = undo_last()
    if result.get("ok"):
        action = result.get("action", "")
        note = result.get("note", "")
        if action == "create_folder":
            sink.info(f"Undone: removed empty folder {result.get('removed', '?')}")
        else:
            sink.info(f"Undone: {result.get('from', '?')} -> {result.get('restored_to', '?')}{note}")
    else:
        sink.info(f"Cannot undo: {result.get('error', 'unknown reason')}")


def _handle_dry_on(sink: TerminalSink) -> None:
    set_dry_run(True)
    sink.info("Dry-run mode: ON. Mutating actions will be simulated only.")


def _handle_dry_off(sink: TerminalSink) -> None:
    set_dry_run(False)
    sink.info("Dry-run mode: OFF. Actions will execute normally.")


def _handle_model(args: str, sink: TerminalSink) -> None:
    arg = args.strip().lower()
    if not arg:
        sink.info(f"Current model: {get_xai_model()}")
        sink.info("Available presets:")
        for key, model_id in MODELS.items():
            marker = " (active)" if get_xai_model() == model_id else ""
            sink.info(f"  {key:10s}  {model_id}{marker}")
        sink.info('Usage: /model fast  OR  /model quality  OR  /model <full-model-id>')
        return

    if arg in MODELS:
        set_runtime_model(MODELS[arg])
        sink.info(f"Switched to: {MODELS[arg]}")
    else:
        set_runtime_model(arg)
        sink.info(f"Switched to custom model: {arg}")


def _handle_mode(args: str, sink: TerminalSink) -> None:
    arg = args.strip().lower()
    if arg == "concise":
        set_verbose(False)
        sink.info("Output mode: concise")
    elif arg == "verbose":
        set_verbose(True)
        sink.info("Output mode: verbose")
    else:
        current = "verbose" if is_verbose() else "concise"
        sink.info(f"Current mode: {current}")
        sink.info("Usage: /mode concise  OR  /mode verbose")


def _resolve_path_arg(arg: str) -> str:
    """Resolve a path argument, falling back to last working folder."""
    if arg:
        return arg
    lwf = get_last_working_folder()
    if lwf:
        return str(lwf)
    return ""


def _handle_analyze(args: str, sink: TerminalSink) -> None:
    path = _resolve_path_arg(args.strip())
    if not path:
        sink.info("Usage: /analyze <path>")
        return
    result = analyze_directory(path)
    if not result.get("ok"):
        sink.info(f"Error: {result.get('error')}")
        return
    sink.info(f"\nAnalysis of {result['path']}:")
    sink.info(f"  Files: {result['total_files']}  |  Folders: {result['total_dirs']}  |  Size: {result['total_size']}")
    if result.get("type_breakdown"):
        sink.info("  Type breakdown:")
        for entry in result["type_breakdown"]:
            sink.info(f"    {entry['type']:15s}  {entry['count']:4d} files  {entry['size']}")
    if result.get("duplicate_count", 0) > 0:
        sink.info(f"  Likely duplicates: {result['duplicate_count']}")
        for d in result["likely_duplicates"][:5]:
            sink.info(f"    {d['file']}  (original: {d['likely_original']})")
    sink.info("")


def _handle_tree(args: str, sink: TerminalSink) -> None:
    parts = args.strip().rsplit(None, 1)
    path_arg = parts[0] if parts else ""
    depth = 2
    if len(parts) == 2:
        try:
            depth = int(parts[1])
            path_arg = parts[0]
        except ValueError:
            path_arg = args.strip()

    path = _resolve_path_arg(path_arg)
    if not path:
        sink.info("Usage: /tree <path> [depth]")
        return
    result = directory_tree(path, depth)
    if not result.get("ok"):
        sink.info(f"Error: {result.get('error')}")
        return
    sink.info(f"\n{result['tree']}\n  ({result['entries']} entries)\n")


def _handle_recent(args: str, sink: TerminalSink) -> None:
    parts = args.strip().rsplit(None, 1)
    path_arg = parts[0] if parts else ""
    limit = 15
    if len(parts) == 2:
        try:
            limit = int(parts[1])
            path_arg = parts[0]
        except ValueError:
            path_arg = args.strip()

    path = _resolve_path_arg(path_arg)
    if not path:
        sink.info("Usage: /recent <path> [limit]")
        return
    result = recent_files(path, limit)
    if not result.get("ok"):
        sink.info(f"Error: {result.get('error')}")
        return
    sink.info(f"\nRecent files in {result['path']}:")
    for f in result["files"]:
        sink.info(f"  {f['modified']}  {f['size']:>10s}  {f['name']}")
    sink.info("")


def _handle_largest(args: str, sink: TerminalSink) -> None:
    parts = args.strip().rsplit(None, 1)
    path_arg = parts[0] if parts else ""
    limit = 10
    if len(parts) == 2:
        try:
            limit = int(parts[1])
            path_arg = parts[0]
        except ValueError:
            path_arg = args.strip()

    path = _resolve_path_arg(path_arg)
    if not path:
        sink.info("Usage: /largest <path> [limit]")
        return
    result = largest_files(path, limit)
    if not result.get("ok"):
        sink.info(f"Error: {result.get('error')}")
        return
    sink.info(f"\nLargest files in {result['path']}:")
    for f in result["files"]:
        sink.info(f"  {f['size']:>10s}  {f['name']}")
    sink.info("")


def try_slash_command(user_text: str, sink: TerminalSink) -> bool:
    """Handle a slash command. Returns True if handled, False if not a command."""
    text = user_text.strip()
    if not text.startswith("/"):
        return False

    parts = text.split(None, 1)
    cmd = parts[0].lower()
    args = parts[1] if len(parts) > 1 else ""

    if cmd in ("/help", "/h"):
        _handle_help(sink)
        return True
    if cmd == "/status":
        _handle_status(sink)
        return True
    if cmd in ("/history", "/hist"):
        _handle_history(sink)
        return True
    if cmd == "/undo":
        _handle_undo(sink, args)
        return True
    if cmd == "/dry-on":
        _handle_dry_on(sink)
        return True
    if cmd == "/dry-off":
        _handle_dry_off(sink)
        return True
    if cmd == "/model":
        _handle_model(args, sink)
        return True
    if cmd == "/mode":
        _handle_mode(args, sink)
        return True
    if cmd == "/analyze":
        _handle_analyze(args, sink)
        return True
    if cmd == "/tree":
        _handle_tree(args, sink)
        return True
    if cmd == "/recent":
        _handle_recent(args, sink)
        return True
    if cmd == "/largest":
        _handle_largest(args, sink)
        return True
    if cmd in ("/quit", "/exit", "/q"):
        return False  # let the main loop handle exit

    sink.info(f'Unknown command: {cmd}. Type /help for available commands.')
    return True


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def run_cli() -> None:
    """Run the interactive CLI chat loop."""
    from config import get_xai_api_key

    sink = TerminalSink()

    api_key = get_xai_api_key()
    if not api_key:
        sink.error("Missing XAI_API_KEY. Copy .env.example to .env and set your key.")
        return

    info = get_startup_info()
    sink.info("Local Windows assistant (xAI Grok). Type /help for commands.\n")
    sink.info(f"  Model:    {info['model']}")
    sink.info(f"  Desktop:  {info['desktop']}")
    sink.info(f"  Session:  {SESSION_ID}")
    dry_tag = " [DRY RUN]" if info["dry_run"] else ""
    sink.info(f"  Roots:    {', '.join(info['allowed_roots'])}{dry_tag}\n")

    messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]

    while True:
        try:
            user_text = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            sink.info("\nBye.")
            break
        if not user_text:
            continue
        if user_text.lower() in {"exit", "quit", "/exit", "/quit", "/q"}:
            sink.info("Bye.")
            break

        # Handle slash commands locally — never send to model
        if user_text.startswith("/"):
            if not try_slash_command(user_text, sink):
                if user_text.strip().lower() in {"/quit", "/exit", "/q"}:
                    sink.info("Bye.")
                    break
            continue

        # Normal conversation turn
        handle_user_turn(messages, user_text, sink)
