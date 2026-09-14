"""R12-1: plain Docker commands keep the recorded Docker client configuration."""

import subprocess
from pathlib import Path

from rig import cli as rig

stack = rig


def _compose_record(root, container="abc123", compose_file="compose.yml", name="db"):
    return {
        "name": name,
        "type": "compose",
        "instance": "inst-1",
        "compose_file": str(Path(root) / compose_file),
        "compose_service": name,
        "container": container,
    }


def _capture_docker_argv_and_env(monkeypatch, inspect="running"):
    seen: list[tuple[list[str], dict]] = []

    def fake_run(argv, **kwargs):
        argv = list(argv)
        seen.append((argv, dict(kwargs.get("env") or {})))
        if "ps" in argv:
            return subprocess.CompletedProcess(argv, 0, "abc123\n", "")
        if "inspect" in argv:
            return subprocess.CompletedProcess(argv, 0, f"{inspect}\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return seen


def test_run_docker_replays_the_recorded_docker_client_settings(monkeypatch, tmp_path):
    monkeypatch.delenv("DOCKER_CONFIG", raising=False)
    seen = _capture_docker_argv_and_env(monkeypatch)

    stack.run_docker(
        ["inspect", "abc123"],
        "colima",
        env={"DOCKER_CONFIG": "/srv/rig/.docker", "DB_USER": "app"},
    )

    argv, env = seen[0]
    assert argv[:3] == ["docker", "--context", "colima"]
    assert env["DOCKER_CONFIG"] == "/srv/rig/.docker"
    assert "DB_USER" not in env
    assert "PATH" in env


def test_run_docker_ignores_a_client_setting_the_record_never_held(monkeypatch, tmp_path):
    monkeypatch.setenv("DOCKER_CONFIG", "/home/dev/.docker")
    monkeypatch.setenv("DOCKER_TLS_VERIFY", "1")
    seen = _capture_docker_argv_and_env(monkeypatch)

    stack.run_docker(["ps", "-q"], None, env={"DB_USER": "app"})

    _, env = seen[0]
    assert "DOCKER_CONFIG" not in env
    assert "DOCKER_TLS_VERIFY" not in env


def test_run_docker_without_a_recorded_environment_uses_the_ambient_settings(monkeypatch, tmp_path):
    monkeypatch.setenv("DOCKER_CONFIG", "/home/dev/.docker")
    seen = _capture_docker_argv_and_env(monkeypatch)

    stack.run_docker(["ps", "-q"], None)

    _, env = seen[0]
    assert env["DOCKER_CONFIG"] == "/home/dev/.docker"


def test_docker_teardown_carries_the_recorded_client_configuration(monkeypatch, tmp_path):
    monkeypatch.delenv("DOCKER_CONFIG", raising=False)
    seen = _capture_docker_argv_and_env(monkeypatch)
    record = dict(
        _compose_record(tmp_path),
        compose_env={"DOCKER_CONFIG": "/srv/rig/.docker", "DOCKER_TLS_VERIFY": "1"},
    )

    assert stack._stop_record(record, tmp_path, remove=True) == "terminated"

    verbs = {argv[1] for argv, _ in seen}
    assert {"stop", "rm"} <= verbs
    assert all(env.get("DOCKER_CONFIG") == "/srv/rig/.docker" for _, env in seen)
    assert all(env.get("DOCKER_TLS_VERIFY") == "1" for _, env in seen)
