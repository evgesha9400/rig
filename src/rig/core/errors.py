"""Exceptions and JSON error formatting for rig."""

from __future__ import annotations

import json
from typing import Any

from rig.core.constants import EXIT_OP_FAILED, EXIT_USAGE


class RigError(RuntimeError):
    """A rig operation cannot proceed safely."""

    def __init__(self, message: str, **kwargs: Any) -> None:
        super().__init__(message)
        self.message = message
        self.code: str = str(kwargs.get("code", "E_GENERIC"))
        self.exit_code: int = int(kwargs.get("exit_code", EXIT_OP_FAILED))
        self.hint: str | None = kwargs.get("hint")
        self.headline: str | None = kwargs.get("headline")
        self.context: str | None = kwargs.get("context")
        self.details: dict[str, Any] = kwargs.get("details") or {}


StackError = RigError


def format_human_error(exc: RigError, th: Any) -> str:
    """Format a RigError into a high-signal human structured error card."""
    headline = exc.headline or exc.message
    lines = [f"  {th.red}✖ {headline}{th.r}"]
    if exc.context:
        for cl in exc.context.splitlines():
            lines.append(f"    {th.d}{cl}{th.r}")
    elif exc.headline and exc.headline != exc.message:
        lines.append(f"    {th.d}{exc.message}{th.r}")
    if exc.hint:
        lines.append(f"    {th.cyan}Hint:{th.r} {exc.hint}  {th.d}[{exc.code}]{th.r}")
    else:
        lines.append(f"    {th.d}[{exc.code}]{th.r}")
    return "\n".join(lines) + "\n"


def manifest_error(message: str, *, hint: str | None = None) -> RigError:
    """Return the error for a manifest that cannot be used as written."""
    return RigError(message, code="E_USAGE", exit_code=EXIT_USAGE, hint=hint)


def print_json_envelope(command: str, data: Any, ok: bool | None = None) -> None:
    """Output envelope for structured JSON responses."""
    if ok is None:
        ok = bool(data["ok"]) if isinstance(data, dict) and "ok" in data else True
    envelope = {
        "schema": f"rig.{command}/1",
        "ok": ok,
        "data": data,
    }
    print(json.dumps(envelope, indent=2))


def print_json_error(exc: RigError, command: str = "error") -> None:
    """Output envelope for structured JSON errors."""
    envelope = {
        "schema": "rig.error/1",
        "ok": False,
        "error": {
            "code": exc.code,
            "exit_code": exc.exit_code,
            "message": exc.message,
            "hint": exc.hint,
            "details": exc.details,
        },
    }
    print(json.dumps(envelope, indent=2))
