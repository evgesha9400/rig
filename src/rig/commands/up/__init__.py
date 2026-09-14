"""Orchestrator for rig up command."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rig.commands.common import _resolve_manifest_context, prune_state
from rig.commands.status import _print_status
from rig.commands.up.loop import _start_loop, _switch_mode
from rig.commands.up.relink import _relink_affected
from rig.commands.up.runner import _rollback, _start_with_retry
from rig.commands.up.service import (
    _await_ready,
    _check_port_listener,
    _start_service,
)
from rig.core.constants import EXIT_OK
from rig.core.errors import print_json_envelope
from rig.core.identity import get_boot_id
from rig.core.locks import ensure_runtime_dir, exclusive_lock
from rig.core.state import read_state, write_state
from rig.manifest.models import Manifest


def _run_up_transaction(
    order: list[str],
    manifest: Manifest,
    ctx: tuple[tuple[Path, Path, str, Path], tuple[str, bool, bool]],
) -> int:
    paths, cfgs = ctx
    root_path, runtime, instance, state_path = paths
    selected_mode, switch, as_json = cfgs
    state = read_state(state_path)
    state.update(
        {
            "instance": instance,
            "project": manifest.project,
            "root": str(root_path),
            "boot_id": get_boot_id(),
        }
    )
    _switch_mode(state, (root_path, state_path), (selected_mode, switch))
    prune_state(state, root_path)
    write_state(state_path, state)

    relink = _relink_affected(order, manifest, (state, root_path, state_path, as_json))
    if isinstance(relink, int):
        return relink
    res = _start_loop(relink, manifest, (state, root_path, runtime, instance, state_path, as_json))
    if isinstance(res, int):
        return res
    state["generation"] = int(state.get("generation", 0)) + 1
    write_state(state_path, state)
    return EXIT_OK


def _finish_up_output(ctx: tuple[str, str, Path, Path], manifest: Manifest, as_json: bool) -> None:
    instance, selected_mode, state_path, root_path = ctx
    if as_json:
        payload = {
            "instance": instance,
            "project": manifest.project,
            "mode": selected_mode,
            "generation": read_state(state_path).get("generation", 0),
            "services": read_state(state_path).get("services", {}),
        }
        print_json_envelope("up", payload)
    else:
        _print_status(manifest, read_state(state_path), root_path)


def cmd_up(root: Path, manifest_path: Path, *args: Any, **kwargs: Any) -> int:
    argv = list(args)
    scope = kwargs.get("scope") or (argv.pop(0) if argv else "full")
    mode = kwargs.get("mode") if "mode" in kwargs else (argv.pop(0) if argv else None)
    switch = kwargs.get("switch") if "switch" in kwargs else (argv.pop(0) if argv else False)
    as_json = kwargs.get("as_json") if "as_json" in kwargs else (argv.pop(0) if argv else False)

    raw_m, root_path, instance, state_path, lock_path = _resolve_manifest_context(
        root, manifest_path
    )
    manifest = raw_m.for_mode(mode) if mode or raw_m.modes else raw_m
    selected_mode = manifest.active_mode or "default"
    order = manifest.resolve_scope(scope)
    runtime = ensure_runtime_dir(root_path)

    with exclusive_lock(lock_path):
        res = _run_up_transaction(
            order,
            manifest,
            ((root_path, runtime, instance, state_path), (selected_mode, switch, as_json)),
        )
        if res != EXIT_OK:
            return res

    _finish_up_output((instance, selected_mode, state_path, root_path), manifest, as_json)
    return EXIT_OK


__all__ = [
    "_await_ready",
    "_check_port_listener",
    "_relink_affected",
    "_rollback",
    "_start_loop",
    "_start_service",
    "_start_with_retry",
    "_switch_mode",
    "cmd_up",
]
