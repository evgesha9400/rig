"""cmd_up: an exited compose dependency is recovered, not rejected as unhealthy."""

import json

from rig import cli as rig

stack = rig


def test_cmd_up_recovers_exited_compose_dependency(monkeypatch, tmp_path, capsys):
    """R5-2: an exited compose dependency must be restarted, not rejected as unhealthy."""
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

    assert ret == stack.EXIT_OK
    assert ("db", True) in stop_calls
    assert started == ["db", "api"]
    assert set(stack.read_state(state_path)["services"]) == {"db", "api"}
