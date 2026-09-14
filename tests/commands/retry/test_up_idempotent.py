"""cmd_up: idempotent re-runs, clean teardown, and dependency-port propagation."""

import json
import os
import sys
import textwrap
import time

import pytest

from rig import cli as rig

stack = rig

ACCEPTOR = textwrap.dedent(
    """
    import socket, sys
    fd = int(sys.argv[sys.argv.index("--fd") + 1])
    listener = socket.socket(fileno=fd)
    listener.settimeout(20)
    conn, _ = listener.accept()
    conn.sendall(b"inherited")
    conn.close()
    """
)


def test_up_is_idempotent_for_an_already_running_verified_service(tmp_path):
    script = tmp_path / "acceptor.py"
    script.write_text(ACCEPTOR)
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
                "scopes": {"full": ["backend"], "backend": ["backend"]},
            }
        )
    )
    runtime = stack.ensure_runtime_dir(tmp_path)
    try:
        assert stack.cmd_up(root=tmp_path, manifest_path=manifest_path, scope="full") == 0
        first = stack.read_state(runtime / "state.json")["services"]["backend"]

        assert stack.cmd_up(root=tmp_path, manifest_path=manifest_path, scope="full") == 0
        second = stack.read_state(runtime / "state.json")["services"]["backend"]

        assert first["pid"] == second["pid"]
        assert first["port"] == second["port"]
    finally:
        stack.cmd_down(root=tmp_path, manifest_path=manifest_path, scope="full")


def test_up_then_down_leaves_no_process_and_frees_the_port(tmp_path):
    script = tmp_path / "acceptor.py"
    script.write_text(ACCEPTOR)
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
                "scopes": {"full": ["backend"], "backend": ["backend"]},
            }
        )
    )
    runtime = stack.ensure_runtime_dir(tmp_path)

    assert stack.cmd_up(root=tmp_path, manifest_path=manifest_path, scope="full") == 0
    record = stack.read_state(runtime / "state.json")["services"]["backend"]
    port = record["port"]

    assert stack.cmd_down(root=tmp_path, manifest_path=manifest_path, scope="full") == 0
    assert stack.read_state(runtime / "state.json")["services"] == {}
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and not stack.port_is_free(port):
        time.sleep(0.05)
    assert stack.port_is_free(port) is True
    with pytest.raises(ProcessLookupError):
        os.kill(record["pid"], 0)


def test_up_records_the_dependency_port_for_dependent_services(tmp_path):
    script = tmp_path / "acceptor.py"
    script.write_text(ACCEPTOR)
    reporter = tmp_path / "reporter.py"
    reporter.write_text(
        textwrap.dedent(
            """
            import os, socket, sys, time
            port = int(sys.argv[sys.argv.index("--port") + 1])
            listener = socket.socket()
            listener.bind(("127.0.0.1", port))
            listener.listen(8)
            open(os.environ["REPORT_TO"], "w").write(os.environ.get("BACKEND_PORT", ""))
            time.sleep(300)
            """
        )
    )
    report = tmp_path / "report.txt"
    manifest_path = tmp_path / "stack.json"
    manifest = {
        "project": "sample",
        "services": {
            "backend": {
                "type": "fd",
                "cwd": ".",
                "command": [sys.executable, str(script), "--fd", "{fd}"],
                "healthcheck_path": None,
            },
            "frontend": {
                "type": "port",
                "cwd": ".",
                "command": [sys.executable, str(reporter), "--port", "{port}"],
                "depends_on": ["backend"],
                "healthcheck_path": None,
                "env": {"BACKEND_PORT": "{backend_port}", "REPORT_TO": str(report)},
            },
        },
        "scopes": {"full": ["backend", "frontend"], "ui": ["frontend"]},
    }
    manifest_path.write_text(json.dumps(manifest))
    runtime = stack.ensure_runtime_dir(tmp_path)
    try:
        assert stack.cmd_up(root=tmp_path, manifest_path=manifest_path, scope="full") == 0
        services = stack.read_state(runtime / "state.json")["services"]

        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and not report.exists():
            time.sleep(0.05)
        assert report.exists(), "the dependent service never reported its configuration"
        assert report.read_text() == str(services["backend"]["port"])
    finally:
        stack.cmd_down(root=tmp_path, manifest_path=manifest_path, scope="full")
