"""Vetted local filesystem, desktop, and constrained shell helpers.

All mutating operations record undo entries when not in dry-run mode.
Read-only analysis tools are safe and never modify the filesystem.
Shell execution goes through shell_guard.py for deterministic classification.
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
import time
import webbrowser
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import browser_tools
import desktop_tools
import editor_tools
import process_tools
from backup_utils import unique_backup_path
from config import get_default_desktop_path, is_dry_run, set_last_working_folder
from logger import log_event
from safety import (
    is_hidden_name,
    is_system_or_protected_name,
    require_allowed_path,
    require_allowed_path_readonly,
)
from shell_guard import (
    classify_command,
    get_extra_allowlist,
    redact_secrets,
    truncate_output,
    validate_working_dir,
)
from undo import record_create_folder, record_move, record_organize_move, record_rename, record_write_file

ToolHandler = Callable[..., dict[str, Any]]

# ---------------------------------------------------------------------------
# File type classification
# ---------------------------------------------------------------------------

_EXTENSION_TO_CATEGORY: list[tuple[frozenset[str], str]] = [
    (frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg",
                ".heic", ".tif", ".tiff", ".ico", ".raw", ".cr2", ".nef",
                ".psd", ".ai", ".eps"}), "Images"),
    (frozenset({".pdf"}), "PDFs"),
    (frozenset({".doc", ".docx", ".txt", ".md", ".rtf", ".odt", ".tex",
                ".log", ".cfg", ".ini", ".yaml", ".yml", ".toml",
                ".json", ".xml"}), "Documents"),
    (frozenset({".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2",
                ".xz", ".zst", ".cab", ".iso"}), "Archives"),
    (frozenset({".lnk", ".url"}), "Shortcuts"),
    (frozenset({".mp4", ".mov", ".avi", ".mkv", ".webm", ".wmv", ".flv",
                ".m4v", ".mpg", ".mpeg", ".3gp"}), "Videos"),
    (frozenset({".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a", ".wma",
                ".opus", ".mid", ".midi"}), "Audio"),
    (frozenset({".xls", ".xlsx", ".csv", ".ods", ".tsv"}), "Spreadsheets"),
    (frozenset({".ppt", ".pptx", ".odp", ".key"}), "Presentations"),
    (frozenset({".exe", ".msi", ".bat", ".cmd", ".ps1", ".sh",
                ".com"}), "Executables"),
    (frozenset({".py", ".js", ".ts", ".html", ".css", ".cpp", ".c", ".h",
                ".java", ".go", ".rs", ".rb", ".php", ".cs", ".swift",
                ".kt", ".scala", ".r", ".sql", ".lua", ".pl"}), "Code"),
    (frozenset({".ttf", ".otf", ".woff", ".woff2", ".eot"}), "Fonts"),
    (frozenset({".db", ".sqlite", ".sqlite3", ".mdb", ".accdb"}), "Databases"),
    (frozenset({".torrent", ".nfo", ".srt", ".sub", ".ass"}), "Misc"),
]

# Patterns that suggest duplicate files
_DUPLICATE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^(.+?)[\s\-_]*\((\d+)\)(\.[^.]+)$"),       # file (1).txt
    re.compile(r"^(.+?)[\s\-_]*copy[\s\-_]*(\d*)(\.[^.]+)$", re.IGNORECASE),  # file copy.txt
    re.compile(r"^(.+?)_dup(\d+)(\.[^.]+)$"),                 # file_dup1.txt
]

# Max bytes for read_text_file
_MAX_READ_BYTES = 100_000  # ~100 KB


def _category_for_suffix(suffix: str) -> str:
    s = suffix.lower()
    for exts, name in _EXTENSION_TO_CATEGORY:
        if s in exts:
            return name
    return "Other"


def _unique_destination(dest: Path) -> Path:
    if not dest.exists():
        return dest
    if dest.is_dir():
        return dest
    parent = dest.parent
    stem = dest.stem
    suffix = dest.suffix
    n = 1
    while True:
        candidate = parent / f"{stem}_dup{n}{suffix}"
        if not candidate.exists():
            return candidate
        n += 1


def _format_size(size: int) -> str:
    """Human-readable file size."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size) < 1024:
            return f"{size:.1f} {unit}" if unit != "B" else f"{size} B"
        size /= 1024  # type: ignore[assignment]
    return f"{size:.1f} PB"


def _detect_duplicates(names: list[str]) -> list[dict[str, str]]:
    """Detect likely duplicate files by filename pattern matching."""
    dupes: list[dict[str, str]] = []
    for name in names:
        for pat in _DUPLICATE_PATTERNS:
            m = pat.match(name)
            if m:
                base = m.group(1).strip()
                ext = m.group(3) if m.lastindex and m.lastindex >= 3 else ""
                dupes.append({
                    "file": name,
                    "likely_original": f"{base}{ext}",
                    "pattern": "copy_variant",
                })
                break
    return dupes


# ---------------------------------------------------------------------------
# Read-only tools
# ---------------------------------------------------------------------------


