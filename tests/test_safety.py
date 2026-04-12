"""Tests for path safety, traversal protection, and confirmation parsing."""

from __future__ import annotations

from pathlib import Path

import pytest

from safety import (
    _has_traversal,
    _hits_blocked_location,
    is_affirmative_confirmation,
    is_hidden_name,
    is_path_allowed,
    is_system_or_protected_name,
    require_allowed_path,
)


# ---------------------------------------------------------------------------
# Confirmation parsing
# ---------------------------------------------------------------------------


class TestConfirmation:
    @pytest.mark.parametrize("text", ["yes", "Yes", "YES", "y", "Y", "confirm", "CONFIRMED", "approve", "approved"])
    def test_affirmative(self, text: str) -> None:
        assert is_affirmative_confirmation(text)

    @pytest.mark.parametrize("text", ["no", "nope", "cancel", "maybe", "", "yes please", "yes!", "y y"])
    def test_negative(self, text: str) -> None:
        assert not is_affirmative_confirmation(text)

    def test_whitespace_tolerance(self) -> None:
        assert is_affirmative_confirmation("  yes  ")
        assert is_affirmative_confirmation("\tconfirm\n")


# ---------------------------------------------------------------------------
# Traversal detection
# ---------------------------------------------------------------------------


class TestTraversal:
    def test_detects_dotdot(self) -> None:
        assert _has_traversal(r"C:\Users\..\Windows")
        assert _has_traversal("../etc/passwd")
        assert _has_traversal("foo/../../bar")

    def test_clean_paths(self) -> None:
        assert not _has_traversal(r"C:\Users\lucas\Desktop")
        assert not _has_traversal("/home/user/docs")

    def test_traversal_rejected_by_require(self, tmp_allowed_root: Path) -> None:
        bad = tmp_allowed_root / ".." / "escape"
        with pytest.raises(PermissionError, match="traversal"):
            require_allowed_path(bad, roots=[tmp_allowed_root])


# ---------------------------------------------------------------------------
# Blocked locations
# ---------------------------------------------------------------------------


class TestBlockedLocations:
    @pytest.mark.parametrize("path_str", [
        "C:/Windows/System32/foo",
        "C:/Program Files/test",
        "C:/Program Files (x86)/app",
        "C:/ProgramData/secret",
        "D:/$Recycle.Bin/item",
    ])
    def test_blocked(self, path_str: str) -> None:
        assert _hits_blocked_location(Path(path_str))

    @pytest.mark.parametrize("path_str", [
        "C:/Users/lucas/Desktop/test",
        "D:/MyData/files",
    ])
    def test_allowed(self, path_str: str) -> None:
        assert not _hits_blocked_location(Path(path_str))


# ---------------------------------------------------------------------------
# Path allowlisting
# ---------------------------------------------------------------------------


class TestPathAllowed:
    def test_inside_root(self, tmp_allowed_root: Path) -> None:
        child = tmp_allowed_root / "subdir" / "file.txt"
        assert is_path_allowed(child, roots=[tmp_allowed_root])

    def test_outside_root(self, tmp_path: Path, tmp_allowed_root: Path) -> None:
        outside = tmp_path / "other" / "file.txt"
        assert not is_path_allowed(outside, roots=[tmp_allowed_root])

    def test_require_raises_outside(self, tmp_path: Path, tmp_allowed_root: Path) -> None:
        outside = tmp_path / "other"
        outside.mkdir(exist_ok=True)
        with pytest.raises(PermissionError):
            require_allowed_path(outside, roots=[tmp_allowed_root])

    def test_require_returns_resolved(self, tmp_allowed_root: Path) -> None:
        child = tmp_allowed_root / "file.txt"
        child.touch()
        result = require_allowed_path(child, roots=[tmp_allowed_root])
        assert result == child.resolve()


# ---------------------------------------------------------------------------
# System / hidden name detection
# ---------------------------------------------------------------------------


class TestNameDetection:
    @pytest.mark.parametrize("name", ["desktop.ini", "Thumbs.db", "NTUSER.DAT", "~$temp.docx"])
    def test_system_names(self, name: str) -> None:
        assert is_system_or_protected_name(name)

    def test_normal_names(self) -> None:
        assert not is_system_or_protected_name("report.pdf")
        assert not is_system_or_protected_name("photo.jpg")

    def test_hidden_dotfiles(self) -> None:
        assert is_hidden_name(".gitignore")
        assert is_hidden_name(".env")
        assert not is_hidden_name("regular.txt")
