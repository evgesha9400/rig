"""Retry loop, rollback, and process supervisor for rig up."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from rig.commands.common import _stop_record, _values_for
from rig.commands.up.context import RetryContext
from rig.commands.up.rollback import _rollback, rollback_started
from rig.commands.up.service import _await_ready, _start_service
from rig.core.constants import PORT_RETRY_ATTEMPTS
from rig.core.errors import RigError, StackError
from rig.core.state import write_state
from rig.manifest.models import Service
from rig.net.ports import compute_candidate_ports

_LAUNCH_EXCEPTIONS = (
    KeyboardInterrupt,
    SystemExit,
    RigError,
    StackError,
    OSError,
    RuntimeError,
    ValueError,
)


def _spawn_candidate(
    service: Service,
    ctx: tuple[Path, Path, str, Mapping[str, Any]],
    cands: Sequence[int],
) -> dict[str, Any]:
    root, runtime, instance, values = ctx
    return _start_service(service, root, runtime, instance, values, cands)


def _handle_launch_failure(
    exc: Exception | KeyboardInterrupt | SystemExit,
    service: Service,
    ctx: tuple[dict[str, Any], Path],
) -> None:
    state, state_path = ctx
    partial = (
        exc.details.get("partial_record") if isinstance(exc, RigError) and exc.details else None
    )
    if isinstance(partial, dict):
        state["services"][service.name] = partial
        write_state(state_path, state)
    raise exc


def _handle_unready(
    record: dict[str, Any],
    ctx: tuple[Path, Service, dict[str, Any], Path],
    collided: set[int],
) -> bool:
    root, service, state, state_path = ctx
    outcome = _stop_record(record, root)
    if record.get("port"):
        collided.add(int(record["port"]))
    if outcome in ("terminated", "killed", "stale"):
        state["services"].pop(service.name, None)
        write_state(state_path, state)
        return True
    return False


def _start_attempt(
    service: Service,
    paths: tuple[Path, Path, str],
    ctx: tuple[dict[str, Any], Path, set[int]],
) -> dict[str, Any] | None:
    root, runtime, instance = paths
    state, state_path, collided = ctx
    values = _values_for(state, root, instance)
    cands = compute_candidate_ports(service, state, avoid=collided)
    try:
        record = _spawn_candidate(service, (root, runtime, instance, values), cands)
    except _LAUNCH_EXCEPTIONS as exc:
        _handle_launch_failure(exc, service, (state, state_path))
    state["services"][service.name] = record
    write_state(state_path, state)
    if _await_ready(service, record, root):
        if record.get("port"):
            state.setdefault("ports", {})[service.name] = record["port"]
            write_state(state_path, state)
        return record
    _handle_unready(record, (root, service, state, state_path), collided)
    return None


def start_with_retry(service: Service, retry: RetryContext) -> dict[str, Any] | None:
    attempts = PORT_RETRY_ATTEMPTS if service.type == "port" else 1
    collided: set[int] = set()
    for _ in range(attempts):
        record = _start_attempt(
            service,
            (retry.root, retry.runtime, retry.instance),
            (retry.state, retry.state_path, collided),
        )
        if record is not None:
            return record
    return None


def _start_with_retry(
    service: Service, root: Path, runtime: Path, *args: Any, **kwargs: Any
) -> dict[str, Any] | None:
    """External test adapter delegating to start_with_retry."""
    argv = list(args)
    inst = kwargs.get("instance") or (argv.pop(0) if argv else "")
    st = kwargs.get("state") or (argv.pop(0) if argv else {})
    sp = kwargs.get("state_path") or (argv.pop(0) if argv else Path("."))
    return start_with_retry(service, RetryContext(root, runtime, inst, st, sp))


__all__ = ["_rollback", "_start_with_retry", "rollback_started", "start_with_retry"]
