"""Service execution and health checking logic for rig up."""

from __future__ import annotations

import shutil
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from rig.commands.up.context import ServiceContext
from rig.compose.starter import _start_compose_service
from rig.compose.supervisor import compose_record_alive
from rig.core.constants import EXIT_EXTERNAL_TOOL
from rig.core.env import build_service_env, render
from rig.core.errors import RigError, StackError
from rig.core.state import redact
from rig.manifest.models import Service
from rig.net.health import wait_for_http
from rig.net.ports import _safe_reserve_port, compute_candidate_ports
from rig.net.probe import port_listener_matches
from rig.proc.process import identity_matches, pid_alive
from rig.proc.spawn import spawn_fd_service, spawn_port_service, uvicorn_argv


def _validate_service_env(
    service: Service, root: Path, svc_vals: Mapping[str, Any]
) -> tuple[Path, dict[str, str]]:
    cwd = (Path(root) / service.cwd).resolve()
    if not cwd.is_dir():
        raise StackError(f"service {service.name!r} working directory {cwd} does not exist")
    if service.type in ("port", "fd") and not shutil.which("lsof"):
        raise RigError(
            "'lsof' required but not on PATH", code="E_EXTERNAL_TOOL", exit_code=EXIT_EXTERNAL_TOOL
        )
    env = build_service_env(service.env, service.inherit, Path(root), svc_vals, service.env_files)
    return cwd, env


def _spawn_local_service(
    service: Service,
    paths: tuple[Path, Path],
    ctx: tuple[dict[str, str], Mapping[str, Any], Sequence[int]],
) -> dict[str, Any]:
    cwd, log_path = paths
    env, svc_vals, cands = ctx
    if service.type == "fd":
        py_bin = str(render(service.python or sys.executable, svc_vals))
        cmd = service.command or uvicorn_argv(py_bin, service.app or "", service.factory)
        return spawn_fd_service(service.name, cmd, cwd, env, log_path, svc_vals, cands)
    p = _safe_reserve_port(cands)
    return spawn_port_service(service.name, service.command, cwd, env, log_path, p, svc_vals)


def start_service(service: Service, ctx: ServiceContext) -> dict[str, Any]:
    cwd = (Path(ctx.root) / service.cwd).resolve()
    log_path = ctx.runtime / "logs" / f"{service.name}.log"
    cwd, env = _validate_service_env(service, ctx.root, {**ctx.values, "cwd": str(cwd)})
    if service.type == "compose":
        return _start_compose_service(service, ctx.root, ctx.instance, env=env)
    ports = (
        ctx.candidate_ports
        if ctx.candidate_ports is not None
        else compute_candidate_ports(service, {})
    )
    rec = _spawn_local_service(
        service, (cwd, log_path), (env, {**ctx.values, "cwd": str(cwd)}, ports)
    )
    rec.update(
        {
            "log": str(log_path),
            "env": redact(env),
            "depends_on": list(service.depends_on),
            "health": service.healthcheck_path,
            "healthcheck_path": service.healthcheck_path,
        }
    )
    return rec


def _start_service(
    service: Service, root: Path, runtime: Path, *args: Any, **kwargs: Any
) -> dict[str, Any]:
    """External test adapter delegating to start_service."""
    argv = list(args)
    inst = kwargs.get("instance") or (argv.pop(0) if argv else "")
    vals = kwargs.get("values") or (argv.pop(0) if argv else {})
    cands = kwargs.get("candidate_ports") or (argv.pop(0) if argv else None)
    return start_service(service, ServiceContext(root, runtime, inst, vals, cands))


def _check_port_listener(port: int, ids: tuple[Any, Any], record: Mapping[str, Any]) -> bool:
    pid, pgid = ids
    p_id = pid if isinstance(pid, int) else None
    pg_id = pgid if isinstance(pgid, int) else None
    if not port_listener_matches(port, pgid=pg_id, pid=p_id):
        return False
    return pid_alive(p_id) and identity_matches(record) if isinstance(p_id, int) else True


def _await_process_service(
    service: Service, record: Mapping[str, Any], target: tuple[Any, Any, int | None]
) -> bool:
    pid, pgid, port = target
    p_id = pid if isinstance(pid, int) else None
    pg_id = pgid if isinstance(pgid, int) else None
    if service.healthcheck_path and port is not None:
        wait_target = (service.healthcheck_timeout, p_id, pg_id)
        ok = wait_for_http(port, service.healthcheck_path, wait_target)
        return ok and _check_port_listener(port, (pid, pgid), record)
    if isinstance(pid, int):
        time.sleep(0.3)
        matched = port is None or port_listener_matches(port, pgid=pg_id, pid=p_id)
        return matched and pid_alive(pid) and identity_matches(record)
    return True


def _await_ready(service: Service, record: Mapping[str, Any], root: Path = Path(".")) -> bool:
    pid, pgid = record.get("pid"), record.get("pgid")
    if isinstance(pid, int) and not pid_alive(pid):
        return False
    port = int(record["port"]) if record.get("port") and str(record["port"]).isdigit() else None
    if service.type == "compose":
        p_id = pid if isinstance(pid, int) else None
        pg_id = pgid if isinstance(pgid, int) else None
        if (
            service.healthcheck_path
            and port is not None
            and not wait_for_http(
                port, service.healthcheck_path, (service.healthcheck_timeout, p_id, pg_id)
            )
        ):
            return False
        return compose_record_alive(record, root)
    return _await_process_service(service, record, (pid, pgid, port))
