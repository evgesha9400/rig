"""Tests for runtime directory setup and readiness/ownership checks during recovery."""

import json
import subprocess
from pathlib import Path

from rig import cli as rig

stack = rig

SAMPLE = {
    "project": "sample",
    "services": {"backend": {"type": "fd", "cwd": ".", "command": ["true", "--fd", "{fd}"]}},
    "scopes": {"full": ["backend"], "local": ["backend"], "backend": ["backend"]},
}


def test_ensure_runtime_dir_creates_data_directory(tmp_path: Path):
    runtime = stack.ensure_runtime_dir(tmp_path)
    assert runtime.is_dir()
    assert (tmp_path / "data").is_dir()


def test_await_ready_fails_when_pid_dies_despite_http_success(monkeypatch):
    service = stack.Service(
        name="test",
        type="port",
        cwd=Path("."),
        command=["echo"],
        healthcheck_path="/health",
        healthcheck_timeout=1.0,
    )
    record = {"pid": 12345, "port": 8080}
    # PID is dead
    monkeypatch.setattr(stack, "pid_alive", lambda pid: False)
    assert not stack._await_ready(service, record)


def test_start_with_retry_preserves_record_if_cleanup_fails(monkeypatch, tmp_path):
    service = stack.Service(
        name="backend",
        type="fd",
        cwd=Path("."),
        command=["echo"],
        healthcheck_path="/health",
        healthcheck_timeout=0.1,
    )
    state = {"services": {}}
    state_path = tmp_path / "state.json"

    # Mock service startup so it returns a dummy record
    dummy_record = {"name": "backend", "pid": 12345, "port": 8080}
    monkeypatch.setattr(stack, "_start_service", lambda *args: dummy_record)
    # Mock readiness failure
    monkeypatch.setattr(stack, "_await_ready", lambda *args: False)
    # Mock cleanup returning "failed"
    monkeypatch.setattr(stack, "_stop_record", lambda *args: "failed")

    result = stack._start_with_retry(service, tmp_path, tmp_path, "inst-1", state, state_path)
    assert result is None
    # Record must be preserved in state because cleanup failed!
    assert "backend" in state["services"]
    assert state["services"]["backend"]["pid"] == 12345


def test_await_ready_rejects_foreign_port_listener(monkeypatch):
    service = stack.Service(
        name="frontend",
        type="port",
        cwd=Path("."),
        command=["echo"],
        healthcheck_path="/",
        healthcheck_timeout=1.0,
    )
    record = {"name": "frontend", "pid": 1234, "pgid": 1234, "port": 5173}

    monkeypatch.setattr(stack, "pid_alive", lambda pid: True)
    monkeypatch.setattr(stack, "identity_matches", lambda rec: True)
    monkeypatch.setattr(stack, "wait_for_http", lambda *args, **kwargs: True)
    # Foreign listener detected on port 5173!
    monkeypatch.setattr(stack, "port_listener_matches", lambda *args, **kwargs: False)

    assert not stack._await_ready(service, record)


def test_port_listener_matches_requires_positive_ownership(monkeypatch):
    import shutil

    # Missing lsof returns False
    monkeypatch.setattr(shutil, "which", lambda *_: None)
    assert not stack.port_listener_matches(8080, pgid=1234, pid=1234)

    # Subprocess error/timeout returns False
    monkeypatch.setattr(shutil, "which", lambda *_: "/usr/bin/lsof")

    def mock_run_error(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="lsof", timeout=1.0)

    monkeypatch.setattr(subprocess, "run", mock_run_error)
    assert not stack.port_listener_matches(8080, pgid=1234, pid=1234)

    # Empty stdout / no listeners returns False
    mock_empty = subprocess.CompletedProcess(args=["lsof"], returncode=0, stdout="", stderr="")
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: mock_empty)
    assert not stack.port_listener_matches(8080, pgid=1234, pid=1234)


def test_status_and_prune_state_preserves_dead_leader_with_living_pg(monkeypatch, tmp_path):
    manifest_path = tmp_path / "stack.json"
    manifest_path.write_text(json.dumps(SAMPLE))
    runtime = stack.ensure_runtime_dir(tmp_path)
    state = {
        "generation": 1,
        "services": {
            "backend": {
                "name": "backend",
                "pid": 1111,
                "pgid": 1111,
                "port": 8080,
                "start_time": "12:00",
                "binary": "/bin/echo",
                "identity": "echo",
            }
        },
    }
    stack.write_state(runtime / "state.json", state)

    # Leader dead, but surviving children in pgid
    monkeypatch.setattr(stack, "record_alive", lambda rec, root: False)
    monkeypatch.setattr(stack, "pgid_alive", lambda pgid: True)

    assert stack.prune_state(state, tmp_path) == []
    assert "backend" in state["services"]

    code = stack.cmd_status(root=tmp_path, manifest_path=manifest_path)
    assert code == 0
    # State must NOT have had backend removed!
    saved = stack.read_state(runtime / "state.json")
    assert "backend" in saved["services"]
