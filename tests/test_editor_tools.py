"""Tests for precise editor tools."""

from __future__ import annotations

from pathlib import Path

import pytest

import editor_tools
from config import set_dry_run


@pytest.fixture(autouse=True)
def _isolate_undo_file(sample_files: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("undo._undo_path", lambda: sample_files / "editor_undo.jsonl")


class TestReadFileRange:
    def test_reads_inclusive_range(self, sample_files: Path) -> None:
        result = editor_tools.read_file_range(str(sample_files / "notes.txt"), 1, 2)
        assert result["ok"] is True
        assert result["line_count"] == 2
        assert "hello world" in result["content"]
        assert "second line" in result["content"]


class TestAppendFile:
    def test_appends_existing_file(self, sample_files: Path) -> None:
        target = sample_files / "notes.txt"
        result = editor_tools.append_file(str(target), "\nappended")
        assert result["ok"] is True
        assert target.read_text(encoding="utf-8").endswith("appended")

    def test_creates_missing_file(self, sample_files: Path) -> None:
        target = sample_files / "append_new.txt"
        result = editor_tools.append_file(str(target), "hello")
        assert result["ok"] is True
        assert target.read_text(encoding="utf-8") == "hello"

    def test_dry_run(self, sample_files: Path) -> None:
        set_dry_run(True)
        try:
            target = sample_files / "dry_append.txt"
            result = editor_tools.append_file(str(target), "hello")
            assert result["ok"] is True
            assert result["dry_run"] is True
            assert not target.exists()
        finally:
            set_dry_run(False)


class TestReplaceInFile:
    def test_replaces_first_match(self, sample_files: Path) -> None:
        target = sample_files / "notes.txt"
        result = editor_tools.replace_in_file(str(target), "line", "ROW", replace_all=False)
        assert result["ok"] is True
        assert result["replacements"] == 1
        assert target.read_text(encoding="utf-8").count("ROW") == 1

    def test_replace_all(self, sample_files: Path) -> None:
        target = sample_files / "notes.txt"
        result = editor_tools.replace_in_file(str(target), "line", "ROW", replace_all=True)
        assert result["ok"] is True
        assert result["replacements"] == 2

    def test_missing_old_text_fails(self, sample_files: Path) -> None:
        result = editor_tools.replace_in_file(str(sample_files / "notes.txt"), "missing", "new")
        assert result["ok"] is False


class TestApplyPatch:
    def test_applies_single_hunk(self, sample_files: Path) -> None:
        target = sample_files / "notes.txt"
        diff = (
            "--- a/notes.txt\n"
            "+++ b/notes.txt\n"
            "@@ -1,3 +1,3 @@\n"
            " hello world\n"
            "-second line\n"
            "+SECOND LINE\n"
            " third line"
        )
        result = editor_tools.apply_patch(str(target), diff)
        assert result["ok"] is True
        assert "SECOND LINE" in target.read_text(encoding="utf-8")

    def test_patch_context_mismatch_fails(self, sample_files: Path) -> None:
        target = sample_files / "notes.txt"
        diff = (
            "--- a/notes.txt\n"
            "+++ b/notes.txt\n"
            "@@ -1,1 +1,1 @@\n"
            "-not present\n"
            "+new"
        )
        result = editor_tools.apply_patch(str(target), diff)
        assert result["ok"] is False

    def test_patch_target_keeps_leading_b_chars(self, sample_files: Path) -> None:
        target = sample_files / "beta.txt"
        target.write_text("one\ntwo\n", encoding="utf-8")
        diff = (
            "--- a/beta.txt\n"
            "+++ b/beta.txt\n"
            "@@ -1,2 +1,2 @@\n"
            " one\n"
            "-two\n"
            "+TWO\n"
        )
        result = editor_tools.apply_patch(str(target), diff)
        assert result["ok"] is True
        assert "TWO" in target.read_text(encoding="utf-8")

    def test_undo_restores_backup(self, sample_files: Path) -> None:
        from undo import undo_last

        target = sample_files / "notes.txt"
        diff = (
            "--- a/notes.txt\n"
            "+++ b/notes.txt\n"
            "@@ -1,3 +1,3 @@\n"
            " hello world\n"
            "-second line\n"
            "+patched line\n"
            " third line"
        )
        assert editor_tools.apply_patch(str(target), diff)["ok"] is True
        undo_result = undo_last()
        assert undo_result["ok"] is True
        assert "second line" in target.read_text(encoding="utf-8")
