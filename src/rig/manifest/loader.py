"""Manifest parsing and loading for rig."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from rig.core.errors import StackError, manifest_error
from rig.manifest.models import Manifest, Service
from rig.manifest.parser import _parse_service, _validate_service_integrity


def _load_raw_json(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(Path(path).read_text())
    except OSError:
        raise StackError(f"manifest not found: {path}") from None
    except json.JSONDecodeError as exc:
        raise manifest_error(f"manifest {path} is not valid JSON: {exc}") from None
    if not isinstance(raw, dict):
        raise manifest_error(f"manifest {path} must be a JSON object")
    if not isinstance(raw.get("project"), str) or not raw.get("project"):
        raise manifest_error(f"manifest {path} must declare a non-empty 'project'")
    return raw


def _build_mode_services(raw_modes: Any) -> dict[str, dict[str, Service]]:
    if raw_modes is None:
        return {}
    if not isinstance(raw_modes, dict):
        raise manifest_error("'modes' must be a JSON object")
    modes: dict[str, dict[str, Service]] = {}
    for mode_name, mode_obj in raw_modes.items():
        if not isinstance(mode_obj, dict) or not isinstance(mode_obj.get("services"), dict):
            raise manifest_error(f"mode {mode_name!r} must declare a 'services' object")
        modes[mode_name] = {
            sname: _parse_service(sname, spec) for sname, spec in mode_obj["services"].items()
        }
    return modes


def _register_service_aliases(
    item: tuple[str, Service], services: dict[str, Service], derived: dict[str, list[str]]
) -> None:
    sname, service = item
    derived[sname] = [sname]
    for alias in service.aliases:
        if not isinstance(alias, str) or not alias:
            raise manifest_error(f"service {sname!r} has invalid alias {alias!r}")
        if alias in services and alias != sname:
            msg = f"alias {alias!r} for service {sname!r} conflicts with another service"
            raise manifest_error(msg)
        derived[alias] = [sname]


def _add_service_aliases(derived: dict[str, list[str]], services: dict[str, Service]) -> None:
    for item in services.items():
        _register_service_aliases(item, services, derived)


def _validate_scope_members(scope: str, members: list[str], known: set[str]) -> None:
    for m in members:
        if m not in known:
            raise manifest_error(f"scope {scope!r} names unknown service {m!r}")


def _parse_explicit_scopes(
    scopes_raw: Any, initial_services: dict[str, Service]
) -> dict[str, list[str]]:
    if scopes_raw is None:
        return {}
    if not isinstance(scopes_raw, dict):
        raise manifest_error("'scopes' must be a JSON object")
    explicit: dict[str, list[str]] = {}
    known = set(initial_services)
    for scope, members in scopes_raw.items():
        if not isinstance(members, list) or not all(isinstance(m, str) for m in members):
            raise manifest_error(f"scope {scope!r} must be a list of string service names")
        _validate_scope_members(scope, members, known)
        explicit[scope] = list(members)
    return explicit


def _derive_scopes(
    initial_services: dict[str, Service], scopes_raw: Any
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    derived: dict[str, list[str]] = {
        "full": list(initial_services),
        "local": list(initial_services),
    }
    _add_service_aliases(derived, initial_services)
    explicit = _parse_explicit_scopes(scopes_raw, initial_services)
    derived.update(explicit)
    return derived, explicit


def _validate_raw_services(path: Path, declared: Any, raw_modes: Any) -> None:
    if declared is not None and not isinstance(declared, dict):
        raise manifest_error(f"manifest {path} 'services' must be a JSON object")
    if not declared and not raw_modes:
        raise manifest_error(f"manifest {path} must declare at least one service")


def _resolve_initial_mode(
    modes: dict[str, dict[str, Service]], default_mode: str | None
) -> tuple[str | None, str | None]:
    if default_mode and default_mode not in modes:
        known = list(modes.keys())
        raise manifest_error(f"default_mode {default_mode!r} not declared in modes: {known}")
    init_mode = default_mode or (next(iter(modes.keys())) if modes else None)
    return default_mode, init_mode


def load_manifest(path: Path) -> Manifest:
    """Read and validate a stack manifest."""
    path = Path(path)
    raw = _load_raw_json(path)
    declared, raw_modes = raw.get("services"), raw.get("modes")
    _validate_raw_services(path, declared, raw_modes)

    base = {name: _parse_service(name, spec) for name, spec in (declared or {}).items()}
    modes = _build_mode_services(raw_modes)
    default_mode, init_mode = _resolve_initial_mode(modes, raw.get("default_mode"))

    services = {**base, **(modes[init_mode] if init_mode else {})}
    _validate_service_integrity(services)
    scopes, explicit_scopes = _derive_scopes(services, raw.get("scopes"))

    manifest = Manifest(
        project=raw["project"],
        services=services,
        scopes=scopes,
        path=path,
        base_services=base,
        modes=modes,
        default_mode=default_mode,
        active_mode=init_mode,
        explicit_scopes=explicit_scopes,
    )
    for m_name in modes:
        manifest.for_mode(m_name)
    if not modes:
        for scope in scopes:
            manifest.resolve_scope(scope)
    return manifest
