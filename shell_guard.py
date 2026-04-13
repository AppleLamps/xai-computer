"""Deterministic safety gate for shell command execution.

Every proposed command is classified into one of four tiers BEFORE execution:
  BLOCKED  — rejected unconditionally, no override possible
  SAFE     — on explicit allowlist, runs after user confirmation
  RISKY    — not allowlisted but not blocked, runs after confirmation + warning
  BLOCKED  — via structural pattern (chaining, subshell, redirection)

The safety path is fully deterministic: no LLM inference, no heuristics.
Allowlisting is the primary gate. Blocklisting catches known-dangerous commands.
Structural patterns catch evasion attempts.
"""

from __future__ import annotations

import os
import re
import shlex
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config import get_allowed_roots

# ---------------------------------------------------------------------------
# Classification result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CommandVerdict:
    """Result of classifying a command."""
    tier: str          # "blocked", "safe", "risky"
    reason: str        # human-readable explanation
    command: str       # normalized command string
    executable: str    # first token (the program name)


# ---------------------------------------------------------------------------
# Permanent blocklist — never overridden, not even by user approval
# ---------------------------------------------------------------------------

_BLOCKED_EXECUTABLES: frozenset[str] = frozenset({
    # Deletion / destruction
    "rm", "rmdir", "del", "erase", "shred",
    "format", "mkfs", "dd",
    # System control
    "shutdown", "reboot", "logoff",
    "taskkill", "tskill", "pskill",
    # Registry / system modification
    "reg", "regedit", "regsvr32",
    "sfc", "dism", "bcdedit", "bcdboot",
    "cipher",
    # Credential / account manipulation
    "net", "runas", "cmdkey", "certutil",
    # Dangerous utilities
    "attrib", "icacls", "takeown", "cacls",
    "sc",  # service control
    "wmic",
    "mshta", "cscript", "wscript",
    # Download-and-execute vectors
    "curl", "wget", "invoke-webrequest", "invoke-restmethod",
    "start-bitstransfer",
    "bitsadmin",
})

_BLOCKED_SUBSTRINGS: list[str] = [
    # PowerShell encoded command bypass
    "-encodedcommand",
    "-enc ",
    "-e ",  # short alias for -EncodedCommand
    # Pipe-to-shell patterns
    "| bash", "| sh", "| python", "| powershell", "| pwsh", "| cmd",
    # Registry
    "reg delete", "reg add",
    # Hidden file creation
    "attrib +h",
    # Secure wipe
    "cipher /w",
]

# ---------------------------------------------------------------------------
# Safe allowlist — confirmed before running, but not warned
# ---------------------------------------------------------------------------

_SAFE_EXECUTABLES: frozenset[str] = frozenset({
    # Directory listing
    "dir", "ls",
    # File reading (read-only)
    "type", "cat", "head", "tail", "more",
    # Search / info
    "echo", "where", "which",
    "whoami", "hostname",
    "systeminfo", "tasklist",
})

# Powerful executables that should only be safe for exact allowlisted commands.
_RESTRICTED_EXECUTABLES: frozenset[str] = frozenset({
    "python", "pip", "pytest",
    "git",
    "node", "npm", "npx",
    "ipconfig",
    "date", "time",
})

# Specific multi-word commands that are safe
_SAFE_FULL_COMMANDS: frozenset[str] = frozenset({
    "python --version",
    "python -V",
    "pip list",
    "pip --version",
    "pip freeze",
    "pytest --version",
    "git status",
    "git log",
    "git log --oneline",
    "git diff",
    "git diff --stat",
    "git branch",
    "git branch -a",
    "git remote -v",
    "ipconfig",
    "ipconfig /all",
    "date",
    "time",
    "systeminfo",
    "tasklist",
    "whoami",
    "hostname",
})

# pip subcommands that are blocked even though pip is on the safe list
_BLOCKED_PIP_SUBCOMMANDS: frozenset[str] = frozenset({
    "install", "uninstall", "download",
})

# git subcommands that are blocked
_BLOCKED_GIT_SUBCOMMANDS: frozenset[str] = frozenset({
    "push", "reset", "clean", "rm", "checkout",  # destructive
    "config",  # can modify settings
})

