"""Centralized machine-wide port registry and reservation management."""

from __future__ import annotations

import json
import os
import socket
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from rig.core.constants import DIR_MODE_PRIVATE, FILE_MODE_PRIVATE, PORT_MAX, PORT_MIN
from rig.core.identity import get_state_home
from rig.core.locks import exclusive_lock

PORT_REGISTRY_FILE = "ports.json"
PORT_LOCK_FILE = "ports.lock"


def port_is_free(port: int) -> bool:
    """Return True when nothing is listening on port on loopback."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    with probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _ensure_dir(filename: str) -> Path:
    home = get_state_home()
    home.mkdir(parents=True, mode=DIR_MODE_PRIVATE, exist_ok=True)
    return home / filename


def read_port_registry(path: Path | None = None) -> dict[str, Any]:
    """Read central port registry, returning empty schema on error."""
    target = path or _ensure_dir(PORT_REGISTRY_FILE)
    try:
        data = json.loads(target.read_text())
        if isinstance(data, dict):
            allocs = data.get("allocations")
            data["allocations"] = allocs if isinstance(allocs, dict) else {}
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"version": 1, "allocations": {}}


def _write_port_registry(target: Path, data: Mapping[str, Any]) -> None:
    target.parent.mkdir(parents=True, mode=DIR_MODE_PRIVATE, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", dir=target.parent, delete=False, prefix=".ports.tmp."
    ) as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        tmp = handle.name
    os.chmod(tmp, FILE_MODE_PRIVATE)
    os.replace(tmp, target)


def allocation_key(project: str, service_name: str) -> str:
    return f"{project}:{service_name}"


def _find_free_port(base_port: int, window: int, used_ports: set[int]) -> int:
    limit = min(base_port + window, PORT_MAX + 1)
    for cand in range(base_port, limit):
        if cand not in used_ports and port_is_free(cand):
            return cand
    for cand in range(limit, PORT_MAX + 1):
        if cand not in used_ports and port_is_free(cand):
            return cand
    return base_port


def get_or_allocate_port(target: tuple[str, str], base_port: int, window: int = 50) -> int:
    """Return assigned port from central registry, allocating next free if absent."""
    project, service_name = target
    key = allocation_key(project, service_name)
    reg_path, lock_path = _ensure_dir(PORT_REGISTRY_FILE), _ensure_dir(PORT_LOCK_FILE)
    with exclusive_lock(lock_path):
        data = read_port_registry(reg_path)
        allocs = data["allocations"]
        if key in allocs:
            assigned = allocs[key]
            if isinstance(assigned, int) and PORT_MIN <= assigned <= PORT_MAX:
                return assigned
        used = {int(p) for p in allocs.values() if isinstance(p, int) and PORT_MIN <= p <= PORT_MAX}
        port = _find_free_port(base_port, window, used)
        allocs[key] = port
        _write_port_registry(reg_path, data)
        return port


def get_allocated_ports_for_others(target: tuple[str, str]) -> set[int]:
    """Return set of ports allocated to other projects or services."""
    project, service_name = target
    key = allocation_key(project, service_name)
    data = read_port_registry(_ensure_dir(PORT_REGISTRY_FILE))
    allocs = data.get("allocations", {})
    return {
        int(p)
        for k, p in allocs.items()
        if k != key and isinstance(p, int) and PORT_MIN <= p <= PORT_MAX
    }


def release_port_allocation(project: str, service_name: str | None = None) -> list[int]:
    """Release port allocation for a project or specific service."""
    reg_path, lock_path = _ensure_dir(PORT_REGISTRY_FILE), _ensure_dir(PORT_LOCK_FILE)
    with exclusive_lock(lock_path):
        data = read_port_registry(reg_path)
        allocs = data.get("allocations", {})
        prefix = f"{project}:" if service_name is None else f"{project}:{service_name}"
        released = [
            allocs.pop(k)
            for k in list(allocs.keys())
            if k == prefix or (service_name is None and k.startswith(prefix))
        ]
        if released:
            _write_port_registry(reg_path, data)
        return released


def list_port_allocations() -> dict[str, int]:
    return dict(read_port_registry(_ensure_dir(PORT_REGISTRY_FILE)).get("allocations", {}))