def list_directory(path: str) -> dict[str, Any]:
    try:
        root = require_allowed_path_readonly(Path(path))
    except (OSError, ValueError, PermissionError) as e:
        return {"ok": False, "error": str(e)}
    set_last_working_folder(root)
    try:
        entries: list[dict[str, Any]] = []
        for child in sorted(root.iterdir(), key=lambda p: p.name.casefold()):
            try:
                stat = child.stat()
            except OSError:
                continue
            entries.append(
                {
                    "name": child.name,
                    "is_dir": child.is_dir(),
                    "is_file": child.is_file(),
                    "size": stat.st_size if child.is_file() else None,
                }
            )
        log_event("list_directory", {"path": str(root), "count": len(entries)}, phase="executed")
        return {"ok": True, "path": str(root), "entries": entries}
    except OSError as e:
        return {"ok": False, "error": str(e)}


def analyze_directory(path: str) -> dict[str, Any]:
    """Analyze a directory: file counts by type, total size, duplicate detection."""
    try:
        root = require_allowed_path_readonly(Path(path))
    except (OSError, ValueError, PermissionError) as e:
        return {"ok": False, "error": str(e)}
    if not root.is_dir():
        return {"ok": False, "error": f"Not a directory: {root}"}
    set_last_working_folder(root)

    total_size = 0
    file_count = 0
    dir_count = 0
    type_counts: Counter[str] = Counter()
    type_sizes: Counter[str] = Counter()
    file_names: list[str] = []

    try:
        for child in root.iterdir():
            try:
                stat = child.stat()
            except OSError:
                continue
            if child.is_dir():
                dir_count += 1
            elif child.is_file():
                file_count += 1
                sz = stat.st_size
                total_size += sz
                cat = _category_for_suffix(child.suffix)
                type_counts[cat] += 1
                type_sizes[cat] += sz
                file_names.append(child.name)
    except OSError as e:
        return {"ok": False, "error": str(e)}

    duplicates = _detect_duplicates(file_names)

    breakdown = [
        {"type": cat, "count": cnt, "size": _format_size(type_sizes[cat])}
        for cat, cnt in type_counts.most_common()
    ]

    log_event("analyze_directory", {"path": str(root), "files": file_count}, phase="executed")
    return {
        "ok": True,
        "path": str(root),
        "total_files": file_count,
        "total_dirs": dir_count,
        "total_size": _format_size(total_size),
        "total_size_bytes": total_size,
        "type_breakdown": breakdown,
        "likely_duplicates": duplicates[:20],
        "duplicate_count": len(duplicates),
    }


def largest_files(path: str, limit: int = 10) -> dict[str, Any]:
    """List the largest files in a directory (non-recursive, within allowed roots)."""
    try:
        root = require_allowed_path_readonly(Path(path))
    except (OSError, ValueError, PermissionError) as e:
        return {"ok": False, "error": str(e)}
    if not root.is_dir():
        return {"ok": False, "error": f"Not a directory: {root}"}
    set_last_working_folder(root)

    limit = max(1, min(limit, 50))
    files: list[tuple[Path, int]] = []
    try:
        for child in root.iterdir():
            if child.is_file():
                try:
                    files.append((child, child.stat().st_size))
                except OSError:
                    continue
    except OSError as e:
        return {"ok": False, "error": str(e)}

    files.sort(key=lambda x: x[1], reverse=True)
    top = [
        {"name": f.name, "size": _format_size(s), "size_bytes": s, "path": str(f)}
        for f, s in files[:limit]
    ]
    log_event("largest_files", {"path": str(root), "limit": limit}, phase="executed")
    return {"ok": True, "path": str(root), "files": top, "count": len(top)}


def file_type_summary(path: str) -> dict[str, Any]:
    """Summarize file types and their aggregate sizes in a directory."""
    try:
        root = require_allowed_path_readonly(Path(path))
    except (OSError, ValueError, PermissionError) as e:
        return {"ok": False, "error": str(e)}
    if not root.is_dir():
        return {"ok": False, "error": f"Not a directory: {root}"}
    set_last_working_folder(root)

    ext_counts: Counter[str] = Counter()
    ext_sizes: Counter[str] = Counter()
    cat_counts: Counter[str] = Counter()
    cat_sizes: Counter[str] = Counter()

    try:
        for child in root.iterdir():
            if not child.is_file():
                continue
            try:
                sz = child.stat().st_size
            except OSError:
                continue
            ext = child.suffix.lower() or "(no extension)"
            cat = _category_for_suffix(child.suffix)
            ext_counts[ext] += 1
            ext_sizes[ext] += sz
            cat_counts[cat] += 1
            cat_sizes[cat] += sz
    except OSError as e:
        return {"ok": False, "error": str(e)}

    by_extension = [
        {"ext": ext, "count": cnt, "size": _format_size(ext_sizes[ext])}
        for ext, cnt in ext_counts.most_common()
    ]
    by_category = [
        {"category": cat, "count": cnt, "size": _format_size(cat_sizes[cat])}
        for cat, cnt in cat_counts.most_common()
    ]

    log_event("file_type_summary", {"path": str(root)}, phase="executed")
    return {"ok": True, "path": str(root), "by_extension": by_extension, "by_category": by_category}


