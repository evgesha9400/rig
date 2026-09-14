"""Service specification parsing and integrity validation."""

from __future__ import annotations

import math
import shlex
from collections.abc import Mapping
from typing import Any

from rig.core.constants import PORT_MAX, PORT_MIN, SERVICE_TYPES
from rig.core.errors import manifest_error
from rig.manifest.models import Service


def _validate_spec_types(name: str, spec: dict[str, Any]) -> None:
    kind = spec.get("type")
    if kind not in SERVICE_TYPES:
        raise manifest_error(
            f"service {name!r} has unknown type {kind!r}; expected one of {SERVICE_TYPES}"
        )
    for key in ("env_files", "depends_on", "aliases", "inherit"):
        if key in spec and (
            not isinstance(spec[key], list) or not all(isinstance(i, str) for i in spec[key])
        ):
            raise manifest_error(f"service {name!r} {key!r} must be a list of strings")
    if "env" in spec and (
        not isinstance(spec["env"], dict) or not all(isinstance(k, str) for k in spec["env"])
    ):
        raise manifest_error(
            f"service {name!r} 'env' must be a JSON object mapping strings to values"
        )
    if "cwd" in spec and not isinstance(spec["cwd"], str):
        raise manifest_error(f"service {name!r} 'cwd' must be a string")


def _validate_spec_health(name: str, spec: dict[str, Any]) -> None:
    if "health" in spec:
        if "healthcheck_path" in spec and spec["health"] != spec["healthcheck_path"]:
            raise manifest_error(
                f"service {name!r} defines conflicting 'health' and 'healthcheck_path'"
            )
        spec["healthcheck_path"] = spec.pop("health")
    if "healthcheck_timeout" in spec:
        t = spec["healthcheck_timeout"]
        if isinstance(t, bool) or not isinstance(t, (int, float)) or not math.isfinite(t) or t <= 0:
            raise manifest_error(
                f"service {name!r} 'healthcheck_timeout' must be a positive finite number"
            )
    if "healthcheck_path" in spec and spec["healthcheck_path"] is not None:
        hp = spec["healthcheck_path"]
        if not isinstance(hp, str) or not hp or not hp.startswith("/"):
            raise manifest_error(
                f"service {name!r} 'healthcheck_path' must be a non-empty string starting with '/'"
            )


def _parse_command_string(name: str, cmd_str: str) -> list[str]:
    clean = cmd_str.strip()
    if not clean or "\0" in clean:
        raise manifest_error(f"service {name!r} 'command' cannot be empty or contain NUL")
    try:
        tokens = shlex.split(clean, comments=False, posix=True)
    except ValueError as exc:
        raise manifest_error(f"service {name!r} invalid command syntax: {exc}") from None
    if not tokens:
        raise manifest_error(f"service {name!r} 'command' cannot be empty")
    return tokens


def _validate_spec_command(name: str, spec: dict[str, Any]) -> None:
    raw_cmd = spec.get("command")
    if isinstance(raw_cmd, str):
        spec["command"] = _parse_command_string(name, raw_cmd)
    elif isinstance(raw_cmd, list):
        if not all(isinstance(t, str) for t in raw_cmd):
            raise manifest_error(f"service {name!r} 'command' must be a list of strings")
    elif raw_cmd is not None:
        raise manifest_error(f"service {name!r} 'command' must be a string or list of strings")


def _validate_spec_port(name: str, spec: dict[str, Any]) -> None:
    if "port" in spec and "preferred_port" not in spec:
        spec["preferred_port"] = spec.pop("port")
    if "preferred_port" in spec and spec["preferred_port"] is not None:
        p = spec["preferred_port"]
        if isinstance(p, bool) or not isinstance(p, int) or not (PORT_MIN <= p <= PORT_MAX):
            raise manifest_error(
                f"service {name!r} 'preferred_port' must be an integer between 1 and {PORT_MAX}"
            )


def _parse_service(name: str, raw_spec: Any) -> Service:
    if not isinstance(raw_spec, dict):
        raise manifest_error(f"service {name!r} must be a JSON object")
    spec = dict(raw_spec)
    _validate_spec_types(name, spec)
    _validate_spec_health(name, spec)
    _validate_spec_command(name, spec)
    _validate_spec_port(name, spec)

    known = {f.name for f in Service.__dataclass_fields__.values()} - {"name"}
    unknown = set(spec) - known
    if unknown:
        raise manifest_error(f"service {name!r} has unknown keys: {sorted(unknown)}")
    return Service(name=name, **spec)


def _check_dependencies(name: str, service: Service, services: Mapping[str, Service]) -> None:
    for dependency in service.depends_on:
        if dependency not in services:
            raise manifest_error(f"service {name!r} depends on unknown service {dependency!r}")


def _validate_service_integrity(services: Mapping[str, Service]) -> None:
    for name, service in services.items():
        _check_dependencies(name, service, services)
        if service.type == "fd" and not (service.command or service.app):
            raise manifest_error(f"service {name!r} needs a 'command' or an 'app'")
        if service.type == "port" and not service.command:
            raise manifest_error(f"service {name!r} needs a 'command'")
        if service.type == "compose" and not (service.compose_file and service.compose_service):
            raise manifest_error(f"service {name!r} needs 'compose_file' and 'compose_service'")
