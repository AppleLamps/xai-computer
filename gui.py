"""Tkinter GUI for the local Windows desktop assistant.

Implements the OutputSink protocol from core.py so the GUI shares
the same orchestration, tools, safety, and undo logic as the CLI.

Launch:  python gui.py
"""

from __future__ import annotations

import os
import sys
import threading
import tkinter as tk
import uuid
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext
from typing import Any

from config import (
    MODELS,
    get_allowed_roots,
    get_log_dir,
    get_state_dir,
    get_xai_api_key,
    get_xai_model,
    is_dry_run,
    is_verbose,
    reset_allowed_roots,
    set_allowed_roots,
    set_dry_run,
    set_runtime_model,
    set_verbose,
)
from core import ApprovalCard, get_startup_info, handle_user_turn
from logger import SESSION_ID
from schemas import SYSTEM_PROMPT
from session_store import SessionStore
from undo import get_history, undo_last

from gui_markdown import insert_markdown

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    _DND_AVAILABLE = True
except ImportError:
    _DND_AVAILABLE = False

# ---------------------------------------------------------------------------
# Design tokens
# ---------------------------------------------------------------------------

_APP_TITLE = "xai-computer"
_INPUT_MIN_LINES = 3
_INPUT_MAX_LINES = 8
_WIN_W = 960
_WIN_H = 720
_MIN_W = 720
_MIN_H = 520
_SIDE_W = 220
_APPROVAL_MAX_ACTIONS_VISIBLE = 8

_BG = "#f3f3f3"
_BG_SIDE = "#eaeaea"
_BG_CHAT = "#ffffff"
_BG_INPUT = "#ffffff"
_BG_TOP_BAR = "#17212b"
_BG_TOP_CHIP = "#233140"
_BG_SIDE_BUTTON_HOVER = "#cfd8dc"
_BG_SIDE_BUTTON_ACTIVE = "#d6e4f2"
_BG_WELCOME = "#f7f9fb"
_BG_WELCOME_CHIP = "#eef3f8"
_BG_APPROVAL_ROW = "#fffaf2"
_BG_INPUT_FOCUS = "#f8fbff"

_BG_USER_MSG = "#e8f0fe"
_BG_ASST_MSG = "#f6f6f6"
_BG_PLAN = "#fff8e1"
_BG_ERROR = "#fde8e8"
_BG_APPROVAL = "#fff3e0"
_BG_RESULT = "#e8f5e9"

_FG = "#1a1a1a"
_FG_DIM = "#777777"
_FG_LABEL = "#444444"
_FG_RISK_MED = "#d84315"
_FG_RISK_LOW = "#2e7d32"

_BTN_APPROVE_BG = "#2e7d32"
_BTN_APPROVE_HOVER_BG = "#1b5e20"
_BTN_CANCEL_BG = "#b71c1c"
_BTN_CANCEL_HOVER_BG = "#8f1212"
_BTN_PRIMARY_BG = "#1565c0"
_BTN_PRIMARY_HOVER_BG = "#0d47a1"
_BTN_SIDE_BG = "#dcdcdc"

_SEP_COLOR = "#c8c8c8"
_TURN_SEP_COLOR = "#e0e0e0"
_APPROVAL_BORDER = "#ef6c00"
_APPROVAL_BORDER_HIGH = "#b71c1c"
_FG_RISK_HIGH = "#b71c1c"
_STATUS_READY = "#2e7d32"
_STATUS_BUSY = "#ef6c00"
_STATUS_INFO = "#1565c0"


# How long the worker thread waits for the user to approve/cancel before
# auto-cancelling. Prevents permanent hangs if the UI becomes unresponsive.
_CONFIRMATION_TIMEOUT_S = 300  # 5 minutes


# ---------------------------------------------------------------------------
# GuiSink — thread-safe OutputSink for Tkinter
# ---------------------------------------------------------------------------


class GuiSink:
    """Posts structured events to the Tkinter main thread."""

    def __init__(self, app: AssistantApp) -> None:
        self._app = app
        self._confirmation_event = threading.Event()
        self._confirmation_answer: str = "cancel"
        # Increments with every plan() call so stale approval-card clicks
        # from a timed-out card are silently ignored.
        self._plan_generation: int = 0
        # Streaming support
        self.stop_event = threading.Event()
        self._is_streaming: bool = False
        self._stream_started: bool = False

    def _post(self, fn: Any, *args: Any) -> None:
        """Schedule *fn* on the main thread, silently dropping if shutting down."""
        if self._app._shutting_down:
            return
        try:
            self._app.root.after(0, fn, *args)
        except RuntimeError:
            pass  # root already destroyed

    def info(self, text: str) -> None:
        self._post(self._app.append_info, text)

    def error(self, text: str) -> None:
        self._post(self._app.append_error, text)

    def assistant(self, text: str) -> None:
        if self._is_streaming and self._stream_started:
            self._is_streaming = False
            self._stream_started = False
            self._post(self._app.finalize_assistant_stream, text)
        else:
            self._is_streaming = False
            self._stream_started = False
            self._post(self._app.append_assistant, text)

    def start_stream(self) -> None:
        """Mark that the next LLM response is beginning (lazy — header deferred to first delta)."""
        self._is_streaming = True
        self._stream_started = False

    def stream_delta(self, text: str) -> None:
        if not self._is_streaming:
            return
        if not self._stream_started:
            self._stream_started = True
            self._post(self._app.start_assistant_stream)
        self._post(self._app.append_stream_delta, text)

    def cancel_stream(self) -> None:
        if self._is_streaming and self._stream_started:
            self._post(self._app.cancel_assistant_stream)
        self._is_streaming = False
        self._stream_started = False

    def stop(self) -> None:
        self.stop_event.set()
        self.cancel_stream()

    def plan(self, card: ApprovalCard) -> None:
        if self._app._shutting_down:
            return
        self._plan_generation += 1
        my_generation = self._plan_generation
        self._confirmation_event.clear()
        self._confirmation_answer = "cancel"
        self._post(self._app.show_approval_card, card, self, my_generation)
        timed_out = not self._confirmation_event.wait(timeout=_CONFIRMATION_TIMEOUT_S)
        if timed_out and not self._app._shutting_down:
            # Dismiss the stale card and cancel; any late button click will
            # be ignored because its generation no longer matches.
            self._post(self._app._dismiss_approval_card)
            self._post(self._app.append_error,
                       "[error] Approval timed out after 5 minutes — action cancelled.")

    def progress(self, text: str) -> None:
        self._post(self._app.append_progress, text)

    def tool_start(self, name: str, label: str) -> None:
        self._post(self._app._set_tool_activity, label)

    def tool_end(self, name: str, ok: bool) -> None:
        self._post(self._app._set_tool_activity, None)

    def usage(self, data: dict, model: str) -> None:
        self._post(self._app._record_usage, data, model)

    def prompt_confirmation(self, prompt_text: str) -> str:
        return self._confirmation_answer

    def resolve_confirmation(self, answer: str, generation: int | None = None) -> None:
        # If a generation is provided, only accept if it matches the current one.
        # None means force-accept (used by _on_close to unblock on shutdown).
        if generation is not None and generation != self._plan_generation:
            return  # stale click from an old timed-out card — ignore
        self._confirmation_answer = answer
        self._confirmation_event.set()


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------


