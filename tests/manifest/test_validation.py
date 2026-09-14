"""Manifest validation rejects malformed structures."""

import json

import pytest

from rig import cli as rig

stack = rig


def test_manifest_validation_rejects_malformed_structures(tmp_path):
    p = tmp_path / "invalid.json"

    # Services is not a mapping
    p.write_text(json.dumps({"project": "x", "services": ["not", "a", "dict"]}))
    with pytest.raises(stack.StackError, match="must be a JSON object"):
        stack.load_manifest(p)

    # env is not a dict
    p.write_text(
        json.dumps(
            {"project": "x", "services": {"s": {"type": "port", "command": ["echo"], "env": "foo"}}}
        )
    )
    with pytest.raises(stack.StackError, match="must be a JSON object"):
        stack.load_manifest(p)

    # depends_on is not a list
    p.write_text(
        json.dumps(
            {
                "project": "x",
                "services": {"s": {"type": "port", "command": ["echo"], "depends_on": "other"}},
            }
        )
    )
    with pytest.raises(stack.StackError, match="'depends_on' must be a list"):
        stack.load_manifest(p)
