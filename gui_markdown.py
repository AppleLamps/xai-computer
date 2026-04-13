"""Subset Markdown → Tkinter Text inserts (assistant replies).

Covers fenced code, headings, bullet/numbered lists, blockquotes,
**bold**, *italic*, `inline code`, and [label](url) links.
"""

from __future__ import annotations

import re
from typing import Callable

_RE_H1 = re.compile(r"^#\s+(.+)$")
_RE_H2 = re.compile(r"^##\s+(.+)$")
_RE_H3 = re.compile(r"^###\s+(.+)$")
_RE_UL = re.compile(r"^(\s*)[-*]\s+(.+)$")
_RE_OL = re.compile(r"^(\s*)(\d+)\.\s+(.+)$")
_RE_QUOTE = re.compile(r"^>\s?(.*)$")
_RE_RULE = re.compile(r"^(?:---|\*\*\*|___)\s*$")
_RE_LINK = re.compile(r"\[([^\]]*)\]\(([^)]+)\)")


def _parse_inline_insert(
    line: str,
    insert: Callable[[str, tuple[str, ...]], None],
    base: tuple[str, ...],
) -> None:
    """Parse one line for `code`, **bold**, *italic*, and [text](url)."""
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if ch == "`":
            j = line.find("`", i + 1)
            if j == -1:
                insert(line[i:], base)
                return
            insert(line[i + 1 : j], base + ("md_code",))
            i = j + 1
            continue
        if ch == "*" and i + 1 < n and line[i + 1] == "*":
            j = line.find("**", i + 2)
            if j == -1:
                insert(line[i:], base)
                return
            insert(line[i + 2 : j], base + ("md_bold",))
            i = j + 2
            continue
        if ch == "*" and (i == 0 or line[i - 1] != "*"):
            j = line.find("*", i + 1)
            while j != -1 and j + 1 < n and line[j + 1] == "*":
                j = line.find("*", j + 1)
            if j == -1:
                insert(line[i:], base)
                return
            insert(line[i + 1 : j], base + ("md_italic",))
            i = j + 1
            continue
        m = _RE_LINK.search(line, i)
        if m and m.start() == i:
            insert(m.group(1), base + ("md_link",))
            insert(f" ({m.group(2)})", base + ("md_url_note",))
            i = m.end()
            continue
        next_special = n
        if m:
            next_special = min(next_special, m.start())
        tick = line.find("`", i)
        if tick != -1:
            next_special = min(next_special, tick)
        star2 = line.find("**", i)
        if star2 != -1:
            next_special = min(next_special, star2)
        star1 = line.find("*", i)
        if star1 != -1 and (star1 == 0 or line[star1 - 1] != "*"):
            if star1 + 1 < n and line[star1 + 1] != "*":
                next_special = min(next_special, star1)
        insert(line[i:next_special], base)
        i = next_special


def _insert_line_markdown(
    raw_line: str,
    insert: Callable[[str, tuple[str, ...]], None],
    base: tuple[str, ...],
    nl: str,
) -> None:
    line = raw_line.rstrip("\r\n")
    if not line.strip():
        insert(nl, base)
        return
    m = _RE_RULE.match(line)
    if m:
        insert("\u2500" * 40 + nl, base + ("md_rule",))
        return
    m = _RE_H1.match(line)
    if m:
        _parse_inline_insert(m.group(1), insert, base + ("md_h1",))
        insert(nl, base)
        return
    m = _RE_H2.match(line)
    if m:
        _parse_inline_insert(m.group(1), insert, base + ("md_h2",))
        insert(nl, base)
        return
    m = _RE_H3.match(line)
    if m:
        _parse_inline_insert(m.group(1), insert, base + ("md_h3",))
        insert(nl, base)
        return
    m = _RE_UL.match(line)
    if m:
        indent, body = m.group(1), m.group(2)
        bullet = " \u2022 " if len(indent) == 0 else "   \u2022 "
        insert(bullet, base + ("md_li",))
        _parse_inline_insert(body, insert, base + ("md_li",))
        insert(nl, base)
        return
    m = _RE_OL.match(line)
    if m:
        indent, num, body = m.group(1), m.group(2), m.group(3)
        prefix = f"{num}. " if not indent else f"   {num}. "
        insert(prefix, base + ("md_li_num",))
        _parse_inline_insert(body, insert, base + ("md_li_num",))
        insert(nl, base)
        return
    m = _RE_QUOTE.match(line)
    if m:
        insert("\u2014 ", base + ("md_quote",))
        _parse_inline_insert(m.group(1), insert, base + ("md_quote",))
        insert(nl, base)
        return
    _parse_inline_insert(line, insert, base)
    insert(nl, base)


def _flush_markdown_segment(
    segment: str,
    widget,
    base_tags: tuple[str, ...],
) -> None:
    if not segment:
        return

    def ins(chunk: str, tags: tuple[str, ...]) -> None:
        if chunk:
            widget.insert("end", chunk, tags)

    for ln in segment.splitlines(keepends=True):
        raw = ln
        nl = "\n"
        if raw.endswith("\r\n"):
            raw = raw[:-2]
        elif raw.endswith("\n"):
            raw = raw[:-1]
        else:
            nl = ""
        _insert_line_markdown(raw, ins, base_tags, nl)


def insert_markdown(
    widget,
    text: str,
    *,
    base_tags: tuple[str, ...],
    trailing: str = "\n",
) -> None:
    """Append *text* at END of *widget* using md_* tags plus *base_tags*."""
    if not text:
        return

    pos = 0
    n = len(text)
    while pos < n:
        start = text.find("```", pos)
        if start == -1:
            _flush_markdown_segment(text[pos:], widget, base_tags)
            break
        if start > pos:
            _flush_markdown_segment(text[pos:start], widget, base_tags)
        hdr_end = text.find("\n", start + 3)
        if hdr_end == -1:
            widget.insert("end", text[start:], base_tags)
            return
        close = text.find("```", hdr_end + 1)
        if close == -1:
            block = text[hdr_end + 1 :]
            if block:
                widget.insert(
                    "end",
                    block.rstrip("\r\n") + "\n",
                    base_tags + ("md_codeblock",),
                )
            break
        block = text[hdr_end + 1 : close]
        if block:
            widget.insert(
                "end",
                block.rstrip("\r\n") + "\n",
                base_tags + ("md_codeblock",),
            )
        pos = close + 3

    if trailing:
        widget.insert("end", trailing, base_tags)
