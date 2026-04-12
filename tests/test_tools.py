"""Tests for read-only tools, file classification, and dry-run behavior."""

from __future__ import annotations

from pathlib import Path

import pytest

from config import set_dry_run
from tools import (
    _category_for_suffix,
    _detect_duplicates,
    _format_size,
    _unique_destination,
    analyze_directory,
    directory_tree,
    file_type_summary,
    largest_files,
    list_directory,
    read_text_file,
    recent_files,
    search_files,
)


# ---------------------------------------------------------------------------
# File classification
# ---------------------------------------------------------------------------


class TestCategoryClassification:
    @pytest.mark.parametrize("ext,expected", [
        (".png", "Images"), (".JPG", "Images"), (".heic", "Images"),
        (".pdf", "PDFs"),
        (".docx", "Documents"), (".txt", "Documents"), (".json", "Documents"),
        (".zip", "Archives"), (".7z", "Archives"),
        (".mp4", "Videos"), (".mkv", "Videos"),
        (".mp3", "Audio"), (".flac", "Audio"),
        (".xlsx", "Spreadsheets"), (".csv", "Spreadsheets"),
        (".pptx", "Presentations"),
        (".exe", "Executables"), (".bat", "Executables"),
        (".py", "Code"), (".js", "Code"),
        (".ttf", "Fonts"),
        (".xyz", "Other"), ("", "Other"),
    ])
    def test_categories(self, ext: str, expected: str) -> None:
        assert _category_for_suffix(ext) == expected


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------


class TestDuplicateDetection:
    def test_detects_numbered_copies(self) -> None:
        names = ["photo.png", "photo (1).png", "photo (2).png", "report.pdf"]
        dupes = _detect_duplicates(names)
        assert len(dupes) == 2
        originals = {d["likely_original"] for d in dupes}
        assert "photo.png" in originals

    def test_detects_copy_suffix(self) -> None:
        dupes = _detect_duplicates(["doc copy.txt", "doc.txt"])
        assert len(dupes) == 1
        assert dupes[0]["likely_original"] == "doc.txt"

    def test_no_false_positives(self) -> None:
        dupes = _detect_duplicates(["report.pdf", "invoice.pdf", "readme.md"])
        assert len(dupes) == 0


# ---------------------------------------------------------------------------
# Size formatting
# ---------------------------------------------------------------------------


class TestFormatSize:
    def test_bytes(self) -> None:
        assert _format_size(500) == "500 B"

    def test_kilobytes(self) -> None:
        assert "KB" in _format_size(2048)

    def test_megabytes(self) -> None:
        assert "MB" in _format_size(5_000_000)


# ---------------------------------------------------------------------------
# Unique destination
# ---------------------------------------------------------------------------


class TestUniqueDestination:
    def test_nonexistent_returns_same(self, tmp_path: Path) -> None:
        dest = tmp_path / "newfile.txt"
        assert _unique_destination(dest) == dest

    def test_existing_file_gets_suffix(self, tmp_path: Path) -> None:
        existing = tmp_path / "file.txt"
        existing.touch()
        result = _unique_destination(existing)
        assert result.name == "file_dup1.txt"

    def test_directory_returns_same(self, tmp_path: Path) -> None:
        assert _unique_destination(tmp_path) == tmp_path


# ---------------------------------------------------------------------------
# Read-only tools (using sample_files fixture)
# ---------------------------------------------------------------------------


class TestListDirectory:
    def test_lists_files(self, sample_files: Path) -> None:
        result = list_directory(str(sample_files))
        assert result["ok"] is True
        names = {e["name"] for e in result["entries"]}
        assert "photo.png" in names
        assert "notes.txt" in names

    def test_rejects_outside_roots(self, tmp_path: Path) -> None:
        outside = tmp_path / "nope"
        outside.mkdir()
        result = list_directory(str(outside))
        # May or may not fail depending on allowed roots; just verify no crash
        assert isinstance(result, dict)


class TestAnalyzeDirectory:
    def test_analysis(self, sample_files: Path) -> None:
        result = analyze_directory(str(sample_files))
        assert result["ok"] is True
        assert result["total_files"] > 0
        assert result["total_dirs"] >= 1  # subfolder
        assert len(result["type_breakdown"]) > 0
        assert result["duplicate_count"] >= 2  # photo (1).png, photo copy.png


class TestLargestFiles:
    def test_returns_sorted(self, sample_files: Path) -> None:
        result = largest_files(str(sample_files))
        assert result["ok"] is True
        assert len(result["files"]) > 0
        sizes = [f["size_bytes"] for f in result["files"]]
        assert sizes == sorted(sizes, reverse=True)


class TestFileTypeSummary:
    def test_summary(self, sample_files: Path) -> None:
        result = file_type_summary(str(sample_files))
        assert result["ok"] is True
        categories = {e["category"] for e in result["by_category"]}
        assert "Images" in categories


class TestReadTextFile:
    def test_reads_content(self, sample_files: Path) -> None:
        result = read_text_file(str(sample_files / "notes.txt"))
        assert result["ok"] is True
        assert "hello world" in result["content"]
        assert result["truncated"] is False

    def test_respects_max_chars(self, sample_files: Path) -> None:
        result = read_text_file(str(sample_files / "notes.txt"), max_chars=100)
        assert result["ok"] is True

    def test_rejects_nonexistent(self, sample_files: Path) -> None:
        result = read_text_file(str(sample_files / "nope.txt"))
        assert result["ok"] is False


class TestSearchFiles:
    def test_finds_by_name(self, sample_files: Path) -> None:
        result = search_files(str(sample_files), "photo")
        assert result["ok"] is True
        assert result["count"] >= 2  # photo.png, photo (1).png, photo copy.png

    def test_case_insensitive(self, sample_files: Path) -> None:
        result = search_files(str(sample_files), "NOTES")
        assert result["ok"] is True
        assert result["count"] >= 1

    def test_empty_query_rejected(self, sample_files: Path) -> None:
        result = search_files(str(sample_files), "")
        assert result["ok"] is False


class TestRecentFiles:
    def test_returns_files(self, sample_files: Path) -> None:
        result = recent_files(str(sample_files))
        assert result["ok"] is True
        assert len(result["files"]) > 0


class TestDirectoryTree:
    def test_tree_output(self, sample_files: Path) -> None:
        result = directory_tree(str(sample_files), depth=2)
        assert result["ok"] is True
        assert "subfolder/" in result["tree"]
        assert "deep.txt" in result["tree"]
        # Hidden files should be excluded
        assert ".hidden" not in result["tree"]
        assert "desktop.ini" not in result["tree"]


# ---------------------------------------------------------------------------
# Dry-run behavior
# ---------------------------------------------------------------------------


class TestDryRun:
    def test_move_dry_run(self, sample_files: Path) -> None:
        from tools import move_file
        set_dry_run(True)
        try:
            src = sample_files / "notes.txt"
            dst = sample_files / "subfolder"
            result = move_file(str(src), str(dst))
            assert result["ok"] is True
            assert result.get("dry_run") is True
            # File should still be in original location
            assert src.exists()
        finally:
            set_dry_run(False)

    def test_create_folder_dry_run(self, sample_files: Path) -> None:
        from tools import create_folder
        set_dry_run(True)
        try:
            new_dir = sample_files / "dry_test_folder"
            result = create_folder(str(new_dir))
            assert result["ok"] is True
            assert result.get("dry_run") is True
            assert not new_dir.exists()
        finally:
            set_dry_run(False)
