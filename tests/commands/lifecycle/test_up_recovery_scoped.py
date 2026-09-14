"""cmd_up: aborted recovery preserves state, and scoped recovery restarts dependents."""

import json

from rig import cli as rig

stack = rig


def test_cmd_up_aborts_and_preserves_state_when_dependent_stop_fails(monkeypatch, tmp_path):
    manifest_data = {
        "project": "testproj",
        "scopes": {"backend": ["backend"], "full": ["backend", "frontend"]},
        "services": {
            "backend": {
                "type": "fd",
                "cwd": ".",
                "command": ["echo", "backend"],
                "healthcheck_path": "/health",
            },
            "frontend": {
                "type": "port",
                "cwd": ".",
                "command": ["echo", "frontend"],
                "depends_on": ["backend"],
                "healthcheck_path": "/",
            },
        },
    }
    manifest_path = tmp_path / "stack.json"
    manifest_path.write_text(json.dumps(manifest_data))

    state_path = tmp_path / ".local-run" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "instance": "test",
        "generation": 1,
        "services": {
            "frontend": {
                "name": "frontend",
                "pid": 5555,
                "pgid": 5555,
                "port": 5173,
                "start_time": "12:00",
                "binary": "/bin/echo",
                "identity": "echo",
            }
        },
    }
    state_path.write_text(json.dumps(state))

    monkeypatch.setattr(stack, "pid_alive", lambda pid: True)
    monkeypatch.setattr(stack, "identity_matches", lambda rec: True)
    monkeypatch.setattr(stack, "_stop_record", lambda *args: "refused")

    code = stack.cmd_up(root=tmp_path, manifest_path=manifest_path, scope="full")
    assert code == 1

    saved_state = stack.read_state(state_path)
    assert "frontend" in saved_state["services"]
    assert saved_state["services"]["frontend"]["pid"] == 5555


def test_cmd_up_scoped_recovery_restarts_stopped_dependents(monkeypatch, tmp_path):
    manifest_data = {
        "project": "testproj",
        "scopes": {"backend": ["backend"], "full": ["backend", "frontend"]},
        "services": {
            "backend": {
                "type": "fd",
                "cwd": ".",
                "command": ["echo", "backend"],
                "healthcheck_path": "/health",
            },
            "frontend": {
                "type": "port",
                "cwd": ".",
                "command": ["echo", "frontend"],
                "depends_on": ["backend"],
                "healthcheck_path": "/",
            },
        },
    }
    manifest_path = tmp_path / "stack.json"
    manifest_path.write_text(json.dumps(manifest_data))

    state_path = tmp_path / ".local-run" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "instance": "test",
        "generation": 1,
        "services": {
            "frontend": {
                "name": "frontend",
                "pid": 5555,
                "pgid": 5555,
                "port": 5173,
                "start_time": "12:00",
                "binary": "/bin/echo",
                "identity": "echo",
            }
        },
    }
    state_path.write_text(json.dumps(state))

    monkeypatch.setattr(stack, "pid_alive", lambda pid: True)
    monkeypatch.setattr(stack, "identity_matches", lambda rec: True)
    monkeypatch.setattr(stack, "_stop_record", lambda *args: "terminated")

    started = []

    def mock_start(service, root, runtime, instance, state, state_path):
        rec = {"name": service.name, "pid": 6000 + len(started), "port": 8000 + len(started)}
        started.append(service.name)
        state["services"][service.name] = rec
        return rec

    monkeypatch.setattr(stack, "_start_with_retry", mock_start)

    code = stack.cmd_up(root=tmp_path, manifest_path=manifest_path, scope="backend")
    assert code == 0

    assert started == ["backend", "frontend"]
    saved_state = stack.read_state(state_path)
    assert "backend" in saved_state["services"]
    assert "frontend" in saved_state["services"]
