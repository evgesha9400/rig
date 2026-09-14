"""Process identity verification: baseline command lines, binary/pid checks."""

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


def test_identity_verification_accepts_the_recorded_process(tmp_path):
    proc, record = _spawn_sleeper(tmp_path)
    try:
        assert stack.identity_matches(record) is True
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_identity_verification_survives_a_launcher_that_replaces_itself(tmp_path):
    """A shebang script is replaced by its interpreter, exactly as `npm` becomes `node`.

    Verification must compare against what the kernel reported at spawn time, not
    against the requested argument vector, or a healthy service is pruned and orphaned.
    """
    script = tmp_path / "wrapper.py"
    script.write_text(f"#!{sys.executable}\n" + ACCEPTOR)
    script.chmod(0o700)

    record = stack.spawn_fd_service(
        name="wrapper",
        argv=[str(script), "--fd", "{fd}"],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", "")},
        log_path=tmp_path / "wrapper.log",
    )
    try:
        observed = stack.process_args(record["pid"])
        assert observed != " ".join(record["argv"]), (
            "this test is only meaningful when the kernel rewrites the command line"
        )
        assert record["identity"] == observed
        assert stack.identity_matches(record) is True
    finally:
        assert stack.terminate_record(record, timeout=5.0) in ("terminated", "killed")


def test_identity_baseline_prefers_the_observed_command_line():
    record = {"identity": "node /path/vite --port 1", "argv": ["npm", "run", "dev"]}

    assert stack.identity_baseline(record) == "node /path/vite --port 1"


def test_identity_baseline_falls_back_to_the_requested_argv():
    record = {"argv": ["/bin/sleep", "300"]}

    assert stack.identity_baseline(record) == "/bin/sleep 300"


def test_identity_verification_rejects_a_mismatched_start_time(tmp_path):
    proc, record = _spawn_sleeper(tmp_path)
    try:
        record["start_time"] = "1999-01-01T00:00:00"
        assert stack.identity_matches(record) is False
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_identity_verification_rejects_a_mismatched_binary(tmp_path):
    proc, record = _spawn_sleeper(tmp_path)
    try:
        record["binary"] = "/usr/bin/totally-different"
        assert stack.identity_matches(record) is False
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_identity_verification_rejects_a_dead_pid(tmp_path):
    proc, record = _spawn_sleeper(tmp_path)
    proc.kill()
    proc.wait(timeout=5)

    assert stack.identity_matches(record) is False


def test_identity_verification_rejects_a_reused_pid_running_something_else(tmp_path):
    proc, record = _spawn_sleeper(tmp_path)
    try:
        record["argv"] = [sys.executable, str(tmp_path / "some-other-script.py")]
        assert stack.identity_matches(record) is False
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_identity_verification_rejects_a_record_without_evidence():
    assert stack.identity_matches({"pid": os.getpid()}) is False
    assert stack.identity_matches({}) is False