def read_text_file(path: str, max_chars: int = 5000) -> dict[str, Any]:
    """Read the beginning of a text file (capped at max_chars)."""
    try:
        fp = require_allowed_path_readonly(Path(path))
    except (OSError, ValueError, PermissionError) as e:
        return {"ok": False, "error": str(e)}
    if not fp.is_file():
        return {"ok": False, "error": f"Not a file: {fp}"}

    max_chars = max(100, min(max_chars, _MAX_READ_BYTES))

    # Size guard
    try:
        sz = fp.stat().st_size
    except OSError as e:
        return {"ok": False, "error": str(e)}
    if sz > 10_000_000:  # 10 MB hard limit
        return {"ok": False, "error": f"File too large to read: {_format_size(sz)}"}

    try:
        raw = fp.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return {"ok": False, "error": str(e)}

    truncated = len(raw) > max_chars
    content = raw[:max_chars]
    log_event("read_text_file", {"path": str(fp), "chars": len(content)}, phase="executed")
    return {
        "ok": True,
        "path": str(fp),
        "content": content,
        "truncated": truncated,
        "total_chars": len(raw),
        "size": _format_size(sz),
    }


def search_files(path: str, query: str) -> dict[str, Any]:
    """Search for files by name pattern in a directory (non-recursive)."""
    try:
        root = require_allowed_path_readonly(Path(path))
    except (OSError, ValueError, PermissionError) as e:
        return {"ok": False, "error": str(e)}
    if not root.is_dir():
        return {"ok": False, "error": f"Not a directory: {root}"}
    set_last_working_folder(root)

    q = query.strip().lower()
    if not q:
        return {"ok": False, "error": "Empty search query."}

    matches: list[dict[str, Any]] = []
    try:
        for child in root.iterdir():
            if q in child.name.lower():
                try:
                    stat = child.stat()
                    matches.append({
                        "name": child.name,
                        "path": str(child),
                        "is_dir": child.is_dir(),
                        "is_file": child.is_file(),
                        "size": stat.st_size if child.is_file() else None,
                    })
                except OSError:
                    continue
            if len(matches) >= 50:
                break
    except OSError as e:
        return {"ok": False, "error": str(e)}

    log_event("search_files", {"path": str(root), "query": q, "found": len(matches)}, phase="executed")
    return {"ok": True, "path": str(root), "query": query, "matches": matches, "count": len(matches)}


def recent_files(path: str, limit: int = 15) -> dict[str, Any]:
    """List most recently modified files in a directory."""
    try:
        root = require_allowed_path_readonly(Path(path))
    except (OSError, ValueError, PermissionError) as e:
        return {"ok": False, "error": str(e)}
    if not root.is_dir():
        return {"ok": False, "error": f"Not a directory: {root}"}
    set_last_working_folder(root)

    limit = max(1, min(limit, 50))
    files: list[tuple[Path, float, int]] = []
    try:
        for child in root.iterdir():
            if child.is_file():
                try:
                    stat = child.stat()
                    files.append((child, stat.st_mtime, stat.st_size))
                except OSError:
                    continue
    except OSError as e:
        return {"ok": False, "error": str(e)}

    files.sort(key=lambda x: x[1], reverse=True)
    top = [
        {
            "name": f.name,
            "path": str(f),
            "modified": datetime.fromtimestamp(mt, tz=timezone.utc).isoformat()[:19],
            "size": _format_size(sz),
        }
        for f, mt, sz in files[:limit]
    ]
    log_event("recent_files", {"path": str(root), "limit": limit}, phase="executed")
    return {"ok": True, "path": str(root), "files": top, "count": len(top)}


def directory_tree(path: str, depth: int = 2) -> dict[str, Any]:
    """Show directory structure as a tree (read-only, bounded depth)."""
    try:
        root = require_allowed_path_readonly(Path(path))
    except (OSError, ValueError, PermissionError) as e:
        return {"ok": False, "error": str(e)}
    if not root.is_dir():
        return {"ok": False, "error": f"Not a directory: {root}"}
    set_last_working_folder(root)

    depth = max(1, min(depth, 5))
    lines: list[str] = [root.name + "/"]
    _entry_count = 0
    _MAX_ENTRIES = 200

    def _walk(folder: Path, prefix: str, current_depth: int) -> None:
        nonlocal _entry_count
        if current_depth > depth or _entry_count > _MAX_ENTRIES:
            return
        try:
            children = sorted(folder.iterdir(), key=lambda p: (not p.is_dir(), p.name.casefold()))
        except OSError:
            return
        visible = [c for c in children if not is_hidden_name(c.name) and not is_system_or_protected_name(c.name)]
        for i, child in enumerate(visible):
            _entry_count += 1
            if _entry_count > _MAX_ENTRIES:
                lines.append(f"{prefix}... (truncated)")
                return
            is_last = i == len(visible) - 1
            connector = "└── " if is_last else "├── "
            suffix = "/" if child.is_dir() else ""
            lines.append(f"{prefix}{connector}{child.name}{suffix}")
            if child.is_dir():
                extension = "    " if is_last else "│   "
                _walk(child, prefix + extension, current_depth + 1)

    _walk(root, "", 1)
    log_event("directory_tree", {"path": str(root), "depth": depth}, phase="executed")
    return {"ok": True, "path": str(root), "tree": "\n".join(lines), "entries": _entry_count}


