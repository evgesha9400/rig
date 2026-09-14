"""R9-4/R3-2: a deleted recorded container never fails a successful reclaim."""

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


def _write_compose_file(root, name="compose.yml"):
    path = Path(root) / name
    path.write_text("services: {}\n")
    return path


def _forbid_compose(monkeypatch):
    def _never(*args, **kwargs):
        raise AssertionError("run_compose must not run without a compose file")

    monkeypatch.setattr(stack, "run_compose", _never)


def _deleted_recorded_container_docker(calls: list[list[str]], survivor="beef02", refuse=()):
    def fake_docker(args, context=None, timeout=60.0, **kwargs):
        args = list(args)
        calls.append(args)
        verb = args[0]
        if verb == "ps":
            return subprocess.CompletedProcess(["docker"], 0, f"{survivor}\n", "")
        if args[-1] == "abc123":
            return subprocess.CompletedProcess(
                ["docker"], 1, "", "Error response from daemon: No such container: abc123"
            )
        if verb in refuse:
            return subprocess.CompletedProcess(["docker"], 1, "", "permission denied")
        if verb == "inspect":
            return subprocess.CompletedProcess(["docker"], 0, "running\n", "")
        return subprocess.CompletedProcess(["docker"], 0, "", "")

    return fake_docker


def test_docker_teardown_treats_an_already_deleted_container_as_reclaimed(monkeypatch, tmp_path):
    _forbid_compose(monkeypatch)
    calls: list[list[str]] = []
    monkeypatch.setattr(stack, "run_docker", _deleted_recorded_container_docker(calls))

    outcome = stack.docker_record_stop(_compose_record(tmp_path / "deleted"), remove=True)

    assert outcome == "terminated"
    assert ["stop", "beef02"] in calls
    assert ["rm", "-f", "beef02"] in calls
    assert ["rm", "-f", "abc123"] not in calls


def test_docker_teardown_still_fails_when_a_live_container_refuses(monkeypatch, tmp_path):
    _forbid_compose(monkeypatch)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        stack, "run_docker", _deleted_recorded_container_docker(calls, refuse=("stop",))
    )

    assert stack.docker_record_stop(_compose_record(tmp_path / "deleted"), remove=True) == "failed"


def test_docker_teardown_reports_stale_when_every_target_is_already_gone(monkeypatch, tmp_path):
    _forbid_compose(monkeypatch)
    calls: list[list[str]] = []

    def fake_docker(args, context=None, timeout=60.0, **kwargs):
        args = list(args)
        calls.append(args)
        if args[0] == "ps":
            return subprocess.CompletedProcess(["docker"], 0, "", "")
        return subprocess.CompletedProcess(
            ["docker"], 1, "", "Error response from daemon: No such container: abc123"
        )

    monkeypatch.setattr(stack, "run_docker", fake_docker)

    assert (
        stack.docker_record_stop(_compose_record(tmp_path / "deleted"), remove=True) == "terminated"
    )
    assert ["stop", "abc123"] in calls


def test_stop_record_reclaims_a_replica_when_the_checkout_and_container_are_gone(
    monkeypatch, tmp_path
):
    _forbid_compose(monkeypatch)
    calls: list[list[str]] = []
    monkeypatch.setattr(stack, "run_docker", _deleted_recorded_container_docker(calls))

    record = _compose_record(tmp_path / "deleted")
    assert stack._stop_record(record, tmp_path / "deleted") == "terminated"
    assert ["rm", "-f", "beef02"] in calls


def test_stop_record_reclaims_undiscovered_compose_container(monkeypatch, tmp_path):
    """R3-2: an untracked container is stopped and removed by service name."""
    _write_compose_file(tmp_path)
    calls: list[list[str]] = []

    def fake_run_compose(
        instance, root, cfile, args, context=None, timeout=180.0, env=None, **kwargs
    ):
        calls.append(list(args))
        return subprocess.CompletedProcess(["docker"], 0, "abc123\n", "")

    monkeypatch.setattr(stack, "run_compose", fake_run_compose)
    monkeypatch.setattr(
        stack,
        "run_docker",
        lambda *a, **k: subprocess.CompletedProcess(["docker"], 0, "running\n", ""),
    )

    record = {
        "name": "db",
        "type": "compose",
        "instance": "inst-1",
        "compose_file": "compose.yml",
        "compose_service": "db",
        "container": "",
    }
    assert stack._stop_record(record, tmp_path) == "terminated"
    verbs = [c[0] for c in calls]
    assert "stop" in verbs and "rm" in verbs
    assert ["rm", "-f", "db"] in calls
