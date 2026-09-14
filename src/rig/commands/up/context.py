"""Typed parameter contexts for rig up services, retries, and rollbacks."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rig.manifest.models import Manifest


@dataclass(frozen=True)
class ServiceContext:
    root: Path
    runtime: Path
    instance: str = ""
    values: Mapping[str, Any] = field(default_factory=dict)
    candidate_ports: Sequence[int] | None = None


@dataclass
class RetryContext:
    root: Path
    runtime: Path
    instance: str
    state: dict[str, Any]
    state_path: Path


@dataclass(frozen=True)
class RollbackContext:
    state: dict[str, Any]
    state_path: Path
    root: Path = Path(".")
    manifest: Manifest | None = None
