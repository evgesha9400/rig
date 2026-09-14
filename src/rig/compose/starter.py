"""Compose service startup and container tracking."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from rig.compose.client import run_compose
from rig.compose.context import resolve_current_docker_context
from rig.compose.discovery import (
    INTERRUPTION_EXCEPTIONS,
    discover_container_and_port,
    handle_compose_interruption,
    init_compose_record,
    stranded_error,
)
from rig.core.constants import DOCKER_CLIENT_ENV_PASSTHROUGH, EXIT_OP_FAILED
from rig.core.errors import RigError


def _resolve_compose_endpoints(
    service: Any, env: Mapping[str, str] | None
) -> tuple[dict[str, str] | None, str | None, str | None]:
    docker_host = os.environ.get("DOCKER_HOST")
    cmd_env = dict(env) if env is not None else None
    if cmd_env is not None:
        for name in DOCKER_CLIENT_ENV_PASSTHROUGH:
            if name not in cmd_env and name in os.environ:
                cmd_env[name] = os.environ[name]
    docker_context = service.docker_context or os.environ.get("DOCKER_CONTEXT") or None
    if docker_context is None and not docker_host:
        docker_context = resolve_current_docker_context(cmd_env)
    if docker_context:
        docker_host = None
    return cmd_env, docker_context, docker_host


def _cleanup_compose(
    target: tuple[str, Path, Path, str],
    endpoint: tuple[str | None, dict[str, str] | None, str | None],
) -> bool:
    instance, root, cfile, svc = target
    ctx, env, host = endpoint
    removed = True
    for args in (["stop", svc], ["rm", "-f", svc]):
        try:
            res = run_compose(
                instance, root, cfile, args, ctx, timeout=30.0, env=env, docker_host=host
            )
            if res.returncode != 0:
                removed = False
        except INTERRUPTION_EXCEPTIONS:
            return False
    return removed


def _run_compose_up(
    info: tuple[Any, dict[str, Any]],
    compose_fn: Any,
    handlers: tuple[Any, Any],
) -> None:
    service, record = info
    fail_fn, cleanup_fn = handlers
    try:
        res = compose_fn(["up", "-d", "--no-deps", "--wait", str(service.compose_service)])
    except (RigError, OSError) as exc:
        if not cleanup_fn():
            msg = f"compose could not start {service.name!r}: {exc}"
            raise stranded_error(msg, record) from exc
        raise
    except INTERRUPTION_EXCEPTIONS as exc:
        handle_compose_interruption(exc, info, cleanup_fn, "the start of")
    if res.returncode != 0:
        err = res.stderr.strip() or res.stdout.strip()
        raise fail_fn(f"compose could not start {service.name!r}: {err}")


def _run_discovery_phase(
    service: Any,
    record: dict[str, Any],
    helpers: tuple[Any, Any, Any],
) -> None:
    compose_fn, fail_fn, cleanup_fn = helpers
    try:
        discover_container_and_port(service, record, (compose_fn, fail_fn))
    except RigError:
        raise
    except INTERRUPTION_EXCEPTIONS as exc:
        handle_compose_interruption(exc, (service, record), cleanup_fn, "discovery for")


def _start_compose_service(
    service: Any, root: Path, instance: str, *args: Any, **kwargs: Any
) -> dict[str, Any]:
    env = args[0] if args else kwargs.get("env")
    cfile = Path(root) / str(service.compose_file)
    cmd_env, ctx, host = _resolve_compose_endpoints(service, env)
    record = init_compose_record(service, instance, (cfile, ctx, host, cmd_env))

    target = (instance, Path(root), cfile, str(service.compose_service))
    endpoint = (ctx, cmd_env, host)

    def _compose(compose_args: list[str], timeout: float = 180.0):
        return run_compose(
            instance,
            Path(root),
            cfile,
            compose_args,
            ctx,
            timeout=timeout,
            env=cmd_env,
            docker_host=host,
        )

    def _cleanup() -> bool:
        return _cleanup_compose(target, endpoint)

    def _fail(msg: str) -> RigError:
        if _cleanup():
            return RigError(msg, code="E_COMPOSE_FAILED", exit_code=EXIT_OP_FAILED)
        return stranded_error(msg, record)

    _run_compose_up((service, record), _compose, (_fail, _cleanup))
    _run_discovery_phase(service, record, (_compose, _fail, _cleanup))
    return record
