"""Terminal formatting, ANSI color support, and monospace table alignment."""

from __future__ import annotations

import os
import re
import sys
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def supports_color() -> bool:
    """Return True if stdout supports ANSI escape sequences."""
    if not sys.stdout.isatty():
        return False
    if os.environ.get("NO_COLOR"):
        return False
    return os.environ.get("TERM") != "dumb"


@dataclass(frozen=True)
class Theme:
    """ANSI styling token container with automatic color suppression."""

    r: str = ""
    b: str = ""
    d: str = ""
    green: str = ""
    red: str = ""
    yellow: str = ""
    cyan: str = ""
    magenta: str = ""


def get_theme(color: bool | None = None) -> Theme:
    """Return active Theme instance with ANSI codes or empty strings."""
    enabled = supports_color() if color is None else color
    if not enabled:
        return Theme()
    return Theme(
        r="\033[0m",
        b="\033[1m",
        d="\033[2m",
        green="\033[32m",
        red="\033[31m",
        yellow="\033[33m",
        cyan="\033[36m",
        magenta="\033[35m",
    )


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from text."""
    return ANSI_RE.sub("", text)


def visible_width(text: str) -> int:
    """Return visual monospace column width of text, ignoring ANSI escape codes."""
    clean = strip_ansi(text)
    return sum(
        2 if unicodedata.east_asian_width(c) in ("W", "F") else 0 if unicodedata.combining(c) else 1
        for c in clean
    )


def pad_cell(text: str, width: int, align: str = "left") -> str:
    """Pad text to visual width, preserving embedded ANSI escape sequences."""
    pad = " " * max(0, width - visible_width(text))
    return pad + text if align == "right" else text + pad


def format_table(
    headers: Sequence[str],
    rows: Sequence[Sequence[str]],
    gutter: int = 4,
) -> list[str]:
    """Format tabular data with dynamically calculated column widths and alignment."""
    th = get_theme()
    cols = len(headers)
    all_rows = [headers, *rows]
    widths = [max(visible_width(r[i]) for r in all_rows) for i in range(cols)]
    space = " " * gutter
    hdr_line = space.join(pad_cell(f"{th.d}{h}{th.r}", widths[i]) for i, h in enumerate(headers))
    div_line = f"{th.d}{'─' * (sum(widths) + gutter * (cols - 1))}{th.r}"
    row_lines = [space.join(pad_cell(r[i], widths[i]) for i in range(cols)) for r in rows]
    return [hdr_line, div_line, *row_lines]
