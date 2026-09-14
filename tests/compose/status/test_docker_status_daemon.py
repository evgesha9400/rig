"""R7-1/R7-2/R8-1: docker_record_status/stop distinguish outages from absence."""

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


def _unanswered_labels_docker(calls: list[list[str]]):
    """Refuse the label query, and answer every inspect with 'no such object'."""

    def fake_docker(args, context=None, timeout=60.0, **kwargs):
        args = list(args)
        calls.append(args)
        if args[0] == "ps":
            return subprocess.CompletedProcess(
                ["docker"],
                1,
                "",
                "Cannot connect to the Docker daemon at unix:///var/run/docker.sock.",
            )
        if args[0] == "inspect":
            return subprocess.CompletedProcess(["docker"], 1, "", "Error: No such object: abc123")
        return subprocess.CompletedProcess(["docker"], 0, "", "")

    return fake_docker


def test_docker_status_reports_error_when_the_daemon_is_unreachable(monkeypatch, tmp_path):
    """R7-1: an unreachable daemon must not be read as a missing container."""
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
    assert stack.docker_record_status(record) == "error"


def test_docker_status_reports_absent_only_when_docker_confirms_no_such_object(
    monkeypatch, tmp_path
):
    """R7-1: only Docker's own 'no such object' answer proves the container is gone."""
    _forbid_compose(monkeypatch)
    monkeypatch.setattr(
        stack,
        "run_docker",
        _FakeDocker(failing=("inspect",), failure_stderr="Error: No such object: abc123"),
    )

    record = _compose_record(tmp_path / "deleted")
    assert stack.docker_record_status(record) == "absent"


def test_docker_status_reports_alive_when_any_replica_still_runs(monkeypatch, tmp_path):
    """R7-2: one running replica keeps the whole service alive."""
    _forbid_compose(monkeypatch)

    def fake_docker(args, context=None, timeout=60.0, **kwargs):
        args = list(args)
        if args[0] == "ps":
            return subprocess.CompletedProcess(["docker"], 0, "abc123\nbeef02\n", "")
        state = "running" if args[-1] == "beef02" else "exited"
        return subprocess.CompletedProcess(["docker"], 0, f"{state}\n", "")

    monkeypatch.setattr(stack, "run_docker", fake_docker)
    assert stack.docker_record_status(_compose_record(tmp_path / "deleted")) == "alive"


def test_docker_status_reports_error_when_the_label_query_fails(monkeypatch, tmp_path):
    """R8-1: an unanswered label query must not report `absent` for a gone container."""
    _forbid_compose(monkeypatch)
    calls: list[list[str]] = []
    monkeypatch.setattr(stack, "run_docker", _unanswered_labels_docker(calls))

    record = _compose_record(tmp_path / "deleted")
    assert stack.docker_record_status(record) == "error"
    assert ["ps"] == [c[0] for c in calls if c[0] == "ps"]


def test_docker_stop_reports_failure_when_the_label_query_fails(monkeypatch, tmp_path):
    """R8-1: teardown cannot claim success while un-queried replicas may survive."""
    _forbid_compose(monkeypatch)
    calls: list[list[str]] = []
    monkeypatch.setattr(stack, "run_docker", _unanswered_labels_docker(calls))

    record = _compose_record(tmp_path / "deleted")
    assert stack.docker_record_stop(record, remove=True) == "failed"
    assert ["stop", "abc123"] in calls
    assert ["rm", "-f", "abc123"] in calls
