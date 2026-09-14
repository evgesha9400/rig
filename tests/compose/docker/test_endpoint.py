"""R9-2: the Docker endpoint pinned at startup is the one teardown reaches."""

import subprocess

from rig import cli as rig

stack = rig


def _compose_service(name="db", **kwargs):
    return stack.Service(
        name=name, type="compose", compose_file="compose.yml", compose_service=name, **kwargs
    )


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


def test_compose_start_records_the_docker_host_in_force(monkeypatch, tmp_path):
    monkeypatch.setenv("DOCKER_HOST", "tcp://remote:2375")
    calls: list[dict] = []
    monkeypatch.setattr(stack, "run_compose", _healthy_compose(calls))

    record = stack._start_compose_service(_compose_service(), tmp_path, "inst-1")

    assert record["docker_host"] == "tcp://remote:2375"
    assert all(call["kwargs"]["docker_host"] == "tcp://remote:2375" for call in calls)


def test_compose_start_records_the_absence_of_a_docker_host(monkeypatch, tmp_path):
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setattr(stack, "run_compose", _healthy_compose())

    record = stack._start_compose_service(_compose_service(), tmp_path, "inst-1")

    assert "docker_host" in record
    assert record["docker_host"] is None


def _capture_docker_env(monkeypatch):
    seen: list[dict[str, str]] = []

    def fake_run(argv, **kwargs):
        seen.append(dict(kwargs.get("env") or {}))
        if "ps" in argv:
            return subprocess.CompletedProcess(argv, 0, "abc123\n", "")
        if "inspect" in argv:
            return subprocess.CompletedProcess(argv, 0, "running\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return seen


def test_docker_status_queries_the_daemon_recorded_at_startup(monkeypatch, tmp_path):
    monkeypatch.setenv("DOCKER_HOST", "tcp://somewhere-else:2375")
    seen = _capture_docker_env(monkeypatch)
    record = dict(_compose_record(tmp_path / "deleted"), docker_host="tcp://remote:2375")

    assert stack.docker_record_status(record) == "alive"
    assert seen, "docker must have been called"
    assert all(env.get("DOCKER_HOST") == "tcp://remote:2375" for env in seen)


def test_docker_teardown_reaches_the_daemon_recorded_at_startup(monkeypatch, tmp_path):
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    seen = _capture_docker_env(monkeypatch)
    record = dict(_compose_record(tmp_path / "deleted"), docker_host="tcp://remote:2375")

    assert stack.docker_record_stop(record, remove=True) == "terminated"
    assert all(env.get("DOCKER_HOST") == "tcp://remote:2375" for env in seen)


def test_run_compose_applies_the_pinned_endpoint_to_a_declared_environment(monkeypatch, tmp_path):
    captured: dict[str, str] = {}

    def fake_run(argv, **kwargs):
        captured.update(kwargs.get("env") or {})
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    stack.run_compose(
        "inst-1",
        tmp_path,
        tmp_path / "compose.yml",
        ["ps"],
        None,
        env={"PATH": "/usr/bin", "POSTGRES_PASSWORD": "s3cret"},
        docker_host="tcp://remote:2375",
    )

    assert captured["POSTGRES_PASSWORD"] == "s3cret"
    assert captured["DOCKER_HOST"] == "tcp://remote:2375"