# ---------------------------------------------------------------------------
# Folder organization (generalized from desktop-only)
# ---------------------------------------------------------------------------


def _folder_cleanup_plan(folder: Path) -> list[dict[str, Any]]:
    """Build a type-based organization plan for any folder."""
    plan: list[dict[str, Any]] = []
    if not folder.is_dir():
        return plan
    for item in folder.iterdir():
        if not item.is_file():
            continue
        try:
            if item.parent.resolve() != folder.resolve():
                continue
        except OSError:
            continue
        if is_hidden_name(item.name) or is_system_or_protected_name(item.name):
            continue
        category = _category_for_suffix(item.suffix)
        dest_dir = folder / category
        dest_file = dest_dir / item.name
        final = _unique_destination(dest_file) if dest_file.exists() and dest_file != item else dest_file
        try:
            if final.resolve() == item.resolve():
                continue
        except OSError:
            pass
        plan.append({
            "source": str(item),
            "category": category,
            "dest_dir": str(dest_dir),
            "destination": str(final),
        })
    return plan


def _date_based_plan(folder: Path, group_by: str = "month") -> list[dict[str, Any]]:
    """Build a date-based organization plan (by month or year)."""
    plan: list[dict[str, Any]] = []
    if not folder.is_dir():
        return plan
    for item in folder.iterdir():
        if not item.is_file():
            continue
        if is_hidden_name(item.name) or is_system_or_protected_name(item.name):
            continue
        try:
            mtime = item.stat().st_mtime
        except OSError:
            continue
        dt = datetime.fromtimestamp(mtime, tz=timezone.utc)
        if group_by == "year":
            subfolder = str(dt.year)
        else:
            subfolder = f"{dt.year}-{dt.month:02d}"
        dest_dir = folder / subfolder
        dest_file = dest_dir / item.name
        final = _unique_destination(dest_file) if dest_file.exists() and dest_file != item else dest_file
        try:
            if final.resolve() == item.resolve():
                continue
        except OSError:
            pass
        plan.append({
            "source": str(item),
            "group": subfolder,
            "dest_dir": str(dest_dir),
            "destination": str(final),
        })
    return plan


# ---------------------------------------------------------------------------
# Mutating tools
# ---------------------------------------------------------------------------


def _destination_is_directory(dst_arg: Path, destination_raw: str) -> bool:
    if str(destination_raw).rstrip().endswith(("/", "\\")):
        return True
    if dst_arg.exists():
        return dst_arg.is_dir()
    return dst_arg.suffix == ""


def _prepare_move_paths(source: str, destination: str) -> tuple[Path, Path]:
    src = require_allowed_path(Path(source))
    dst_arg = Path(destination).expanduser()
    if _destination_is_directory(dst_arg, destination):
        dest_path = require_allowed_path(dst_arg) / src.name
    else:
        dest_path = require_allowed_path(dst_arg)
    require_allowed_path(dest_path.parent)
    return src, dest_path


def move_file(source: str, destination: str) -> dict[str, Any]:
    try:
        src, dest_path = _prepare_move_paths(source, destination)
    except (OSError, ValueError, PermissionError) as e:
        return {"ok": False, "error": str(e)}
    if not src.is_file():
        return {"ok": False, "error": f"Source is not a file: {src}"}
    final_dest = _unique_destination(dest_path)

    if is_dry_run():
        log_event("move_file", {"source": str(src), "destination": str(final_dest)}, phase="dry_run")
        return {"ok": True, "dry_run": True, "source": str(src), "destination": str(final_dest)}

    try:
        final_dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(final_dest))
    except OSError as e:
        return {"ok": False, "error": str(e)}
    record_move(str(src), str(final_dest))
    log_event("move_file", {"source": str(src), "destination": str(final_dest)}, phase="executed")
    return {"ok": True, "source": str(src), "destination": str(final_dest)}


def rename_file(source: str, new_name: str) -> dict[str, Any]:
    try:
        src = require_allowed_path(Path(source))
    except (OSError, ValueError, PermissionError) as e:
        return {"ok": False, "error": str(e)}
    if not src.is_file():
        return {"ok": False, "error": f"Source is not a file: {src}"}
    name = Path(new_name).name
    if name != new_name.strip():
        return {"ok": False, "error": "new_name must be a base file name only (no folders)."}
    dest = src.with_name(name)
    try:
        require_allowed_path(dest.parent)
    except PermissionError as e:
        return {"ok": False, "error": str(e)}
    final_dest = _unique_destination(dest)

    if is_dry_run():
        log_event("rename_file", {"source": str(src), "destination": str(final_dest)}, phase="dry_run")
        return {"ok": True, "dry_run": True, "source": str(src), "destination": str(final_dest)}

    try:
        src.rename(final_dest)
    except OSError as e:
        return {"ok": False, "error": str(e)}
    record_rename(str(src), str(final_dest))
    log_event("rename_file", {"source": str(src), "destination": str(final_dest)}, phase="executed")
    return {"ok": True, "source": str(src), "destination": str(final_dest)}


