"""R11-1: a Docker context switch after startup must not strand the container."""

import json
import subprocess

from rig import cli as rig

stack = rig
RESOLVE_DOCKER_CONTEXT = rig.resolve_current_docker_context


def _compose_record(root, container="abc123", compose_file="compose.yml", name="db"):
    from pathlib import Path

    return {
        "name": name,
        "type": "compose",
        "instance": "inst-1",
        "compose_file": str(Path(root) / compose_file),
        "compose_service": name,
        "container": container,
    }


def _capture_docker_argv(monkeypatch, port="0.0.0.0:54321", active=None):
    argvs: list[list[str]] = []

    def fake_run(argv, **kwargs):
        argv = list(argv)
        argvs.append(argv)
        if argv[1:] == ["context", "show"]:
            name = (active or {}).get("context", "")
            return subprocess.CompletedProcess(argv, 0, f"{name}\n", "")
        if "port" in argv:
            return subprocess.CompletedProcess(argv, 0, f"{port}\n", "")
        if "ps" in argv:
            return subprocess.CompletedProcess(argv, 0, "abc123\n", "")
        if "inspect" in argv:
            return subprocess.CompletedProcess(argv, 0, "running\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return argvs


def test_docker_status_and_teardown_reach_the_recorded_docker_context(monkeypatch, tmp_path):
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setenv("DOCKER_CONTEXT", "desktop-linux")

    def _never(*args, **kwargs):
        raise AssertionError("run_compose must not run without a compose file")

    monkeypatch.setattr(stack, "run_compose", _never)
    argvs = _capture_docker_argv(monkeypatch)
    record = dict(_compose_record(tmp_path / "deleted"), docker_context="colima", docker_host=None)

    assert stack.docker_record_status(record) == "alive"
    assert stack.docker_record_stop(record, remove=True) == "terminated"

    assert argvs
    assert all(argv[:3] == ["docker", "--context", "colima"] for argv in argvs)


def test_a_context_switch_between_up_and_down_does_not_strand_the_container(monkeypatch, tmp_path):
    monkeypatch.setenv("RIG_STATE_HOME", str(tmp_path / "rig_state"))
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setattr(stack, "resolve_current_docker_context", RESOLVE_DOCKER_CONTEXT)
    active = {"context": "colima"}
    (tmp_path / "compose.yml").write_text("services: {}\n")
    manifest_path = tmp_path / "rig.json"
    manifest_path.write_text(
        json.dumps(
            {
                "project": "shop",
                "services": {
                    "db": {
                        "type": "compose",
                        "compose_file": "compose.yml",
                        "compose_service": "db",
                    }
                },
                "scopes": {"full": ["db"]},
            }
        )
    )
    argvs = _capture_docker_argv(monkeypatch, active=active)

    assert stack.cmd_up(tmp_path, manifest_path, scope="full") == stack.EXIT_OK
    instance = stack.instance_id("shop", tmp_path)
    state_path = stack._state_path(tmp_path, instance=instance)
    assert stack.read_state(state_path)["services"]["db"]["docker_context"] == "colima"

    active["context"] = "desktop-linux"
    argvs.clear()

    assert stack.cmd_down(root=tmp_path, manifest_path=manifest_path) == stack.EXIT_OK

    assert argvs
    assert all(argv[:3] == ["docker", "--context", "colima"] for argv in argvs)
    assert stack.read_state(state_path)["services"] == {}
