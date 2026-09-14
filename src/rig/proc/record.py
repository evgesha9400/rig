"""Process record construction and metadata formatting."""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Mapping
from typing import Any

from rig.proc.process import process_start_time, resolve_binary, stable_process_args


def build_record(
    proc: subprocess.Popen,
    name: str,
    meta: tuple[str, list[str], int | None, Mapping[str, Any] | None],
) -> dict[str, Any]:
    """Construct structured process record dictionary."""
    kind, resolved, port, extra = meta
    observed = stable_process_args(proc.pid)
    argv0 = observed.split()[0] if observed else str(resolved[0])
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        pgid = proc.pid
    rec = {
        "name": name,
        "type": kind,
        "pid": proc.pid,
        "pgid": pgid,
        "binary": resolve_binary(argv0),
        "argv": list(resolved),
        "identity": observed,
        "start_time": process_start_time(proc.pid),
        "port": port,
        "url": f"http://127.0.0.1:{port}" if port else None,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    return {**rec, **(extra or {})}


def _record(
    name: str,
    kind: str,
    proc: subprocess.Popen,
    *args: Any,
    **kwargs: Any,
) -> dict[str, Any]:
    argv = list(args)
    resolved = kwargs.get("argv") or (argv.pop(0) if argv else [])
    port = kwargs.get("port") if "port" in kwargs else (argv.pop(0) if argv else None)
    extra = kwargs.get("extra") if "extra" in kwargs else (argv.pop(0) if argv else None)
    return build_record(proc, name, (kind, resolved, port, extra))
