"""Process status inspection across all machine checkouts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rig.commands.common import compose_record_alive
from rig.core.constants import EXIT_OK, LOCK_FILE_NAME, STATE_FILE_NAME
from rig.core.errors import print_json_envelope
from rig.core.identity import get_instances_dir, is_locked
from rig.core.state import read_state
from rig.net.health import wait_for_http
from rig.proc.process import identity_matches, pid_alive

SUMMARY_MAX_LENGTH = 24


def _is_record_running(srec: dict[str, Any], root: Path | None, stype: str) -> bool:
    if stype == "compose":
        return compose_record_alive(srec, root or Path("."))
    pid = srec.get("pid")
    return isinstance(pid, int) and pid_alive(pid) and identity_matches(srec)


def _check_service_health(srec: dict[str, Any], port: Any, pid: Any) -> str:
    hp = srec.get("healthcheck_path") or srec.get("health") or "/"
    ready = wait_for_http(int(port), hp, (1.0, pid, srec.get("pgid")))
    return "healthy" if ready else "unhealthy"


def _inspect_instance_service(
    srec: dict[str, Any], root: Path | None, health: bool
) -> tuple[dict[str, Any], bool]:
    stype = srec.get("type", "unknown")
    port, pid = srec.get("port"), srec.get("pid")
    is_alive = _is_record_running(srec, root, stype)
    h_status = _check_service_health(srec, port, pid) if health and is_alive and port else None
    return {
        "type": stype,
        "status": "running" if is_alive else "stopped",
        "port": port,
        "url": srec.get("url"),
        "pid": pid,
        "health": h_status,
    }, is_alive


def _compute_instance_status(root_exists: bool, running_count: int, total: int) -> str:
    if not root_exists:
        return "orphaned"
    if total > 0 and running_count == total:
        return "running"
    return "partial" if running_count > 0 else "stopped"


def _summarize_instance(inst_dir: Path, health: bool) -> dict[str, Any] | None:
    state_file = inst_dir / STATE_FILE_NAME
    if not state_file.is_file():
        return None
    state = read_state(state_file)
    inst_id = state.get("instance") or inst_dir.name
    root_str = state.get("root")
    root_path = Path(root_str).resolve() if root_str else None
    root_exists = root_path.is_dir() if root_path else False

    services_info, running_count = {}, 0
    for sname, srec in state.get("services", {}).items():
        info, alive = _inspect_instance_service(srec, root_path, health)
        services_info[sname] = info
        if alive:
            running_count += 1

    total = len(state.get("services", {}))
    status = _compute_instance_status(root_exists, running_count, total)
    project = state.get("project") or inst_id.rsplit("-", 1)[0]
    return {
        "instance": inst_id,
        "project": project,
        "mode": state.get("mode") or "default",
        "status": status,
        "locked": is_locked(inst_dir / LOCK_FILE_NAME),
        "root": root_str,
        "root_exists": root_exists,
        "services_running": running_count,
        "services_total": total,
        "services": services_info,
    }


def _print_table(instances: list[dict[str, Any]]) -> None:
    col1 = f"{'PROJECT':<16} {'INSTANCE':<22} {'MODE':<10}"
    print(f"{col1} {'STATUS':<10} {'SERVICES':<25} {'ROOT'}")
    for it in instances:
        summary = ", ".join(f"{s}:{info['status']}" for s, info in it["services"].items()) or "none"
        if len(summary) > SUMMARY_MAX_LENGTH:
            summary = f"{it['services_running']}/{it['services_total']} up"
        root_display = (it["root"] or "n/a") + ("" if it["root_exists"] else " [deleted]")
        line = (
            f"{it['project']:<16} {it['instance']:<22} {it['mode']:<10} "
            f"{it['status']:<10} {summary:<25} {root_display}"
        )
        print(line)


def cmd_ps(health: bool = False, as_json: bool = False) -> int:
    instances_dir = get_instances_dir()
    instances_data = []
    if instances_dir.is_dir():
        for d in sorted(instances_dir.iterdir()):
            if d.is_dir() and (info := _summarize_instance(d, health)):
                instances_data.append(info)

    if as_json:
        print_json_envelope("ps", {"instances": instances_data})
        return EXIT_OK
    if not instances_data:
        print("No active or recorded rig instances found.")
        return EXIT_OK
    _print_table(instances_data)
    return EXIT_OK
