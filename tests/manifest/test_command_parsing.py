"""Tests for manifest string-command parsing, derived scopes, and health field conflicts."""

import json

import pytest

from rig import cli as rig

stack = rig


def test_manifest_string_command_parsing(tmp_path):
    manifest_path = tmp_path / "stack.json"
    manifest_path.write_text(
        json.dumps(
            {
                "project": "sample",
                "services": {
                    "backend": {
                        "type": "fd",
                        "command": 'python -m myapp --title "hello world" --fd {fd}',
                        "health": "/health",
                    }
                },
            }
        )
    )
    manifest = stack.load_manifest(manifest_path)
    backend = manifest.services["backend"]
    assert backend.command == ["python", "-m", "myapp", "--title", "hello world", "--fd", "{fd}"]
    assert backend.healthcheck_path == "/health"


def test_manifest_string_command_syntax_error(tmp_path):
    manifest_path = tmp_path / "stack.json"
    manifest_path.write_text(
        json.dumps(
            {
                "project": "sample",
                "services": {
                    "backend": {
                        "type": "fd",
                        "command": 'python -m myapp "unclosed quote',
                    }
                },
            }
        )
    )
    with pytest.raises(stack.StackError, match="invalid command syntax"):
        stack.load_manifest(manifest_path)


def test_manifest_auto_derived_scopes_and_aliases(tmp_path):
    manifest_path = tmp_path / "stack.json"
    manifest_path.write_text(
        json.dumps(
            {
                "project": "sample",
                "services": {
                    "backend": {"type": "fd", "command": "python server.py --fd {fd}"},
                    "frontend": {
                        "type": "port",
                        "command": "node vite.js --port {port}",
                        "aliases": ["ui"],
                        "depends_on": ["backend"],
                    },
                },
            }
        )
    )
    manifest = stack.load_manifest(manifest_path)
    assert set(manifest.scopes["full"]) == {"backend", "frontend"}
    assert set(manifest.scopes["local"]) == {"backend", "frontend"}
    assert manifest.scopes["backend"] == ["backend"]
    assert manifest.scopes["frontend"] == ["frontend"]
    assert manifest.scopes["ui"] == ["frontend"]

    # up ui resolves dependencies: backend then frontend
    assert manifest.resolve_scope("ui") == ["backend", "frontend"]

    # down ui tears down direct members only: frontend
    assert manifest.teardown_scope("ui") == ["frontend"]


def test_manifest_conflicting_health_and_healthcheck_path(tmp_path):
    manifest_path = tmp_path / "stack.json"
    manifest_path.write_text(
        json.dumps(
            {
                "project": "sample",
                "services": {
                    "backend": {
                        "type": "fd",
                        "command": "echo",
                        "health": "/health1",
                        "healthcheck_path": "/health2",
                    }
                },
            }
        )
    )
    with pytest.raises(stack.StackError, match="conflicting 'health' and 'healthcheck_path'"):
        stack.load_manifest(manifest_path)