def create_folder(path: str) -> dict[str, Any]:
    try:
        folder = require_allowed_path(Path(path))
    except (OSError, ValueError, PermissionError) as e:
        return {"ok": False, "error": str(e)}
    try:
        if folder.exists():
            if folder.is_dir():
                log_event("create_folder_exists", {"path": str(folder)}, phase="skipped")
                return {"ok": True, "path": str(folder), "note": "already existed"}
            return {"ok": False, "error": f"Path exists and is not a directory: {folder}"}

        if is_dry_run():
            log_event("create_folder", {"path": str(folder)}, phase="dry_run")
            return {"ok": True, "dry_run": True, "path": str(folder)}

        folder.mkdir(parents=True, exist_ok=False)
    except OSError as e:
        return {"ok": False, "error": str(e)}
    record_create_folder(str(folder))
    log_event("create_folder", {"path": str(folder)}, phase="executed")
    return {"ok": True, "path": str(folder)}


# ---------------------------------------------------------------------------
# Desktop / folder organization
# ---------------------------------------------------------------------------


def preview_plan_for_desktop_cleanup(desktop_path: str | None = None) -> dict[str, Any]:
    path = Path(desktop_path) if desktop_path else get_default_desktop_path()
    try:
        desktop = require_allowed_path_readonly(path)
    except (OSError, ValueError, PermissionError) as e:
        return {"ok": False, "error": str(e)}
    plan = _folder_cleanup_plan(desktop)
    # Group by category for display
    by_category: dict[str, list[str]] = defaultdict(list)
    for step in plan:
        by_category[step["category"]].append(Path(step["source"]).name)

    log_event("preview_plan_for_desktop_cleanup", {"desktop": str(desktop), "moves": len(plan)}, phase="preview")
    return {
        "ok": True,
        "desktop": str(desktop),
        "planned_moves": plan,
        "count": len(plan),
        "by_category": dict(by_category),
    }


def preview_organize_folder(path: str, mode: str = "type") -> dict[str, Any]:
    """Preview organization of any allowed folder by type, month, or year."""
    try:
        folder = require_allowed_path_readonly(Path(path))
    except (OSError, ValueError, PermissionError) as e:
        return {"ok": False, "error": str(e)}
    if not folder.is_dir():
        return {"ok": False, "error": f"Not a directory: {folder}"}

    if mode in ("month", "year"):
        plan = _date_based_plan(folder, group_by=mode)
        groups: dict[str, list[str]] = defaultdict(list)
        for step in plan:
            groups[step["group"]].append(Path(step["source"]).name)
    else:
        plan = _folder_cleanup_plan(folder)
        groups = defaultdict(list)
        for step in plan:
            groups[step.get("category", "Other")].append(Path(step["source"]).name)

    log_event("preview_organize_folder", {"path": str(folder), "mode": mode, "moves": len(plan)}, phase="preview")
    return {
        "ok": True,
        "path": str(folder),
        "mode": mode,
        "planned_moves": plan,
        "count": len(plan),
        "groups": dict(groups),
    }


def organize_desktop_by_type(desktop_path: str | None = None) -> dict[str, Any]:
    path = Path(desktop_path) if desktop_path else get_default_desktop_path()
    try:
        desktop = require_allowed_path(path)
    except (OSError, ValueError, PermissionError) as e:
        return {"ok": False, "error": str(e)}
    plan = _folder_cleanup_plan(desktop)
    return _execute_organize_plan(plan, str(desktop))


def organize_folder(path: str, mode: str = "type") -> dict[str, Any]:
    """Organize files in any allowed folder by type, month, or year."""
    try:
        folder = require_allowed_path(Path(path))
    except (OSError, ValueError, PermissionError) as e:
        return {"ok": False, "error": str(e)}
    if not folder.is_dir():
        return {"ok": False, "error": f"Not a directory: {folder}"}

    if mode in ("month", "year"):
        plan = _date_based_plan(folder, group_by=mode)
    else:
        plan = _folder_cleanup_plan(folder)
    return _execute_organize_plan(plan, str(folder))


def _execute_organize_plan(plan: list[dict[str, Any]], root_path: str) -> dict[str, Any]:
    """Execute an organization plan (shared by desktop and folder organize)."""
    results: list[dict[str, Any]] = []
    errors: list[str] = []

    if is_dry_run():
        log_event("organize", {"path": root_path, "planned": len(plan)}, phase="dry_run")
        return {
            "ok": True,
            "dry_run": True,
            "path": root_path,
            "planned_moves": plan,
            "count": len(plan),
        }

    for step in plan:
        src = Path(step["source"])
        dest = Path(step["destination"])
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if src.resolve() == dest.resolve():
                continue
            final = _unique_destination(dest)
            shutil.move(str(src), str(final))
            record_organize_move(str(src), str(final))
            results.append({"source": str(src), "destination": str(final)})
            log_event("organize_move", {"source": str(src), "destination": str(final)}, phase="executed")
        except OSError as e:
            errors.append(f"{src} -> {dest}: {e}")
    return {
        "ok": len(errors) == 0,
        "path": root_path,
        "moved": results,
        "errors": errors,
        "count": len(results),
    }


