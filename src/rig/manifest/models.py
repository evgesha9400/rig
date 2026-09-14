"""Data models for rig service and manifest declarations."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rig.core.constants import HEALTH_TIMEOUT_SECS
from rig.core.errors import manifest_error


@dataclass
class Service:
    name: str
    type: str
    cwd: str = "."
    command: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    app: str | None = None
    factory: bool = False
    python: str | None = None
    env: dict[str, Any] = field(default_factory=dict)
    inherit: list[str] = field(default_factory=list)
    env_files: list[str] = field(default_factory=list)
    healthcheck_path: str | None = None
    healthcheck_timeout: float = HEALTH_TIMEOUT_SECS
    depends_on: list[str] = field(default_factory=list)
    compose_file: str | None = None
    compose_service: str | None = None
    compose_port: int | None = None
    docker_context: str | None = None
    description: str = ""
    preferred_port: int | None = None


@dataclass
class Manifest:
    project: str
    services: dict[str, Service]
    scopes: dict[str, list[str]]
    path: Path
    base_services: dict[str, Service] = field(default_factory=dict)
    modes: dict[str, dict[str, Service]] = field(default_factory=dict)
    default_mode: str | None = None
    active_mode: str | None = None
    explicit_scopes: dict[str, list[str]] = field(default_factory=dict)

    def resolve_services(self, names: Iterable[str]) -> list[str]:
        """Return the given services plus their transitive dependencies, in start order."""
        ordered: list[str] = []
        for name in names:
            self._visit(name, ordered, set())
        return ordered

    def resolve_scope(self, scope: str) -> list[str]:
        """Return the scope's services plus their transitive dependencies, in start order."""
        return self.resolve_services(self._members(scope))

    def teardown_scope(self, scope: str) -> list[str]:
        """Return only the scope's declared services, in reverse start order."""
        declared = set(self._members(scope))
        return [name for name in reversed(self.resolve_scope(scope)) if name in declared]

    def dependents(self, name: str) -> list[str]:
        return [other for other, service in self.services.items() if name in service.depends_on]

    def _members(self, scope: str) -> list[str]:
        if scope not in self.scopes:
            known = ", ".join(sorted(self.scopes))
            raise manifest_error(f"unknown scope {scope!r}; manifest declares {known}")
        return list(self.scopes[scope])

    def _visit(self, name: str, ordered: list[str], seen: set[str]) -> None:
        if name in ordered:
            return
        if name in seen:
            raise manifest_error(f"dependency cycle through service {name!r}")
        seen.add(name)
        for dependency in self.services[name].depends_on:
            self._visit(dependency, ordered, seen)
        ordered.append(name)

    def _build_derived_scopes(self, mode_services: dict[str, Service]) -> dict[str, list[str]]:
        derived: dict[str, list[str]] = {
            "full": list(mode_services.keys()),
            "local": list(mode_services.keys()),
        }
        derived.update(
            {
                alias: [name]
                for name, service in mode_services.items()
                for alias in (name, *service.aliases)
            }
        )
        derived.update(
            {
                scope: selected
                for scope, members in self.explicit_scopes.items()
                if (selected := [member for member in members if member in mode_services])
            }
        )
        return derived

    def for_mode(self, mode_name: str | None = None) -> Manifest:
        if not self.modes:
            return self
        target_mode = mode_name or self.default_mode or next(iter(self.modes.keys()))
        if target_mode not in self.modes:
            known = ", ".join(sorted(self.modes.keys()))
            raise manifest_error(f"unknown mode {target_mode!r}; manifest declares modes: {known}")
        mode_services = {**self.base_services, **self.modes[target_mode]}
        derived_scopes = self._build_derived_scopes(mode_services)

        m = Manifest(
            project=self.project,
            services=mode_services,
            scopes=derived_scopes,
            path=self.path,
            base_services=self.base_services,
            modes=self.modes,
            default_mode=self.default_mode,
            active_mode=target_mode,
            explicit_scopes=self.explicit_scopes,
        )
        for scope in derived_scopes:
            m.resolve_scope(scope)
        return m
