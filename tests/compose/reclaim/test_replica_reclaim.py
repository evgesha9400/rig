"""R7-2/R8-2/R9-4: teardown reclaims replicas the state record never named."""

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


def _forbid_compose(monkeypatch):
    def _never(*args, **kwargs):
        raise AssertionError("run_compose must not run without a compose file")

    monkeypatch.setattr(stack, "run_compose", _never)


def _replica_docker(alive_state: str, survivor: str = "beef02"):
    def fake_docker(args, context=None, timeout=60.0, **kwargs):
        args = list(args)
        if args[0] == "inspect" and args[-1] == "abc123":
            return subprocess.CompletedProcess(["docker"], 1, "", "Error: No such object: abc123")
        if args[0] == "inspect" and args[-1] == survivor:
            return subprocess.CompletedProcess(["docker"], 0, f"{alive_state}\n", "")
        return subprocess.CompletedProcess(["docker"], 0, "", "")

    return fake_docker


def test_stop_record_reclaims_every_replica_of_a_scaled_service(monkeypatch, tmp_path):
    """R7-2: a replica outside the state record must still be stopped and removed."""
    _forbid_compose(monkeypatch)
    calls: list[list[str]] = []

    def fake_docker(args, context=None, timeout=60.0, **kwargs):
        args = list(args)
        calls.append(args)
        if args[0] == "ps":
            return subprocess.CompletedProcess(["docker"], 0, "abc123\nbeef02\n", "")
        if args[0] == "inspect":
            return subprocess.CompletedProcess(["docker"], 0, "running\n", "")
        return subprocess.CompletedProcess(["docker"], 0, "", "")

    monkeypatch.setattr(stack, "run_docker", fake_docker)

    record = _compose_record(tmp_path / "deleted")
    assert stack._stop_record(record, tmp_path / "deleted") == "terminated"
    assert ["stop", "abc123"] in calls
    assert ["rm", "-f", "abc123"] in calls
    assert ["stop", "beef02"] in calls
    assert ["rm", "-f", "beef02"] in calls


def test_stop_record_reports_failure_when_one_replica_survives(monkeypatch, tmp_path):
    """R7-2: a replica this rig cannot remove fails the teardown, and the rest still go."""
    _forbid_compose(monkeypatch)
    calls: list[list[str]] = []

    def fake_docker(args, context=None, timeout=60.0, **kwargs):
        args = list(args)
        calls.append(args)
        if args[0] == "ps":
            return subprocess.CompletedProcess(["docker"], 0, "abc123\nbeef02\n", "")
        if args[0] == "inspect":
            return subprocess.CompletedProcess(["docker"], 0, "running\n", "")
        if args[0] == "rm" and args[-1] == "beef02":
            return subprocess.CompletedProcess(["docker"], 1, "", "rm refused")
        return subprocess.CompletedProcess(["docker"], 0, "", "")

    monkeypatch.setattr(stack, "run_docker", fake_docker)

    record = _compose_record(tmp_path / "deleted")
    assert stack._stop_record(record, tmp_path / "deleted") == "failed"
    assert ["rm", "-f", "abc123"] in calls


def test_stop_record_reclaims_a_replica_when_the_recorded_container_is_gone(monkeypatch, tmp_path):
    """R8-2: teardown reclaims the surviving replica instead of reporting a stale record."""
    _write_compose_file(tmp_path)
    compose_calls: list[list[str]] = []

    def fake_run_compose(
        instance, root, cfile, args, context=None, timeout=180.0, env=None, **kwargs
    ):
        compose_calls.append(list(args))
        return subprocess.CompletedProcess(["docker"], 0, "beef02\n", "")

    monkeypatch.setattr(stack, "run_compose", fake_run_compose)
    monkeypatch.setattr(stack, "run_docker", _replica_docker("running"))

    outcome = stack._stop_record(_compose_record(tmp_path), tmp_path)

    assert outcome == "terminated"
    assert ["stop", "db"] in compose_calls
    assert ["rm", "-f", "db"] in compose_calls
