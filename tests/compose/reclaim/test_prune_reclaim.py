"""R9-4: `prune` drops an instance whose recorded container is gone."""

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


def _deleted_recorded_container_docker(calls: list[list[str]], survivor="beef02", refuse=()):
    """Report the recorded container gone while ``survivor`` still exists."""

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
        return subprocess.CompletedProcess(["docker"], 0, "", "")

    return fake_docker


def test_cmd_prune_reclaims_a_replica_when_the_recorded_container_is_gone(
    monkeypatch, tmp_path, capsys
):
    """R9-4: `prune` drops the instance instead of reporting a failure it cannot fix."""
    monkeypatch.setenv("RIG_STATE_HOME", str(tmp_path / "rig_state"))
    _forbid_compose(monkeypatch)
    deleted_root = tmp_path / "deleted"
    instance = "gone-inst"
    inst_dir = stack.ensure_instance_dir(instance)
    stack.write_state(
        inst_dir / "state.json",
        {
            "instance": instance,
            "project": "shop",
            "root": str(deleted_root),
            "services": {"db": _compose_record(deleted_root)},
        },
    )
    calls: list[list[str]] = []
    monkeypatch.setattr(stack, "run_docker", _deleted_recorded_container_docker(calls))

    assert stack.cmd_prune(force=True) == stack.EXIT_OK
    assert ["rm", "-f", "beef02"] in calls
    assert not (inst_dir / "state.json").exists()
