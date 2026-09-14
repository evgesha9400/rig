"""Teardown command for stopping individual checkouts or all machine instances."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rig.commands.common import (
    _merged_depends_on,
    _stop_record,
    reverse_dependency_order,
)
from rig.commands.down.runner import down_checkout
from rig.core.constants import (
    EXIT_NOT_FOUND,
    EXIT_OK,
    EXIT_OP_FAILED,
    LOCK_FILE_NAME,
    STATE_FILE_NAME,
)
from rig.core.errors import RigError, print_json_envelope
from rig.core.identity import get_instances_dir
from rig.core.locks import exclusive_lock
from rig.core.state import read_state, write_state


def _stop_instance_records(
    services: dict[str, Any], root: Path, state: dict[str, Any]
) -> tuple[list[str], list[str]]:
    stopped, failed, failed_svcs = [], [], set()
    for name in reverse_dependency_order(services):
        if deps := [d for d in failed_svcs if name in _merged_depends_on(d, services[d])]:
            failed.append(f"{name}: refused (needed by running dependent {', '.join(deps)})")
            failed_svcs.add(name)
            continue
        outcome = _stop_record(services[name], root)
        if outcome in ("terminated", "killed", "stale"):
            state["services"].pop(name, None)
            stopped.append(name)
        else:
            failed_svcs.add(name)
            failed.append(f"{name}: {outcome}")
    return stopped, failed


def _stop_instance(inst_dir: Path) -> dict[str, Any]:
    state_file = inst_dir / STATE_FILE_NAME
    if not state_file.exists():
        return {"instance": inst_dir.name, "status": "no_state", "stopped": [], "failed": []}
    with exclusive_lock(inst_dir / LOCK_FILE_NAME):
        state = read_state(state_file)
        root = Path(state["root"]).resolve() if state.get("root") else inst_dir
        stopped, failed = _stop_instance_records(dict(state.get("services", {})), root, state)
        state["generation"] = int(state.get("generation", 0)) + 1
        write_state(state_file, state)
        proj = state.get("project", inst_dir.name)
        return {"instance": inst_dir.name, "project": proj, "stopped": stopped, "failed": failed}


def _down_all_instances(as_json: bool) -> int:
    instances_dir = get_instances_dir()
    if not instances_dir.is_dir():
        if as_json:
            print_json_envelope("down", {"instances": [], "all": True})
        else:
            print("No active rig instances to stop.")
        return EXIT_OK
    results = [
        _stop_instance(d)
        for d in sorted(instances_dir.iterdir())
        if d.is_dir() and (d / STATE_FILE_NAME).is_file()
    ]
    failed_any = any(r.get("failed") for r in results)
    if as_json:
        print_json_envelope("down", {"instances": results, "all": True}, ok=not failed_any)
    return EXIT_OP_FAILED if failed_any else EXIT_OK


def _matches_target(d: Path, target: str) -> bool:
    if not (d.is_dir() and (d / STATE_FILE_NAME).is_file()):
        return False
    proj = read_state(d / STATE_FILE_NAME).get("project") or d.name.rsplit("-", 1)[0]
    return target.lower() in (d.name.lower(), proj.lower()) or d.name == target


def _find_matching_instances(instances_dir: Path, target: str) -> list[Path]:
    if not instances_dir.is_dir():
        return []
    return [d for d in sorted(instances_dir.iterdir()) if _matches_target(d, target)]


def _find_target(target: str) -> Path:
    matched = _find_matching_instances(get_instances_dir(), target)
    if not matched:
        msg = f"no instance found matching {target!r}"
        raise RigError(msg, code="E_NOT_FOUND", exit_code=EXIT_NOT_FOUND)
    if len(matched) > 1:
        names = ", ".join(d.name for d in matched)
        msg = f"ambiguous target {target!r}; matches: {names}"
        raise RigError(msg, code="E_AMBIGUOUS", exit_code=EXIT_NOT_FOUND)
    return matched[0]


def cmd_down(
    root: Path | None = None,
    manifest_path: Path | None = None,
    scope: str = "full",
    *args: Any,
    **kwargs: Any,
) -> int:
    argv = list(args)
    target = kwargs.get("target") or (argv.pop(0) if argv else None)
    all_inst = bool(kwargs.get("all_instances") or (argv.pop(0) if argv else False))
    as_json = bool(kwargs.get("as_json") or (argv.pop(0) if argv else False))
    if all_inst:
        return _down_all_instances(as_json)
    if target:
        res = _stop_instance(_find_target(target))
        if as_json:
            print_json_envelope("down", res, ok=not res.get("failed"))
        return EXIT_OP_FAILED if res.get("failed") else EXIT_OK
    if root and manifest_path:
        return down_checkout(root, manifest_path, scope, as_json=as_json)
    return EXIT_OK


__all__ = ["_down_all_instances", "_find_target", "_stop_instance", "cmd_down", "down_checkout"]
