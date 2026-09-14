"""Tests for healthcheck field validation, literal-brace rendering, and dependency order."""

import json
import socket
import subprocess

import pytest

from rig import cli as rig

stack = rig


def test_single_pass_rendering_tolerates_literal_braces_in_paths(monkeypatch, tmp_path):
    root_with_brace = tmp_path / "repo_{dev}"
    root_with_brace.mkdir()
    log_path = root_with_brace / "test.log"

    raw_argv = ["python", "-m", "app", "--root", "{root}", "--fd", "{fd}"]
    values = {"root": str(root_with_brace)}

    monkeypatch.setattr(stack, "allocate_listener", lambda: (socket.socket(), 8000))
    recorded = []

    def mock_popen(argv, *args, **kwargs):
        recorded.append(argv)

        class MockProc:
            pid = 12345

        return MockProc()

    monkeypatch.setattr(subprocess, "Popen", mock_popen)
    monkeypatch.setattr(stack, "_record", lambda *a, **kw: {"name": "backend"})

    stack.spawn_fd_service("backend", raw_argv, root_with_brace, {}, log_path, values=values)

    assert len(recorded) == 1
    assert str(root_with_brace) in recorded[0]


def test_healthcheck_field_validation(tmp_path):
    """R2-7: healthcheck_timeout and healthcheck_path are validated as usage errors."""
    manifest_path = tmp_path / "rig.json"

    def load(spec):
        manifest_path.write_text(json.dumps({"project": "hc", "services": {"s": spec}}))
        return stack.load_manifest(manifest_path)

    for bad in (0, -1, "fast", float("inf")):
        with pytest.raises(stack.RigError) as exc_info:
            load({"type": "port", "command": ["echo"], "healthcheck_timeout": bad})
        assert exc_info.value.code == "E_USAGE"
        assert "healthcheck_timeout" in exc_info.value.message

    with pytest.raises(stack.RigError) as exc_info:
        load({"type": "port", "command": ["echo"], "healthcheck_path": 5})
    assert exc_info.value.code == "E_USAGE"
    with pytest.raises(stack.RigError):
        load({"type": "port", "command": ["echo"], "healthcheck_path": ""})

    manifest = load({"type": "port", "command": ["echo"], "healthcheck_timeout": 2.5})
    assert manifest.services["s"].healthcheck_timeout == 2.5


def test_reverse_dependency_order_tolerates_duplicate_dependencies():
    """R2-8: a repeated depends_on entry must not corrupt the teardown order."""
    services = {
        "db": {"depends_on": []},
        "api": {"depends_on": ["db", "db", "db"]},
    }
    order = stack.reverse_dependency_order(services)
    assert order.index("api") < order.index("db")

    deeper = {
        "db": {"depends_on": []},
        "cache": {"depends_on": ["db", "db"]},
        "api": {"depends_on": ["cache", "cache", "db"]},
    }
    deep_order = stack.reverse_dependency_order(deeper)
    assert deep_order.index("api") < deep_order.index("cache") < deep_order.index("db")

    # A self-referential record must not hang or vanish.
    assert set(stack.reverse_dependency_order({"a": {"depends_on": ["a"]}})) == {"a"}
