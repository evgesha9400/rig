"""cmd_prune: keeps the lock inode, is idempotent, forces past a stuck dependent."""

import json

from rig import cli as rig

stack = rig


def _write_instance(ident: str, state: dict):
    inst_dir = stack.ensure_instance_dir(ident)
    stack.write_state(inst_dir / stack.STATE_FILE_NAME, state)
    return inst_dir


def test_cmd_prune(monkeypatch, tmp_path):
    state_home = tmp_path / "rig_state"
    monkeypatch.setenv("RIG_STATE_HOME", str(state_home))

    dead_dir = stack.ensure_instance_dir("dead-00000000")
    stack.write_state(
        dead_dir / "state.json",
        {
            "instance": "dead-00000000",
            "project": "dead",
            "root": str(tmp_path / "nonexistent"),
            "services": {},
        },
    )

    alive_dir = stack.ensure_instance_dir("alive-11111111")
    stack.write_state(
        alive_dir / "state.json",
        {
            "instance": "alive-11111111",
            "project": "alive",
            "root": str(tmp_path),
            "services": {"s": {"name": "s", "type": "port", "pid": 123, "pgid": 123}},
        },
    )
    monkeypatch.setattr(stack, "pid_alive", lambda pid: True)
    monkeypatch.setattr(stack, "identity_matches", lambda rec: True)

    ret = stack.cmd_prune()
    assert ret == 0
    assert not (dead_dir / stack.STATE_FILE_NAME).exists()
    assert (dead_dir / stack.LOCK_FILE_NAME).exists()
    assert (alive_dir / stack.STATE_FILE_NAME).is_file()


def test_cmd_prune_respects_living_pgid(monkeypatch, tmp_path, capsys):
    state_home = tmp_path / "rig_state"
    monkeypatch.setenv("RIG_STATE_HOME", str(state_home))

    inst_dir = stack.ensure_instance_dir("prune-test-1234")
    state = {
        "instance": "prune-test-1234",
        "project": "prune-test",
        "mode": "native",
        "root": str(tmp_path / "deleted_repo"),
        "services": {
            "worker": {"name": "worker", "type": "port", "pid": 99999, "pgid": 99999, "port": 9000}
        },
    }
    stack.write_state(inst_dir / "state.json", state)

    monkeypatch.setattr(stack, "pid_alive", lambda pid: False)
    monkeypatch.setattr(stack, "pgid_alive", lambda pgid: True)

    ret = stack.cmd_prune(force=False, as_json=True)
    assert ret == 0
    out = json.loads(capsys.readouterr().out)
    assert out["data"]["pruned"] == []
    assert inst_dir.exists(), "Instance directory must not be pruned when pgid is still alive"


def test_prune_keeps_lock_inode_and_is_idempotent(monkeypatch, tmp_path, capsys):
    """R2-1: pruning must never unlink the lock file other processes contend for."""
    monkeypatch.setenv("RIG_STATE_HOME", str(tmp_path / "rig_state"))
    inst_dir = _write_instance(
        "dead-00000000",
        {
            "instance": "dead-00000000",
            "project": "dead",
            "root": str(tmp_path / "gone"),
            "services": {},
        },
    )
    lock_file = inst_dir / stack.LOCK_FILE_NAME
    with stack.exclusive_lock(lock_file):
        pass
    lock_inode = lock_file.stat().st_ino

    assert stack.cmd_prune(as_json=True) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["data"]["pruned"] == ["dead-00000000"]
    assert not (inst_dir / stack.STATE_FILE_NAME).exists()
    assert lock_file.exists()
    assert lock_file.stat().st_ino == lock_inode

    assert stack.cmd_prune(as_json=True) == 0
    out2 = json.loads(capsys.readouterr().out)
    assert out2["data"]["pruned"] == []


def test_force_prune_preserves_dependency_when_dependent_refuses(monkeypatch, tmp_path, capsys):
    """R2-2: a dependency must survive when its dependent cannot be stopped."""
    monkeypatch.setenv("RIG_STATE_HOME", str(tmp_path / "rig_state"))
    inst_dir = _write_instance(
        "stuck-00000000",
        {
            "instance": "stuck-00000000",
            "project": "stuck",
            "root": str(tmp_path),
            "services": {
                "db": {"name": "db", "type": "port", "pid": 111, "pgid": 111, "depends_on": []},
                "api": {
                    "name": "api",
                    "type": "port",
                    "pid": 222,
                    "pgid": 222,
                    "depends_on": ["db"],
                },
            },
        },
    )
    monkeypatch.setattr(stack, "pid_alive", lambda pid: True)
    monkeypatch.setattr(stack, "pgid_alive", lambda pgid: True)
    monkeypatch.setattr(stack, "identity_matches", lambda rec: True)

    stopped: list[str] = []

    def fake_stop(record, root, remove=False):
        stopped.append(record["name"])
        assert remove is True, "a forced prune must reclaim the container, not just stop it"
        return "failed" if record["name"] == "api" else "terminated"

    monkeypatch.setattr(stack, "_stop_record", fake_stop)

    ret = stack.cmd_prune(force=True, as_json=True)
    out = json.loads(capsys.readouterr().out)

    assert ret == stack.EXIT_OP_FAILED
    assert out["ok"] is False
    assert out["data"]["pruned"] == []
    assert stopped == ["api"], "db must not be stopped once its dependent failed"
    saved = stack.read_state(inst_dir / stack.STATE_FILE_NAME)
    assert set(saved["services"]) == {"db", "api"}
    assert any("api" in entry for entry in out["data"]["failed"][0]["failed"])
