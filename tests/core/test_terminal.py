"""Tests for terminal formatting, ANSI stripping, and table alignment."""

import io

from rig.core.terminal import (
    format_table,
    get_theme,
    pad_cell,
    strip_ansi,
    supports_color,
    visible_width,
)


def test_strip_ansi_removes_color_codes():
    styled = "\033[32m● running\033[0m"
    assert strip_ansi(styled) == "● running"


def test_visible_width_accounts_for_ansi_and_glyphs():
    styled = "\033[36mhttp://localhost:3000\033[0m"
    assert visible_width(styled) == 21
    # Full-width glyph (e.g. CJK or wide symbol)
    assert visible_width("中文") == 4
    # Combining character
    assert visible_width("e\u0301") == 1


def test_pad_cell_maintains_visual_alignment():
    styled = "\033[32m● running\033[0m"
    padded = pad_cell(styled, 15)
    assert visible_width(padded) == 15
    assert padded.startswith(styled)


def test_supports_color_respects_environment(monkeypatch):
    # Non-TTY stdout
    monkeypatch.setattr("sys.stdout", io.StringIO())
    assert supports_color() is False

    # Force isatty
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    assert supports_color() is True

    # NO_COLOR set
    monkeypatch.setenv("NO_COLOR", "1")
    assert supports_color() is False

    # TERM=dumb
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "dumb")
    assert supports_color() is False


def test_get_theme_toggle():
    colored = get_theme(color=True)
    assert colored.green != ""
    assert colored.magenta != ""
    assert colored.r != ""

    plain = get_theme(color=False)
    assert plain.green == ""
    assert plain.magenta == ""
    assert plain.r == ""


def test_format_table_aligns_columns():
    headers = ["STATUS", "SERVICE", "ENDPOINT"]
    rows = [
        ["\033[32m● running\033[0m", "api", "http://127.0.0.1:8000"],
        ["\033[31m○ stopped\033[0m", "worker", "-"],
    ]
    lines = format_table(headers, rows, gutter=2)
    assert len(lines) == 4  # header, div, row1, row2
    # Check that visible width of each row is consistent
    v_widths = [visible_width(line) for line in [lines[0], lines[2], lines[3]]]
    assert len(set(v_widths)) == 1


def test_format_human_error():
    from rig.core.errors import RigError, format_human_error

    err = RigError(
        "manifest not found: /tmp/rig.json",
        code="E_USAGE",
        exit_code=2,
        headline="No manifest found in this directory",
        context="Searched in: /tmp",
        hint="Run 'rig init' to scaffold a new manifest",
    )
    th = get_theme(color=False)
    output = format_human_error(err, th)
    assert "✖ No manifest found in this directory" in output
    assert "Searched in: /tmp" in output
    assert "Hint: Run 'rig init' to scaffold a new manifest" in output
    assert "[E_USAGE]" in output
