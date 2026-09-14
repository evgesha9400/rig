"""R10-2: a compose file declaring a required variable stays checkable and stoppable."""

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


def _compose_service(name="db", **kwargs):
    return stack.Service(
        name=name, type="compose", compose_file="compose.yml", compose_service=name, **kwargs
    )


class _FakeDocker:
    def __init__(self, inspect="running", ps_ids=("abc123",), failing=()):
        self.inspect = inspect
        self.ps_ids = list(ps_ids)
        self.failing = set(failing)
        self.calls: list[list[str]] = []

    def __call__(self, args, context=None, timeout=60.0, **kwargs):
        args = list(args)
        self.calls.append(args)
        verb = args[0]
        if verb in self.failing:
            return subprocess.CompletedProcess(["docker"], 1, "", f"{verb} refused")
        if verb == "inspect":
            return subprocess.CompletedProcess(["docker"], 0, f"{self.inspect}\n", "")
        if verb == "ps":
            return subprocess.CompletedProcess(
                ["docker"], 0, "".join(f"{i}\n" for i in self.ps_ids), ""
            )
        return subprocess.CompletedProcess(["docker"], 0, "", "")

    def verbs(self):
        return [c[0] for c in self.calls]


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


def test_compose_status_survives_a_missing_required_variable(monkeypatch, tmp_path):
    _write_required_variable_compose_file(tmp_path)
    monkeypatch.delenv(REQUIRED_VAR, raising=False)
    monkeypatch.setattr(stack, "run_compose", _compose_requiring_variable())
    docker = _FakeDocker(inspect="running")
    monkeypatch.setattr(stack, "run_docker", docker)

    assert stack.compose_record_status(_compose_record(tmp_path), tmp_path) == "alive"
    assert "ps" in docker.verbs()
    assert "inspect" in docker.verbs()


def test_compose_teardown_survives_a_missing_required_variable(monkeypatch, tmp_path):
    _write_required_variable_compose_file(tmp_path)
    monkeypatch.delenv(REQUIRED_VAR, raising=False)
    monkeypatch.setattr(stack, "run_compose", _compose_requiring_variable())
    docker = _FakeDocker(inspect="running")
    monkeypatch.setattr(stack, "run_docker", docker)

    assert stack._stop_record(_compose_record(tmp_path), tmp_path) == "terminated"
    assert ["stop", "abc123"] in docker.calls
    assert ["rm", "-f", "abc123"] in docker.calls
    assert not any("-v" in args for args in docker.calls)


def test_compose_record_keeps_the_environment_its_compose_file_requires(monkeypatch, tmp_path):
    _write_required_variable_compose_file(tmp_path)
    monkeypatch.setattr(
        stack,
        "run_compose",
        lambda *a, **k: subprocess.CompletedProcess(["docker"], 0, "abc123\n", ""),
    )

    record = stack._start_compose_service(
        _compose_service(), tmp_path, "inst-1", env={REQUIRED_VAR: "s3cret", "DB_USER": "app"}
    )

    assert REQUIRED_VAR in record["compose_env"]
    assert record["compose_env"][REQUIRED_VAR] == stack.REDACTED
    assert record["compose_env"]["DB_USER"] == "app"


def test_compose_status_replays_the_recorded_environment(monkeypatch, tmp_path):
    _write_required_variable_compose_file(tmp_path)
    monkeypatch.delenv(REQUIRED_VAR, raising=False)
    seen: list[tuple[list[str], dict | None]] = []
    monkeypatch.setattr(stack, "run_compose", _compose_requiring_variable(seen))

    def fake_run_docker(args, context=None, timeout=60.0, **kwargs):
        args = list(args)
        assert args[0] != "ps", "the recorded environment did not reach Compose"
        return subprocess.CompletedProcess(["docker"], 0, "running\n", "")

    monkeypatch.setattr(stack, "run_docker", fake_run_docker)

    record = dict(_compose_record(tmp_path), compose_env={REQUIRED_VAR: stack.REDACTED})
    assert stack.compose_record_status(record, tmp_path) == "alive"
    assert seen and seen[0][1] is not None
    assert seen[0][1][REQUIRED_VAR] == stack.REDACTED
