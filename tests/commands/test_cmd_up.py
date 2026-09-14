"""cmd_up: mode conflicts and switch failures."""

import json

import pytest

from rig import cli as rig

stack = rig


def _mode_manifest(tmp_path, project="mode-test"):
    manifest_path = tmp_path / "rig.json"
    manifest_path.write_text(
        json.dumps(
            {
                "project": project,
                "default_mode": "native",
                "modes": {
                    "native": {
                        "services": {"backend": {"type": "port", "cwd": ".", "command": ["echo"]}}
                    },
                    "container": {
                        "services": {"backend": {"type": "port", "cwd": ".", "command": ["echo"]}}
                    },
                },
            }
        )
    )
    return manifest_path


def test_cmd_up_mode_conflict_requires_switch(monkeypatch, tmp_path):
    monkeypatch.setenv("RIG_STATE_HOME", str(tmp_path / "state"))
    manifest_path = _mode_manifest(tmp_path)

    monkeypatch.setattr(stack, "pid_alive", lambda pid: True)
    monkeypatch.setattr(stack, "identity_matches", lambda rec: True)
    monkeypatch.setattr(stack, "_await_ready", lambda *args, **kwargs: True)

    dummy_native = {"name": "backend", "type": "port", "pid": 1001, "pgid": 1001, "port": 8000}
    monkeypatch.setattr(stack, "_start_service", lambda *args, **kwargs: dummy_native)

    ret = stack.cmd_up(tmp_path, manifest_path, mode="native")
    assert ret == 0

    dummy_container = {"name": "backend", "type": "port", "pid": 1002, "pgid": 1002, "port": 8000}
    monkeypatch.setattr(stack, "_start_service", lambda *args, **kwargs: dummy_container)

    with pytest.raises(stack.RigError) as exc_info:
        stack.cmd_up(tmp_path, manifest_path, mode="container", switch=False)
    assert exc_info.value.code == "E_MODE_CONFLICT"
    assert exc_info.value.exit_code == stack.EXIT_MUTEX_CONFLICT


def test_cmd_up_switch_aborts_and_preserves_active_mode_when_stop_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("RIG_STATE_HOME", str(tmp_path / "state"))
    manifest_path = _mode_manifest(tmp_path, project="switch-fail")

    monkeypatch.setattr(stack, "pid_alive", lambda pid: True)
    monkeypatch.setattr(stack, "pgid_alive", lambda pgid: True)
    monkeypatch.setattr(stack, "identity_matches", lambda rec: True)
    monkeypatch.setattr(stack, "is_service_verifiable_alive", lambda rec, root: True)
    monkeypatch.setattr(stack, "_await_ready", lambda *args, **kwargs: True)

    dummy_native = {"name": "backend", "type": "port", "pid": 4321, "pgid": 4321, "port": 8000}
    monkeypatch.setattr(stack, "_start_service", lambda *args, **kwargs: dummy_native)

    assert stack.cmd_up(tmp_path, manifest_path, mode="native") == 0

    state_file = stack._state_path(tmp_path)
    monkeypatch.setattr(stack, "_stop_record", lambda rec, root: "refused")

    with pytest.raises(stack.RigError) as exc_info:
        stack.cmd_up(tmp_path, manifest_path, mode="container", switch=True, as_json=True)
    assert exc_info.value.code == "E_SWITCH_FAILED"
    assert exc_info.value.exit_code == stack.EXIT_REFUSED

    saved_state = stack.read_state(state_file)
    assert saved_state["mode"] == "native"
