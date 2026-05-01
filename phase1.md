# Phase 1 Code Review Findings and Implementation Record

## Review Snapshot

- Branch: `master`
- Baseline status before review: clean and synced with `origin/master`
- Verification run: `python -m pytest -q`
- Initial result: `327 passed in 4.64s`
- Implementation status: completed in this phase
- Final result after implementation: `358 passed in 4.77s`

This document began as the Phase 1 review and fix plan. The Phase 1 items below have now been implemented, with tests added or updated for each behavior change.

## Major Findings

### P1 - Browser tools accept non-http URLs despite the schema and README saying http(s)

`schemas.py:189-190` describes `browser_navigate.url` as an absolute http(s) URL, but `browser_tools.py:65-71` passes whatever string it receives directly to Playwright. `browser_download` similarly accepts arbitrary `url` input and sends it to `page.goto()` at `browser_tools.py:159-165`.

Risk: the model can request `file://`, local browser-internal URLs, or other schemes through the Playwright session. This conflicts with the documented browser isolation model and creates a local-file exposure path.

Implemented:
- Add a shared URL validator in `browser_tools.py` that permits only `http://` and `https://`.
- Apply it to `browser_navigate()` and the URL path of `browser_download()`.
- Add tests for rejecting `file://`, `about:`, empty strings, and malformed URLs.
- Keep `open_url()` behavior aligned with the browser tools.

### P1 - `start_process` can bypass shell command safety

`process_tools.py:45-56` validates only the working directory, then starts any executable with arbitrary args via `subprocess.Popen(..., shell=False)`. Because this is separate from `run_command`, it bypasses the deterministic command classifier in `shell_guard.py`.

Risk: a model-suggested action could launch `powershell.exe`, `cmd.exe`, script hosts, installers, or system tools with arguments. It still requires approval, but it does not get the shell guard's blocked/risky classification and is currently treated as only medium risk in `core.py`.

Implemented:
- Introduce a deterministic process launch guard, either by reusing `shell_guard` concepts or by adding a smaller executable blocklist for `start_process`.
- Block obvious shell/script/system-control executables such as `cmd`, `powershell`, `pwsh`, `wscript`, `cscript`, `mshta`, `reg`, `regedit`, `rundll32`, and package/install utilities unless explicitly allowed.
- Reclassify `start_process` as high risk unless it is a known safe app or explicitly user-provided.
- Add tests for blocked executables, allowed simple app launches, dry-run behavior, and working-directory validation.

### P1 - Backup files collide, which can break multi-step undo

`tools.py:919-925` writes overwrite backups to a fixed `*.bak` path. `editor_tools.py:20-38` uses the same fixed backup naming for append/replace/patch. Repeated edits to the same file overwrite the prior backup path, while the undo stack records multiple actions that may all point at the same backup.

Risk: undo history becomes unreliable after multiple edits. The newest undo can consume the shared `.bak`, leaving older undo records unable to restore. A stale `.bak` may also be overwritten by a later operation.

Implemented:
- Replace fixed `.bak` naming with unique backup paths, for example `file.ext.bak.<session>.<counter>` or `file.ext.<timestamp>.bak`.
- Centralize backup path creation in one helper shared by `tools.py` and `editor_tools.py`.
- Record exact unique backup paths in undo records.
- Add tests for two consecutive overwrites/edits to the same file and then undoing both in order.

### P1 - Streaming retry can duplicate partial output and tool arguments

`xai_client.py:158-207` accumulates streamed content/tool-call deltas outside the retry attempt loop. If a stream emits partial content or partial tool-call arguments and then raises a retryable error, the next attempt appends to the same accumulators and can emit duplicate UI deltas via `on_delta`.

Risk: users can see duplicated assistant text, and tool-call JSON argument assembly can become corrupted after a mid-stream retry.

Implemented:
- Treat streaming retries differently from non-stream retries.
- Prefer no retry after any content/tool delta has been emitted, or keep per-attempt accumulators and commit only after a complete stream.
- Add tests simulating a stream that emits a delta then raises `URLError`, proving there is no duplicated `on_delta` output or corrupted tool-call args.

### P2 - Session persistence is embedded in the GUI class and silently swallows save failures

