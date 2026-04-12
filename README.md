# xai-computer

A local Windows desktop assistant that uses **xAI Grok** as the reasoning layer and **vetted Python functions** as the execution layer. You chat in a terminal; Grok decides which tools to call; the app runs them locally after you approve. There is no shell execution, no model-generated code execution, and no file deletion.

## What It Can Do

- **Inspect folders** — list contents, show directory trees, find files by name
- **Analyze directories** — file counts by type, total sizes, duplicate detection
- **Preview organization plans** — see exactly what would move before anything changes
- **Move and rename files** — with explicit approval, collision-safe naming, and undo
- **Organize folders** — by file type, by month, or by year (desktop or any allowed folder)
- **Show recent or largest files** — quick answers about what's taking up space
- **Read small text files** — peek at file contents (capped at 100 KB)
- **Write files** — create or update text/code files within allowed folders, with backup on overwrite and undo support
- **Create folders** — with undo support
- **Run shell commands** — constrained by a deterministic allowlist; dangerous patterns blocked unconditionally; all commands require confirmation
- **Open URLs** — launches your default browser
- **Web search** — optional xAI-backed web search if configured and supported by the model

## What It Cannot Do

- **Delete files or folders** — no delete tool exists
- **Run arbitrary shell commands** — only allowlisted or confirmed commands run; dangerous patterns (rm, del, format, pipe-to-shell, etc.) are blocked unconditionally and cannot be overridden, even by user approval
- **Run model-generated code** — the model's output is never evaluated as code
- **Access files outside allowed roots** — mutations are restricted to configured directories
- **Silently overwrite files** — collisions get `_dup1`, `_dup2`, etc. suffixes
- **Operate autonomously** — every mutating action requires explicit approval
- **Automate the browser** — it can open a URL, not click or fill forms
- **Run as a background service** — it is an interactive terminal session

## Safety Model

The assistant enforces multiple safety layers:

**Allowed roots.** File mutations (move, rename, create, organize) only work inside configured directories. By default these are your Desktop, Documents, and Downloads folders. Read-only tools (list, analyze, search) use the same boundary. Override with `XAI_ASSISTANT_ALLOWED_ROOTS`.

**Path validation.** All paths are normalized and resolved before any operation. Path traversal (`..`) is rejected before resolution. Dangerous system locations (Windows, System32, Program Files, ProgramData, $Recycle.Bin) are blocked even if they somehow fall inside an allowed root.

**Approval before mutation.** Every mutating tool call is batched and shown in a structured approval card with a risk level (LOW or MEDIUM) before execution. You must type `yes` or `confirm` — anything else cancels. Ambiguous text is never treated as approval.

**Dry-run mode.** Toggle with `/dry-on`. Mutating actions simulate without touching the filesystem. Output is labeled `[DRY RUN]`.

**Undo.** Moves, renames, and folder creations are recorded in `state/undo_history.jsonl`. Undo moves files back to their original location; if that location is occupied, the file is restored with a `_restored1` suffix. Empty folders created by the app can be undone. Undo never overwrites existing files and never deletes non-empty folders. Undo is scoped to the current session.

**Hidden and system files.** Desktop organization skips dotfiles (`.gitignore`, `.env`), Office lock files (`~$*.docx`), and known system files (`desktop.ini`, `Thumbs.db`, `NTUSER.DAT`).

**Read limits.** `read_text_file` caps at 100 KB of content and refuses files over 10 MB. `directory_tree` caps at depth 5 and 200 entries.

**Shell execution.** Commands go through a deterministic four-tier classifier (`shell_guard.py`). Dangerous commands are blocked unconditionally — no user override. Safe commands require confirmation. Unknown commands require confirmation with a HIGH-risk warning. `shell=True` is never used. Output is redacted for secrets and truncated to 200 lines. Shell commands are not undoable. See [`docs/SHELL_SAFETY.md`](docs/SHELL_SAFETY.md).

## Architecture Overview

```
User input
    |
    v
  cli.py  ── slash commands handled locally (never sent to model)
    |
    v
  core.py ── builds messages, calls xAI API, processes tool calls
    |            |
    |            v
    |       xai_client.py ── HTTPS POST to api.x.ai/v1/chat/completions
    |            |
    |            v
    |       Model returns text or tool_calls
    |            |
    v            v
  core.py ── dispatches tool calls through tools.py
    |            |
    |            +── read-only tools: execute immediately, return results
    |            +── mutating tools: batch into approval card, wait for user
    |                    |
    |                    v
    |               User approves? ── yes ── execute, record undo ── return result
    |                                  no ── return declined      ── return result
    v
  Results appended to conversation ── loop back to model or render final response
```

