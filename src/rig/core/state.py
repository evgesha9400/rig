"""State file reading, atomic persistence, and redaction."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from rig.core.constants import (
    REDACTED,
    RUNTIME_DIR_NAME,
    SECRET_NAME_PATTERN,
    STATE_FILE_NAME,
)
from rig.core.identity import (
    _get_project_name,
    ensure_instance_dir,
    get_instances_dir,
    instance_id,
)


def empty_state() -> dict[str, Any]:
    return {"generation": 0, "services": {}}


def read_state(path: Path) -> dict[str, Any]:
    """Return persisted state, or an empty stack when it is absent or unreadable."""
    try:
        state = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return empty_state()
    if not isinstance(state, dict):
        return empty_state()
    state.setdefault("generation", 0)
    services = state.get("services")
    state["services"] = services if isinstance(services, dict) else {}
    if "ports" in state and not isinstance(state["ports"], dict):
        state["ports"] = {}
    return state


def resolve_state_file(path: Path) -> Path:
    p = Path(path)
    if p.is_symlink():
        with contextlib.suppress(OSError):
            return p.resolve()
    return p


def _write_temp_state(target_path: Path, state: Mapping[str, Any]) -> str:
    target_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        dir=target_path.parent,
        delete=False,
        prefix=f".{target_path.name}.tmp.",
    ) as handle:
        json.dump(state, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        return handle.name


def write_state(path: Path, state: Mapping[str, Any]) -> None:
    """Publish state atomically so no reader observes a partial generation."""
    target_path = resolve_state_file(path)
    tmp: str | None = None
    try:
        tmp = _write_temp_state(target_path, state)
        os.chmod(tmp, 0o600)
        os.replace(tmp, target_path)
    except BaseException:
        if tmp is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
        raise


def redact(value: Any) -> Any:
    """Return ``value`` with secret-looking mapping entries masked."""
    if isinstance(value, Mapping):
        return {
            k: REDACTED if isinstance(k, str) and SECRET_NAME_PATTERN.search(k) else redact(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def _relink_symlink(local_state: Path, target: Path) -> None:
    with contextlib.suppress(OSError):
        if local_state.resolve() != target.resolve():
            local_state.unlink()
            local_state.symlink_to(target)


def _relink_file(local_state: Path, target: Path) -> None:
    with contextlib.suppress(OSError):
        if not target.exists():
            shutil.copy2(local_state, target)
        local_state.unlink()
        local_state.symlink_to(target)


def _sync_state_symlink(local_state: Path, authoritative_state: Path) -> None:
    if local_state.is_symlink():
        _relink_symlink(local_state, authoritative_state)
    elif local_state.is_file():
        _relink_file(local_state, authoritative_state)
    elif not local_state.exists():
        with contextlib.suppress(OSError):
            local_state.symlink_to(authoritative_state)


def _resolve_instance(target: str) -> Path | None:
    instances_dir = get_instances_dir()
    if not instances_dir.is_dir():
        return None
    direct = instances_dir / target
    if direct.is_dir():
        return direct
    for item in instances_dir.iterdir():
        sf = item / STATE_FILE_NAME
        if item.is_dir() and sf.is_file() and read_state(sf).get("project") == target:
            return item
    return None


def _state_path(root: Path | str, instance: str | None = None) -> Path:
    if isinstance(root, str) and "/" not in root and "\\" not in root and instance is None:
        inst_dir = _resolve_instance(root) or ensure_instance_dir(root)
        return inst_dir / STATE_FILE_NAME
    root_path = Path(root).resolve()
    if instance is None:
        instance = instance_id(_get_project_name(root_path), root_path)
    inst_dir = ensure_instance_dir(instance)
    authoritative_state = inst_dir / STATE_FILE_NAME

    runtime = root_path / RUNTIME_DIR_NAME
    if runtime.is_dir():
        _sync_state_symlink(runtime / STATE_FILE_NAME, authoritative_state)
    return authoritative_state
