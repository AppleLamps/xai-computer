"""Tests for configuration and runtime state."""

from __future__ import annotations

from pathlib import Path

from config import (
    MODELS,
    get_last_working_folder,
    get_xai_model,
    is_dry_run,
    is_verbose,
    set_dry_run,
    set_last_working_folder,
    set_runtime_model,
    set_verbose,
)


class TestDryRun:
    def test_default_off(self) -> None:
        set_dry_run(False)
        assert not is_dry_run()

    def test_toggle(self) -> None:
        set_dry_run(True)
        assert is_dry_run()
        set_dry_run(False)
        assert not is_dry_run()


class TestVerboseMode:
    def test_default_verbose(self) -> None:
        set_verbose(True)
        assert is_verbose()

    def test_toggle(self) -> None:
        set_verbose(False)
        assert not is_verbose()
        set_verbose(True)
        assert is_verbose()


class TestModelSwitching:
    def test_default_model(self) -> None:
        set_runtime_model(MODELS["fast"])
        assert get_xai_model() == MODELS["fast"]

    def test_switch_to_quality(self) -> None:
        set_runtime_model(MODELS["quality"])
        assert get_xai_model() == MODELS["quality"]
        set_runtime_model(MODELS["fast"])

    def test_custom_model(self) -> None:
        set_runtime_model("custom-model-v1")
        assert get_xai_model() == "custom-model-v1"
        set_runtime_model(MODELS["fast"])


class TestSessionMemory:
    def test_last_working_folder(self, tmp_path: Path) -> None:
        set_last_working_folder(tmp_path)
        assert get_last_working_folder() == tmp_path

    def test_default_none(self) -> None:
        # Reset
        import config
        config._last_working_folder = None
        assert get_last_working_folder() is None


class TestModelPresets:
    def test_presets_exist(self) -> None:
        assert "fast" in MODELS
        assert "quality" in MODELS

    def test_preset_values(self) -> None:
        assert "grok" in MODELS["fast"]
        assert "grok" in MODELS["quality"]