**Structured outputs.** On Grok 4 models, the app uses the xAI SDK's `chat.parse()` to get type-safe structured responses for shell command explanations and execution summaries. These are used for richer UI rendering only — never for safety decisions. If structured output is unavailable (non-Grok-4 model, network error), the app falls back to existing behavior.

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for details on each module.

## Project Structure

```
app.py             CLI entry point
gui.py             GUI entry point (Tkinter)
cli.py             Terminal I/O, slash commands, approval rendering
core.py            Conversation loop, tool dispatch, ApprovalCard
tools.py           18 local tools (filesystem, analysis, write, shell, browser)
schemas.py         System prompt and tool JSON schemas for the API
safety.py          Path allowlisting, traversal protection, confirmation parsing
shell_guard.py     Deterministic shell command classifier (blocklist + allowlist)
structured_models.py  Pydantic models for structured output responses
xai_structured.py  xAI SDK wrapper for structured output calls (chat.parse)
config.py          .env loading, model presets, runtime state
logger.py          Append-only JSONL logging with session tracking
undo.py            Undo stack (record, reverse, history)
xai_client.py      Minimal HTTPS client for xAI chat completions
pyproject.toml     Pytest configuration
requirements.txt   Runtime dependency (python-dotenv)
.env.example       Template for environment variables
tests/             250 tests
logs/              Runtime action logs (created automatically)
state/             Undo history (created automatically)
docs/              Architecture and reference documentation
```

## Requirements

