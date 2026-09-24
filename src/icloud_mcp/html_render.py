"""HTML rendering for free-text iCloud fields.

iCloud lets users paste rich-text into calendar event description /
location fields and contact notes; the values come back through CalDAV
/ CardDAV verbatim, including embedded HTML like ``<br>`` and ``<a>``.

This module renders those values into a form chosen by the operator via
the ``ICLOUD_HTML_MODE`` env var:

* ``markdown`` (default) — convert common HTML to Markdown so links and
  emphasis survive while LLMs read it natively. ``<br>`` → newline,
  ``<a href="x">y</a>`` → ``[y](x)``, ``<b>``/``<strong>`` → ``**…**``,
  ``<i>``/``<em>`` → ``*…*``, ``<li>`` → ``- ``.
* ``strip`` — drop all tags, decode entities, collapse whitespace. Smaller
  token footprint; loses link URLs.
* ``raw`` — return the value untouched. Lossless passthrough.

Email message bodies are *not* rendered through this module — the IMAP
side already exposes ``text`` and ``html`` parts separately, and the
agent picks per-call.
"""

from __future__ import annotations

import os
import re
from html import unescape
from html.parser import HTMLParser

_MODE_ENV_VAR = "ICLOUD_HTML_MODE"
_VALID_MODES = ("markdown", "strip", "raw")
_DEFAULT_MODE = "markdown"


def get_mode() -> str:
    """Resolve the active render mode from env, falling back to markdown.

    Unknown / empty values fall through to the default rather than raising —
    operator typos shouldn't break the server.
    """
    raw = (os.environ.get(_MODE_ENV_VAR) or "").strip().lower()
    return raw if raw in _VALID_MODES else _DEFAULT_MODE


def _looks_like_html(value: str) -> bool:
    """Cheap pre-check so plain-text fields skip the parser entirely.

    Trips on common tag patterns and entity references; false positives
    are harmless (the renderers are idempotent on plain text).
    """
    if not value:
        return False
    return ("<" in value and ">" in value) or "&" in value


class _MarkdownRenderer(HTMLParser):
    """Convert a subset of HTML to Markdown in a single pass.

    Unknown tags are dropped (their inner text is kept). Whitespace is
    collapsed to single spaces; explicit ``<br>``/``<p>`` produce
    newlines.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        self._href_stack: list[str | None] = []
        self._list_stack: list[str] = []  # "ul" | "ol"

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "br":
            self._out.append("\n")
        elif tag in ("p", "div"):
            if self._out and not self._out[-1].endswith("\n"):
                self._out.append("\n")
        elif tag in ("b", "strong"):
            self._out.append("**")
        elif tag in ("i", "em"):
            self._out.append("*")
        elif tag == "a":
            self._href_stack.append(a.get("href"))
            self._out.append("[")
        elif tag in ("ul", "ol"):
            self._list_stack.append(tag)
            if self._out and not self._out[-1].endswith("\n"):
                self._out.append("\n")
        elif tag == "li":
            marker = "1. " if (self._list_stack and self._list_stack[-1] == "ol") else "- "
            if self._out and not self._out[-1].endswith("\n"):
                self._out.append("\n")
            self._out.append(marker)

    def handle_endtag(self, tag: str) -> None:
        if tag in ("b", "strong"):
            self._out.append("**")
        elif tag in ("i", "em"):
            self._out.append("*")
        elif tag == "a":
            href = self._href_stack.pop() if self._href_stack else None
            self._out.append(f"]({href})" if href else "]")
        elif tag in ("p", "div", "li"):
            self._out.append("\n")
        elif tag in ("ul", "ol"):
            if self._list_stack:
                self._list_stack.pop()
            self._out.append("\n")

    def handle_data(self, data: str) -> None:
        self._out.append(data)

    def render(self, value: str) -> str:
        self.feed(value)
        self.close()
        out = "".join(self._out)
        # Collapse 3+ blank lines into 2; trim outer whitespace
        out = re.sub(r"\n{3,}", "\n\n", out)
        return out.strip()


class _StripRenderer(HTMLParser):
    """Drop tags, keep text, normalize whitespace."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("br", "p", "div", "li", "tr"):
            self._out.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("p", "div", "li", "tr"):
            self._out.append("\n")

    def handle_data(self, data: str) -> None:
        self._out.append(data)

    def render(self, value: str) -> str:
        self.feed(value)
        self.close()
        out = "".join(self._out)
        out = re.sub(r"[ \t]+", " ", out)
        out = re.sub(r"\n{3,}", "\n\n", out)
        return out.strip()


def render(value: str | None, mode: str | None = None) -> str | None:
    """Render a free-text field per the active mode.

    Parameters
    ----------
    value:
        The string to render, or ``None``. ``None`` and empty strings are
        returned unchanged.
    mode:
        Override the env-var-driven mode. Mainly for tests.

    Returns
    -------
    The rendered string, or ``value`` unchanged if mode is ``raw`` or the
    input doesn't look like HTML at all.
    """
    if not value:
        return value
    active = mode or get_mode()
    if active == "raw":
        return value
    if not _looks_like_html(value):
        # Still unescape entities (&amp; etc.) even in plain-text-looking
        # values, since they're cheap and fix common copy-paste artifacts.
        return unescape(value)
    if active == "markdown":
        return _MarkdownRenderer().render(value)
    if active == "strip":
        return _StripRenderer().render(value)
    return value
