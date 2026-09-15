"""cmd_check, cmd_init, cmd_schema, and the --json envelope in main()."""

import json

import pytest

from rig import cli as rig

stack = rig


def test_cmd_check(tmp_path, capsys):
    manifest_path = tmp_path / "rig.json"
    manifest_path.write_text(
        json.dumps(
            {
                "project": "check-test",
                "services": {"valid_svc": {"type": "port", "cwd": ".", "command": ["echo"]}},
            }
        )
    )

    ret_ok = stack.cmd_check(tmp_path, manifest_path)
    assert ret_ok == stack.EXIT_OK

    bad_manifest = tmp_path / "bad_rig.json"
    bad_manifest.write_text(
        json.dumps(
            {
                "project": "bad-test",
                "services": {
                    "bad_cwd": {"type": "port", "cwd": "nonexistent_dir", "command": ["echo"]}
                },
            }
        )
    )
    ret_fail = stack.cmd_check(tmp_path, bad_manifest)
    assert ret_fail == stack.EXIT_USAGE
    err = capsys.readouterr().err
    assert str((tmp_path / "nonexistent_dir").resolve()) in err


def test_cmd_init_fastapi_and_package_json(tmp_path):
    proj_dir = tmp_path / "sample_app"
    proj_dir.mkdir()
    (proj_dir / "pyproject.toml").write_text(
        "[project]\nname = 'sample_app'\ndependencies = ['fastapi', 'uvicorn']\n"
    )
    (proj_dir / "package.json").write_text('{"name": "frontend", "scripts": {"dev": "vite"}}\n')

    ret = stack.cmd_init(proj_dir, dry_run=False)
    assert ret == 0
    manifest_file = proj_dir / "rig.json"
    assert manifest_file.is_file()
    data = json.loads(manifest_file.read_text())
    assert data["project"] == "sample-app"
    assert "native" in data["modes"]
    services = data["modes"]["native"]["services"]
    assert "backend" in services
    assert "frontend" in services
    assert services["frontend"]["depends_on"] == ["backend"]

    with pytest.raises(stack.RigError) as exc_info:
        stack.cmd_init(proj_dir)
    assert exc_info.value.code == "E_USAGE"


def test_cmd_schema(capsys):
    ret = stack.cmd_schema()
    assert ret == 0
    captured = capsys.readouterr()
    schema = json.loads(captured.out)
    assert schema["title"] == "RigManifest"
    assert "modes" in schema["properties"]
    assert "services" in schema["properties"]


def test_main_json_envelope_success_and_error(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("RIG_STATE_HOME", str(tmp_path / "state"))

    code = stack.main(["schema", "--json"])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["schema"] == "rig.schema/1"
    assert out["ok"] is True

    code_err = stack.main(["down", "nonexistent_target", "--json"])
    assert code_err == stack.EXIT_NOT_FOUND
    err_out = json.loads(capsys.readouterr().out)
    assert err_out["schema"] == "rig.error/1"
    assert err_out["ok"] is False
    assert err_out["error"]["code"] == "E_NOT_FOUND"
    assert err_out["error"]["exit_code"] == stack.EXIT_NOT_FOUND


def test_main_argument_parsing_error_emits_json_envelope(capsys):
    code = stack.main(["--json"])
    assert code == stack.EXIT_USAGE
    err = json.loads(capsys.readouterr().out)
    assert err["schema"] == "rig.error/1"
    assert err["ok"] is False
    assert err["error"]["code"] == "E_USAGE"
    assert err["error"]["exit_code"] == stack.EXIT_USAGE

    code2 = stack.main(["status", "--invalid-flag", "--json"])
    assert code2 == stack.EXIT_USAGE
    err2 = json.loads(capsys.readouterr().out)
    assert err2["schema"] == "rig.error/1"
    assert err2["error"]["code"] == "E_USAGE"


def test_cmd_init_dangling_symlink(tmp_path):
    manifest_link = tmp_path / "rig.json"
    manifest_link.symlink_to(tmp_path / "nonexistent.json")

    with pytest.raises(stack.RigError) as exc_info:
        stack.cmd_init(tmp_path, dry_run=False, force=False)
    assert exc_info.value.code == "E_USAGE"
