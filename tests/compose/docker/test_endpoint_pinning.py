"""R9-2: a record's pinned Docker endpoint outranks the ambient environment."""

import subprocess
from pathlib import Path

from rig import cli as rig

stack = rig


def _compose_record(root, container="abc123", compose_file="compose.yml", name="db"):
    return {
        "name": name,
        "type": "compose",
        "instance": "inst-1",
        "compose_file": str(Path(root) / compose_file),
        "compose_service": name,
        "container": container,
    }


def _capture_docker_env(monkeypatch):
    seen: list[dict[str, str]] = []

    def fake_run(argv, **kwargs):
        seen.append(dict(kwargs.get("env") or {}))
        if "ps" in argv:
            return subprocess.CompletedProcess(argv, 0, "abc123\n", "")
        if "inspect" in argv:
            return subprocess.CompletedProcess(argv, 0, "running\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return seen


def test_a_record_pinned_to_the_local_daemon_ignores_an_ambient_docker_host(monkeypatch, tmp_path):
    monkeypatch.setenv("DOCKER_HOST", "tcp://appeared-later:2375")
    seen = _capture_docker_env(monkeypatch)
    record = dict(_compose_record(tmp_path / "deleted"), docker_host=None)

    assert stack.docker_record_status(record) == "alive"
    assert seen and all("DOCKER_HOST" not in env for env in seen)


def test_a_record_without_a_pinned_endpoint_inherits_the_ambient_daemon(monkeypatch, tmp_path):
    monkeypatch.setenv("DOCKER_HOST", "tcp://ambient:2375")
    seen = _capture_docker_env(monkeypatch)
    record = _compose_record(tmp_path / "deleted")
    assert "docker_host" not in record

    assert stack.docker_record_status(record) == "alive"
    assert seen and all(env.get("DOCKER_HOST") == "tcp://ambient:2375" for env in seen)


def test_compose_status_and_teardown_pin_the_recorded_endpoint(monkeypatch, tmp_path):
    (tmp_path / "compose.yml").write_text("services: {}\n")
    calls: list[dict] = []

    def fake_run_compose(
        instance, root, cfile, args, context=None, timeout=180.0, env=None, **kwargs
    ):
        calls.append({"args": list(args), "kwargs": dict(kwargs)})
        return subprocess.CompletedProcess(["docker"], 0, "abc123\n", "")

    monkeypatch.setattr(stack, "run_compose", fake_run_compose)
    monkeypatch.setattr(
        stack,
        "run_docker",
        lambda *a, **k: subprocess.CompletedProcess(["docker"], 0, "running\n", ""),
    )
    record = dict(
        _compose_record(tmp_path), docker_host="tcp://remote:2375", docker_context="colima"
    )

    assert stack.compose_record_status(record, tmp_path) == "alive"
    assert stack._stop_record(record, tmp_path) == "terminated"
    assert calls
    assert all(call["kwargs"]["docker_host"] == "tcp://remote:2375" for call in calls)


def test_stop_record_pins_the_endpoint_of_a_deleted_checkout(monkeypatch, tmp_path):
    monkeypatch.delenv("DOCKER_HOST", raising=False)

    def _never(*args, **kwargs):
        raise AssertionError("run_compose must not run without a compose file")

    monkeypatch.setattr(stack, "run_compose", _never)
    seen = _capture_docker_env(monkeypatch)
    record = dict(_compose_record(tmp_path / "deleted"), docker_host="tcp://remote:2375")

    assert stack._stop_record(record, tmp_path / "deleted") == "terminated"
    assert seen and all(env.get("DOCKER_HOST") == "tcp://remote:2375" for env in seen)


def test_a_pinned_context_outranks_the_context_named_in_the_environment(monkeypatch, tmp_path):
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setenv("DOCKER_CONTEXT", "desktop-linux")
    seen = _capture_docker_env(monkeypatch)
    record = dict(_compose_record(tmp_path / "deleted"), docker_context="colima", docker_host=None)

    assert stack.docker_record_status(record) == "alive"
    assert seen and all("DOCKER_CONTEXT" not in env for env in seen)
