"""Compose argv scoping to a checkout, a port lookup, and an explicit context."""

import pytest

from rig import cli as rig

stack = rig


def test_compose_argv_scopes_every_call_to_this_checkout(tmp_path):
    argv = stack.compose_argv(
        instance="deltalytic-abcd1234",
        root=tmp_path,
        compose_file=tmp_path / "docker-compose.yml",
        args=["up", "-d"],
    )

    assert argv[:2] == ["docker", "compose"]
    assert "--project-directory" in argv
    assert argv[argv.index("--project-directory") + 1] == str(tmp_path)
    assert argv[argv.index("-p") + 1] == "deltalytic-abcd1234"
    assert argv[argv.index("-f") + 1] == str(tmp_path / "docker-compose.yml")
    assert argv[-2:] == ["up", "-d"]


def test_compose_argv_scopes_a_port_lookup_the_same_way(tmp_path):
    argv = stack.compose_argv(
        instance="deltalytic-abcd1234",
        root=tmp_path,
        compose_file=tmp_path / "docker-compose.yml",
        args=["port", "db", "5432"],
    )

    assert "-p" in argv and "-f" in argv and "--project-directory" in argv
    assert argv[-3:] == ["port", "db", "5432"]


def test_compose_argv_passes_an_explicit_docker_context(tmp_path):
    argv = stack.compose_argv(
        instance="deltalytic-abcd1234",
        root=tmp_path,
        compose_file=tmp_path / "docker-compose.yml",
        args=["ps"],
        context="colima",
    )

    assert argv[:4] == ["docker", "--context", "colima", "compose"]


def test_compose_port_output_is_parsed_into_a_host_port():
    assert stack.parse_compose_port("127.0.0.1:54321\n") == 54321
    assert stack.parse_compose_port("0.0.0.0:5432") == 5432
    assert stack.parse_compose_port("[::1]:5555") == 5555


def test_compose_port_output_without_a_mapping_is_rejected():
    with pytest.raises(stack.StackError):
        stack.parse_compose_port("\n")
