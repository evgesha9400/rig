"""R2-6/R4-1: compose start persists a partial record and passes `--no-deps`."""

import subprocess
from pathlib import Path

import pytest

from rig import cli as rig

stack = rig


def test_compose_partial_start_persists_container_when_cleanup_fails(monkeypatch, tmp_path):
    """R2-6: an undiscoverable Compose container must stay recorded for teardown."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text("services:\n  db:\n    image: postgres:16\n")
    service = stack.Service(
        name="db",
        type="compose",
        compose_file="compose.yml",
        compose_service="db",
        compose_port=5432,
    )
    calls: list[list[str]] = []

    def fake_run_compose(
        instance, root, cfile, args, context=None, timeout=180.0, env=None, **kwargs
    ):
        calls.append(list(args))
        verb = args[0]
        if verb == "up":
            return subprocess.CompletedProcess(["docker"], 0, "", "")
        if verb == "ps":
            return subprocess.CompletedProcess(["docker"], 0, "deadbeefcafe\n", "")
        if verb == "port":
            return subprocess.CompletedProcess(["docker"], 1, "", "no such port")
        return subprocess.CompletedProcess(["docker"], 1, "", "cleanup refused")

    monkeypatch.setattr(stack, "run_compose", fake_run_compose)

    with pytest.raises(stack.RigError) as exc_info:
        stack._start_compose_service(service, tmp_path, "inst-1")

    verbs = [c[0] for c in calls]
    assert "stop" in verbs and "rm" in verbs
    partial = exc_info.value.details.get("partial_record")
    assert partial is not None
    assert partial["container"] == "deadbeefcafe"

    state = {"generation": 0, "services": {}}
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(
        stack, "_start_service", lambda *a, **k: (_ for _ in ()).throw(exc_info.value)
    )
    with pytest.raises(stack.RigError):
        stack._start_with_retry(service, tmp_path, tmp_path, "inst-1", state, state_path)
    assert state["services"]["db"]["container"] == "deadbeefcafe"


def test_compose_up_passes_no_deps(monkeypatch, tmp_path):
    """R4-1: `compose up` must pass `--no-deps` to prevent starting unrecorded dependencies."""
    compose_calls: list[list[str]] = []

    def fake_compose(instance, root, cfile, args, context=None, timeout=180.0, env=None, **kwargs):
        compose_calls.append(list(args))
        if "up" in args:
            return subprocess.CompletedProcess(["docker"], 0, "", "")
        if "ps" in args:
            return subprocess.CompletedProcess(["docker"], 0, "c123\n", "")
        if "port" in args:
            return subprocess.CompletedProcess(["docker"], 0, "0.0.0.0:5432\n", "")
        return subprocess.CompletedProcess(["docker"], 0, "", "")

    monkeypatch.setattr(stack, "run_compose", fake_compose)
    monkeypatch.setattr(stack, "port_is_free", lambda p: True)

    service = stack.Service(
        name="db",
        type="compose",
        cwd=Path("."),
        compose_file="docker-compose.yml",
        compose_service="db",
        compose_port=5432,
    )
    record = stack._start_compose_service(service, tmp_path, "inst1")
    assert record["container"] == "c123"
    up_calls = [c for c in compose_calls if "up" in c]
    assert up_calls, "Expected a compose up call"
    assert "--no-deps" in up_calls[0]
