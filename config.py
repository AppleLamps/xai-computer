"""Application configuration, resolved Windows paths, and runtime state."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Available models
# ---------------------------------------------------------------------------

MODELS: dict[str, str] = {
    "fast": "grok-4-1-fast-reasoning",
    "quality": "grok-4.20-0309-reasoning",
}

# ---------------------------------------------------------------------------
# Runtime mutable state (not persisted across sessions)
# ---------------------------------------------------------------------------

_runtime_model: str | None = None
_dry_run: bool = False
_verbose: bool = True  # True = verbose (default), False = concise
_last_working_folder: Path | None = None


def set_dry_run(enabled: bool) -> None:
    global _dry_run
    _dry_run = enabled


def is_dry_run() -> bool:
    return _dry_run


def set_runtime_model(model: str) -> None:
    global _runtime_model
    _runtime_model = model


def get_runtime_model_override() -> str | None:
    return _runtime_model


def set_verbose(enabled: bool) -> None:
    global _verbose
    _verbose = enabled


def is_verbose() -> bool:
    return _verbose


def set_last_working_folder(path: Path) -> None:
    global _last_working_folder
    _last_working_folder = path


def get_last_working_folder() -> Path | None:
    return _last_working_folder


# ---------------------------------------------------------------------------
# Windows desktop resolution
# ---------------------------------------------------------------------------


def _windows_desktop() -> Path:
    """Resolve the real Windows desktop folder (handles OneDrive redirection)."""
    try:
        import ctypes
        from ctypes import wintypes

        csidl_desktop = 0  # CSIDL_DESKTOP
        buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
        if ctypes.windll.shell32.SHGetFolderPathW(None, csidl_desktop, None, 0, buf) == 0:
            return Path(buf.value)
    except Exception:
        pass
    return Path.home() / "Desktop"


def get_default_desktop_path() -> Path:
    override = os.environ.get("XAI_ASSISTANT_DESKTOP")
    if override:
        return Path(override).expanduser().resolve()
    return _windows_desktop().resolve()


# ---------------------------------------------------------------------------
# Allowed roots
# ---------------------------------------------------------------------------


def get_allowed_roots() -> list[Path]:
    """
    Paths under which mutating file operations are permitted.
    Override with XAI_ASSISTANT_ALLOWED_ROOTS as a semicolon-separated list of absolute paths.
    """
    raw = os.environ.get("XAI_ASSISTANT_ALLOWED_ROOTS")
    if raw:
        roots = [Path(p.strip()).expanduser().resolve() for p in raw.split(";") if p.strip()]
        if roots:
            return roots
    home = Path.home().resolve()
    defaults = [
        get_default_desktop_path(),
        home / "Documents",
        home / "Downloads",
        home / "Desktop",
    ]
    seen: set[str] = set()
    out: list[Path] = []
    for p in defaults:
        try:
            r = p.resolve()
        except OSError:
            continue
        key = str(r).casefold()
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


# ---------------------------------------------------------------------------
# API / model config
# ---------------------------------------------------------------------------


def get_xai_api_key() -> str | None:
    return os.environ.get("XAI_API_KEY")


def get_xai_model() -> str:
    override = get_runtime_model_override()
    if override:
        return override
    return os.environ.get("XAI_MODEL", MODELS["fast"])


def get_max_tool_loops() -> int:
    raw = os.environ.get("XAI_MAX_TOOL_LOOPS", "12")
    try:
        val = int(raw)
        return max(1, min(val, 50))
    except ValueError:
        return 12


# ---------------------------------------------------------------------------
# Logging / state directories
# ---------------------------------------------------------------------------


def get_log_dir() -> Path:
    base = Path(__file__).resolve().parent
    d = base / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_log_path() -> Path:
    return get_log_dir() / "actions.log"


def get_state_dir() -> Path:
    base = Path(__file__).resolve().parent
    d = base / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# Web search
# ---------------------------------------------------------------------------


def web_search_enabled() -> bool:
    return os.environ.get("XAI_ENABLE_WEB_SEARCH", "").strip().lower() in ("1", "true", "yes", "on")