def open_url(url: str) -> dict[str, Any]:
    u = url.strip()
    if not (u.startswith("http://") or u.startswith("https://")):
        return {"ok": False, "error": "Only http(s) URLs are allowed."}
    try:
        opened = webbrowser.open(u)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
    log_event("open_url", {"url": u, "opened": opened}, phase="executed")
    return {"ok": True, "url": u, "opened": bool(opened)}


# ---------------------------------------------------------------------------
# Shell execution (constrained — see shell_guard.py)
# ---------------------------------------------------------------------------

_SHELL_TIMEOUT = 30


def run_command(command: str, working_dir: str | None = None) -> dict[str, Any]:
    """Run a shell command after deterministic safety classification.

    Every command passes through shell_guard.classify_command() BEFORE execution.
    BLOCKED commands are rejected unconditionally — no user override.
    SAFE and RISKY commands require user confirmation (handled by core.py).
    This tool is NOT undoable — shell side effects cannot be reliably reversed.
    shell=True is NEVER used.
    """
    # Classify
    verdict = classify_command(command, extra_allowlist=get_extra_allowlist())

    # BLOCKED — reject immediately, no confirmation, no execution
    if verdict.tier == "blocked":
        log_event("run_command_blocked", {
            "command": verdict.command,
            "executable": verdict.executable,
            "reason": verdict.reason,
        }, phase="blocked")
        return {
            "ok": False,
            "blocked": True,
            "error": f"Command blocked: {verdict.reason}",
            "command": verdict.command,
            "tier": "blocked",
        }

    # Validate working directory
    try:
        cwd = validate_working_dir(working_dir)
    except (PermissionError, ValueError, OSError) as e:
        return {"ok": False, "error": str(e)}

    # Dry-run: simulate only
    if is_dry_run():
        log_event("run_command", {
            "command": verdict.command,
            "working_dir": str(cwd),
            "tier": verdict.tier,
        }, phase="dry_run")
        return {
            "ok": True,
            "dry_run": True,
            "command": verdict.command,
            "working_dir": str(cwd),
            "tier": verdict.tier,
        }

    # Execute with shell=False
    try:
        tokens = shlex.split(verdict.command, posix=False)
    except ValueError as e:
        return {"ok": False, "error": f"Failed to parse command: {e}"}

    try:
        result = subprocess.run(
            tokens,
            capture_output=True,
            text=True,
            timeout=_SHELL_TIMEOUT,
            cwd=str(cwd),
            shell=False,  # NEVER shell=True
        )
    except subprocess.TimeoutExpired:
        log_event("run_command_timeout", {
            "command": verdict.command,
            "timeout": _SHELL_TIMEOUT,
        }, phase="error")
        return {"ok": False, "error": f"Command timed out after {_SHELL_TIMEOUT}s."}
    except FileNotFoundError:
        return {"ok": False, "error": f"Executable not found: {verdict.executable}"}
    except OSError as e:
        return {"ok": False, "error": f"Failed to run command: {e}"}

    # Process output
    stdout_raw = result.stdout or ""
    stderr_raw = result.stderr or ""

    stdout_redacted = redact_secrets(stdout_raw)
    stderr_redacted = redact_secrets(stderr_raw)

    stdout_final, stdout_truncated = truncate_output(stdout_redacted)
    stderr_final, stderr_truncated = truncate_output(stderr_redacted)

    log_event("run_command", {
        "command": verdict.command,
        "working_dir": str(cwd),
        "tier": verdict.tier,
        "exit_code": result.returncode,
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
    }, phase="executed")

    return {
        "ok": result.returncode == 0,
        "command": verdict.command,
        "working_dir": str(cwd),
        "tier": verdict.tier,
        "exit_code": result.returncode,
        "stdout": stdout_final,
        "stderr": stderr_final,
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
    }


# ---------------------------------------------------------------------------
# File writing
# ---------------------------------------------------------------------------

_WRITE_MAX_BYTES = 500_000  # 500 KB


def write_file(path: str, content: str, overwrite: bool = False) -> dict[str, Any]:
    """Write text content to a file within allowed roots.

    New files can be undone (sent to Recycle Bin).
    Overwrites create a .bak backup first; undo restores the backup.
    Content is capped at 500 KB.  Only UTF-8 text is accepted.
    """
    try:
        fp = require_allowed_path(Path(path))
    except (OSError, ValueError, PermissionError) as e:
        return {"ok": False, "error": str(e)}

    # Validate content
    if not isinstance(content, str):
        return {"ok": False, "error": "Content must be a text string."}
    encoded = content.encode("utf-8")
    if len(encoded) > _WRITE_MAX_BYTES:
        return {"ok": False, "error": f"Content too large: {len(encoded)} bytes (max {_WRITE_MAX_BYTES})."}

    # Check overwrite
    file_exists = fp.exists()
    if file_exists and not overwrite:
        return {
            "ok": False,
            "error": f"File already exists: {fp}. Set overwrite=true to replace it (a .bak backup will be created).",
        }

    # Dry run
    if is_dry_run():
        action = "overwrite" if file_exists else "create"
        log_event("write_file", {"path": str(fp), "action": action, "bytes": len(encoded)}, phase="dry_run")
        return {"ok": True, "dry_run": True, "path": str(fp), "action": action, "bytes": len(encoded)}

    # Backup before overwrite
    backup_path: str | None = None
    if file_exists and overwrite:
        bak = unique_backup_path(fp)
        try:
            shutil.copy2(str(fp), str(bak))
            backup_path = str(bak)
        except OSError as e:
            return {"ok": False, "error": f"Failed to create backup: {e}"}

    # Write
    try:
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")
    except OSError as e:
        return {"ok": False, "error": f"Failed to write file: {e}"}

    action = "overwritten" if file_exists else "created"
    record_write_file(str(fp), backup_path)
    log_event("write_file", {
        "path": str(fp), "action": action, "bytes": len(encoded),
        "backup": backup_path,
    }, phase="executed")

    result: dict[str, Any] = {
        "ok": True,
        "path": str(fp),
        "action": action,
        "bytes": len(encoded),
    }
    if backup_path:
        result["backup_path"] = backup_path
    return result


