"""Environment variable allowlisting and placeholder formatting."""

from __future__ import annotations

import os
import string
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from rig.core.constants import BASE_ENV_ALLOWLIST
from rig.core.errors import RigError


class _StrictValues(dict):
    def __missing__(self, key):
        raise RigError(f"unknown placeholder {{{key}}}")


_FORMATTER = string.Formatter()


def render(value: Any, values: Mapping[str, Any]) -> Any:
    """Substitute ``{name}`` placeholders in strings, lists and mappings."""
    strict = _StrictValues(values)
    if isinstance(value, str):
        try:
            return _FORMATTER.vformat(value, (), strict)
        except (IndexError, KeyError) as exc:
            raise RigError(f"cannot render {value!r}: {exc}") from None
    if isinstance(value, list):
        return [render(item, values) for item in value]
    if isinstance(value, Mapping):
        return {key: render(item, values) for key, item in value.items()}
    return value


_MIN_QUOTED_LEN = 2


def _strip_env_quotes(item: str) -> str:
    if len(item) >= _MIN_QUOTED_LEN and item[0] == item[-1] and item[0] in "\"'":
        return item[1:-1]
    return item


def parse_env_file(path: Path) -> dict[str, str]:
    """Return ``KEY=VALUE`` pairs from a dotenv-style file, ignoring comments."""
    values: dict[str, str] = {}
    try:
        text = Path(path).read_text()
    except OSError:
        return values
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, raw = stripped.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        values[key] = _strip_env_quotes(raw.strip())
    return values


def build_service_env(
    declared: Mapping[str, Any],
    *args: Any,
    **kwargs: Any,
) -> dict[str, str]:
    """Compose a service environment from an allowlist, env files and manifest values."""
    params = list(args)
    inherit: Sequence[str] = params.pop(0) if params else kwargs.get("inherit", ())
    root: Path = params.pop(0) if params else kwargs.get("root", Path.cwd())
    values: Mapping[str, Any] = params.pop(0) if params else kwargs.get("values", {})
    env_files: Sequence[str] = params.pop(0) if params else kwargs.get("env_files", ())

    env = {name: os.environ[name] for name in (*BASE_ENV_ALLOWLIST, *inherit) if name in os.environ}
    for relative in env_files:
        env.update(parse_env_file(Path(root) / relative))
    merged = {**values, "root": str(root)}
    env.update((str(key), str(render(raw, merged))) for key, raw in declared.items())
    return env
