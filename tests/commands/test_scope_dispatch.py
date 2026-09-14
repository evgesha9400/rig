"""Scope resolution rejects unknown scopes; manifest errors exit with EXIT_USAGE."""

import json

import pytest

from rig import cli as rig

stack = rig

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


def _manifest(tmp_path, payload):
    manifest_path = tmp_path / "rig.json"
    manifest_path.write_text(json.dumps(payload))
    return stack.load_manifest(manifest_path)


def test_scope_resolution_rejects_an_unknown_scope(tmp_path):
    manifest = _manifest(tmp_path, SAMPLE)

    with pytest.raises(stack.StackError):
        manifest.resolve_scope("sideways")


def test_invalid_manifest_reports_usage_exit_code(tmp_path):
    """R2-10: manifest syntax, schema and scope errors exit with EXIT_USAGE."""
    manifest_path = tmp_path / "rig.json"

    manifest_path.write_text("{not json")
    with pytest.raises(stack.RigError) as exc_info:
        stack.load_manifest(manifest_path)
    assert (exc_info.value.code, exc_info.value.exit_code) == ("E_USAGE", stack.EXIT_USAGE)

    manifest_path.write_text(json.dumps({"services": {"s": {"type": "port", "command": ["echo"]}}}))
    with pytest.raises(stack.RigError) as exc_info:
        stack.load_manifest(manifest_path)
    assert exc_info.value.exit_code == stack.EXIT_USAGE

    manifest_path.write_text(
        json.dumps(
            {
                "project": "p",
                "services": {"s": {"type": "port", "command": ["echo"]}},
                "scopes": {"weird": [7]},
            }
        )
    )
    with pytest.raises(stack.RigError) as exc_info:
        stack.load_manifest(manifest_path)
    assert (exc_info.value.code, exc_info.value.exit_code) == ("E_USAGE", stack.EXIT_USAGE)

    manifest_path.write_text(
        json.dumps({"project": "p", "services": {"s": {"type": "port", "command": ["echo"]}}})
    )
    manifest = stack.load_manifest(manifest_path)
    with pytest.raises(stack.RigError) as exc_info:
        manifest.resolve_scope("missing")
    assert (exc_info.value.code, exc_info.value.exit_code) == ("E_USAGE", stack.EXIT_USAGE)
