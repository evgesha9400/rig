"""R9-3: a Compose service receives the environment its manifest declares."""

import subprocess
import sys

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


def _start_compose_through_start_service(monkeypatch, tmp_path, service, calls):
    monkeypatch.setattr(stack, "run_compose", _healthy_compose(calls))
    values = {
        "root": str(tmp_path),
        "instance": "inst-1",
        "data_dir": str(tmp_path / "data"),
        "python": sys.executable,
    }
    return stack._start_service(service, tmp_path, tmp_path / ".local-run", "inst-1", values)


def test_compose_service_receives_its_declared_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("UNRELATED_AMBIENT", "leaked")
    calls: list[dict] = []
    service = _compose_service(
        env={"POSTGRES_PASSWORD": "s3cret", "PGDATA": "{root}/data/pg"},
    )

    record = _start_compose_through_start_service(monkeypatch, tmp_path, service, calls)

    assert record["container"] == "abc123"
    assert calls, "compose must have been invoked"
    env = calls[0]["env"]
    assert env is not None, "compose received no environment at all"
    assert env["POSTGRES_PASSWORD"] == "s3cret"
    assert env["PGDATA"] == f"{tmp_path}/data/pg"
    assert "UNRELATED_AMBIENT" not in env
    assert "PATH" in env


def test_compose_service_receives_its_env_file(monkeypatch, tmp_path):
    (tmp_path / ".env.db").write_text("POSTGRES_USER=shop\n# comment\nPOSTGRES_DB=shop_dev\n")
    calls: list[dict] = []
    service = _compose_service(env_files=[".env.db"], env={"TZ_OVERRIDE": "UTC"})

    _start_compose_through_start_service(monkeypatch, tmp_path, service, calls)

    env = calls[0]["env"]
    assert env["POSTGRES_USER"] == "shop"
    assert env["POSTGRES_DB"] == "shop_dev"
    assert env["TZ_OVERRIDE"] == "UTC"


def test_compose_service_inherits_only_what_it_declares(monkeypatch, tmp_path):
    monkeypatch.setenv("REGISTRY_MIRROR", "mirror.internal")
    monkeypatch.setenv("SECRET_AMBIENT", "nope")
    calls: list[dict] = []
    service = _compose_service(inherit=["REGISTRY_MIRROR"])

    _start_compose_through_start_service(monkeypatch, tmp_path, service, calls)

    env = calls[0]["env"]
    assert env["REGISTRY_MIRROR"] == "mirror.internal"
    assert "SECRET_AMBIENT" not in env


def test_compose_environment_keeps_the_docker_client_settings(monkeypatch, tmp_path):
    monkeypatch.setenv("DOCKER_CONFIG", "/home/dev/.docker")
    monkeypatch.setenv("DOCKER_TLS_VERIFY", "1")
    monkeypatch.setenv("DOCKER_CERT_PATH", "/home/dev/.docker/certs")
    monkeypatch.setenv("DOCKER_HOST", "tcp://remote:2375")
    calls: list[dict] = []

    _start_compose_through_start_service(
        monkeypatch, tmp_path, _compose_service(env={"A": "b"}), calls
    )

    env = calls[0]["env"]
    assert env["DOCKER_CONFIG"] == "/home/dev/.docker"
    assert env["DOCKER_TLS_VERIFY"] == "1"
    assert env["DOCKER_CERT_PATH"] == "/home/dev/.docker/certs"
    assert calls[0]["kwargs"]["docker_host"] == "tcp://remote:2375"


def test_compose_start_keeps_the_ambient_docker_context(monkeypatch, tmp_path):
    """R12-1: `DOCKER_CONTEXT=colima rig up` must survive the environment allowlist."""
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setenv("DOCKER_CONTEXT", "colima")

    def _never(*args, **kwargs):
        raise AssertionError("an ambient DOCKER_CONTEXT already names the context")

    monkeypatch.setattr(stack, "resolve_current_docker_context", _never)
    calls: list[dict] = []
    record = _start_compose_through_start_service(
        monkeypatch, tmp_path, _compose_service(env={"PGDATA": "{root}/data/pg"}), calls
    )

    assert record["docker_context"] == "colima"
    assert calls and all(call["context"] == "colima" for call in calls)
    assert calls[0]["env"] is not None and "DOCKER_CONTEXT" not in calls[0]["env"]
