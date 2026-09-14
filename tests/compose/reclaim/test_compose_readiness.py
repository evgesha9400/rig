"""R2-6/R4-1: compose start/readiness cleans up cleanly on partial failure."""

import subprocess
from pathlib import Path

import pytest

from rig import cli as rig

stack = rig


def _write_compose_file(root, name="compose.yml"):
    path = Path(root) / name
    path.write_text("services: {}\n")
    return path


def test_compose_service_with_healthcheck_evaluates_readiness(monkeypatch, tmp_path):
    service = stack.Service(
        name="db",
        type="compose",
        cwd=Path("."),
        command=[],
        healthcheck_path="/health",
        healthcheck_timeout=1.0,
    )
    record = {"name": "db", "type": "compose", "port": 5432, "container": "c123"}

    monkeypatch.setattr(stack, "wait_for_http", lambda *args, **kwargs: True)
    monkeypatch.setattr(stack, "compose_record_alive", lambda *args, **kwargs: True)

    assert stack._await_ready(service, record, tmp_path) is True


def test_compose_record_status_error_preserves_record(monkeypatch, tmp_path):
    _write_compose_file(tmp_path, "docker-compose.yml")
    record = {
        "name": "db",
        "type": "compose",
        "instance": "inst1",
        "compose_file": "docker-compose.yml",
        "compose_service": "db",
        "container": "cont123",
    }
    mock_res = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="daemon down")
    monkeypatch.setattr(stack, "run_compose", lambda *args, **kwargs: mock_res)
    monkeypatch.setattr(
        stack,
        "run_docker",
        lambda *a, **k: subprocess.CompletedProcess(
            ["docker"], 1, "", "cannot connect to the Docker daemon"
        ),
    )

    status = stack.compose_record_status(record, tmp_path)
    assert status == "error"
    outcome = stack._stop_record(record, tmp_path)
    assert outcome == "failed"


def test_start_compose_service_cleans_up_on_port_discovery_failure(monkeypatch, tmp_path):
    service = stack.Service(
        name="db",
        type="compose",
        cwd=Path("."),
        command=[],
        compose_file="docker-compose.yml",
        compose_service="db",
        compose_port=5432,
    )
    stopped = []

    def mock_run_compose(
        instance, root, compose_file, args, context=None, timeout=180.0, env=None, **kwargs
    ):
        if "up" in args:
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="started", stderr="")
        if "ps" in args:
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="c123\n", stderr="")
        if "port" in args:
            raise RuntimeError("port inspection failed")
        if "stop" in args:
            stopped.append(args)
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="stopped", stderr="")
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    monkeypatch.setattr(stack, "run_compose", mock_run_compose)

    with pytest.raises(stack.StackError, match="compose failed to resolve port"):
        stack._start_compose_service(service, tmp_path, "inst1")

    assert len(stopped) == 1
    assert "stop" in stopped[0]
