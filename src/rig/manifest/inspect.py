"""Binary executable resolution utilities."""

from __future__ import annotations

import os
import shutil
from pathlib import Path


def _resolve_executable(spec: str, cwd: Path) -> Path | None:
    """Return the file a spawn would execute for ``spec``, or None when absent."""
    if os.sep in spec:
        candidate = Path(spec)
        if not candidate.is_absolute():
            candidate = cwd / candidate
        return candidate if candidate.is_file() else None
    found = shutil.which(spec)
    return Path(found) if found else None
