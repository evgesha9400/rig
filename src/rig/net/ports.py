"""Loopback socket allocation, port reservation, and sticky lease computation."""

from __future__ import annotations

import contextlib
import socket
import time
from collections.abc import Mapping, Sequence
from typing import Any

from rig.core.constants import PORT_MAX, PORT_MIN, PORT_RELEASE_TIMEOUT_SECS
from rig.net.probe import _get_listener_pids
from rig.net.registry import (
    get_allocated_ports_for_others,
    get_or_allocate_port,
    port_is_free,
)

DEFAULT_PORT_WINDOW = 50

_BASE_PORT_RULES = (
    (("front", "web", "ui", "client", "next", "vite"), 3000),
    (("back", "api", "server", "app", "worker"), 8000),
    (("doc", "storybook", "admin"), 4000),
)


def _bind_candidate_port(port: int) -> tuple[socket.socket, int] | None:
    if port < PORT_MIN or port > PORT_MAX or _get_listener_pids(port):
        return None
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", port))
        listener.listen(socket.SOMAXCONN)
    except OSError:
        listener.close()
        return None
    else:
        return listener, port


def _find_candidate_listener(
    candidate_ports: Sequence[int],
) -> tuple[socket.socket, int] | None:
    return next((r for p in candidate_ports if (r := _bind_candidate_port(p)) is not None), None)


def allocate_listener(candidate_ports: Sequence[int] | None = None) -> tuple[socket.socket, int]:
    """Bind and listen on a loopback port, keeping ownership of it."""
    if candidate_ports and (candidate := _find_candidate_listener(candidate_ports)) is not None:
        return candidate
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind(("127.0.0.1", 0))
        listener.listen(socket.SOMAXCONN)
    except BaseException:
        listener.close()
        raise
    return listener, listener.getsockname()[1]


def _safe_allocate_listener(
    candidate_ports: Sequence[int] | None = None,
) -> tuple[socket.socket, int]:
    try:
        return allocate_listener(candidate_ports) if candidate_ports else allocate_listener()
    except TypeError:
        return allocate_listener()


def reserve_port(candidate_ports: Sequence[int] | None = None) -> int:
    """Return a currently free loopback port for a service that cannot inherit a socket."""
    listener, port = _safe_allocate_listener(candidate_ports)
    listener.close()
    return port


def _safe_reserve_port(candidate_ports: Sequence[int] | None = None) -> int:
    try:
        return reserve_port(candidate_ports) if candidate_ports else reserve_port()
    except TypeError:
        return reserve_port()


def wait_for_port_release(port: int, timeout: float = PORT_RELEASE_TIMEOUT_SECS) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if port_is_free(port):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def default_base_port_for_service(name: str) -> int:
    """Return a human-friendly default base port based on service name conventions."""
    slug = name.lower()
    return next((port for keys, port in _BASE_PORT_RULES if any(k in slug for k in keys)), 5000)


def _collect_excluded_ports(
    state: Mapping[str, Any],
    avoid: set[int] | None = None,
    target: tuple[str, str] | None = None,
) -> set[int]:
    excluded = set(avoid or ())
    for srec in state.get("services", {}).values():
        if isinstance(srec, dict) and srec.get("port"):
            with contextlib.suppress(ValueError, TypeError):
                excluded.add(int(srec["port"]))
    if target is not None:
        excluded.update(get_allocated_ports_for_others(target))
    return excluded


def _resolve_base_port(service: Any, state: Mapping[str, Any]) -> int:
    pref = getattr(service, "preferred_port", None)
    if pref is not None and PORT_MIN <= pref <= PORT_MAX:
        return pref
    project = state.get("project") or getattr(service, "project", None)
    if project:
        base = default_base_port_for_service(service.name)
        return get_or_allocate_port((str(project), service.name), base)
    leased = state.get("ports", {}).get(service.name)
    if leased is not None and isinstance(leased, int) and PORT_MIN <= leased <= PORT_MAX:
        return leased
    return default_base_port_for_service(service.name)


def compute_candidate_ports(
    service: Any,
    state: Mapping[str, Any],
    avoid: set[int] | None = None,
) -> list[int]:
    """Compute an ordered list of candidate ports for a service."""
    project = state.get("project") or getattr(service, "project", None)
    sname = getattr(service, "name", None)
    target = (str(project), sname) if project and sname else None
    excluded = _collect_excluded_ports(state, avoid, target)
    base_port = _resolve_base_port(service, state)
    limit = min(base_port + DEFAULT_PORT_WINDOW, PORT_MAX + 1)
    return [port for port in range(base_port, limit) if port not in excluded]
