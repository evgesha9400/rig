"""Process spawning with socket inheritance and strict port contracts."""

from __future__ import annotations

import contextlib
import os
import signal
import socket
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from rig.core.constants import DIR_MODE_PRIVATE
from rig.core.env import render
from rig.core.errors import RigError
from rig.net.ports import _safe_allocate_listener
from rig.proc.process import _OWN_CHILDREN
from rig.proc.record import _record


def uvicorn_argv(python: str, app: str, factory: bool = False) -> list[str]:
    """Return an argv that runs uvicorn on an inherited socket descriptor."""
    return [python, "-m", "uvicorn", app, *(["--factory"] if factory else []), "--fd", "{fd}"]


def _open_log(log_path: Path):
    Path(log_path).parent.mkdir(mode=DIR_MODE_PRIVATE, parents=True, exist_ok=True)
    return open(log_path, "ab", buffering=0)


def _popen_service(
    cmd: tuple[str, list[str]],
    ctx: tuple[Path, Mapping[str, str], Any],
    pass_fds: tuple[int, ...] = (),
) -> subprocess.Popen:
    name, resolved = cmd
    cwd, env, log = ctx
    proc = None
    try:
        proc = subprocess.Popen(
            resolved,
            cwd=str(cwd),
            env=dict(env),
            pass_fds=pass_fds,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        _OWN_CHILDREN[proc.pid] = proc
    except BaseException as exc:
        if proc is not None:
            with contextlib.suppress(OSError):
                os.killpg(proc.pid, signal.SIGKILL)
        if isinstance(exc, OSError):
            raise RigError(f"cannot start service {name}: {exc}") from None
        raise
    else:
        return proc


def _unpack_spawn(
    args: Sequence[Any], kwargs: Mapping[str, Any]
) -> tuple[Path, dict[str, str], Path]:
    p = list(args)
    cwd = Path(p.pop(0) if p else kwargs.get("cwd", Path.cwd()))
    env = dict(p.pop(0) if p else kwargs.get("env", {}))
    log_path = Path(p.pop(0) if p else kwargs["log_path"])
    return cwd, env, log_path


def _exec_fd_service(
    spec: tuple[str, Sequence[str], Any],
    ctx: tuple[Path, Mapping[str, str], Any],
    net: tuple[socket.socket, int],
) -> tuple[subprocess.Popen, list[str], int]:
    name, argv, values = spec
    listener, port = net
    fd = listener.fileno()
    os.set_inheritable(fd, True)
    render_vals = {**(values or {}), "fd": fd, "port": port}
    resolved = [str(render(item, render_vals)) for item in argv]
    proc = _popen_service((name, resolved), ctx, pass_fds=(fd,))
    return proc, resolved, fd


def _unpack_spawn_fd_args(
    args: Sequence[Any], kwargs: Mapping[str, Any]
) -> tuple[tuple[Path, dict[str, str], Path], Any, Any]:
    cwd, env, log_path = _unpack_spawn(args, kwargs)
    p = list(args)[3:]
    values = p.pop(0) if p else kwargs.get("values", {})
    cands = p.pop(0) if p else kwargs.get("candidate_ports")
    return (cwd, env, log_path), values, cands


def spawn_fd_service(name: str, argv: Sequence[str], *args: Any, **kwargs: Any) -> dict[str, Any]:
    """Start a service on a listening socket this process binds and hands over."""
    ctx, values, cands = _unpack_spawn_fd_args(args, kwargs)
    log = _open_log(ctx[2])
    listener: socket.socket | None = None
    try:
        listener, port = _safe_allocate_listener(cands)
        proc, res, fd = _exec_fd_service(
            (name, argv, values), (ctx[0], ctx[1], log), (listener, port)
        )
        listener.close()
        listener = None
        return _record(name, "fd", proc, res, port, {"fd": fd})
    finally:
        if listener is not None:
            listener.close()
        log.close()


def spawn_port_service(name: str, argv: Sequence[str], *args: Any, **kwargs: Any) -> dict[str, Any]:
    """Start a service that binds a port itself, such as a Vite dev server."""
    cwd, env, log_path = _unpack_spawn(args, kwargs)
    p = list(args)[3:]
    port = int(p.pop(0) if p else kwargs["port"])
    values = p.pop(0) if p else kwargs.get("values", {})
    log = _open_log(log_path)
    try:
        render_vals = {**(values or {}), "port": port}
        resolved = [str(render(item, render_vals)) for item in argv]
        proc = _popen_service((name, resolved), (cwd, env, log))
        return _record(name, "port", proc, resolved, port)
    finally:
        log.close()
