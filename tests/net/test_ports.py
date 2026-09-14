"""Tests for reserving a free loopback port for services without fd transfer."""

import socket

from rig import cli as rig

stack = rig


def test_reserve_port_returns_a_free_port():
    port = stack.reserve_port()
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    with probe:
        probe.bind(("127.0.0.1", port))


def test_reserve_port_avoids_ports_already_listening():
    holder, taken = stack.allocate_listener()
    try:
        for _ in range(20):
            assert stack.reserve_port() != taken
    finally:
        holder.close()


def test_port_is_free_ignores_connections_lingering_in_time_wait():
    """A closed service can leave TIME_WAIT sockets on its port.

    Those must not read as "still held", or teardown reports a false failure.
    """
    listener, port = stack.allocate_listener()
    client = socket.create_connection(("127.0.0.1", port), timeout=5)
    served, _ = listener.accept()
    client.close()
    served.close()
    listener.close()

    assert stack.port_is_free(port) is True


def test_port_is_free_detects_a_live_listener():
    listener, port = stack.allocate_listener()
    try:
        assert stack.port_is_free(port) is False
    finally:
        listener.close()
    assert stack.port_is_free(port) is True
