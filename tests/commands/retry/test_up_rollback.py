"""cmd_up/cmd_down: rollback and dependency-preservation on partial failure."""

import json
import os
import subprocess
import sys
import textwrap

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

SAMPLE = {
    "project": "sample",
    "services": {
        "backend": {"type": "fd", "cwd": ".", "command": ["true", "--fd", "{fd}"]},
        "frontend": {
            "type": "port",
            "cwd": ".",
            "command": ["true", "--port", "{port}"],
            "depends_on": ["backend"],
        },
    },
    "scopes": {"full": ["backend", "frontend"], "backend": ["backend"]},
}


def _spawn_sleeper(tmp_path):
    script = tmp_path / "sleeper.py"
    script.write_text("import time\ntime.sleep(300)\n")
    proc = subprocess.Popen(
        [sys.executable, str(script)],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc, {
        "name": "sleeper",
        "pid": proc.pid,
        "pgid": os.getpgid(proc.pid),
        "binary": os.path.realpath(sys.executable),
        "argv": [sys.executable, str(script)],
        "start_time": stack.process_start_time(proc.pid),
        "port": None,
    }


def test_down_refuses_to_remove_a_dependency_a_running_service_still_needs(tmp_path):
    manifest_path = tmp_path / "stack.json"
    manifest_path.write_text(json.dumps(SAMPLE))
    runtime = stack.ensure_runtime_dir(tmp_path)
    proc, record = _spawn_sleeper(tmp_path)
    record["name"] = "frontend"
    stack.write_state(runtime / "state.json", {"generation": 1, "services": {"frontend": record}})
    try:
        exit_code = stack.cmd_down(root=tmp_path, manifest_path=manifest_path, scope="backend")

        assert exit_code != 0
        assert proc.poll() is None
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_up_rolls_back_services_it_started_when_a_later_service_fails(tmp_path):
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
                    },
                    "broken": {
                        "type": "fd",
                        "cwd": ".",
                        "command": [str(tmp_path / "does-not-exist"), "--fd", "{fd}"],
                        "depends_on": ["backend"],
                    },
                },
                "scopes": {"full": ["backend", "broken"]},
            }
        )
    )
    runtime = stack.ensure_runtime_dir(tmp_path)

    exit_code = stack.cmd_up(root=tmp_path, manifest_path=manifest_path, scope="full")

    assert exit_code != 0
    assert stack.read_state(runtime / "state.json")["services"] == {}


def test_up_leaves_a_pre_existing_service_running_when_a_later_service_fails(tmp_path):
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
                    },
                    "broken": {
                        "type": "fd",
                        "cwd": ".",
                        "command": [str(tmp_path / "does-not-exist"), "--fd", "{fd}"],
                        "depends_on": ["backend"],
                    },
                },
                "scopes": {"full": ["backend", "broken"], "backend": ["backend"]},
            }
        )
    )
    runtime = stack.ensure_runtime_dir(tmp_path)
    try:
        assert stack.cmd_up(root=tmp_path, manifest_path=manifest_path, scope="backend") == 0
        started = stack.read_state(runtime / "state.json")["services"]["backend"]

        assert stack.cmd_up(root=tmp_path, manifest_path=manifest_path, scope="full") != 0

        survivors = stack.read_state(runtime / "state.json")["services"]
        assert survivors["backend"]["pid"] == started["pid"]
        assert stack.identity_matches(survivors["backend"]) is True
    finally:
        stack.cmd_down(root=tmp_path, manifest_path=manifest_path, scope="full")
