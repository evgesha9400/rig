#!/usr/bin/env python3
"""Anti-tamper audit entry point."""

from __future__ import annotations

import sys
from pathlib import Path

try:
    from anti_tamper.runner import run_audit
except ModuleNotFoundError:
    from scripts.anti_tamper.runner import run_audit

if __name__ == "__main__":
    sys.exit(run_audit(Path.cwd()))
