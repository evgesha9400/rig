"""R9-1: `up` persists a partial compose record when startup is interrupted."""

import json
import subprocess

import pytest

from rig import cli as rig

stack = rig


def _interrupted_compose(step: str, cleanup_ok: bool):
    def fake_run_compose(
        instance, root, cfile, args, context=None, timeout=180.0, env=None, **kwargs
    ):
        verb = args[0]
        if verb == step:
            raise KeyboardInterrupt()
        if verb == "up":
            return subprocess.CompletedProcess(["docker"], 0, "", "")
        if verb == "ps":
            return subprocess.CompletedProcess(["docker"], 0, "abc123\n", "")
        if verb == "port":
            return subprocess.CompletedProcess(["docker"], 0, "0.0.0.0:54321\n", "")
        return subprocess.CompletedProcess(
            ["docker"], 0 if cleanup_ok else 1, "", "" if cleanup_ok else "cleanup refused"
        )

    return fake_run_compose


def _compose_manifest(tmp_path, project="ints", port=None):
    (tmp_path / "compose.yml").write_text("services: {}\n")
    spec = {"type": "compose", "compose_file": "compose.yml", "compose_service": "db"}
    if port:
        spec["compose_port"] = port
    manifest_path = tmp_path / "rig.json"
    manifest_path.write_text(
        json.dumps({"project": project, "services": {"db": spec}, "scopes": {"full": ["db"]}})
    )
    return manifest_path


def test_cmd_up_persists_the_partial_container_after_an_interrupt(monkeypatch, tmp_path):
    monkeypatch.setenv("RIG_STATE_HOME", str(tmp_path / "rig_state"))
    manifest_path = _compose_manifest(tmp_path)
    monkeypatch.setattr(stack, "run_compose", _interrupted_compose("ps", cleanup_ok=False))

    try:
        exit_code = stack.cmd_up(tmp_path, manifest_path, scope="full")
    except (KeyboardInterrupt, SystemExit, RuntimeError, OSError) as exc:
        pytest.fail(f"the interrupt escaped `up`: {exc!r}")

    assert exit_code == stack.EXIT_OP_FAILED
    instance = stack.instance_id("ints", tmp_path)
    saved = stack.read_state(stack._state_path(tmp_path, instance=instance))["services"]
    assert "db" in saved, "the container up created must stay recorded"
    assert saved["db"]["type"] == "compose"
    assert saved["db"]["compose_service"] == "db"


def test_cmd_up_writes_state_before_an_interrupt_unwinds(monkeypatch, tmp_path):
    monkeypatch.setenv("RIG_STATE_HOME", str(tmp_path / "rig_state"))
    manifest_path = _compose_manifest(tmp_path)
    partial = {
        "name": "db",
        "type": "compose",
        "instance": "inst-1",
        "compose_file": str(tmp_path / "compose.yml"),
        "compose_service": "db",
        "container": "abc123",
    }

    def interrupted_start(service, root, runtime, instance, state, state_path):
        state["services"][service.name] = dict(partial)
        raise KeyboardInterrupt()

    monkeypatch.setattr(stack, "_start_with_retry", interrupted_start)

    with pytest.raises(KeyboardInterrupt):
        stack.cmd_up(tmp_path, manifest_path, scope="full")

    instance = stack.instance_id("ints", tmp_path)
    saved = stack.read_state(stack._state_path(tmp_path, instance=instance))["services"]
    assert saved.get("db", {}).get("container") == "abc123"
