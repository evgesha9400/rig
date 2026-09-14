"""R10-1: an interrupted `up` leaves no unrecorded container."""

import json
import subprocess

import pytest

from rig import cli as rig

stack = rig


def _compose_service(name="db", **kwargs):
    return stack.Service(
        name=name, type="compose", compose_file="compose.yml", compose_service=name, **kwargs
    )


def _interrupted_compose(
    step: str, cleanup_ok: bool, error: type[BaseException] = KeyboardInterrupt
):
    def fake_run_compose(
        instance, root, cfile, args, context=None, timeout=180.0, env=None, **kwargs
    ):
        verb = args[0]
        if verb == step:
            raise error()
        if verb == "up":
            return subprocess.CompletedProcess(["docker"], 0, "", "")
        if verb == "ps":
            return subprocess.CompletedProcess(["docker"], 0, "abc123\n", "")
        if verb == "port":
            return subprocess.CompletedProcess(["docker"], 0, "0.0.0.0:54321\n", "")
        return subprocess.CompletedProcess(
            ["docker"], 0 if cleanup_ok else 1, "", "" if cleanup_ok else "cleanup refused"
        )

    return fake_run_compose


def _failed_compose_start(service, root, instance="inst-1"):
    try:
        stack._start_compose_service(service, root, instance)
    except (KeyboardInterrupt, SystemExit, RuntimeError, OSError) as exc:
        return exc
    raise AssertionError("expected _start_compose_service to fail")


def test_compose_up_interrupt_records_the_container(monkeypatch, tmp_path):
    monkeypatch.setattr(stack, "run_compose", _interrupted_compose("up", cleanup_ok=False))

    err = _failed_compose_start(_compose_service(), tmp_path)

    assert isinstance(err, stack.RigError), f"the interrupt escaped unrecorded: {err!r}"
    partial = err.details.get("partial_record")
    assert partial is not None
    assert partial["name"] == "db"
    assert partial["container"] == ""
    assert partial["compose_service"] == "db"
    assert isinstance(err.__cause__, KeyboardInterrupt)
    assert "prune" in (err.hint or "")


def test_compose_up_interrupt_reraises_after_a_clean_reclaim(monkeypatch, tmp_path):
    calls: list[list[str]] = []
    fake = _interrupted_compose("up", cleanup_ok=True)

    def recording(instance, root, cfile, args, context=None, timeout=180.0, env=None, **kwargs):
        calls.append(list(args))
        return fake(instance, root, cfile, args, context, timeout, env, **kwargs)

    monkeypatch.setattr(stack, "run_compose", recording)

    with pytest.raises(KeyboardInterrupt):
        stack._start_compose_service(_compose_service(), tmp_path, "inst-1")

    assert ["stop", "db"] in calls
    assert ["rm", "-f", "db"] in calls


def test_compose_up_system_exit_records_the_container(monkeypatch, tmp_path):
    monkeypatch.setattr(
        stack, "run_compose", _interrupted_compose("up", cleanup_ok=False, error=SystemExit)
    )

    err = _failed_compose_start(_compose_service(), tmp_path)

    assert isinstance(err, stack.RigError), f"the exit escaped unrecorded: {err!r}"
    assert err.details.get("partial_record") is not None
    assert isinstance(err.__cause__, SystemExit)


def test_compose_up_interrupt_is_recorded_by_cmd_up(monkeypatch, tmp_path):
    monkeypatch.setenv("RIG_STATE_HOME", str(tmp_path / "rig_state"))
    (tmp_path / "compose.yml").write_text("services: {}\n")
    manifest_path = tmp_path / "rig.json"
    manifest_path.write_text(
        json.dumps(
            {
                "project": "ints",
                "services": {
                    "db": {
                        "type": "compose",
                        "compose_file": "compose.yml",
                        "compose_service": "db",
                    }
                },
                "scopes": {"full": ["db"]},
            }
        )
    )
    monkeypatch.setattr(stack, "run_compose", _interrupted_compose("up", cleanup_ok=False))

    ret = stack.cmd_up(tmp_path, manifest_path)

    assert ret == stack.EXIT_OP_FAILED
    state = stack.read_state(
        stack._state_path(tmp_path, instance=stack.instance_id("ints", tmp_path))
    )
    assert "db" in state["services"], "the interrupted container was left unrecorded"
    assert state["services"]["db"]["type"] == "compose"
