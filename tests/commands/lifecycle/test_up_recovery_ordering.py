"""cmd_up: stop ordering during recovery, and mode conflicts when Compose is unreachable."""

import json
import subprocess

import pytest

from rig import cli as rig

stack = rig


def test_cmd_up_recovery_stops_dependents_before_dependencies_and_preserves(monkeypatch, tmp_path):
    manifest_path = tmp_path / "stack.json"
    manifest_path.write_text(
        json.dumps(
            {
                "project": "sample",
                "services": {
                    "svc_a": {"type": "port", "cwd": ".", "command": ["echo"]},
                    "svc_b": {
                        "type": "port",
                        "cwd": ".",
                        "command": ["echo"],
                        "depends_on": ["svc_a"],
                    },
                    "svc_c": {
                        "type": "port",
                        "cwd": ".",
                        "command": ["echo"],
                        "depends_on": ["svc_b"],
                    },
                },
                "scopes": {"full": ["svc_a", "svc_b", "svc_c"]},
            }
        )
    )
    state = {
        "instance": "sample-inst",
        "services": {
            "svc_b": {"name": "svc_b", "type": "port", "pid": 102, "pgid": 102, "port": 8002},
            "svc_c": {"name": "svc_c", "type": "port", "pid": 103, "pgid": 103, "port": 8003},
        },
    }
    state_file = stack._state_path(tmp_path)
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(state))

    monkeypatch.setattr(stack, "pid_alive", lambda pid: True)
    monkeypatch.setattr(stack, "pgid_alive", lambda pgid: True)
    monkeypatch.setattr(stack, "identity_matches", lambda rec: True)
    monkeypatch.setattr(stack, "is_service_verifiable_alive", lambda rec, root: True)

    stopped_order = []

    def mock_stop(record, root):
        name = record["name"]
        stopped_order.append(name)
        if name == "svc_c":
            return "refused"
        return "terminated"

    monkeypatch.setattr(stack, "_stop_record", mock_stop)

    ret = stack.cmd_up(root=tmp_path, manifest_path=manifest_path, scope="full")
    assert ret == 1
    assert stopped_order == ["svc_c"]
    reloaded = json.loads(state_file.read_text())
    assert "svc_b" in reloaded["services"]
    assert "svc_c" in reloaded["services"]


def test_cmd_up_mode_conflict_holds_when_compose_status_is_unknown(
    monkeypatch, tmp_path, write_compose_file
):
    """A Compose record Docker cannot inspect still occupies the active mode."""
    monkeypatch.setenv("RIG_STATE_HOME", str(tmp_path / "state"))
    write_compose_file(tmp_path)
    manifest_path = tmp_path / "rig.json"
    manifest_path.write_text(
        json.dumps(
            {
                "project": "mode-compose",
                "default_mode": "native",
                "modes": {
                    "native": {
                        "services": {"api": {"type": "port", "cwd": ".", "command": ["echo"]}}
                    },
                    "container": {
                        "services": {"api": {"type": "port", "cwd": ".", "command": ["echo"]}}
                    },
                },
            }
        )
    )

    state_path = stack._state_path(tmp_path)
    stack.write_state(
        state_path,
        {
            "instance": stack.instance_id("mode-compose", tmp_path),
            "generation": 1,
            "mode": "native",
            "services": {
                "db": {
                    "name": "db",
                    "type": "compose",
                    "pid": None,
                    "pgid": None,
                    "instance": "inst-1",
                    "compose_file": str(tmp_path / "compose.yml"),
                    "compose_service": "db",
                    "container": "abc123",
                }
            },
        },
    )

    monkeypatch.setattr(
        stack,
        "run_compose",
        lambda *a, **k: subprocess.CompletedProcess(["docker"], 1, "", "daemon down"),
    )
    monkeypatch.setattr(
        stack,
        "run_docker",
        lambda *a, **k: subprocess.CompletedProcess(
            ["docker"], 1, "", "cannot connect to the Docker daemon"
        ),
    )

    with pytest.raises(stack.RigError) as exc_info:
        stack.cmd_up(tmp_path, manifest_path, mode="container", switch=False)
    assert exc_info.value.code == "E_MODE_CONFLICT"
    assert exc_info.value.exit_code == stack.EXIT_MUTEX_CONFLICT
