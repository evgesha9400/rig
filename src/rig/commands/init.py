"""Auto-detection and project scaffolding command."""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

from rig.commands.up import cmd_up
from rig.core.constants import EXIT_OK, EXIT_USAGE
from rig.core.errors import RigError, print_json_envelope
from rig.manifest.detector import (
    _extract_compose_services,
    classify_compose_service,
    detect_backend,
    detect_frontend,
)

DEFAULT_COMPOSE_SERVICES = {
    "postgres": (5432, "PostgreSQL database container"),
    "redis": (6379, "Redis cache container"),
}


def _add_compose_service(base: dict[str, Any], detected: str, item: tuple[str, Any]) -> None:
    svc, blk = item
    if (kind := classify_compose_service(svc, blk)) and kind not in base:
        port, desc = DEFAULT_COMPOSE_SERVICES[kind]
        base[kind] = {
            "type": "compose",
            "compose_file": detected,
            "compose_service": svc,
            "compose_port": port,
            "description": desc,
        }


def _detect_compose(root: Path, base_services: dict[str, Any]) -> str | None:
    cands = ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")
    if not (detected := next((c for c in cands if (root / c).is_file()), None)):
        return None
    try:
        for item in _extract_compose_services((root / detected).read_text()).items():
            _add_compose_service(base_services, detected, item)
    except OSError:
        pass
    return detected


def _write_temp_manifest(target: Path, content: str, root: Path) -> None:
    tmp: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", dir=root, delete=False, prefix=".rig.json.tmp."
        ) as h:
            tmp = h.name
            h.write(content)
            h.flush()
            os.fsync(h.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, target)
    except BaseException:
        if tmp is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
        raise


def _write_manifest_file(target: Path, content: str, opts: tuple[bool, Path]) -> None:
    force, root = opts
    if force:
        _write_temp_manifest(target, content, root)
        return
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(str(target), flags, 0o644), "w") as f:
            f.write(content)
    except FileExistsError:
        msg = f"'{target}' already exists. Pass --force to overwrite."
        raise RigError(msg, code="E_USAGE", exit_code=EXIT_USAGE) from None


def _build_init_manifest(root: Path) -> dict[str, Any]:
    proj = re.sub(r"[^a-zA-Z0-9]+", "-", root.name.lower()).strip("-") or "app"
    base_services, native_services = {}, {}
    _detect_compose(root, base_services)
    has_backend = detect_backend(root, native_services, base_services)
    detect_frontend(root, native_services, has_backend)

    if not base_services and not native_services:
        native_services["web"] = {
            "type": "port",
            "cwd": ".",
            "command": [sys.executable, "-m", "http.server", "--bind", "127.0.0.1", "{port}"],
            "healthcheck_path": "/",
            "description": "Local HTTP static file server",
        }

    data: dict[str, Any] = {
        "$schema": "https://raw.githubusercontent.com/evgesha9400/rig/main/rig.schema.json",
        "project": proj,
    }
    if base_services:
        data["services"] = base_services
    if native_services:
        data["default_mode"], data["modes"] = "native", {"native": {"services": native_services}}
    return data


def _finish_init(target: Path, data: dict[str, Any], opts: tuple[bool, bool, Path]) -> int:
    up, as_json, root = opts
    if up:
        return cmd_up(root, target, as_json=as_json)
    payload = {"manifest": data, "path": str(target), "created": True}
    print_json_envelope("init", payload) if as_json else print(f"Created {target}")
    return EXIT_OK


def _unpack_init_flags(args: tuple[Any, ...], kwargs: dict[str, Any]) -> tuple[bool, ...]:
    argv = list(args)
    keys = ("dry_run", "force", "up", "as_json")
    return tuple(bool(kwargs.get(k) or (argv.pop(0) if argv else False)) for k in keys)


def cmd_init(root: Path, *args: Any, **kwargs: Any) -> int:
    dry_run, force, up, as_json = _unpack_init_flags(args, kwargs)
    resolved_root = Path(root).resolve()
    target = resolved_root / "rig.json"
    if (target.is_symlink() or target.exists()) and not (force or dry_run):
        msg = f"'{target}' already exists. Pass --force to overwrite."
        hint = "pass --force to overwrite the existing manifest"
        raise RigError(msg, code="E_USAGE", exit_code=EXIT_USAGE, hint=hint)

    data = _build_init_manifest(resolved_root)
    content = json.dumps(data, indent=2) + "\n"
    if dry_run:
        print_json_envelope("init", {"manifest": data, "dry_run": True}) if as_json else print(
            content, end=""
        )
        return EXIT_OK

    _write_manifest_file(target, content, (force, resolved_root))
    return _finish_init(target, data, (up, as_json, resolved_root))
