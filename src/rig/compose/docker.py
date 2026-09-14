"""Plain Docker container queries and lifecycle supervision."""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from typing import Any

from rig.compose.client import run_docker
from rig.compose.context import INHERIT_DOCKER_HOST, record_compose_env, record_docker_endpoint
from rig.core.errors import RigError

DOCKER_ABSENT_MARKERS = ("no such object", "no such container")


def docker_label_container_ids(record: Mapping[str, Any]) -> list[str] | None:
    """Return the container IDs Compose labelled with this record's project and service."""
    context, docker_host = record_docker_endpoint(record)
    filters = [
        "--filter",
        f"label=com.docker.compose.project={record.get('instance')}",
        "--filter",
        f"label=com.docker.compose.service={record.get('compose_service')}",
    ]
    try:
        res = run_docker(
            ["ps", "-q", "-a", *filters],
            context,
            docker_host=docker_host,
            env=record_compose_env(record),
        )
    except RigError:
        return None
    if res.returncode != 0:
        return None
    return [line.strip() for line in res.stdout.splitlines() if line.strip()]


def docker_reports_no_such_object(probe: subprocess.CompletedProcess) -> bool:
    answer = f"{probe.stderr or ''}\n{probe.stdout or ''}".lower()
    return any(m in answer for m in DOCKER_ABSENT_MARKERS)


def docker_container_status(
    container: str,
    context: Any = None,
    *args: Any,
    **kwargs: Any,
) -> str:
    p = list(args)
    docker_host = p.pop(0) if p else kwargs.get("docker_host", INHERIT_DOCKER_HOST)
    env = p.pop(0) if p else kwargs.get("env")
    try:
        cmd = ["inspect", "--format", "{{.State.Status}}", str(container)]
        probe = run_docker(cmd, context, docker_host=docker_host, env=env)
    except RigError:
        return "error"
    if probe.returncode != 0:
        return "absent" if docker_reports_no_such_object(probe) else "error"
    return "alive" if probe.stdout.strip().lower() in ("running", "restarting") else "stopped"


def docker_record_targets(record: Mapping[str, Any]) -> tuple[list[str], bool]:
    ids = docker_label_container_ids(record)
    targets = list(ids or [])
    recorded = str(record.get("container") or "")
    if recorded and not any(recorded.startswith(f) or f.startswith(recorded) for f in targets):
        targets.insert(0, recorded)
    return targets, ids is not None


def docker_record_status(record: Mapping[str, Any]) -> str:
    targets, answered = docker_record_targets(record)
    if not targets:
        return "absent" if answered else "error"
    ctx, host = record_docker_endpoint(record)
    cenv = record_compose_env(record)
    states = [docker_container_status(t, ctx, host, cenv) for t in targets]
    return next(
        (s for s in ("alive", "stopped", "error") if s in states),
        "absent" if answered else "error",
    )


def _stop_target(
    target: str,
    endpoint: tuple[Any, Any, Any],
    remove: bool,
) -> bool:
    ctx, host, cenv = endpoint
    cmds = [["stop", target]]
    if remove:
        cmds.append(["rm", "-f", target])
    for args in cmds:
        try:
            res = run_docker(args, ctx, docker_host=host, env=cenv)
        except RigError:
            return False
        if res.returncode == 0:
            continue
        if docker_reports_no_such_object(res):
            break
        return False
    return True


def docker_record_stop(record: Mapping[str, Any], remove: bool) -> str:
    targets, answered = docker_record_targets(record)
    if not targets:
        return "stale" if answered else "failed"
    ctx, host = record_docker_endpoint(record)
    cenv = record_compose_env(record)
    endpoint = (ctx, host, cenv)
    for t in targets:
        if not _stop_target(t, endpoint, remove):
            return "failed"
    return "failed" if not answered else "terminated"
