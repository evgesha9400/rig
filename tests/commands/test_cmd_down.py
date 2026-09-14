"""cmd_down: targeting by instance id or project slug, ambiguity, and "all"."""

import pytest

from rig import cli as rig

stack = rig


def test_cmd_down_by_instance_id_and_project_slug(monkeypatch, tmp_path):
    state_home = tmp_path / "rig_state"
    monkeypatch.setenv("RIG_STATE_HOME", str(state_home))

    inst_dir = stack.ensure_instance_dir("alpha-11223344")
    state = {
        "instance": "alpha-11223344",
        "project": "alpha",
        "root": str(tmp_path),
        "services": {
            "web": {"name": "web", "type": "port", "pid": 1234, "pgid": 1234, "port": 3000}
        },
    }
    stack.write_state(inst_dir / "state.json", state)

    stopped = []
    monkeypatch.setattr(
        stack, "_stop_record", lambda rec, root: stopped.append(rec["name"]) or "terminated"
    )

    ret = stack.cmd_down(target="alpha")
    assert ret == 0
    assert stopped == ["web"]

    ret2 = stack.cmd_down(target="alpha-11223344")
    assert ret2 == 0

    with pytest.raises(stack.RigError) as exc_info:
        stack.cmd_down(target="nonexistent")
    assert exc_info.value.code == "E_NOT_FOUND"
    assert exc_info.value.exit_code == stack.EXIT_NOT_FOUND


def test_cmd_down_ambiguous_slug_error(monkeypatch, tmp_path):
    state_home = tmp_path / "rig_state"
    monkeypatch.setenv("RIG_STATE_HOME", str(state_home))

    dir1 = stack.ensure_instance_dir("beta-11111111")
    stack.write_state(
        dir1 / "state.json", {"instance": "beta-11111111", "project": "beta", "services": {}}
    )

    dir2 = stack.ensure_instance_dir("beta-22222222")
    stack.write_state(
        dir2 / "state.json", {"instance": "beta-22222222", "project": "beta", "services": {}}
    )

    with pytest.raises(stack.RigError) as exc_info:
        stack.cmd_down(target="beta")
    assert exc_info.value.code == "E_AMBIGUOUS"
    assert exc_info.value.exit_code == stack.EXIT_NOT_FOUND


def test_cmd_down_all(monkeypatch, tmp_path):
    state_home = tmp_path / "rig_state"
    monkeypatch.setenv("RIG_STATE_HOME", str(state_home))

    dir1 = stack.ensure_instance_dir("proj1-11111111")
    stack.write_state(
        dir1 / "state.json",
        {
            "instance": "proj1-11111111",
            "project": "proj1",
            "services": {"s1": {"name": "s1", "type": "port", "pid": 11, "pgid": 11}},
        },
    )

    dir2 = stack.ensure_instance_dir("proj2-22222222")
    stack.write_state(
        dir2 / "state.json",
        {
            "instance": "proj2-22222222",
            "project": "proj2",
            "services": {"s2": {"name": "s2", "type": "port", "pid": 22, "pgid": 22}},
        },
    )

    stopped = []
    monkeypatch.setattr(
        stack, "_stop_record", lambda rec, root: stopped.append(rec["name"]) or "terminated"
    )

    ret = stack.cmd_down(all_instances=True)
    assert ret == 0
    assert "s1" in stopped
    assert "s2" in stopped


def test_cmd_down_orphaned_instance(monkeypatch, tmp_path):
    state_home = tmp_path / "rig_state"
    monkeypatch.setenv("RIG_STATE_HOME", str(state_home))

    deleted_root = tmp_path / "deleted_repo"
    inst_dir = stack.ensure_instance_dir("orphan-99999999")
    stack.write_state(
        inst_dir / "state.json",
        {
            "instance": "orphan-99999999",
            "project": "orphan",
            "root": str(deleted_root),
            "services": {
                "orphan_svc": {"name": "orphan_svc", "type": "port", "pid": 999, "pgid": 999}
            },
        },
    )

    stopped = []
    monkeypatch.setattr(
        stack, "_stop_record", lambda rec, root: stopped.append(rec["name"]) or "terminated"
    )

    ret = stack.cmd_down(target="orphan-99999999")
    assert ret == 0
    assert "orphan_svc" in stopped
