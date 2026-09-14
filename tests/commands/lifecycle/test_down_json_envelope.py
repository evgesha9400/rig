"""cmd_down: JSON envelope failure reporting and merged-dependency ordering."""

import json

from rig import cli as rig

stack = rig


def _write_instance(ident: str, state: dict):
    inst_dir = stack.ensure_instance_dir(ident)
    stack.write_state(inst_dir / stack.STATE_FILE_NAME, state)
    return inst_dir


def test_down_json_envelope_reports_failure(monkeypatch, tmp_path, capsys):
    """A failed teardown must not be reported as ok in the JSON envelope."""
    monkeypatch.setenv("RIG_STATE_HOME", str(tmp_path / "rig_state"))
    _write_instance(
        "fail-00000000",
        {
            "instance": "fail-00000000",
            "project": "fail",
            "root": str(tmp_path),
            "services": {"api": {"name": "api", "type": "port", "pid": 7, "pgid": 7}},
        },
    )
    monkeypatch.setattr(stack, "_stop_record", lambda rec, root: "failed")

    assert stack.cmd_down(target="fail-00000000", as_json=True) == stack.EXIT_OP_FAILED
    single = json.loads(capsys.readouterr().out)
    assert single["ok"] is False

    assert stack.cmd_down(all_instances=True, as_json=True) == stack.EXIT_OP_FAILED
    every = json.loads(capsys.readouterr().out)
    assert every["ok"] is False


def test_down_json_envelope_reports_local_failure(monkeypatch, tmp_path, capsys):
    """The local checkout path must also mark a failed teardown."""
    monkeypatch.setenv("RIG_STATE_HOME", str(tmp_path / "rig_state"))
    manifest_path = tmp_path / "rig.json"
    manifest_path.write_text(
        json.dumps(
            {"project": "local-fail", "services": {"api": {"type": "port", "command": ["echo"]}}}
        )
    )
    instance = stack.instance_id("local-fail", tmp_path)
    stack.write_state(
        stack._state_path(tmp_path, instance=instance),
        {
            "instance": instance,
            "project": "local-fail",
            "root": str(tmp_path),
            "services": {"api": {"name": "api", "type": "port", "pid": 7, "pgid": 7}},
        },
    )
    monkeypatch.setattr(stack, "_stop_record", lambda rec, root: "failed")

    assert stack.cmd_down(tmp_path, manifest_path, as_json=True) == stack.EXIT_OP_FAILED
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["ok"] is False
    assert envelope["data"]["failures"]


def test_cmd_down_local_orders_by_merged_dependencies(monkeypatch, tmp_path):
    """`cmd_down` orders targets using merged dependency graph from state and manifest."""
    manifest_path = tmp_path / "rig.json"
    manifest_path.write_text(
        json.dumps(
            {
                "project": "merged_down",
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
            "instance": "merged_down",
            "generation": 1,
            "services": {
                "db": {"name": "db", "type": "port", "pid": 201, "pgid": 201},
                "api": {
                    "name": "api",
                    "type": "port",
                    "pid": 202,
                    "pgid": 202,
                    "depends_on": ["db"],
                },
            },
        },
    )

    stopped_order: list[str] = []

    def fake_stop(record, root):
        stopped_order.append(record["name"])
        return "terminated"

    monkeypatch.setattr(stack, "_stop_record", fake_stop)

    exit_code = stack.cmd_down(root=tmp_path, manifest_path=manifest_path, scope="full")
    assert exit_code == 0
    assert stopped_order == ["api", "db"]
