"""Application configuration, resolved Windows paths, and runtime state."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

_SKIP_DOTENV_VALUES = {"1", "true", "yes", "on"}


def _load_project_dotenv() -> None:
    """Load this repo's .env, preferring it over inherited shell variables."""
    if os.environ.get("XAI_ASSISTANT_SKIP_DOTENV", "").strip().lower() in _SKIP_DOTENV_VALUES:
        return
    load_dotenv(Path(__file__).with_name(".env"), override=True)


_load_project_dotenv()

# ---------------------------------------------------------------------------
# Available models
# ---------------------------------------------------------------------------

DEFAULT_MODEL = "grok-4.3-latest"

MODELS: dict[str, str] = {
    DEFAULT_MODEL: DEFAULT_MODEL,
}

MODEL_ALIASES: dict[str, str] = {
    "fast": DEFAULT_MODEL,
    "quality": DEFAULT_MODEL,
    "code": DEFAULT_MODEL,
}

# ---------------------------------------------------------------------------
# Runtime mutable state (not persisted across sessions)
# ---------------------------------------------------------------------------

_runtime_model: str | None = None
_user_set_model: bool = False  # True if user explicitly chose a model via /model
_dry_run: bool = False
_verbose: bool = True  # True = verbose (default), False = concise
_auto_switch_models: bool = True
_last_working_folder: Path | None = None


def set_dry_run(enabled: bool, *, persist: bool = False) -> None:
    global _dry_run
    _dry_run = enabled
    if persist:
        _set_web_setting("dry_run", enabled)


def is_dry_run() -> bool:
    return _dry_run


def set_runtime_model(model: str, *, user_initiated: bool = True, persist: bool = False) -> None:
    global _runtime_model, _user_set_model
    normalized_model = normalize_model_id(model)
    _runtime_model = normalized_model
    if user_initiated:
        _user_set_model = True
    if persist:
        _set_web_setting("model", normalized_model)


def get_runtime_model_override() -> str | None:
    return _runtime_model


def normalize_model_id(model: str) -> str:
    """Map old local Grok model settings to the single supported model."""
    raw = model.strip()
    if raw.startswith("grok-") and raw != DEFAULT_MODEL:
        return DEFAULT_MODEL
    return raw


def user_has_set_model() -> bool:
    """True if the user explicitly chose a model this session (via /model)."""
    return _user_set_model


def set_verbose(enabled: bool, *, persist: bool = False) -> None:
    global _verbose
    _verbose = enabled
    if persist:
        _set_web_setting("verbose", enabled)


def is_verbose() -> bool:
    return _verbose


def set_auto_switch_models(enabled: bool, *, persist: bool = False) -> None:
    global _auto_switch_models
    _auto_switch_models = enabled
    if persist:
        _set_web_setting("auto_switch_models", enabled)


def is_auto_switch_models() -> bool:
    return _auto_switch_models


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


def _default_allowed_roots() -> list[Path]:
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


def _settings_path() -> Path:
    return get_state_dir() / "settings.json"


def _load_settings() -> dict:
    import json
    try:
        return json.loads(_settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_settings(data: dict) -> None:
    import json
    path = _settings_path()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _set_web_setting(key: str, value: object) -> None:
    data = _load_settings()
    web = data.get("web")
    if not isinstance(web, dict):
        web = {}
    web[key] = value
    data["web"] = web
    _save_settings(data)


def get_web_settings() -> dict:
    settings = _load_settings().get("web")
    return dict(settings) if isinstance(settings, dict) else {}


def load_persisted_web_settings() -> None:
    """Apply persisted non-sensitive web settings to runtime state."""
    settings = get_web_settings()
    model = settings.get("model")
    if isinstance(model, str) and model.strip():
        set_runtime_model(model.strip(), user_initiated=False)
    if isinstance(settings.get("dry_run"), bool):
        set_dry_run(bool(settings["dry_run"]))
    if isinstance(settings.get("verbose"), bool):
        set_verbose(bool(settings["verbose"]))
    if isinstance(settings.get("auto_switch_models"), bool):
        set_auto_switch_models(bool(settings["auto_switch_models"]))


def get_allowed_roots() -> list[Path]:
    """
    Paths under which mutating file operations are permitted.
    Precedence: XAI_ASSISTANT_ALLOWED_ROOTS env var > state/settings.json > defaults.
    """
    raw = os.environ.get("XAI_ASSISTANT_ALLOWED_ROOTS")
    if raw:
        roots = [Path(p.strip()).expanduser().resolve() for p in raw.split(";") if p.strip()]
        if roots:
            return roots
    settings = _load_settings()
    saved = settings.get("allowed_roots")
    if isinstance(saved, list) and saved:
        out: list[Path] = []
        seen: set[str] = set()
        for s in saved:
            try:
                r = Path(str(s)).expanduser().resolve()
            except OSError:
                continue
            key = str(r).casefold()
            if key not in seen:
                seen.add(key)
                out.append(r)
        if out:
            return out
    return _default_allowed_roots()


def set_allowed_roots(paths: list[Path]) -> None:
    """Persist a new allowed-roots list to state/settings.json."""
    data = _load_settings()
    data["allowed_roots"] = [str(Path(p).expanduser().resolve()) for p in paths]
    _save_settings(data)


def reset_allowed_roots() -> None:
    """Clear any persisted override; revert to defaults."""
    data = _load_settings()
    data.pop("allowed_roots", None)
    _save_settings(data)


# ---------------------------------------------------------------------------
# API / model config
# ---------------------------------------------------------------------------


def get_xai_api_key() -> str | None:
    return os.environ.get("XAI_API_KEY")


def get_xai_model() -> str:
    override = get_runtime_model_override()
    if override:
        return override
    return normalize_model_id(os.environ.get("XAI_MODEL", DEFAULT_MODEL))


def get_coding_model() -> str | None:
    """Return the optional coding model ID, normalized away from retired IDs."""
    raw = os.environ.get("XAI_CODING_MODEL", "").strip()
    return normalize_model_id(raw) if raw else None


def get_max_tool_loops() -> int:
    raw = os.environ.get("XAI_MAX_TOOL_LOOPS", "24")
    try:
        val = int(raw)
        return max(1, min(val, 50))
    except ValueError:
        return 24


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
