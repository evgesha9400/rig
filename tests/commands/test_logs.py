"""Tests for rig logs command."""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from rig import cli as rig
from rig.core.constants import EXIT_NOT_FOUND, EXIT_OK
from rig.core.errors import RigError
from rig.core.identity import instance_id


def _setup_rig_env(tmp_path: Path) -> tuple[Path, Path]:
    manifest_path = tmp_path / "rig.json"
    manifest_path.write_text(
        json.dumps(
            {
                "project": "logproj",
                "services": {
                    "backend": {
                        "type": "port",
                        "cwd": ".",
                        "command": "python -m http.server {port}",
                    },
                    "worker": {
                        "type": "port",
                        "cwd": ".",
                        "command": "python worker.py",
                    },
                },
            }
        )
    )
    return tmp_path, manifest_path


def test_logs_service_not_found(tmp_path: Path):
    root, mf = _setup_rig_env(tmp_path)
    with pytest.raises(RigError) as exc_info:
        rig.cmd_logs(root, mf, service="nonexistent")
    assert exc_info.value.code == "E_SERVICE_NOT_FOUND"


def test_logs_missing_service_when_multiple(tmp_path: Path):
    root, mf = _setup_rig_env(tmp_path)
    with pytest.raises(RigError) as exc_info:
        rig.cmd_logs(root, mf, service=None)
    assert exc_info.value.code == "E_USAGE"


def test_logs_file_not_found(tmp_path: Path, monkeypatch):
    root, mf = _setup_rig_env(tmp_path)
    state_dir = tmp_path / "state"
    monkeypatch.setenv("RIG_STATE_HOME", str(state_dir))
    with pytest.raises(RigError) as exc_info:
        rig.cmd_logs(root, mf, service="backend")
    assert exc_info.value.code == "E_LOG_NOT_FOUND"
    assert exc_info.value.exit_code == EXIT_NOT_FOUND


def test_logs_local_tail_success(tmp_path: Path, monkeypatch, capsys):
    root, mf = _setup_rig_env(tmp_path)
    state_dir = tmp_path / "state"
    monkeypatch.setenv("RIG_STATE_HOME", str(state_dir))

    inst = instance_id("logproj", root.resolve())
    inst_dir = state_dir / "instances" / inst
    log_dir = inst_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "backend.log"
    log_lines = [f"line {i}\n" for i in range(1, 11)]
    log_file.write_text("".join(log_lines))

    state_file = inst_dir / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "project": "logproj",
                "instance": inst,
                "services": {
                    "backend": {
                        "name": "backend",
                        "type": "port",
                        "pid": 1234,
                        "log": str(log_file),
                    }
                },
            }
        )
    )

    ret = rig.cmd_logs(root, mf, service="backend", tail=5)
    assert ret == EXIT_OK
    out = capsys.readouterr().out
    assert "line 6" in out
    assert "line 10" in out
    assert "line 5" not in out


def test_logs_json_output(tmp_path: Path, monkeypatch, capsys):
    root, mf = _setup_rig_env(tmp_path)
    state_dir = tmp_path / "state"
    monkeypatch.setenv("RIG_STATE_HOME", str(state_dir))

    inst = instance_id("logproj", root.resolve())
    inst_dir = state_dir / "instances" / inst
    log_dir = inst_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "backend.log"
    log_file.write_text("line 1\nline 2\n")

    ret = rig.cmd_logs(root, mf, service="backend", tail=10, as_json=True)
    assert ret == EXIT_OK
    captured = capsys.readouterr().out
    parsed = json.loads(captured)
    assert parsed["schema"] == "rig.logs/1"
    assert parsed["ok"] is True
    assert parsed["data"]["service"] == "backend"
    assert parsed["data"]["count"] == 2
    assert parsed["data"]["lines"] == ["line 1", "line 2"]


def test_logs_compose_service(tmp_path: Path, monkeypatch, capsys):
    manifest_path = tmp_path / "rig.json"
    manifest_path.write_text(
        json.dumps(
            {
                "project": "composeproj",
                "services": {
                    "db": {
                        "type": "compose",
                        "compose_file": "docker-compose.yml",
                        "compose_service": "postgres",
                    }
                },
            }
        )
    )
    mock_run = MagicMock()
    mock_run.return_value = MagicMock(stdout="postgres ready\naccepting connections\n")
    monkeypatch.setattr("rig.commands.logs.run_compose", mock_run)

    ret = rig.cmd_logs(tmp_path, manifest_path, service="db", tail=2, as_json=True)
    assert ret == EXIT_OK
    mock_run.assert_called_once()
    captured = capsys.readouterr().out
    parsed = json.loads(captured)
    assert parsed["data"]["lines"] == ["postgres ready", "accepting connections"]
