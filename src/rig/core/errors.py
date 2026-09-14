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
        self.details: dict[str, Any] = kwargs.get("details") or {}


StackError = RigError


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
