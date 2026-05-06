# xai-computer

`xai-computer` is a local Windows computer agent powered by xAI Grok and a vetted Python tool layer. The primary product surface is the local browser web app, which provides a chat-first agent interface with progress narration, approval cards, session restore, outputs, model controls, and safety-aware local automation.

The project also includes a CLI for terminal workflows and a legacy Tkinter GUI kept as a side project. All interfaces share the same orchestration, tool dispatch, approval model, undo stack, path safety, logging, and shell guard.

## Highlights

- **Web-first local agent**: React/Vite frontend served by `web_server.py` at `127.0.0.1`.
- **Safety-first execution**: local tools are explicit Python functions; model output is never executed as code.
- **Approval-gated mutations**: file writes, desktop actions, browser actions, process control, and shell commands require approval unless session-only BYPASS ALL is enabled.
- **Hard safety blocks remain absolute**: BYPASS ALL only auto-approves approval cards; blocked shell commands, path validation, dangerous hotkeys, and permanent-delete restrictions still apply.
- **Undo support**: reversible file operations record undo entries and never overwrite existing files during restore.
- **Dedicated computer tools**: file search, metadata, text search, screenshots, OCR, clipboard, window control, process management, and Playwright browser automation.
- **Deterministic shell safety**: `run_command` uses a classifier and blocklist; dangerous commands are rejected before approval.

## Quick Start

Requirements:

