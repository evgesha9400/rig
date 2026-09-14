"""CLI surface: up/down/status flags with scopes, default scope, and idempotency."""

import json

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


def test_cli_exposes_up_down_and_status_with_every_scope():
    parser = stack.build_parser()

    for command in ("up", "down"):
        for scope in ("full", "local", "backend", "ui"):
            args = parser.parse_args([command, "--scope", scope])
            assert args.command == command
            assert args.scope == scope

    assert parser.parse_args(["status"]).command == "status"


def test_cli_defaults_to_the_full_scope():
    parser = stack.build_parser()

    assert parser.parse_args(["up"]).scope == "full"


def test_cli_rejects_an_unknown_scope(tmp_path):
    manifest_path = tmp_path / "stack.json"
    manifest_path.write_text(json.dumps(SAMPLE))
    ret = stack.main(
        ["--root", str(tmp_path), "--manifest", str(manifest_path), "up", "--scope", "sideways"]
    )
    assert ret != 0


def test_status_of_an_empty_checkout_reports_no_services(tmp_path, capsys):
    manifest_path = tmp_path / "stack.json"
    manifest_path.write_text(json.dumps(SAMPLE))

    exit_code = stack.cmd_status(root=tmp_path, manifest_path=manifest_path)

    assert exit_code == 0
    assert "backend" in capsys.readouterr().out


def test_down_on_an_empty_checkout_is_idempotent(tmp_path):
    manifest_path = tmp_path / "stack.json"
    manifest_path.write_text(json.dumps(SAMPLE))

    first = stack.cmd_down(root=tmp_path, manifest_path=manifest_path, scope="full")
    second = stack.cmd_down(root=tmp_path, manifest_path=manifest_path, scope="full")

    assert first == 0
    assert second == 0
