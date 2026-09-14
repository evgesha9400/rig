"""Affected dependent service resolution and cleanup for rig up."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rig.commands.common import (
    _consumers_of,
    _merged_depends_on,
    _stop_record,
    is_service_verifiable_alive,
    reverse_dependency_order,
)
from rig.core.constants import EXIT_OP_FAILED
from rig.core.errors import RigError
from rig.core.state import write_state
from rig.manifest.models import Manifest


def _find_affected(
    order: list[str], manifest: Manifest, state_and_root: tuple[dict[str, Any], Path]
) -> set[str]:
    state, root = state_and_root
    svcs = state["services"]
    missing = [n for n in order if n not in svcs or not is_service_verifiable_alive(svcs[n], root)]
    affected, queue, seen = set(), list(missing), set(missing)
    while queue:
        curr = queue.pop(0)
        for dep in _consumers_of(curr, svcs, manifest):
            if dep in svcs:
                affected.add(dep)
            if dep not in seen:
                seen.add(dep)
                queue.append(dep)
    return affected


def _stop_affected(
    affected: set[str], manifest: Manifest, ctx: tuple[dict[str, Any], Path, Path]
) -> set[str]:
    state, root, state_path = ctx
    svcs = state["services"]
    dep_dict = {
        name: {"depends_on": sorted(_merged_depends_on(name, svcs[name], manifest))}
        for name in affected
    }
    stop_order = reverse_dependency_order(dep_dict)
    failed_stops: set[str] = set()
    for dep_name in stop_order:
        if any(c in failed_stops for c in _consumers_of(dep_name, svcs, manifest)):
            failed_stops.add(dep_name)
            continue
        outcome = _stop_record(svcs[dep_name], root)
        if outcome not in ("terminated", "killed", "stale"):
            failed_stops.add(dep_name)
        else:
            svcs.pop(dep_name, None)
            write_state(state_path, state)
    return failed_stops


def _relink_affected(
    order: list[str], manifest: Manifest, ctx: tuple[dict[str, Any], Path, Path, bool]
) -> list[str] | int:
    state, root, state_path, as_json = ctx
    affected = _find_affected(order, manifest, (state, root))
    if not affected:
        return order
    failed_stops = _stop_affected(affected, manifest, (state, root, state_path))
    if failed_stops:
        err = RigError(
            f"cleanup failed for dependent services: {', '.join(failed_stops)}",
            code="E_CLEANUP_FAILED",
            exit_code=EXIT_OP_FAILED,
        )
        if as_json:
            raise err
        return err.exit_code
    extra = [name for name in affected if name in manifest.services]
    return manifest.resolve_services(order + extra)
