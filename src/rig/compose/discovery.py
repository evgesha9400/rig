"""Compose container discovery, initialization, and port resolution."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any

from rig.compose.client import parse_compose_port
from rig.core.constants import EXIT_OP_FAILED
from rig.core.errors import RigError
from rig.core.state import redact


def init_compose_record(
    service: Any,
    instance: str,
    target: tuple[Path, str | None, str | None, dict[str, str] | None],
) -> dict[str, Any]:
    """Build the initial state record for a starting compose service."""
    cfile, ctx, host, env = target
    rec: dict[str, Any] = {
        "name": service.name,
        "type": "compose",
        "pid": None,
        "pgid": None,
        "instance": instance,
        "compose_file": str(cfile),
        "compose_service": service.compose_service,
        "docker_context": ctx,
        "docker_host": host,
        "container": "",
        "port": None,
        "url": None,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "depends_on": list(service.depends_on),
        "health": service.healthcheck_path,
        "healthcheck_path": service.healthcheck_path,
    }
    return {**rec, **({"compose_env": redact(env)} if env is not None else {})}


INTERRUPTION_EXCEPTIONS = (
    KeyboardInterrupt,
    SystemExit,
    OSError,
    RigError,
    subprocess.SubprocessError,
    RuntimeError,
    ValueError,
)


def stranded_error(msg: str, record: dict[str, Any]) -> RigError:
    """Format RigError when a container failed to start and could not be cleaned up."""
    return RigError(
        f"{msg}; the partial container could not be removed and stays recorded",
        code="E_COMPOSE_FAILED",
        exit_code=EXIT_OP_FAILED,
        hint="run 'rig down' or 'rig prune --force' to reclaim it",
        details={"partial_record": record},
    )


def handle_compose_interruption(
    exc: BaseException,
    info: tuple[Any, dict[str, Any]],
    cleanup_fn: Any,
    *args: Any,
) -> None:
    """Handle interruption during compose start or discovery."""
    action = args[0] if args else "the start of"
    service, record = info
    if not cleanup_fn():
        name = type(exc).__name__
        msg = f"{action} {service.name!r} was interrupted by {name}"
        raise stranded_error(msg, record) from exc
    raise exc


def _discover_container_id(service: Any, compose_fn: Any, fail_fn: Any) -> str:
    try:
        ids = compose_fn(["ps", "-q", str(service.compose_service)], timeout=60.0)
    except (RigError, OSError, subprocess.SubprocessError, RuntimeError) as exc:
        raise fail_fn(f"compose failed to list containers for {service.name!r}: {exc}") from exc
    if ids.returncode != 0:
        err = ids.stderr.strip() or ids.stdout.strip()
        raise fail_fn(f"compose failed to list containers for {service.name!r}: {err}")
    container = ids.stdout.strip().splitlines()[0].strip() if ids.stdout.strip() else ""
    if not container:
        raise fail_fn(f"compose reported no container for {service.name!r}")
    return container


def _discover_service_port(service: Any, compose_fn: Any, fail_fn: Any) -> int:
    try:
        pub = compose_fn(
            ["port", str(service.compose_service), str(service.compose_port)],
            timeout=60.0,
        )
    except (RigError, OSError, subprocess.SubprocessError, RuntimeError) as exc:
        raise fail_fn(f"compose failed to resolve port for {service.name!r}: {exc}") from exc
    if pub.returncode != 0:
        err = pub.stderr.strip() or pub.stdout.strip() or "no output"
        raise fail_fn(f"compose failed to resolve port for {service.name!r}: {err}")
    return parse_compose_port(pub.stdout)


def discover_container_and_port(
    service: Any,
    record: dict[str, Any],
    helpers: tuple[Any, Any],
) -> None:
    """Populate record with running container ID and resolved loopback port."""
    compose_fn, fail_fn = helpers
    record["container"] = _discover_container_id(service, compose_fn, fail_fn)
    if service.compose_port:
        port = _discover_service_port(service, compose_fn, fail_fn)
        record["port"] = port
        record["url"] = f"http://127.0.0.1:{port}"