class AssistantApp:
    def _create_root(self) -> tk.Tk:
        if _DND_AVAILABLE:
            try:
                return TkinterDnD.Tk()
            except Exception:
                pass
        return tk.Tk()

    def __init__(self) -> None:
        self.root = self._create_root()
        self.root.title(_APP_TITLE)
        self.root.geometry(f"{_WIN_W}x{_WIN_H}")
        self.root.configure(bg=_BG)
        self.root.minsize(_MIN_W, _MIN_H)

        self._messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
        ]
        self._busy = False
        self._shutting_down = False
        self._sink = GuiSink(self)
        self._stream_mark: str | None = None
        self._streaming: bool = False
        self._welcome_frame: tk.Frame | None = None
        self._session_id: str = SESSION_ID
        self._session_created: str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._session_store = SessionStore(Path(get_state_dir()) / "sessions")
        self._sessions_list_frame: tk.Frame | None = None
        self._session_buttons: list[tk.Widget] = []
        self._token_totals: dict[str, int] = {
            "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
        }
        self._approval_scroll_widgets: list[tk.Widget] = []
        self._find_win: tk.Toplevel | None = None
        self._find_var: tk.StringVar | None = None
        self._find_last_start: str | None = None
        self._find_last_end: str | None = None
        self._find_trace_id: str | None = None  # trace id for query field
        self._find_close_callback: Any = None

        # Fonts
        self._f_ui = ("Segoe UI", 10)
        self._f_ui_bold = ("Segoe UI", 10, "bold")
        self._f_ui_sm = ("Segoe UI", 9)
        self._f_ui_sm_bold = ("Segoe UI", 9, "bold")
        self._f_mono = ("Consolas", 9)
        self._f_mono_sm = ("Consolas", 8)
        self._f_header = ("Segoe UI", 9)
        self._f_approval_title = ("Segoe UI", 12, "bold")
        self._f_approval_btn = ("Segoe UI", 10, "bold")

        self._build_ui()
        self._update_header()
        self._refresh_undo_indicator()
        self._show_welcome()
        self.root.after(100, self._focus_input)
        self.root.after(120, self._sync_input_state)

    # ── Window close ──
    def _on_close(self) -> None:
        """Handle window close: release any blocked worker thread, then destroy."""
        self._shutting_down = True
        # If the worker is blocked waiting for approval, release it
        self._sink.resolve_confirmation("cancel")
        self.root.destroy()

    # ── Shortcuts ──
    def _bind_shortcuts(self) -> None:
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.bind_all("<Escape>", self._on_escape_all)
        self.root.bind_all("<Control-l>", lambda e: self._focus_input())
        self.root.bind_all("<Control-f>", self._on_find_shortcut)
        self.root.bind_all("<Control-End>", lambda e: self._scroll_chat_end())

    def _focus_input(self) -> None:
        if not self._busy:
            self._input.focus_set()

    def _on_escape_all(self, event: tk.Event) -> str | None:
        if self._find_win is not None:
            try:
                if self._find_win.winfo_exists():
                    cb = self._find_close_callback
                    if cb:
                        cb()
                    return "break"
            except tk.TclError:
                pass
        self._focus_input()
        return None

    def _on_find_shortcut(self, event: tk.Event) -> str | None:
        self._open_find_dialog()
        return "break"

    def _on_link_click(self, event: tk.Event) -> None:
        idx = self._chat.index(f"@{event.x},{event.y}")
        ranges = self._chat.tag_ranges("md_link")
        for i in range(0, len(ranges), 2):
            start, end = ranges[i], ranges[i + 1]
            if (self._chat.compare(start, "<=", idx) and
                    self._chat.compare(idx, "<=", end)):
                url_range = self._chat.tag_nextrange("md_url_note", end)
                if url_range:
                    raw = self._chat.get(url_range[0], url_range[1]).strip()
                    if raw.startswith("(") and raw.endswith(")"):
                        webbrowser.open(raw[1:-1])
                break

    # ===================================================================
    # UI CONSTRUCTION
    # ===================================================================

    def _build_ui(self) -> None:
        self._build_header()
        self._build_input_bar()
        self._build_body()
        self._bind_shortcuts()

    # ── Header ──

    def _hover_button(self, button: tk.Button, normal_bg: str, hover_bg: str) -> None:
        self._set_hover_colors(button, normal_bg, hover_bg, apply=False)

        def on_enter(_event: tk.Event) -> None:
            if str(button.cget("state")) != tk.DISABLED:
                button.config(bg=getattr(button, "_hover_bg", hover_bg))

        def on_leave(_event: tk.Event) -> None:
            if str(button.cget("state")) != tk.DISABLED:
                button.config(bg=getattr(button, "_normal_bg", normal_bg))

        button.bind("<Enter>", on_enter)
        button.bind("<Leave>", on_leave)

    def _set_hover_colors(
        self,
        button: tk.Button,
        normal_bg: str,
        hover_bg: str,
        *,
        apply: bool = True,
    ) -> None:
        setattr(button, "_normal_bg", normal_bg)
        setattr(button, "_hover_bg", hover_bg)
        if apply and str(button.cget("state")) != tk.DISABLED:
            button.config(bg=normal_bg)

    def _build_header(self) -> None:
        hdr = tk.Frame(self.root, bg=_BG_TOP_BAR, padx=12, pady=8)
        hdr.pack(fill=tk.X)

        left = tk.Frame(hdr, bg=_BG_TOP_BAR)
        left.pack(side=tk.LEFT)

        tk.Label(
            left, text="xai-computer", font=("Segoe UI", 12, "bold"),
            bg=_BG_TOP_BAR, fg="#ffffff", padx=2,
        ).pack(side=tk.LEFT, padx=(0, 16))

        self._chip_model = tk.Label(left, text="", font=self._f_header,
                                    bg=_BG_TOP_CHIP, fg="#e8edf2", padx=8, pady=2)
        self._chip_model.pack(side=tk.LEFT, padx=(0, 8))

        self._chip_dry = tk.Label(left, text="", font=self._f_header,
                                  bg=_BG_TOP_CHIP, fg="#e8edf2", padx=8, pady=2)
        self._chip_dry.pack(side=tk.LEFT, padx=(0, 8))

        self._chip_mode = tk.Label(left, text="", font=self._f_header,
                                   bg=_BG_TOP_CHIP, fg="#e8edf2", padx=8, pady=2)
        self._chip_mode.pack(side=tk.LEFT, padx=(0, 8))

        self._chip_busy = tk.Label(left, text="", font=("Segoe UI", 9, "bold"),
                                   bg=_BG_TOP_BAR, fg="#ffcc80", padx=2)
        self._chip_busy.pack(side=tk.LEFT)

        self._chip_session = tk.Label(hdr, text="", font=self._f_mono_sm,
                                      bg=_BG_TOP_BAR, fg="#b0bec5")
        self._chip_session.pack(side=tk.RIGHT)

        self._chip_tokens = tk.Label(
            hdr, text="0 tokens", font=self._f_mono_sm,
            bg=_BG_TOP_CHIP, fg="#dce3ea", padx=8, pady=2,
        )
        self._chip_tokens.pack(side=tk.RIGHT, padx=(0, 12))

    # ── Body ──

    def _build_body(self) -> None:
        body = tk.PanedWindow(self.root, orient=tk.HORIZONTAL, bg=_BG,
                              sashwidth=5, sashrelief=tk.FLAT)
        body.pack(fill=tk.BOTH, expand=True, padx=0, pady=0)

        # Left: chat + approval overlay
        self._chat_container = tk.Frame(body, bg=_BG)
        body.add(self._chat_container, stretch="always", minsize=400)

        bar = tk.Frame(self._chat_container, bg=_BG_CHAT, padx=6, pady=4)
        bar.pack(fill=tk.X)
        bf = ("Segoe UI", 9)
        btn_latest = tk.Button(
            bar, text="\u2193 Latest", font=bf, command=self._scroll_chat_end,
            bg=_BG_CHAT, fg=_FG, relief=tk.FLAT, cursor="hand2", padx=6, pady=2,
        )
        btn_latest.pack(side=tk.RIGHT, padx=(4, 0))
        btn_find = tk.Button(
            bar, text="Find\u2026", font=bf, command=self._open_find_dialog,
            bg=_BG_CHAT, fg=_FG, relief=tk.FLAT, cursor="hand2", padx=6, pady=2,
        )
        btn_find.pack(side=tk.RIGHT, padx=(4, 0))
        btn_copy = tk.Button(
            bar, text="Copy all", font=bf, command=self._copy_transcript,
            bg=_BG_CHAT, fg=_FG, relief=tk.FLAT, cursor="hand2", padx=6, pady=2,
        )
        btn_copy.pack(side=tk.RIGHT, padx=(4, 0))
        self._hover_button(btn_latest, _BG_CHAT, "#eef3f8")
        self._hover_button(btn_find, _BG_CHAT, "#eef3f8")
        self._hover_button(btn_copy, _BG_CHAT, "#eef3f8")
        tk.Label(
            bar, text="Transcript", font=("Segoe UI", 9, "bold"),
            bg=_BG_CHAT, fg=_FG_LABEL, anchor=tk.W,
        ).pack(side=tk.LEFT)

        self._chat = scrolledtext.ScrolledText(
            self._chat_container, wrap=tk.WORD, state=tk.DISABLED,
            font=self._f_ui, bg=_BG_CHAT, fg=_FG,
            relief=tk.FLAT, borderwidth=0, padx=12, pady=10,
            cursor="", insertwidth=0,
        )
        self._chat.pack(fill=tk.BOTH, expand=True)
        self._configure_tags()

        # Approval panel skeleton — filled dynamically, packed only when needed
        self._approval_outer = tk.Frame(
            self._chat_container, bg=_APPROVAL_BORDER, padx=2, pady=2,
        )
        self._approval_inner = tk.Frame(self._approval_outer, bg=_BG_APPROVAL,
                                        padx=14, pady=10)
        self._approval_inner.pack(fill=tk.BOTH)
        self._approval_wrap_labels: list[tuple[tk.Label, int]] = []

        # Right: sidebar
        sidebar = tk.Frame(body, bg=_BG_SIDE, width=_SIDE_W)
        body.add(sidebar, stretch="never", minsize=180)
        self._sidebar_widgets: list[tk.Widget] = []
        self._build_sidebar(sidebar)

    def _configure_tags(self) -> None:
        c = self._chat

        # Turn separator
        c.tag_configure("turn_sep", font=("Segoe UI", 2), spacing1=2, spacing3=2,
                        foreground=_TURN_SEP_COLOR)

        # User
        c.tag_configure("user_label", foreground=_BTN_PRIMARY_BG,
                        font=self._f_ui_sm_bold, lmargin1=8, spacing1=10, spacing3=2)
        c.tag_configure("user_msg", background=_BG_USER_MSG, font=self._f_ui,
                        lmargin1=8, lmargin2=8, rmargin=40, spacing1=5, spacing3=8)

        # Assistant
        c.tag_configure("asst_label", foreground=_FG_LABEL,
                        font=self._f_ui_sm_bold, lmargin1=8, spacing1=10, spacing3=2)
        c.tag_configure("asst_msg", background=_BG_ASST_MSG, font=self._f_ui,
                        lmargin1=8, lmargin2=8, rmargin=40, spacing1=5, spacing3=8)

        # Info / progress / error
        c.tag_configure("info", foreground=_FG_DIM, font=self._f_ui_sm,
                        lmargin1=4, spacing1=2, spacing3=2)
        c.tag_configure("error", foreground=_BTN_CANCEL_BG, background=_BG_ERROR,
                        font=self._f_ui_sm, lmargin1=4, lmargin2=4, rmargin=40,
                        spacing1=3, spacing3=3)
        c.tag_configure("progress", foreground=_FG_RISK_LOW, font=self._f_ui_sm,
                        lmargin1=4, spacing1=1, spacing3=1)

        # Result summary block
        c.tag_configure("result_hdr", foreground=_FG_RISK_LOW,
                        font=self._f_ui_sm_bold, lmargin1=4,
                        spacing1=4, spacing3=1)
        c.tag_configure("result_line", foreground=_FG, font=self._f_ui_sm,
                        background=_BG_RESULT, lmargin1=12, lmargin2=12,
                        spacing1=1, spacing3=1)

        # Plan echo in transcript
        c.tag_configure("plan_hdr", foreground=_FG_RISK_MED,
                        font=self._f_ui_sm_bold, lmargin1=4,
                        spacing1=6, spacing3=2)
        c.tag_configure("plan_line", foreground=_FG, font=self._f_mono,
                        background=_BG_PLAN, lmargin1=16, lmargin2=24,
                        spacing1=1, spacing3=1)

        # Undo result
        c.tag_configure("undo_ok", foreground=_FG_RISK_LOW,
                        font=self._f_ui_sm_bold, lmargin1=4,
                        spacing1=4, spacing3=4)
        c.tag_configure("undo_fail", foreground=_BTN_CANCEL_BG,
                        font=self._f_ui_sm, lmargin1=4,
                        spacing1=4, spacing3=4)

        # Markdown (assistant); combine with "asst_msg" at insert time
        c.tag_configure("md_bold", font=self._f_ui_bold)
        c.tag_configure("md_italic", font=("Segoe UI", 10, "italic"))
        c.tag_configure("md_code", font=self._f_mono, background="#eeeeee")
        c.tag_configure(
            "md_codeblock", font=self._f_mono, background="#eceff1",
            foreground=_FG, lmargin1=8, lmargin2=8, spacing1=2, spacing3=4,
        )
        c.tag_configure(
            "md_h1", font=("Segoe UI", 11, "bold"), spacing1=8, spacing3=2,
        )
        c.tag_configure("md_h2", font=self._f_ui_bold, spacing1=6, spacing3=2)
        c.tag_configure("md_h3", font=self._f_ui_sm_bold, spacing1=4, spacing3=1)
        c.tag_configure(
            "md_li", font=self._f_ui, lmargin1=4, lmargin2=18,
        )
        c.tag_configure(
            "md_li_num", font=self._f_ui, lmargin1=4, lmargin2=18,
        )
        c.tag_configure(
            "md_quote", font=self._f_ui, foreground=_FG_LABEL,
            background="#f5f5f5", lmargin1=8, lmargin2=8, spacing1=1, spacing3=1,
        )
        c.tag_configure("md_rule", foreground=_TURN_SEP_COLOR, font=("Segoe UI", 2))
        c.tag_configure("md_link", foreground=_BTN_PRIMARY_BG, underline=True)
        c.tag_bind("md_link", "<Button-1>", self._on_link_click)
        c.tag_bind("md_link", "<Enter>", lambda e: c.config(cursor="hand2"))
        c.tag_bind("md_link", "<Leave>", lambda e: c.config(cursor=""))
        c.tag_configure("md_url_note", font=self._f_mono_sm, foreground=_FG_DIM)
        c.tag_configure("find_hl", background="#fff59d")

    # ── Sidebar ──

    def _build_sidebar(self, parent: tk.Frame) -> None:
        pad_x = 10

        # Model
        tk.Label(
            parent, text="Workspace", font=("Segoe UI", 11, "bold"),
            bg=_BG_SIDE, fg=_FG, anchor=tk.W,
        ).pack(fill=tk.X, padx=10, pady=(14, 2))
        tk.Label(
            parent, text="Local assistant controls", font=self._f_ui_sm,
            bg=_BG_SIDE, fg=_FG_DIM, anchor=tk.W,
        ).pack(fill=tk.X, padx=10, pady=(0, 8))

        self._side_section(parent, "Model")
        model_frame = tk.Frame(parent, bg=_BG_SIDE)
        model_frame.pack(fill=tk.X, padx=pad_x, pady=(0, 8))

        self._model_var = tk.StringVar(value=self._current_preset())
        self._model_radios: list[tk.Radiobutton] = []
        for preset in MODELS:
            rb = tk.Radiobutton(
                model_frame, text=preset.capitalize(), variable=self._model_var,
                value=preset, command=self._on_model_change,
                font=self._f_ui_sm, bg=_BG_SIDE, fg=_FG, anchor=tk.W,
                activebackground=_BG_SIDE, selectcolor=_BG_SIDE,
            )
            rb.pack(fill=tk.X)
            self._model_radios.append(rb)
            self._sidebar_widgets.append(rb)

        # Options
        self._side_sep(parent)
        self._side_section(parent, "Options")

        self._dry_var = tk.BooleanVar(value=is_dry_run())
        self._cb_dry = tk.Checkbutton(
            parent, text="Dry-run mode", variable=self._dry_var,
            command=self._on_dry_toggle, font=self._f_ui_sm,
            bg=_BG_SIDE, fg=_FG, activebackground=_BG_SIDE, selectcolor=_BG_SIDE,
            anchor=tk.W,
        )
        self._cb_dry.pack(fill=tk.X, padx=pad_x, pady=(4, 0))
        self._sidebar_widgets.append(self._cb_dry)

        self._verbose_var = tk.BooleanVar(value=is_verbose())
        self._cb_verbose = tk.Checkbutton(
            parent, text="Verbose output", variable=self._verbose_var,
            command=self._on_verbose_toggle, font=self._f_ui_sm,
            bg=_BG_SIDE, fg=_FG, activebackground=_BG_SIDE, selectcolor=_BG_SIDE,
            anchor=tk.W,
        )
        self._cb_verbose.pack(fill=tk.X, padx=pad_x, pady=(4, 0))
        self._sidebar_widgets.append(self._cb_verbose)

        # Actions
        self._side_sep(parent)
        self._side_section(parent, "Actions")

        self._btn_undo = self._side_button(parent, "Undo Last", self._on_undo)
        self._sidebar_widgets.append(self._btn_undo)
        self._btn_history = self._side_button(parent, "Show History", self._on_history)
        self._sidebar_widgets.append(self._btn_history)
        btn_clear = self._side_button(parent, "New Session", self._on_clear)
        self._sidebar_widgets.append(btn_clear)
        btn_folders = self._side_button(parent, "Allowed Folders...", self._on_allowed_folders)
        self._sidebar_widgets.append(btn_folders)
        btn_logs = self._side_button(parent, "Open Logs Folder", self._on_open_logs)
        self._sidebar_widgets.append(btn_logs)

        # Recent Sessions
        self._side_sep(parent)
        self._side_section(parent, "Recent Sessions")
        self._sessions_list_frame = tk.Frame(parent, bg=_BG_SIDE)
        self._sessions_list_frame.pack(fill=tk.X, padx=pad_x, pady=(0, 4))
        self._refresh_session_list()

        # Undo indicator
        self._side_sep(parent)
        self._side_section(parent, "Last Action")
        self._undo_indicator = tk.Label(
            parent, text="", font=self._f_mono_sm, bg=_BG_SIDE, fg=_FG_DIM,
            anchor=tk.NW, justify=tk.LEFT, wraplength=_SIDE_W - 24, padx=2,
        )
        self._undo_indicator.pack(fill=tk.X, padx=pad_x, pady=(0, 4))

        # Status info
        self._side_sep(parent)
        self._status_label = tk.Label(
            parent, text="", font=self._f_mono_sm, bg=_BG_SIDE, fg=_FG_DIM,
            anchor=tk.NW, justify=tk.LEFT, wraplength=_SIDE_W - 24, padx=2,
        )
        self._status_label.pack(fill=tk.X, padx=pad_x, pady=(4, 8))

    def _side_section(self, parent: tk.Frame, title: str, top_pad: int = 0) -> None:
        tk.Label(parent, text=title.upper(), font=("Segoe UI", 8, "bold"), bg=_BG_SIDE,
                 fg=_FG_DIM, anchor=tk.W).pack(fill=tk.X, padx=10, pady=(top_pad, 4))

    def _side_sep(self, parent: tk.Frame) -> None:
        tk.Frame(parent, bg=_SEP_COLOR, height=1).pack(fill=tk.X, padx=8, pady=10)

    def _side_button(self, parent: tk.Frame, text: str, cmd: Any) -> tk.Button:
        btn = tk.Button(
            parent, text=text, command=cmd, font=self._f_ui_sm,
            bg=_BTN_SIDE_BG, fg=_FG, activebackground="#cfcfcf",
            relief=tk.FLAT, padx=10, pady=5, cursor="hand2", anchor=tk.W,
        )
        btn.pack(fill=tk.X, padx=10, pady=2)
        self._hover_button(btn, _BTN_SIDE_BG, _BG_SIDE_BUTTON_HOVER)
        return btn

    # ── Input bar ──

    def _build_input_bar(self) -> None:
        composer = tk.Frame(self.root, bg=_BG)
        composer.pack(fill=tk.X, side=tk.BOTTOM)

        tk.Frame(composer, bg=_SEP_COLOR, height=1).pack(fill=tk.X)

        status_row = tk.Frame(composer, bg=_BG, padx=10)
        status_row.pack(fill=tk.X, pady=(6, 0))
        self._input_status = tk.Label(
            status_row, text="Ready", font=self._f_ui_sm,
            bg=_BG, fg=_STATUS_READY, anchor=tk.W,
        )
        self._input_status.pack(side=tk.LEFT)
        self._input_count = tk.Label(
            status_row, text="0 chars", font=self._f_mono_sm,
            bg=_BG, fg=_FG_DIM, anchor=tk.E,
        )
        self._input_count.pack(side=tk.RIGHT)

        bar = tk.Frame(composer, bg=_BG, padx=10)
        bar.pack(fill=tk.X, pady=(4, 8))

        self._input = tk.Text(
            bar, height=_INPUT_MIN_LINES, font=self._f_ui, bg=_BG_INPUT, fg=_FG,
            relief=tk.SOLID, borderwidth=1, padx=8, pady=6, wrap=tk.WORD,
            insertbackground=_FG, highlightthickness=1,
            highlightbackground="#bcc7d1", highlightcolor=_BTN_PRIMARY_BG,
        )
        self._input.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 8))
        self._input.bind("<Return>", self._on_enter)
        self._input.bind("<Shift-Return>", lambda e: None)
        self._input.bind("<Control-Return>", self._on_ctrl_enter)
        self._input.bind("<Control-KP_Enter>", self._on_ctrl_enter)
        self._input.bind("<KeyRelease>", lambda e: self._sync_input_state())
        self._input.bind("<FocusIn>", lambda e: self._input.config(bg=_BG_INPUT_FOCUS))
        self._input.bind("<FocusOut>", lambda e: self._input.config(bg=_BG_INPUT))
        self._input.bind("<<Paste>>", lambda e: self.root.after(1, self._sync_input_state))
        for seq in ("<ButtonRelease-1>", "<<Cut>>"):
            self._input.bind(seq, lambda e: self.root.after(1, self._sync_input_state))

        if _DND_AVAILABLE:
            try:
                self._input.drop_target_register(DND_FILES)
                self._input.dnd_bind("<<Drop>>", self._on_drop_files)
            except tk.TclError:
                pass

        btn_col = tk.Frame(bar, bg=_BG)
        btn_col.pack(side=tk.RIGHT, fill=tk.Y)

        self._btn_send = tk.Button(
            btn_col, text="Send", font=self._f_approval_btn, width=8,
            command=self._on_send, bg=_BTN_PRIMARY_BG, fg="white",
            activebackground="#1976d2", activeforeground="white",
            relief=tk.FLAT, cursor="hand2",
        )
        self._btn_send.pack(fill=tk.X, pady=(0, 2))
        self._hover_button(self._btn_send, _BTN_PRIMARY_BG, _BTN_PRIMARY_HOVER_BG)

        tk.Label(
            btn_col, text="Enter sends\nShift+Enter newline",
            font=("Segoe UI", 7), bg=_BG, fg=_FG_DIM, justify=tk.CENTER,
        ).pack()
        self._sync_input_state()

    # ===================================================================
    # HEADER / STATUS
    # ===================================================================

    def _current_preset(self) -> str:
        model = get_xai_model()
        for preset, mid in MODELS.items():
            if model == mid:
                return preset
        return "fast"

    def _update_header(self) -> None:
        model = get_xai_model()
        short = model
        for preset, mid in MODELS.items():
            if model == mid:
                short = f"{preset} ({mid})"
                break
        self._chip_model.config(text=f"Model: {short}")

        if is_dry_run():
            self._chip_dry.config(text="Dry run", fg="#ffcc80",
                                  bg="#3c2f1f", font=("Segoe UI", 9, "bold"))
        else:
            self._chip_dry.config(text="Live", fg="#c8e6c9",
                                  bg="#1f3a2a", font=self._f_header)

        mode = "Verbose" if is_verbose() else "Concise"
        self._chip_mode.config(text=mode)
        self._chip_session.config(text=f"Session {self._session_id}")

        info = get_startup_info()
        roots_short = [os.path.basename(r) for r in info["allowed_roots"]]
        self._status_label.config(
            text=f"Desktop: {info['desktop']}\n"
                 f"Roots: {', '.join(roots_short)}\n"
                 f"Max loops: {info['max_tool_loops']}"
        )

    def _refresh_undo_indicator(self) -> None:
        """Update the sidebar's last-action indicator."""
        records = get_history(1)
        if not records:
            self._undo_indicator.config(text="No actions yet", fg=_FG_DIM)
            return
        r = records[0]
        action = r.get("action", "?")
        undone = r.get("undone", False)
        if undone:
            self._undo_indicator.config(text="(last action undone)", fg=_FG_DIM)
            return
        # Show a short summary of the undoable action
        if action in ("move_file", "rename_file", "organize_move"):
            src_name = os.path.basename(r.get("source", "?"))
            self._undo_indicator.config(
                text=f"{action}: {src_name}\nUndo available",
                fg=_FG_RISK_LOW,
            )
        elif action == "create_folder":
            folder_name = os.path.basename(r.get("path", "?"))
            self._undo_indicator.config(
                text=f"Created: {folder_name}\nUndo available",
                fg=_FG_RISK_LOW,
            )
        else:
            self._undo_indicator.config(text=f"{action}", fg=_FG_DIM)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        if busy:
            self._set_hover_colors(
                self._btn_send, _BTN_CANCEL_BG, _BTN_CANCEL_HOVER_BG, apply=False,
            )
            self._btn_send.config(state=tk.NORMAL, text="\u25a0 Stop",
                                  bg=_BTN_CANCEL_BG, command=self._on_stop)
            self._chip_busy.config(text="Working...")
            self._set_input_status("Working", _STATUS_BUSY)
            # Input stays enabled so the user can compose their next message.
            for w in self._sidebar_widgets + self._session_buttons:
                try:
                    w.config(state=tk.DISABLED)
                except tk.TclError:
                    pass
        else:
            self._sink.stop_event.clear()
            self._set_hover_colors(
                self._btn_send, _BTN_PRIMARY_BG, _BTN_PRIMARY_HOVER_BG, apply=False,
            )
            self._btn_send.config(state=tk.NORMAL, text="Send",
                                  bg=_BTN_PRIMARY_BG, command=self._on_send)
            self._chip_busy.config(text="")
            self._input.config(state=tk.NORMAL, bg=_BG_INPUT)
            self._sync_input_state()
            for w in self._sidebar_widgets + self._session_buttons:
                try:
                    w.config(state=tk.NORMAL)
                except tk.TclError:
                    pass
            self._refresh_undo_indicator()
            self._focus_input()

    def _on_stop(self) -> None:
        self._sink.stop()
        self._set_hover_colors(self._btn_send, "#90a4ae", "#90a4ae", apply=False)
        self._btn_send.config(state=tk.DISABLED, text="Stopping...", bg="#90a4ae")
        self._set_input_status("Stopping current response", _STATUS_BUSY)

    def _record_usage(self, data: dict, model: str) -> None:
        for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
            self._token_totals[k] += int(data.get(k) or 0)
        self._update_token_chip()

    def _update_token_chip(self) -> None:
        total = self._token_totals.get("total_tokens", 0)
        if total >= 1_000_000:
            label = f"{total / 1_000_000:.2f}M tokens"
        elif total >= 1_000:
            label = f"{total / 1_000:.1f}k tokens"
        else:
            label = f"{total} tokens"
        self._chip_tokens.config(text=label)

    def _set_tool_activity(self, label: str | None) -> None:
        if not self._busy:
            return
        if label:
            truncated = label if len(label) <= 60 else label[:57] + "..."
            self._chip_busy.config(text=f"\u21b3 {truncated}")
            self._set_input_status(truncated, _STATUS_BUSY)
        else:
            self._chip_busy.config(text="Working...")
            self._set_input_status("Working", _STATUS_BUSY)

    def _set_input_status(self, text: str, color: str = _STATUS_INFO) -> None:
        if hasattr(self, "_input_status"):
            self._input_status.config(text=text, fg=color)

    # ===================================================================
    # CHAT TRANSCRIPT
    # ===================================================================

    def _insert(self, text: str, *tags: str) -> None:
        self._chat.config(state=tk.NORMAL)
        self._chat.insert(tk.END, text, tags)
        self._chat.config(state=tk.DISABLED)
        self._chat.see(tk.END)

    def _insert_turn_separator(self) -> None:
        """Visual divider between conversation turns."""
        self._chat.config(state=tk.NORMAL)
        self._chat.insert(tk.END, "\u2500" * 50 + "\n", "turn_sep")
        self._chat.config(state=tk.DISABLED)

    _EXAMPLE_PROMPTS = [
        "List what's on my Desktop",
        "Find files larger than 100 MB in Downloads",
        "Organize my Desktop by file type",
        "Show me my 10 most recent Downloads",
        "Take a screenshot of my screen",
    ]

    def _show_welcome(self) -> None:
        # Dismiss any existing welcome frame first (idempotent).
        self._hide_welcome()

        info = get_startup_info()
        dry = "  [DRY RUN active]" if info["dry_run"] else ""
        roots = ", ".join(os.path.basename(r) for r in info["allowed_roots"])

        frame = tk.Frame(self._chat_container, bg=_BG_WELCOME, padx=20, pady=18)
        frame.pack(fill=tk.X, before=self._chat)

        tk.Label(
            frame, text=f"Welcome to xai-computer{dry}",
            font=("Segoe UI", 14, "bold"),
            bg=_BG_WELCOME, fg=_FG, anchor=tk.W,
        ).pack(fill=tk.X, pady=(0, 6))

        tk.Label(
            frame,
            text=(
                f"Desktop: {info['desktop']}    "
                f"Roots: {roots}    "
                f"Model: {info['model']}"
            ),
            font=self._f_mono_sm, bg=_BG_WELCOME, fg=_FG_DIM, anchor=tk.W,
            justify=tk.LEFT, wraplength=800,
        ).pack(fill=tk.X, pady=(0, 12))

        tk.Label(
            frame, text="Quick starts",
            font=self._f_ui_sm_bold, bg=_BG_WELCOME, fg=_FG_LABEL, anchor=tk.W,
        ).pack(fill=tk.X, pady=(0, 6))

        chips = tk.Frame(frame, bg=_BG_WELCOME)
        chips.pack(fill=tk.X)
        for idx, prompt in enumerate(self._EXAMPLE_PROMPTS):
            btn = tk.Button(
                chips, text=prompt, font=self._f_ui_sm,
                bg=_BG_WELCOME_CHIP, fg=_FG, activebackground="#dbe8f5",
                relief=tk.FLAT, padx=10, pady=5, cursor="hand2",
                anchor=tk.W, justify=tk.LEFT, wraplength=240,
                command=lambda p=prompt: self._use_prompt(p),
            )
            btn.grid(row=idx // 2, column=idx % 2, sticky="ew", padx=(0, 6), pady=2)
            self._hover_button(btn, _BG_WELCOME_CHIP, "#dbe8f5")
        chips.grid_columnconfigure(0, weight=1)
        chips.grid_columnconfigure(1, weight=1)

        tk.Label(
            frame,
            text="Type below when you need something specific. Mutating work still uses approval and undo.",
            font=self._f_ui_sm, bg=_BG_WELCOME, fg=_FG_DIM, anchor=tk.W,
            justify=tk.LEFT, wraplength=800,
        ).pack(fill=tk.X, pady=(12, 0))

        self._welcome_frame = frame

    def _hide_welcome(self) -> None:
        if self._welcome_frame is not None:
            try:
                self._welcome_frame.destroy()
            except tk.TclError:
                pass
            self._welcome_frame = None

    def _on_drop_files(self, event: Any) -> str:
        raw = event.data or ""
        try:
            # Use Tk's own list splitter — it handles {braced paths} correctly.
            paths = list(self.root.tk.splitlist(raw))
        except tk.TclError:
            paths = [raw]
        quoted = []
        for p in paths:
            p = p.strip().strip("{}")
            if not p:
                continue
            quoted.append(f'"{p}"' if (" " in p or "\t" in p) else p)
        if not quoted:
            return ""
        snippet = " ".join(quoted)
        current = self._input.get("1.0", tk.END).rstrip()
        sep = " " if current and not current.endswith((" ", "\n")) else ""
        self._input.insert(tk.END, f"{sep}{snippet}")
        self._sync_input_state()
        self._focus_input()
        return "break"

    def _use_prompt(self, text: str) -> None:
        if self._busy:
            return
        self._input.delete("1.0", tk.END)
        self._input.insert("1.0", text)
        self._on_send()

    def append_user(self, text: str) -> None:
        self._hide_welcome()
        self._insert_turn_separator()
        self._insert("You\n", "user_label")
        self._insert(text + "\n\n", "user_msg")

    def append_assistant(self, text: str) -> None:
        self._chat.config(state=tk.NORMAL)
        self._chat.insert(tk.END, "Assistant\n", ("asst_label",))
        insert_markdown(self._chat, text, base_tags=("asst_msg",), trailing="\n\n")
        self._chat.config(state=tk.DISABLED)
        self._chat.see(tk.END)

    def start_assistant_stream(self) -> None:
        """Insert the 'Assistant' label and record the stream start mark."""
        self._chat.config(state=tk.NORMAL)
        self._chat.insert(tk.END, "Assistant\n", ("asst_label",))
        # Text.insert(END, ...) inserts before Tk's implicit trailing newline,
        # while END points after it. Mark end-1c so finalize/cancel removes
        # the raw streamed text instead of leaving it before the mark.
        self._stream_mark = self._chat.index("end-1c")
        self._streaming = True
        self._chat.config(state=tk.DISABLED)
        self._chat.see(tk.END)

    def append_stream_delta(self, text: str) -> None:
        """Append a raw token chunk while streaming."""
        if not self._streaming:
            return
        self._chat.config(state=tk.NORMAL)
        self._chat.insert(tk.END, text, ("asst_msg",))
        self._chat.config(state=tk.DISABLED)
        self._chat.see(tk.END)

    def finalize_assistant_stream(self, full_text: str) -> None:
        """Replace the raw streamed text with fully markdown-rendered version."""
        if not self._streaming or not self._stream_mark:
            self.append_assistant(full_text)
            return
        self._chat.config(state=tk.NORMAL)
        self._chat.delete(self._stream_mark, "end-1c")
        insert_markdown(self._chat, full_text, base_tags=("asst_msg",), trailing="\n\n")
        self._chat.config(state=tk.DISABLED)
        self._chat.see(tk.END)
        self._streaming = False
        self._stream_mark = None

    def cancel_assistant_stream(self) -> None:
        """Terminate a stream that was interrupted (Stop button or empty tool preamble)."""
        if not self._streaming:
            return
        self._chat.config(state=tk.NORMAL)
        if self._stream_mark:
            raw = self._chat.get(self._stream_mark, "end-1c")
            self._chat.delete(self._stream_mark, "end-1c")
            if raw.strip():
                insert_markdown(self._chat, raw.rstrip(), base_tags=("asst_msg",), trailing="")
            self._chat.insert(tk.END, "\n[stopped]\n\n", ("info",))
        self._chat.config(state=tk.DISABLED)
        self._chat.see(tk.END)
        self._streaming = False
        self._stream_mark = None

    def append_info(self, text: str) -> None:
        # Detect result summaries from core.py ("Completed N/M operation(s).")
        if text.startswith("Completed ") and "operation(s)" in text:
            self._insert_result_summary(text)
            return
        self._insert(text + "\n", "info")

    def append_error(self, text: str) -> None:
        self._insert(text + "\n", "error")

    def append_progress(self, text: str) -> None:
        self._insert(text + "\n", "progress")

    def _insert_result_summary(self, summary_text: str) -> None:
        """Render the post-execution summary in a distinct result block."""
        dry = " (dry run)" if is_dry_run() else ""

        self._insert(f"Result{dry}\n", "result_hdr")
        self._insert(f"  {summary_text}\n", "result_line")
        self._insert("\n", "info")

    # ===================================================================
    # APPROVAL PANEL
    # ===================================================================

    def _track_approval_wrap(self, label: tk.Label, margin: int = 32) -> None:
        self._approval_wrap_labels.append((label, margin))

    def _sync_approval_wraps(self, event: tk.Event | None = None) -> None:
        width = int(getattr(event, "width", 0) or self._approval_inner.winfo_width())
        if width <= 1:
            return
        for label, margin in self._approval_wrap_labels:
            try:
                label.config(wraplength=max(180, width - margin))
            except tk.TclError:
                pass

    def show_approval_card(self, card: ApprovalCard, sink: GuiSink, generation: int = 0) -> None:
        inner = self._approval_inner
        for w in inner.winfo_children():
            w.destroy()
        self._approval_wrap_labels.clear()

        dry_tag = "  [DRY RUN]" if card.dry_run else ""
        is_high = card.risk_level == "high"
        is_medium = card.risk_level == "medium"
        n_actions = len(card.actions)
        needs_scroll = n_actions > _APPROVAL_MAX_ACTIONS_VISIBLE

        # Set border color by risk level
        border_color = _APPROVAL_BORDER_HIGH if is_high else _APPROVAL_BORDER
        self._approval_outer.config(bg=border_color)

        def _cleanup_and_resolve(answer: str) -> None:
            for w in self._approval_scroll_widgets:
                try:
                    w.unbind("<MouseWheel>")
                except tk.TclError:
                    pass
            self._approval_scroll_widgets.clear()
            self._resolve_approval(answer, sink, generation)

        # Title row
        title_frame = tk.Frame(inner, bg=_BG_APPROVAL)
        title_frame.pack(fill=tk.X)

        tk.Label(
            title_frame, text=f"APPROVAL REQUIRED{dry_tag}",
            font=self._f_approval_title, bg=_BG_APPROVAL, fg=_FG,
        ).pack(side=tk.LEFT)

        if is_high:
            risk_fg = _FG_RISK_HIGH
            risk_bg = "#ffebee"
            risk_text = "HIGH"
        elif is_medium:
            risk_fg = _FG_RISK_MED
            risk_bg = "#fff3e0"
            risk_text = "MEDIUM"
        else:
            risk_fg = _FG_RISK_LOW
            risk_bg = "#e8f5e9"
            risk_text = "LOW"
        tk.Label(
            title_frame, text=f"Risk: {risk_text}",
            font=self._f_ui_sm_bold, bg=risk_bg, fg=risk_fg, padx=8, pady=2,
        ).pack(side=tk.RIGHT)

        # Buttons live near the top so they remain visible even when action
        # details wrap or the window is short/narrow.
        btn_frame = tk.Frame(inner, bg=_BG_APPROVAL)
        btn_frame.pack(fill=tk.X, pady=(8, 6))
        btn_frame.grid_columnconfigure(0, weight=1, uniform="approval_btns")
        btn_frame.grid_columnconfigure(1, weight=1, uniform="approval_btns")

        btn_approve = tk.Button(
            btn_frame, text="Approve", font=self._f_approval_btn,
            bg=_BTN_APPROVE_BG, fg="white",
            activebackground="#388e3c", activeforeground="white",
            relief=tk.FLAT, cursor="hand2", padx=10, pady=5,
            command=lambda: _cleanup_and_resolve("yes"),
        )
        btn_approve.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self._hover_button(btn_approve, _BTN_APPROVE_BG, _BTN_APPROVE_HOVER_BG)

        btn_cancel = tk.Button(
            btn_frame, text="Cancel", font=self._f_ui_sm_bold,
            bg=_BTN_CANCEL_BG, fg="white",
            activebackground="#c62828", activeforeground="white",
            relief=tk.FLAT, cursor="hand2", padx=10, pady=5,
            command=lambda: _cleanup_and_resolve("cancel"),
        )
        btn_cancel.grid(row=0, column=1, sticky="ew", padx=(4, 0))
        self._hover_button(btn_cancel, _BTN_CANCEL_BG, _BTN_CANCEL_HOVER_BG)

        count_text = f"{n_actions} action(s)"
        if needs_scroll:
            count_text += " (scroll to see all)"
        tk.Label(
            btn_frame, text=count_text, font=self._f_ui_sm,
            bg=_BG_APPROVAL, fg=_FG_DIM, anchor=tk.W,
        ).grid(row=1, column=0, columnspan=2, sticky="ew", pady=(5, 0))

        # Extra warnings for shell commands
        has_shell = any(a.tool_name == "run_command" for a in card.actions)
        if has_shell:
            warn_frame = tk.Frame(inner, bg=_BG_APPROVAL)
            warn_frame.pack(fill=tk.X, pady=(2, 0))
            tk.Label(
                warn_frame,
                text="Shell command — not undoable. Output redacted for secrets.",
                font=self._f_ui_sm, bg=_BG_APPROVAL, fg=risk_fg,
            ).pack(anchor=tk.W)

            # Structured shell explanation (from xai_structured.py if available)
            if card.shell_explanation:
                exp = card.shell_explanation
                exp_frame = tk.Frame(inner, bg=_BG_APPROVAL)
                exp_frame.pack(fill=tk.X, pady=(4, 0))
                for label, key in [("Does", "what_it_does"),
                                   ("Side effects", "side_effects"),
                                   ("Risk", "risk_reason")]:
                    val = exp.get(key, "")
                    if val:
                        exp_label = tk.Label(
                            exp_frame,
                            text=f"  {label}: {val}",
                            font=self._f_ui_sm, bg=_BG_APPROVAL, fg=_FG_LABEL,
                            anchor=tk.W, justify=tk.LEFT,
                        )
                        exp_label.pack(fill=tk.X, anchor=tk.W)
                        self._track_approval_wrap(exp_label, margin=44)

        # Scope + summary
        meta_parts: list[str] = []
        if card.affected_root:
            meta_parts.append(f"Scope: {card.affected_root}")
        meta_parts.append(card.summary)
        meta_label = tk.Label(
            inner, text="  |  ".join(meta_parts), font=self._f_ui_sm,
            bg=_BG_APPROVAL, fg=_FG_DIM, anchor=tk.W,
            justify=tk.LEFT,
        )
        meta_label.pack(fill=tk.X, pady=(2, 6))
        self._track_approval_wrap(meta_label, margin=32)

        # Divider
        tk.Frame(inner, bg="#e0c080", height=1).pack(fill=tk.X, pady=(0, 4))

        # Action list — scrollable if large
        # Track scroll binding so it can be cleaned up reliably
        self._approval_scroll_widgets: list[tk.Widget] = []

        if needs_scroll:
            list_container = tk.Frame(inner, bg=_BG_APPROVAL)
            list_container.pack(fill=tk.X, pady=2)

            canvas = tk.Canvas(list_container, bg=_BG_APPROVAL,
                               highlightthickness=0, height=160)
            scrollbar = tk.Scrollbar(list_container, orient=tk.VERTICAL,
                                     command=canvas.yview)
            actions_frame = tk.Frame(canvas, bg=_BG_APPROVAL)

            actions_frame.bind(
                "<Configure>",
                lambda e: canvas.configure(scrollregion=canvas.bbox("all")),
            )
            canvas_window = canvas.create_window((0, 0), window=actions_frame, anchor=tk.NW)
            canvas.configure(yscrollcommand=scrollbar.set)
            canvas.bind(
                "<Configure>",
                lambda e, c=canvas, w=canvas_window: c.itemconfigure(w, width=e.width),
            )

            canvas.pack(side=tk.LEFT, fill=tk.X, expand=True)
            scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

            # Mouse wheel — bind to canvas, frame, and all child widgets
            def _on_mousewheel(event: tk.Event) -> None:
                canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
            canvas.bind("<MouseWheel>", _on_mousewheel)
            actions_frame.bind("<MouseWheel>", _on_mousewheel)
            self._approval_scroll_widgets = [canvas, actions_frame]
        else:
            _on_mousewheel = None
            actions_frame = tk.Frame(inner, bg=_BG_APPROVAL)
            actions_frame.pack(fill=tk.X, pady=2)

        for action in card.actions:
            row = tk.Frame(actions_frame, bg=_BG_APPROVAL_ROW, padx=6, pady=4)
            row.pack(fill=tk.X, pady=1)

            if action.risk == "high":
                marker_fg = _FG_RISK_HIGH
                marker = " [!!]"
            elif action.risk == "medium":
                marker_fg = _FG_RISK_MED
                marker = " [!]"
            else:
                marker_fg = _FG
                marker = ""

            num_lbl = tk.Label(
                row, text=f"{action.index}.", font=self._f_ui_sm_bold,
                bg=_BG_APPROVAL_ROW, fg=_FG_DIM, width=3, anchor=tk.E,
            )
            num_lbl.pack(side=tk.LEFT, padx=(0, 4))
            act_lbl = tk.Label(
                row, text=f"{action.label}{marker}", font=self._f_mono,
                bg=_BG_APPROVAL_ROW, fg=marker_fg, anchor=tk.W,
                justify=tk.LEFT,
            )
            act_lbl.pack(side=tk.LEFT, fill=tk.X, expand=True)
            self._track_approval_wrap(act_lbl, margin=80)

            # Propagate scroll to all child widgets in the scrollable case
            if _on_mousewheel is not None:
                for w in (row, num_lbl, act_lbl):
                    w.bind("<MouseWheel>", _on_mousewheel)
                    self._approval_scroll_widgets.append(w)

        # Show panel
        self._approval_outer.pack(
            fill=tk.X, side=tk.BOTTOM, padx=6, pady=(0, 2), before=self._chat,
        )
        inner.bind("<Configure>", self._sync_approval_wraps)
        self.root.after(1, self._sync_approval_wraps)
        self._set_input_status("Approval needed", _STATUS_BUSY)

        # Echo to transcript
        self._insert("Approval requested\n", "plan_hdr")
        for action in card.actions:
            if action.risk == "high":
                risk_mark = " [!!]"
            elif action.risk == "medium":
                risk_mark = " [!]"
            else:
                risk_mark = ""
            self._insert(f"  {action.index}. {action.label}{risk_mark}\n", "plan_line")

    def _resolve_approval(self, answer: str, sink: GuiSink, generation: int = 0) -> None:
        # Clean up any scroll bindings (safety net)
        for w in self._approval_scroll_widgets:
            try:
                w.unbind("<MouseWheel>")
            except tk.TclError:
                pass
        self._approval_scroll_widgets.clear()
        self._approval_wrap_labels.clear()

        self._approval_outer.pack_forget()
        if answer == "yes":
            self._insert("[Approved]\n", "progress")
        else:
            self._insert("[Cancelled]\n\n", "info")
        sink.resolve_confirmation(answer, generation)

    def _dismiss_approval_card(self) -> None:
        """Hide the approval card after a timeout and clean up its bindings.

        Called by GuiSink.plan() when the 5-minute confirmation timeout fires.
        Any subsequent button click on the (now hidden) card will be ignored
        because its generation no longer matches the current one in GuiSink.
        """
        for w in self._approval_scroll_widgets:
            try:
                w.unbind("<MouseWheel>")
            except tk.TclError:
                pass
        self._approval_scroll_widgets.clear()
        self._approval_wrap_labels.clear()
        try:
            self._approval_outer.pack_forget()
        except tk.TclError:
            pass
        self._insert("[Timed out — cancelled]\n\n", "info")

    # ===================================================================
    # TRANSCRIPT TOOLS
    # ===================================================================

    def _scroll_chat_end(self) -> None:
        self._chat.see(tk.END)

    def _copy_transcript(self) -> None:
        try:
            t = self._chat.get("1.0", tk.END).strip()
        except tk.TclError:
            return
        if not t:
            self._set_input_status("Nothing to copy", _STATUS_INFO)
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(t)
        self._set_input_status("Transcript copied", _STATUS_INFO)

    def _find_clear_highlight(self) -> None:
        self._chat.config(state=tk.NORMAL)
        self._chat.tag_remove("find_hl", "1.0", tk.END)
        self._chat.config(state=tk.DISABLED)

    def _find_highlight(self, start: str, end: str) -> None:
        self._chat.config(state=tk.NORMAL)
        self._chat.tag_remove("find_hl", "1.0", tk.END)
        self._chat.tag_add("find_hl", start, end)
        self._chat.config(state=tk.DISABLED)
        self._chat.see(start)

    def _find_next(self, backwards: bool = False) -> None:
        if not self._find_var:
            return
        q = self._find_var.get().strip()
        if not q:
            return
        idx = ""
        if backwards:
            if self._find_last_start:
                try:
                    start_from = self._chat.index(f"{self._find_last_start}-1c")
                except tk.TclError:
                    start_from = tk.END
            else:
                start_from = tk.END
            idx = self._chat.search(q, start_from, "1.0", backwards=True, nocase=True)
            if not idx and self._find_last_start:
                idx = self._chat.search(q, tk.END, "1.0", backwards=True, nocase=True)
        else:
            start_from = self._find_last_end if self._find_last_end else "1.0"
            idx = self._chat.search(q, start_from, tk.END, nocase=True)
            if not idx and self._find_last_start:
                idx = self._chat.search(q, "1.0", self._find_last_start, nocase=True)
        if not idx:
            if hasattr(self, "_find_status"):
                self._find_status.config(text="Not found")
            return
        end = f"{idx}+{len(q)}c"
        self._find_highlight(idx, end)
        self._find_last_start = idx
        self._find_last_end = end
        if hasattr(self, "_find_status"):
            self._find_status.config(text="")

    def _open_find_dialog(self) -> None:
        if self._find_win is not None:
            try:
                if self._find_win.winfo_exists():
                    self._find_win.lift()
                    self._find_win.focus_force()
                    return
            except tk.TclError:
                pass
        self._find_last_start = None
        self._find_last_end = None
        win = tk.Toplevel(self.root)
        win.title("Find in transcript")
        win.transient(self.root)
        win.configure(bg=_BG)
        win.resizable(False, False)
        self._find_win = win
        self._find_var = tk.StringVar()

        row = tk.Frame(win, bg=_BG, padx=10, pady=8)
        row.pack(fill=tk.X)
        tk.Label(row, text="Find:", font=self._f_ui_sm, bg=_BG, fg=_FG).pack(side=tk.LEFT)
        ent = tk.Entry(
            row, textvariable=self._find_var, width=36, font=self._f_ui,
            relief=tk.SOLID, borderwidth=1,
        )
        ent.pack(side=tk.LEFT, padx=(6, 0), fill=tk.X, expand=True)

        self._find_status = tk.Label(
            win, text="", font=self._f_ui_sm, bg=_BG, fg=_FG_RISK_MED,
        )
        self._find_status.pack(anchor=tk.W, padx=10)

        btn_row = tk.Frame(win, bg=_BG, padx=10)
        btn_row.pack(fill=tk.X, pady=(0, 10))

        def do_next() -> None:
            self._find_next(backwards=False)

        def do_prev() -> None:
            self._find_next(backwards=True)

        def on_close() -> None:
            if self._find_var is not None and self._find_trace_id is not None:
                try:
                    self._find_var.trace_remove("write", self._find_trace_id)
                except (tk.TclError, AttributeError, ValueError):
                    pass
            self._find_trace_id = None
            self._find_clear_highlight()
            self._find_last_start = None
            self._find_last_end = None
            self._find_win = None
            self._find_var = None
            self._find_close_callback = None
            try:
                win.destroy()
            except tk.TclError:
                pass

        self._find_close_callback = on_close

        for text, command in (
            ("Next", do_next),
            ("Previous", do_prev),
            ("Close", on_close),
        ):
            btn = tk.Button(
                btn_row, text=text, command=command, width=10,
                bg=_BTN_SIDE_BG, fg=_FG, activebackground=_BG_SIDE_BUTTON_HOVER,
                relief=tk.FLAT, cursor="hand2",
            )
            btn.pack(side=tk.LEFT, padx=(0, 6))
            self._hover_button(btn, _BTN_SIDE_BG, _BG_SIDE_BUTTON_HOVER)

        win.protocol("WM_DELETE_WINDOW", on_close)
        win.bind("<Escape>", lambda e: on_close())
        ent.bind("<Return>", lambda e: do_next())
        ent.bind("<Shift-Return>", lambda e: do_prev())

        def on_query_change(*_a: Any) -> None:
            self._find_last_start = None
            self._find_last_end = None
            self._find_clear_highlight()
            if hasattr(self, "_find_status"):
                self._find_status.config(text="")

        self._find_trace_id = self._find_var.trace_add("write", on_query_change)
        win.after(50, ent.focus_set)

    # ===================================================================
    # INPUT HANDLING
    # ===================================================================

    def _sync_input_height(self) -> None:
        if self._busy:
            return
        try:
            body = self._input.get("1.0", "end-1c")
        except tk.TclError:
            return
        lines = body.count("\n") + 1 if body else 1
        h = max(_INPUT_MIN_LINES, min(_INPUT_MAX_LINES, lines))
        try:
            cur = int(self._input.cget("height"))
        except (tk.TclError, ValueError):
            cur = _INPUT_MIN_LINES
        if cur != h:
            self._input.config(height=h)

    def _sync_input_state(self) -> None:
        self._sync_input_height()
        try:
            body = self._input.get("1.0", "end-1c")
        except tk.TclError:
            return
        if hasattr(self, "_input_count"):
            self._input_count.config(text=f"{len(body)} chars")
        if not self._busy:
            if body.strip():
                if hasattr(self, "_btn_send"):
                    self._btn_send.config(state=tk.NORMAL)
                self._set_input_status("Ready to send", _STATUS_INFO)
            else:
                if hasattr(self, "_btn_send"):
                    self._btn_send.config(state=tk.DISABLED)
                self._set_input_status("Ready", _STATUS_READY)

    def _on_ctrl_enter(self, event: tk.Event) -> str:
        self.root.after(1, self._on_send)
        return "break"

    def _on_enter(self, event: tk.Event) -> str:
        if not (event.state & 0x1):  # Shift not held
            self.root.after(1, self._on_send)
            return "break"
        return ""

    def _on_send(self) -> None:
        text = self._input.get("1.0", tk.END).strip()
        if not text:
            self._set_input_status("Type a message first", _STATUS_INFO)
            return
        if self._busy:
            return
        self._input.delete("1.0", tk.END)
        self._sync_input_state()
        self.append_user(text)
        self._set_busy(True)

        thread = threading.Thread(target=self._worker, args=(text,), daemon=True)
        thread.start()

    def _worker(self, user_text: str) -> None:
        try:
            handle_user_turn(self._messages, user_text, self._sink)
        except Exception as e:
            if not self._shutting_down:
                self._sink.error(f"[error] {e}")
        finally:
            if not self._shutting_down:
                try:
                    save_result = self._save_session()
                    if not save_result.get("ok"):
                        self._sink.error(f"[error] Could not save session: {save_result.get('error')}")
                except Exception as e:
                    self._sink.error(f"[error] Could not save session: {e}")
                try:
                    self._sink.stop_event.clear()
                    self.root.after(0, self._set_busy, False)
                    self.root.after(0, self._refresh_session_list)
                except RuntimeError:
                    pass

    # ===================================================================
    # SIDEBAR CALLBACKS
    # ===================================================================

    def _on_model_change(self) -> None:
        preset = self._model_var.get()
        if preset in MODELS:
            set_runtime_model(MODELS[preset])
            self._update_header()
            self.append_info(f"Model: {MODELS[preset]}")

    def _on_dry_toggle(self) -> None:
        set_dry_run(self._dry_var.get())
        self._update_header()
        self.append_info(f"Dry-run mode: {'ON' if is_dry_run() else 'OFF'}")

    def _on_verbose_toggle(self) -> None:
        set_verbose(self._verbose_var.get())
        self._update_header()
        self.append_info(f"Output mode: {'verbose' if is_verbose() else 'concise'}")

    def _on_undo(self) -> None:
        result = undo_last()
        if result.get("ok"):
            action = result.get("action", "")
            note = result.get("note", "")
            if action == "create_folder":
                self._insert(
                    f"Undo successful: removed empty folder {result.get('removed', '?')}\n",
                    "undo_ok",
                )
            else:
                restored = result.get("restored_to", "?")
                self._insert(
                    f"Undo successful: restored to {restored}{note}\n", "undo_ok",
                )
        else:
            self._insert(
                f"Undo failed: {result.get('error', 'unknown')}\n", "undo_fail",
            )
        self._refresh_undo_indicator()

    def _on_history(self) -> None:
        records = get_history(20)
        if not records:
            self.append_info("No actions in this session's undo history.")
            return
        self.append_info(f"Undo history (session {SESSION_ID}):")
        for i, r in enumerate(records, 1):
            action = r.get("action", "?")
            undone = " [UNDONE]" if r.get("undone") else ""
            ts = r.get("ts", "")[:19]
            if action in ("move_file", "rename_file", "organize_move"):
                self.append_info(
                    f"  {i}. [{ts}] {action}: "
                    f"{r.get('source', '?')} -> {r.get('destination', '?')}{undone}")
            elif action == "create_folder":
                self.append_info(
                    f"  {i}. [{ts}] {action}: {r.get('path', '?')}{undone}")
            else:
                self.append_info(f"  {i}. [{ts}] {action}{undone}")

    # ===================================================================
    # SESSION PERSISTENCE
    # ===================================================================

    _SESSIONS_MAX_VISIBLE = 8
    _SESSION_TITLE_MAX = 60

    def _sessions_dir(self) -> Path:
        return self._session_store.ensure_dir()

    def _session_path(self, session_id: str) -> Path:
        return self._session_store.session_path(session_id)

    def _session_title(self) -> str:
        return self._session_store.session_title(self._messages, self._SESSION_TITLE_MAX)

    def _save_session(self, *, report_errors: bool = False) -> dict[str, Any]:
        result = self._session_store.save_session(
            session_id=self._session_id,
            created=self._session_created,
            messages=self._messages,
            token_totals=self._token_totals,
            title_max=self._SESSION_TITLE_MAX,
        )
        if report_errors and not result.get("ok"):
            self.append_error(f"[error] Could not save session: {result.get('error')}")
        return result

    def _list_sessions(self) -> list[dict[str, Any]]:
        return self._session_store.list_sessions()

    def _refresh_session_list(self) -> None:
        if self._sessions_list_frame is None:
            return
        self._session_buttons.clear()
        for child in self._sessions_list_frame.winfo_children():
            child.destroy()
        sessions = self._list_sessions()[: self._SESSIONS_MAX_VISIBLE]
        if not sessions:
            tk.Label(
                self._sessions_list_frame, text="(none yet)",
                font=self._f_mono_sm, bg=_BG_SIDE, fg=_FG_DIM, anchor=tk.W,
            ).pack(fill=tk.X)
            return
        for entry in sessions:
            sid = entry.get("id", "?")
            title = entry.get("title") or "(untitled)"
            is_current = sid == self._session_id
            label = title if not is_current else f"\u25cf {title}"
            bg = _BG_SIDE_BUTTON_ACTIVE if is_current else _BTN_SIDE_BG
            hover_bg = "#c5d8eb" if is_current else _BG_SIDE_BUTTON_HOVER
            btn = tk.Button(
                self._sessions_list_frame,
                text=label,
                command=lambda p=entry["_path"]: self._load_session(p),
                font=self._f_ui_sm,
                bg=bg, fg=_FG, activebackground=hover_bg,
                relief=tk.FLAT, padx=6, pady=2, cursor="hand2",
                anchor=tk.W, justify=tk.LEFT,
                wraplength=_SIDE_W - 28,
            )
            btn.pack(fill=tk.X, pady=1)
            self._hover_button(btn, bg, hover_bg)
            if self._busy:
                btn.config(state=tk.DISABLED)
            self._session_buttons.append(btn)

    def _rebuild_transcript(self) -> None:
        self._chat.config(state=tk.NORMAL)
        self._chat.delete("1.0", tk.END)
        self._chat.config(state=tk.DISABLED)
        for msg in self._messages:
            role = msg.get("role")
            content = msg.get("content")
            if not content or not isinstance(content, str):
                continue
            if role == "user":
                self._insert_turn_separator()
                self._insert("You\n", "user_label")
                self._insert(content + "\n\n", "user_msg")
            elif role == "assistant":
                self.append_assistant(content)

    def _load_session(self, path: Path) -> None:
        if self._busy:
            self.append_info("Cannot switch sessions while working.")
            return
        loaded = self._session_store.load_session(path)
        if not loaded.get("ok"):
            self.append_error(f"[error] Could not load session: {loaded.get('error')}")
            return
        # Save current session first so no work is lost.
        self._save_session(report_errors=True)
        data = loaded["data"]
        self._session_id = data.get("id") or path.stem
        self._session_created = data.get("created") or (
            datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        )
        self._messages = list(data.get("messages") or [])
        # Ensure a system prompt exists at the front.
        if not self._messages or self._messages[0].get("role") != "system":
            self._messages.insert(0, {"role": "system", "content": SYSTEM_PROMPT})
        saved_totals = data.get("token_totals") or {}
        for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
            self._token_totals[k] = int(saved_totals.get(k) or 0)
        self._update_token_chip()
        self._update_header()
        self._hide_welcome()
        self._rebuild_transcript()
        self._refresh_session_list()

    def _on_clear(self) -> None:
        # Save the outgoing session before starting a fresh one.
        self._save_session(report_errors=True)
        self._chat.config(state=tk.NORMAL)
        self._chat.delete("1.0", tk.END)
        self._chat.config(state=tk.DISABLED)
        self._messages.clear()
        self._messages.append({"role": "system", "content": SYSTEM_PROMPT})
        self._session_id = uuid.uuid4().hex[:12]
        self._session_created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for k in self._token_totals:
            self._token_totals[k] = 0
        self._update_token_chip()
        self._update_header()
        self._show_welcome()
        self._refresh_session_list()

    def _on_allowed_folders(self) -> None:
        dlg = tk.Toplevel(self.root)
        dlg.title("Allowed Folders")
        dlg.configure(bg=_BG)
        dlg.transient(self.root)
        dlg.geometry("560x360")

        tk.Label(
            dlg,
            text="The assistant can only read or modify files under these folders.",
            font=self._f_ui_sm, bg=_BG, fg=_FG_LABEL, anchor=tk.W, justify=tk.LEFT,
            wraplength=540,
        ).pack(fill=tk.X, padx=12, pady=(12, 6))

        list_frame = tk.Frame(dlg, bg=_BG)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 6))

        scrollbar = tk.Scrollbar(list_frame)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        listbox = tk.Listbox(
            list_frame, font=self._f_mono_sm, bg=_BG_INPUT, fg=_FG,
            relief=tk.SOLID, borderwidth=1, activestyle="none",
            yscrollcommand=scrollbar.set,
        )
        listbox.pack(fill=tk.BOTH, expand=True)
        scrollbar.config(command=listbox.yview)

        roots: list[Path] = [Path(p) for p in get_allowed_roots()]

        def refresh() -> None:
            listbox.delete(0, tk.END)
            for r in roots:
                listbox.insert(tk.END, str(r))

        def on_add() -> None:
            chosen = filedialog.askdirectory(parent=dlg, title="Add allowed folder")
            if not chosen:
                return
            p = Path(chosen).expanduser().resolve()
            if any(str(r).casefold() == str(p).casefold() for r in roots):
                return
            roots.append(p)
            refresh()

        def on_remove() -> None:
            sel = list(listbox.curselection())
            for idx in reversed(sel):
                del roots[idx]
            refresh()

        def on_reset() -> None:
            if not messagebox.askyesno(
                "Reset", "Revert to default folders (Desktop, Documents, Downloads)?",
                parent=dlg,
            ):
                return
            reset_allowed_roots()
            roots.clear()
            roots.extend(get_allowed_roots())
            refresh()

        def on_save() -> None:
            if not roots:
                messagebox.showwarning(
                    "Empty", "Keep at least one folder so the assistant has somewhere to work.",
                    parent=dlg,
                )
                return
            if os.environ.get("XAI_ASSISTANT_ALLOWED_ROOTS"):
                messagebox.showwarning(
                    "Env override",
                    "XAI_ASSISTANT_ALLOWED_ROOTS is set in the environment and takes "
                    "precedence. Unset it for these saved folders to apply.",
                    parent=dlg,
                )
            set_allowed_roots(roots)
            self._update_header()
            self.append_info(f"Allowed folders updated ({len(roots)}).")
            dlg.destroy()

        refresh()

        btns = tk.Frame(dlg, bg=_BG)
        btns.pack(fill=tk.X, padx=12, pady=(0, 12))
        for text, cmd in (
            ("Add...", on_add),
            ("Remove", on_remove),
            ("Reset", on_reset),
        ):
            btn = tk.Button(
                btns, text=text, command=cmd, font=self._f_ui_sm,
                bg=_BTN_SIDE_BG, fg=_FG, activebackground="#cfcfcf",
                relief=tk.FLAT, padx=10, pady=4, cursor="hand2",
            )
            btn.pack(side=tk.LEFT, padx=(0, 6))
            self._hover_button(btn, _BTN_SIDE_BG, _BG_SIDE_BUTTON_HOVER)
        btn_save = tk.Button(
            btns, text="Save", command=on_save, font=self._f_approval_btn,
            bg=_BTN_PRIMARY_BG, fg="white", activebackground="#1976d2",
            activeforeground="white", relief=tk.FLAT, padx=14, pady=4, cursor="hand2",
        )
        btn_save.pack(side=tk.RIGHT)
        self._hover_button(btn_save, _BTN_PRIMARY_BG, _BTN_PRIMARY_HOVER_BG)
        btn_cancel = tk.Button(
            btns, text="Cancel", command=dlg.destroy, font=self._f_ui_sm,
            bg=_BTN_SIDE_BG, fg=_FG, relief=tk.FLAT, padx=10, pady=4, cursor="hand2",
        )
        btn_cancel.pack(side=tk.RIGHT, padx=(0, 6))
        self._hover_button(btn_cancel, _BTN_SIDE_BG, _BG_SIDE_BUTTON_HOVER)

    def _on_open_logs(self) -> None:
        log_dir = get_log_dir()
        try:
            os.startfile(str(log_dir))  # type: ignore[attr-defined]
        except Exception:
            self.append_info(f"Logs folder: {log_dir}")

    # ===================================================================
    # RUN
    # ===================================================================

    def run(self) -> None:
        self.root.mainloop()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    api_key = get_xai_api_key()
    if not api_key:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            _APP_TITLE,
            "Missing XAI_API_KEY.\n\nCopy .env.example to .env and add your API key.",
        )
        root.destroy()
        sys.exit(1)

    app = AssistantApp()
    app.run()


if __name__ == "__main__":
    main()
