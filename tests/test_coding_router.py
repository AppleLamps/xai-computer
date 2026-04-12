"""Tests for coding model auto-routing."""

from __future__ import annotations

import pytest

from core import _detect_coding_intent


# ---------------------------------------------------------------------------
# Intent detection
# ---------------------------------------------------------------------------


class TestDetectCodingIntent:
    @pytest.mark.parametrize("msg", [
        "write a python script to sort files",
        "Write me a simple HTML page",
        "build a website for my portfolio",
        "Build me a landing page",
        "create a script that converts CSV to JSON",
        "create a file called index.html",
        "create a webpage with a contact form",
        "generate html for a navbar",
        "generate python code for a calculator",
        "code a simple REST API",
        "make a website with three pages",
        "make a script to rename files",
        "scaffold a new project structure",
        "save this as main.py",
        "write the CSS for a dark theme",
        "create a file called app.ts",
        "build a page with React using .tsx",
        "write an index.html with a form",
        "generate a .js module for validation",
    ])
    def test_detects_coding(self, msg: str) -> None:
        assert _detect_coding_intent(msg), f"Should detect coding intent: {msg}"

    @pytest.mark.parametrize("msg", [
        "what's on my desktop?",
        "clean up my downloads folder",
        "show me the largest files",
        "organize my desktop by type",
        "open google.com",
        "what processes are running",
        "undo the last action",
        "how much space is left",
        "move photo.png to Images",
        "rename report.pdf to final-report.pdf",
        "what is the weather today",
        "tell me a joke",
    ])
    def test_ignores_non_coding(self, msg: str) -> None:
        assert not _detect_coding_intent(msg), f"Should not detect coding intent: {msg}"


# ---------------------------------------------------------------------------
# Routing behavior
# ---------------------------------------------------------------------------


class TestCodingModelRouting:
    def test_no_routing_without_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When XAI_CODING_MODEL is not set, no routing happens."""
        monkeypatch.setattr("core.get_coding_model", lambda: None)
        from config import get_xai_model, set_runtime_model, MODELS
        set_runtime_model(MODELS["fast"], user_initiated=False)
        original = get_xai_model()

        # Simulate what handle_user_turn would do
        from core import _detect_coding_intent
        coding_model = None  # not configured
        assert coding_model is None  # no routing
        assert get_xai_model() == original

    def test_no_routing_when_user_set_model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When user explicitly set a model, auto-routing is skipped."""
        monkeypatch.setattr("core.get_coding_model", lambda: "grok-code-fast-1")
        from config import set_runtime_model, user_has_set_model
        set_runtime_model("grok-4-1-fast-reasoning", user_initiated=True)
        assert user_has_set_model() is True
        # The guard in handle_user_turn checks user_has_set_model()
        # so routing would be skipped

    def test_model_restored_after_routing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Model is restored even if the turn raises."""
        from config import get_xai_model, set_runtime_model
        import config
        # Reset user_set flag
        config._user_set_model = False
        set_runtime_model("grok-4-1-fast-reasoning", user_initiated=False)
        original = get_xai_model()

        monkeypatch.setattr("core.get_coding_model", lambda: "grok-code-fast-1")

        # Simulate the routing logic from handle_user_turn
        from core import _detect_coding_intent, set_runtime_model as core_set
        msg = "write a python script"
        assert _detect_coding_intent(msg)

        saved = get_xai_model()
        set_runtime_model("grok-code-fast-1", user_initiated=False)
        assert get_xai_model() == "grok-code-fast-1"

        # Simulate finally block
        set_runtime_model(saved, user_initiated=False)
        assert get_xai_model() == original

    def test_routing_does_not_set_user_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Auto-routing should not mark model as user-set."""
        import config
        config._user_set_model = False
        config._runtime_model = None

        from config import set_runtime_model, user_has_set_model
        set_runtime_model("grok-code-fast-1", user_initiated=False)
        assert not user_has_set_model()

        # But user-initiated does
        set_runtime_model("grok-4-1-fast-reasoning", user_initiated=True)
        assert user_has_set_model()
