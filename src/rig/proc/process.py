"""Process identity inspection and liveness verification."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from collections.abc import Mapping
from typing import Any

_OWN_CHILDREN: dict[int, subprocess.Popen] = {}


def _ps(pid: int, fields: str) -> str | None:
    try:
        completed = subprocess.run(
            ["ps", "-ww", "-p", str(pid), "-o", fields],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    line = completed.stdout.strip()
    return line or None


def process_start_time(pid: int) -> str | None:
    """Return the kernel-reported start time of ``pid``."""
    value = _ps(pid, "lstart=")
    return " ".join(value.split()) if value else None


def process_args(pid: int) -> str | None:
    value = _ps(pid, "args=")
    return " ".join(value.split()) if value else None


def stable_process_args(pid: int, settle: float = 1.0, interval: float = 0.06) -> str | None:
    """Return the command line once two consecutive reads agree."""
    previous = process_args(pid)
    deadline = time.monotonic() + max(settle, 0.0)
    while time.monotonic() < deadline:
        time.sleep(interval)
        current = process_args(pid)
        if current is None or current == previous:
            return current if current is not None else previous
        previous = current
    return previous


def _is_zombie(pid: int) -> bool:
    state = _ps(pid, "state=")
    return bool(state) and state.lstrip().upper().startswith("Z")


def _collect(pid: int) -> bool:
    """Reap a child this process started. Returns ``True`` when it has exited."""
    child = _OWN_CHILDREN.get(pid)
    if child is None or child.poll() is None:
        return False
    _OWN_CHILDREN.pop(pid, None)
    return True


def _is_live_signal(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    else:
        return True


def pid_alive(pid: int) -> bool:
    """Return ``True`` when ``pid`` names a live, non-zombie process."""
    if _collect(pid) or not _is_live_signal(pid):
        return False
    if _is_zombie(pid):
        _collect(pid)
        return False
    return True


def resolve_binary(argv0: str) -> str:
    """Return the canonical executable path for ``argv0``."""
    if os.sep in argv0:
        return os.path.realpath(argv0)
    found = shutil.which(argv0)
    return os.path.realpath(found) if found else argv0


def _binary_matches(recorded: str, observed_argv0: str) -> bool:
    if os.sep in observed_argv0:
        return os.path.realpath(observed_argv0) == recorded
    return os.path.basename(recorded).startswith(observed_argv0)


def identity_baseline(record: Mapping[str, Any]) -> str:
    """Return the command line that ``pid`` must still report to be considered ours."""
    observed = record.get("identity")
    if observed:
        return str(observed)
    argv = record.get("argv") or []
    return " ".join(str(item) for item in argv)


def _record_fields_valid(record: Mapping[str, Any], baseline: str) -> bool:
    pid = record.get("pid")
    return (
        isinstance(pid, int)
        and pid > 0
        and bool(record.get("start_time") and baseline and record.get("binary"))
    )


def identity_matches(record: Mapping[str, Any]) -> bool:
    """Return ``True`` only when ``pid`` still runs exactly the recorded program."""
    baseline = identity_baseline(record)
    if not _record_fields_valid(record, baseline):
        return False
    pid = int(record["pid"])
    if not pid_alive(pid) or process_start_time(pid) != record.get("start_time"):
        return False
    observed = process_args(pid)
    if not observed or observed != baseline:
        return False
    return _binary_matches(str(record.get("binary")), observed.split()[0])
