"""Tests for undo system: recording, reversal, and edge cases."""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

from undo import (
    _load_all,
    _safe_restore_path,
    _save_all,
    get_history,
    record_create_folder,
    record_copy_file,
    record_move,
    record_rename,
    undo_last,
)


@pytest.fixture(autouse=True)
def _isolate_undo_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect undo storage to temp directory for each test."""
    undo_file = tmp_path / "undo_history.jsonl"
    monkeypatch.setattr("undo._undo_path", lambda: undo_file)


# ---------------------------------------------------------------------------
# Safe restore path
# ---------------------------------------------------------------------------


class TestSafeRestorePath:
    def test_returns_original_if_free(self, tmp_path: Path) -> None:
        target = tmp_path / "file.txt"
        assert _safe_restore_path(target) == target

    def test_suffixes_when_occupied(self, tmp_path: Path) -> None:
        target = tmp_path / "file.txt"
        target.touch()
        result = _safe_restore_path(target)
        assert result.name == "file_restored1.txt"
        assert not result.exists()

    def test_increments_suffix(self, tmp_path: Path) -> None:
        target = tmp_path / "file.txt"
        target.touch()
        (tmp_path / "file_restored1.txt").touch()
        result = _safe_restore_path(target)
        assert result.name == "file_restored2.txt"


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------


class TestRecording:
    def test_record_move(self) -> None:
        record_move("/a/b.txt", "/c/b.txt")
        records = _load_all()
        assert len(records) == 1
        assert records[0]["action"] == "move_file"
        assert records[0]["source"] == "/a/b.txt"
        assert records[0]["destination"] == "/c/b.txt"
        assert records[0]["undone"] is False

    def test_record_rename(self) -> None:
        record_rename("/a/old.txt", "/a/new.txt")
        records = _load_all()
        assert len(records) == 1
        assert records[0]["action"] == "rename_file"

    def test_record_create_folder(self) -> None:
        record_create_folder("/a/new_folder")
        records = _load_all()
        assert len(records) == 1
        assert records[0]["action"] == "create_folder"

    def test_record_copy_file(self) -> None:
        record_copy_file("/a/source.txt", "/a/copy.txt")
        records = _load_all()
        assert len(records) == 1
        assert records[0]["action"] == "copy_file"
        assert records[0]["source"] == "/a/source.txt"
        assert records[0]["destination"] == "/a/copy.txt"

    def test_multiple_records_append(self) -> None:
        record_move("/a/1.txt", "/b/1.txt")
        record_move("/a/2.txt", "/b/2.txt")
        records = _load_all()
        assert len(records) == 2


# ---------------------------------------------------------------------------
# Undo execution
# ---------------------------------------------------------------------------


class TestUndoLast:
    def test_undo_move(self, tmp_path: Path) -> None:
        src = tmp_path / "original.txt"
        dst = tmp_path / "moved.txt"
        dst.write_text("content")
        record_move(str(src), str(dst))

        result = undo_last()
        assert result["ok"] is True
        assert src.exists()
        assert not dst.exists()

    def test_undo_rename(self, tmp_path: Path) -> None:
        old_name = tmp_path / "old.txt"
        new_name = tmp_path / "new.txt"
        new_name.write_text("content")
        record_rename(str(old_name), str(new_name))

        result = undo_last()
        assert result["ok"] is True
        assert old_name.exists()
        assert not new_name.exists()

    def test_undo_create_folder_empty(self, tmp_path: Path) -> None:
        folder = tmp_path / "new_folder"
        folder.mkdir()
        record_create_folder(str(folder))

        result = undo_last()
        assert result["ok"] is True
        assert not folder.exists()

    def test_undo_create_folder_nonempty_fails(self, tmp_path: Path) -> None:
        folder = tmp_path / "new_folder"
        folder.mkdir()
        (folder / "child.txt").write_text("content")
        record_create_folder(str(folder))

        result = undo_last()
        assert result["ok"] is False
        assert "not empty" in result["error"]
        assert folder.exists()

    def test_undo_nothing(self) -> None:
        result = undo_last()
        assert result["ok"] is False
        assert "Nothing to undo" in result["error"]

    def test_undo_with_occupied_path(self, tmp_path: Path) -> None:
        src = tmp_path / "file.txt"
        dst = tmp_path / "moved.txt"
        src.write_text("occupant")  # original location is occupied
        dst.write_text("moved content")
        record_move(str(src), str(dst))

        result = undo_last()
        assert result["ok"] is True
        assert "restored" in result.get("note", "")
        # File restored with suffix
        restored = Path(result["restored_to"])
        assert restored.exists()
        assert "restored" in restored.name

    def test_undo_marks_record(self, tmp_path: Path) -> None:
        src = tmp_path / "a.txt"
        dst = tmp_path / "b.txt"
        dst.write_text("c")
        record_move(str(src), str(dst))

        undo_last()
        records = _load_all()
        assert records[0]["undone"] is True

    def test_double_undo(self, tmp_path: Path) -> None:
        f1_src = tmp_path / "f1.txt"
        f1_dst = tmp_path / "f1_moved.txt"
        f1_dst.write_text("f1")
        record_move(str(f1_src), str(f1_dst))

        f2_src = tmp_path / "f2.txt"
        f2_dst = tmp_path / "f2_moved.txt"
        f2_dst.write_text("f2")
        record_move(str(f2_src), str(f2_dst))

        # First undo reverses f2
        r1 = undo_last()
        assert r1["ok"] is True
        assert f2_src.exists()

        # Second undo reverses f1
        r2 = undo_last()
        assert r2["ok"] is True
        assert f1_src.exists()

        # Third undo: nothing left
        r3 = undo_last()
        assert r3["ok"] is False

    def test_undo_copy_file_recycles_copy(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        copied = tmp_path / "copy.txt"
        copied.write_text("copy", encoding="utf-8")
        calls: list[str] = []

        def fake_send2trash(path: str) -> None:
            calls.append(path)
            Path(path).unlink()

        monkeypatch.setitem(sys.modules, "send2trash", types.SimpleNamespace(send2trash=fake_send2trash))
        record_copy_file(str(tmp_path / "source.txt"), str(copied))

        result = undo_last()
        assert result["ok"] is True
        assert calls == [str(copied)]
        assert not copied.exists()

    def test_undo_overwritten_copy_restores_backup(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        copied = tmp_path / "copy.txt"
        copied.write_text("new", encoding="utf-8")
        backup = tmp_path / "copy.txt.bak"
        backup.write_text("old", encoding="utf-8")
        recycled: list[str] = []

        def fake_send2trash(path: str) -> None:
            recycled.append(path)
            Path(path).unlink()

        monkeypatch.setitem(sys.modules, "send2trash", types.SimpleNamespace(send2trash=fake_send2trash))
        record_copy_file(str(tmp_path / "source.txt"), str(copied), str(backup))

        result = undo_last()
        assert result["ok"] is True
        assert copied.read_text(encoding="utf-8") == "old"
        assert not backup.exists()
        assert recycled == [str(copied)]


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


class TestHistory:
    def test_empty_history(self) -> None:
        assert get_history() == []

    def test_returns_session_records(self, tmp_path: Path) -> None:
        record_move("/a/1.txt", "/b/1.txt")
        record_move("/a/2.txt", "/b/2.txt")
        h = get_history()
        assert len(h) == 2
        # Newest first
        assert h[0]["source"] == "/a/2.txt"

    def test_limit(self) -> None:
        for i in range(5):
            record_move(f"/a/{i}.txt", f"/b/{i}.txt")
        h = get_history(limit=3)
        assert len(h) == 3


# ---------------------------------------------------------------------------
# undo_n
# ---------------------------------------------------------------------------


class TestUndoN:
    def test_undo_zero_returns_empty(self) -> None:
        from undo import undo_n
        assert undo_n(0) == []

    def test_undo_negative_returns_empty(self) -> None:
        from undo import undo_n
        assert undo_n(-5) == []

    def test_undoes_n_actions(self, tmp_path: Path) -> None:
        from undo import undo_n

        f1 = tmp_path / "a.txt"
        f2 = tmp_path / "b.txt"
        f3 = tmp_path / "c.txt"
        for f in (f1, f2, f3):
            f.write_text("x")

        dst1 = tmp_path / "moved_a.txt"
        dst2 = tmp_path / "moved_b.txt"
        dst3 = tmp_path / "moved_c.txt"
        f1.rename(dst1)
        f2.rename(dst2)
        f3.rename(dst3)

        record_move(str(f1), str(dst1))
        record_move(str(f2), str(dst2))
        record_move(str(f3), str(dst3))

        results = undo_n(2)
        assert len(results) == 2
        assert all(r["ok"] for r in results)
        # The two most recent were undone; first remains
        assert dst1.exists()
        assert f2.exists() or f3.exists()  # one of them was restored twice

    def test_stops_early_when_nothing_left(self, tmp_path: Path) -> None:
        from undo import undo_n

        f = tmp_path / "f.txt"
        f.write_text("x")
        dst = tmp_path / "dst.txt"
        f.rename(dst)
        record_move(str(f), str(dst))

        # Ask for 5 but only 1 available
        results = undo_n(5)
        ok_results = [r for r in results if r.get("ok")]
        fail_results = [r for r in results if not r.get("ok")]
        assert len(ok_results) == 1
        assert len(fail_results) == 1  # stopped after "nothing to undo"

    def test_undo_n_one_behaves_like_undo_last(self, tmp_path: Path) -> None:
        from undo import undo_n

        f = tmp_path / "file.txt"
        dst = tmp_path / "moved.txt"
        f.write_text("hi")
        f.rename(dst)
        record_move(str(f), str(dst))

        results = undo_n(1)
        assert len(results) == 1
        assert results[0]["ok"] is True
