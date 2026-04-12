"""Append-only JSONL action logging with session tracking."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import get_log_path, is_dry_run

SESSION_ID: str = uuid.uuid4().hex[:12]


def log_event(
    event: str,
    details: dict[str, Any] | None = None,
    *,
    tool_name: str | None = None,
    parameters: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
    phase: str | None = None,
    user_request: str | None = None,
) -> None:
    """
    Write one structured log record.

    phase values: preview, confirmed, executed, skipped, undone, dry_run, error
    """
    path: Path = get_log_path()
    record: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "session": SESSION_ID,
        "event": event,
        "dry_run": is_dry_run(),
    }
    if phase:
        record["phase"] = phase
    if tool_name:
        record["tool"] = tool_name
    if parameters:
        record["params"] = parameters
    if result:
        record["result"] = result
    if user_request:
        record["user_request"] = user_request
    if details:
        record["details"] = details

    line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(line)
