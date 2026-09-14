"""Checkout-specific teardown and service stopping."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from rig.commands.common import (
    _consumers_of,
    _merged_depends_on,
    _resolve_manifest_context,
    _stop_record,
    reverse_dependency_order,
)
from rig.core.constants import EXIT_OK, EXIT_OP_FAILED, EXIT_REFUSED
from rig.core.errors import RigError, print_json_envelope
from rig.core.locks import exclusive_lock
from rig.core.state import read_state, write_state
from rig.net.ports import wait_for_port_release


def _stop_service_checkout(
    name: str, state: dict[str, Any], ctx: tuple[Path, Path]
) -> tuple[bool, str | None]:
    root_path, state_path = ctx
    if not (rec := state.get("services", {}).get(name)):
        return False, None
    outcome = _stop_record(rec, root_path)
    if outcome in ("terminated", "killed", "stale"):
        port = rec.get("port")
        state["services"].pop(name, None)
        write_state(state_path, state)
        held = bool(port and not wait_for_port_release(int(port)))
        return True, f"{name}: port {port} still held" if held else None
    return False, f"{name}: {outcome}"


def _stop_target_step(
    name: str,
    state: dict[str, Any],
    ctx: tuple[Any, tuple[Path, Path], tuple[list[str], list[str], set[str]]],
) -> None:
    manifest, paths, tracking = ctx
    root_path, state_path = paths
    stopped, failures, failed_svcs = tracking
    services = state.get("services", {})
    if name not in services:
        return
    deps = [d for d in _consumers_of(name, services, manifest) if d in failed_svcs or d in services]
    if deps:
        failures.append(f"{name}: preserved because dependent(s) {', '.join(deps)} are active")
        return
    ok, err = _stop_service_checkout(name, state, (root_path, state_path))
    if ok:
        stopped.append(name)
    else:
        failed_svcs.add(name)
    if err:
        failures.append(err)


def _teardown_targets(
    targets: list[str], state: dict[str, Any], ctx: tuple[Any, Path, Path]
) -> tuple[list[str], list[str]]:
    manifest, root_path, state_path = ctx
    stopped, failures, failed_svcs = [], [], set()
    step_ctx = (manifest, (root_path, state_path), (stopped, failures, failed_svcs))
    for name in targets:
        _stop_target_step(name, state, step_ctx)
    return stopped, failures


def _teardown_scope(
    manifest: Any, state: dict[str, Any], scope: str
) -> tuple[list[str], list[str]]:
    services = state.get("services", {})
    td_spec = {
        n: {"depends_on": sorted(_merged_depends_on(n, services.get(n, {}), manifest))}
        for n in manifest.teardown_scope(scope)
    }
    targets = reverse_dependency_order(td_spec)
    blocked = [
        f"{n} is still needed by running service {d}"
        for n in targets
        for d in _consumers_of(n, services, manifest)
        if d in services and d not in targets
    ]
    return targets, blocked


def _handle_blocked(blocked: list[str], as_json: bool) -> int:
    if as_json:
        msg = "; ".join(blocked)
        hint = "stop dependent first or use --scope full"
        raise RigError(msg, code="E_REFUSED", exit_code=EXIT_REFUSED, hint=hint)
    for m in blocked:
        print(f"  refused: {m}", file=sys.stderr)
    return EXIT_REFUSED


def down_checkout(root: Path, manifest_path: Path, *args: Any, **kwargs: Any) -> int:
    argv = list(args)
    scope = kwargs.get("scope") or (argv.pop(0) if argv else "full")
    as_json = bool(kwargs.get("as_json") or (argv.pop(0) if argv else False))
    raw_m, root_path, inst, state_path, lock_path = _resolve_manifest_context(root, manifest_path)
    with exclusive_lock(lock_path):
        state = read_state(state_path)
        manifest = raw_m.for_mode(state.get("mode")) if raw_m.modes else raw_m
        targets, blocked = _teardown_scope(manifest, state, scope)
        if blocked:
            return _handle_blocked(blocked, as_json)
        stopped, failures = _teardown_targets(targets, state, (manifest, root_path, state_path))
        state["generation"] = int(state.get("generation", 0)) + 1
        write_state(state_path, state)
    if as_json:
        data = {
            "instance": inst,
            "project": manifest.project,
            "stopped": stopped,
            "failures": failures,
        }
        print_json_envelope("down", data, ok=not failures)
    return EXIT_OP_FAILED if failures else EXIT_OK