- Windows 10 or later
- Python 3.11 or later
- Node.js 20 or later
- An xAI API key from [console.x.ai](https://console.x.ai/)

Install and configure:

```powershell
cd C:\Users\lucas\Desktop\xai-computer
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium
copy .env.example .env
```

Edit `.env` and set:

```env
XAI_API_KEY=xai-your-key-here
```

Start the main web app:

```powershell
python web_server.py --open
```

Or use the Windows launcher:

```powershell
.\Start-Web-Agent.ps1
```

For frontend development:

```powershell
cd web
npm install
npm run dev
```

For production-style local serving, build the web assets first:

```powershell
cd web
npm install
npm run build
cd ..
python web_server.py --open
```

## Interfaces

### Web App

The web app is the recommended interface. It provides:

- Chat-first transcript and fixed composer
- Compact status, model, dry-run, and BYPASS ALL indicators
- Controls drawer for settings, allowed roots, recent sessions, and diagnostics
- Outputs drawer for screenshots, generated/copied files, downloads, and clipboard summaries
- Stop button for cooperative cancellation
- Session restore from saved local session files

### CLI

Use the CLI for quick terminal sessions or debugging:

```powershell
python app.py
```

Useful commands include `/status`, `/model`, `/dry-on`, `/dry-off`, `/undo`, `/history`, `/recent`, `/largest`, `/tree`, and `/quit`.

### Tkinter GUI

The Tkinter GUI is still available, but it is no longer the primary product surface:

```powershell
python gui.py
```

## Configuration

Configuration is read from `.env`.

| Variable | Default | Description |
| --- | ---: | --- |
| `XAI_API_KEY` | required | xAI API key |
| `XAI_MODEL` | `grok-4.3-latest` | Default chat model |
| `XAI_CODING_MODEL` | unset | Optional coding-task override; normally leave unset because `grok-4.3-latest` is the only supported model |
| `XAI_ASSISTANT_ALLOWED_ROOTS` | Desktop; Documents; Downloads | Semicolon-separated roots for local file access |
| `XAI_ASSISTANT_DESKTOP` | auto-detected | Override the Desktop path |
| `XAI_ENABLE_WEB_SEARCH` | `0` | Enable xAI built-in web search when supported |
| `XAI_MAX_TOOL_LOOPS` | `24` | Tool/model loop limit before asking whether to continue |
| `XAI_SHELL_ALLOWLIST_EXTRA` | empty | Extra commands to classify as safe shell commands |

Available web model choices:

- `grok-4.3-latest`

## Safety Model

The agent is designed to be useful without being reckless.

**Allowed roots.** File reads and mutations are restricted to configured local roots. By default, these are the current user's Desktop, Documents, and Downloads folders.

**Path validation.** Paths are normalized and resolved before use. Traversal attempts and protected system locations such as Windows, System32, Program Files, ProgramData, and `$Recycle.Bin` are blocked.

**Approval before mutation.** Mutating tools are batched into approval cards. The web app can enable session-only BYPASS ALL, but that only auto-approves approval cards and does not disable hard safety checks.

**No permanent delete.** The only delete tool sends files to the Recycle Bin. There is no permanent delete tool.

**Undo.** Moves, renames, folder creation, writes, edits, and copies record undo entries where possible. Undo never overwrites existing files and never deletes non-empty folders.

**Shell guard.** Shell commands use `shell_guard.py`. Dangerous commands are blocked unconditionally; unknown or risky commands require approval. `shell=True` is never used by the app. See [Shell Safety](docs/SHELL_SAFETY.md).

**Untrusted content.** Files, webpages, screenshots, OCR text, and clipboard data are treated as data, not instructions.

## Local Tools

Read-only tools run immediately. Mutating or sensitive tools are approval-gated.

Representative read-only tools:

- `list_directory`, `directory_tree`
- `get_file_info`, `recent_files`, `largest_files`
- `search_files`, `recursive_find_files`, `search_file_contents`
- `read_text_file`, `read_file_range`
- `take_screenshot`, `get_screen_info`, `ocr_image`
- `list_windows`, `get_active_window`, `list_processes`
- `browser_extract_text`, `browser_wait_for`

Representative mutating or sensitive tools:

- `write_file`, `replace_in_file`, `append_file`, `apply_patch`
- `move_file`, `copy_file`, `rename_file`, `create_folder`, `delete_file_to_recycle_bin`
- `organize_desktop_by_type`, `organize_folder`
- `run_command`
- `focus_window`, `start_process`, `stop_process`
- `move_mouse`, `click`, `scroll`, `type_text`, `press_hotkey`
- `browser_navigate`, `browser_click`, `browser_fill`, `browser_press`, `browser_download`, `browser_screenshot`
- `copy_to_clipboard`, `read_clipboard`, `window_screenshot`

## Architecture

```text
User
  |
  v
Web app / CLI / Tkinter GUI
  |
  v
core.py
  - builds the runtime system prompt
  - calls xAI chat completions
  - renders narration and progress
  - batches approval-gated tools
  - records tool results in conversation history
  |
  v
tools.py and specialized tool modules
  - filesystem and analysis tools
  - editor tools
  - desktop tools
  - browser tools
  - process tools
  - shell guard
```

Important modules:

| Path | Purpose |
| --- | --- |
| `web_server.py` | Local HTTP API and static web app server |
| `web/` | React/Vite frontend |
| `core.py` | Turn orchestration, tool loop, approvals, narration, cancellation |
| `schemas.py` | System prompt and model-facing tool schemas |
| `tools.py` | Main tool dispatcher and filesystem tools |
| `safety.py` | Allowed roots, path validation, confirmation parsing |
| `shell_guard.py` | Deterministic shell command classification |
| `undo.py` | Undo history and reversal |
| `session_store.py` | Saved web sessions |
| `xai_client.py` | xAI chat completions client |

See [Architecture](docs/ARCHITECTURE.md) for more detail.

## Development

Run Python tests:

```powershell
python -m pytest -q
```

Build the web app:

```powershell
cd web
npm run build
```

Common local checks:

```powershell
python -m pytest tests/test_core.py tests/test_web_server.py -q
cd web
npm run build
```

Generated/runtime files are intentionally ignored by Git:

- `.env`
- `.venv/`
- `logs/`
- `state/`
- `__pycache__/`
- `.pytest_cache/`
- `web/node_modules/`
- `web/dist/`
- `web/tsconfig.tsbuildinfo`

## Troubleshooting

**Missing API key.** Copy `.env.example` to `.env` and set `XAI_API_KEY`.

**Web app shows stale UI.** Rebuild with `cd web; npm run build`, then restart `python web_server.py --open`.

**Path not allowed.** The requested path is outside allowed roots. Update `XAI_ASSISTANT_ALLOWED_ROOTS` if needed.

**Playwright browser missing.** Run `playwright install chromium`.

**OCR or screenshot fails.** Confirm dependencies from `requirements.txt` are installed and the process has display access.

**Shell command blocked.** The shell guard rejected a dangerous pattern. Use a dedicated tool or a safer command.

**Undo cannot restore.** Undo never overwrites existing files and never removes non-empty folders; check the undo result for the restored path or reason.
