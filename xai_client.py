"""Minimal HTTPS client for xAI chat completions (OpenAI-compatible)."""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

XAI_BASE_URL = "https://api.x.ai/v1/chat/completions"


@dataclass
class ToolCallSpec:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ChatCompletionResult:
    message_role: str
    content: str | None
    tool_calls: list[ToolCallSpec]
    raw: dict[str, Any]


def _parse_tool_calls(message: dict[str, Any]) -> list[ToolCallSpec]:
    raw_calls = message.get("tool_calls") or []
    out: list[ToolCallSpec] = []
    for tc in raw_calls:
        fn = tc.get("function") or {}
        name = fn.get("name") or ""
        args_raw = fn.get("arguments") or "{}"
        try:
            args = json.loads(args_raw) if isinstance(args_raw, str) else dict(args_raw)
        except json.JSONDecodeError:
            args = {}
        out.append(ToolCallSpec(id=tc.get("id") or "", name=name, arguments=args))
    return out


def chat_completion(
    api_key: str,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    *,
    timeout_sec: float = 120.0,
    temperature: float = 0.2,
) -> ChatCompletionResult:
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "tools": tools,
        "tool_choice": "auto",
        "temperature": temperature,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        XAI_BASE_URL,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec, context=ctx) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_text = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"xAI HTTP {e.code}: {err_text}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"xAI connection error: {e}") from e

    choices = body.get("choices") or []
    if not choices:
        raise RuntimeError(f"xAI unexpected response (no choices): {json.dumps(body)[:2000]}")
    msg = choices[0].get("message") or {}
    content = msg.get("content")
    tool_calls = _parse_tool_calls(msg)
    return ChatCompletionResult(
        message_role=msg.get("role") or "assistant",
        content=content if isinstance(content, str) else None,
        tool_calls=tool_calls,
        raw=body,
    )
