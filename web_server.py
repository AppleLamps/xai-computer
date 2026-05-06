"""Local HTTP API for the browser-based assistant UI.

The web UI is only a frontend. Local authority stays in the Python backend:
core.py still owns model turns, tool dispatch, approval gating, undo, logging,
and safety validation.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import secrets
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
    get_log_dir,
    get_state_dir,
    is_dry_run,
    is_verbose,
    load_persisted_web_settings,
    reset_allowed_roots,
    set_allowed_roots,
    set_auto_switch_models,
    set_dry_run,
    set_runtime_model,
    set_verbose,
)
from core import ApprovalCard, get_startup_info, handle_user_turn
from logger import log_event
from safety import is_path_allowed
from schemas import SYSTEM_PROMPT
from session_store import SessionStore
from shell_guard import redact_secrets
from undo import get_history, undo_last


_ACTION_DETAIL_MAX = 600


def _redact_preview(text: str, *, max_chars: int = _ACTION_DETAIL_MAX) -> dict[str, Any]:
    """Build a redacted, length-capped preview of a free-form text argument."""
    raw = "" if text is None else str(text)
    redacted = redact_secrets(raw)
    truncated = len(redacted) > max_chars
    preview = redacted[:max_chars] + ("…" if truncated else "")
    return {
        "preview": preview,
        "chars": len(raw),
        "bytes": len(raw.encode("utf-8", errors="replace")),
        "truncated": truncated,
    }


def _action_details(tool: str, args: dict[str, Any]) -> dict[str, Any]:
    """Return a small, redacted dict the UI can render verbatim per action."""

    def pick(*names: str) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name in names:
            if name in args and args[name] is not None:
                value = args[name]
                if isinstance(value, str):
                    out[name] = redact_secrets(value)
                else:
                    out[name] = value
        return out

    if tool == "run_command":
        details = pick("command", "working_dir", "timeout_sec")
        return details
    if tool == "write_file":
        details = pick("path", "overwrite")
        details["content"] = _redact_preview(args.get("content", ""))
        return details
    if tool == "append_file":
        details = pick("path")
        details["content"] = _redact_preview(args.get("content", ""))
        return details
    if tool == "replace_in_file":
        details = pick("path", "replace_all")
        details["old_text"] = _redact_preview(args.get("old_text", ""))
        details["new_text"] = _redact_preview(args.get("new_text", ""))
        return details
    if tool == "apply_patch":
        details = pick("path")
        diff = args.get("unified_diff", "") or ""
        details["unified_diff"] = _redact_preview(diff, max_chars=1200)
        details["hunks"] = diff.count("@@") // 2 if diff else 0
        return details
    if tool in ("move_file", "copy_file"):
        return pick("source", "destination", "overwrite")
    if tool == "rename_file":
        return pick("source", "new_name")
    if tool == "delete_file_to_recycle_bin":
        return pick("path")
    if tool == "create_folder":
        return pick("path")
    if tool == "organize_desktop_by_type":
        return pick("desktop_path")
    if tool == "organize_folder":
        return pick("path", "mode")
    if tool == "start_process":
        details = pick("executable", "working_dir")
        raw_args = args.get("args") or []
        if isinstance(raw_args, list):
            details["args"] = [redact_secrets(str(item)) for item in raw_args]
        return details
    if tool == "stop_process":
        return pick("pid", "force")
    if tool == "focus_window":
        return pick("window_id", "title_substring")
    if tool in ("move_mouse", "click", "scroll"):
        return pick("x", "y", "button", "clicks", "amount", "direction")
    if tool == "type_text":
        details = pick("delay_ms")
        details["text"] = _redact_preview(args.get("text", ""), max_chars=240)
        return details
    if tool == "press_hotkey":
        keys = args.get("keys") or []
        if isinstance(keys, list):
            return {"keys": [str(k) for k in keys]}
        return {}
    if tool == "browser_navigate":
        return pick("url", "wait_for")
    if tool == "browser_click":
        return pick("selector", "nth")
    if tool == "browser_fill":
        details = pick("selector")
        details["text"] = _redact_preview(args.get("text", ""), max_chars=240)
        return details
    if tool == "browser_press":
        return pick("selector", "key")
    if tool == "browser_download":
        return pick("url", "click_selector", "save_as")
    if tool == "browser_screenshot":
        return pick("selector", "full_page", "save_as")
    if tool == "read_clipboard":
        return pick("max_chars")
    if tool == "window_screenshot":
        return pick("window_id")
    return {}


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
                "details": _action_details(a.tool_name, a.arguments),
            }
            for a in card.actions
        ],
    }


def _message_events_from_history(messages: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    """Convert saved chat messages into display events for restored web sessions."""
    events: list[tuple[str, dict[str, Any]]] = []
    tool_names: dict[str, str] = {}
    for msg in messages:
        role = msg.get("role")
        if role == "system":
            continue
        content = msg.get("content")
        if role == "user":
            events.append(("user", {"text": str(content or "")}))
        elif role == "assistant":
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function") or {}
                if tc.get("id") and fn.get("name"):
                    tool_names[str(tc["id"])] = str(fn["name"])
            if content:
                events.append(("assistant", {"text": str(content)}))
        elif role == "tool":
            try:
                payload = json.loads(str(content or "{}"))
            except json.JSONDecodeError:
                payload = {"ok": False, "error": "Could not parse saved tool result."}
            name = tool_names.get(str(msg.get("tool_call_id")), "saved_tool_result")
            event_payload: dict[str, Any] = {"name": name, "result": payload}
            artifact = _artifact_from_result(name, payload)
            if artifact:
                event_payload["artifact"] = artifact
            events.append(("tool_result", event_payload))
            if artifact:
                events.append(("artifact", artifact))
    return events


_artifact_lock = threading.Lock()
_released_artifacts: set[str] = set()


def _register_artifact_path(raw: Any) -> None:
    if not raw:
        return
    try:
        resolved = Path(str(raw)).expanduser().resolve()
    except OSError:
        return
    key = str(resolved).casefold()
    with _artifact_lock:
        _released_artifacts.add(key)


def _is_released_artifact(target: Path) -> bool:
    key = str(target).casefold()
    with _artifact_lock:
        return key in _released_artifacts


def _artifact_from_result(tool: str, result: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(result, dict) or not result.get("ok"):
        return None
    if tool in {"take_screenshot", "window_screenshot", "browser_screenshot"} and result.get("path"):
        _register_artifact_path(result["path"])
        return {"kind": "screenshot", "title": tool, "path": str(result["path"]), "tool": tool}
    if tool == "browser_download" and result.get("path"):
        _register_artifact_path(result["path"])
        return {"kind": "download", "title": "Browser download", "path": str(result["path"]), "tool": tool}
    if tool == "copy_to_clipboard":
        return {
            "kind": "clipboard",
            "title": "Clipboard updated",
            "chars": result.get("chars"),
            "preview": result.get("text_preview") or result.get("preview"),
            "tool": tool,
        }
    for key in ("destination", "path"):
        value = result.get(key)
        if value and tool in {
            "write_file", "append_file", "replace_in_file", "apply_patch", "copy_file",
            "move_file", "rename_file", "create_folder",
        }:
            _register_artifact_path(value)
            return {"kind": "file", "title": tool, "path": str(value), "tool": tool}
    return None


def _validate_allowed_root_candidate(raw_path: str) -> tuple[bool, Path | str]:
    if not raw_path.strip():
        return False, "Path is required."
    if ".." in raw_path:
        return False, "Path contains suspicious traversal."
    try:
        resolved = Path(raw_path).expanduser().resolve()
    except OSError as e:
        return False, f"Invalid path: {e}"
    if not resolved.exists() or not resolved.is_dir():
        return False, f"Allowed root must be an existing folder: {resolved}"
    if not is_path_allowed(resolved, roots=[resolved]):
        return False, f"Path targets a protected system location: {resolved}"
    return True, resolved


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
    stopped: bool = False
    bypass_approvals: bool = False
    active_error: str | None = None
    active_turn_id: str | None = None
    lock: threading.RLock = field(default_factory=threading.RLock)
    approval_condition: threading.Condition = field(init=False)
    event_condition: threading.Condition = field(init=False)
    approval_generation: int = 0
    approval_answer: str | None = None
    stop_event: threading.Event = field(default_factory=threading.Event)
    closed: bool = False

    def __post_init__(self) -> None:
        self.approval_condition = threading.Condition(self.lock)
        self.event_condition = threading.Condition(self.lock)

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
            self.event_condition.notify_all()
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

    def request_stop(self) -> bool:
        with self.approval_condition:
            was_busy = self.busy
            self.stopped = True
            self.stop_event.set()
            self.approval_answer = "cancel"
            self.approval_condition.notify_all()
        return was_busy


class WebSink:
    def __init__(self, session: WebSession, timeout_sec: float = 300.0) -> None:
        self.session = session
        self.timeout_sec = timeout_sec
        self.stop_event = session.stop_event

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

    def start_stream(self) -> None:
        self.session.add_event("phase", {"phase": "planning", "label": "Planning response"})
        self.session.add_event("stream_start", {})

    def stream_delta(self, text: str) -> None:
        if not text:
            return
        self.session.add_event("stream_delta", {"text": text})

    def cancel_stream(self) -> None:
        self.session.add_event("stream_cancel", {})

    def tool_result(self, name: str, result: dict[str, Any]) -> None:
        payload = {"name": name, "result": result}
        artifact = _artifact_from_result(name, result)
        if artifact:
            payload["artifact"] = artifact
        self.session.add_event("tool_result", payload)
        if artifact:
            self.session.add_event("artifact", artifact)

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
        if self.session.bypass_approvals and card.action_class != "continuation":
            with self.session.approval_condition:
                self.session.approval_answer = "yes"
                self.session.approval_condition.notify_all()
            payload = {
                "generation": generation,
                "card": _card_to_dict(card, generation),
                "text": (
                    f"BYPASS ALL is on for this session; auto-approved {card.summary}. "
                    "Hard safety blocks and path validation still apply."
                ),
            }
            self.session.add_event("auto_approved", payload)
            self.session.add_event("info", {
                "text": payload["text"],
            })
            log_event("approval_bypassed", {
                "summary": card.summary,
                "risk_level": card.risk_level,
                "action_class": card.action_class,
                "generation": generation,
            })
            return
        self.session.add_event("approval", {"card": _card_to_dict(card, generation)})
        with self.session.approval_condition:
            answered = self.session.approval_condition.wait_for(
                lambda: self.session.approval_answer is not None or self.session.stop_event.is_set(),
                timeout=self.timeout_sec,
            )
            if self.session.stop_event.is_set():
                self.session.approval_answer = "cancel"
            elif not answered:
                self.session.approval_answer = "cancel"
                self.session.add_event("error", {"text": "[error] Approval timed out after 5 minutes."})

    def prompt_confirmation(self, prompt_text: str) -> str:
        with self.session.lock:
            return self.session.approval_answer or "cancel"


class SessionManager:
    def __init__(self) -> None:
        load_persisted_web_settings()
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
            if session_id:
                loaded = self.load_session(session_id)
                if loaded is not None:
                    return loaded
            return self.create_session()

    def load_session(self, session_id: str) -> WebSession | None:
        loaded = self.store.load_session(self.store.session_path(session_id))
        if not loaded.get("ok"):
            return None
        data = loaded["data"]
        session = WebSession(
            session_id=str(data.get("id") or session_id),
            created=str(data.get("created") or _utc_now()),
            messages=list(data.get("messages") or [{"role": "system", "content": SYSTEM_PROMPT}]),
            token_totals=dict(data.get("token_totals") or {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            }),
        )
        for kind, payload in _message_events_from_history(session.messages):
            session.add_event(kind, payload)
        with self.lock:
            self.sessions[session.session_id] = session
        return session

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
        turn_id = uuid.uuid4().hex[:12]
        with session.lock:
            if session.busy:
                return {"ok": False, "error": "Session is already running a turn."}
            session.busy = True
            session.stopped = False
            session.stop_event.clear()
            session.active_error = None
            session.active_turn_id = turn_id
        session.add_event("user", {"text": cleaned, "turn_id": turn_id})

        def worker() -> None:
            sink = WebSink(session)
            try:
                handle_user_turn(session.messages, cleaned, sink)
            except Exception as e:  # noqa: BLE001
                with session.lock:
                    session.active_error = str(e)
                sink.error(f"[error] {e}")
            finally:
                self.save(session)
                with session.lock:
                    canceled = session.stop_event.is_set() or session.stopped
                    current_error = session.active_error
                    session.active_turn_id = None
                if canceled and not current_error:
                    sink.info("Stopped by user.")
                    session.add_event("stopped", {"turn_id": turn_id})
                session.set_busy(False)
                session.add_event("done", {
                    "turn_id": turn_id,
                    "error": current_error,
                    "canceled": canceled,
                })

        threading.Thread(target=worker, name=f"web-turn-{turn_id}", daemon=True).start()
        return {"ok": True, "session_id": session.session_id, "turn_id": turn_id}

    def stop_turn(self, session: WebSession) -> dict[str, Any]:
        was_busy = session.request_stop()
        with session.lock:
            current_turn_id = session.active_turn_id
        session.add_event("stopped", {"turn_id": current_turn_id, "requested": True})
        return {"ok": True, "stopping": was_busy, "session_id": session.session_id}


MANAGER = SessionManager()


class WebHandler(BaseHTTPRequestHandler):
    server_version = "XaiComputerWeb/0.1"
    auth_token: str = ""

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False, default=_json_default).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
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

    def _expected_hosts(self) -> set[str]:
        port = self.server.server_address[1]
        return {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}

    def _check_host(self) -> bool:
        host = (self.headers.get("Host") or "").strip().lower()
        return host in self._expected_hosts()

    def _check_auth(self, parsed) -> bool:
        expected = WebHandler.auth_token
        if not expected:
            return False
        provided = self.headers.get("X-Auth-Token") or ""
        if not provided and parsed is not None:
            qs = parse_qs(parsed.query)
            provided = qs.get("token", [""])[0]
        if not provided:
            return False
        try:
            return secrets.compare_digest(provided, expected)
        except (TypeError, ValueError):
            return False

    def _reject(self, status: int, message: str) -> None:
        body = json.dumps({"ok": False, "error": message}).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _gate_api(self, parsed) -> bool:
        if not self._check_host():
            self._reject(HTTPStatus.FORBIDDEN, "Host header rejected.")
            return False
        if not self._check_auth(parsed):
            self._reject(HTTPStatus.UNAUTHORIZED, "Authentication required.")
            return False
        return True

    def do_OPTIONS(self) -> None:
        self._reject(HTTPStatus.METHOD_NOT_ALLOWED, "Preflight not supported.")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            if not self._gate_api(parsed):
                return
        elif not self._check_host():
            self._reject(HTTPStatus.FORBIDDEN, "Host header rejected.")
            return
        if parsed.path == "/api/startup":
            session = MANAGER.get_session(parse_qs(parsed.query).get("session_id", [None])[0])
            self._send_json({
                "ok": True,
                "startup": self._startup_payload(session),
                "models": MODELS,
                "session": self._session_payload(session),
                "saved_sessions": MANAGER.list_sessions(),
            })
            return
        if parsed.path == "/api/session":
            qs = parse_qs(parsed.query)
            session_id = qs.get("session_id", [""])[0]
            session = MANAGER.get_session(session_id)
            self._send_json({
                "ok": True,
                "startup": self._startup_payload(session),
                "session": self._session_payload(session),
                "events": session.events_after(0),
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
        if parsed.path == "/api/stream":
            qs = parse_qs(parsed.query)
            self._serve_event_stream(qs)
            return
        if parsed.path == "/api/sessions":
            self._send_json({"ok": True, "sessions": MANAGER.list_sessions()})
            return
        if parsed.path == "/api/undo-history":
            self._send_json({"ok": True, "history": get_history()})
            return
        if parsed.path == "/api/local-file":
            qs = parse_qs(parsed.query)
            self._serve_local_file(qs.get("path", [""])[0])
            return
        self._serve_static(parsed.path)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if not self._gate_api(parsed):
            return
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
        if parsed.path == "/api/stop":
            session = MANAGER.get_session(str(body.get("session_id") or ""))
            self._send_json(MANAGER.stop_turn(session))
            return
        if parsed.path == "/api/settings":
            session = MANAGER.get_session(str(body.get("session_id") or "")) if body.get("session_id") else None
            if "dry_run" in body:
                set_dry_run(bool(body["dry_run"]), persist=True)
            if "verbose" in body:
                set_verbose(bool(body["verbose"]), persist=True)
            if "bypass_approvals" in body:
                if session is not None:
                    session.bypass_approvals = bool(body["bypass_approvals"])
            if "auto_switch_models" in body:
                set_auto_switch_models(bool(body["auto_switch_models"]), persist=True)
            if body.get("model"):
                set_runtime_model(str(body["model"]), persist=True)
            self._send_json({"ok": True, "startup": self._startup_payload(session)})
            return
        if parsed.path == "/api/allowed-roots":
            action = str(body.get("action") or "add")
            if action == "reset":
                reset_allowed_roots()
                self._send_json({"ok": True, "startup": self._startup_payload(None)})
                return
            raw_path = str(body.get("path") or "")
            ok, value = _validate_allowed_root_candidate(raw_path)
            if not ok:
                self._send_json({"ok": False, "error": str(value)}, HTTPStatus.BAD_REQUEST)
                return
            current = [Path(p) for p in get_startup_info()["allowed_roots"]]
            target = value if isinstance(value, Path) else Path(str(value))
            target_key = str(target).casefold()
            if action == "remove":
                next_roots = [p for p in current if str(p).casefold() != target_key]
            else:
                next_roots = current
                if all(str(p).casefold() != target_key for p in next_roots):
                    next_roots.append(target)
            if next_roots:
                set_allowed_roots(next_roots)
            else:
                reset_allowed_roots()
            self._send_json({"ok": True, "startup": self._startup_payload(None)})
            return
        if parsed.path == "/api/undo":
            result = undo_last()
            self._send_json(result, HTTPStatus.OK if result.get("ok") else HTTPStatus.BAD_REQUEST)
            return
        if parsed.path == "/api/open-logs":
            try:
                log_dir = get_log_dir()
                os_startfile = getattr(__import__("os"), "startfile", None)
                if os_startfile:
                    os_startfile(str(log_dir))
                self._send_json({"ok": True, "path": str(log_dir)})
            except OSError as e:
                self._send_json({"ok": False, "error": str(e)}, HTTPStatus.BAD_REQUEST)
            return
        self._send_json({"ok": False, "error": "Not found."}, HTTPStatus.NOT_FOUND)

    def _session_payload(self, session: WebSession) -> dict[str, Any]:
        with session.lock:
            return {
                "id": session.session_id,
                "created": session.created,
                "busy": session.busy,
                "stopped": session.stopped,
                "bypass_approvals": session.bypass_approvals,
                "active_error": session.active_error,
                "token_totals": dict(session.token_totals),
                "event_count": session.next_event_id - 1,
            }

    def _startup_payload(self, session: WebSession | None) -> dict[str, Any]:
        startup = get_startup_info()
        startup["bypass_approvals"] = bool(session.bypass_approvals) if session is not None else False
        return startup

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
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if request_path in ("", "/") or request_path.endswith(".html"):
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _serve_event_stream(self, qs: dict[str, list[str]]) -> None:
        session_id = qs.get("session_id", [None])[0]
        session = MANAGER.get_session(session_id)
        last_header = self.headers.get("Last-Event-ID")
        try:
            after = int(last_header) if last_header else int(qs.get("after", ["0"])[0])
        except ValueError:
            after = 0
        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-store")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
        except OSError:
            return

        def write_chunk(data: bytes) -> bool:
            try:
                self.wfile.write(data)
                self.wfile.flush()
                return True
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                return False

        last = after
        if not write_chunk(f"retry: 1500\n: connected after={last}\n\n".encode("utf-8")):
            return
        keepalive_seconds = 20.0
        while True:
            with session.event_condition:
                pending = [e for e in session.events if int(e["id"]) > last]
                if not pending:
                    woke_for_event = session.event_condition.wait(timeout=keepalive_seconds)
                    if woke_for_event:
                        pending = [e for e in session.events if int(e["id"]) > last]
            if not pending:
                if not write_chunk(b": keepalive\n\n"):
                    return
                continue
            for event in pending:
                payload = json.dumps(event, default=_json_default, ensure_ascii=False)
                chunk = f"id: {event['id']}\ndata: {payload}\n\n".encode("utf-8")
                if not write_chunk(chunk):
                    return
                last = int(event["id"])

    def _serve_local_file(self, raw_path: str) -> None:
        sec_fetch_site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        if sec_fetch_site and sec_fetch_site != "same-origin":
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        try:
            target = Path(raw_path).expanduser().resolve()
        except OSError:
            self.send_error(HTTPStatus.BAD_REQUEST)
            return
        if not _is_released_artifact(target) or not target.exists() or not target.is_file():
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        data = target.read_bytes()
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        disposition = "inline" if ctype.startswith("image/") else "attachment"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Disposition",
            f'{disposition}; filename="{target.name}"',
        )
        self.end_headers()
        self.wfile.write(data)


def run(host: str = "127.0.0.1", port: int = 8765, *, open_browser: bool = False) -> None:
    token = secrets.token_urlsafe(32)
    WebHandler.auth_token = token
    server = ThreadingHTTPServer((host, port), WebHandler)
    base_url = f"http://{host}:{port}"
    launch_url = f"{base_url}/?token={token}"
    print(f"xai-computer web server running at {base_url}")
    print(f"Open this URL in your browser (token rotates every launch):\n  {launch_url}")
    if open_browser:
        webbrowser.open(launch_url)
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
