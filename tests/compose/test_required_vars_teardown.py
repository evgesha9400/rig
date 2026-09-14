"""R10-2: teardown of a required-variable Compose file without reclaiming, or replaying env."""

import os
import subprocess
from pathlib import Path

from rig import cli as rig

stack = rig
REQUIRED_VAR = "DB_PASSWORD"


def _compose_record(root, container="abc123", compose_file="compose.yml", name="db"):
    return {
        "name": name,
        "type": "compose",
        "instance": "inst-1",
        "compose_file": str(Path(root) / compose_file),
        "compose_service": name,
        "container": container,
    }


class _FakeDocker:
    def __init__(self, inspect="running"):
        self.inspect = inspect
        self.calls: list[list[str]] = []

    def __call__(self, args, context=None, timeout=60.0, **kwargs):
        args = list(args)
        self.calls.append(args)
        if args[0] == "inspect":
            return subprocess.CompletedProcess(["docker"], 0, f"{self.inspect}\n", "")
        if args[0] == "ps":
            return subprocess.CompletedProcess(["docker"], 0, "abc123\n", "")
        return subprocess.CompletedProcess(["docker"], 0, "", "")


def _write_required_variable_compose_file(root, name="compose.yml"):
    path = Path(root) / name
    path.write_text(
        "services:\n"
        "  db:\n"
        "    image: postgres\n"
        f"    environment:\n      POSTGRES_PASSWORD: ${{{REQUIRED_VAR}:?required}}\n"
    )
    return path


def _compose_requiring_variable(seen=None):
    def fake_run_compose(
        instance, root, cfile, args, context=None, timeout=180.0, env=None, **kwargs
    ):
        if seen is not None:
            seen.append((list(args), None if env is None else dict(env)))
        ambient = os.environ if env is None else env
        if not ambient.get(REQUIRED_VAR):
            return subprocess.CompletedProcess(
                ["docker"], 1, "", f"required variable {REQUIRED_VAR} is missing a value"
            )
        return subprocess.CompletedProcess(["docker"], 0, "abc123\n", "")

    return fake_run_compose


def test_compose_stop_only_survives_a_missing_required_variable(monkeypatch, tmp_path):
    _write_required_variable_compose_file(tmp_path)
    monkeypatch.delenv(REQUIRED_VAR, raising=False)
    monkeypatch.setattr(stack, "run_compose", _compose_requiring_variable())
    docker = _FakeDocker(inspect="running")
    monkeypatch.setattr(stack, "run_docker", docker)

    outcome = stack._stop_record(_compose_record(tmp_path), tmp_path, remove=False)

    assert outcome == "terminated"
    assert ["stop", "abc123"] in docker.calls
    assert not any(args[0] == "rm" for args in docker.calls)


def test_compose_teardown_replays_the_recorded_environment(monkeypatch, tmp_path):
    _write_required_variable_compose_file(tmp_path)
    monkeypatch.delenv(REQUIRED_VAR, raising=False)
    seen: list[tuple[list[str], dict | None]] = []
    monkeypatch.setattr(stack, "run_compose", _compose_requiring_variable(seen))
    docker = _FakeDocker(inspect="running")
    monkeypatch.setattr(stack, "run_docker", docker)

    record = dict(_compose_record(tmp_path), compose_env={REQUIRED_VAR: stack.REDACTED})
    assert stack._stop_record(record, tmp_path) == "terminated"

    assert ["stop", "db"] in [args for args, _ in seen]
    assert ["rm", "-f", "db"] in [args for args, _ in seen]
    assert not any(args[0] in ("stop", "rm") for args in docker.calls)


def test_compose_env_is_optional_for_records_written_before_it(monkeypatch, tmp_path):
    (tmp_path / "compose.yml").write_text("services: {}\n")
    captured: list[dict | None] = []

    def fake_run_compose(
        instance, root, cfile, args, context=None, timeout=180.0, env=None, **kwargs
    ):
        captured.append(None if env is None else dict(env))
        return subprocess.CompletedProcess(["docker"], 0, "abc123\n", "")

    monkeypatch.setattr(stack, "run_compose", fake_run_compose)
    monkeypatch.setattr(stack, "run_docker", _FakeDocker(inspect="running"))

    record = _compose_record(tmp_path)
    assert "compose_env" not in record
    assert stack.compose_record_status(record, tmp_path) == "alive"
    assert captured == [None]