# ---------------------------------------------------------------------------
# Structural pattern detection — catches evasion attempts
# ---------------------------------------------------------------------------

_CHAINING_PATTERN = re.compile(r"[;&|]{1,2}")
_REDIRECT_PATTERN = re.compile(r"[<>]{1,2}")
_SUBSHELL_PATTERN = re.compile(r"\$\(|`")
_BACKTICK_PATTERN = re.compile(r"`")

# System paths that commands must never reference
_BLOCKED_PATHS: list[str] = [
    r"c:\windows",
    r"c:\system32",
    r"c:\program files",
    r"c:\program files (x86)",
    r"c:\programdata",
    r"c:\$recycle.bin",
    r"c:\recovery",
    "system32",
    "syswow64",
]

# Secret patterns to redact from output
_SECRET_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(sk-[a-zA-Z0-9]{20,})", re.IGNORECASE),
    re.compile(r"(xai-[a-zA-Z0-9]{20,})", re.IGNORECASE),
    re.compile(r"(ghp_[a-zA-Z0-9]{20,})"),
    re.compile(r"(glpat-[a-zA-Z0-9]{20,})"),
    re.compile(r"(password\s*=\s*\S+)", re.IGNORECASE),
    re.compile(r"(token\s*=\s*\S+)", re.IGNORECASE),
    re.compile(r"(api[_-]?key\s*=\s*\S+)", re.IGNORECASE),
    re.compile(r"(bearer\s+[a-zA-Z0-9._\-]{20,})", re.IGNORECASE),
]

_MAX_OUTPUT_LINES = 200


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def _normalize(command: str) -> str:
    """Strip whitespace, normalize unicode, collapse internal whitespace."""
    # Normalize unicode to catch homoglyph evasion
    normalized = unicodedata.normalize("NFKC", command)
    # Strip and collapse whitespace
    return " ".join(normalized.split())


def _extract_executable(command: str) -> str:
    """Extract the first token (executable name) from a command string."""
    try:
        tokens = shlex.split(command, posix=False)
    except ValueError:
        tokens = command.split()
    if not tokens:
        return ""
    exe = tokens[0].strip().strip('"').strip("'")
    # Remove path prefix — just the basename
    return Path(exe).stem.casefold()


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def classify_command(command: str, extra_allowlist: list[str] | None = None) -> CommandVerdict:
    """Classify a command into blocked / safe / risky. Deterministic, no LLM."""

    raw = command
    normalized = _normalize(command)

    if not normalized:
        return CommandVerdict(tier="blocked", reason="Empty command.", command="", executable="")

    lower = normalized.casefold()
    executable = _extract_executable(normalized)

    # ── 1. Structural patterns — block unconditionally ──

    if _SUBSHELL_PATTERN.search(normalized):
        return CommandVerdict(
            tier="blocked",
            reason="Subshell patterns ($() or backticks) are blocked unconditionally.",
            command=normalized, executable=executable,
        )

    if _CHAINING_PATTERN.search(normalized):
        return CommandVerdict(
            tier="blocked",
            reason="Command chaining operators (&&, ||, ;, |, &) are blocked.",
            command=normalized, executable=executable,
        )

    if _REDIRECT_PATTERN.search(normalized):
        return CommandVerdict(
            tier="blocked",
            reason="Redirection operators (>, >>, <) are blocked.",
            command=normalized, executable=executable,
        )

    # ── 2. Blocked substrings ──

    for pattern in _BLOCKED_SUBSTRINGS:
        if pattern.casefold() in lower:
            return CommandVerdict(
                tier="blocked",
                reason=f"Contains blocked pattern: {pattern.strip()}",
                command=normalized, executable=executable,
            )

    # ── 3. Blocked executables ──

    if executable in _BLOCKED_EXECUTABLES:
        return CommandVerdict(
            tier="blocked",
            reason=f"Executable '{executable}' is on the permanent blocklist.",
            command=normalized, executable=executable,
        )

    # ── 4. pip subcommand check ──

    if executable == "pip":
        try:
            tokens = shlex.split(normalized, posix=False)
        except ValueError:
            tokens = normalized.split()
        if len(tokens) >= 2 and tokens[1].casefold() in _BLOCKED_PIP_SUBCOMMANDS:
            return CommandVerdict(
                tier="blocked",
                reason=f"pip {tokens[1]} is blocked (use pip list, pip --version, pip freeze instead).",
                command=normalized, executable=executable,
            )

    # ── 5. git subcommand check ──

    if executable == "git":
        try:
            tokens = shlex.split(normalized, posix=False)
        except ValueError:
            tokens = normalized.split()
        if len(tokens) >= 2 and tokens[1].casefold() in _BLOCKED_GIT_SUBCOMMANDS:
            return CommandVerdict(
                tier="blocked",
                reason=f"git {tokens[1]} is blocked (destructive or config-modifying).",
                command=normalized, executable=executable,
            )

    # ── 6. Blocked system paths in arguments ──

    for blocked_path in _BLOCKED_PATHS:
        if blocked_path in lower:
            return CommandVerdict(
                tier="blocked",
                reason=f"Command references a protected system path: {blocked_path}",
                command=normalized, executable=executable,
            )

    # ── 7. Safe allowlist — exact full-command match first ──

    if lower in {c.casefold() for c in _SAFE_FULL_COMMANDS}:
        return CommandVerdict(
            tier="safe", reason="Command is on the safe allowlist.",
            command=normalized, executable=executable,
        )

    # ── 8. User-configured extra allowlist ──

    if extra_allowlist:
        for pattern in extra_allowlist:
            if lower == pattern.strip().casefold():
                return CommandVerdict(
                    tier="safe", reason="Command is on the user-configured allowlist.",
                    command=normalized, executable=executable,
                )

    # ── 9. Restricted executables remain risky unless exactly allowlisted ──

    if executable in _RESTRICTED_EXECUTABLES:
        return CommandVerdict(
            tier="risky",
            reason=(
                f"Executable '{executable}' is only safe for explicitly allowlisted read-only commands."
            ),
            command=normalized,
            executable=executable,
        )

    # ── 10. Safe executable (but not exact full-command match) ──

    if executable in _SAFE_EXECUTABLES:
        return CommandVerdict(
            tier="safe",
            reason=f"Executable '{executable}' is on the safe allowlist.",
            command=normalized, executable=executable,
        )

    # ── 11. Everything else is RISKY ──

    return CommandVerdict(
        tier="risky",
        reason="Command is not on the allowlist. Requires confirmation and review.",
        command=normalized, executable=executable,
    )


