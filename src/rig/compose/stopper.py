"""Compose service stop and teardown."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from rig.compose import client as cclient
from rig.compose import context as cctx
from rig.compose.docker import docker_record_stop
from rig.compose.supervisor import compose_record_status
from rig.core.errors import RigError, StackError


def _run_compose_teardown(
    target: tuple[str, Path, Path],
    args: list[str],
    endpoint: tuple[str | None, Any, Any],
) -> bool:
    instance, root, compose_file = target
    context, docker_host, compose_env = endpoint
    try:
        res = cclient.run_compose(
            instance,
            root,
            compose_file,
            args,
            context,
            env=compose_env,
            docker_host=docker_host,
        )
    except (StackError, RigError):
        return False
    else:
        return res.returncode == 0


def _teardown_services(
    target: tuple[str, Path, Path, str],
    endpoint: tuple[str | None, Any, Any],
    remove: bool,
) -> bool:
    instance, root, compose_file, compose_service = target
    cmds = [["stop", compose_service]]
    if remove:
        cmds.append(["rm", "-f", compose_service])
    tup = (instance, root, compose_file)
    return all(_run_compose_teardown(tup, cmd, endpoint) for cmd in cmds)


def compose_stop_record(record: Mapping[str, Any], root: Path, remove: bool = True) -> str:
    status = compose_record_status(record, root)
    if status in ("absent", "error"):
        return "stale" if status == "absent" else "failed"
    if not cclient.compose_file_present(record, root):
        return docker_record_stop(record, remove)

    target = (
        str(record["instance"]),
        Path(root),
        Path(str(record["compose_file"])),
        str(record["compose_service"]),
    )
    context, docker_host = cctx.record_docker_endpoint(record)
    endpoint = (context, docker_host, cctx.record_compose_env(record))
    should_remove = remove or not record.get("container")

    if _teardown_services(target, endpoint, should_remove):
        return "terminated"
    return docker_record_stop(record, remove)
