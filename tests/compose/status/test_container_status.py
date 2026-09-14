"""R3-2/R4-2/R9-5: container status reflects what Docker actually confirms."""

import subprocess
from pathlib import Path

from rig import cli as rig

stack = rig


def _write_compose_file(root, name="compose.yml"):
    path = Path(root) / name
    path.write_text("services: {}\n")
    return path


def _undiscovered_record():
    return {
        "name": "db",
        "type": "compose",
        "instance": "inst-1",
        "compose_file": "compose.yml",
        "compose_service": "db",
        "container": "",
    }


def test_compose_status_probes_service_when_container_unknown(monkeypatch, tmp_path):
    """R3-2: an empty container ID must not be read as `absent` without asking Docker."""
    _write_compose_file(tmp_path)
    monkeypatch.setattr(
        stack,
        "run_compose",
        lambda *a, **k: subprocess.CompletedProcess(["docker"], 0, "abc123\n", ""),
    )
    inspected: list[list[str]] = []

    def fake_run_docker(args, context=None, timeout=60.0, **kwargs):
        inspected.append(list(args))
        return subprocess.CompletedProcess(["docker"], 0, "running\n", "")

    monkeypatch.setattr(stack, "run_docker", fake_run_docker)

    assert stack.compose_record_status(_undiscovered_record(), tmp_path) == "alive"
    assert inspected and inspected[0][0] == "inspect"
    assert "abc123" in inspected[0]


def test_compose_status_absent_only_when_docker_confirms(monkeypatch, tmp_path):
    """R3-2: `absent` requires a successful Compose query that found no container."""
    _write_compose_file(tmp_path)
    monkeypatch.setattr(
        stack,
        "run_compose",
        lambda *a, **k: subprocess.CompletedProcess(["docker"], 0, "\n", ""),
    )
    assert stack.compose_record_status(_undiscovered_record(), tmp_path) == "absent"

    monkeypatch.setattr(
        stack,
        "run_compose",
        lambda *a, **k: subprocess.CompletedProcess(["docker"], 1, "", "daemon down"),
    )
    monkeypatch.setattr(
        stack,
        "run_docker",
        lambda *a, **k: subprocess.CompletedProcess(
            ["docker"], 1, "", "cannot connect to the Docker daemon"
        ),
    )
    assert stack.compose_record_status(_undiscovered_record(), tmp_path) == "error"

    monkeypatch.setattr(
        stack,
        "run_compose",
        lambda *a, **k: subprocess.CompletedProcess(["docker"], 0, "abc123\n", ""),
    )
    monkeypatch.setattr(
        stack,
        "run_docker",
        lambda *a, **k: subprocess.CompletedProcess(["docker"], 1, "", "no such object"),
    )
    assert stack.compose_record_status(_undiscovered_record(), tmp_path) == "error"


def test_compose_status_queries_all_and_reports_stopped(monkeypatch, tmp_path):
    """R4-2: `compose_record_status` queries with `-a` and inspects container status."""
    _write_compose_file(tmp_path)
    ps_args: list[list[str]] = []

    def fake_compose(instance, root, cfile, args, context=None, timeout=60.0, env=None, **kwargs):
        ps_args.append(list(args))
        return subprocess.CompletedProcess(["docker"], 0, "c123\n", "")

    def fake_docker(args, context=None, timeout=60.0, **kwargs):
        return subprocess.CompletedProcess(["docker"], 0, "exited\n", "")

    monkeypatch.setattr(stack, "run_compose", fake_compose)
    monkeypatch.setattr(stack, "run_docker", fake_docker)

    record = {
        "name": "db",
        "type": "compose",
        "instance": "inst1",
        "compose_file": "compose.yml",
        "compose_service": "db",
        "container": "c123",
    }
    status = stack.compose_record_status(record, tmp_path)
    assert status == "stopped"
    assert any("-a" in c or "--all" in c for c in ps_args)
