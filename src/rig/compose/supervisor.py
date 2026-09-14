"""Docker container lifecycle supervision, status queries, and reclamation."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from rig.compose.client import compose_file_present, run_compose
from rig.compose.context import record_compose_env, record_docker_endpoint
from rig.compose.docker import (
    docker_container_status,
    docker_record_status,
)
from rig.core.errors import RigError


def _check_compose_ps(record: Mapping[str, Any], root: Path) -> list[str] | None:
    context, docker_host = record_docker_endpoint(record)
    compose_env = record_compose_env(record)
    try:
        res = run_compose(
            str(record["instance"]),
            Path(root),
            Path(record["compose_file"]),
            ["ps", "-q", "-a", str(record["compose_service"])],
            context,
            timeout=60.0,
            env=compose_env,
            docker_host=docker_host,
        )
        if res.returncode == 0:
            return [line.strip() for line in res.stdout.splitlines() if line.strip()]
    except RigError:
        pass
    return None


def _has_compose_metadata(record: Mapping[str, Any]) -> bool:
    return bool(
        record.get("instance") and record.get("compose_file") and record.get("compose_service")
    )


def _evaluate_container_states(
    ids: list[str], recorded: str, endpoint: tuple[Any, Any, Any]
) -> str:
    ctx, host, cenv = endpoint
    states = [docker_container_status(found, ctx, host, cenv) for found in ids]
    if recorded and not any(recorded.startswith(f) or f.startswith(recorded) for f in ids):
        states.append(docker_container_status(recorded, ctx, host, cenv))
    for state in ("alive", "stopped"):
        if state in states:
            return state
    return "error"


def compose_record_status(record: Mapping[str, Any], root: Path) -> str:
    """Return 'alive', 'stopped', 'absent', or 'error' for one compose record."""
    if not _has_compose_metadata(record):
        return "absent"
    ids = _check_compose_ps(record, root) if compose_file_present(record, root) else None
    if ids is None:
        return docker_record_status(record)

    context, docker_host = record_docker_endpoint(record)
    compose_env = record_compose_env(record)
    recorded = str(record.get("container") or "")
    if not ids:
        return (
            docker_container_status(recorded, context, docker_host, compose_env)
            if recorded
            else "absent"
        )

    return _evaluate_container_states(ids, recorded, (context, docker_host, compose_env))


def compose_record_alive(record: Mapping[str, Any], root: Path) -> bool:
    """Return ``True`` when the recorded container is still running under this instance."""
    return compose_record_status(record, root) == "alive"
