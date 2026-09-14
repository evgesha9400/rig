"""Compose service image detection and datastore classification heuristics."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

POSTGRES_IMAGES = ("postgres", "postgresql", "postgis", "timescaledb")
POSTGRES_NAMES = ("db", "database", "postgres", "postgresql")
REDIS_IMAGES = ("redis", "valkey")
REDIS_NAMES = ("redis", "valkey", "cache")


def _is_top_level_section(line: str) -> bool:
    return bool(re.match(r"^[a-zA-Z0-9_-]+\s*:\s*$", line) and not line.startswith(" "))


def _parse_service_header(line: str) -> str | None:
    m = re.match(r"^ {2}([a-zA-Z0-9_-]+)\s*:\s*$", line)
    return m.group(1) if m else None


def _append_service_line(
    services: dict[str, list[str]], current_svc: str | None, line: str
) -> str | None:
    svc_name = _parse_service_header(line)
    if svc_name:
        services[svc_name] = []
        return svc_name
    if current_svc and line.startswith(("   ", "\t")):
        services[current_svc].append(line)
    return current_svc


def _extract_compose_services(content: str) -> dict[str, str]:
    """Return each top-level Compose service name mapped to its own block."""
    services: dict[str, list[str]] = {}
    in_services = False
    current_svc = None

    for line in content.splitlines():
        trimmed = line.strip()
        if not trimmed or trimmed.startswith("#"):
            continue
        if re.match(r"^services\s*:\s*$", line):
            in_services, current_svc = True, None
            continue
        if in_services and _is_top_level_section(line):
            in_services, current_svc = False, None
            continue
        if in_services:
            current_svc = _append_service_line(services, current_svc, line)

    return {k: "\n".join(v).lower() for k, v in services.items()}


def _compose_service_image(block: str) -> str | None:
    """Return the image name declared in one Compose service block."""
    match = re.search(r"^\s{2,}image\s*:\s*[\"']?([^\"'\s#]+)", block, re.MULTILINE)
    if not match:
        return None
    reference = match.group(1)
    return reference.rsplit("/", 1)[-1].split(":", 1)[0]


def classify_compose_service(name: str, block: str) -> str | None:
    """Return 'postgres', 'redis' or None for one Compose service."""
    image = _compose_service_image(block)
    if image is not None:
        return (
            "postgres" if image in POSTGRES_IMAGES else ("redis" if image in REDIS_IMAGES else None)
        )
    if name.lower() in POSTGRES_NAMES:
        return "postgres"
    if name.lower() in REDIS_NAMES:
        return "redis"
    return None


def _find_fastapi_app(root: Path) -> str:
    if (root / "app" / "main.py").is_file():
        return "app.main:app"
    return "src.main:app" if (root / "src" / "main.py").is_file() else "main:app"


def detect_backend(
    root: Path, native_services: dict[str, Any], base_services: dict[str, Any]
) -> bool:
    if (root / "manage.py").is_file():
        native_services["backend"] = {
            "type": "port",
            "cwd": ".",
            "command": ["python", "manage.py", "runserver", "127.0.0.1:{port}"],
            "healthcheck_path": "/",
            "description": "Django web application",
        }
        return True
    if (root / "pyproject.toml").is_file() or (root / "requirements.txt").is_file():
        spec: dict[str, Any] = {
            "type": "fd",
            "cwd": ".",
            "python": sys.executable,
            "app": _find_fastapi_app(root),
            "healthcheck_path": "/healthz",
            "description": "FastAPI / ASGI backend application",
        }
        if "postgres" in base_services:
            spec["depends_on"] = ["postgres"]
        native_services["backend"] = spec
        return True
    return False


def detect_frontend(root: Path, native_services: dict[str, Any], has_backend: bool) -> None:
    if not (root / "package.json").is_file():
        return
    pm = "npm"
    if (root / "pnpm-lock.yaml").is_file():
        pm = "pnpm"
    elif (root / "yarn.lock").is_file():
        pm = "yarn"
    elif (root / "bun.lockb").is_file():
        pm = "bun"
    spec: dict[str, Any] = {
        "type": "port",
        "cwd": ".",
        "command": [pm, "run", "dev", "--", "--port", "{port}"],
        "healthcheck_path": "/",
        "description": "Frontend development server",
    }
    if has_backend:
        spec["depends_on"] = ["backend"]
    native_services["frontend"] = spec
