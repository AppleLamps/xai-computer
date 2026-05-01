"""Tests for write_file tool and its undo behavior."""

from __future__ import annotations

from pathlib import Path

import pytest

from config import set_dry_run
from tools import write_file


# ---------------------------------------------------------------------------
# Basic write operations
# ---------------------------------------------------------------------------


class TestWriteNewFile:
    def test_creates_file(self, sample_files: Path) -> None:
        result = write_file(str(sample_files / "new.txt"), "hello world")
        assert result["ok"] is True
        assert result["action"] == "created"
        assert (sample_files / "new.txt").read_text() == "hello world"

    def test_returns_byte_count(self, sample_files: Path) -> None:
        result = write_file(str(sample_files / "new.txt"), "abc")
        assert result["bytes"] == 3

    def test_creates_intermediate_dirs(self, sample_files: Path) -> None:
        deep = sample_files / "sub1" / "sub2" / "deep.txt"
        result = write_file(str(deep), "deep content")
        assert result["ok"] is True
        assert deep.read_text() == "deep content"


class TestWriteOutsideRoot:
    def test_rejected(self, tmp_path: Path, sample_files: Path) -> None:
        outside = tmp_path / "outside" / "nope.txt"
        result = write_file(str(outside), "nope")
        assert result["ok"] is False
        assert "not allowed" in result["error"].lower() or "outside" in result["error"].lower()


# ---------------------------------------------------------------------------
# Overwrite behavior
# ---------------------------------------------------------------------------


class TestOverwrite:
    def test_overwrite_false_rejects_existing(self, sample_files: Path) -> None:
        result = write_file(str(sample_files / "notes.txt"), "new content", overwrite=False)
        assert result["ok"] is False
        assert "already exists" in result["error"]
        # Original content unchanged
        assert "hello world" in (sample_files / "notes.txt").read_text()

    def test_overwrite_true_creates_backup(self, sample_files: Path) -> None:
        target = sample_files / "notes.txt"
        original = target.read_text()
        result = write_file(str(target), "replaced", overwrite=True)
        assert result["ok"] is True
        assert result["action"] == "overwritten"
        assert target.read_text() == "replaced"
        # Backup exists
        bak = target.with_suffix(".txt.bak")
        assert bak.exists()
        assert bak.read_text() == original
        assert result["backup_path"] == str(bak)


# ---------------------------------------------------------------------------
# Size cap
# ---------------------------------------------------------------------------


class TestSizeCap:
    def test_rejects_over_500kb(self, sample_files: Path) -> None:
        big_content = "x" * 600_000
        result = write_file(str(sample_files / "big.txt"), big_content)
        assert result["ok"] is False
        assert "too large" in result["error"].lower()

    def test_allows_under_500kb(self, sample_files: Path) -> None:
        content = "x" * 100_000
        result = write_file(str(sample_files / "ok.txt"), content)
        assert result["ok"] is True


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------


class TestDryRun:
    def test_dry_run_does_not_write(self, sample_files: Path) -> None:
        set_dry_run(True)
        try:
            result = write_file(str(sample_files / "dry.txt"), "content")
            assert result["ok"] is True
            assert result.get("dry_run") is True
            assert not (sample_files / "dry.txt").exists()
        finally:
            set_dry_run(False)


# ---------------------------------------------------------------------------
# Undo
# ---------------------------------------------------------------------------


class TestUndoWrite:
    def test_undo_new_file(self, sample_files: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Undo of a new file should remove it."""
        from undo import undo_last, _undo_path
        # Redirect undo file to temp
        undo_file = sample_files / "undo_test.jsonl"
        monkeypatch.setattr("undo._undo_path", lambda: undo_file)

        target = sample_files / "undo_new.txt"
        result = write_file(str(target), "undo me")
        assert result["ok"] is True
        assert target.exists()

        undo_result = undo_last()
        assert undo_result["ok"] is True
        assert not target.exists()

    def test_undo_overwrite_restores_backup(self, sample_files: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
        """Undo of an overwrite should restore the .bak file."""
        from undo import undo_last, _undo_path
        undo_file = sample_files / "undo_test.jsonl"
        monkeypatch.setattr("undo._undo_path", lambda: undo_file)

        target = sample_files / "notes.txt"
        original_content = target.read_text()

        result = write_file(str(target), "overwritten content", overwrite=True)
        assert result["ok"] is True
        assert target.read_text() == "overwritten content"

        undo_result = undo_last()
        assert undo_result["ok"] is True
        assert target.read_text() == original_content
        # Backup should be gone after restore
        bak = target.with_suffix(".txt.bak")
        assert not bak.exists()

    def test_repeated_overwrites_have_distinct_undo_backups(
        self,
        sample_files: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from undo import undo_last

        undo_file = sample_files / "undo_test.jsonl"
        monkeypatch.setattr("undo._undo_path", lambda: undo_file)

        target = sample_files / "notes.txt"
        original_content = target.read_text()

        first = write_file(str(target), "first overwrite", overwrite=True)
        second = write_file(str(target), "second overwrite", overwrite=True)

        assert first["ok"] is True
        assert second["ok"] is True
        assert first["backup_path"] != second["backup_path"]
        assert Path(first["backup_path"]).exists()
        assert Path(second["backup_path"]).exists()

        undo_latest = undo_last()
        assert undo_latest["ok"] is True
        assert target.read_text() == "first overwrite"

        undo_previous = undo_last()
        assert undo_previous["ok"] is True
        assert target.read_text() == original_content


# ---------------------------------------------------------------------------
# Content validation
# ---------------------------------------------------------------------------


class TestContentValidation:
    def test_empty_content_allowed(self, sample_files: Path) -> None:
        result = write_file(str(sample_files / "empty.txt"), "")
        assert result["ok"] is True
        assert (sample_files / "empty.txt").read_text() == ""
