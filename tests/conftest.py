"""Shared fixtures for tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

# Ensure tests don't pick up real .env
os.environ.setdefault("XAI_API_KEY", "test-key-not-real")


@pytest.fixture
def tmp_allowed_root(tmp_path: Path) -> Path:
    """Create a temp directory that can act as an allowed root."""
    root = tmp_path / "allowed"
    root.mkdir()
    return root


@pytest.fixture
def sample_files(tmp_allowed_root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create sample files for testing and patch allowed roots to include them."""
    roots = [tmp_allowed_root]

    # Patch everywhere get_allowed_roots is called from
    monkeypatch.setattr("config.get_allowed_roots", lambda: roots)
    monkeypatch.setattr("safety.get_allowed_roots", lambda: roots)

    (tmp_allowed_root / "photo.png").write_text("fake png")
    (tmp_allowed_root / "report.pdf").write_text("fake pdf")
    (tmp_allowed_root / "notes.txt").write_text("hello world\nsecond line\nthird line")
    (tmp_allowed_root / "data.csv").write_text("a,b,c\n1,2,3")
    (tmp_allowed_root / "archive.zip").write_bytes(b"\x00" * 100)
    (tmp_allowed_root / "photo (1).png").write_text("duplicate")
    (tmp_allowed_root / "photo copy.png").write_text("duplicate 2")
    (tmp_allowed_root / "subfolder").mkdir()
    (tmp_allowed_root / "subfolder" / "deep.txt").write_text("deep file")
    (tmp_allowed_root / ".hidden").write_text("hidden file")
    (tmp_allowed_root / "desktop.ini").write_text("system file")
    return tmp_allowed_root
