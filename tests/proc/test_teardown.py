"""Terminate stops the process group; SIGTERM/SIGKILL escalation; port release."""

import os
import subprocess
import sys
import textwrap
import time

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


def _spawn_sleeper(tmp_path, ignore_sigterm=False):
    body = "import signal, time\n"
    if ignore_sigterm:
        body += "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    body += "time.sleep(300)\n"
    script = tmp_path / ("ignorer.py" if ignore_sigterm else "sleeper.py")
    script.write_text(body)
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
        "url": None,
    }


def test_terminate_stops_the_recorded_process_group(tmp_path):
    proc, record = _spawn_sleeper(tmp_path)

    outcome = stack.terminate_record(record, timeout=5.0)

    assert outcome == "terminated"
    assert proc.wait(timeout=5) is not None
    assert stack.identity_matches(record) is False


def test_terminate_escalates_to_sigkill_when_sigterm_is_ignored(tmp_path):
    proc, record = _spawn_sleeper(tmp_path, ignore_sigterm=True)

    outcome = stack.terminate_record(record, timeout=0.5)

    assert outcome == "killed"
    assert proc.wait(timeout=5) is not None


def test_terminate_refuses_to_signal_an_unverifiable_record(tmp_path):
    proc, record = _spawn_sleeper(tmp_path)
    try:
        record["binary"] = "/usr/bin/totally-different"

        outcome = stack.terminate_record(record, timeout=1.0)

        assert outcome == "refused"
        assert proc.poll() is None, "an unverified process must be left running"
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_terminate_reports_an_already_dead_process_as_stale(tmp_path):
    proc, record = _spawn_sleeper(tmp_path)
    proc.kill()
    proc.wait(timeout=5)

    assert stack.terminate_record(record, timeout=1.0) == "stale"


def test_terminate_never_signals_the_orchestrator_own_group():
    record = {
        "name": "self",
        "pid": os.getpid(),
        "pgid": os.getpgid(0),
        "binary": os.path.realpath(sys.executable),
        "argv": sys.argv,
        "start_time": stack.process_start_time(os.getpid()),
    }

    assert stack.terminate_record(record, timeout=0.2) == "refused"


def test_terminate_releases_the_service_port(tmp_path):
    script = tmp_path / "acceptor.py"
    script.write_text(ACCEPTOR)
    record = stack.spawn_fd_service(
        name="acceptor",
        argv=[sys.executable, str(script), "--fd", "{fd}"],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", "")},
        log_path=tmp_path / "acceptor.log",
    )
    port = record["port"]

    assert stack.terminate_record(record, timeout=5.0) in ("terminated", "killed")

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and not stack.port_is_free(port):
        time.sleep(0.05)
    assert stack.port_is_free(port) is True
