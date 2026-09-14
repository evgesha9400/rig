"""Manifest exposes project/services; rejects unknown service/cycle; scope resolution."""

import json

import pytest

from rig import cli as rig

stack = rig


def _manifest(tmp_path, payload):
    path = tmp_path / "stack.json"
    path.write_text(json.dumps(payload))
    return stack.load_manifest(path)


SAMPLE = {
    "project": "sample",
    "services": {
        "backend": {"type": "fd", "cwd": ".", "command": ["true", "--fd", "{fd}"]},
        "frontend": {
            "type": "port",
            "cwd": ".",
            "command": ["true", "--port", "{port}"],
            "depends_on": ["backend"],
        },
    },
    "scopes": {
        "full": ["backend", "frontend"],
        "local": ["backend", "frontend"],
        "backend": ["backend"],
        "ui": ["frontend"],
    },
}


def test_manifest_exposes_project_and_services(tmp_path):
    manifest = _manifest(tmp_path, SAMPLE)

    assert manifest.project == "sample"
    assert set(manifest.services) == {"backend", "frontend"}


def test_manifest_rejects_a_scope_naming_an_unknown_service(tmp_path):
    payload = json.loads(json.dumps(SAMPLE))
    payload["scopes"]["full"] = ["backend", "ghost"]

    with pytest.raises(stack.StackError):
        _manifest(tmp_path, payload)


def test_manifest_rejects_a_dependency_on_an_unknown_service(tmp_path):
    payload = json.loads(json.dumps(SAMPLE))
    payload["services"]["frontend"]["depends_on"] = ["ghost"]

    with pytest.raises(stack.StackError):
        _manifest(tmp_path, payload)


def test_manifest_rejects_a_dependency_cycle(tmp_path):
    payload = json.loads(json.dumps(SAMPLE))
    payload["services"]["backend"]["depends_on"] = ["frontend"]

    with pytest.raises(stack.StackError):
        _manifest(tmp_path, payload)


def test_manifest_rejects_an_unknown_service_type(tmp_path):
    payload = json.loads(json.dumps(SAMPLE))
    payload["services"]["backend"]["type"] = "telepathy"

    with pytest.raises(stack.StackError):
        _manifest(tmp_path, payload)


def test_manifest_rejects_a_missing_file(tmp_path):
    with pytest.raises(stack.StackError):
        stack.load_manifest(tmp_path / "absent.json")


def test_scope_resolution_orders_dependencies_before_dependents(tmp_path):
    manifest = _manifest(tmp_path, SAMPLE)

    assert manifest.resolve_scope("full") == ["backend", "frontend"]


def test_scope_resolution_pulls_in_transitive_dependencies(tmp_path):
    manifest = _manifest(tmp_path, SAMPLE)

    assert manifest.resolve_scope("ui") == ["backend", "frontend"]


def test_scope_resolution_of_a_leaf_scope_stays_narrow(tmp_path):
    manifest = _manifest(tmp_path, SAMPLE)

    assert manifest.resolve_scope("backend") == ["backend"]


def test_scope_resolution_rejects_an_unknown_scope(tmp_path):
    manifest = _manifest(tmp_path, SAMPLE)

    with pytest.raises(stack.StackError):
        manifest.resolve_scope("sideways")


def test_teardown_scope_is_reversed_and_excludes_dependencies(tmp_path):
    manifest = _manifest(tmp_path, SAMPLE)

    assert manifest.teardown_scope("full") == ["frontend", "backend"]
    assert manifest.teardown_scope("ui") == ["frontend"]


def test_dependents_of_a_service_are_reported(tmp_path):
    manifest = _manifest(tmp_path, SAMPLE)

    assert manifest.dependents("backend") == ["frontend"]
    assert manifest.dependents("frontend") == []
