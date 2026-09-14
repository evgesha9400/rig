"""Tests for zero-race fd transfer of a bound listener to a spawned child."""

import os
import socket
import sys
import textwrap

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


def _spawn(tmp_path, script_text=ACCEPTOR, name="acceptor"):
    script = tmp_path / f"{name}.py"
    script.write_text(script_text)
    return stack.spawn_fd_service(
        name=name,
        argv=[sys.executable, str(script), "--fd", "{fd}"],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", "")},
        log_path=tmp_path / f"{name}.log",
    )


def test_spawn_fd_service_transfers_the_listening_socket_to_the_child(tmp_path):
    record = _spawn(tmp_path)
    try:
        with socket.create_connection(("127.0.0.1", record["port"]), timeout=20) as client:
            assert client.recv(64) == b"inherited"
    finally:
        stack.terminate_record(record, timeout=5.0)


def test_spawn_fd_service_never_releases_the_port_between_bind_and_start(tmp_path):
    record = _spawn(tmp_path)
    try:
        thief = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        with thief, pytest.raises(OSError):
            thief.bind(("127.0.0.1", record["port"]))
    finally:
        stack.terminate_record(record, timeout=5.0)


def test_spawn_fd_service_closes_the_parent_descriptor_after_popen(tmp_path):
    record = _spawn(tmp_path)
    try:
        with pytest.raises(OSError):
            os.fstat(record["fd"])
    finally:
        stack.terminate_record(record, timeout=5.0)


def test_spawn_fd_service_records_group_and_identity_evidence(tmp_path):
    record = _spawn(tmp_path)
    try:
        assert record["pid"] > 0
        assert record["pgid"] == record["pid"], "the child must lead its own process group"
        assert record["binary"] == os.path.realpath(sys.executable)
        assert record["argv"][0] == sys.executable
        assert record["start_time"]
        assert record["url"] == f"http://127.0.0.1:{record['port']}"
    finally:
        stack.terminate_record(record, timeout=5.0)


def test_spawn_fd_service_records_a_service_that_exited_instantly(tmp_path):
    """A crash-on-startup service must produce a usable record, not an exception."""
    record = _spawn(tmp_path, "import sys\nsys.exit(3)\n", name="dies")

    assert record["pid"] > 0
    assert record["pgid"] == record["pid"]
    assert stack.identity_matches(record) is False
    assert stack.terminate_record(record, timeout=1.0) == "stale"


def test_spawn_fd_service_closes_the_listener_when_the_child_cannot_start(tmp_path):
    with pytest.raises(stack.StackError):
        stack.spawn_fd_service(
            name="missing",
            argv=[str(tmp_path / "does-not-exist"), "--fd", "{fd}"],
            cwd=tmp_path,
            env={"PATH": os.environ.get("PATH", "")},
            log_path=tmp_path / "missing.log",
        )


def test_uvicorn_argv_uses_fd_transfer_and_the_environment_interpreter():
    argv = stack.uvicorn_argv(python="/venv/bin/python", app="pkg.mod:app", factory=False)

    assert argv[:4] == ["/venv/bin/python", "-m", "uvicorn", "pkg.mod:app"]
    assert "--fd" in argv
    assert argv[argv.index("--fd") + 1] == "{fd}"
    assert "--factory" not in argv
    assert "--port" not in argv


def test_uvicorn_argv_supports_application_factories():
    argv = stack.uvicorn_argv(python="/venv/bin/python", app="pkg.mod:create_app", factory=True)

    assert "--factory" in argv
