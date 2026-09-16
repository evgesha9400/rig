"""Tests for version alignment and --version CLI flag."""

from pathlib import Path

import pytest
import tomllib

import rig
from rig.core.constants import __version__
from rig.parser import build_parser


def test_constants_version_matches_pyproject_toml():
    """Ensure hardcoded __version__ never drifts from pyproject.toml."""
    pyproject_path = Path(__file__).parents[2] / "pyproject.toml"
    data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    pyproject_version = data["project"]["version"]
    assert __version__ == pyproject_version
    assert rig.__version__ == pyproject_version


def test_parser_version_flag_exits_with_version_string(capsys):
    """Ensure `rig --version` displays the current program version."""
    parser = build_parser()
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["--version"])
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert f"rig {__version__}" in captured.out
