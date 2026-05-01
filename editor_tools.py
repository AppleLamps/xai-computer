"""Precise text-editing helpers with backup-based undo."""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backup_utils import unique_backup_path
from config import is_dry_run
from logger import log_event
from safety import require_allowed_path, require_allowed_path_readonly
from undo import record_write_file

_WRITE_MAX_BYTES = 500_000
_MAX_READ_BYTES = 10_000_000
_MAX_LINE_RANGE = 5_000
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _backup_path(fp: Path) -> Path:
    return unique_backup_path(fp)


def _write_with_backup(fp: Path, content: str) -> dict[str, Any]:
    encoded = content.encode("utf-8")
    if len(encoded) > _WRITE_MAX_BYTES:
        return {"ok": False, "error": f"Content too large: {len(encoded)} bytes (max {_WRITE_MAX_BYTES})."}

    existed = fp.exists()
    backup_path: str | None = None
    if is_dry_run():
        return {"ok": True, "dry_run": True, "path": str(fp), "bytes": len(encoded), "existed": existed}

    if existed:
        bak = _backup_path(fp)
        try:
            shutil.copy2(str(fp), str(bak))
            backup_path = str(bak)
        except OSError as e:
            return {"ok": False, "error": f"Failed to create backup: {e}"}
    try:
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")
    except OSError as e:
        return {"ok": False, "error": f"Failed to write file: {e}"}

    record_write_file(str(fp), backup_path)
    return {"ok": True, "path": str(fp), "bytes": len(encoded), "backup_path": backup_path, "existed": existed}


def read_file_range(path: str, start_line: int, end_line: int) -> dict[str, Any]:
    try:
        fp = require_allowed_path_readonly(Path(path))
    except (OSError, ValueError, PermissionError) as e:
        return {"ok": False, "error": str(e)}
    if not fp.is_file():
        return {"ok": False, "error": f"Not a file: {fp}"}
    if start_line < 1 or end_line < start_line:
        return {"ok": False, "error": "Invalid line range."}
    if end_line - start_line + 1 > _MAX_LINE_RANGE:
        return {"ok": False, "error": f"Line range too large (max {_MAX_LINE_RANGE} lines)."}
    try:
        size = fp.stat().st_size
    except OSError as e:
        return {"ok": False, "error": str(e)}
    if size > _MAX_READ_BYTES:
        return {"ok": False, "error": f"File too large to read by range: {size} bytes."}
    try:
        selected: list[str] = []
        with fp.open("r", encoding="utf-8", errors="replace") as f:
            for line_number, line in enumerate(f, 1):
                if line_number < start_line:
                    continue
                if line_number > end_line:
                    break
                selected.append(line.rstrip("\n").rstrip("\r"))
    except OSError as e:
        return {"ok": False, "error": str(e)}
    return {
        "ok": True,
        "path": str(fp),
        "start_line": start_line,
        "end_line": end_line,
        "content": "\n".join(selected),
        "line_count": len(selected),
    }


def append_file(path: str, content: str) -> dict[str, Any]:
    try:
        fp = require_allowed_path(Path(path))
    except (OSError, ValueError, PermissionError) as e:
        return {"ok": False, "error": str(e)}
    try:
        current = fp.read_text(encoding="utf-8") if fp.exists() else ""
    except OSError as e:
        return {"ok": False, "error": str(e)}
    result = _write_with_backup(fp, current + content)
    if result.get("ok"):
        log_event("append_file", {"path": str(fp), "bytes": len(content.encode('utf-8'))}, phase="executed")
    return result


def replace_in_file(path: str, old_text: str, new_text: str, replace_all: bool = False) -> dict[str, Any]:
    try:
        fp = require_allowed_path(Path(path))
    except (OSError, ValueError, PermissionError) as e:
        return {"ok": False, "error": str(e)}
    if not fp.is_file():
        return {"ok": False, "error": f"Not a file: {fp}"}
    try:
        current = fp.read_text(encoding="utf-8")
    except OSError as e:
        return {"ok": False, "error": str(e)}
    if old_text not in current:
        return {"ok": False, "error": "old_text not found in file."}
    count = current.count(old_text) if replace_all else 1
    updated = current.replace(old_text, new_text, -1 if replace_all else 1)
    result = _write_with_backup(fp, updated)
    if result.get("ok"):
        result["replacements"] = count
        log_event("replace_in_file", {"path": str(fp), "replacements": count}, phase="executed")
    return result


