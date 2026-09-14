"""Tests for machine-wide centralized port registry and sequential allocation."""

from pathlib import Path

from rig.net.ports import compute_candidate_ports
from rig.net.registry import (
    get_allocated_ports_for_others,
    get_or_allocate_port,
    list_port_allocations,
    read_port_registry,
    release_port_allocation,
)


class DummyService:
    def __init__(self, name: str, preferred_port: int | None = None) -> None:
        self.name = name
        self.preferred_port = preferred_port


def test_sequential_allocation_across_projects(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("RIG_STATE_HOME", str(tmp_path))

    port_p1 = get_or_allocate_port(("proj-alpha", "web"), 3000)
    port_p2 = get_or_allocate_port(("proj-beta", "web"), 3000)
    port_p3 = get_or_allocate_port(("proj-gamma", "web"), 3000)

    assert port_p1 == 3000
    assert port_p2 == 3001
    assert port_p3 == 3002

    # Verify backend services get sequential base ports in 8000 block
    api_p1 = get_or_allocate_port(("proj-alpha", "api"), 8000)
    api_p2 = get_or_allocate_port(("proj-beta", "api"), 8000)

    assert api_p1 == 8000
    assert api_p2 == 8001


def test_stability_across_repeated_calls(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("RIG_STATE_HOME", str(tmp_path))

    first = get_or_allocate_port(("my-app", "frontend"), 3000)
    assert first == 3000

    for _ in range(5):
        assert get_or_allocate_port(("my-app", "frontend"), 3000) == 3000


def test_candidate_ports_protects_other_projects_ports(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("RIG_STATE_HOME", str(tmp_path))

    # proj1 claims 3000, proj2 claims 3001
    get_or_allocate_port(("proj1", "frontend"), 3000)
    get_or_allocate_port(("proj2", "frontend"), 3000)

    svc1 = DummyService("frontend")
    svc2 = DummyService("frontend")

    state1 = {"project": "proj1", "services": {}}
    state2 = {"project": "proj2", "services": {}}

    assert get_allocated_ports_for_others(("proj1", "frontend")) == {3001}
    assert get_allocated_ports_for_others(("proj2", "frontend")) == {3000}

    cands1 = compute_candidate_ports(svc1, state1)
    cands2 = compute_candidate_ports(svc2, state2)

    assert cands1[0] == 3000
    assert 3001 not in cands1  # proj1 cannot take proj2's port

    assert cands2[0] == 3001
    assert 3000 not in cands2  # proj2 cannot take proj1's port


def test_release_port_allocation(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("RIG_STATE_HOME", str(tmp_path))

    get_or_allocate_port(("to-delete", "web"), 3000)
    get_or_allocate_port(("to-delete", "api"), 8000)
    assert len(list_port_allocations()) == 2

    released = release_port_allocation("to-delete", "web")
    assert released == [3000]
    assert len(list_port_allocations()) == 1

    # Re-allocating for another project can now reuse 3000
    reused = get_or_allocate_port(("new-project", "web"), 3000)
    assert reused == 3000


def test_registry_corrupted_file_recovery(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("RIG_STATE_HOME", str(tmp_path))
    reg_file = tmp_path / "ports.json"
    reg_file.write_text("invalid json content")

    data = read_port_registry(reg_file)
    assert data == {"version": 1, "allocations": {}}

    port = get_or_allocate_port(("recovered", "web"), 3000)
    assert port == 3000