Session storage lives directly inside `AssistantApp` (`gui.py:1464-1596`). `_save_session()` catches `OSError` and silently returns after trying to clean up a temp file (`gui.py:1499-1510`). `_worker()` also suppresses all session-save exceptions (`gui.py:1386-1390`).

Risk: users may think a session was saved when it was not. The GUI class is also doing too much, making session behavior harder to test and reason about.

Implemented:
- Extract a small `SessionStore` module with `save`, `load`, `list`, and corrupt-file skip behavior.
- Return explicit success/error results from save operations and surface errors in the GUI status area.
- Keep GUI rendering separate from session serialization.
- Expand tests around corrupt JSON, failed writes, missing system prompts, and token total parsing.

### P2 - Patch parsing accepts weak inputs

`editor_tools.py:121-205` parses a single-file unified diff but does not explicitly reject zero-hunk diffs. It also only compares the basename of the diff target to the provided path at `editor_tools.py:196-197`.

Risk: an empty or malformed diff can still write the original content back and create undo records. A multi-file diff can be misinterpreted because the parser is not enforcing exactly one file target.

Implemented:
- Reject diffs with zero hunks.
- Track all `---`/`+++` file headers and reject multi-file diffs.
- Continue using the caller-provided path as authoritative while validating diff headers for single-file intent and basename match.
- Add tests for empty diffs, multi-file diffs, mismatched paths, and valid single-file diffs.

### P2 - `read_file_range` reads entire files without the same size guard as `read_text_file`

`tools.read_text_file()` enforces read caps and a 10 MB hard limit, but `editor_tools.py:51-72` reads the whole file into memory before slicing requested lines.

Risk: asking for a small range from a large allowed file can consume unnecessary memory and stall the app.

Implemented:
- Implement streaming line-range reads that stop after `end_line`.
- Add a maximum line span and optional file-size guard consistent with `read_text_file`.
- Add tests for large files and invalid ranges.

## Dead Code and Cleanup

- `shell_guard.py:215` assigns `raw = command` but never uses it.
- `shell_guard.py:151` defines `_BACKTICK_PATTERN`, but `_SUBSHELL_PATTERN` already covers backticks and the separate pattern is unused.
- `core.py:644` computes `has_shell` in `_try_structured_summary()` but never uses it.
- `browser_tools.py:22-25` defines `_traces_dir()` but no tracing code calls it.
- `desktop_tools.py:62-67` returns `(Image, Image)` from `_load_pillow()` even though callers only use the first item.

Implemented:
- Remove unused variables/helpers where there is no near-term feature using them.
- If browser tracing is intended, add actual trace-start/stop behavior and tests; otherwise delete `_traces_dir()`.
- Keep cleanup commits separate from behavior fixes so safety changes remain easy to review.

## Implementation Summary

### Step 1 - Safety boundary fixes

- Enforce http(s)-only browser navigation/download URLs.
- Add a process-launch guard and update `start_process` risk classification.
- Add targeted tests for both changes.

### Step 2 - Undo reliability

- Introduce unique backup naming.
- Share backup creation across `write_file` and editor operations.
- Add repeated-edit undo tests.

### Step 3 - Streaming robustness

- Make streaming retries all-or-nothing after emitted deltas, or isolate per-attempt buffers.
- Add retry/partial-stream tests for content and tool-call deltas.

### Step 4 - Editor/read robustness

- Harden unified-diff parsing.
- Stream `read_file_range()` instead of reading whole files.
- Add edge-case tests.

### Step 5 - GUI/session maintainability

- Extract session persistence from `AssistantApp`.
- Surface save/load errors instead of swallowing them.
- Add tests for corrupt and failed session storage paths.

### Step 6 - Cleanup pass

- Remove dead code listed above.
- Re-run full tests and update docs if any behavior or safety guarantees change.

## Acceptance Criteria For Phase 1 Fixes

- `python -m pytest -q` passes.
- Browser tools reject non-http(s) schemes.
- `start_process` cannot launch blocked shell/script/system executables.
- Repeated edits to the same file can be undone reliably.
- Streaming retry tests prove no duplicate deltas or corrupted tool-call JSON.
- Session save failures are visible to users.
- Dead code list is either removed or intentionally justified with tests/docs.
