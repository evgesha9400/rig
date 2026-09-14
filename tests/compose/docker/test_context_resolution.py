"""R11-1: the Docker context resolver names the environment or the active context."""

import subprocess

import pytest

from rig import cli as rig

stack = rig
RESOLVE_DOCKER_CONTEXT = rig.resolve_current_docker_context


def test_resolve_docker_context_prefers_the_context_named_in_the_environment(monkeypatch):
    monkeypatch.setenv("DOCKER_CONTEXT", "orbstack")

    def _never(*args, **kwargs):
        raise AssertionError("docker must not be run while DOCKER_CONTEXT names a context")

    monkeypatch.setattr(subprocess, "run", _never)

    assert RESOLVE_DOCKER_CONTEXT() == "orbstack"


def test_resolve_docker_context_asks_docker_which_context_is_active(monkeypatch):
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        assert kwargs.get("timeout", 0) <= 5.0, "the probe must be bounded"
        return subprocess.CompletedProcess(argv, 0, "colima\n", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert RESOLVE_DOCKER_CONTEXT() == "colima"
    assert calls == [["docker", "context", "show"]]


@pytest.mark.parametrize(
    "outcome",
    [
        FileNotFoundError("docker"),
        subprocess.TimeoutExpired(["docker"], 5.0),
        subprocess.CompletedProcess(["docker"], 1, "", "cannot connect"),
        subprocess.CompletedProcess(["docker"], 0, "\n", ""),
    ],
)
def test_resolve_docker_context_reports_no_context_when_docker_cannot_answer(monkeypatch, outcome):
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)

    def fake_run(argv, **kwargs):
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert RESOLVE_DOCKER_CONTEXT() is None
