"""R3-1/R5-1: Compose up failures and forced reclaims record or drop state correctly."""

import subprocess
from pathlib import Path

import pytest

from rig import cli as rig

stack = rig


def _compose_service(name="db", **kwargs):
    return stack.Service(
        name=name, type="compose", compose_file="compose.yml", compose_service=name, **kwargs
    )


def _write_compose_file(root, name="compose.yml"):
    path = Path(root) / name
    path.write_text("services: {}\n")
    return path


def test_compose_up_failure_records_partial_when_cleanup_fails(monkeypatch, tmp_path):
    """R3-1: a timed-out `up` whose cleanup fails must still publish the partial record."""
    service = _compose_service()
    calls: list[list[str]] = []

    def fake_run_compose(
        instance, root, cfile, args, context=None, timeout=180.0, env=None, **kwargs
    ):
        calls.append(list(args))
        if args[0] == "up":
            raise stack.StackError("compose command timed out: up")
        return subprocess.CompletedProcess(["docker"], 1, "", "cleanup refused")

    monkeypatch.setattr(stack, "run_compose", fake_run_compose)

    with pytest.raises(stack.RigError) as exc_info:
        stack._start_compose_service(service, tmp_path, "inst-1")

    partial = exc_info.value.details.get("partial_record")
    assert partial is not None
    assert partial["name"] == "db"
    assert partial["type"] == "compose"
    assert exc_info.value.__cause__ is not None
    assert [c[0] for c in calls].count("up") == 1


def test_compose_up_failure_reraises_when_cleanup_succeeds(monkeypatch, tmp_path):
    """R3-1: a clean reclaim keeps the original failure and records nothing."""
    service = _compose_service()

    def fake_run_compose(
        instance, root, cfile, args, context=None, timeout=180.0, env=None, **kwargs
    ):
        if args[0] == "up":
            raise stack.StackError("compose command timed out: up")
        return subprocess.CompletedProcess(["docker"], 0, "", "")

    monkeypatch.setattr(stack, "run_compose", fake_run_compose)

    with pytest.raises(stack.StackError) as exc_info:
        stack._start_compose_service(service, tmp_path, "inst-1")
    assert "timed out" in str(exc_info.value)
    assert exc_info.value.details.get("partial_record") is None


def _reclaimable_compose_state(instance: str = "recl-00000000") -> dict:
    return {
        "instance": instance,
        "project": "recl",
        "services": {
            "db": {
                "name": "db",
                "type": "compose",
                "instance": instance,
                "compose_file": "compose.yml",
                "compose_service": "db",
                "container": "c0ffee",
            }
        },
    }


def test_force_stop_removes_compose_container_before_dropping_record(monkeypatch, tmp_path):
    """R5-1: a reclaimed compose service must lose its container, not just be stopped."""
    _write_compose_file(tmp_path)
    calls: list[list[str]] = []

    def fake_run_compose(
        instance, root, cfile, args, context=None, timeout=180.0, env=None, **kwargs
    ):
        calls.append(list(args))
        if args and args[0] == "ps":
            return subprocess.CompletedProcess(["docker"], 0, "c0ffee\n", "")
        return subprocess.CompletedProcess(["docker"], 0, "", "")

    monkeypatch.setattr(stack, "run_compose", fake_run_compose)
    monkeypatch.setattr(
        stack,
        "run_docker",
        lambda *a, **k: subprocess.CompletedProcess(["docker"], 0, "exited\n", ""),
    )

    state = _reclaimable_compose_state()
    failures = stack._force_stop_instance(state, tmp_path / "state.json", tmp_path)

    assert failures == []
    assert state["services"] == {}
    assert ["stop", "db"] in calls
    assert ["rm", "-f", "db"] in calls
    assert not any("-v" in args for args in calls)
