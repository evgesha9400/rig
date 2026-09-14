"""R6-2/R8-2: status falls back to plain Docker when the checkout is gone."""

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


class _FakeDocker:
    """Answer plain ``docker`` calls from a script and record every invocation."""

    def __init__(self, inspect="running", ps_ids=("abc123",), failing=(), failure_stderr=None):
        self.inspect = inspect
        self.ps_ids = list(ps_ids)
        self.failing = set(failing)
        self.failure_stderr = failure_stderr
        self.calls: list[list[str]] = []

    def __call__(self, args, context=None, timeout=60.0, **kwargs):
        args = list(args)
        self.calls.append(args)
        verb = args[0]
        if verb in self.failing:
            stderr = self.failure_stderr or f"{verb} refused"
            return subprocess.CompletedProcess(["docker"], 1, "", stderr)
        if verb == "inspect":
            return subprocess.CompletedProcess(["docker"], 0, f"{self.inspect}\n", "")
        if verb == "ps":
            return subprocess.CompletedProcess(
                ["docker"], 0, "".join(f"{i}\n" for i in self.ps_ids), ""
            )
        return subprocess.CompletedProcess(["docker"], 0, "", "")


def _replica_docker(alive_state: str, survivor: str = "beef02"):
    """Answer 'no such object' for the recorded container and ``alive_state`` for a replica."""

    def fake_docker(args, context=None, timeout=60.0, **kwargs):
        args = list(args)
        if args[0] == "inspect" and args[-1] == "abc123":
            return subprocess.CompletedProcess(["docker"], 1, "", "Error: No such object: abc123")
        if args[0] == "inspect" and args[-1] == survivor:
            return subprocess.CompletedProcess(["docker"], 0, f"{alive_state}\n", "")
        return subprocess.CompletedProcess(["docker"], 0, "", "")

    return fake_docker


def test_compose_status_falls_back_to_docker_inspect_when_the_file_is_gone(monkeypatch, tmp_path):
    """R6-2: with no compose file, the recorded container ID is inspected directly."""
    _forbid_compose(monkeypatch)
    record = _compose_record(tmp_path / "deleted")

    docker = _FakeDocker(inspect="running")
    monkeypatch.setattr(stack, "run_docker", docker)
    assert stack.compose_record_status(record, tmp_path / "deleted") == "alive"
    assert docker.calls[0][0] == "ps"
    assert docker.calls[1][:2] == ["inspect", "--format"]
    assert "abc123" in docker.calls[1]

    monkeypatch.setattr(stack, "run_docker", _FakeDocker(inspect="exited"))
    assert stack.compose_record_status(record, tmp_path / "deleted") == "stopped"

    monkeypatch.setattr(stack, "run_docker", _FakeDocker(failing=("inspect",)))
    assert stack.compose_record_status(record, tmp_path / "deleted") == "error"


def test_compose_status_falls_back_to_compose_labels_when_the_container_is_unknown(
    monkeypatch, tmp_path
):
    """R6-2: with no compose file and no container ID, Docker is queried by label."""
    _forbid_compose(monkeypatch)
    record = _compose_record(tmp_path / "deleted", container="")

    docker = _FakeDocker(inspect="running", ps_ids=("cafe01",))
    monkeypatch.setattr(stack, "run_docker", docker)
    assert stack.compose_record_status(record, tmp_path / "deleted") == "alive"
    assert docker.calls[0][0] == "ps"
    assert "label=com.docker.compose.project=inst-1" in docker.calls[0]
    assert "label=com.docker.compose.service=db" in docker.calls[0]
    assert "cafe01" in docker.calls[1]

    monkeypatch.setattr(stack, "run_docker", _FakeDocker(ps_ids=()))
    assert stack.compose_record_status(record, tmp_path / "deleted") == "absent"


def test_compose_status_reports_alive_when_only_an_unrecorded_replica_survives(
    monkeypatch, tmp_path
):
    """R8-2: a deleted recorded container must not hide a running replica."""
    _write_compose_file(tmp_path)
    monkeypatch.setattr(
        stack,
        "run_compose",
        lambda *a, **k: subprocess.CompletedProcess(["docker"], 0, "beef02\n", ""),
    )
    monkeypatch.setattr(stack, "run_docker", _replica_docker("running"))

    assert stack.compose_record_status(_compose_record(tmp_path), tmp_path) == "alive"


def test_compose_status_reports_stopped_when_only_an_unrecorded_replica_remains(
    monkeypatch, tmp_path
):
    """R8-2: an exited replica is still this record's container, not an absence."""
    _write_compose_file(tmp_path)
    monkeypatch.setattr(
        stack,
        "run_compose",
        lambda *a, **k: subprocess.CompletedProcess(["docker"], 0, "beef02\n", ""),
    )
    monkeypatch.setattr(stack, "run_docker", _replica_docker("exited"))

    assert stack.compose_record_status(_compose_record(tmp_path), tmp_path) == "stopped"
