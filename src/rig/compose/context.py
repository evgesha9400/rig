"""Docker context and endpoint resolution."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from typing import Any

INHERIT_DOCKER_HOST: Any = object()


def record_docker_endpoint(record: Mapping[str, Any]) -> tuple[Any, Any]:
    """Return the Docker context and host one record was started against."""
    return record.get("docker_context"), record.get("docker_host", INHERIT_DOCKER_HOST)


def resolve_current_docker_context(env: Mapping[str, str] | None = None) -> str | None:
    """Return the name of the Docker context that is active right now."""
    cmd_env = dict(os.environ) if env is None else dict(env)
    named = cmd_env.get("DOCKER_CONTEXT")
    if named:
        return named
    try:
        probe = subprocess.run(
            ["docker", "context", "show"],
            capture_output=True,
            text=True,
            timeout=5.0,
            env=cmd_env,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if probe.returncode != 0:
        return None
    return probe.stdout.strip() or None


def _pin_docker_endpoint(cmd_env: dict[str, str], context: Any, docker_host: Any) -> dict[str, str]:
    """Point one Docker command at the endpoint its record was started against."""
    cmd_env.pop("DOCKER_CONTEXT", None)
    if docker_host is INHERIT_DOCKER_HOST:
        return cmd_env
    if docker_host:
        cmd_env["DOCKER_HOST"] = str(docker_host)
    else:
        cmd_env.pop("DOCKER_HOST", None)
    return cmd_env


def record_compose_env(record: Mapping[str, Any]) -> dict[str, str] | None:
    """Return the environment one compose record was started with, if it was recorded."""
    env = record.get("compose_env")
    if not isinstance(env, Mapping):
        return None
    return {str(key): str(value) for key, value in env.items()}
