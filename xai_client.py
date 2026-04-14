"""Minimal HTTPS client for xAI chat completions (OpenAI-compatible)."""

from __future__ import annotations

import json
import ssl
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

XAI_BASE_URL = "https://api.x.ai/v1/chat/completions"

# Retry configuration — applied to transient network/server errors only.
_RETRYABLE_HTTP_CODES = frozenset({429, 500, 502, 503, 504})
_MAX_RETRIES = 3
_RETRY_BASE_DELAY = 1.0  # seconds; doubles each attempt (1s, 2s, 4s)


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
    for attempt in range(_MAX_RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=timeout_sec, context=ctx) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            break  # success — exit retry loop
        except urllib.error.HTTPError as e:
            is_last = attempt == _MAX_RETRIES - 1
            if e.code not in _RETRYABLE_HTTP_CODES or is_last:
                err_text = e.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"xAI HTTP {e.code}: {err_text}") from e
        except urllib.error.URLError as e:
            if attempt == _MAX_RETRIES - 1:
                raise RuntimeError(f"xAI connection error: {e}") from e
        time.sleep(_RETRY_BASE_DELAY * (2 ** attempt))

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


def chat_completion_stream(
    api_key: str,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    *,
    stop_event: threading.Event | None = None,
    on_delta: Callable[[str], None] | None = None,
    timeout_sec: float = 120.0,
    temperature: float = 0.2,
) -> ChatCompletionResult:
    """Streaming variant — calls on_delta(chunk) for each content token.

    Returns a ChatCompletionResult identical to chat_completion() once the
    stream is exhausted (or stopped early via stop_event).
    """
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "tools": tools,
        "tool_choice": "auto",
        "temperature": temperature,
        "stream": True,
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

    content_acc: list[str] = []
    # tool_calls_acc: index -> {id, name, arguments_parts}
    tool_calls_acc: dict[int, dict[str, Any]] = {}

    for attempt in range(_MAX_RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=timeout_sec, context=ctx) as resp:
                for raw_line in resp:
                    if stop_event and stop_event.is_set():
                        break
                    line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                    if not line.startswith("data: "):
                        continue
                    payload_str = line[6:]
                    if payload_str.strip() == "[DONE]":
                        break
                    try:
                        chunk = json.loads(payload_str)
                    except json.JSONDecodeError:
                        continue
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}

                    # Content delta
                    text_chunk = delta.get("content")
                    if text_chunk:
                        content_acc.append(text_chunk)
                        if on_delta:
                            on_delta(text_chunk)

                    # Tool-call delta accumulation
                    for tc_delta in (delta.get("tool_calls") or []):
                        idx = tc_delta.get("index", 0)
                        if idx not in tool_calls_acc:
                            tool_calls_acc[idx] = {"id": "", "name": "", "args": []}
                        entry = tool_calls_acc[idx]
                        if tc_delta.get("id"):
                            entry["id"] = tc_delta["id"]
                        fn = tc_delta.get("function") or {}
                        if fn.get("name"):
                            entry["name"] = fn["name"]
                        if fn.get("arguments"):
                            entry["args"].append(fn["arguments"])
            break  # success — exit retry loop
        except urllib.error.HTTPError as e:
            is_last = attempt == _MAX_RETRIES - 1
            if e.code not in _RETRYABLE_HTTP_CODES or is_last:
                err_text = e.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"xAI HTTP {e.code}: {err_text}") from e
        except urllib.error.URLError as e:
            if attempt == _MAX_RETRIES - 1:
                raise RuntimeError(f"xAI connection error: {e}") from e
        time.sleep(_RETRY_BASE_DELAY * (2 ** attempt))

    # Build tool call specs from accumulated data
    tool_call_specs: list[ToolCallSpec] = []
    for idx in sorted(tool_calls_acc):
        entry = tool_calls_acc[idx]
        args_raw = "".join(entry["args"])
        try:
            args = json.loads(args_raw) if args_raw else {}
        except json.JSONDecodeError:
            args = {}
        tool_call_specs.append(ToolCallSpec(
            id=entry["id"],
            name=entry["name"],
            arguments=args,
        ))

    full_content = "".join(content_acc) or None
    return ChatCompletionResult(
        message_role="assistant",
        content=full_content,
        tool_calls=tool_call_specs,
        raw={},
    )
