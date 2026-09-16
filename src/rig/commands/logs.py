"""Process log inspection and tailing command."""

from __future__ import annotations

import collections
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from rig.commands.common import _resolve_manifest_context, record_status
from rig.compose.client import run_compose
from rig.core.constants import EXIT_NOT_FOUND, EXIT_OK
from rig.core.errors import RigError, print_json_envelope
from rig.core.identity import get_instances_dir
from rig.core.state import read_state
from rig.core.terminal import get_theme, highlight_log_line
from rig.manifest.models import Service


def _tail_log_file(path: Path, n: int) -> list[str]:
    if not path.is_file():
        return []
    with open(path, encoding="utf-8", errors="replace") as f:
        return list(collections.deque(f, maxlen=n))


def _fetch_compose_logs(
    target: tuple[Mapping[str, Any], Service],
    env_ctx: tuple[Path, str, int],
) -> list[str]:
    rec, svc = target
    root, inst, n = env_ctx
    cfile = Path(rec.get("compose_file") or (root / str(svc.compose_file)))
    c_svc = str(rec.get("compose_service") or svc.compose_service)
    cmd = ["logs", f"--tail={n}", c_svc]
    res = run_compose(inst, root, cfile, cmd, context=rec.get("docker_context"))
    return res.stdout.splitlines(keepends=True) if res.stdout else []


def _resolve_target_service(service_name: str | None, manifest_services: Mapping[str, Any]) -> str:
    if service_name and service_name in manifest_services:
        return service_name
    avail = ", ".join(sorted(manifest_services.keys())) or "none"
    if not service_name:
        if len(manifest_services) == 1:
            return next(iter(manifest_services.keys()))
        raise RigError(
            f"service name is required when multiple services exist (available: {avail})",
            code="E_USAGE",
            hint=f"Specify one of: {avail}",
        )
    raise RigError(
        f"service '{service_name}' not found in manifest",
        code="E_SERVICE_NOT_FOUND",
        hint=f"Available services: {avail}",
    )


def _print_logs_formatted(
    target_info: tuple[str, str, str | None, str],
    path_display: str,
    lines: list[str],
) -> None:
    th = get_theme()
    project, service, mode, st = target_info
    st_badge = f"{th.green}● running{th.r}" if st == "running" else f"{th.red}○ {st}{th.r}"
    mode_tag = f" {th.cyan}[{mode}]{th.r}" if mode else ""
    hdr_left = f"  {th.b}{project}{th.r}{mode_tag}  {th.d}·{th.r}  {th.b}{service}{th.r}"
    print(f"\n{hdr_left}  {th.d}·{th.r}  {st_badge}  {th.d}(last {len(lines)} lines){th.r}")
    print(f"  {th.d}➜{th.r} {path_display}\n")
    for raw in lines:
        cleaned = raw.rstrip("\r\n")
        print(f"  {th.cyan}│{th.r} {highlight_log_line(cleaned, th)}")
    print()


def _render_logs(
    target_info: tuple[str, str, str | None, str],
    path_display: str,
    ctx: tuple[list[str], bool],
) -> int:
    lines, as_json = ctx
    project, service, mode, st = target_info
    if as_json:
        data = {
            "project": project,
            "service": service,
            "mode": mode,
            "status": st,
            "path": path_display,
            "count": len(lines),
            "lines": [line.rstrip("\r\n") for line in lines],
        }
        print_json_envelope("logs", data)
        return EXIT_OK
    th = get_theme()
    if not th.r:
        for line in lines:
            sys.stdout.write(line if line.endswith("\n") else line + "\n")
        return EXIT_OK
    _print_logs_formatted(target_info, path_display, lines)
    return EXIT_OK


def _read_service_log(
    service_def: Service,
    ctx: tuple[Mapping[str, Any], Path, str],
    tail: int,
) -> tuple[list[str], str]:
    rec, root, inst = ctx
    if service_def.type == "compose":
        lines = _fetch_compose_logs((rec, service_def), (root, inst, tail))
        svc_target = rec.get("compose_service") or service_def.compose_service
        return lines, f"docker://{svc_target!s}"
    inst_dir = get_instances_dir() / inst
    log_file = Path(rec.get("log") or (inst_dir / "logs" / f"{service_def.name}.log"))
    if not log_file.is_file():
        raise RigError(
            f"no log file found for service '{service_def.name}' at {log_file}",
            code="E_LOG_NOT_FOUND",
            exit_code=EXIT_NOT_FOUND,
            hint=f"Run 'rig up {service_def.name}' first to start the service",
        )
    return _tail_log_file(log_file, tail), f"file://{log_file.resolve()}"


def cmd_logs(root: Path, manifest_path: Path, *args: Any, **kwargs: Any) -> int:
    """Inspect and tail logs for a single service in the current rig checkout."""
    argv = list(args)
    service = kwargs.get("service") or (argv.pop(0) if argv else None)
    tail = int(kwargs.get("tail") or (argv.pop(0) if argv else 50))
    mode = kwargs.get("mode") or (argv.pop(0) if argv else None)
    as_json = bool(kwargs.get("as_json") or (argv.pop(0) if argv else False))

    mf, res_root, inst_id, st_path, _ = _resolve_manifest_context(root, manifest_path)
    if mode:
        mf = mf.with_mode(mode)
    sname = _resolve_target_service(service, mf.services)
    state = read_state(st_path) if st_path.is_file() else {}
    srec = state.get("services", {}).get(sname, {})
    svc_def = mf.services[sname]
    st = record_status(srec, res_root) if srec else "stopped"

    lines, path_display = _read_service_log(svc_def, (srec, res_root, inst_id), tail)
    return _render_logs((mf.project, sname, mf.active_mode, st), path_display, (lines, as_json))
