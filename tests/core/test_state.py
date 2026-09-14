"""Tests for atomic, owner-only state file serialization."""

import stat

from rig import cli as rig

stack = rig


def test_state_round_trips_through_an_atomic_write(tmp_path):
    path = tmp_path / "state.json"
    state = {
        "instance": "deltalytic-abcd1234",
        "generation": 3,
        "services": {"backend": {"pid": 42, "port": 5000}},
    }

    stack.write_state(path, state)

    assert stack.read_state(path) == state


def test_state_write_leaves_no_temporary_file_behind(tmp_path):
    path = tmp_path / "state.json"

    stack.write_state(path, {"services": {}})

    assert list(tmp_path.iterdir()) == [path]


def test_state_write_replaces_the_previous_generation_wholesale(tmp_path):
    path = tmp_path / "state.json"
    stack.write_state(path, {"generation": 1, "services": {"backend": {"pid": 1}}})

    stack.write_state(path, {"generation": 2, "services": {}})

    assert stack.read_state(path) == {"generation": 2, "services": {}}


def test_state_write_is_owner_readable_only(tmp_path):
    path = tmp_path / "state.json"

    stack.write_state(path, {"services": {}})

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_state_read_of_a_missing_file_returns_an_empty_stack(tmp_path):
    state = stack.read_state(tmp_path / "absent.json")

    assert state["services"] == {}
    assert state["generation"] == 0


def test_state_read_of_malformed_json_returns_an_empty_stack(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{ this is not json")

    state = stack.read_state(path)

    assert state["services"] == {}


def test_state_never_records_secret_values(tmp_path):
    path = tmp_path / "state.json"
    record = stack.redact(
        {
            "pid": 7,
            "port": 5000,
            "env": {"PLATFORM_TOKEN": "super-secret", "BACKEND_PORT": "5000"},
        }
    )

    stack.write_state(path, {"services": {"backend": record}})

    assert "super-secret" not in path.read_text()
