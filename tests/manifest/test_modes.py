"""Manifest with modes; for_mode scope isolation."""

import json

import pytest

from rig import cli as rig

stack = rig


def test_manifest_with_modes_and_for_mode(tmp_path):
    manifest_path = tmp_path / "rig.json"
    manifest_data = {
        "project": "multi-stack",
        "default_mode": "native",
        "services": {
            "postgres": {
                "type": "compose",
                "compose_file": "docker-compose.yml",
                "compose_service": "postgres",
                "compose_port": 5432,
            }
        },
        "modes": {
            "native": {
                "services": {
                    "backend": {
                        "type": "fd",
                        "command": "python -m app",
                        "depends_on": ["postgres"],
                    }
                }
            },
            "container": {
                "services": {
                    "backend": {
                        "type": "compose",
                        "compose_file": "docker-compose.yml",
                        "compose_service": "backend",
                        "compose_port": 8000,
                        "depends_on": ["postgres"],
                    }
                }
            },
        },
    }
    manifest_path.write_text(json.dumps(manifest_data))

    manifest = stack.load_manifest(manifest_path)
    assert manifest.project == "multi-stack"
    assert manifest.default_mode == "native"
    assert "postgres" in manifest.base_services
    assert "native" in manifest.modes
    assert "container" in manifest.modes

    # Default mode is native
    assert manifest.active_mode == "native"
    assert manifest.services["backend"].type == "fd"

    # Switching to container mode
    container_manifest = manifest.for_mode("container")
    assert container_manifest.active_mode == "container"
    assert container_manifest.services["backend"].type == "compose"
    assert "postgres" in container_manifest.services

    # Invalid mode
    with pytest.raises(stack.StackError, match="unknown mode 'cloud'"):
        manifest.for_mode("cloud")


def test_manifest_for_mode_scopes_isolation(tmp_path):
    manifest_data = {
        "project": "scope-iso",
        "default_mode": "native",
        "modes": {
            "native": {"services": {"backend": {"type": "port", "cwd": ".", "command": ["echo"]}}},
            "container": {
                "services": {
                    "backend": {"type": "port", "cwd": ".", "command": ["echo"]},
                    "db": {
                        "type": "compose",
                        "compose_file": "compose.yml",
                        "compose_service": "db",
                    },
                }
            },
        },
    }
    manifest_path = tmp_path / "rig.json"
    manifest_path.write_text(json.dumps(manifest_data))

    raw = stack.load_manifest(manifest_path)
    native_m = raw.for_mode("native")
    container_m = raw.for_mode("container")

    assert native_m.scopes["full"] == ["backend"]
    assert sorted(container_m.scopes["full"]) == ["backend", "db"]
    # Verify native_m scopes were not contaminated by container_m
    assert "db" not in native_m.scopes["full"]
