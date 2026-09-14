"""R9-5: the status report tells a running service from a stopped one."""

import subprocess
from pathlib import Path

from rig import cli as rig

stack = rig


def _write_compose_file(root, name="compose.yml"):
    path = Path(root) / name
    path.write_text("services: {}\n")
    return path


def _compose_service(name="db", **kwargs):
    return stack.Service(
        name=name, type="compose", compose_file="compose.yml", compose_service=name, **kwargs
    )


def _compose_record(root, container="abc123", compose_file="compose.yml", name="db"):
    return {
        "name": name,
        "type": "compose",
        "instance": "inst-1",
        "compose_file": str(Path(root) / compose_file),
        "compose_service": name,
        "container": container,
    }


def _status_manifest(tmp_path, service):
    return stack.Manifest(
        project="shop",
        services={service.name: service},
        scopes={"full": [service.name]},
        path=tmp_path / "rig.json",
    )


def _status_line(capsys, name: str) -> str:
    return next(
        line
        for line in capsys.readouterr().out.splitlines()
        if line.strip().startswith(f"{name} ") or line.strip().startswith(f"{name}:")
    )


def _print_compose_status(monkeypatch, tmp_path, container_state, service=None, record=None):
    _write_compose_file(tmp_path)
    monkeypatch.setattr(
        stack,
        "run_compose",
        lambda *a, **k: subprocess.CompletedProcess(["docker"], 0, "abc123\n", ""),
    )
    if container_state == "unreachable":
        monkeypatch.setattr(
            stack,
            "run_docker",
            lambda *a, **k: subprocess.CompletedProcess(
                ["docker"], 1, "", "Cannot connect to the Docker daemon"
            ),
        )
    else:
        monkeypatch.setattr(
            stack,
            "run_docker",
            lambda *a, **k: subprocess.CompletedProcess(["docker"], 0, f"{container_state}\n", ""),
        )
    service = service or _compose_service()
    state = {"generation": 3, "services": {"db": record or _compose_record(tmp_path)}}
    stack._print_status(_status_manifest(tmp_path, service), state, tmp_path)


def test_status_reports_a_running_compose_container_as_running(monkeypatch, tmp_path, capsys):
    """R9-5: a live container is still reported as running."""
    _print_compose_status(monkeypatch, tmp_path, "running")
    line = _status_line(capsys, "db")
    assert "running" in line
    assert "container=abc123" in line


def test_status_reports_a_stopped_compose_container_as_stopped(monkeypatch, tmp_path, capsys):
    """R9-5: a retained record whose container exited must not read as running."""
    _print_compose_status(monkeypatch, tmp_path, "exited")
    line = _status_line(capsys, "db")
    assert "stopped" in line
    assert "running" not in line


def test_status_reports_an_unreachable_compose_service_as_error(monkeypatch, tmp_path, capsys):
    """R9-5: an unanswered question is reported as an error, never as running."""
    _print_compose_status(monkeypatch, tmp_path, "unreachable")
    line = _status_line(capsys, "db")
    assert "error" in line
    assert "running" not in line


def test_status_skips_the_health_probe_of_a_stopped_service(monkeypatch, tmp_path, capsys):
    """R9-5: a stopped service is not probed, so it cannot be labelled healthy."""

    def forbidden(*args, **kwargs):
        raise AssertionError("a stopped service must not be health-probed")

    monkeypatch.setattr(stack, "wait_for_http", forbidden)
    service = _compose_service(compose_port=5432, healthcheck_path="/health")
    record = dict(_compose_record(tmp_path), port=5432, url="http://127.0.0.1:5432")

    _print_compose_status(monkeypatch, tmp_path, "exited", service=service, record=record)

    out = capsys.readouterr().out
    assert "stopped" in out
    assert "healthy" not in out
