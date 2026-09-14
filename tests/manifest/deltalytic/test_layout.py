"""Deltalytic repository layout: Makefile targets and runtime directory hygiene."""

from pathlib import Path

from rig import cli as rig

stack = rig

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_repository_makefile_include_exposes_every_symmetrical_target():
    text = (REPO_ROOT / "examples" / "stack.mk").read_text()

    for target in (
        "up",
        "down",
        "local-up",
        "local-down",
        "backend-up",
        "backend-down",
        "ui-up",
        "ui-down",
        "status",
    ):
        assert f"\n{target}:" in text, f"Makefile is missing target {target}"
    phony_lines = [line for line in text.splitlines() if ".PHONY" in line or line.startswith(" ")]
    phony_all = " ".join(phony_lines)
    for target in ("up", "down", "local-up", "ui-down", "status"):
        assert target in phony_all


def test_repository_makefile_includes_the_stack_targets():
    text = (REPO_ROOT / "examples" / "stack.mk").read_text()

    assert "STACK_RUNNER" in text
    assert "scripts/stack.py" in text


def test_runtime_directory_is_ignored_by_version_control():
    assert ".local-run" in (REPO_ROOT / ".gitignore").read_text()


def test_makefile_default_goal_is_help():
    text = (REPO_ROOT / "Makefile").read_text()
    assert ".DEFAULT_GOAL := help" in text
