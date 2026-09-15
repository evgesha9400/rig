"""Static project and manifest verification command."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Any

from rig.core.constants import EXIT_OK, EXIT_USAGE
from rig.core.env import render
from rig.core.errors import RigError, print_json_envelope
from rig.core.terminal import get_theme
from rig.manifest.inspect import _resolve_executable
from rig.manifest.loader import load_manifest

MANIFEST_EXCEPTIONS = (RigError, OSError, ValueError, KeyError)


def _check_binary(
    spec: str, cwd: Path, ctx: tuple[dict[str, Any], str, list[dict[str, Any]]]
) -> None:
    render_vals, tag, issues = ctx
    bin_str = str(render(spec, render_vals))
    actual = _resolve_executable(bin_str, cwd)
    if actual is None:
        msg = f"executable '{spec}' not found"
        issues.append({"level": "error", "check": f"{tag}:binary", "message": msg})
    elif not os.access(actual, os.X_OK):
        msg = f"file '{spec}' is not executable"
        issues.append({"level": "error", "check": f"{tag}:binary", "message": msg})


def _check_proc_service(
    svc: Any, info: tuple[Path, Path, str], issues: list[dict[str, Any]]
) -> None:
    root, cwd, tag = info
    if not shutil.which("lsof"):
        msg = "'lsof' not found on PATH"
        issues.append({"level": "error", "check": f"{tag}:lsof", "message": msg})
    vals = {"root": str(root), "cwd": str(cwd), "python": sys.executable}
    bin_ctx = (vals, tag, issues)
    if svc.command:
        _check_binary(svc.command[0], cwd, bin_ctx)
    elif svc.type == "fd" and svc.python:
        _check_binary(svc.python, cwd, bin_ctx)


def _check_compose_service(svc: Any, info: tuple[Path, str], issues: list[dict[str, Any]]) -> None:
    root, tag = info
    if not shutil.which("docker"):
        msg = "'docker' not found on PATH"
        issues.append({"level": "error", "check": f"{tag}:docker", "message": msg})
    if svc.compose_file and not (p := (root / svc.compose_file).resolve()).is_file():
        msg = f"compose file '{p}' does not exist"
        issues.append({"level": "error", "check": f"{tag}:compose_file", "message": msg})


def _check_service(svc: Any, root: Path, ctx: tuple[str, str, list[dict[str, Any]]]) -> None:
    sname, mode_tag, issues = ctx
    cwd = (root / svc.cwd).resolve()
    tag = f"{mode_tag} service:{sname}".strip()
    if not cwd.is_dir():
        msg = f"working directory '{cwd}' does not exist"
        issues.append({"level": "error", "check": f"{tag}:cwd", "message": msg})
    if svc.type in ("fd", "port"):
        _check_proc_service(svc, (root, cwd, tag), issues)
    elif svc.type == "compose":
        _check_compose_service(svc, (root, tag), issues)


def _check_modes(
    manifest: Any, modes: list[str | None], ctx: tuple[Path, list[dict[str, Any]]]
) -> None:
    root, issues = ctx
    for m in modes:
        try:
            m_manifest = manifest.for_mode(m)
        except MANIFEST_EXCEPTIONS as exc:
            issues.append({"level": "error", "check": f"mode:{m}", "message": str(exc)})
            continue
        tag = f"[{m}]" if m else ""
        for sname, svc in m_manifest.services.items():
            _check_service(svc, root, (sname, tag, issues))


def _report_check_results(
    manifest: Any, issues: list[dict[str, Any]], opts: tuple[int, bool, Path]
) -> int:
    mode_count, as_json, manifest_path = opts
    has_errors = any(i["level"] == "error" for i in issues)
    if as_json:
        data = {"ok": not has_errors, "project": manifest.project, "issues": issues}
        print_json_envelope("check", data)
        return EXIT_USAGE if has_errors else EXIT_OK
    th = get_theme()
    for i in issues:
        prefix = f"{th.red}✖ FAIL{th.r}" if i["level"] == "error" else f"{th.yellow}▲ WARN{th.r}"
        print(f"{prefix} {i['check']}: {i['message']}", file=sys.stderr)
    if not issues:
        msg = f"manifest '{manifest_path}' is valid for {mode_count} mode(s)."
        print(f"{th.green}✓ OK{th.r} check passed: {msg}")
    return EXIT_USAGE if has_errors else EXIT_OK


def cmd_check(root: Path, manifest_path: Path, *args: Any, **kwargs: Any) -> int:
    argv = list(args)
    mode = kwargs.get("mode") or (argv.pop(0) if argv else None)
    as_json = bool(kwargs.get("as_json") or (argv.pop(0) if argv else False))
    issues: list[dict[str, Any]] = []
    resolved_root = Path(root).resolve()
    try:
        manifest = load_manifest(manifest_path)
    except MANIFEST_EXCEPTIONS as exc:
        if as_json:
            issue = {"level": "error", "check": "manifest", "message": str(exc)}
            print_json_envelope("check", {"ok": False, "issues": [issue]})
        else:
            print(f"FAIL manifest: {exc}", file=sys.stderr)
        return EXIT_USAGE

    modes = [mode] if mode else (list(manifest.modes.keys()) if manifest.modes else [None])
    _check_modes(manifest, modes, (resolved_root, issues))
    return _report_check_results(manifest, issues, (len(modes), as_json, manifest_path))
