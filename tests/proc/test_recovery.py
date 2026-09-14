"""Service recovery: living pgid checks, exited compose dependency detection."""

import json

from rig import cli as rig

stack = rig


def test_cmd_up_recovers_exited_compose_dependency(monkeypatch, tmp_path, capsys):
    """An exited compose dependency must be restarted, not rejected as unhealthy."""
    monkeypatch.setenv("RIG_STATE_HOME", str(tmp_path / "rig_state"))
    (tmp_path / "compose.yml").write_text("services:\n  db:\n    image: postgres\n")
    manifest_path = tmp_path / "rig.json"
    manifest_path.write_text(
        json.dumps(
            {
                "project": "recover",
                "services": {
                    "db": {
                        "type": "compose",
                        "compose_file": "compose.yml",
                        "compose_service": "db",
                    },
                    "api": {
                        "type": "port",
                        "cwd": ".",
                        "command": ["echo"],
                        "depends_on": ["db"],
                    },
                },
                "scopes": {"full": ["db", "api"]},
            }
        )
    )
    instance = stack.instance_id("recover", tmp_path)
    state_path = stack._state_path(tmp_path, instance=instance)
    stack.write_state(
        state_path,
        {
            "instance": instance,
            "project": "recover",
            "root": str(tmp_path),
            "services": {
                "db": {
                    "name": "db",
                    "type": "compose",
                    "instance": instance,
                    "compose_file": str(tmp_path / "compose.yml"),
                    "compose_service": "db",
                    "container": "c0ffee",
                },
                "api": {
                    "name": "api",
                    "type": "port",
                    "pid": 4242,
                    "pgid": 4242,
                    "port": 8000,
                    "depends_on": ["db"],
                },
            },
        },
    )

    # The recorded container exists but has exited; the dependent is still healthy.
    monkeypatch.setattr(stack, "compose_record_status", lambda rec, root: "stopped")
    monkeypatch.setattr(stack, "pid_alive", lambda pid: True)
    monkeypatch.setattr(stack, "pgid_alive", lambda pgid: True)
    monkeypatch.setattr(stack, "identity_matches", lambda rec: True)

    stop_calls: list[tuple[str, bool]] = []

    def fake_stop(record, root, remove=False):
        stop_calls.append((record["name"], remove))
        return "terminated"

    started: list[str] = []

    def fake_start(service, root, runtime, instance, state, state_path):
        started.append(service.name)
        record = {
            "name": service.name,
            "type": service.type,
            "port": 9000 + len(started),
            "url": f"http://127.0.0.1:{9000 + len(started)}",
        }
        state["services"][service.name] = record
        stack.write_state(state_path, state)
        return record

    monkeypatch.setattr(stack, "_stop_record", fake_stop)
    monkeypatch.setattr(stack, "_start_with_retry", fake_start)

    ret = stack.cmd_up(root=tmp_path, manifest_path=manifest_path, scope="full", as_json=True)
    envelope = json.loads(capsys.readouterr().out)

    assert ret == stack.EXIT_OK
    assert envelope["ok"] is True
    # The exited dependency is reclaimed (container removed) and started again,
    # then the dependent that was stopped to re-link comes back with it.
    assert ("db", True) in stop_calls
    assert started == ["db", "api"]
    assert set(stack.read_state(state_path)["services"]) == {"db", "api"}


def test_cmd_up_reports_unreclaimable_record_and_preserves_it(monkeypatch, tmp_path):
    """Recovery still fails loudly when the stale record cannot be reclaimed."""
    monkeypatch.setenv("RIG_STATE_HOME", str(tmp_path / "rig_state"))
    manifest_path = tmp_path / "rig.json"
    manifest_path.write_text(
        json.dumps(
            {
                "project": "stuck",
                "services": {"api": {"type": "port", "cwd": ".", "command": ["echo"]}},
                "scopes": {"full": ["api"]},
            }
        )
    )
    instance = stack.instance_id("stuck", tmp_path)
    state_path = stack._state_path(tmp_path, instance=instance)
    stack.write_state(
        state_path,
        {
            "instance": instance,
            "project": "stuck",
            "root": str(tmp_path),
            "services": {"api": {"name": "api", "type": "port", "pid": 4242, "pgid": 4242}},
        },
    )

    monkeypatch.setattr(stack, "pid_alive", lambda pid: False)
    monkeypatch.setattr(stack, "pgid_alive", lambda pgid: True)
    monkeypatch.setattr(stack, "identity_matches", lambda rec: False)
    monkeypatch.setattr(stack, "_stop_record", lambda record, root, remove=False: "refused")

    ret = stack.cmd_up(root=tmp_path, manifest_path=manifest_path, scope="full")

    assert ret == stack.EXIT_OP_FAILED
    assert "api" in stack.read_state(state_path)["services"]
