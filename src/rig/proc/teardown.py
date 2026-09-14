"""Process and process-group termination and signal escalation."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import time
from collections.abc import Mapping
from typing import Any

from rig.core.constants import TEARDOWN_TIMEOUT_SECS
from rig.net.probe import run_query_command
from rig.proc.process import (
    _collect,
    _is_zombie,
    identity_matches,
    pid_alive,
)


def _has_active_pg_members(pgid: int) -> bool:
    """Return ``True`` if any non-zombie process belongs to process group ``pgid``."""
    output = run_query_command(["ps", "-o", "stat=", "-g", str(pgid)])
    if not output:
        return False
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    return any(not line.startswith("Z") for line in lines)


def pgid_alive(pgid: int) -> bool:
    """Return ``True`` when at least one process in process group ``pgid`` is alive."""
    _collect(pgid)
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        _collect(pgid)
        return _has_active_pg_members(pgid)
    else:
        if _is_zombie(pgid):
            _collect(pgid)
            return _has_active_pg_members(pgid)
        return True


def _await_pg_exit(pgid: int, timeout: float) -> bool:
    deadline = time.monotonic() + max(timeout, 0.1)
    while True:
        if not pgid_alive(pgid):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def _validate_record_target(record: Mapping[str, Any]) -> tuple[int, int] | None:
    pid = record.get("pid")
    pgid = record.get("pgid")
    if not isinstance(pid, int) or not isinstance(pgid, int) or pid <= 0 or pgid <= 0:
        return None
    if pid == os.getpid() or pgid == os.getpgid(0):
        return None
    return pid, pgid


def _is_our_pgid(pid: int, pgid: int) -> bool:
    try:
        return os.getpgid(pid) == pgid
    except (ProcessLookupError, PermissionError):
        return False


def _verify_process_ownership(record: Mapping[str, Any], pid: int, pgid: int) -> str | None:
    if not pid_alive(pid):
        return "stale" if not pgid_alive(pgid) else "refused"
    if not identity_matches(record) or not _is_our_pgid(pid, pgid):
        return "refused"
    return None


def _kill_permission_fallback(pgid: int, pid: int) -> str:
    if not pgid_alive(pgid):
        _collect(pid)
        return "terminated"
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(["pkill", "-9", "-g", str(pgid)], check=False)
    if not pgid_alive(pgid):
        _collect(pid)
        return "killed"
    return "refused"


def _kill_process_group(pgid: int, pid: int, timeout: float) -> str:
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        _collect(pid)
        return "terminated"
    except PermissionError:
        return _kill_permission_fallback(pgid, pid)

    if _await_pg_exit(pgid, timeout):
        _collect(pid)
        return "killed"
    return "failed"


def _signal_term_process_group(pgid: int, pid: int, timeout: float) -> str | None:
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return "stale"
    except PermissionError:
        return "stale" if not pgid_alive(pgid) else "refused"
    if _await_pg_exit(pgid, timeout):
        _collect(pid)
        return "terminated"
    return None


def terminate_record(record: Mapping[str, Any], timeout: float = TEARDOWN_TIMEOUT_SECS) -> str:
    """Stop the recorded process group."""
    target = _validate_record_target(record)
    if target is None:
        return "refused"
    pid, pgid = target

    recheck = _verify_process_ownership(record, pid, pgid)
    if recheck is not None:
        return recheck

    term_result = _signal_term_process_group(pgid, pid, timeout)
    if term_result is not None:
        return term_result

    return _kill_process_group(pgid, pid, timeout)
