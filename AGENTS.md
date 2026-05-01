# AGENTS.md

## Project Overview

`xai-computer` is a local Windows desktop assistant that uses xAI Grok for reasoning and vetted Python functions for execution. It has both terminal and Tkinter GUI frontends, a shared orchestration layer, approval-gated mutations, undo support, structured logging, browser/desktop/process automation, and a deterministic shell safety gate. Treat safety behavior as product-critical.

## Architecture Map

- `app.py` launches the CLI; `cli.py` owns terminal I/O, slash commands, and CLI approval rendering.
- `gui.py` launches the Tkinter app and implements the GUI sink for streaming, approvals, retries, history, and session state.
- `core.py` is the main turn loop. It calls the xAI client, handles tool calls, batches mutating actions into `ApprovalCard`s, dispatches tools, and rolls results back into the conversation.
- `xai_client.py` is the minimal HTTPS chat-completions client; `xai_structured.py` uses the xAI SDK only for optional structured UI enrichment.
- `schemas.py` contains the system prompt, model-facing tool JSON schemas, and `MUTATING_TOOL_NAMES`. Keep schemas and dispatch behavior in sync.
- `tools.py` is the central local-tool dispatch layer. It delegates browser, desktop, editor, and process behavior to the corresponding `*_tools.py` modules.
- `safety.py`, `shell_guard.py`, and `undo.py` are the safety core: allowed-root path validation, deterministic shell classification, secret redaction, output truncation, and reversible action tracking.
- `logger.py` writes append-only JSONL logs under `logs/`; runtime state and undo/session data live under `state/`.
- `tests/` covers safety, shell guard behavior, tool dispatch, undo, editor/process/desktop/browser helpers, xAI client retry behavior, routing, and GUI/session behavior.

## Development Commands

Use PowerShell from the repo root unless a task says otherwise.

```powershell
pip install -r requirements.txt
playwright install chromium
python app.py
python gui.py
python -m pytest -q
```

Install `pytest` separately if needed; it is listed as an optional development dependency in `requirements.txt`.

## Safety Rules For Agents

- Never bypass approval cards for mutating actions. Filesystem writes, shell commands, process control, browser control, and desktop input must remain approval-gated through `core.py`.
- Do not loosen `shell_guard.py` casually. The shell classifier is deterministic by design; blocked commands, structural blocking, secret redaction, and output truncation are safety boundaries.
- Do not use arbitrary shell execution in app code. `run_command` must go through `shell_guard.py`, `subprocess.run(..., shell=False)`, bounded timeouts, validated working directories, and redacted/truncated output.
- Preserve allowed-root validation in `safety.py`. Read-only and mutating filesystem tools should resolve paths, reject traversal, reject protected system locations, and stay inside configured roots.
- Preserve dry-run and undo behavior for mutating filesystem operations. New reversible mutations should record undo entries when not in dry-run mode.
- Keep browser, desktop, and process automation explicit and bounded. Dangerous hotkeys and high-risk interactions should remain blocked or approval-gated.
- Do not commit or depend on runtime artifacts: `.env`, `logs/`, `state/`, `.pytest_cache/`, `__pycache__/`, virtualenvs, and generated download/session files.

## Testing Guidance

- Run focused tests for the modules you touch, then run `python -m pytest -q` when practical.
- Add or update tests when changing behavior in `core.py`, `schemas.py`, `tools.py`, `safety.py`, `shell_guard.py`, `undo.py`, or any frontend approval/session flow.
- For GUI changes, prefer tests around state and rendering helpers where possible, and manually launch `python gui.py` for interaction checks when the environment supports Tk.
- For browser automation changes, account for Playwright setup and keep downloads isolated under `state/browser_downloads/`.

## Code Style

- Target Python 3.11+ and use type hints for new public helpers and non-trivial internal helpers.
- Prefer simple functions and existing local patterns over new abstractions. Keep edits tightly scoped to the requested behavior.
- Use the standard library where it is already sufficient; avoid adding dependencies without a clear need.
- Keep comments sparse and useful. Comment safety-sensitive or non-obvious logic, not routine assignments.
- Keep model-facing schemas, tool dispatch names, README/docs, and tests aligned when adding or changing tools.

## Working Tree Etiquette

- The repo may already contain user or agent work in progress. Inspect `git status` and relevant diffs before editing files that are already modified.
- Do not revert, rewrite, or clean up unrelated changes unless explicitly asked.
- Be especially careful around ongoing work in `config.py`, `core.py`, `gui.py`, `xai_client.py`, `tests/test_core.py`, and `tests/test_sessions.py` if they are dirty.
- Avoid broad formatting-only churn. This codebase is safety-sensitive; small, reviewable patches are easier to trust.
