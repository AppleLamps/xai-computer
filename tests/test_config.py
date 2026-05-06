"""Tests for configuration and runtime state."""

from __future__ import annotations

from pathlib import Path

from config import (
    MODELS,
    get_web_settings,
    get_last_working_folder,
    get_max_tool_loops,
    get_xai_model,
    is_auto_switch_models,
    is_dry_run,
    is_verbose,
    set_auto_switch_models,
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
        set_runtime_model(MODELS["grok-4.3-latest"])
        assert get_xai_model() == MODELS["grok-4.3-latest"]

    def test_legacy_model_is_normalized(self) -> None:
        set_runtime_model("grok-old-local-setting")
        assert get_xai_model() == MODELS["grok-4.3-latest"]
        set_runtime_model(MODELS["grok-4.3-latest"])

    def test_custom_model(self) -> None:
        set_runtime_model("custom-model-v1")
        assert get_xai_model() == "custom-model-v1"
        set_runtime_model(MODELS["grok-4.3-latest"])


class TestApprovalAndModelToggles:
    def test_auto_switch_models_toggle(self) -> None:
        set_auto_switch_models(True)
        assert is_auto_switch_models()
        set_auto_switch_models(False)
        assert not is_auto_switch_models()
        set_auto_switch_models(True)

    def test_persisted_web_settings(self, tmp_path: Path, monkeypatch) -> None:
        import config
        monkeypatch.setattr(config, "get_state_dir", lambda: tmp_path)
        set_dry_run(True, persist=True)
        set_verbose(False, persist=True)
        set_auto_switch_models(False, persist=True)
        set_runtime_model("grok-4.3-latest", user_initiated=False, persist=True)

        settings = get_web_settings()
        assert settings["dry_run"] is True
        assert settings["verbose"] is False
        assert settings["auto_switch_models"] is False
        assert settings["model"] == "grok-4.3-latest"

        set_dry_run(False)
        set_verbose(True)
        set_auto_switch_models(True)
        set_runtime_model(MODELS["grok-4.3-latest"], user_initiated=False)


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
        assert list(MODELS) == ["grok-4.3-latest"]

    def test_preset_values(self) -> None:
        assert MODELS["grok-4.3-latest"] == "grok-4.3-latest"


class TestToolLoopLimit:
    def test_default_tool_loop_limit_is_24(self, monkeypatch) -> None:
        monkeypatch.delenv("XAI_MAX_TOOL_LOOPS", raising=False)
        assert get_max_tool_loops() == 24

    def test_invalid_tool_loop_limit_falls_back_to_24(self, monkeypatch) -> None:
        monkeypatch.setenv("XAI_MAX_TOOL_LOOPS", "not-a-number")
        assert get_max_tool_loops() == 24
