"""Persistence helpers for GUI chat sessions."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from schemas import SYSTEM_PROMPT


class SessionStore:
    def __init__(self, sessions_dir: Path) -> None:
        self.sessions_dir = sessions_dir

    def ensure_dir(self) -> Path:
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        return self.sessions_dir

    def session_path(self, session_id: str) -> Path:
        return self.ensure_dir() / f"{session_id}.json"

    def session_title(self, messages: list[dict[str, Any]], max_len: int = 60) -> str:
        for msg in messages:
            if msg.get("role") == "user":
                text = str(msg.get("content", "")).strip().splitlines()
                first = text[0] if text else ""
                if len(first) > max_len:
                    return first[: max_len - 1] + "\u2026"
                return first or "(untitled)"
        return "(untitled)"

    def save_session(
        self,
        *,
        session_id: str,
        created: str,
        messages: list[dict[str, Any]],
        token_totals: dict[str, int],
        title_max: int = 60,
    ) -> dict[str, Any]:
        if len([m for m in messages if m.get("role") != "system"]) == 0:
            return {"ok": True, "skipped": True}

        payload = {
            "id": session_id,
            "created": created,
            "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "title": self.session_title(messages, title_max),
            "messages": messages,
            "token_totals": dict(token_totals),
        }
        path = self.session_path(session_id)
        tmp = path.with_suffix(".json.tmp")
        try:
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
            os.replace(tmp, path)
        except OSError as e:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
            return {"ok": False, "error": str(e), "path": str(path)}
        return {"ok": True, "path": str(path), "title": payload["title"]}

    def list_sessions(self) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for p in self.ensure_dir().glob("*.json"):
            loaded = self.load_session(p)
            if not loaded.get("ok"):
                continue
            data = loaded["data"]
            try:
                data["_mtime"] = p.stat().st_mtime
            except OSError:
                data["_mtime"] = 0
            data["_path"] = p
            entries.append(data)
        entries.sort(key=lambda e: e.get("_mtime", 0), reverse=True)
        return entries

    def load_session(self, path: Path) -> dict[str, Any]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            return {"ok": False, "error": str(e), "path": str(path)}
        messages = list(data.get("messages") or [])
        if not messages or messages[0].get("role") != "system":
            messages.insert(0, {"role": "system", "content": SYSTEM_PROMPT})
        data["messages"] = messages
        return {"ok": True, "data": data, "path": str(path)}
