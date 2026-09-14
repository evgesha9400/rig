"""Anti-tamper audit package enforcing closed schemas and zero pragmas."""

from .runner import run_audit

__all__ = ["run_audit"]
