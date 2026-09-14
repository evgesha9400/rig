"""Tests for candidate-port preference and fallback allocation."""

import socket

import pytest

from rig import cli as rig

stack = rig


def test_allocate_listener_uses_candidate_port():
    candidate = 39120
    if not stack.port_is_free(candidate):
        pytest.skip(f"Port {candidate} is not free on host")
    listener, port = stack.allocate_listener([candidate, candidate + 1])
    try:
        assert port == candidate
        assert listener.getsockname() == ("127.0.0.1", candidate)
    finally:
        listener.close()


def test_allocate_listener_skips_occupied_candidate():
    port1 = 39121
    port2 = 39122
    if not stack.port_is_free(port1) or not stack.port_is_free(port2):
        pytest.skip("Test ports are not free on host")
    squatter = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    squatter.bind(("127.0.0.1", port1))
    squatter.listen(1)
    try:
        listener, port = stack.allocate_listener([port1, port2])
        try:
            assert port == port2
            assert listener.getsockname() == ("127.0.0.1", port2)
        finally:
            listener.close()
    finally:
        squatter.close()


def test_allocate_listener_falls_back_when_all_candidates_exhausted():
    port1 = 39123
    if not stack.port_is_free(port1):
        pytest.skip(f"Port {port1} is not free on host")
    squatter = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    squatter.bind(("127.0.0.1", port1))
    squatter.listen(1)
    try:
        listener, port = stack.allocate_listener([port1])
        try:
            assert port != port1
            assert port > 0
        finally:
            listener.close()
    finally:
        squatter.close()


def test_compute_candidate_ports_precedence():
    frontend_svc = stack.Service(name="frontend", type="port", command=["echo"])
    candidates = stack.compute_candidate_ports(frontend_svc, {})
    assert candidates[0] == 3000
    assert candidates[1] == 3001

    backend_svc = stack.Service(name="backend", type="port", command=["echo"])
    candidates = stack.compute_candidate_ports(backend_svc, {})
    assert candidates[0] == 8000

    docs_svc = stack.Service(name="docs", type="port", command=["echo"])
    candidates = stack.compute_candidate_ports(docs_svc, {})
    assert candidates[0] == 4000

    other_svc = stack.Service(name="metrics", type="port", command=["echo"])
    candidates = stack.compute_candidate_ports(other_svc, {})
    assert candidates[0] == 5000

    leased_state = {"ports": {"frontend": 3042}}
    candidates = stack.compute_candidate_ports(frontend_svc, leased_state)
    assert candidates[0] == 3042

    custom_svc = stack.Service(name="frontend", type="port", command=["echo"], preferred_port=3099)
    candidates = stack.compute_candidate_ports(custom_svc, leased_state)
    assert candidates[0] == 3099

    active_state = {"services": {"other": {"port": 3000}}}
    candidates = stack.compute_candidate_ports(frontend_svc, active_state, avoid={3001})
    assert 3000 not in candidates
    assert 3001 not in candidates
    assert candidates[0] == 3002