# ---------------------------------------------------------------------------
# Working directory validation
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent


def validate_working_dir(working_dir: str | None) -> Path:
    """Resolve and validate working_dir. Raises PermissionError if outside allowed roots."""
    if not working_dir:
        return _PROJECT_ROOT

    resolved = Path(working_dir).expanduser().resolve()
    allowed = get_allowed_roots() + [_PROJECT_ROOT]

    for root in allowed:
        try:
            resolved.relative_to(root.resolve())
            return resolved
        except ValueError:
            continue

    raise PermissionError(
        f"Working directory outside allowed roots: {resolved}. "
        f"Allowed: {', '.join(str(r) for r in allowed)}"
    )


# ---------------------------------------------------------------------------
# Output processing
# ---------------------------------------------------------------------------


def redact_secrets(text: str) -> str:
    """Replace strings that look like secrets with [REDACTED]."""
    result = text
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub("[REDACTED]", result)
    return result


def truncate_output(text: str, max_lines: int = _MAX_OUTPUT_LINES) -> tuple[str, bool]:
    """Truncate output to max_lines. Returns (text, was_truncated)."""
    lines = text.splitlines(keepends=True)
    if len(lines) <= max_lines:
        return text, False
    truncated = "".join(lines[:max_lines])
    truncated += f"\n... ({len(lines) - max_lines} more lines truncated)"
    return truncated, True


# ---------------------------------------------------------------------------
# Config: extra allowlist
# ---------------------------------------------------------------------------


def get_extra_allowlist() -> list[str]:
    """Read user-configured extra safe commands from XAI_SHELL_ALLOWLIST_EXTRA."""
    raw = os.environ.get("XAI_SHELL_ALLOWLIST_EXTRA", "")
    if not raw.strip():
        return []
    return [cmd.strip() for cmd in raw.split(",") if cmd.strip()]
