#!/usr/bin/env python3
"""Anti-tamper audit entry point."""

from __future__ import annotations

import sys
from pathlib import Path

# Add scripts directory to sys.path so anti_tamper can be imported directly
sys.path.insert(0, str(Path(__file__).parent))

from anti_tamper.runner import run_audit

if __name__ == "__main__":
    sys.exit(run_audit(Path.cwd()))
