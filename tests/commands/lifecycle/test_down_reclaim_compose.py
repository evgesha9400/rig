"""cmd_down: reclaiming a compose container that would otherwise be left exited."""

import json
import subprocess
from pathlib import Path

from rig import cli as rig

stack = rig


def _compose_record(root, container="abc123", compose_file="compose.yml", name="db"):
    """Build a compose state record rooted at ``root``."""
    return {
        "name": name,
        "type": "compose",
        "instance": "inst-1",
        "compose_file": str(Path(root) / compose_file),
        "compose_service": name,
        "container": container,
    }


class _FakeDocker:
    """Answer plain ``docker`` calls from a script and record every invocation."""

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


def test_cmd_down_removes_the_compose_container(monkeypatch, tmp_path):
    """`down` must reclaim the container, not leave it exited and unowned."""
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
    instance = stack.instance_id("shop", tmp_path)
    state_path = stack._state_path(tmp_path, instance=instance)
    stack.write_state(
        state_path,
        {
            "instance": instance,
            "project": "shop",
            "root": str(tmp_path),
            "services": {"db": _compose_record(tmp_path)},
        },
    )

    compose_calls: list[list[str]] = []

    def fake_run_compose(
        instance, root, cfile, args, context=None, timeout=180.0, env=None, **kwargs
    ):
        compose_calls.append(list(args))
        return subprocess.CompletedProcess(["docker"], 0, "abc123\n", "")

    monkeypatch.setattr(stack, "run_compose", fake_run_compose)
    monkeypatch.setattr(stack, "run_docker", _FakeDocker())

    assert stack.cmd_down(root=tmp_path, manifest_path=manifest_path) == stack.EXIT_OK
    assert ["rm", "-f", "db"] in compose_calls
    assert stack.read_state(state_path)["services"] == {}
