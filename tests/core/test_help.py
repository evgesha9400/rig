"""Tests for CLI help output, parameter documentation, and formatting."""

import pytest

from rig.parser import build_parser


def test_main_help_output():
    """Ensure `rig --help` displays description, options, commands, and epilog."""
    parser = build_parser()
    help_text = parser.format_help()
    assert "Local dev environment and multi-service process runner." in help_text
    assert "commands:" in help_text
    assert "options:" in help_text
    assert "Use 'rig <command> --help' for details" in help_text


def test_every_action_has_help_text():
    """Verify that all options and subcommand arguments define help documentation."""
    parser = build_parser()
    for action in parser._actions:
        if action.dest != "command":
            assert action.help, f"Global option {action.dest} missing help text"

    sub = next(a for a in parser._actions if a.dest == "command")
    for choice_action in sub._choices_actions:
        assert choice_action.help, f"Subcommand {choice_action.dest} missing help text"

    for cmd_name, subparser in sub.choices.items():
        for action in subparser._actions:
            assert action.help, f"Command '{cmd_name}' option {action.dest} missing help text"


def test_subcommand_help_execution(capsys):
    """Ensure `rig <subcommand> --help` exits cleanly with code 0."""
    parser = build_parser()
    for cmd in ("up", "down", "status", "ps", "prune", "check", "init", "schema", "logs"):
        with pytest.raises(SystemExit) as exc_info:
            parser.parse_args([cmd, "--help"])
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert f"usage: rig {cmd}" in captured.out
