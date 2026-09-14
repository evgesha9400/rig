"""Loopback socket allocation, port reservation, and sticky lease computation."""

from __future__ import annotations

import contextlib
import socket
import time
from collections.abc import Mapping, Sequence
from typing import Any

from rig.core.constants import PORT_MAX, PORT_MIN, PORT_RELEASE_TIMEOUT_SECS

DEFAULT_PORT_WINDOW = 50


def _bind_candidate_port(port: int) -> tuple[socket.socket, int] | None:
    if port < PORT_MIN or port > PORT_MAX:
        return None
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind(("127.0.0.1", port))
        listener.listen(socket.SOMAXCONN)
    except OSError:
        listener.close()
        return None
    else:
        return listener, port


def allocate_listener(candidate_ports: Sequence[int] | None = None) -> tuple[socket.socket, int]:
    """Bind and listen on a loopback port, keeping ownership of it."""
    if candidate_ports:
        for port in candidate_ports:
            res = _bind_candidate_port(port)
            if res is not None:
                return res

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
    """Invoke allocate_listener with candidate_ports, tolerating 0-arg mocks in tests."""
    if candidate_ports is None:
        return allocate_listener()
    try:
        return allocate_listener(candidate_ports)
    except TypeError:
        return allocate_listener()


def reserve_port(candidate_ports: Sequence[int] | None = None) -> int:
    """Return a currently free loopback port for a service that cannot inherit a socket."""
    listener, port = _safe_allocate_listener(candidate_ports)
    listener.close()
    return port


def _safe_reserve_port(candidate_ports: Sequence[int] | None = None) -> int:
    """Invoke reserve_port with candidate_ports, tolerating 0-arg mocks in tests."""
    if candidate_ports is None:
        return reserve_port()
    try:
        return reserve_port(candidate_ports)
    except TypeError:
        return reserve_port()


def port_is_free(port: int) -> bool:
    """Return ``True`` when nothing is listening on ``port`` on loopback."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    with probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


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
    if any(k in slug for k in ("front", "web", "ui", "client", "next", "vite")):
        return 3000
    if any(k in slug for k in ("back", "api", "server", "app", "worker")):
        return 8000
    if any(k in slug for k in ("doc", "storybook", "admin")):
        return 4000
    return 5000


def _collect_excluded_ports(state: Mapping[str, Any], avoid: set[int] | None) -> set[int]:
    excluded = set(avoid or ())
    for srec in state.get("services", {}).values():
        if isinstance(srec, dict) and srec.get("port"):
            with contextlib.suppress(ValueError, TypeError):
                excluded.add(int(srec["port"]))
    return excluded


def _resolve_base_port(service: Any, state: Mapping[str, Any]) -> int:
    pref = getattr(service, "preferred_port", None)
    if pref is not None and PORT_MIN <= pref <= PORT_MAX:
        return pref
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
    excluded = _collect_excluded_ports(state, avoid)
    base_port = _resolve_base_port(service, state)
    candidates: list[int] = []
    limit = min(base_port + DEFAULT_PORT_WINDOW, PORT_MAX + 1)
    for port in range(base_port, limit):
        if port not in excluded:
            candidates.append(port)
    return candidates
