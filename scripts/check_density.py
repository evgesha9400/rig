#!/usr/bin/env python3
"""Architectural Directory Density Audit (Invariant 3: Modularity)

Ceiling: Maximum 10 files per directory (excluding subdirectories).
Fail-closed: Exits 1 if target directory does not exist or contains 0 files.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

MAX_FILES_PER_DIR = 10
SCAN_DIRS = ["src", "scripts"]
IGNORED_DIR_NAMES = {"__pycache__"}


def _audit_directory(directory: Path) -> tuple[int, list[str]]:
    violations: list[str] = []
    try:
        entries = list(directory.iterdir())
    except OSError as err:
        return 0, [f"Cannot read directory '{directory}': {err}"]

    if directory.name in IGNORED_DIR_NAMES:
        py_files = [f.name for f in entries if f.is_file() and f.suffix == ".py"]
        if py_files:
            violations.append(f"Forbidden source in cache '{directory}': {py_files}")
        return 0, violations

    files = [f for f in entries if f.is_file() and f.name != ".DS_Store"]
    count = len(files)
    if count > MAX_FILES_PER_DIR:
        violations.append(f"'{directory}' contains {count} files (ceiling: {MAX_FILES_PER_DIR})")

    return count, violations


def _walk_target(target_path: Path) -> tuple[int, list[str]]:
    total_files = 0
    violations: list[str] = []
    for dirpath, _, _ in os.walk(target_path):
        count, viols = _audit_directory(Path(dirpath))
        total_files += count
        violations.extend(viols)
    return total_files, violations


def _resolve_targets(root_dir: Path) -> list[Path] | None:
    targets = [root_dir / d for d in SCAN_DIRS if (root_dir / d).is_dir()]
    if targets:
        return targets
    return [root_dir] if root_dir.is_dir() else None


def check_density(root_dir: Path) -> int:
    targets = _resolve_targets(root_dir)
    if targets is None:
        print(f"❌ DIRECTORY DENSITY AUDIT FAILED: '{root_dir}' not found.")
        return 1

    total_scanned = 0
    all_violations: list[str] = []
    for target_path in targets:
        count, viols = _walk_target(target_path)
        total_scanned += count
        all_violations.extend(viols)

    if total_scanned == 0:
        print("❌ DIRECTORY DENSITY AUDIT FAILED: Scanned 0 files across targets.")
        return 1

    if all_violations:
        print(f"❌ DIRECTORY DENSITY AUDIT FAILED (Ceiling: {MAX_FILES_PER_DIR} files):")
        for v in all_violations:
            print(f"  • {v}")
        return 1

    msg = f"✅ Directory density passed ({total_scanned} files, ≤ {MAX_FILES_PER_DIR}/dir)."
    print(msg)
    return 0


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
    sys.exit(check_density(target))
