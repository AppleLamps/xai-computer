# Architecture

This document explains how the modules fit together and how a user message flows through the system.

## Module Responsibilities

### `app.py` — CLI entry point

Calls `cli.run_cli()`. Nothing else. Exists so the CLI launch command is `python app.py`.

### `gui.py` — GUI entry point

Tkinter-based desktop interface. Implements `GuiSink` (the `OutputSink` protocol from `core.py`). Runs model calls in a daemon worker thread; all Tkinter updates happen on the main thread via `root.after()`. The approval flow uses a `threading.Event` to block the worker thread while the user clicks Approve or Cancel. Launch with `python gui.py`.

### `cli.py` — Terminal interface

Owns all terminal I/O. Implements `TerminalSink` (the `OutputSink` protocol from `core.py`). Handles slash commands locally — these are never sent to the model. Delegates natural-language input to `core.handle_user_turn()`.

### `core.py` — Orchestration

The conversation engine. Manages the message list, calls the xAI API, and processes tool calls. Contains the `OutputSink` protocol that both CLI and GUI implement.

Key types:
- `PlannedAction` — one step in a pending plan (tool name, arguments, label, risk level)
- `ApprovalCard` — everything the UI needs to render an approval prompt (actions, scope, risk, dry-run flag)

When the model returns tool calls, `core.py` splits them into read-only (executed immediately) and mutating (batched into an `ApprovalCard`, shown to user, executed only after approval).

### `xai_client.py` — API client

Minimal HTTPS client for the xAI chat completions endpoint. Uses only `urllib` from the standard library. Parses tool calls from the response. No external HTTP library required.

### `schemas.py` — Tool definitions

Contains the system prompt and all 17 tool JSON schemas sent to the API. Also defines `MUTATING_TOOL_NAMES` — the set of tools that require confirmation.

### `tools.py` — Local tool implementations

All filesystem, shell, and browser operations. Each tool is a plain function that returns a `dict[str, Any]` result. Mutating tools check `is_dry_run()` before executing and record undo entries via `undo.py` (except `run_command`, which is not undoable). The `dispatch_tool()` function maps tool names to handlers.

### `shell_guard.py` — Shell command safety gate

Deterministic classifier for shell commands. Every proposed command is classified into `blocked`, `safe`, or `risky` using static rules only — no LLM inference. Blocked commands are rejected unconditionally. Also handles working directory validation, output truncation, and secret redaction. See [`docs/SHELL_SAFETY.md`](SHELL_SAFETY.md) for the full safety model.

### `safety.py` — Path validation

Centralizes all path safety logic:
- `require_allowed_path()` — resolve, check traversal, check blocked locations, check allowed roots
- `is_affirmative_confirmation()` — strict regex matching for approval text
- `is_hidden_name()` / `is_system_or_protected_name()` — skip protected files during organization

### `config.py` — Configuration

Loads `.env`, exposes all configuration getters, and holds runtime mutable state (current model, dry-run flag, verbose mode, last working folder). No config state is persisted across sessions — it resets on restart.

### `logger.py` — Structured logging

Append-only JSONL logging to `logs/actions.log`. Every log record includes a session ID, timestamp, event name, dry-run flag, and optional fields for phase, tool name, parameters, and result.

### `undo.py` — Undo stack

Records reversible actions in `state/undo_history.jsonl`. Each record stores the action type, source, destination, session ID, and an `undone` flag. `undo_last()` finds the most recent un-undone record from the current session and reverses it.

## Request Flow

```
1. User types natural language      "organize my desktop by type"
       |
2. cli.py routes to core.py         (slash commands handled locally instead)
       |
3. core.py appends user message     messages.append({"role": "user", ...})
       |
4. core.py calls xAI API            POST api.x.ai/v1/chat/completions
       |
5. Model responds                   tool_calls: [preview_plan_for_desktop_cleanup]
       |
6. core.py dispatches read-only     tools.preview_plan_for_desktop_cleanup()
       |
7. Result appended to messages      messages.append({"role": "tool", ...})
       |
8. core.py calls API again          Model sees the preview result
       |
9. Model responds                   tool_calls: [organize_desktop_by_type]
       |
10. core.py detects mutating tool   Builds ApprovalCard, renders via sink.plan()
       |
11. User approves                   Types "yes"
       |
12. core.py executes                tools.organize_desktop_by_type()
       |                            undo.record_organize_move() for each file
       |                            logger.log_event() for each action
       |
13. Result appended to messages     messages.append({"role": "tool", ...})
       |
14. core.py calls API again         Model sees results
       |
15. Model responds with text        "Done. 12 files organized into 4 folders."
       |
16. core.py renders via sink        sink.assistant(text)
```

## Design Decisions

**Why no external HTTP library?** The xAI API is a single POST endpoint. `urllib` handles it fine. Avoiding `requests` or `httpx` keeps the dependency list at one package (`python-dotenv`).

**Why `OutputSink` as a protocol?** So `core.py` never imports `cli.py` or `gui.py`. Both frontends implement the same six methods (`info`, `error`, `assistant`, `plan`, `progress`, `prompt_confirmation`) without touching orchestration logic. The CLI's `TerminalSink` uses `print()` and `input()`. The GUI's `GuiSink` uses `root.after()` for thread-safe Tkinter updates and a `threading.Event` to block the worker thread during approval.

**Why JSONL for logs and undo?** Append-only writes are safe against crashes. Each line is independently parseable. No database dependency.

**Why session-scoped undo?** Cross-session undo would require tracking whether external tools (Explorer, other apps) modified files between sessions. Session scope keeps undo reliable.
