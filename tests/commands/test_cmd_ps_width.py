"""Tests for terminal-aware width truncation and --wide support in rig ps."""

import json
import os
from pathlib import Path

from rig import cli as rig

stack = rig


def _setup_test_instance(tmp_path: Path, monkeypatch) -> None:
    state_home = tmp_path / "rig_state"
    monkeypatch.setenv("RIG_STATE_HOME", str(state_home))
    inst_dir = stack.ensure_instance_dir("longproject-e9297fec")
    home = os.path.expanduser("~")
    fake_root = Path(home) / "Work" / "long-nested-directory-structure" / "target-project"
    state = {
        "instance": "longproject-e9297fec",
        "project": "longproject",
        "mode": "default",
        "root": str(fake_root),
        "services": {
            "web": {
                "name": "web",
                "type": "port",
                "pid": 1234,
                "pgid": 1234,
                "port": 3000,
                "url": "http://127.0.0.1:3000",
                "start_time": "Thu Jan 1 00:00:00 2026",
                "identity": "python web.py",
            }
        },
    }
    stack.write_state(inst_dir / "state.json", state)
    monkeypatch.setattr(stack, "pid_alive", lambda pid: True)
    monkeypatch.setattr(stack, "identity_matches", lambda rec: True)


def test_cmd_ps_truncates_in_narrow_terminal(monkeypatch, tmp_path, capsys):
    """`ps` must truncate root with ellipsis and keep lines <= terminal width when isatty is True."""
    _setup_test_instance(tmp_path, monkeypatch)

    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr(
        "shutil.get_terminal_size", lambda fallback=(80, 24): os.terminal_size((80, 24))
    )

    assert stack.cmd_ps(as_json=False) == stack.EXIT_OK
    table_out = capsys.readouterr().out
    lines = [line for line in table_out.splitlines() if line.strip()]

    # Header and divider + 1 row
    assert len(lines) >= 3
    for line in lines:
        assert len(line) <= 80
    assert "…" in table_out
    assert "e9297fec" in table_out
    assert "longproject-e9297fec" not in table_out


def test_cmd_ps_wide_preserves_full_paths_and_ids(monkeypatch, tmp_path, capsys):
    """`ps --wide` must output full instance names and unclipped paths."""
    _setup_test_instance(tmp_path, monkeypatch)

    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr(
        "shutil.get_terminal_size", lambda fallback=(80, 24): os.terminal_size((80, 24))
    )

    assert stack.cmd_ps(as_json=False, wide=True) == stack.EXIT_OK
    table_out = capsys.readouterr().out

    assert "longproject-e9297fec" in table_out
    assert "long-nested-directory-structure" in table_out
    assert "…" not in table_out


def test_cmd_ps_json_remains_untruncated(monkeypatch, tmp_path, capsys):
    """`ps --json` must return pristine unshortened paths and instance IDs."""
    _setup_test_instance(tmp_path, monkeypatch)

    assert stack.cmd_ps(as_json=True) == stack.EXIT_OK
    payload = json.loads(capsys.readouterr().out)

    assert payload["ok"] is True
    inst = payload["data"]["instances"][0]
    assert inst["instance"] == "longproject-e9297fec"
    assert "long-nested-directory-structure" in inst["root"]
    assert "…" not in inst["root"]


def test_cmd_ps_non_matching_prefix_preserves_id(monkeypatch, tmp_path, capsys):
    """`ps` must preserve instance ID when it does not start with project prefix."""
    state_home = tmp_path / "rig_state"
    monkeypatch.setenv("RIG_STATE_HOME", str(state_home))
    inst_dir = stack.ensure_instance_dir("custom-instance")
    state = {
        "instance": "custom-instance",
        "project": "otherproject",
        "mode": "default",
        "root": "/tmp/test",
        "services": {},
    }
    stack.write_state(inst_dir / "state.json", state)

    assert stack.cmd_ps(as_json=False) == stack.EXIT_OK
    table_out = capsys.readouterr().out
    assert "custom-instance" in table_out
