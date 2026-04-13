"""Undo stack for reversible file operations.

Records are stored in state/undo_history.jsonl.
Each record describes one atomic operation and how to reverse it.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import get_state_dir
from logger import SESSION_ID, log_event

# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

_UNDO_FILE = "undo_history.jsonl"


def _undo_path() -> Path:
    return get_state_dir() / _UNDO_FILE


def _append_record(record: dict[str, Any]) -> None:
    path = _undo_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def _load_all() -> list[dict[str, Any]]:
    path = _undo_path()
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def _save_all(records: list[dict[str, Any]]) -> None:
    path = _undo_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")


# ---------------------------------------------------------------------------
# Recording undoable actions
# ---------------------------------------------------------------------------


def record_move(source: str, destination: str) -> None:
    """Record a file move (reversible by moving back)."""
    _append_record({
        "ts": datetime.now(timezone.utc).isoformat(),
        "session": SESSION_ID,
        "action": "move_file",
        "source": source,
        "destination": destination,
        "undone": False,
    })


def record_rename(source: str, destination: str) -> None:
    """Record a file rename (reversible by renaming back)."""
    _append_record({
        "ts": datetime.now(timezone.utc).isoformat(),
        "session": SESSION_ID,
        "action": "rename_file",
        "source": source,
        "destination": destination,
        "undone": False,
    })


def record_create_folder(path: str) -> None:
    """Record a folder creation (reversible only if still empty)."""
    _append_record({
        "ts": datetime.now(timezone.utc).isoformat(),
        "session": SESSION_ID,
        "action": "create_folder",
        "path": path,
        "undone": False,
    })


def record_write_file(path: str, backup_path: str | None = None) -> None:
    """Record a file write. New files can be sent to Recycle Bin on undo.
    Overwrites store a backup_path so undo can restore the original."""
    _append_record({
        "ts": datetime.now(timezone.utc).isoformat(),
        "session": SESSION_ID,
        "action": "write_file",
        "path": path,
        "backup_path": backup_path,
        "undone": False,
    })


def record_organize_move(source: str, destination: str) -> None:
    """Record a single file move done during desktop organization."""
    _append_record({
        "ts": datetime.now(timezone.utc).isoformat(),
        "session": SESSION_ID,
        "action": "organize_move",
        "source": source,
        "destination": destination,
        "undone": False,
    })


# ---------------------------------------------------------------------------
# Undo execution
# ---------------------------------------------------------------------------


def _safe_restore_path(original: Path) -> Path:
    """Return *original* if free, or a safe suffixed variant if occupied."""
    if not original.exists():
        return original
    stem = original.stem
    suffix = original.suffix
    parent = original.parent
    n = 1
    while True:
        candidate = parent / f"{stem}_restored{n}{suffix}"
        if not candidate.exists():
            return candidate
        n += 1


def undo_last() -> dict[str, Any]:
    """Undo the most recent undoable action from this session. Returns a status dict."""
    records = _load_all()

    # Find last record from this session that hasn't been undone
    target_idx: int | None = None
    for i in range(len(records) - 1, -1, -1):
        r = records[i]
        if r.get("session") == SESSION_ID and not r.get("undone"):
            target_idx = i
            break

    if target_idx is None:
        return {"ok": False, "error": "Nothing to undo in this session."}

    rec = records[target_idx]
    action = rec.get("action", "")

    try:
        if action in ("move_file", "rename_file", "organize_move"):
            src = Path(rec["destination"])  # current location
            original = Path(rec["source"])  # where it was before
            if not src.exists():
                records[target_idx]["undone"] = True
                _save_all(records)
                return {"ok": False, "error": f"Cannot undo: file no longer at {src}"}
            restore_to = _safe_restore_path(original)
            restore_to.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(restore_to))
            records[target_idx]["undone"] = True
            _save_all(records)
            note = ""
            if restore_to != original:
                note = f" (original path was occupied; restored as {restore_to.name})"
            log_event("undo", {"action": action, "restored": str(restore_to)}, phase="undone")
            return {
                "ok": True,
                "action": action,
                "from": str(src),
                "restored_to": str(restore_to),
                "note": note,
            }

        elif action == "write_file":
            written = Path(rec["path"])
            backup = rec.get("backup_path")
            if not written.exists():
                records[target_idx]["undone"] = True
                _save_all(records)
                return {"ok": False, "error": f"File already gone: {written}"}
            if backup:
                # Overwrite case: restore the .bak file
                bak = Path(backup)
                if not bak.exists():
                    records[target_idx]["undone"] = True
                    _save_all(records)
                    return {"ok": False, "error": f"Backup file missing: {bak}"}
                shutil.move(str(bak), str(written))
                records[target_idx]["undone"] = True
                _save_all(records)
                log_event("undo", {"action": action, "restored_backup": str(written)}, phase="undone")
                return {"ok": True, "action": action, "restored_to": str(written),
                        "note": " (original content restored from backup)"}
            else:
                # New file case: send to Recycle Bin
                try:
                    from send2trash import send2trash
                    send2trash(str(written))
                except ImportError:
                    written.unlink()
                except Exception as e:
                    return {"ok": False, "error": f"Cannot send to Recycle Bin: {e}"}
                records[target_idx]["undone"] = True
                _save_all(records)
                log_event("undo", {"action": action, "recycled": str(written)}, phase="undone")
                return {"ok": True, "action": action, "removed": str(written),
                        "note": " (sent to Recycle Bin)"}

        elif action == "create_folder":
            folder = Path(rec["path"])
            if not folder.exists():
                records[target_idx]["undone"] = True
                _save_all(records)
                return {"ok": False, "error": f"Folder already gone: {folder}"}
            if not folder.is_dir():
                return {"ok": False, "error": f"Path is not a directory: {folder}"}
            # Only remove if empty
            try:
                contents = list(folder.iterdir())
            except OSError:
                contents = []
            if contents:
                return {
                    "ok": False,
                    "error": f"Cannot undo create_folder: {folder} is not empty ({len(contents)} items inside).",
                }
            folder.rmdir()
            records[target_idx]["undone"] = True
            _save_all(records)
            log_event("undo", {"action": action, "removed": str(folder)}, phase="undone")
            return {"ok": True, "action": action, "removed": str(folder)}

        else:
            return {"ok": False, "error": f"Action '{action}' is not undoable."}

    except OSError as e:
        return {"ok": False, "error": f"Undo failed: {e}"}


def undo_n(n: int) -> list[dict[str, Any]]:
    """Undo the last *n* actions from this session.

    Calls ``undo_last()`` up to *n* times, stopping early if there is nothing
    left to undo. Returns the list of result dicts in execution order (oldest
    undo first).
    """
    results: list[dict[str, Any]] = []
    for _ in range(max(0, n)):
        result = undo_last()
        results.append(result)
        if not result.get("ok") and result.get("error", "").startswith("Nothing to undo"):
            break
    return results


# ---------------------------------------------------------------------------
# History display
# ---------------------------------------------------------------------------


def get_history(limit: int = 20) -> list[dict[str, Any]]:
    """Return the last *limit* undo records for this session (newest first)."""
    records = _load_all()
    session_records = [r for r in records if r.get("session") == SESSION_ID]
    return list(reversed(session_records[-limit:]))
