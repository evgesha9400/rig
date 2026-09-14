"""R6-2/R7-1: teardown falls back to plain Docker when the checkout is gone."""

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


def test_stop_record_falls_back_to_plain_docker_when_the_file_is_gone(monkeypatch, tmp_path):
    """R6-2: a deleted checkout must not block stopping and removing the container."""
    _forbid_compose(monkeypatch)
    docker = _FakeDocker(inspect="running")
    monkeypatch.setattr(stack, "run_docker", docker)

    outcome = stack._stop_record(_compose_record(tmp_path / "deleted"), tmp_path / "deleted")

    assert outcome == "terminated"
    assert ["stop", "abc123"] in docker.calls
    assert ["rm", "-f", "abc123"] in docker.calls


def test_stop_record_fallback_finds_the_container_by_label(monkeypatch, tmp_path):
    """R6-2: an undiscovered container is still reclaimed through compose labels."""
    _forbid_compose(monkeypatch)
    docker = _FakeDocker(inspect="running", ps_ids=("cafe01",))
    monkeypatch.setattr(stack, "run_docker", docker)

    record = _compose_record(tmp_path / "deleted", container="")
    assert stack._stop_record(record, tmp_path / "deleted") == "terminated"
    assert ["stop", "cafe01"] in docker.calls
    assert ["rm", "-f", "cafe01"] in docker.calls


def test_stop_record_fallback_reports_a_refused_removal(monkeypatch, tmp_path):
    """R6-2: a failed plain-Docker removal is reported, never silently accepted."""
    _forbid_compose(monkeypatch)
    monkeypatch.setattr(stack, "run_docker", _FakeDocker(failing=("rm",)))

    record = _compose_record(tmp_path / "deleted")
    assert stack._stop_record(record, tmp_path / "deleted") == "failed"


def test_stop_record_keeps_the_record_when_docker_is_unreachable(monkeypatch, tmp_path):
    """R7-1: teardown during an outage reports failure, so ownership is retained."""
    _forbid_compose(monkeypatch)
    monkeypatch.setattr(
        stack,
        "run_docker",
        _FakeDocker(
            failing=("inspect",),
            failure_stderr="Cannot connect to the Docker daemon at unix:///var/run/docker.sock.",
        ),
    )

    record = _compose_record(tmp_path / "deleted")
    assert stack._stop_record(record, tmp_path / "deleted") == "failed"
