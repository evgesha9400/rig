#!/usr/bin/env python3
"""Architectural Directory Density Audit (Invariant 3: Modularity)

Ceiling: Maximum 10 files per directory (excluding subdirectories).
"""

from __future__ import annotations

import sys
from pathlib import Path

MAX_FILES_PER_DIR = 10
SCAN_DIRS = ["src"]
IGNORED_NAMES = {"__pycache__"}


def _scan_directory(directory: Path) -> tuple[Path, int] | None:
    if not directory.is_dir():
        return None
    if directory.name in IGNORED_NAMES:
        py_files = [f for f in directory.glob("*.py") if f.is_file()]
        return (directory, MAX_FILES_PER_DIR + len(py_files)) if py_files else None
    files = [f for f in directory.iterdir() if f.is_file() and f.name != ".DS_Store"]
    return (directory, len(files)) if len(files) > MAX_FILES_PER_DIR else None


def _scan_target(target_path: Path) -> list[tuple[Path, int]]:
    if not target_path.is_dir():
        print(f"❌ DIRECTORY DENSITY AUDIT FAILED: '{target_path}' directory not found.")
        sys.exit(1)
    results = [_scan_directory(d) for d in [target_path, *target_path.rglob("*")]]
    return [r for r in results if r is not None]


def check_density(root_dir: Path) -> int:
    violations: list[tuple[Path, int]] = []
    for scan_target in SCAN_DIRS:
        violations.extend(_scan_target(root_dir / scan_target))

    if violations:
        print(f"❌ DIRECTORY DENSITY AUDIT FAILED (Ceiling: {MAX_FILES_PER_DIR} files):")
        for directory, count in violations:
            print(f"  • {directory} contains {count} files (exceeds {MAX_FILES_PER_DIR})")
        print("\nAction: Decompose dense folders into cohesive domain submodules.")
        return 1

    print(f"✅ Directory density audit passed (All folders ≤ {MAX_FILES_PER_DIR} files).")
    return 0


if __name__ == "__main__":
    sys.exit(check_density(Path.cwd()))
