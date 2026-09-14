"""Tests for sticky port leases surviving down/up restarts."""

import json
import sys
import textwrap

import pytest

from rig import cli as rig

stack = rig

STRICT_BINDER = textwrap.dedent(
    """
    import socket, sys, time
    port = int(sys.argv[sys.argv.index("--port") + 1])
    listener = socket.socket()
    listener.bind(("127.0.0.1", port))
    listener.listen(8)
    time.sleep(300)
    """
)


def test_manifest_parses_preferred_port_and_alias(tmp_path):
    manifest_file = tmp_path / "rig.json"
    manifest_file.write_text(
        json.dumps(
            {
                "project": "sample",
                "services": {
                    "web": {
                        "type": "port",
                        "command": ["echo", "{port}"],
                        "preferred_port": 3456,
                    },
                    "api": {
                        "type": "port",
                        "command": ["echo", "{port}"],
                        "port": 8765,
                    },
                },
            }
        )
    )
    manifest = stack.load_manifest(manifest_file)
    assert manifest.services["web"].preferred_port == 3456
    assert manifest.services["api"].preferred_port == 8765

    bad_manifest = tmp_path / "bad.json"
    bad_manifest.write_text(
        json.dumps(
            {
                "project": "sample",
                "services": {
                    "web": {
                        "type": "port",
                        "command": ["echo"],
                        "preferred_port": 999999,
                    }
                },
            }
        )
    )
    with pytest.raises(stack.RigError) as exc_info:
        stack.load_manifest(bad_manifest)
    assert "preferred_port" in str(exc_info.value)


def test_cmd_up_sticky_ports_across_restarts(tmp_path):
    script = tmp_path / "binder.py"
    script.write_text(STRICT_BINDER)
    manifest_file = tmp_path / "rig.json"
    manifest_file.write_text(
        json.dumps(
            {
                "project": "sticky-sample",
                "services": {
                    "ui": {
                        "type": "port",
                        "cwd": ".",
                        "command": [sys.executable, str(script), "--port", "{port}"],
                        "healthcheck_path": None,
                    }
                },
            }
        )
    )
    runtime = stack.ensure_runtime_dir(tmp_path)
    try:
        assert stack.cmd_up(root=tmp_path, manifest_path=manifest_file, scope="full") == 0
        state1 = stack.read_state(runtime / "state.json")
        port1 = state1["services"]["ui"]["port"]
        assert state1["ports"]["ui"] == port1

        assert stack.cmd_down(root=tmp_path, manifest_path=manifest_file, scope="full") == 0
        state_after_down = stack.read_state(runtime / "state.json")
        assert state_after_down["services"] == {}
        assert state_after_down["ports"]["ui"] == port1

        assert stack.cmd_up(root=tmp_path, manifest_path=manifest_file, scope="full") == 0
        state2 = stack.read_state(runtime / "state.json")
        port2 = state2["services"]["ui"]["port"]
        assert port2 == port1
    finally:
        stack.cmd_down(root=tmp_path, manifest_path=manifest_file, scope="full")
