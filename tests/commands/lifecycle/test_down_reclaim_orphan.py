"""cmd_down --all: reclaiming compose containers of a deleted checkout."""

import json
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


class _FakeDocker:
    def __init__(self, inspect="running", ps_ids=("abc123",)):
        self.inspect = inspect
        self.ps_ids = list(ps_ids)
        self.calls: list[list[str]] = []

    def __call__(self, args, context=None, timeout=60.0, **kwargs):
        args = list(args)
        self.calls.append(args)
        verb = args[0]
        if verb == "inspect":
            return subprocess.CompletedProcess(["docker"], 0, f"{self.inspect}\n", "")
        if verb == "ps":
            return subprocess.CompletedProcess(
                ["docker"], 0, "".join(f"{i}\n" for i in self.ps_ids), ""
            )
        return subprocess.CompletedProcess(["docker"], 0, "", "")


def _write_instance(ident: str, state: dict):
    inst_dir = stack.ensure_instance_dir(ident)
    stack.write_state(inst_dir / stack.STATE_FILE_NAME, state)
    return inst_dir


def _forbid_compose(monkeypatch):
    """Fail the test if Compose is invoked without its file on disk."""

    def _never(*args, **kwargs):
        raise AssertionError("run_compose must not run without a compose file")

    monkeypatch.setattr(stack, "run_compose", _never)


def test_cmd_down_keeps_the_record_when_a_replica_survives(monkeypatch, tmp_path, capsys):
    """A service with a surviving replica keeps its ownership record."""
    monkeypatch.setenv("RIG_STATE_HOME", str(tmp_path / "rig_state"))
    gone = tmp_path / "deleted-checkout"
    _write_instance(
        "orphan-00000000",
        {
            "instance": "orphan-00000000",
            "project": "orphan",
            "root": str(gone),
            "services": {"db": _compose_record(gone)},
        },
    )
    _forbid_compose(monkeypatch)

    def fake_docker(args, context=None, timeout=60.0, **kwargs):
        args = list(args)
        if args[0] == "ps":
            return subprocess.CompletedProcess(["docker"], 0, "abc123\nbeef02\n", "")
        if args[0] == "inspect":
            return subprocess.CompletedProcess(["docker"], 0, "running\n", "")
        if args[0] == "rm" and args[-1] == "beef02":
            return subprocess.CompletedProcess(["docker"], 1, "", "rm refused")
        return subprocess.CompletedProcess(["docker"], 0, "", "")

    monkeypatch.setattr(stack, "run_docker", fake_docker)

    stack.cmd_down(all_instances=True, as_json=True)
    capsys.readouterr()

    state_file = stack.get_instances_dir() / "orphan-00000000" / stack.STATE_FILE_NAME
    assert "db" in stack.read_state(state_file)["services"]


def test_cmd_down_all_reclaims_an_instance_whose_checkout_was_deleted(
    monkeypatch, tmp_path, capsys
):
    """`down --all` must stop compose services of a deleted checkout."""
    monkeypatch.setenv("RIG_STATE_HOME", str(tmp_path / "rig_state"))
    gone = tmp_path / "deleted-checkout"
    _write_instance(
        "orphan-00000000",
        {
            "instance": "orphan-00000000",
            "project": "orphan",
            "root": str(gone),
            "services": {"db": _compose_record(gone)},
        },
    )
    _forbid_compose(monkeypatch)
    docker = _FakeDocker(inspect="running")
    monkeypatch.setattr(stack, "run_docker", docker)

    assert stack.cmd_down(all_instances=True, as_json=True) == stack.EXIT_OK
    envelope = json.loads(capsys.readouterr().out)

    assert envelope["data"]["instances"][0]["stopped"] == ["db"]
    assert ["rm", "-f", "abc123"] in docker.calls
    state_file = stack.get_instances_dir() / "orphan-00000000" / stack.STATE_FILE_NAME
    assert stack.read_state(state_file)["services"] == {}
