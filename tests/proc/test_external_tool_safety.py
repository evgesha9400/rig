"""Tests for shared-dependency recovery, external-tool checks, and safe state writes."""

import json

import pytest

from rig import cli as rig

stack = rig


def test_recovery_stops_state_only_consumer_of_shared_dependency(monkeypatch, tmp_path):
    """R2-5: every recorded consumer of a restarted dependency must be stopped."""
    monkeypatch.setenv("RIG_STATE_HOME", str(tmp_path / "rig_state"))
    manifest_path = tmp_path / "rig.json"
    manifest_path.write_text(
        json.dumps(
            {
                "project": "shared-dep",
                "services": {
                    "db": {"type": "port", "command": ["echo", "db"]},
                    "api": {"type": "port", "command": ["echo", "api"], "depends_on": ["db"]},
                },
            }
        )
    )
    root = tmp_path
    instance = stack.instance_id("shared-dep", root)
    state_path = stack._state_path(root, instance=instance)
    stack.write_state(
        state_path,
        {
            "instance": instance,
            "project": "shared-dep",
            "root": str(root),
            "mode": "default",
            "services": {
                "db": {"name": "db", "type": "port", "pid": 11, "pgid": 11, "depends_on": []},
                "api": {"name": "api", "type": "port", "pid": 22, "pgid": 22, "depends_on": ["db"]},
                "worker": {
                    "name": "worker",
                    "type": "port",
                    "pid": 33,
                    "pgid": 33,
                    "depends_on": ["db"],
                },
            },
        },
    )
    # db's leader is gone; api and worker are still running against its old port.
    monkeypatch.setattr(stack, "pid_alive", lambda pid: pid != 11)
    monkeypatch.setattr(stack, "pgid_alive", lambda pgid: pgid != 11)
    monkeypatch.setattr(stack, "identity_matches", lambda rec: rec.get("pid") != 11)
    monkeypatch.setattr(stack, "_await_ready", lambda *a, **k: True)
    stopped: list[str] = []

    def fake_stop(record, root):
        stopped.append(record["name"])
        return "terminated"

    monkeypatch.setattr(stack, "_stop_record", fake_stop)
    monkeypatch.setattr(
        stack,
        "_start_service",
        lambda service, *a, **k: {
            "name": service.name,
            "type": "port",
            "pid": 900,
            "pgid": 900,
            "port": 9000,
            "depends_on": list(service.depends_on),
        },
    )

    assert stack.cmd_up(root, manifest_path, scope="db") == stack.EXIT_OK
    assert "worker" in stopped, "state-only consumer of db was left running"
    saved = stack.read_state(state_path)
    assert "worker" not in saved["services"]
    assert "db" in saved["services"]


def test_fd_service_requires_lsof(monkeypatch, tmp_path):
    """R2-11: fd services verify their listener with lsof, so it must be present."""
    monkeypatch.setattr(
        stack.shutil, "which", lambda name: None if name == "lsof" else f"/usr/bin/{name}"
    )
    service = stack.Service(name="api", type="fd", app="app:app")
    runtime = stack.ensure_runtime_dir(tmp_path)
    with pytest.raises(stack.RigError) as exc_info:
        stack._start_service(service, tmp_path, runtime, "inst", {"root": str(tmp_path)})
    assert exc_info.value.code == "E_EXTERNAL_TOOL"
    assert exc_info.value.exit_code == stack.EXIT_EXTERNAL_TOOL


def test_check_resolves_binaries_against_service_cwd(tmp_path):
    """R2-12: a relative command is resolved against root/cwd, not the process cwd."""
    sub = tmp_path / "sub"
    (sub / "bin").mkdir(parents=True)
    binary = sub / "bin" / "app"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)

    manifest_path = tmp_path / "rig.json"
    manifest_path.write_text(
        json.dumps(
            {
                "project": "cwdcheck",
                "services": {
                    "app": {"type": "port", "cwd": "sub", "command": ["./bin/app", "{port}"]}
                },
            }
        )
    )
    assert stack.cmd_check(tmp_path, manifest_path) == stack.EXIT_OK

    binary.chmod(0o644)
    assert stack.cmd_check(tmp_path, manifest_path) == stack.EXIT_USAGE

    # A path that only exists relative to the repository root is not runnable
    # from the service working directory.
    (tmp_path / "bin").mkdir()
    root_only = tmp_path / "bin" / "other"
    root_only.write_text("#!/bin/sh\nexit 0\n")
    root_only.chmod(0o755)
    manifest_path.write_text(
        json.dumps(
            {
                "project": "cwdcheck",
                "services": {"app": {"type": "port", "cwd": "sub", "command": ["./bin/other"]}},
            }
        )
    )
    assert stack.cmd_check(tmp_path, manifest_path) == stack.EXIT_USAGE


def test_write_state_does_not_follow_predictable_temp_symlink(tmp_path):
    """R2-14: a planted temp-file symlink must not divert the state write."""
    target_dir = tmp_path / "state"
    target_dir.mkdir()
    state_file = target_dir / "state.json"
    victim = tmp_path / "victim.txt"
    victim.write_text("untouched")
    (target_dir / "state.json.tmp").symlink_to(victim)

    stack.write_state(state_file, {"generation": 1, "services": {}})

    assert victim.read_text() == "untouched"
    assert json.loads(state_file.read_text())["generation"] == 1
