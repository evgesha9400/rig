"""Status inspection and formatting command."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from rig.commands.common import (
    _resolve_manifest_context,
    is_service_verifiable_alive,
    prune_state,
    record_status,
)
from rig.core.constants import EXIT_OK
from rig.core.errors import print_json_envelope
from rig.core.identity import get_instances_dir
from rig.core.locks import exclusive_lock
from rig.core.state import read_state, write_state
from rig.core.terminal import Theme, format_table, get_theme
from rig.manifest.models import Manifest
from rig.net.health import wait_for_http


def _format_service_health(record: Mapping[str, Any], service: Any) -> str:
    if not (service.healthcheck_path and record.get("port")):
        return "-"
    ctx = (1.0, record.get("pid"), record.get("pgid"))
    ready = wait_for_http(int(record["port"]), service.healthcheck_path, ctx)
    return "healthy" if ready else "unhealthy"


def _process_ident(record: Mapping[str, Any] | None) -> str:
    if pid := (record or {}).get("pid"):
        return f"pid:{pid}"
    return f"container:{str(c)[:12]}" if (c := (record or {}).get("container")) else "-"


def _status_badge(st: str, h: str, th: Theme) -> str:
    if st == "running":
        return f"{th.yellow}▲ degraded{th.r}" if h == "unhealthy" else f"{th.green}● running{th.r}"
    return f"{th.red}{'✖ error' if st == 'error' else '○ stopped'}{th.r}"


def _health_badge(raw_h: str, th: Theme) -> str:
    tags = {"healthy": f"{th.green}✓ healthy{th.r}", "unhealthy": f"{th.red}✖ failing{th.r}"}
    return tags.get(raw_h, f"{th.d}-{th.r}")


def _build_service_row(
    name: str, ctx: tuple[Mapping[str, Any] | None, Any, Path]
) -> tuple[str, str, list[str]]:
    record, service, root = ctx
    th = get_theme()
    if record is None:
        d = f"{th.d}-{th.r}"
        return "stopped", "-", [f"{th.red}○ stopped{th.r}", f"{th.b}{name}{th.r}", d, d, d]
    raw_st = record_status(record, root)
    raw_h = _format_service_health(record, service) if raw_st == "running" else "-"
    ep = f"{th.cyan}{u}{th.r}" if (u := record.get("url")) else f"{th.d}-{th.r}"
    row = [
        _status_badge(raw_st, raw_h, th),
        f"{th.b}{name}{th.r}",
        ep,
        _health_badge(raw_h, th),
        f"{th.d}{_process_ident(record)}{th.r}",
    ]
    return raw_st, raw_h, row


def _collect_status_rows(
    manifest: Manifest, state: Mapping[str, Any], root: Path
) -> tuple[list[list[str]], list[str], int]:
    rows, degraded, running = [], [], 0
    for name in sorted(manifest.services):
        rec = state.get("services", {}).get(name)
        st_val, hlth_val, row = _build_service_row(name, (rec, manifest.services[name], root))
        running += int(st_val == "running")
        if hlth_val == "unhealthy" or st_val == "error":
            degraded.append(name)
        rows.append(row)
    return rows, degraded, running


def _print_degraded(degraded: list[str], inst_id: str | None, th: Theme) -> None:
    if not (degraded and inst_id):
        return
    inst_dir = get_instances_dir() / inst_id
    count = len(degraded)
    label = "service degraded" if count == 1 else "services degraded"
    print(f"  {th.yellow}▲ {count} {label}. View logs:{th.r}")
    for sname in degraded:
        print(f"    {th.d}{sname} ➜{th.r} {inst_dir / 'logs' / f'{sname}.log'}")
    print()


def _print_status(manifest: Manifest, state: Mapping[str, Any], root: Path) -> None:
    th, headers = get_theme(), ["STATUS", "SERVICE", "ENDPOINT", "HEALTH", "PROCESS"]
    rows, degraded, running = _collect_status_rows(manifest, state, root)
    total = len(manifest.services)
    mode_str = f" {th.cyan}[{manifest.active_mode}]{th.r}" if manifest.active_mode else ""
    stopped_str = f" {th.red}({total - running} stopped){th.r}" if running != total else ""
    col = th.green if running == total and total > 0 else th.yellow
    badge = f"{col}{running}/{total} running{th.r}{stopped_str}"
    print(f"\n  {th.b}{manifest.project}{th.r}{mode_str}  {th.d}·{th.r}  {badge}\n")
    for line in format_table(headers, rows, gutter=4):
        print(f"  {line}")
    print()
    _print_degraded(degraded, state.get("instance"), th)


def _print_status_json(
    manifest: Manifest, state: Mapping[str, Any], ctx: tuple[Path, str, str | None]
) -> None:
    root, instance, mode = ctx
    info = {
        s: {
            "running": bool(r and is_service_verifiable_alive(r, root)),
            "type": manifest.services[s].type,
            "port": (r or {}).get("port"),
            "url": (r or {}).get("url"),
            "pid": (r or {}).get("pid"),
        }
        for s in sorted(manifest.services)
        for r in [state.get("services", {}).get(s)]
    }
    payload = {
        "project": manifest.project,
        "instance": instance,
        "mode": mode or "default",
        "generation": state.get("generation", 0),
        "services": info,
    }
    print_json_envelope("status", payload)


def cmd_status(root: Path, manifest_path: Path, as_json: bool = False) -> int:
    raw_m, root_p, inst, s_path, l_path = _resolve_manifest_context(root, manifest_path)
    with exclusive_lock(l_path):
        state = read_state(s_path)
        if prune_state(state, root_p):
            write_state(s_path, state)
        active_mode = state.get("mode")
        manifest = raw_m.for_mode(active_mode) if raw_m.modes else raw_m
        if as_json:
            _print_status_json(manifest, state, (root_p, inst, active_mode))
        else:
            _print_status(manifest, state, root_p)
    return EXIT_OK
