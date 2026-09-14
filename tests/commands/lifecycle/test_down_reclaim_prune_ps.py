"""cmd_prune and cmd_ps: reclaiming a deleted checkout and reporting instance status."""

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


def test_cmd_prune_reclaims_an_instance_whose_checkout_was_deleted(monkeypatch, tmp_path, capsys):
    """`prune --force` must reclaim a live container of a deleted checkout."""
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

    assert stack.cmd_prune(force=True, as_json=True) == stack.EXIT_OK
    envelope = json.loads(capsys.readouterr().out)

    assert envelope["data"]["pruned"] == ["orphan-00000000"]
    assert envelope["data"]["failed"] == []
    assert ["rm", "-f", "abc123"] in docker.calls


def test_cmd_ps_reports_partial_and_orphaned_instances(monkeypatch, tmp_path, capsys):
    """`ps` must distinguish partial and orphaned instances from running ones."""
    monkeypatch.setenv("RIG_STATE_HOME", str(tmp_path / "rig_state"))
    _write_instance(
        "partial-00000000",
        {
            "instance": "partial-00000000",
            "project": "partial",
            "root": str(tmp_path),
            "services": {
                "up": {"name": "up", "type": "port", "pid": 11, "pgid": 11},
                "down": {"name": "down", "type": "port", "pid": 22, "pgid": 22},
            },
        },
    )
    _write_instance(
        "orphan-00000000",
        {
            "instance": "orphan-00000000",
            "project": "orphan",
            "root": str(tmp_path / "deleted-checkout"),
            "services": {"up": {"name": "up", "type": "port", "pid": 33, "pgid": 33}},
        },
    )
    _write_instance(
        "idle-00000000",
        {
            "instance": "idle-00000000",
            "project": "idle",
            "root": str(tmp_path),
            "services": {"down": {"name": "down", "type": "port", "pid": 22, "pgid": 22}},
        },
    )

    monkeypatch.setattr(stack, "pid_alive", lambda pid: pid != 22)
    monkeypatch.setattr(stack, "identity_matches", lambda rec: True)

    assert stack.cmd_ps(as_json=True) == stack.EXIT_OK
    instances = {
        item["instance"]: item for item in json.loads(capsys.readouterr().out)["data"]["instances"]
    }

    assert instances["partial-00000000"]["status"] == "partial"
    assert instances["partial-00000000"]["services_running"] == 1
    assert instances["orphan-00000000"]["status"] == "orphaned"
    assert instances["idle-00000000"]["status"] == "stopped"
