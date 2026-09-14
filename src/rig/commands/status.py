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
from rig.core.identity import instance_id
from rig.core.locks import exclusive_lock
from rig.core.state import read_state, write_state
from rig.manifest.models import Manifest
from rig.net.health import wait_for_http


def _format_service_health(record: Mapping[str, Any], service: Any) -> str:
    if service.healthcheck_path and record.get("port"):
        ready = wait_for_http(
            int(record["port"]),
            service.healthcheck_path,
            (1.0, record.get("pid"), record.get("pgid")),
        )
        return "  healthy" if ready else "  unhealthy"
    return ""


def _print_status(manifest: Manifest, state: Mapping[str, Any], root: Path) -> None:
    instance = instance_id(manifest.project, root)
    mode_str = f"  mode={manifest.active_mode}" if manifest.active_mode else ""
    gen = state.get("generation", 0)
    print(f"{manifest.project}  instance={instance}{mode_str}  generation={gen}")
    width = max((len(name) for name in manifest.services), default=8)
    for name in sorted(manifest.services):
        record = state.get("services", {}).get(name)
        if record is None:
            print(f"  {name.ljust(width)}  stopped")
            continue
        status = record_status(record, root)
        health = (
            _format_service_health(record, manifest.services[name]) if status == "running" else ""
        )
        pid = record.get("pid")
        pid_text = f"pid={pid}" if pid else f"container={str(record.get('container'))[:12]}"
        url_text = record.get("url") or "no port"
        print(f"  {name.ljust(width)}  {status.ljust(7)}  {pid_text}  {url_text}{health}")


def cmd_status(root: Path, manifest_path: Path, as_json: bool = False) -> int:
    ctx = _resolve_manifest_context(root, manifest_path)
    raw_m, root_path, instance, state_path, lock_path = ctx
    with exclusive_lock(lock_path):
        state = read_state(state_path)
        if prune_state(state, root_path):
            write_state(state_path, state)
        active_mode = state.get("mode")
        manifest = raw_m.for_mode(active_mode) if raw_m.modes else raw_m

        if as_json:
            services_info = {}
            for sname in sorted(manifest.services):
                rec = state.get("services", {}).get(sname)
                services_info[sname] = {
                    "running": is_service_verifiable_alive(rec, root_path) if rec else False,
                    "type": manifest.services[sname].type,
                    "port": rec.get("port") if rec else None,
                    "url": rec.get("url") if rec else None,
                    "pid": rec.get("pid") if rec else None,
                }
            print_json_envelope(
                "status",
                {
                    "project": manifest.project,
                    "instance": instance,
                    "mode": active_mode or "default",
                    "generation": state.get("generation", 0),
                    "services": services_info,
                },
            )
        else:
            _print_status(manifest, state, root_path)
    return EXIT_OK
