"""Local HTTP API for the browser-based assistant UI.

The web UI is only a frontend. Local authority stays in the Python backend:
core.py still owns model turns, tool dispatch, approval gating, undo, logging,
and safety validation.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import threading
import uuid
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from config import (
    MODELS,
    get_state_dir,
    is_dry_run,
    is_verbose,
    set_dry_run,
    set_runtime_model,
    set_verbose,
)
from core import ApprovalCard, get_startup_info, handle_user_turn
from schemas import SYSTEM_PROMPT
from session_store import SessionStore
from undo import get_history, undo_last


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _json_default(value: Any) -> str:
    return str(value)


def _card_to_dict(card: ApprovalCard, generation: int) -> dict[str, Any]:
    return {
        "generation": generation,
        "action_class": card.action_class,
        "affected_root": card.affected_root,
        "dry_run": card.dry_run,
        "risk_level": card.risk_level,
        "summary": card.summary,
        "shell_explanation": card.shell_explanation,
        "actions": [
            {
                "index": a.index,
                "tool_name": a.tool_name,
                "action_class": a.action_class,
                "label": a.label,
                "risk": a.risk,
            }
            for a in card.actions
        ],
    }


@dataclass
class WebSession:
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created: str = field(default_factory=_utc_now)
    messages: list[dict[str, Any]] = field(default_factory=lambda: [{"role": "system", "content": SYSTEM_PROMPT}])
    token_totals: dict[str, int] = field(default_factory=lambda: {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    })
    events: list[dict[str, Any]] = field(default_factory=list)
    next_event_id: int = 1
    busy: bool = False
    active_error: str | None = None
    lock: threading.RLock = field(default_factory=threading.RLock)
    approval_condition: threading.Condition = field(init=False)
    approval_generation: int = 0
    approval_answer: str | None = None

    def __post_init__(self) -> None:
        self.approval_condition = threading.Condition(self.lock)

    def add_event(self, kind: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        with self.lock:
            event = {
                "id": self.next_event_id,
                "ts": _utc_now(),
                "kind": kind,
                "payload": payload or {},
            }
            self.next_event_id += 1
            self.events.append(event)
            if len(self.events) > 1000:
                self.events = self.events[-1000:]
            return event

    def events_after(self, after: int) -> list[dict[str, Any]]:
        with self.lock:
            return [e for e in self.events if int(e["id"]) > after]

    def set_busy(self, value: bool) -> None:
        with self.lock:
            self.busy = value

    def set_approval(self, answer: str, generation: int | None = None) -> bool:
        with self.approval_condition:
            if generation is not None and generation != self.approval_generation:
                return False
            self.approval_answer = answer
            self.approval_condition.notify_all()
            return True


class WebSink:
    def __init__(self, session: WebSession, timeout_sec: float = 300.0) -> None:
        self.session = session
        self.timeout_sec = timeout_sec

    def info(self, text: str) -> None:
        self.session.add_event("info", {"text": text})

    def error(self, text: str) -> None:
        self.session.add_event("error", {"text": text})

    def assistant(self, text: str) -> None:
        self.session.add_event("assistant", {"text": text})

    def progress(self, text: str) -> None:
        self.session.add_event("progress", {"text": text})

    def tool_start(self, name: str, label: str) -> None:
        self.session.add_event("tool_start", {"name": name, "label": label})

    def tool_end(self, name: str, ok: bool) -> None:
        self.session.add_event("tool_end", {"name": name, "ok": ok})

    def usage(self, data: dict[str, int], model: str) -> None:
        with self.session.lock:
            for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                self.session.token_totals[key] = self.session.token_totals.get(key, 0) + int(data.get(key, 0))
        self.session.add_event("usage", {"usage": data, "model": model, "totals": dict(self.session.token_totals)})

    def plan(self, card: ApprovalCard) -> None:
        with self.session.lock:
            self.session.approval_generation += 1
            self.session.approval_answer = None
            generation = self.session.approval_generation
        self.session.add_event("approval", {"card": _card_to_dict(card, generation)})
        with self.session.approval_condition:
            answered = self.session.approval_condition.wait_for(
                lambda: self.session.approval_answer is not None,
                timeout=self.timeout_sec,
            )
            if not answered:
                self.session.approval_answer = "cancel"
                self.session.add_event("error", {"text": "[error] Approval timed out after 5 minutes."})

    def prompt_confirmation(self, prompt_text: str) -> str:
        with self.session.lock:
            return self.session.approval_answer or "cancel"


class SessionManager:
    def __init__(self) -> None:
        self.sessions: dict[str, WebSession] = {}
        self.lock = threading.RLock()
        self.store = SessionStore(Path(get_state_dir()) / "sessions")

    def create_session(self) -> WebSession:
        session = WebSession()
        with self.lock:
            self.sessions[session.session_id] = session
        session.add_event("session", {"session_id": session.session_id, "created": session.created})
        return session

    def get_session(self, session_id: str | None) -> WebSession:
        with self.lock:
            if session_id and session_id in self.sessions:
                return self.sessions[session_id]
            return self.create_session()

    def list_sessions(self) -> list[dict[str, Any]]:
        return [
            {
                "id": item.get("id"),
                "title": item.get("title"),
                "created": item.get("created"),
                "updated": item.get("updated"),
                "token_totals": item.get("token_totals", {}),
            }
            for item in self.store.list_sessions()
        ]

    def save(self, session: WebSession) -> dict[str, Any]:
        with session.lock:
            return self.store.save_session(
                session_id=session.session_id,
                created=session.created,
                messages=list(session.messages),
                token_totals=dict(session.token_totals),
            )

    def start_turn(self, session: WebSession, text: str) -> dict[str, Any]:
        cleaned = text.strip()
        if not cleaned:
            return {"ok": False, "error": "Message is empty."}
        with session.lock:
            if session.busy:
                return {"ok": False, "error": "Session is already running a turn."}
            session.busy = True
            session.active_error = None
        turn_id = uuid.uuid4().hex[:12]
        session.add_event("user", {"text": cleaned, "turn_id": turn_id})

        def worker() -> None:
            sink = WebSink(session)
            try:
                handle_user_turn(session.messages, cleaned, sink)
            except Exception as e:  # noqa: BLE001
                session.active_error = str(e)
                sink.error(f"[error] {e}")
            finally:
                self.save(session)
                session.set_busy(False)
                session.add_event("done", {"turn_id": turn_id, "error": session.active_error})

        threading.Thread(target=worker, name=f"web-turn-{turn_id}", daemon=True).start()
        return {"ok": True, "session_id": session.session_id, "turn_id": turn_id}


MANAGER = SessionManager()


class WebHandler(BaseHTTPRequestHandler):
    server_version = "XaiComputerWeb/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False, default=_json_default).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}

    def do_OPTIONS(self) -> None:
        self._send_json({"ok": True})

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/startup":
            session = MANAGER.get_session(parse_qs(parsed.query).get("session_id", [None])[0])
            self._send_json({
                "ok": True,
                "startup": get_startup_info(),
                "models": MODELS,
                "session": self._session_payload(session),
                "saved_sessions": MANAGER.list_sessions(),
            })
            return
        if parsed.path == "/api/events":
            qs = parse_qs(parsed.query)
            session = MANAGER.get_session(qs.get("session_id", [None])[0])
            try:
                after = int(qs.get("after", ["0"])[0])
            except ValueError:
                after = 0
            self._send_json({
                "ok": True,
                "session": self._session_payload(session),
                "events": session.events_after(after),
            })
            return
        if parsed.path == "/api/sessions":
            self._send_json({"ok": True, "sessions": MANAGER.list_sessions()})
            return
        if parsed.path == "/api/undo-history":
            self._send_json({"ok": True, "history": get_history()})
            return
        self._serve_static(parsed.path)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        body = self._read_json()
        if parsed.path == "/api/sessions":
            session = MANAGER.create_session()
            self._send_json({"ok": True, "session": self._session_payload(session)})
            return
        if parsed.path == "/api/chat":
            session = MANAGER.get_session(str(body.get("session_id") or ""))
            result = MANAGER.start_turn(session, str(body.get("text") or ""))
            self._send_json(result, HTTPStatus.OK if result.get("ok") else HTTPStatus.BAD_REQUEST)
            return
        if parsed.path == "/api/approval":
            session = MANAGER.get_session(str(body.get("session_id") or ""))
            generation = body.get("generation")
            ok = session.set_approval(str(body.get("answer") or "cancel"), int(generation) if generation is not None else None)
            self._send_json({"ok": ok})
            return
        if parsed.path == "/api/settings":
            if "dry_run" in body:
                set_dry_run(bool(body["dry_run"]))
            if "verbose" in body:
                set_verbose(bool(body["verbose"]))
            if body.get("model"):
                set_runtime_model(str(body["model"]))
            self._send_json({"ok": True, "startup": get_startup_info()})
            return
        if parsed.path == "/api/undo":
            result = undo_last()
            self._send_json(result, HTTPStatus.OK if result.get("ok") else HTTPStatus.BAD_REQUEST)
            return
        self._send_json({"ok": False, "error": "Not found."}, HTTPStatus.NOT_FOUND)

    def _session_payload(self, session: WebSession) -> dict[str, Any]:
        with session.lock:
            return {
                "id": session.session_id,
                "created": session.created,
                "busy": session.busy,
                "token_totals": dict(session.token_totals),
                "event_count": session.next_event_id - 1,
            }

    def _serve_static(self, request_path: str) -> None:
        root = Path(__file__).resolve().parent / "web" / "dist"
        if request_path in ("", "/"):
            target = root / "index.html"
        else:
            target = (root / request_path.lstrip("/")).resolve()
            try:
                target.relative_to(root.resolve())
            except ValueError:
                self.send_error(HTTPStatus.FORBIDDEN)
                return
            if not target.exists() and "." not in request_path.rsplit("/", 1)[-1]:
                target = root / "index.html"
        if not target.exists() or not target.is_file():
            self._send_json({
                "ok": False,
                "error": "Web app build not found. Run `cd web && npm install && npm run build`, or use `npm run dev`.",
            }, HTTPStatus.NOT_FOUND)
            return
        data = target.read_bytes()
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def run(host: str = "127.0.0.1", port: int = 8765, *, open_browser: bool = False) -> None:
    server = ThreadingHTTPServer((host, port), WebHandler)
    url = f"http://{host}:{port}"
    print(f"xai-computer web server running at {url}")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nxai-computer web server stopped.")
    finally:
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the xai-computer local web UI server.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true", help="Open the web UI in your default browser.")
    args = parser.parse_args()
    run(args.host, args.port, open_browser=args.open)


if __name__ == "__main__":
    main()
