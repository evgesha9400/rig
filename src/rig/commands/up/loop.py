"""Mode switching and service initialization loop for rig up."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rig.commands.common import (
    _stop_record,
    is_service_active_in_mode,
    is_service_verifiable_alive,
    reverse_dependency_order,
)
from rig.commands.up.context import RollbackContext
from rig.commands.up.rollback import rollback_started
from rig.commands.up.runner import _start_with_retry
from rig.core.constants import EXIT_MUTEX_CONFLICT, EXIT_OP_FAILED, EXIT_REFUSED
from rig.core.errors import RigError
from rig.core.state import write_state
from rig.manifest.models import Manifest

_ABORT_EXCEPTIONS = (
    KeyboardInterrupt,
    SystemExit,
    RigError,
    OSError,
    RuntimeError,
    ValueError,
)


def _stop_one_service_for_switch(sname: str, rec: dict[str, Any], root: Path) -> None:
    outcome = _stop_record(rec, root)
    if outcome not in ("terminated", "killed", "stale"):
        raise RigError(
            f"service {sname!r} failed to stop during mode switch ({outcome})",
            code="E_SWITCH_FAILED",
            exit_code=EXIT_REFUSED,
        )


def _stop_services_for_switch(state: dict[str, Any], root: Path) -> None:
    for sname in reverse_dependency_order(state.get("services", {})):
        rec = state["services"].get(sname)
        if rec:
            _stop_one_service_for_switch(sname, rec, root)
            state["services"].pop(sname, None)


def _switch_mode(
    state: dict[str, Any], paths: tuple[Path, Path], mode_cfg: tuple[str, bool]
) -> None:
    root, state_path = paths
    mode, switch = mode_cfg
    current_mode = state.get("mode")
    services = state.get("services", {}).values()
    active = sum(1 for rec in services if is_service_active_in_mode(rec, root))
    if current_mode and current_mode != mode and active > 0:
        if not switch:
            raise RigError(
                f"stack running in mode {current_mode!r}; "
                f"cannot start in {mode!r} without --switch",
                code="E_MODE_CONFLICT",
                exit_code=EXIT_MUTEX_CONFLICT,
            )
        _stop_services_for_switch(state, root)
        write_state(state_path, state)
    state["mode"] = mode


def _reclaim_dead_service(name: str, root: Path, state_ctx: tuple[dict[str, Any], Path]) -> bool:
    state, state_path = state_ctx
    if not (existing := state["services"].get(name)):
        return True
    if _stop_record(existing, root, remove=True) not in ("terminated", "killed", "stale"):
        return False
    state["services"].pop(name, None)
    write_state(state_path, state)
    return True


def _start_step(
    name: str,
    manifest: Manifest,
    ctx: tuple[Path, Path, str, dict[str, Any], Path],
) -> tuple[dict[str, Any] | None, Exception | None]:
    root, runtime, instance, state, state_path = ctx
    service = manifest.services[name]
    if not _reclaim_dead_service(name, root, (state, state_path)):
        err = RigError(
            f"service {name!r} could not be reclaimed before start",
            code="E_SERVICE_UNHEALTHY",
            exit_code=EXIT_OP_FAILED,
        )
        return None, err
    try:
        rec = _start_with_retry(service, root, runtime, instance, state, state_path)
    except RigError as exc:
        return None, exc
    except _ABORT_EXCEPTIONS:
        write_state(state_path, state)
        raise
    return rec, None


def _handle_step_failure(name: str, exc: Exception, as_json: bool) -> int:
    if as_json:
        if isinstance(exc, RigError):
            raise exc
        raise RigError(
            f"failed to start {name}: {exc}", code="E_START_FAILED", exit_code=EXIT_OP_FAILED
        ) from None
    return exc.exit_code if isinstance(exc, RigError) else EXIT_OP_FAILED


def _handle_timeout(name: str, as_json: bool) -> int:
    timeout_err = RigError(
        f"service {name!r} failed to reach healthy state",
        code="E_START_TIMEOUT",
        exit_code=EXIT_OP_FAILED,
    )
    if as_json:
        raise timeout_err
    return timeout_err.exit_code


def _start_loop(
    order: list[str],
    manifest: Manifest,
    ctx: tuple[dict[str, Any], Path, Path, str, Path, bool],
) -> int | None:
    state, root, runtime, instance, state_path, as_json = ctx
    started: list[str] = []
    for name in order:
        existing = state["services"].get(name)
        if existing and is_service_verifiable_alive(existing, root):
            continue
        rec, err = _start_step(name, manifest, (root, runtime, instance, state, state_path))
        if err is not None:
            rollback_started(started, RollbackContext(state, state_path, root, manifest))
            return _handle_step_failure(name, err, as_json)
        if rec is None:
            rollback_started(started, RollbackContext(state, state_path, root, manifest))
            return _handle_timeout(name, as_json)
        started.append(name)
    return None
