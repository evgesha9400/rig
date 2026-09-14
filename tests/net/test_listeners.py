"""Tests for the loopback listener socket used for zero-race binding."""

import errno
import socket

import pytest

from rig import cli as rig

stack = rig


def test_allocate_listener_returns_a_bound_loopback_socket():
    listener, port = stack.allocate_listener()
    try:
        host, bound_port = listener.getsockname()
        assert host == "127.0.0.1"
        assert bound_port == port
        assert port > 0
    finally:
        listener.close()


def test_allocate_listener_holds_the_port_so_it_cannot_be_stolen():
    listener, port = stack.allocate_listener()
    try:
        thief = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        with thief, pytest.raises(OSError) as raised:
            thief.bind(("127.0.0.1", port))
        assert raised.value.errno in (errno.EADDRINUSE, errno.EACCES)
    finally:
        listener.close()


def test_allocate_listener_does_not_enable_so_reuseport():
    listener, _ = stack.allocate_listener()
    try:
        reuseport = getattr(socket, "SO_REUSEPORT", None)
        if reuseport is None:
            pytest.skip("SO_REUSEPORT is unavailable on this platform")
        assert listener.getsockopt(socket.SOL_SOCKET, reuseport) == 0
    finally:
        listener.close()
