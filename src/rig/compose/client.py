"""Docker and Docker Compose CLI client invocation."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from rig.compose.context import (
    INHERIT_DOCKER_HOST,
    _pin_docker_endpoint,
)
from rig.core.constants import (
    COMPOSE_DISCOVERY_TIMEOUT_SECS,
    COMPOSE_UP_TIMEOUT_SECS,
    DOCKER_CLIENT_ENV_PASSTHROUGH,
    EXIT_EXTERNAL_TOOL,
)
from rig.core.errors import RigError, StackError


def compose_argv(
    instance: str,
    root: Path,
    *extra_args: Any,
    **kwargs: Any,
) -> list[str]:
    """Build a Compose command scoped to this checkout."""
    p = list(extra_args)
    compose_file = p.pop(0) if p else kwargs["compose_file"]
    args = p.pop(0) if p else kwargs.get("args", ())
    context = p.pop(0) if p else kwargs.get("context")

    argv = ["docker"]
    if context:
        argv += ["--context", str(context)]
    argv += [
        "compose",
        "--project-directory",
        str(root),
        "-p",
        instance,
        "-f",
        str(compose_file),
    ]
    return argv + list(args)


def parse_compose_port(output: str) -> int:
    """Extract the host port from ``docker compose port`` output."""
    line = output.strip().splitlines()[-1].strip() if output.strip() else ""
    _, sep, port = line.rpartition(":")
    if not sep or not port.isdigit():
        raise StackError(f"no published host port in compose output {output!r}")
    return int(port)


def compose_file_present(record: Mapping[str, Any], root: Path) -> bool:
    """Return ``True`` when this record's compose file is still on disk."""
    compose_file = record.get("compose_file")
    if not compose_file:
        return False
    return (Path(root) / str(compose_file)).is_file()


def _exec_docker_cmd(
    argv: list[str],
    env: Mapping[str, str],
    timeout_spec: tuple[float, str],
) -> subprocess.CompletedProcess:
    timeout, timeout_msg = timeout_spec
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env)
    except FileNotFoundError:
        msg = "docker is not installed or not on PATH"
        raise RigError(msg, code="E_EXTERNAL_TOOL", exit_code=EXIT_EXTERNAL_TOOL) from None
    except subprocess.TimeoutExpired:
        raise StackError(timeout_msg) from None


def _unpack_compose_call(
    args: Sequence[Any], kwargs: Mapping[str, Any]
) -> tuple[Path, Sequence[str], str | None, float, Mapping[str, str] | None, Any]:
    p = list(args)
    cfile = Path(p.pop(0) if p else kwargs["compose_file"])
    cmd_args = p.pop(0) if p else kwargs.get("args", ())
    context = p.pop(0) if p else kwargs.get("context")
    timeout = float(p.pop(0) if p else kwargs.get("timeout", COMPOSE_UP_TIMEOUT_SECS))
    env = p.pop(0) if p else kwargs.get("env")
    host = p.pop(0) if p else kwargs.get("docker_host", INHERIT_DOCKER_HOST)
    return cfile, cmd_args, context, timeout, env, host


def run_compose(
    instance: str,
    root: Path,
    *args: Any,
    **kwargs: Any,
) -> subprocess.CompletedProcess:
    cfile, cmd_args, context, timeout, env, host = _unpack_compose_call(args, kwargs)
    argv = compose_argv(instance, root, cfile, cmd_args, context)
    cmd_env = dict(os.environ) if env is None else dict(env)
    _pin_docker_endpoint(cmd_env, context, host)
    msg = f"compose command timed out: {' '.join(cmd_args)}"
    return _exec_docker_cmd(argv, cmd_env, (timeout, msg))


def _apply_env_passthrough(cmd_env: dict[str, str], env: Mapping[str, str]) -> None:
    for name in DOCKER_CLIENT_ENV_PASSTHROUGH:
        value = env.get(name)
        if value is None:
            cmd_env.pop(name, None)
        else:
            cmd_env[name] = str(value)


def _prepare_docker_env(
    env: Mapping[str, str] | None, context: str | None, docker_host: Any
) -> dict[str, str]:
    cmd_env = dict(os.environ)
    if env is not None:
        _apply_env_passthrough(cmd_env, env)
    _pin_docker_endpoint(cmd_env, context, docker_host)
    return cmd_env


def run_docker(
    args: Sequence[str],
    *extra: Any,
    **kwargs: Any,
) -> subprocess.CompletedProcess:
    """Run one plain ``docker`` command against one explicit Docker endpoint."""
    p = list(extra)
    context = p.pop(0) if p else kwargs.get("context")
    timeout = float(p.pop(0) if p else kwargs.get("timeout", COMPOSE_DISCOVERY_TIMEOUT_SECS))
    docker_host = p.pop(0) if p else kwargs.get("docker_host", INHERIT_DOCKER_HOST)
    env = p.pop(0) if p else kwargs.get("env")

    argv = ["docker", "--context", str(context)] if context else ["docker"]
    argv.extend(args)
    cmd_env = _prepare_docker_env(env, context, docker_host)
    msg = f"docker command timed out: {' '.join(args)}"
    return _exec_docker_cmd(argv, cmd_env, (timeout, msg))
