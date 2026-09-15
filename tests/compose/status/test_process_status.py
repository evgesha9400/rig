"""R9-5: process-type service status reflects whether the recorded pid is alive."""

from rig import cli as rig

stack = rig


def _status_manifest(tmp_path, service):
    return stack.Manifest(
        project="shop",
        services={service.name: service},
        scopes={"full": [service.name]},
        path=tmp_path / "rig.json",
    )


def _status_line(capsys, name: str) -> str:
    return next(
        line
        for line in capsys.readouterr().out.splitlines()
        if f" {name} " in f" {line} " and not line.strip().startswith("STATUS")
    )


def test_status_reports_a_dead_process_service_as_stopped(monkeypatch, tmp_path, capsys):
    """R9-5: a retained process record whose identity no longer matches is stopped."""
    service = stack.Service(name="api", type="port", command=["echo"])
    state = {
        "generation": 1,
        "services": {
            "api": {
                "name": "api",
                "type": "port",
                "pid": 999999,
                "port": 8000,
                "url": "http://127.0.0.1:8000",
            }
        },
    }

    stack._print_status(_status_manifest(tmp_path, service), state, tmp_path)

    line = _status_line(capsys, "api")
    assert "stopped" in line
    assert "running" not in line


def test_status_reports_a_live_process_service_as_running(monkeypatch, tmp_path, capsys):
    """R9-5: a verifiable process record is still reported as running."""
    monkeypatch.setattr(stack, "identity_matches", lambda record: True)
    service = stack.Service(name="api", type="port", command=["echo"])
    state = {
        "generation": 1,
        "services": {
            "api": {
                "name": "api",
                "type": "port",
                "pid": 4242,
                "port": 8000,
                "url": "http://127.0.0.1:8000",
            }
        },
    }

    stack._print_status(_status_manifest(tmp_path, service), state, tmp_path)

    line = _status_line(capsys, "api")
    assert "running" in line
    assert "pid:4242" in line
