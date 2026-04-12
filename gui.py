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
from tkinter import messagebox, scrolledtext
from typing import Any

from config import (
    MODELS,
    get_log_dir,
    get_xai_api_key,
    get_xai_model,
    is_dry_run,
    is_verbose,
    set_dry_run,
    set_runtime_model,
    set_verbose,
)
from core import ApprovalCard, get_startup_info, handle_user_turn
from logger import SESSION_ID
from schemas import SYSTEM_PROMPT
from undo import get_history, undo_last

# ---------------------------------------------------------------------------
# Design tokens
# ---------------------------------------------------------------------------

_APP_TITLE = "xai-computer"
_WIN_W = 960
_WIN_H = 720
_MIN_W = 720
_MIN_H = 520
_SIDE_W = 210
_APPROVAL_MAX_ACTIONS_VISIBLE = 8

_BG = "#f3f3f3"
_BG_SIDE = "#eaeaea"
_BG_CHAT = "#ffffff"
_BG_INPUT = "#ffffff"
_BG_HEADER = "#e2e2e2"
_BG_DISABLED = "#f0f0f0"

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
_BTN_CANCEL_BG = "#b71c1c"
_BTN_PRIMARY_BG = "#1565c0"
_BTN_SIDE_BG = "#dcdcdc"

_SEP_COLOR = "#c8c8c8"
_TURN_SEP_COLOR = "#e0e0e0"
_APPROVAL_BORDER = "#ef6c00"
_APPROVAL_BORDER_HIGH = "#b71c1c"
_FG_RISK_HIGH = "#b71c1c"
_BUSY_COLOR = "#ef6c00"


# ---------------------------------------------------------------------------
# GuiSink — thread-safe OutputSink for Tkinter
# ---------------------------------------------------------------------------


class GuiSink:
    """Posts structured events to the Tkinter main thread."""

    def __init__(self, app: AssistantApp) -> None:
        self._app = app
        self._confirmation_event = threading.Event()
        self._confirmation_answer: str = "cancel"

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
        self._post(self._app.append_assistant, text)

    def plan(self, card: ApprovalCard) -> None:
        if self._app._shutting_down:
            return
        self._confirmation_event.clear()
        self._confirmation_answer = "cancel"
        self._post(self._app.show_approval_card, card, self)
        self._confirmation_event.wait()

    def progress(self, text: str) -> None:
        self._post(self._app.append_progress, text)

    def prompt_confirmation(self, prompt_text: str) -> str:
        return self._confirmation_answer

    def resolve_confirmation(self, answer: str) -> None:
        self._confirmation_answer = answer
        self._confirmation_event.set()


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------


