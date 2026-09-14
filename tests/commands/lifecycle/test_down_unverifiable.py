"""cmd_down: a stop failure must be reported, not silently emptied from state."""

import json
import os

from rig import cli as rig

stack = rig


def test_cmd_down_preserves_unverifiable_record_and_reports_failure(monkeypatch, tmp_path):
    manifest_data = {
        "project": "testproj",
        "scopes": {"full": ["backend"]},
        "services": {
            "backend": {
                "type": "fd",
                "cwd": ".",
                "command": ["echo", "backend"],
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
            "backend": {
                "name": "backend",
                "pid": 1234,
                "pgid": 1234,
                "port": 8080,
            }
        },
    }
    state_path.write_text(json.dumps(state))

    monkeypatch.setattr(stack, "pid_alive", lambda pid: False)
    monkeypatch.setattr(stack, "pgid_alive", lambda pgid: True)
    monkeypatch.setattr(os, "getpgid", lambda target: 99999 if target == 0 else 1234)

    exit_code = stack.cmd_down(root=tmp_path, manifest_path=manifest_path, scope="full")
    assert exit_code == 1
    saved_state = stack.read_state(state_path)
    assert "backend" in saved_state["services"]


def test_cmd_down_preserves_dependencies_when_dependent_stop_fails(monkeypatch, tmp_path):
    manifest_data = {
        "project": "testproj",
        "scopes": {"full": ["backend", "frontend"]},
        "services": {
            "backend": {"type": "fd", "cwd": ".", "command": ["echo", "backend"]},
            "frontend": {
                "type": "port",
                "cwd": ".",
                "command": ["echo", "frontend"],
                "depends_on": ["backend"],
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
            "backend": {
                "name": "backend",
                "pid": 1111,
                "pgid": 1111,
                "port": 8080,
                "start_time": "12:00",
                "binary": "/bin/echo",
                "identity": "echo",
            },
            "frontend": {
                "name": "frontend",
                "pid": 2222,
                "pgid": 2222,
                "port": 5173,
                "start_time": "12:00",
                "binary": "/bin/echo",
                "identity": "echo",
            },
        },
    }
    state_path.write_text(json.dumps(state))

    monkeypatch.setattr(stack, "pid_alive", lambda pid: True)
    monkeypatch.setattr(stack, "identity_matches", lambda rec: True)

    stopped = []

    def mock_stop(record, root):
        name = record["name"]
        stopped.append(name)
        if name == "frontend":
            return "refused"
        return "terminated"

    monkeypatch.setattr(stack, "_stop_record", mock_stop)

    exit_code = stack.cmd_down(root=tmp_path, manifest_path=manifest_path, scope="full")
    assert exit_code == 1

    assert "backend" not in stopped
    saved_state = stack.read_state(state_path)
    assert "backend" in saved_state["services"]
    assert "frontend" in saved_state["services"]
