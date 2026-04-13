"""Tests for process tools."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

import process_tools
from config import set_dry_run


class TestListProcesses:
    def test_filters_query(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_proc = MagicMock()
        fake_proc.info = {"pid": 1, "name": "python.exe", "status": "running", "cmdline": ["python", "app.py"]}
        monkeypatch.setattr(process_tools, "_load_psutil", lambda: MagicMock(process_iter=lambda *_: [fake_proc]))
        result = process_tools.list_processes("python")
        assert result["ok"] is True
        assert result["count"] == 1


class TestStartProcess:
    def test_rejects_working_dir_outside_roots(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("shell_guard.get_allowed_roots", lambda: [Path("C:/allowed")])
        result = process_tools.start_process("python", ["--version"], working_dir="C:/forbidden")
        assert result["ok"] is False

    def test_dry_run(self, sample_files: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("shell_guard.get_allowed_roots", lambda: [sample_files])
        set_dry_run(True)
        try:
            result = process_tools.start_process("python", ["--version"], working_dir=str(sample_files))
            assert result["ok"] is True
            assert result["dry_run"] is True
        finally:
            set_dry_run(False)

    def test_starts_process(self, sample_files: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("shell_guard.get_allowed_roots", lambda: [sample_files])
        fake_proc = MagicMock(pid=1234)
        monkeypatch.setattr(process_tools.subprocess, "Popen", lambda *args, **kwargs: fake_proc)
        result = process_tools.start_process("python", ["--version"], working_dir=str(sample_files))
        assert result["ok"] is True
        assert result["pid"] == 1234


class TestStopAndWait:
    def test_stop_process_force(self, monkeypatch: pytest.MonkeyPatch) -> None:
        proc = MagicMock()
        psutil = MagicMock(Process=lambda pid: proc, NoSuchProcess=RuntimeError)
        monkeypatch.setattr(process_tools, "_load_psutil", lambda: psutil)
        result = process_tools.stop_process(10, force=True)
        assert result["ok"] is True
        proc.kill.assert_called_once()

    def test_wait_for_process_exit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        proc = MagicMock()
        proc.is_running.side_effect = [True, False]
        psutil = MagicMock(Process=lambda pid: proc, NoSuchProcess=RuntimeError)
        monkeypatch.setattr(process_tools, "_load_psutil", lambda: psutil)
        result = process_tools.wait_for_process_exit(10, timeout_sec=1)
        assert result["ok"] is True