class AssistantApp:
    def __init__(self) -> None:
        self.root = tk.Tk()
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
        self._approval_scroll_widgets: list[tk.Widget] = []

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
        self.root.bind_all("<Escape>", lambda e: self._focus_input())
        self.root.bind_all("<Control-l>", lambda e: self._focus_input())

    def _focus_input(self) -> None:
        if not self._busy:
            self._input.focus_set()

    # ===================================================================
    # UI CONSTRUCTION
    # ===================================================================

    def _build_ui(self) -> None:
        self._build_header()
        self._build_body()
        self._build_input_bar()
        self._bind_shortcuts()

    # ── Header ──

    def _build_header(self) -> None:
        hdr = tk.Frame(self.root, bg=_BG_HEADER, padx=10, pady=5)
        hdr.pack(fill=tk.X)

        left = tk.Frame(hdr, bg=_BG_HEADER)
        left.pack(side=tk.LEFT)

        self._chip_model = tk.Label(left, text="", font=self._f_header,
                                    bg=_BG_HEADER, fg=_FG_LABEL, padx=2)
        self._chip_model.pack(side=tk.LEFT, padx=(0, 14))

        self._chip_dry = tk.Label(left, text="", font=self._f_header,
                                  bg=_BG_HEADER, fg=_FG_DIM, padx=2)
        self._chip_dry.pack(side=tk.LEFT, padx=(0, 14))

        self._chip_mode = tk.Label(left, text="", font=self._f_header,
                                   bg=_BG_HEADER, fg=_FG_DIM, padx=2)
        self._chip_mode.pack(side=tk.LEFT, padx=(0, 14))

        self._chip_busy = tk.Label(left, text="", font=("Segoe UI", 9, "bold"),
                                   bg=_BG_HEADER, fg=_BUSY_COLOR, padx=2)
        self._chip_busy.pack(side=tk.LEFT)

        tk.Label(hdr, text=f"Session {SESSION_ID}", font=self._f_mono_sm,
                 bg=_BG_HEADER, fg=_FG_DIM).pack(side=tk.RIGHT)

    # ── Body ──

    def _build_body(self) -> None:
        body = tk.PanedWindow(self.root, orient=tk.HORIZONTAL, bg=_BG,
                              sashwidth=5, sashrelief=tk.FLAT)
        body.pack(fill=tk.BOTH, expand=True, padx=0, pady=0)

        # Left: chat + approval overlay
        self._chat_container = tk.Frame(body, bg=_BG)
        body.add(self._chat_container, stretch="always", minsize=400)

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
                        font=self._f_ui_sm_bold, lmargin1=4, spacing1=6, spacing3=1)
        c.tag_configure("user_msg", background=_BG_USER_MSG, font=self._f_ui,
                        lmargin1=4, lmargin2=4, rmargin=40, spacing3=4)

        # Assistant
        c.tag_configure("asst_label", foreground=_FG_LABEL,
                        font=self._f_ui_sm_bold, lmargin1=4, spacing1=6, spacing3=1)
        c.tag_configure("asst_msg", background=_BG_ASST_MSG, font=self._f_ui,
                        lmargin1=4, lmargin2=4, rmargin=40, spacing3=4)

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

    # ── Sidebar ──

    def _build_sidebar(self, parent: tk.Frame) -> None:
        pad_x = 10

        # Model
        self._side_section(parent, "Model", top_pad=10)
        model_frame = tk.Frame(parent, bg=_BG_SIDE)
        model_frame.pack(fill=tk.X, padx=pad_x, pady=(0, 4))

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
        self._cb_dry.pack(fill=tk.X, padx=pad_x)
        self._sidebar_widgets.append(self._cb_dry)

        self._verbose_var = tk.BooleanVar(value=is_verbose())
        self._cb_verbose = tk.Checkbutton(
            parent, text="Verbose output", variable=self._verbose_var,
            command=self._on_verbose_toggle, font=self._f_ui_sm,
            bg=_BG_SIDE, fg=_FG, activebackground=_BG_SIDE, selectcolor=_BG_SIDE,
            anchor=tk.W,
        )
        self._cb_verbose.pack(fill=tk.X, padx=pad_x)
        self._sidebar_widgets.append(self._cb_verbose)

        # Actions
        self._side_sep(parent)
        self._side_section(parent, "Actions")

        self._btn_undo = self._side_button(parent, "Undo Last", self._on_undo)
        self._sidebar_widgets.append(self._btn_undo)
        self._btn_history = self._side_button(parent, "Show History", self._on_history)
        self._sidebar_widgets.append(self._btn_history)
        btn_clear = self._side_button(parent, "Clear Chat", self._on_clear)
        self._sidebar_widgets.append(btn_clear)
        self._side_button(parent, "Open Logs Folder", self._on_open_logs)

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
        tk.Label(parent, text=title, font=self._f_ui_sm_bold, bg=_BG_SIDE,
                 fg=_FG_LABEL, anchor=tk.W).pack(fill=tk.X, padx=10, pady=(top_pad, 3))

    def _side_sep(self, parent: tk.Frame) -> None:
        tk.Frame(parent, bg=_SEP_COLOR, height=1).pack(fill=tk.X, padx=8, pady=8)

    def _side_button(self, parent: tk.Frame, text: str, cmd: Any) -> tk.Button:
        btn = tk.Button(
            parent, text=text, command=cmd, font=self._f_ui_sm,
            bg=_BTN_SIDE_BG, fg=_FG, activebackground="#cfcfcf",
            relief=tk.FLAT, padx=8, pady=3, cursor="hand2", anchor=tk.W,
        )
        btn.pack(fill=tk.X, padx=10, pady=1)
        return btn

    # ── Input bar ──

    def _build_input_bar(self) -> None:
        # Separator
        tk.Frame(self.root, bg=_SEP_COLOR, height=1).pack(fill=tk.X, side=tk.BOTTOM)

        bar = tk.Frame(self.root, bg=_BG, padx=10, pady=8)
        bar.pack(fill=tk.X, side=tk.BOTTOM)

        self._input = tk.Text(
            bar, height=3, font=self._f_ui, bg=_BG_INPUT, fg=_FG,
            relief=tk.SOLID, borderwidth=1, padx=8, pady=6, wrap=tk.WORD,
            insertbackground=_FG,
        )
        self._input.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 8))
        self._input.bind("<Return>", self._on_enter)
        self._input.bind("<Shift-Return>", lambda e: None)

        btn_col = tk.Frame(bar, bg=_BG)
        btn_col.pack(side=tk.RIGHT, fill=tk.Y)

        self._btn_send = tk.Button(
            btn_col, text="Send", font=self._f_approval_btn, width=8,
            command=self._on_send, bg=_BTN_PRIMARY_BG, fg="white",
            activebackground="#1976d2", activeforeground="white",
            relief=tk.FLAT, cursor="hand2",
        )
        self._btn_send.pack(fill=tk.X, pady=(0, 2))

        tk.Label(
            btn_col, text="Enter to send\nShift+Enter: newline",
            font=("Segoe UI", 7), bg=_BG, fg=_FG_DIM, justify=tk.CENTER,
        ).pack()

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
            self._chip_dry.config(text="DRY RUN", fg=_FG_RISK_MED,
                                  font=("Segoe UI", 9, "bold"))
        else:
            self._chip_dry.config(text="Live", fg=_FG_DIM, font=self._f_header)

        mode = "Verbose" if is_verbose() else "Concise"
        self._chip_mode.config(text=mode)

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
            self._btn_send.config(state=tk.DISABLED, text="Working...",
                                  bg="#90a4ae")
            self._chip_busy.config(text="Working...")
            self._input.config(state=tk.DISABLED, bg=_BG_DISABLED)
            for w in self._sidebar_widgets:
                try:
                    w.config(state=tk.DISABLED)
                except tk.TclError:
                    pass
        else:
            self._btn_send.config(state=tk.NORMAL, text="Send",
                                  bg=_BTN_PRIMARY_BG)
            self._chip_busy.config(text="")
            self._input.config(state=tk.NORMAL, bg=_BG_INPUT)
            for w in self._sidebar_widgets:
                try:
                    w.config(state=tk.NORMAL)
                except tk.TclError:
                    pass
            self._refresh_undo_indicator()
            self._focus_input()

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

    def _show_welcome(self) -> None:
        info = get_startup_info()
        dry = "  [DRY RUN active]" if info["dry_run"] else ""
        roots = ", ".join(os.path.basename(r) for r in info["allowed_roots"])
        self._insert(
            f"Ready.{dry}\n\n"
            f"  Desktop:  {info['desktop']}\n"
            f"  Roots:    {roots}\n"
            f"  Model:    {info['model']}\n\n"
            f"Type a message below to get started.\n"
            f"Use the sidebar to switch models, toggle dry-run, or undo.\n",
            "info",
        )

    def append_user(self, text: str) -> None:
        self._insert_turn_separator()
        self._insert("You\n", "user_label")
        self._insert(text + "\n\n", "user_msg")

    def append_assistant(self, text: str) -> None:
        self._insert("Assistant\n", "asst_label")
        self._insert(text + "\n\n", "asst_msg")

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
        undo_hint = "  Use Undo Last in the sidebar to reverse." if not is_dry_run() else ""

        self._insert(f"Result{dry}\n", "result_hdr")
        self._insert(f"  {summary_text}\n", "result_line")
        if undo_hint:
            self._insert(f"  {undo_hint}\n", "result_line")
        self._insert("\n", "info")

    # ===================================================================
    # APPROVAL PANEL
    # ===================================================================

    def show_approval_card(self, card: ApprovalCard, sink: GuiSink) -> None:
        inner = self._approval_inner
        for w in inner.winfo_children():
            w.destroy()

        dry_tag = "  [DRY RUN]" if card.dry_run else ""
        is_high = card.risk_level == "high"
        is_medium = card.risk_level == "medium"

        # Set border color by risk level
        border_color = _APPROVAL_BORDER_HIGH if is_high else _APPROVAL_BORDER
        self._approval_outer.config(bg=border_color)

        # Title row
        title_frame = tk.Frame(inner, bg=_BG_APPROVAL)
        title_frame.pack(fill=tk.X)

        tk.Label(
            title_frame, text=f"APPROVAL REQUIRED{dry_tag}",
            font=self._f_approval_title, bg=_BG_APPROVAL, fg=_FG,
        ).pack(side=tk.LEFT)

        if is_high:
            risk_fg = _FG_RISK_HIGH
            risk_text = "HIGH"
        elif is_medium:
            risk_fg = _FG_RISK_MED
            risk_text = "MEDIUM"
        else:
            risk_fg = _FG_RISK_LOW
            risk_text = "LOW"
        tk.Label(
            title_frame, text=f"Risk: {risk_text}",
            font=self._f_ui_sm_bold, bg=_BG_APPROVAL, fg=risk_fg,
        ).pack(side=tk.RIGHT)

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

        # Scope + summary
        meta_parts: list[str] = []
        if card.affected_root:
            meta_parts.append(f"Scope: {card.affected_root}")
        meta_parts.append(card.summary)
        tk.Label(
            inner, text="  |  ".join(meta_parts), font=self._f_ui_sm,
            bg=_BG_APPROVAL, fg=_FG_DIM, anchor=tk.W,
            wraplength=600, justify=tk.LEFT,
        ).pack(fill=tk.X, pady=(2, 6))

        # Divider
        tk.Frame(inner, bg="#e0c080", height=1).pack(fill=tk.X, pady=(0, 4))

        # Action list — scrollable if large
        n_actions = len(card.actions)
        needs_scroll = n_actions > _APPROVAL_MAX_ACTIONS_VISIBLE

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
            canvas.create_window((0, 0), window=actions_frame, anchor=tk.NW)
            canvas.configure(yscrollcommand=scrollbar.set)

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
            row = tk.Frame(actions_frame, bg=_BG_APPROVAL)
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
                bg=_BG_APPROVAL, fg=_FG_DIM, width=3, anchor=tk.E,
            )
            num_lbl.pack(side=tk.LEFT, padx=(0, 4))
            act_lbl = tk.Label(
                row, text=f"{action.label}{marker}", font=self._f_mono,
                bg=_BG_APPROVAL, fg=marker_fg, anchor=tk.W,
                wraplength=520, justify=tk.LEFT,
            )
            act_lbl.pack(side=tk.LEFT, fill=tk.X, expand=True)

            # Propagate scroll to all child widgets in the scrollable case
            if _on_mousewheel is not None:
                for w in (row, num_lbl, act_lbl):
                    w.bind("<MouseWheel>", _on_mousewheel)
                    self._approval_scroll_widgets.append(w)

        # Divider
        tk.Frame(inner, bg="#e0c080", height=1).pack(fill=tk.X, pady=(4, 8))

        # Buttons — always visible, never scrolled
        btn_frame = tk.Frame(inner, bg=_BG_APPROVAL)
        btn_frame.pack(fill=tk.X)

        def _cleanup_and_resolve(answer: str) -> None:
            for w in self._approval_scroll_widgets:
                try:
                    w.unbind("<MouseWheel>")
                except tk.TclError:
                    pass
            self._approval_scroll_widgets.clear()
            self._resolve_approval(answer, sink)

        tk.Button(
            btn_frame, text="  Approve  ", font=self._f_approval_btn,
            bg=_BTN_APPROVE_BG, fg="white",
            activebackground="#388e3c", activeforeground="white",
            relief=tk.FLAT, cursor="hand2", padx=12, pady=4,
            command=lambda: _cleanup_and_resolve("yes"),
        ).pack(side=tk.LEFT, padx=(0, 10))

        tk.Button(
            btn_frame, text="  Cancel  ", font=self._f_ui_sm_bold,
            bg=_BTN_CANCEL_BG, fg="white",
            activebackground="#c62828", activeforeground="white",
            relief=tk.FLAT, cursor="hand2", padx=12, pady=4,
            command=lambda: _cleanup_and_resolve("cancel"),
        ).pack(side=tk.LEFT)

        count_text = f"{n_actions} action(s)"
        if needs_scroll:
            count_text += f" (scroll to see all)"
        tk.Label(
            btn_frame, text=count_text, font=self._f_ui_sm,
            bg=_BG_APPROVAL, fg=_FG_DIM,
        ).pack(side=tk.RIGHT)

        # Show panel
        self._approval_outer.pack(fill=tk.X, side=tk.BOTTOM, padx=6, pady=(0, 2))

        # Echo to transcript
        self._insert("Approval requested\n", "plan_hdr")
        for action in card.actions:
            risk_mark = " [!]" if action.risk == "medium" else ""
            self._insert(f"  {action.index}. {action.label}{risk_mark}\n", "plan_line")

    def _resolve_approval(self, answer: str, sink: GuiSink) -> None:
        # Clean up any scroll bindings (safety net)
        for w in self._approval_scroll_widgets:
            try:
                w.unbind("<MouseWheel>")
            except tk.TclError:
                pass
        self._approval_scroll_widgets.clear()

        self._approval_outer.pack_forget()
        if answer == "yes":
            self._insert("[Approved]\n", "progress")
        else:
            self._insert("[Cancelled]\n\n", "info")
        sink.resolve_confirmation(answer)

    # ===================================================================
    # INPUT HANDLING
    # ===================================================================

    def _on_enter(self, event: tk.Event) -> str:
        if not (event.state & 0x1):  # Shift not held
            self.root.after(1, self._on_send)
            return "break"
        return ""

    def _on_send(self) -> None:
        text = self._input.get("1.0", tk.END).strip()
        if not text or self._busy:
            return
        self._input.delete("1.0", tk.END)
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
                    self.root.after(0, self._set_busy, False)
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

    def _on_clear(self) -> None:
        self._chat.config(state=tk.NORMAL)
        self._chat.delete("1.0", tk.END)
        self._chat.config(state=tk.DISABLED)
        self._messages.clear()
        self._messages.append({"role": "system", "content": SYSTEM_PROMPT})
        self._show_welcome()

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
