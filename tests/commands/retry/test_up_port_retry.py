"""cmd_up: port-contention retry budget, and fd services skipping retry entirely."""

import json
import sys
import textwrap

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


def _port_service_manifest(tmp_path, script):
    manifest_path = tmp_path / "stack.json"
    manifest_path.write_text(
        json.dumps(
            {
                "project": "sample",
                "services": {
                    "ui": {
                        "type": "port",
                        "cwd": ".",
                        "command": [sys.executable, str(script), "--port", "{port}"],
                        "healthcheck_path": None,
                    }
                },
                "scopes": {"full": ["ui"], "ui": ["ui"]},
            }
        )
    )
    return manifest_path


def test_up_retries_a_port_service_whose_port_was_taken(tmp_path, monkeypatch):
    """A strict-port service must move to a new port, never drift onto a neighbour."""
    script = tmp_path / "binder.py"
    script.write_text(STRICT_BINDER)
    manifest_path = _port_service_manifest(tmp_path, script)
    runtime = stack.ensure_runtime_dir(tmp_path)

    squatter, taken = stack.allocate_listener()
    free = stack.reserve_port()
    handed_out = iter([taken, free])
    monkeypatch.setattr(stack, "reserve_port", lambda: next(handed_out))
    try:
        assert stack.cmd_up(root=tmp_path, manifest_path=manifest_path, scope="full") == 0

        record = stack.read_state(runtime / "state.json")["services"]["ui"]
        assert record["port"] == free, "the service must not keep the contended port"
        assert record["port"] != taken
    finally:
        stack.cmd_down(root=tmp_path, manifest_path=manifest_path, scope="full")
        squatter.close()


def test_up_gives_up_after_the_retry_budget_is_exhausted(tmp_path, monkeypatch):
    script = tmp_path / "binder.py"
    script.write_text(STRICT_BINDER)
    manifest_path = _port_service_manifest(tmp_path, script)
    runtime = stack.ensure_runtime_dir(tmp_path)

    squatter, taken = stack.allocate_listener()
    monkeypatch.setattr(stack, "reserve_port", lambda: taken)
    try:
        assert stack.cmd_up(root=tmp_path, manifest_path=manifest_path, scope="full") != 0
        assert stack.read_state(runtime / "state.json")["services"] == {}
    finally:
        squatter.close()


def test_fd_services_are_not_retried_because_they_cannot_collide(tmp_path, monkeypatch):
    calls = []
    original = stack.allocate_listener

    def counting():
        calls.append(1)
        return original()

    script = tmp_path / "dies.py"
    script.write_text("import sys\nsys.exit(1)\n")
    manifest_path = tmp_path / "stack.json"
    manifest_path.write_text(
        json.dumps(
            {
                "project": "sample",
                "services": {
                    "backend": {
                        "type": "fd",
                        "cwd": ".",
                        "command": [sys.executable, str(script), "--fd", "{fd}"],
                        "healthcheck_path": None,
                    }
                },
                "scopes": {"full": ["backend"]},
            }
        )
    )
    monkeypatch.setattr(stack, "allocate_listener", counting)

    assert stack.cmd_up(root=tmp_path, manifest_path=manifest_path, scope="full") != 0
    assert len(calls) == 1, "an inherited socket cannot be contended, so do not retry"