# ---------------------------------------------------------------------------
# Desktop, process, browser, and editor wrappers
# ---------------------------------------------------------------------------


def take_screenshot(region: dict[str, int] | None = None) -> dict[str, Any]:
    return desktop_tools.take_screenshot(region)


def ocr_image(path: str, region: dict[str, int] | None = None) -> dict[str, Any]:
    return desktop_tools.ocr_image(path, region)


def list_windows() -> dict[str, Any]:
    return desktop_tools.list_windows()


def get_active_window() -> dict[str, Any]:
    return desktop_tools.get_active_window()


def focus_window(window_id: int) -> dict[str, Any]:
    return desktop_tools.focus_window(window_id)


def move_mouse(x: int, y: int, duration_ms: int = 0) -> dict[str, Any]:
    return desktop_tools.move_mouse(x, y, duration_ms)


def click(x: int, y: int, button: str = "left", clicks: int = 1) -> dict[str, Any]:
    return desktop_tools.click(x, y, button, clicks)


def scroll(amount: int, x: int | None = None, y: int | None = None) -> dict[str, Any]:
    return desktop_tools.scroll(amount, x, y)


def type_text(text: str) -> dict[str, Any]:
    return desktop_tools.type_text(text)


def press_hotkey(keys: list[str]) -> dict[str, Any]:
    return desktop_tools.press_hotkey(keys)


def list_processes(query: str | None = None, limit: int = 25) -> dict[str, Any]:
    return process_tools.list_processes(query, limit)


def start_process(executable: str, args: list[str] | None = None, working_dir: str | None = None) -> dict[str, Any]:
    return process_tools.start_process(executable, args, working_dir)


def stop_process(pid: int, force: bool = False) -> dict[str, Any]:
    return process_tools.stop_process(pid, force)


def wait_seconds(seconds: float) -> dict[str, Any]:
    seconds = max(0.0, min(float(seconds), 60.0))
    if is_dry_run():
        return {"ok": True, "dry_run": True, "seconds": seconds}
    time.sleep(seconds)
    return {"ok": True, "seconds": seconds}


def wait_for_window(title_query: str, timeout_sec: float = 10.0) -> dict[str, Any]:
    return desktop_tools.wait_for_window(title_query, timeout_sec)


def wait_for_file(path: str, timeout_sec: float = 10.0) -> dict[str, Any]:
    deadline = time.time() + max(0.1, timeout_sec)
    try:
        target = require_allowed_path_readonly(Path(path))
    except (OSError, ValueError, PermissionError) as e:
        return {"ok": False, "error": str(e)}
    while time.time() < deadline:
        if target.exists():
            return {"ok": True, "path": str(target), "exists": True}
        time.sleep(0.1)
    return {"ok": False, "error": f"File not found within {timeout_sec}s: {target}"}


def wait_for_process_exit(pid: int, timeout_sec: float = 10.0) -> dict[str, Any]:
    return process_tools.wait_for_process_exit(pid, timeout_sec)


def browser_navigate(url: str) -> dict[str, Any]:
    return browser_tools.browser_navigate(url)


def browser_click(selector: str, timeout_sec: float = 10.0) -> dict[str, Any]:
    return browser_tools.browser_click(selector, timeout_sec)


def browser_fill(selector: str, text: str, timeout_sec: float = 10.0) -> dict[str, Any]:
    return browser_tools.browser_fill(selector, text, timeout_sec)


def browser_press(selector: str, key: str, timeout_sec: float = 10.0) -> dict[str, Any]:
    return browser_tools.browser_press(selector, key, timeout_sec)


def browser_download(
    url: str | None = None,
    click_selector: str | None = None,
    timeout_sec: float = 10.0,
) -> dict[str, Any]:
    return browser_tools.browser_download(url, click_selector, timeout_sec)


def browser_extract_text(selector: str | None = None, timeout_sec: float = 10.0) -> dict[str, Any]:
    return browser_tools.browser_extract_text(selector, timeout_sec)


def browser_wait_for(selector: str, timeout_sec: float = 10.0) -> dict[str, Any]:
    return browser_tools.browser_wait_for(selector, timeout_sec)