- Windows 10 or later
- Python 3.11 or later
- An xAI API key from [console.x.ai](https://console.x.ai/)

## Quick Start

```powershell
cd C:\Users\lucas\Desktop\xai-computer
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
```

Open `.env` in a text editor and paste your API key:

```
XAI_API_KEY=xai-your-key-here
```

Start the assistant (CLI):

```powershell
python app.py
```

Or launch the desktop GUI:

```powershell
python gui.py
```

## Configuration

All configuration is through environment variables in `.env`.

| Variable | Default | Description |
|---|---|---|
| `XAI_API_KEY` | *(required)* | Your xAI API key |
| `XAI_MODEL` | `grok-4-1-fast-reasoning` | Model ID. Use `/model` at runtime to switch |
| `XAI_ASSISTANT_ALLOWED_ROOTS` | Desktop; Documents; Downloads | Semicolon-separated absolute paths for file operations |
| `XAI_ASSISTANT_DESKTOP` | *(auto-detected)* | Override desktop path (useful for OneDrive redirection) |
| `XAI_ENABLE_WEB_SEARCH` | `0` | Set to `1` to enable xAI built-in web search |
| `XAI_MAX_TOOL_LOOPS` | `12` | Max tool-call round-trips per user turn (1-50) |
| `XAI_CODING_MODEL` | *(not set)* | Auto-switch to this model for coding tasks (e.g., `grok-code-fast-1`) |
| `XAI_SHELL_ALLOWLIST_EXTRA` | *(empty)* | Comma-separated commands to add to the SAFE shell tier |

**Model presets** (switchable at runtime with `/model`):

| Preset | Model ID | Use case |
|---|---|---|
| `fast` | `grok-4-1-fast-reasoning` | Lower cost, faster responses |
| `quality` | `grok-4.20-0309-reasoning` | Maximum reasoning quality |
| `code` | `grok-code-fast-1` | Fastest and cheapest for code generation |

## Slash Commands

Commands are handled locally and never sent to the model.

| Command | Description | Example |
|---|---|---|
| `/help` | List all commands | `/help` |
| `/status` | Show session info: model, mode, roots, dry-run | `/status` |
| `/model` | Show or switch model | `/model quality` |
| `/mode` | Switch verbose or concise output | `/mode concise` |
| `/dry-on` | Enable dry-run mode | `/dry-on` |
| `/dry-off` | Disable dry-run mode | `/dry-off` |
| `/undo` | Undo the last reversible action | `/undo` |
| `/history` | Show undo history for this session | `/history` |
| `/analyze` | Analyze a directory (types, sizes, duplicates) | `/analyze C:\Users\you\Desktop` |
| `/tree` | Show directory tree | `/tree C:\Users\you\Documents 3` |
| `/recent` | Show most recently modified files | `/recent C:\Users\you\Downloads` |
| `/largest` | Show largest files | `/largest C:\Users\you\Desktop 20` |
| `/quit` | Exit the assistant | `/quit` |

Path arguments fall back to the last folder you inspected if omitted.

## Local Tools

These are the functions the model can call. Read-only tools run immediately. Mutating tools require your approval.

### Read-Only (no approval needed)

| Tool | What it does |
|---|---|
| `list_directory` | List files and folders at a path |
| `analyze_directory` | File counts by type, total size, duplicate detection |
| `largest_files` | Show the biggest files in a directory |
| `file_type_summary` | Aggregate sizes by file extension and category |
| `read_text_file` | Read the beginning of a text file (capped at 100 KB) |
| `search_files` | Find files by name substring (case-insensitive) |
| `recent_files` | Most recently modified files in a directory |
| `directory_tree` | Indented tree view (max depth 5, max 200 entries) |
| `preview_plan_for_desktop_cleanup` | Preview how desktop files would be grouped by type |
| `preview_organize_folder` | Preview organization of any folder (by type, month, or year) |

### Mutating (approval required)

| Tool | What it does |
|---|---|
| `move_file` | Move a file to a new location |
| `rename_file` | Rename a file (basename only, no directory change) |
| `create_folder` | Create a folder and any missing parents |
| `organize_desktop_by_type` | Sort desktop files into category subfolders |
| `organize_folder` | Sort any allowed folder by type, month, or year |
| `write_file` | Create or update a text file (backup on overwrite, undo available) |
| `run_command` | Run a constrained shell command (see [Shell Safety](docs/SHELL_SAFETY.md)) |

### Browser

| Tool | What it does |
|---|---|
| `open_url` | Open an http(s) URL in the default browser |

## Example Workflows

**Inspect and organize the desktop:**

```
You: what's on my desktop?
Assistant: [calls list_directory, analyzes contents, reports summary]

You: clean up my desktop
Assistant: [calls preview_plan_for_desktop_cleanup, shows plan]

============================================================
  APPROVAL REQUIRED
============================================================
  Scope: C:\Users\you\Desktop
  1 organize operation(s)  Risk: MEDIUM [!]
------------------------------------------------------------
  1. ORGANIZE DESKTOP (default) [!]
============================================================

Approve? (yes / cancel): yes
  Done: organize_desktop_by_type
Completed 1/1 operation(s).
```

**Check what's using space:**

```
You: /largest C:\Users\you\Downloads
  Largest files in C:\Users\you\Downloads:
     1.2 GB  big-dataset.zip
   345.0 MB  installer.exe
    89.5 MB  video.mp4
    ...
```

**Organize by date:**

```
You: organize my Downloads folder by month
Assistant: [calls preview_organize_folder with mode=month, shows grouping]
Assistant: [calls organize_folder after your approval]
```

**Read a file:**

```
You: read the notes.txt on my desktop
Assistant: [calls read_text_file, shows content up to 5000 chars]
```

**Undo a mistake:**

```
You: /undo
Undone: C:\Users\you\Desktop\Images\photo.png -> C:\Users\you\Desktop\photo.png

You: /history
  1. [2026-04-12T14:30:22] move_file: ... -> ... [UNDONE]
  2. [2026-04-12T14:30:22] organize_move: ... -> ...
```

**Open a website:**

```
You: open perplexity.ai
Assistant: [calls open_url with https://www.perplexity.ai]
```

## Coding Workflows

The assistant can generate and write code files, making it useful for scaffolding projects and quick coding tasks.

**What it can do:**
- Generate and write HTML, CSS, JS, Python, or any text file
- Scaffold project folder structures (create folders + write files)
- Read existing code with `read_text_file`
- Run tests with `run_command` (e.g., `pytest`)
- Iterate on files: read, modify, write back with `overwrite=true`
- Open results in the browser with `open_url`

**How to use it:**
```
You: build a simple landing page in my Documents/projects/site folder
You: write a Python script that converts CSV to JSON, save it to my Desktop
You: read main.py and add error handling, then write it back
```

Use `/model code` for the fastest, cheapest code generation, or set `XAI_CODING_MODEL=grok-code-fast-1` in `.env` to auto-route coding requests to that model. When auto-routing is enabled, the app detects coding intent (keywords like "write a", "build a website", file extensions like `.py` or `.html`) and switches to the coding model for that turn only, then switches back. Auto-routing is skipped if you've manually selected a model with `/model`.

**Limitations:**
- No live preview server (write files, then open in browser manually)
- No real-time collaborative editing (write-then-read loop only)
- No binary files (images, compiled code, etc.)
- Content capped at 500 KB per write
- Overwrites require explicit `overwrite=true` and create a `.bak` backup

## Dry-Run and Undo

**Dry-run mode** lets you see what would happen without making changes:

```
/dry-on          # mutating tools simulate only, output labeled [DRY RUN]
/dry-off         # back to normal execution
```

**Undo** reverses the most recent action from this session:

```
/undo            # reverse last move, rename, or folder creation
/history         # see all reversible actions and their status
```

Undo guarantees:
- Never overwrites an existing file (uses `_restored1` suffix if the original path is occupied)
- Never deletes a non-empty folder
- Only reverses actions from the current session
- Reports clearly when undo is not possible and why

## Running Tests

```powershell
pip install pytest
python -m pytest tests/ -v
```

Current status: **250 tests passing** across 6 test modules covering path safety, traversal rejection, confirmation parsing, file classification, duplicate detection, all read-only tools, dry-run behavior, shell command classification (blocked/safe/risky tiers, chaining detection, subshell detection, output truncation, secret redaction, `shell=True` static check), undo recording and reversal, collision-safe restore, model switching, verbose mode, session state, and approval card construction.

## Troubleshooting

**"Missing XAI_API_KEY"** — Copy `.env.example` to `.env` and add your API key from [console.x.ai](https://console.x.ai/).

**"Path not allowed (outside approved locations)"** — The file you referenced is outside your allowed roots. Check `/status` to see your current roots. Override with `XAI_ASSISTANT_ALLOWED_ROOTS` in `.env`.

**"Path contains suspicious traversal"** — The path includes `..` sequences. Use absolute paths instead.

**"Path targets a protected system location"** — You tried to operate on Windows, System32, Program Files, or similar. These are always blocked.

**Web search not working** — Set `XAI_ENABLE_WEB_SEARCH=1` in `.env`. If the model or endpoint doesn't support it, the app falls back silently and logs the event. Check `/status` to see if web search is enabled.

**Nothing happens after the preview** — Mutating actions require explicit approval. Type `yes` or `confirm` at the prompt. Anything else (including just pressing Enter) cancels.

**Undo says "original path was occupied"** — Another file now exists at the original location. The restored file gets a `_restored1` suffix. Check `/history` for the exact restored path.

**Undo says "folder is not empty"** — Folder undo only works on empty folders. If files were added after creation, the folder cannot be automatically removed.

**`pytest` not found** — Install it: `pip install pytest`. It is not a runtime dependency.

**OneDrive desktop redirect** — If your Desktop folder is under OneDrive, set `XAI_ASSISTANT_DESKTOP` in `.env` to the actual path.

## Desktop GUI

A Tkinter-based GUI is available as an alternative to the CLI. It shares the same core orchestration, tools, safety model, and undo system.

```powershell
python gui.py
```

### GUI features

- **Chat area** — user and assistant messages with visual distinction, tool results and errors styled separately
- **Approval panel** — appears inline when mutating actions are proposed; shows scope, risk level, numbered actions, and Approve / Cancel buttons. Nothing executes until you click Approve.
- **Side panel controls** — model selector (fast / quality), dry-run toggle, verbose toggle, undo, history, clear chat
- **Status header** — current model, dry-run state, output mode, session ID
- **Non-blocking** — model calls run in a background thread; the UI stays responsive
- **Send with Enter** — Shift+Enter for newline; Send button disables while processing

### GUI limitations

- No file drag-and-drop
- No per-step partial approval (approve-all or cancel-all only)
- No system tray or background monitoring
- No packaged installer — run from source with `python gui.py`
- Visual design is plain and functional, not polished

### Safety in the GUI

The GUI enforces the same safety model as the CLI:
- Mutating tools always show an approval card before execution
- Dry-run mode is toggled via the side panel checkbox
- Path safety, traversal protection, and allowed roots are unchanged
- The GUI cannot bypass approval — the `GuiSink.prompt_confirmation()` method blocks the worker thread until the user clicks a button on the main thread

## Roadmap

Possible future directions (not committed):

- Background file monitoring with change notifications
- Per-step approval (approve or skip individual actions in a batch)
- Richer MIME-based file classification
- Responses API migration for newer xAI agentic features
- Stronger OneDrive and cloud-synced folder handling
- Packaged installer for the GUI
