"""Mode switch required when only pgid survives; invalid scope rejected."""

import json
from pathlib import Path

import pytest

from rig import cli as rig

stack = rig


def _mode_manifest(tmp_path, project: str) -> Path:
    manifest_path = tmp_path / "rig.json"
    manifest_path.write_text(
        json.dumps(
            {
                "project": project,
                "default_mode": "native",
                "modes": {
                    "native": {"services": {"backend": {"type": "port", "command": ["echo"]}}},
                    "container": {"services": {"backend": {"type": "port", "command": ["echo"]}}},
                },
            }
        )
    )
    return manifest_path


def test_mode_switch_required_when_only_pgid_survives(monkeypatch, tmp_path):
    """An unverifiable but living process group still blocks a mode change."""
    monkeypatch.setenv("RIG_STATE_HOME", str(tmp_path / "rig_state"))
    manifest_path = _mode_manifest(tmp_path, "pgid-mode")
    root = tmp_path
    instance = stack.instance_id("pgid-mode", root)
    state_path = stack._state_path(root, instance=instance)
    stack.write_state(
        state_path,
        {
            "instance": instance,
            "project": "pgid-mode",
            "root": str(root),
            "mode": "native",
            "services": {"backend": {"name": "backend", "type": "port", "pid": 4321, "pgid": 4321}},
        },
    )
    monkeypatch.setattr(stack, "pid_alive", lambda pid: False)
    monkeypatch.setattr(stack, "pgid_alive", lambda pgid: True)

    with pytest.raises(stack.RigError) as exc_info:
        stack.cmd_up(root, manifest_path, mode="container", as_json=True)
    assert exc_info.value.code == "E_MODE_CONFLICT"
    assert exc_info.value.exit_code == stack.EXIT_MUTEX_CONFLICT


def test_invalid_scope_rejected_before_any_teardown(monkeypatch, tmp_path):
    """An unknown scope must be a usage error, not a reason to stop the stack."""
    monkeypatch.setenv("RIG_STATE_HOME", str(tmp_path / "rig_state"))
    manifest_path = _mode_manifest(tmp_path, "scope-guard")
    root = tmp_path
    instance = stack.instance_id("scope-guard", root)
    state_path = stack._state_path(root, instance=instance)
    stack.write_state(
        state_path,
        {
            "instance": instance,
            "project": "scope-guard",
            "root": str(root),
            "mode": "native",
            "services": {"backend": {"name": "backend", "type": "port", "pid": 4321, "pgid": 4321}},
        },
    )
    monkeypatch.setattr(stack, "is_service_verifiable_alive", lambda rec, root: True)

    def refuse(record, root):
        raise AssertionError("no service may be stopped for an invalid scope")

    monkeypatch.setattr(stack, "_stop_record", refuse)

    with pytest.raises(stack.RigError) as exc_info:
        stack.cmd_up(root, manifest_path, scope="nope", mode="container", switch=True, as_json=True)
    assert exc_info.value.code == "E_USAGE"
    assert exc_info.value.exit_code == stack.EXIT_USAGE

    saved = stack.read_state(state_path)
    assert saved["mode"] == "native"
    assert "backend" in saved["services"]