def read_file_range(path: str, start_line: int, end_line: int) -> dict[str, Any]:
    return editor_tools.read_file_range(path, start_line, end_line)


def replace_in_file(path: str, old_text: str, new_text: str, replace_all: bool = False) -> dict[str, Any]:
    return editor_tools.replace_in_file(path, old_text, new_text, replace_all)


def append_file(path: str, content: str) -> dict[str, Any]:
    return editor_tools.append_file(path, content)


def apply_patch(path: str, unified_diff: str) -> dict[str, Any]:
    return editor_tools.apply_patch(path, unified_diff)


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def dispatch_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    handlers: dict[str, ToolHandler] = {
        # Read-only
        "list_directory": lambda **a: list_directory(a["path"]),
        "analyze_directory": lambda **a: analyze_directory(a["path"]),
        "largest_files": lambda **a: largest_files(a["path"], a.get("limit", 10)),
        "file_type_summary": lambda **a: file_type_summary(a["path"]),
        "read_text_file": lambda **a: read_text_file(a["path"], a.get("max_chars", 5000)),
        "search_files": lambda **a: search_files(a["path"], a["query"]),
        "recent_files": lambda **a: recent_files(a["path"], a.get("limit", 15)),
        "directory_tree": lambda **a: directory_tree(a["path"], a.get("depth", 2)),
        "preview_plan_for_desktop_cleanup": lambda **a: preview_plan_for_desktop_cleanup(a.get("desktop_path")),
        "preview_organize_folder": lambda **a: preview_organize_folder(a["path"], a.get("mode", "type")),
        "take_screenshot": lambda **a: take_screenshot(a.get("region")),
        "ocr_image": lambda **a: ocr_image(a["path"], a.get("region")),
        "list_windows": lambda **a: list_windows(),
        "get_active_window": lambda **a: get_active_window(),
        "list_processes": lambda **a: list_processes(a.get("query"), a.get("limit", 25)),
        "read_file_range": lambda **a: read_file_range(a["path"], a["start_line"], a["end_line"]),
        "wait_seconds": lambda **a: wait_seconds(a["seconds"]),
        "wait_for_window": lambda **a: wait_for_window(a["title_query"], a.get("timeout_sec", 10.0)),
        "wait_for_file": lambda **a: wait_for_file(a["path"], a.get("timeout_sec", 10.0)),
        "wait_for_process_exit": lambda **a: wait_for_process_exit(a["pid"], a.get("timeout_sec", 10.0)),
        "browser_extract_text": lambda **a: browser_extract_text(a.get("selector"), a.get("timeout_sec", 10.0)),
        "browser_wait_for": lambda **a: browser_wait_for(a["selector"], a.get("timeout_sec", 10.0)),
        # Mutating
        "move_file": lambda **a: move_file(a["source"], a["destination"]),
        "rename_file": lambda **a: rename_file(a["source"], a["new_name"]),
        "create_folder": lambda **a: create_folder(a["path"]),
        "organize_desktop_by_type": lambda **a: organize_desktop_by_type(a.get("desktop_path")),
        "organize_folder": lambda **a: organize_folder(a["path"], a.get("mode", "type")),
        "write_file": lambda **a: write_file(a["path"], a["content"], a.get("overwrite", False)),
        "focus_window": lambda **a: focus_window(a["window_id"]),
        "start_process": lambda **a: start_process(a["executable"], a.get("args"), a.get("working_dir")),
        "stop_process": lambda **a: stop_process(a["pid"], a.get("force", False)),
        "move_mouse": lambda **a: move_mouse(a["x"], a["y"], a.get("duration_ms", 0)),
        "click": lambda **a: click(a["x"], a["y"], a.get("button", "left"), a.get("clicks", 1)),
        "scroll": lambda **a: scroll(a["amount"], a.get("x"), a.get("y")),
        "type_text": lambda **a: type_text(a["text"]),
        "press_hotkey": lambda **a: press_hotkey(a["keys"]),
        "browser_navigate": lambda **a: browser_navigate(a["url"]),
        "browser_click": lambda **a: browser_click(a["selector"], a.get("timeout_sec", 10.0)),
        "browser_fill": lambda **a: browser_fill(a["selector"], a["text"], a.get("timeout_sec", 10.0)),
        "browser_press": lambda **a: browser_press(a["selector"], a["key"], a.get("timeout_sec", 10.0)),
        "browser_download": lambda **a: browser_download(a.get("url"), a.get("click_selector"), a.get("timeout_sec", 10.0)),
        "replace_in_file": lambda **a: replace_in_file(a["path"], a["old_text"], a["new_text"], a.get("replace_all", False)),
        "append_file": lambda **a: append_file(a["path"], a["content"]),
        "apply_patch": lambda **a: apply_patch(a["path"], a["unified_diff"]),
        # Shell (constrained)
        "run_command": lambda **a: run_command(a["command"], a.get("working_dir")),
        # Browser
        "open_url": lambda **a: open_url(a["url"]),
    }
    fn = handlers.get(name)
    if not fn:
        return {"ok": False, "error": f"Unknown tool: {name}"}
    try:
        return fn(**arguments)
    except TypeError as e:
        return {"ok": False, "error": f"Invalid arguments for {name}: {e}"}
