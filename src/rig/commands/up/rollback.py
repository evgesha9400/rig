"""Rollback lifecycle operations for failed service launches."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from rig.commands.common import _stop_record
from rig.commands.up.context import RollbackContext
from rig.core.state import write_state
from rig.manifest.models import Manifest


def _rollback_step(
    name: str,
    ctx: tuple[dict[str, Any], Path, Path, Manifest | None],
    failed_services: set[str],
) -> None:
    state, state_path, root, manifest = ctx
    rec = state["services"].get(name)
    if rec is None:
        return
    if manifest and any(
        d in failed_services or d in state["services"] for d in manifest.dependents(name)
    ):
        return
    outcome = _stop_record(rec, root)
    if outcome in ("terminated", "killed", "stale"):
        state["services"].pop(name, None)
    else:
        failed_services.add(name)
    write_state(state_path, state)


def rollback_started(started: Sequence[str], ctx: RollbackContext) -> None:
    failed_services: set[str] = set()
    for name in reversed(list(started)):
        _rollback_step(name, (ctx.state, ctx.state_path, ctx.root, ctx.manifest), failed_services)


def _rollback(
    state: dict[str, Any], state_path: Path, started: Sequence[str], *args: Any, **kwargs: Any
) -> None:
    """External test adapter delegating to rollback_started."""
    argv = list(args)
    root = kwargs.get("root") or (argv.pop(0) if argv else Path("."))
    man = kwargs.get("manifest") or (argv.pop(0) if argv else None)
    rollback_started(started, RollbackContext(state, state_path, root, man))
