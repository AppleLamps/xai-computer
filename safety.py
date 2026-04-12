"""Path allowlisting, traversal protection, and confirmation helpers."""

from __future__ import annotations

import re
from pathlib import Path

from config import get_allowed_roots

_CONFIRM_PATTERN = re.compile(
    r"^\s*(yes|y|confirm|confirmed|approve|approved)\s*$",
    re.IGNORECASE,
)

# Locations that are never valid targets even if inside an allowed root.
_BLOCKED_NAMES: frozenset[str] = frozenset(
    {
        "windows",
        "system32",
        "program files",
        "program files (x86)",
        "programdata",
        "$recycle.bin",
        "recovery",
        "system volume information",
    }
)


def is_affirmative_confirmation(text: str) -> bool:
    return bool(_CONFIRM_PATTERN.match(text.strip()))


def _is_within_root(path: Path, root: Path) -> bool:
    try:
        path = path.resolve()
        root = root.resolve()
    except OSError:
        return False
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _has_traversal(raw: str) -> bool:
    """Reject suspicious traversal sequences before resolution."""
    return ".." in raw


def _hits_blocked_location(resolved: Path) -> bool:
    """Reject paths that land in dangerous OS locations."""
    parts_lower = [p.casefold() for p in resolved.parts]
    for blocked in _BLOCKED_NAMES:
        if blocked in parts_lower:
            return True
    return False


def is_path_allowed(path: Path, roots: list[Path] | None = None) -> bool:
    roots = roots or get_allowed_roots()
    try:
        resolved = path.resolve()
    except OSError:
        return False
    if _hits_blocked_location(resolved):
        return False
    return any(_is_within_root(resolved, r) for r in roots)


def _validate_and_resolve(path: Path, roots: list[Path] | None = None) -> Path:
    """Common validation: traversal check, resolution, blocked-location check, root check."""
    roots = roots or get_allowed_roots()

    raw_str = str(path)
    if _has_traversal(raw_str):
        raise PermissionError(f"Path contains suspicious traversal: {raw_str}")

    try:
        resolved = path.expanduser().resolve()
    except OSError as e:
        raise ValueError(f"Invalid path: {path}") from e

    if _hits_blocked_location(resolved):
        raise PermissionError(f"Path targets a protected system location: {resolved}")

    if not is_path_allowed(resolved, roots):
        raise PermissionError(
            f"Path not allowed (outside approved locations): {resolved}. "
            f"Approved roots: {', '.join(str(r) for r in roots)}"
        )
    return resolved


def require_allowed_path(path: Path, roots: list[Path] | None = None) -> Path:
    """Resolve *path* and raise if it is outside approved roots or hits a blocked location."""
    return _validate_and_resolve(path, roots)


def require_allowed_path_readonly(path: Path, roots: list[Path] | None = None) -> Path:
    """Same validation as require_allowed_path, used by read-only tools for clarity."""
    return _validate_and_resolve(path, roots)


def is_system_or_protected_name(name: str) -> bool:
    n = name.casefold()
    protected = {
        "desktop.ini",
        "thumbs.db",
        "ntuser.dat",
        "ntuser.ini",
        "ntuser.pol",
    }
    if n in protected:
        return True
    if n.startswith("~$"):  # Office lock files
        return True
    return False


def is_hidden_name(name: str) -> bool:
    if name.startswith("."):
        return True
    # Windows hidden attribute check (best effort)
    try:
        import ctypes
        attrs = ctypes.windll.kernel32.GetFileAttributesW(name)
        if attrs != -1 and (attrs & 0x2):  # FILE_ATTRIBUTE_HIDDEN
            return True
    except Exception:
        pass
    return False
