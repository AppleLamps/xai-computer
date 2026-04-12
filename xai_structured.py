"""xAI SDK wrapper for structured output calls.

Uses the xai_sdk Client and chat.parse() for type-safe responses.
This module is used ONLY for enrichment (better labels, explanations,
summaries) — never for safety decisions.  Falls back gracefully if the
SDK call fails for any reason.

The main tool-calling loop in core.py continues to use xai_client.py.
"""

from __future__ import annotations

import os
from typing import Any, TypeVar

from pydantic import BaseModel

from config import get_xai_api_key, get_xai_model
from logger import log_event

T = TypeVar("T", bound=BaseModel)

# Grok 4 family model prefixes — structured outputs require these
_GROK4_PREFIXES = ("grok-4",)


def _is_grok4_model(model: str | None = None) -> bool:
    """Check if the current model supports structured outputs."""
    m = (model or get_xai_model()).casefold()
    return any(m.startswith(p) for p in _GROK4_PREFIXES)


def structured_parse(
    model_class: type[T],
    prompt: str,
    *,
    system_prompt: str = "",
    model: str | None = None,
) -> T | None:
    """Call the xAI SDK's chat.parse() and return a parsed Pydantic object.

    Returns None on any failure (network, schema mismatch, unsupported model).
    Never raises — the caller should treat None as "use fallback behavior."
    """
    resolved_model = model or get_xai_model()

    if not _is_grok4_model(resolved_model):
        log_event("structured_output_skipped", {
            "model": resolved_model,
            "reason": "Not a Grok 4 model; structured outputs not supported.",
        })
        return None

    api_key = get_xai_api_key()
    if not api_key:
        return None

    try:
        from xai_sdk import Client
        from xai_sdk.chat import system, user

        client = Client(api_key=api_key)
        chat = client.chat.create(model=resolved_model)

        if system_prompt:
            chat.append(system(system_prompt))
        chat.append(user(prompt))

        _response, result = chat.parse(model_class)

        log_event("structured_output_success", {
            "model": resolved_model,
            "schema": model_class.__name__,
        })
        return result

    except Exception as e:
        log_event("structured_output_error", {
            "model": resolved_model,
            "schema": model_class.__name__,
            "error": str(e),
        })
        return None


# ---------------------------------------------------------------------------
# Convenience wrappers for specific structured output tasks
# ---------------------------------------------------------------------------


def explain_shell_command(command: str, tier: str) -> dict[str, str] | None:
    """Get a structured explanation of a shell command. Returns dict or None."""
    from structured_models import ShellCommandExplanation

    prompt = (
        f"Explain this shell command concisely.\n"
        f"Command: {command}\n"
        f"Security tier: {tier}\n"
        f"What does it do? What are the side effects? Why is it at this risk level?"
    )
    result = structured_parse(
        ShellCommandExplanation, prompt,
        system_prompt="You explain shell commands concisely and accurately for a non-technical user.",
    )
    if result is None:
        return None
    return {
        "command": result.command,
        "what_it_does": result.what_it_does,
        "side_effects": result.side_effects,
        "risk_reason": result.risk_reason,
    }


def summarize_execution(
    results: list[dict[str, Any]],
    dry_run: bool,
) -> dict[str, Any] | None:
    """Get a structured summary of execution results. Returns dict or None."""
    from structured_models import ExecutionSummary

    completed = sum(1 for r in results if r.get("ok"))
    skipped = len(results) - completed
    collisions = sum(1 for r in results if "_dup" in str(r.get("destination", "")))
    has_shell = any(r.get("tier") for r in results)

    prompt = (
        f"Summarize this execution result in one sentence.\n"
        f"Actions attempted: {len(results)}\n"
        f"Succeeded: {completed}\n"
        f"Skipped/failed: {skipped}\n"
        f"File collisions resolved: {collisions}\n"
        f"Dry run: {dry_run}\n"
        f"Contains shell commands: {has_shell}\n"
        f"Undo available: {not dry_run and not has_shell}"
    )
    result = structured_parse(
        ExecutionSummary, prompt,
        system_prompt="Summarize execution results concisely.",
    )
    if result is None:
        return None
    return {
        "actions_completed": result.actions_completed,
        "actions_skipped": result.actions_skipped,
        "collisions_handled": result.collisions_handled,
        "undo_available": result.undo_available,
        "dry_run": result.dry_run,
        "one_line_summary": result.one_line_summary,
    }
