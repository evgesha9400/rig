"""Common command helpers, dependency ordering, and state inspection."""

from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from rig.compose.stopper import compose_stop_record
from rig.compose.supervisor import compose_record_alive, compose_record_status
from rig.core.constants import RUNTIME_DIR_NAME, TEARDOWN_TIMEOUT_SECS
from rig.core.identity import instance_id
from rig.core.locks import _lock_path, ensure_runtime_dir
from rig.core.state import _state_path
from rig.manifest.loader import load_manifest
from rig.manifest.models import Manifest
from rig.net.ports import port_is_free
from rig.proc.process import identity_matches, pid_alive
from rig.proc.teardown import pgid_alive, terminate_record

_COMPOSE_STATUS_LABELS: dict[str, str] = {"alive": "running", "error": "error"}


def record_alive(record: Mapping[str, Any], root: Path) -> bool:
    if record.get("type") == "compose":
        return compose_record_alive(record, root)
    return identity_matches(record)


def record_status(record: Mapping[str, Any], root: Path) -> str:
    """Return 'running', 'stopped' or 'error' for one recorded service."""
    if record.get("type") != "compose":
        return "running" if identity_matches(record) else "stopped"
    return _COMPOSE_STATUS_LABELS.get(compose_record_status(record, root), "stopped")


def is_service_verifiable_alive(record: Mapping[str, Any], root: Path) -> bool:
    if record.get("type") == "compose":
        return compose_record_alive(record, root)
    pid = record.get("pid")
    return isinstance(pid, int) and pid_alive(pid) and identity_matches(record)


def is_service_active_in_mode(record: Mapping[str, Any], root: Path) -> bool:
    if record.get("type") == "compose":
        return compose_record_status(record, root) != "absent"
    return (
        is_service_verifiable_alive(record, root)
        or (isinstance(record.get("pgid"), int) and pgid_alive(record["pgid"]))
        or (isinstance(record.get("pid"), int) and pid_alive(record["pid"]))
    )


def _is_dead(rec: Mapping[str, Any], root: Path) -> bool:
    if rec.get("type") == "compose":
        return compose_record_status(rec, root) == "absent"
    pgid_ok = isinstance(rec.get("pgid"), int) and pgid_alive(rec["pgid"])
    return not identity_matches(rec) and not pgid_ok


def prune_state(state: dict[str, Any], root: Path) -> list[str]:
    dropped = [n for n, r in list(state.get("services", {}).items()) if _is_dead(r, root)]
    for name in dropped:
        rec = state["services"].pop(name, None) or {}
        if (port := rec.get("port")) and not port_is_free(int(port)):
            warn = f"  warning: {name} port {port} in use (pid {rec.get('pid')} may be orphaned)"
            print(warn, file=sys.stderr)
    return dropped


def _values_for(state: Mapping[str, Any], root: Path, instance: str) -> dict[str, Any]:
    values: dict[str, Any] = {
        "root": str(root),
        "instance": instance,
        "data_dir": str(Path(root) / RUNTIME_DIR_NAME / "data"),
        "python": sys.executable,
    }
    for name, record in state.get("services", {}).items():
        if record.get("port"):
            values[f"{name}_port"] = record["port"]
            values[f"{name}_url"] = record.get("url") or f"http://127.0.0.1:{record['port']}"
    return values


def record_depends_on(record: Mapping[str, Any]) -> list[str]:
    raw = record.get("depends_on")
    return [i for i in raw if isinstance(i, str)] if isinstance(raw, (list, tuple, set)) else []


def _merged_depends_on(
    name: str, record: Mapping[str, Any], manifest: Manifest | None = None
) -> set[str]:
    deps = set(record_depends_on(record))
    if manifest is not None and name in manifest.services:
        deps.update(manifest.services[name].depends_on)
    return deps - {name}


def _consumers_of(
    name: str, services: Mapping[str, Mapping[str, Any]], manifest: Manifest | None = None
) -> set[str]:
    consumers = {
        other for other, rec in services.items() if name in _merged_depends_on(other, rec, manifest)
    }
    if manifest is not None:
        consumers.update(manifest.dependents(name))
    return consumers - {name}


def _drain_ready_deps(deps: set[str], in_degree: dict[str, int]) -> list[str]:
    in_degree.update({d: in_degree[d] - 1 for d in deps})
    return [d for d in deps if in_degree[d] == 0]


def reverse_dependency_order(services: Mapping[str, Mapping[str, Any]]) -> list[str]:
    in_degree = {k: 0 for k in services}
    dep_map = {k: set() for k in services}
    for name, rec in services.items():
        for d in {d for d in record_depends_on(rec) if d in services and d != name}:
            dep_map[name].add(d)
            in_degree[d] += 1
    queue = [k for k, deg in in_degree.items() if deg == 0]
    order = []
    while queue:
        curr = queue.pop(0)
        order.append(curr)
        queue.extend(_drain_ready_deps(dep_map[curr], in_degree))
    return order + [k for k in services if k not in order]


def _stop_record(record: Mapping[str, Any], root: Path, remove: bool = True) -> str:
    if record.get("type") == "compose":
        return compose_stop_record(record, root, remove=remove)
    return terminate_record(record, TEARDOWN_TIMEOUT_SECS)


def _resolve_manifest_context(root: Path, manifest_path: Path):
    raw_manifest = load_manifest(manifest_path)
    resolved_root = Path(root).resolve()
    ensure_runtime_dir(resolved_root)
    instance = instance_id(raw_manifest.project, resolved_root)
    return (
        raw_manifest,
        resolved_root,
        instance,
        _state_path(resolved_root, instance=instance),
        _lock_path(resolved_root, instance=instance),
    )
