"""Service env: hostile variable drop, executable path, declared env files."""

import pytest

from rig import cli as rig

stack = rig


def test_service_env_drops_hostile_ambient_variables(monkeypatch, tmp_path):
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "someone-else")
    monkeypatch.setenv("DOCKER_HOST", "tcp://evil:2375")
    monkeypatch.setenv("VITE_API_URL", "http://evil")
    monkeypatch.setenv("HTTP_PROXY", "http://evil:8080")
    monkeypatch.setenv("http_proxy", "http://evil:8080")
    monkeypatch.setenv("DATABASE_URL", "postgres://evil/db")

    env = stack.build_service_env({}, inherit=[], root=tmp_path, values={})

    for hostile in (
        "COMPOSE_PROJECT_NAME",
        "DOCKER_HOST",
        "VITE_API_URL",
        "HTTP_PROXY",
        "http_proxy",
        "DATABASE_URL",
    ):
        assert hostile not in env


def test_service_env_keeps_the_executable_search_path(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    env = stack.build_service_env({}, inherit=[], root=tmp_path, values={})

    assert env["PATH"] == "/usr/bin:/bin"


def test_service_env_inherits_only_explicitly_named_variables(monkeypatch, tmp_path):
    monkeypatch.setenv("WANTED", "yes")
    monkeypatch.setenv("UNWANTED", "no")

    env = stack.build_service_env({}, inherit=["WANTED"], root=tmp_path, values={})

    assert env["WANTED"] == "yes"
    assert "UNWANTED" not in env


def test_service_env_applies_orchestrator_values_last(monkeypatch, tmp_path):
    monkeypatch.setenv("BACKEND_PORT", "9999")

    env = stack.build_service_env(
        {"BACKEND_PORT": "{backend_port}"},
        inherit=["BACKEND_PORT"],
        root=tmp_path,
        values={"backend_port": 5123},
    )

    assert env["BACKEND_PORT"] == "5123"


def test_service_env_resolves_path_placeholders_against_the_project_root(tmp_path):
    env = stack.build_service_env(
        {"DATABASE_URL": "sqlite:///{root}/data/deltalytic.db"},
        inherit=[],
        root=tmp_path,
        values={},
    )

    assert env["DATABASE_URL"] == f"sqlite:///{tmp_path}/data/deltalytic.db"


def test_service_env_rejects_an_unknown_placeholder(tmp_path):
    with pytest.raises(stack.StackError):
        stack.build_service_env({"X": "{no_such_value}"}, inherit=[], root=tmp_path, values={})


def test_service_env_reads_a_declared_env_file_relative_to_the_project_root(tmp_path):
    (tmp_path / ".env").write_text("PLATFORM_TOKEN=from-file\n# comment\nEMPTY=\n")

    env = stack.build_service_env({}, inherit=[], root=tmp_path, values={}, env_files=[".env"])

    assert env["PLATFORM_TOKEN"] == "from-file"
    assert env["EMPTY"] == ""


def test_service_env_lets_manifest_values_override_the_env_file(tmp_path):
    (tmp_path / ".env").write_text("PLATFORM_TOKEN=from-file\n")

    env = stack.build_service_env(
        {"PLATFORM_TOKEN": "from-manifest"},
        inherit=[],
        root=tmp_path,
        values={},
        env_files=[".env"],
    )

    assert env["PLATFORM_TOKEN"] == "from-manifest"
