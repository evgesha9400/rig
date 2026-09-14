"""cmd_down: a dependency edge recorded only in state must still block teardown."""

import json

from rig import cli as rig

stack = rig


def test_cmd_down_refuses_when_only_state_records_the_dependency(monkeypatch, tmp_path):
    """A dependent known only to state must still block teardown."""
    monkeypatch.setenv("RIG_STATE_HOME", str(tmp_path / "state"))
    manifest_path = tmp_path / "rig.json"
    manifest_path.write_text(
        json.dumps(
            {
                "project": "deps",
                "scopes": {"full": ["db"]},
                "services": {"db": {"type": "port", "cwd": ".", "command": ["echo"]}},
            }
        )
    )

    state_path = stack._state_path(tmp_path)
    stack.write_state(
        state_path,
        {
            "instance": "deps",
            "generation": 1,
            "services": {
                "db": {"name": "db", "type": "port", "pid": 11, "pgid": 11},
                "api": {
                    "name": "api",
                    "type": "port",
                    "pid": 12,
                    "pgid": 12,
                    "depends_on": ["db"],
                },
            },
        },
    )

    stopped: list[str] = []
    monkeypatch.setattr(
        stack, "_stop_record", lambda rec, root: stopped.append(rec["name"]) or "terminated"
    )

    exit_code = stack.cmd_down(root=tmp_path, manifest_path=manifest_path, scope="full")
    assert exit_code == stack.EXIT_REFUSED
    assert stopped == []
    assert "db" in stack.read_state(state_path)["services"]


def test_cmd_down_preserves_dependency_for_recorded_only_dependent(monkeypatch, tmp_path):
    """A recorded dependency edge also protects a dependency mid-teardown."""
    monkeypatch.setenv("RIG_STATE_HOME", str(tmp_path / "state"))
    manifest_path = tmp_path / "rig.json"
    manifest_path.write_text(
        json.dumps(
            {
                "project": "deps2",
                "scopes": {"full": ["db", "api"]},
                "services": {
                    "db": {"type": "port", "cwd": ".", "command": ["echo"]},
                    "api": {"type": "port", "cwd": ".", "command": ["echo"]},
                },
            }
        )
    )

    state_path = stack._state_path(tmp_path)
    stack.write_state(
        state_path,
        {
            "instance": "deps2",
            "generation": 1,
            "services": {
                "db": {"name": "db", "type": "port", "pid": 11, "pgid": 11},
                "api": {
                    "name": "api",
                    "type": "port",
                    "pid": 12,
                    "pgid": 12,
                    "depends_on": ["db"],
                },
            },
        },
    )

    attempted: list[str] = []

    def fake_stop(record, root):
        attempted.append(record["name"])
        return "failed" if record["name"] == "api" else "terminated"

    monkeypatch.setattr(stack, "_stop_record", fake_stop)

    exit_code = stack.cmd_down(root=tmp_path, manifest_path=manifest_path, scope="full")
    assert exit_code == stack.EXIT_OP_FAILED
    assert attempted == ["api"]
    saved = stack.read_state(state_path)["services"]
    assert "db" in saved and "api" in saved
