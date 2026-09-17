"""Shared fixtures and configuration for rig test suites."""

from __future__ import annotations

from pathlib import Path

import pytest

from rig import cli as rig

stack = rig
REPO_ROOT = Path(__file__).resolve().parents[1]
STACK_PATH = Path(rig.__file__).resolve()
CLI_PATH = STACK_PATH
RESOLVE_DOCKER_CONTEXT = rig.resolve_current_docker_context


@pytest.fixture(autouse=True)
def _no_ambient_docker_context(monkeypatch):
    """Keep the suite off this machine's own Docker context."""
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    monkeypatch.setattr(rig, "resolve_current_docker_context", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _isolate_rig_state_home(monkeypatch, tmp_path_factory):
    """Keep test instances isolated from the user's real state directory."""
    temp_state = tmp_path_factory.mktemp("rig_state")
    monkeypatch.setenv("RIG_STATE_HOME", str(temp_state))


@pytest.fixture
def write_compose_file():
    """Fixture to write a basic compose.yml into a target directory."""

    def _writer(root: Path | str, name: str = "compose.yml") -> Path:
        path = Path(root) / name
        path.write_text("services: {}\n")
        return path

    return _writer


def write_compose_file_helper(root: Path | str, name: str = "compose.yml") -> Path:
    """Helper function to write a basic compose.yml into a target directory."""
    path = Path(root) / name
    path.write_text("services: {}\n")
    return path
