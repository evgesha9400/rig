"""R11-1/R12-1: the Docker context in force at startup is pinned with the record."""

import subprocess

from rig import cli as rig

stack = rig


def _compose_service(name="db", **kwargs):
    return stack.Service(
        name=name, type="compose", compose_file="compose.yml", compose_service=name, **kwargs
    )


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


def test_compose_start_records_the_active_docker_context(monkeypatch, tmp_path):
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setattr(stack, "resolve_current_docker_context", lambda *a, **k: "colima")
    calls: list[dict] = []
    monkeypatch.setattr(stack, "run_compose", _healthy_compose(calls))

    record = stack._start_compose_service(_compose_service(), tmp_path, "inst-1")

    assert record["docker_context"] == "colima"
    assert calls and all(call["context"] == "colima" for call in calls)


def test_compose_start_keeps_the_context_the_manifest_declares(monkeypatch, tmp_path):
    monkeypatch.delenv("DOCKER_HOST", raising=False)

    def _never(*args, **kwargs):
        raise AssertionError("a declared context must not be resolved again")

    monkeypatch.setattr(stack, "resolve_current_docker_context", _never)
    calls: list[dict] = []
    monkeypatch.setattr(stack, "run_compose", _healthy_compose(calls))

    record = stack._start_compose_service(
        _compose_service(docker_context="orbstack"), tmp_path, "inst-1"
    )

    assert record["docker_context"] == "orbstack"
    assert calls and all(call["context"] == "orbstack" for call in calls)


def test_compose_start_pins_no_context_when_docker_host_decides_the_daemon(monkeypatch, tmp_path):
    monkeypatch.setenv("DOCKER_HOST", "tcp://remote:2375")

    def _never(*args, **kwargs):
        raise AssertionError("DOCKER_HOST already decides the daemon")

    monkeypatch.setattr(stack, "resolve_current_docker_context", _never)
    calls: list[dict] = []
    monkeypatch.setattr(stack, "run_compose", _healthy_compose(calls))

    record = stack._start_compose_service(_compose_service(), tmp_path, "inst-1")

    assert record["docker_context"] is None
    assert record["docker_host"] == "tcp://remote:2375"
    assert calls and all(call["context"] is None for call in calls)


def test_compose_start_prefers_the_ambient_context_over_the_ambient_host(monkeypatch, tmp_path):
    monkeypatch.setenv("DOCKER_CONTEXT", "colima")
    monkeypatch.setenv("DOCKER_HOST", "tcp://remote:2375")

    def _never(*args, **kwargs):
        raise AssertionError("an ambient DOCKER_CONTEXT already names the context")

    monkeypatch.setattr(stack, "resolve_current_docker_context", _never)
    calls: list[dict] = []
    monkeypatch.setattr(stack, "run_compose", _healthy_compose(calls))

    record = stack._start_compose_service(_compose_service(), tmp_path, "inst-1")

    assert record["docker_context"] == "colima"
    assert record["docker_host"] is None
    assert calls and all(call["context"] == "colima" for call in calls)
    assert all(call["kwargs"]["docker_host"] is None for call in calls)


def test_compose_start_prefers_the_declared_context_over_the_ambient_one(monkeypatch, tmp_path):
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setenv("DOCKER_CONTEXT", "desktop-linux")
    calls: list[dict] = []
    monkeypatch.setattr(stack, "run_compose", _healthy_compose(calls))

    record = stack._start_compose_service(
        _compose_service(docker_context="orbstack"), tmp_path, "inst-1"
    )

    assert record["docker_context"] == "orbstack"
    assert calls and all(call["context"] == "orbstack" for call in calls)


def test_compose_start_resolves_the_active_context_only_as_a_last_resort(monkeypatch, tmp_path):
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    resolved: list[object] = []

    def _resolve(env=None, *args, **kwargs):
        resolved.append(env)
        return "desktop-linux"

    monkeypatch.setattr(stack, "resolve_current_docker_context", _resolve)
    calls: list[dict] = []
    monkeypatch.setattr(stack, "run_compose", _healthy_compose(calls))

    record = stack._start_compose_service(_compose_service(), tmp_path, "inst-1")

    assert len(resolved) == 1
    assert record["docker_context"] == "desktop-linux"
    assert record["docker_host"] is None
    assert calls and all(call["context"] == "desktop-linux" for call in calls)
