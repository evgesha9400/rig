#!/usr/bin/env python3
"""Architectural Directory Density Audit (Invariant 3: Modularity)

Ceiling: Maximum 10 files per directory (excluding subdirectories).
Fail-closed: Exits 1 if target directory does not exist or contains 0 files.
Counts all files including dotfiles (excluding OS metadata like .DS_Store).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

DEFAULT_MAX_FILES = 10
DEFAULT_SCAN_DIRS = ("src", "scripts")
IGNORED_DIR_NAMES = frozenset({"__pycache__", ".git", ".venv", ".pytest_cache", ".ruff_cache"})


def _audit_directory(directory: Path, ceiling: int) -> tuple[int, list[str]]:
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
    if count > ceiling:
        violations.append(f"'{directory}' contains {count} files (ceiling: {ceiling})")

    return count, violations


def _walk_target(target_path: Path, ceiling: int) -> tuple[int, list[str]]:
    total_files = 0
    violations: list[str] = []
    for dirpath, _, _ in os.walk(target_path):
        count, viols = _audit_directory(Path(dirpath), ceiling)
        total_files += count
        violations.extend(viols)
    return total_files, violations


def _resolve_target_list(raw_targets: list[str]) -> list[Path]:
    resolved: list[Path] = []
    for item in raw_targets:
        p = Path(item)
        if p.is_dir():
            resolved.append(p)
    return resolved


def _default_targets() -> list[Path]:
    root = Path.cwd()
    targets = [root / d for d in DEFAULT_SCAN_DIRS if (root / d).is_dir()]
    return targets if targets else [root]


def check_density(targets: list[Path], ceiling: int = DEFAULT_MAX_FILES) -> int:
    if not targets:
        print("❌ DIRECTORY DENSITY AUDIT FAILED: Zero valid target directories specified.")
        return 1

    total_scanned = 0
    all_violations: list[str] = []
    for target_path in targets:
        count, viols = _walk_target(target_path, ceiling)
        total_scanned += count
        all_violations.extend(viols)

    if total_scanned == 0:
        print("❌ DIRECTORY DENSITY AUDIT FAILED: Scanned 0 files across targets.")
        return 1

    if all_violations:
        print(f"❌ DIRECTORY DENSITY AUDIT FAILED (Ceiling: {ceiling} files per directory):")
        for v in all_violations:
            print(f"  • {v}")
        return 1

    msg = (
        f"✅ Directory density passed ({total_scanned} files "
        f"across {len(targets)} targets, ≤ {ceiling}/dir)."
    )
    print(msg)
    return 0


def main() -> int:
    args = sys.argv[1:]
    ceiling = DEFAULT_MAX_FILES
    if args and args[-1].isdigit():
        ceiling = int(args.pop())

    targets = _resolve_target_list(args) if args else _default_targets()
    return check_density(targets, ceiling)


if __name__ == "__main__":
    sys.exit(main())
