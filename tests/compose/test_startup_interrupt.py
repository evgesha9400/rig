"""R9-1: an interrupted Compose startup leaves no unrecorded container."""

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
    """Answer Compose normally until ``step``, which raises ``error``."""

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
    """Return whatever exception `_start_compose_service` raises, of any class."""
    try:
        stack._start_compose_service(service, root, instance)
    except (KeyboardInterrupt, SystemExit, RuntimeError, OSError) as exc:
        return exc
    raise AssertionError("expected _start_compose_service to fail")


def test_compose_container_discovery_interrupt_records_the_container(monkeypatch, tmp_path):
    monkeypatch.setattr(stack, "run_compose", _interrupted_compose("ps", cleanup_ok=False))

    err = _failed_compose_start(_compose_service(), tmp_path)

    assert isinstance(err, stack.RigError), f"the interrupt escaped unrecorded: {err!r}"
    partial = err.details.get("partial_record")
    assert partial is not None
    assert partial["name"] == "db"
    assert partial["type"] == "compose"
    assert partial["instance"] == "inst-1"
    assert isinstance(err.__cause__, KeyboardInterrupt)
    assert "prune" in (err.hint or "")


def test_compose_container_discovery_interrupt_reraises_after_a_clean_reclaim(
    monkeypatch, tmp_path
):
    calls: list[list[str]] = []
    fake = _interrupted_compose("ps", cleanup_ok=True)

    def recording(instance, root, cfile, args, context=None, timeout=180.0, env=None, **kwargs):
        calls.append(list(args))
        return fake(instance, root, cfile, args, context, timeout, env, **kwargs)

    monkeypatch.setattr(stack, "run_compose", recording)

    with pytest.raises(KeyboardInterrupt):
        stack._start_compose_service(_compose_service(), tmp_path, "inst-1")

    assert ["stop", "db"] in calls
    assert ["rm", "-f", "db"] in calls


def test_compose_port_discovery_interrupt_records_the_discovered_container(monkeypatch, tmp_path):
    monkeypatch.setattr(stack, "run_compose", _interrupted_compose("port", cleanup_ok=False))

    err = _failed_compose_start(_compose_service(compose_port=5432), tmp_path)

    assert isinstance(err, stack.RigError), f"the interrupt escaped unrecorded: {err!r}"
    partial = err.details.get("partial_record")
    assert partial is not None
    assert partial["container"] == "abc123"
    assert partial["port"] is None
    assert isinstance(err.__cause__, KeyboardInterrupt)


def test_compose_discovery_system_exit_records_the_container(monkeypatch, tmp_path):
    monkeypatch.setattr(
        stack, "run_compose", _interrupted_compose("ps", cleanup_ok=False, error=SystemExit)
    )

    err = _failed_compose_start(_compose_service(), tmp_path)

    assert isinstance(err, stack.RigError), f"the exit escaped unrecorded: {err!r}"
    assert err.details.get("partial_record") is not None
    assert isinstance(err.__cause__, SystemExit)


def test_compose_cleanup_interrupted_twice_still_records_the_container(monkeypatch, tmp_path):
    def fake_run_compose(
        instance, root, cfile, args, context=None, timeout=180.0, env=None, **kwargs
    ):
        if args[0] == "up":
            return subprocess.CompletedProcess(["docker"], 0, "", "")
        raise KeyboardInterrupt()

    monkeypatch.setattr(stack, "run_compose", fake_run_compose)

    err = _failed_compose_start(_compose_service(), tmp_path)
    assert isinstance(err, stack.RigError), f"the interrupt escaped unrecorded: {err!r}"
    assert err.details.get("partial_record") is not None
