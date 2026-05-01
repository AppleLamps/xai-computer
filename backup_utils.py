"""Shared helpers for unique backup file paths."""

from __future__ import annotations

from pathlib import Path


def unique_backup_path(path: Path) -> Path:
    """Return an unused backup path for *path* without creating it."""
    base = path.with_suffix(path.suffix + ".bak")
    if not base.exists():
        return base
    n = 1
    while True:
        candidate = base.with_name(f"{base.name}.{n}")
        if not candidate.exists():
            return candidate
        n += 1
