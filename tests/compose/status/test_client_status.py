"""R11-1/R12-1: status probes carry the recorded Docker client configuration."""

import subprocess
from pathlib import Path

from rig import cli as rig

stack = rig


def _write_compose_file(root, name="compose.yml"):
    path = Path(root) / name
    path.write_text("services: {}\n")
    return path


def _compose_record(root, container="abc123", compose_file="compose.yml", name="db"):
    return {
        "name": name,
        "type": "compose",
        "instance": "inst-1",
        "compose_file": str(Path(root) / compose_file),
        "compose_service": name,
        "container": container,
    }


def _healthy_compose(calls=None):
    def fake_run_compose(
        instance, root, cfile, args, context=None, timeout=180.0, env=None, **kwargs
    ):
        if calls is not None:
            calls.append(
                {"args": list(args), "context": context, "env": env, "kwargs": dict(kwargs)}
            )
        if args[0] == "ps":
            return subprocess.CompletedProcess(["docker"], 0, "abc123\n", "")
        if args[0] == "port":
            return subprocess.CompletedProcess(["docker"], 0, "0.0.0.0:54321\n", "")
        return subprocess.CompletedProcess(["docker"], 0, "", "")

    return fake_run_compose


class _FakeDocker:
    """Answer plain ``docker`` calls from a script and record every invocation."""

    def __init__(self, inspect="running", ps_ids=("abc123",)):
        self.inspect = inspect
        self.ps_ids = list(ps_ids)
        self.calls: list[list[str]] = []

    def __call__(self, args, context=None, timeout=60.0, **kwargs):
        args = list(args)
        self.calls.append(args)
        if args[0] == "inspect":
            return subprocess.CompletedProcess(["docker"], 0, f"{self.inspect}\n", "")
        if args[0] == "ps":
            return subprocess.CompletedProcess(
                ["docker"], 0, "".join(f"{i}\n" for i in self.ps_ids), ""
            )
        return subprocess.CompletedProcess(["docker"], 0, "", "")


class _EnvRecordingDocker(_FakeDocker):
    """A ``_FakeDocker`` that also keeps the environment each call carried."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.envs: list[dict | None] = []

    def __call__(self, args, context=None, timeout=60.0, **kwargs):
        self.envs.append(kwargs.get("env"))
        return super().__call__(args, context=context, timeout=timeout, **kwargs)


def _capture_docker_argv_and_env(monkeypatch, inspect="running"):
    """Answer every real ``docker`` invocation, recording argv and environment."""
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


def test_compose_status_and_teardown_reach_the_recorded_docker_context(monkeypatch, tmp_path):
    """R11-1: Compose commands carry the recorded context, not the active one."""
    _write_compose_file(tmp_path)
    monkeypatch.setenv("DOCKER_CONTEXT", "desktop-linux")
    calls: list[dict] = []
    monkeypatch.setattr(stack, "run_compose", _healthy_compose(calls))
    monkeypatch.setattr(stack, "run_docker", _FakeDocker(inspect="running"))
    record = dict(_compose_record(tmp_path), docker_context="colima", docker_host=None)

    assert stack.compose_record_status(record, tmp_path) == "alive"
    assert stack._stop_record(record, tmp_path) == "terminated"

    assert calls and all(call["context"] == "colima" for call in calls)


def test_docker_status_carries_the_recorded_client_configuration(monkeypatch, tmp_path):
    """R12-1: the Docker fallback inspects through the recorded config directory."""
    monkeypatch.setenv("DOCKER_CONFIG", "/home/dev/.docker")
    seen = _capture_docker_argv_and_env(monkeypatch)
    record = dict(
        _compose_record(tmp_path),
        compose_env={"DOCKER_CONFIG": "/srv/rig/.docker", "DB_USER": "app"},
    )

    assert stack.compose_record_status(record, tmp_path) == "alive"

    assert [argv for argv, _ in seen]
    assert all(env.get("DOCKER_CONFIG") == "/srv/rig/.docker" for _, env in seen)
    verbs = {argv[1] for argv, _ in seen}
    assert {"ps", "inspect"} <= verbs


def test_compose_status_hands_plain_docker_the_recorded_environment(monkeypatch, tmp_path):
    """R12-1: the compose path passes the record's environment to every probe."""
    _write_compose_file(tmp_path)
    monkeypatch.setattr(
        stack,
        "run_compose",
        lambda *a, **k: subprocess.CompletedProcess(["docker"], 0, "def456\n", ""),
    )
    docker = _EnvRecordingDocker(inspect="running")
    monkeypatch.setattr(stack, "run_docker", docker)
    record = dict(_compose_record(tmp_path), compose_env={"DOCKER_CONFIG": "/srv/rig/.docker"})

    assert stack.compose_record_status(record, tmp_path) == "alive"

    assert docker.envs
    assert all(env and env["DOCKER_CONFIG"] == "/srv/rig/.docker" for env in docker.envs)
