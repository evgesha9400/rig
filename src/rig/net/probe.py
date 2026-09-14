"""Port listener inspection and process group querying."""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Sequence


def run_query_command(cmd: Sequence[str], timeout: float = 1.0) -> str | None:
    """Run a query subprocess safely, returning stdout if successful."""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return proc.stdout.strip()


def _matches_pgid(p: int, pgid: int) -> bool:
    try:
        return os.getpgid(p) == pgid
    except (ProcessLookupError, PermissionError, OSError):
        return False


def _is_pid_owned(p: int, pid: int | None, pgid: int | None) -> bool:
    if pid is not None and p == pid:
        return True
    return bool(pgid is not None and _matches_pgid(p, pgid))


def _get_listener_pids(port: int) -> list[int]:
    lsof_bin = shutil.which("lsof")
    if not lsof_bin:
        return []
    output = run_query_command([lsof_bin, "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"])
    if not output:
        return []
    return [int(line.strip()) for line in output.splitlines() if line.strip().isdigit()]


def port_listener_matches(port: int, pgid: int | None = None, pid: int | None = None) -> bool:
    """Return ``True`` only when a listener on ``port`` belongs to ``pid`` or ``pgid``."""
    if pid is None and pgid is None:
        return False
    pids = _get_listener_pids(port)
    return bool(pids) and all(_is_pid_owned(p, pid, pgid) for p in pids)