@dataclass
class _Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[str]


def _parse_unified_diff(unified_diff: str) -> tuple[str | None, list[_Hunk]] | dict[str, Any]:
    def _strip_diff_prefix(raw: str) -> str:
        if raw.startswith("a/") or raw.startswith("b/"):
            return raw[2:]
        return raw

    lines = unified_diff.splitlines(keepends=True)
    target_path: str | None = None
    target_paths: list[str] = []
    hunks: list[_Hunk] = []
    current: _Hunk | None = None
    for line in lines:
        if line.startswith("--- ") or line.startswith("+++ "):
            if line.startswith("+++ "):
                target_path = _strip_diff_prefix(line[4:].strip())
                if target_path not in ("/dev/null", "dev/null"):
                    target_paths.append(target_path)
            continue
        match = _HUNK_RE.match(line)
        if match:
            current = _Hunk(
                old_start=int(match.group(1)),
                old_count=int(match.group(2) or "1"),
                new_start=int(match.group(3)),
                new_count=int(match.group(4) or "1"),
                lines=[],
            )
            hunks.append(current)
            continue
        if line.startswith("\\ No newline"):
            return {"ok": False, "error": "Diffs with missing newline markers are not supported."}
        if current is None:
            continue
        current.lines.append(line)
    unique_targets = {p for p in target_paths if p}
    if len(unique_targets) > 1:
        return {"ok": False, "error": "Multi-file diffs are not supported."}
    return target_path, hunks


def _apply_hunks(original_lines: list[str], hunks: list[_Hunk]) -> str | dict[str, Any]:
    result: list[str] = []
    pointer = 0
    for hunk in hunks:
        target_index = hunk.old_start - 1
        if target_index < pointer:
            return {"ok": False, "error": "Overlapping diff hunks are not supported."}
        result.extend(original_lines[pointer:target_index])
        idx = target_index
        for line in hunk.lines:
            prefix = line[:1]
            payload = line[1:]
            if prefix == " ":
                if idx >= len(original_lines) or original_lines[idx] != payload:
                    return {"ok": False, "error": "Patch context mismatch."}
                result.append(original_lines[idx])
                idx += 1
            elif prefix == "-":
                if idx >= len(original_lines) or original_lines[idx] != payload:
                    return {"ok": False, "error": "Patch removal mismatch."}
                idx += 1
            elif prefix == "+":
                result.append(payload)
            else:
                return {"ok": False, "error": f"Unsupported patch line: {line!r}"}
        pointer = idx
    result.extend(original_lines[pointer:])
    return "".join(result)


def apply_patch(path: str, unified_diff: str) -> dict[str, Any]:
    try:
        fp = require_allowed_path(Path(path))
    except (OSError, ValueError, PermissionError) as e:
        return {"ok": False, "error": str(e)}
    if not fp.is_file():
        return {"ok": False, "error": f"Not a file: {fp}"}
    parsed = _parse_unified_diff(unified_diff)
    if isinstance(parsed, dict):
        return parsed
    target_path, hunks = parsed
    if not hunks:
        return {"ok": False, "error": "Patch contains no hunks."}
    if target_path and Path(target_path).name != fp.name:
        return {"ok": False, "error": "Patch target does not match provided path."}
    try:
        original = fp.read_text(encoding="utf-8")
    except OSError as e:
        return {"ok": False, "error": str(e)}
    updated = _apply_hunks(original.splitlines(keepends=True), hunks)
    if isinstance(updated, dict):
        return updated
    result = _write_with_backup(fp, updated)
    if result.get("ok"):
        result["hunks"] = len(hunks)
        log_event("apply_patch", {"path": str(fp), "hunks": len(hunks)}, phase="executed")
    return result
