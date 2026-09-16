"""cmd_ps and cmd_status: empty/populated registries and running vs stopped records."""

import json

from rig import cli as rig

stack = rig


def test_cmd_ps_empty_and_populated(monkeypatch, tmp_path, capsys):
    state_home = tmp_path / "rig_state"
    monkeypatch.setenv("RIG_STATE_HOME", str(state_home))

    ret = stack.cmd_ps(as_json=True)
    assert ret == 0
    captured = capsys.readouterr()
    res = json.loads(captured.out)
    assert res["schema"] == "rig.ps/1"
    assert res["ok"] is True
    assert res["data"]["instances"] == []

    inst_dir = stack.ensure_instance_dir("proj-12345678")
    state = {
        "instance": "proj-12345678",
        "project": "proj",
        "mode": "native",
        "root": str(tmp_path),
        "services": {
            "api": {
                "name": "api",
                "type": "fd",
                "pid": 55555,
                "pgid": 55555,
                "port": 8080,
                "url": "http://127.0.0.1:8080",
                "binary": "/usr/bin/python",
                "argv": ["python", "app.py"],
                "start_time": "Thu Jan 1 00:00:00 2026",
                "identity": "python app.py",
            }
        },
    }
    stack.write_state(inst_dir / "state.json", state)

    monkeypatch.setattr(stack, "pid_alive", lambda pid: True)
    monkeypatch.setattr(stack, "identity_matches", lambda rec: True)

    ret_table = stack.cmd_ps(as_json=False)
    assert ret_table == 0
    table_out = capsys.readouterr().out
    assert "proj" in table_out
    assert "12345678" in table_out
    assert "running" in table_out

    ret_wide = stack.cmd_ps(as_json=False, wide=True)
    assert ret_wide == 0
    wide_out = capsys.readouterr().out
    assert "proj-12345678" in wide_out

    ret_json = stack.cmd_ps(as_json=True)
    assert ret_json == 0
    json_out = json.loads(capsys.readouterr().out)
    assert json_out["ok"] is True
    assert len(json_out["data"]["instances"]) == 1
    inst_data = json_out["data"]["instances"][0]
    assert inst_data["project"] == "proj"
    assert inst_data["status"] == "running"
    assert inst_data["services"]["api"]["status"] == "running"


SAMPLE = {
    "project": "sample",
    "services": {
        "backend": {"type": "fd", "cwd": ".", "command": ["true", "--fd", "{fd}"]},
        "frontend": {"type": "port", "cwd": ".", "command": ["true", "--port", "{port}"]},
    },
    "scopes": {"full": ["backend", "frontend"]},
}


def test_pruning_warns_when_an_unverifiable_record_still_holds_its_port(tmp_path, capsys):
    listener, port = stack.allocate_listener()
    try:
        state = {
            "generation": 1,
            "services": {
                "ghost": {
                    "name": "ghost",
                    "pid": 999999,
                    "pgid": 999999,
                    "binary": "/nonexistent",
                    "argv": ["/nonexistent"],
                    "start_time": "1999-01-01T00:00:00",
                    "port": port,
                }
            },
        }

        assert stack.prune_state(state, tmp_path) == ["ghost"]

        captured = capsys.readouterr()
        assert "warning" in captured.err
        assert str(port) in captured.err
        assert "orphaned" in captured.err
    finally:
        listener.close()


def test_status_prunes_a_stale_record_and_leaves_the_stack_empty(tmp_path, capsys):
    manifest_path = tmp_path / "stack.json"
    manifest_path.write_text(json.dumps(SAMPLE))
    runtime = stack.ensure_runtime_dir(tmp_path)
    stack.write_state(
        runtime / "state.json",
        {
            "generation": 1,
            "services": {
                "backend": {
                    "name": "backend",
                    "pid": 999999,
                    "pgid": 999999,
                    "binary": "/nonexistent",
                    "argv": ["/nonexistent"],
                    "start_time": "1999-01-01T00:00:00",
                    "port": 1,
                    "url": "http://127.0.0.1:1",
                }
            },
        },
    )

    exit_code = stack.cmd_status(root=tmp_path, manifest_path=manifest_path)

    assert exit_code == 0
    assert stack.read_state(runtime / "state.json")["services"] == {}
