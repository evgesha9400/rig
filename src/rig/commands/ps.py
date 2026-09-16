"""Process status inspection across all machine checkouts."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Any

from rig.commands.common import compose_record_alive
from rig.core.constants import EXIT_OK, LOCK_FILE_NAME, STATE_FILE_NAME
from rig.core.errors import print_json_envelope
from rig.core.identity import get_instances_dir, is_locked
from rig.core.state import read_state
from rig.core.terminal import contract_path, format_table, get_theme, middle_truncate, visible_width
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
    return "healthy" if wait_for_http(int(port), hp, (1.0, pid, srec.get("pgid"))) else "unhealthy"


def _inspect_instance_service(
    srec: dict[str, Any], root: Path | None, health: bool
) -> tuple[dict[str, Any], bool]:
    stype, port, pid = srec.get("type", "unknown"), srec.get("port"), srec.get("pid")
    alive = _is_record_running(srec, root, stype)
    h_status = _check_service_health(srec, port, pid) if health and alive and port else None
    return {
        "type": stype,
        "status": "running" if alive else "stopped",
        "port": port,
        "url": srec.get("url"),
        "pid": pid,
        "health": h_status,
    }, alive


def _summarize_instance(inst_dir: Path, health: bool) -> dict[str, Any] | None:
    if not (state_file := inst_dir / STATE_FILE_NAME).is_file():
        return None
    state = read_state(state_file)
    inst_id, root_str = state.get("instance") or inst_dir.name, state.get("root")
    root_path = Path(root_str).resolve() if root_str else None
    root_exists = root_path.is_dir() if root_path else False
    svcs = state.get("services", {})
    inspected = {n: _inspect_instance_service(r, root_path, health) for n, r in svcs.items()}
    running = sum(bool(alive) for _, alive in inspected.values())
    if not root_exists:
        status = "orphaned"
    elif svcs and running == len(svcs):
        status = "running"
    else:
        status = "partial" if running > 0 else "stopped"
    return {
        "instance": inst_id,
        "project": state.get("project") or inst_id.rsplit("-", 1)[0],
        "mode": state.get("mode") or "default",
        "status": status,
        "locked": is_locked(inst_dir / LOCK_FILE_NAME),
        "root": root_str,
        "root_exists": root_exists,
        "services_running": running,
        "services_total": len(svcs),
        "services": {n: info for n, (info, _) in inspected.items()},
    }


def _ps_badge(st: str, th: Any) -> str:
    badges = {"running": (th.green, "●"), "partial": (th.yellow, "▲"), "stopped": (th.red, "○")}
    col, gly = badges.get(st, (th.magenta, "✖"))
    return f"{col}{gly} {st}{th.r}"


def _format_ps_prefix(it: dict[str, Any], th: Any, wide: bool) -> list[str]:
    summary = ", ".join(f"{s}:{info['status']}" for s, info in it["services"].items()) or "none"
    if len(summary) > SUMMARY_MAX_LENGTH:
        summary = f"{it['services_running']}/{it['services_total']} up"
    inst, proj = it["instance"], it["project"]
    short_id = inst if wide or not inst.startswith(f"{proj}-") else inst[len(proj) + 1 :]
    return [
        f"{th.b}{proj}{th.r}",
        f"{th.d}{short_id}{th.r}",
        f"{th.cyan}{it['mode']}{th.r}",
        _ps_badge(it["status"], th),
        summary,
    ]


def _format_ps_root(it: dict[str, Any], avail: int | None) -> str:
    r_str = contract_path(it["root"] or "n/a")
    suffix = "" if it["root_exists"] else " [deleted]"
    if avail is None:
        return r_str + suffix
    budget = max(8, avail - len(suffix)) if suffix else avail
    return middle_truncate(r_str, budget) + suffix


def _build_ps_rows(instances: list[dict[str, Any]], wide: bool) -> list[list[str]]:
    th = get_theme()
    pre = [_format_ps_prefix(it, th, wide) for it in instances]
    avail = None
    if not wide and sys.stdout.isatty():
        all_p = [["PROJECT", "ID", "MODE", "STATUS", "SERVICES"], *pre]
        p_w = 2 + sum(max(visible_width(r[i]) for r in all_p) for i in range(5)) + 10
        avail = max(15, shutil.get_terminal_size((80, 24)).columns - p_w)
    return [
        [*p, f"{th.d}{_format_ps_root(it, avail)}{th.r}"]
        for it, p in zip(instances, pre, strict=True)
    ]


def _print_table(instances: list[dict[str, Any]], wide: bool = False) -> None:
    headers = ["PROJECT", "INSTANCE" if wide else "ID", "MODE", "STATUS", "SERVICES", "ROOT"]
    for line in format_table(headers, _build_ps_rows(instances, wide), gutter=4 if wide else 2):
        print(f"  {line}")
    print()


def _collect_instances(instances_dir: Path, health: bool) -> list[dict[str, Any]]:
    if not instances_dir.is_dir():
        return []
    return [
        i
        for d in sorted(instances_dir.iterdir())
        if d.is_dir() and (i := _summarize_instance(d, health))
    ]


def cmd_ps(health: bool = False, as_json: bool = False, wide: bool = False) -> int:
    instances_data = _collect_instances(get_instances_dir(), health)
    if as_json:
        print_json_envelope("ps", {"instances": instances_data})
    elif not instances_data:
        print("No active or recorded rig instances found.")
    else:
        _print_table(instances_data, wide=wide)
    return EXIT_OK
