"""R5-1/R6-1: forced reclaims and teardown keep or drop a compose record correctly."""

import json
import subprocess

from rig import cli as rig

stack = rig


class _FakeDocker:
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


def _compose_record(root, container="abc123", compose_file="compose.yml", name="db"):
    from pathlib import Path

    return {
        "name": name,
        "type": "compose",
        "instance": "inst-1",
        "compose_file": str(Path(root) / compose_file),
        "compose_service": name,
        "container": container,
    }


def test_rollback_removes_the_compose_container(monkeypatch, tmp_path):
    """R6-1: rollback drops the record, so it must remove the container too."""
    monkeypatch.setenv("RIG_STATE_HOME", str(tmp_path / "rig_state"))
    (tmp_path / "compose.yml").write_text("services: {}\n")
    manifest_path = tmp_path / "rig.json"
    manifest_path.write_text(
        json.dumps(
            {
                "project": "shop",
                "services": {
                    "db": {
                        "type": "compose",
                        "compose_file": "compose.yml",
                        "compose_service": "db",
                    }
                },
                "scopes": {"full": ["db"]},
            }
        )
    )
    manifest = stack.load_manifest(manifest_path)
    state_path = tmp_path / "state.json"
    state = {"services": {"db": _compose_record(tmp_path)}}

    compose_calls: list[list[str]] = []

    def fake_run_compose(
        instance, root, cfile, args, context=None, timeout=180.0, env=None, **kwargs
    ):
        compose_calls.append(list(args))
        return subprocess.CompletedProcess(["docker"], 0, "abc123\n", "")

    monkeypatch.setattr(stack, "run_compose", fake_run_compose)
    monkeypatch.setattr(stack, "run_docker", _FakeDocker())

    stack._rollback(state, state_path, ["db"], tmp_path, manifest)

    assert ["rm", "-f", "db"] in compose_calls
    assert state["services"] == {}


def test_stop_record_keeps_the_record_when_removal_fails(monkeypatch, tmp_path):
    """R6-1: a container this rig cannot remove keeps its ownership record."""
    (tmp_path / "compose.yml").write_text("services: {}\n")

    def fake_run_compose(
        instance, root, cfile, args, context=None, timeout=180.0, env=None, **kwargs
    ):
        if args[0] == "rm":
            return subprocess.CompletedProcess(["docker"], 1, "", "rm refused")
        return subprocess.CompletedProcess(["docker"], 0, "abc123\n", "")

    monkeypatch.setattr(stack, "run_compose", fake_run_compose)
    monkeypatch.setattr(stack, "run_docker", _FakeDocker(failing=("rm",)))

    assert stack._stop_record(_compose_record(tmp_path), tmp_path) == "failed"


def test_force_stop_retains_record_when_compose_removal_fails(monkeypatch, tmp_path):
    """R5-1: a container that cannot be removed keeps its ownership record."""
    (tmp_path / "compose.yml").write_text("services: {}\n")
    calls: list[list[str]] = []

    def fake_run_compose(
        instance, root, cfile, args, context=None, timeout=180.0, env=None, **kwargs
    ):
        calls.append(list(args))
        if args and args[0] == "ps":
            return subprocess.CompletedProcess(["docker"], 0, "c0ffee\n", "")
        if args and args[0] == "rm":
            return subprocess.CompletedProcess(["docker"], 1, "", "device or resource busy")
        return subprocess.CompletedProcess(["docker"], 0, "", "")

    monkeypatch.setattr(stack, "run_compose", fake_run_compose)
    monkeypatch.setattr(stack, "run_docker", _FakeDocker(failing=("rm",)))

    state = {
        "instance": "recl-1",
        "project": "recl",
        "services": {"db": _compose_record(tmp_path, container="c0ffee")},
    }
    failures = stack._force_stop_instance(state, tmp_path / "state.json", tmp_path)

    assert failures == ["db: failed"]
    assert "db" in state["services"]
    assert ["rm", "-f", "db"] in calls
