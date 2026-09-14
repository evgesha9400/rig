"""Rig state home override, pid/pgid ownership tracking, settled command lines."""

import os
import signal
import stat
import subprocess
import sys

from rig import cli as rig

stack = rig


def test_rig_state_home_override(monkeypatch, tmp_path):
    custom_state = tmp_path / "custom_rig_state"
    monkeypatch.setenv("RIG_STATE_HOME", str(custom_state))

    assert stack.get_state_home() == custom_state.resolve()
    assert stack.get_instances_dir() == custom_state.resolve() / "instances"
    assert stack.get_instance_dir("my-inst") == custom_state.resolve() / "instances" / "my-inst"

    ensured = stack.ensure_instance_dir("my-inst")
    assert ensured.is_dir()
    assert stat.S_IMODE(ensured.stat().st_mode) == 0o700


def test_stable_process_args_returns_a_settled_command_line(tmp_path):
    script = tmp_path / "sleeper.py"
    script.write_text("import time\ntime.sleep(60)\n")
    proc = subprocess.Popen(
        [sys.executable, str(script)],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        settled = stack.stable_process_args(proc.pid, settle=1.0)

        assert settled == f"{sys.executable} {script}"
        assert settled == stack.process_args(proc.pid)
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_stable_process_args_tolerates_a_process_that_exits():
    assert stack.stable_process_args(999999, settle=0.2) is None


def test_terminate_record_refuses_when_actual_pgid_mismatches(monkeypatch):
    record = {
        "pid": 12345,
        "pgid": 99999,
        "binary": "/bin/sh",
        "identity": "sh",
        "start_time": "12:00",
    }
    monkeypatch.setattr(stack, "pid_alive", lambda pid: True)
    monkeypatch.setattr(stack, "identity_matches", lambda rec: True)
    monkeypatch.setattr(os, "getpgid", lambda pid: 12345)  # actual pgid is 12345, recorded is 99999
    assert stack.terminate_record(record) == "refused"


def test_terminate_record_escalates_to_kill_on_surviving_child_in_pg(monkeypatch):
    record = {
        "name": "backend",
        "pid": 1234,
        "pgid": 1234,
        "binary": "/bin/echo",
        "identity": "echo",
        "start_time": "12:00",
    }
    monkeypatch.setattr(stack, "pid_alive", lambda pid: True)
    monkeypatch.setattr(stack, "identity_matches", lambda rec: True)
    monkeypatch.setattr(os, "getpgid", lambda target: 99999 if target == 0 else 1234)

    signals_sent = []

    def mock_killpg(pgid, sig):
        signals_sent.append((pgid, sig))

    monkeypatch.setattr(os, "killpg", mock_killpg)

    call_count = [0]

    def mock_await_pg_exit(pgid, timeout):
        call_count[0] += 1
        return call_count[0] > 1

    monkeypatch.setattr(stack, "_await_pg_exit", mock_await_pg_exit)

    outcome = stack.terminate_record(record, timeout=0.01)
    assert outcome == "killed"
    assert (1234, signal.SIGTERM) in signals_sent
    assert (1234, signal.SIGKILL) in signals_sent


def test_terminate_record_refuses_when_leader_dead_but_pg_alive(monkeypatch):
    record = {
        "name": "backend",
        "pid": 1234,
        "pgid": 1234,
        "binary": "/bin/echo",
        "identity": "echo",
        "start_time": "12:00",
    }
    monkeypatch.setattr(stack, "pid_alive", lambda pid: False)
    monkeypatch.setattr(stack, "pgid_alive", lambda pgid: True)
    monkeypatch.setattr(os, "getpgid", lambda target: 99999 if target == 0 else 1234)

    assert stack.terminate_record(record) == "refused"


def test_port_listener_matches_filters_foreign_pids(monkeypatch):
    mock_run = subprocess.CompletedProcess(args=["lsof"], returncode=0, stdout="9999\n", stderr="")
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: mock_run)
    monkeypatch.setattr(os, "getpgid", lambda pid: 9999)

    assert not stack.port_listener_matches(5173, pgid=1234, pid=1234)
    assert stack.port_listener_matches(5173, pgid=1234, pid=9999)

    monkeypatch.setattr(os, "getpgid", lambda pid: 1234)
    assert stack.port_listener_matches(5173, pgid=1234, pid=1234)
