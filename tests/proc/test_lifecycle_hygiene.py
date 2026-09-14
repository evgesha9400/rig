"""Tests for rollback ordering and unverifiable-service rejection during `up`."""

import json
from pathlib import Path

from rig import cli as rig

stack = rig


def test_rollback_preserves_dependencies_when_dependent_cleanup_fails(monkeypatch, tmp_path):
    manifest = stack.Manifest(
        project="sample",
        services={
            "backend": stack.Service("backend", "fd", Path("."), ["echo"]),
            "frontend": stack.Service(
                "frontend", "port", Path("."), ["echo"], depends_on=["backend"]
            ),
        },
        scopes={"full": ["backend", "frontend"]},
        path=tmp_path / "stack.json",
    )
    state_path = tmp_path / "state.json"
    state = {
        "services": {
            "backend": {"name": "backend", "pid": 1111, "pgid": 1111},
            "frontend": {"name": "frontend", "pid": 2222, "pgid": 2222},
        }
    }
    stack.write_state(state_path, state)

    stopped = []

    def mock_stop(record, root):
        name = record["name"]
        stopped.append(name)
        if name == "frontend":
            return "refused"
        return "terminated"

    monkeypatch.setattr(stack, "_stop_record", mock_stop)

    # Rollback started services [backend, frontend]
    stack._rollback(state, state_path, ["backend", "frontend"], tmp_path, manifest)

    # Frontend cleanup was attempted and failed; backend must NOT be stopped!
    assert "backend" not in stopped
    assert "backend" in state["services"]
    assert "frontend" in state["services"]


def test_up_rejects_unverifiable_existing_services(monkeypatch, tmp_path):
    manifest_path = tmp_path / "stack.json"
    manifest_path.write_text(
        json.dumps(
            {
                "project": "sample",
                "services": {
                    "backend": {
                        "type": "fd",
                        "cwd": ".",
                        "command": ["echo"],
                    },
                    "frontend": {
                        "type": "port",
                        "cwd": ".",
                        "command": ["echo"],
                        "depends_on": ["backend"],
                    },
                },
                "scopes": {"full": ["backend", "frontend"]},
            }
        )
    )
    state = {
        "instance": "sample-inst",
        "services": {
            "backend": {
                "name": "backend",
                "type": "fd",
                "pid": 999999,
                "pgid": 999999,
                "port": 8000,
            }
        },
    }
    state_file = stack._state_path(tmp_path)
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(state))

    # Backend leader is dead and unverifiable
    monkeypatch.setattr(stack, "pid_alive", lambda pid: False)
    monkeypatch.setattr(stack, "pgid_alive", lambda pgid: True)

    ret = stack.cmd_up(root=tmp_path, manifest_path=manifest_path, scope="full")
    assert ret == 1
    # State should still preserve backend ownership evidence
    reloaded = json.loads(state_file.read_text())
    assert "backend" in reloaded["services"]
