"""Dead and orphaned instance registry cleanup command."""

from __future__ import annotations

import contextlib
import fcntl
import os
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from rig.commands.common import (
    _consumers_of,
    _stop_record,
    compose_record_status,
    reverse_dependency_order,
)
from rig.core.constants import (
    EXIT_OK,
    EXIT_OP_FAILED,
    FILE_MODE_PRIVATE,
    LOCK_FILE_NAME,
    STATE_FILE_NAME,
)
from rig.core.errors import print_json_envelope
from rig.core.identity import get_instances_dir
from rig.core.state import read_state, write_state
from rig.proc.process import identity_matches, pid_alive
from rig.proc.teardown import pgid_alive


def _is_service_live(record: Mapping[str, Any], root: Path) -> bool:
    if record.get("type") == "compose":
        return compose_record_status(record, root) != "absent"
    pid, pgid = record.get("pid"), record.get("pgid")
    ok_pid = isinstance(pid, int) and pid_alive(pid) and identity_matches(record)
    return ok_pid or (isinstance(pgid, int) and pgid_alive(pgid))


def _instance_live_services(
    services: Mapping[str, Mapping[str, Any]], root: Path
) -> dict[str, Mapping[str, Any]]:
    return {n: r for n, r in services.items() if _is_service_live(r, root)}


def _stop_instance_service(
    name: str, services: dict[str, Any], ctx: tuple[Path, set[str]]
) -> str | None:
    root, failed = ctx
    if not (rec := services.get(name)):
        return None
    if any(dep in failed for dep in _consumers_of(name, services)):
        failed.add(name)
        return f"{name}: preserved because dependent is still running"
    outcome = _stop_record(rec, root, remove=True)
    if outcome not in ("terminated", "killed", "stale"):
        failed.add(name)
        return f"{name}: {outcome}"
    services.pop(name, None)
    return None


def _force_stop_instance(state: dict[str, Any], state_file: Path, root: Path) -> list[str]:
    services = state.get("services", {})
    failures, failed_services = [], set()
    ctx = (root, failed_services)
    for name in reverse_dependency_order(services):
        if (err := _stop_instance_service(name, services, ctx)) is not None:
            failures.append(err)
    state["generation"] = int(state.get("generation", 0)) + 1
    write_state(state_file, state)
    return failures


def _clear_instance_dir(inst_dir: Path) -> bool:
    items = [i for i in inst_dir.iterdir() if i.name != LOCK_FILE_NAME]
    for item in items:
        if item.is_dir() and not item.is_symlink():
            shutil.rmtree(item, ignore_errors=True)
        else:
            with contextlib.suppress(OSError):
                item.unlink()
    return bool(items)


def _try_lock_fd(lock_file: Path) -> int | None:
    try:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(str(lock_file), flags, FILE_MODE_PRIVATE)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        return None
    else:
        return fd


def _inspect_and_prune(inst_dir: Path, force: bool) -> tuple[str | None, dict[str, Any] | None]:
    state_file = inst_dir / STATE_FILE_NAME
    state = read_state(state_file) if state_file.is_file() else {}
    services, root_str = state.get("services", {}), state.get("root")
    root_exists = bool(root_str and Path(root_str).is_dir())
    ref_root = Path(root_str) if root_exists else inst_dir

    live = _instance_live_services(services, ref_root)
    if live and not force:
        return None, None
    if live:
        if errs := _force_stop_instance(state, state_file, ref_root):
            return None, {"instance": inst_dir.name, "failed": errs}
        services = state.get("services", {})

    can_prune = force or not root_exists or len(services) == 0
    return (inst_dir.name, None) if can_prune and _clear_instance_dir(inst_dir) else (None, None)


def _prune_instance(inst_dir: Path, force: bool) -> tuple[str | None, dict[str, Any] | None]:
    if (lock_fd := _try_lock_fd(inst_dir / LOCK_FILE_NAME)) is None:
        return None, None
    try:
        return _inspect_and_prune(inst_dir, force)
    finally:
        with contextlib.suppress(OSError):
            os.close(lock_fd)


def _collect_prune_results(
    instances_dir: Path, force: bool
) -> tuple[list[str], list[dict[str, Any]]]:
    dirs = [directory for directory in sorted(instances_dir.iterdir()) if directory.is_dir()]
    results = [_prune_instance(directory, force) for directory in dirs]
    return [pruned for pruned, _ in results if pruned], [failed for _, failed in results if failed]


def cmd_prune(force: bool = False, as_json: bool = False) -> int:
    instances_dir = get_instances_dir()
    pruned, failed = (
        _collect_prune_results(instances_dir, force) if instances_dir.is_dir() else ([], [])
    )
    if as_json:
        print_json_envelope("prune", {"pruned": pruned, "failed": failed}, ok=not failed)
    return EXIT_OP_FAILED if failed else EXIT_OK
