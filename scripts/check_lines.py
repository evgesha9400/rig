#!/usr/bin/env python3
"""Physical Line Budget Audit (Invariant 3: Modularity)

Ceiling: Maximum 150 physical lines per file in src/.
Fail-closed: Exits 1 if target directory does not exist or contains 0 Python files.
Counts literal newline bytes (b'\\n') to prevent Unicode separator distortions.
"""

from __future__ import annotations

import sys
from pathlib import Path

MAX_LINES_PER_FILE = 150
SCAN_DIR = "src"


def _count_physical_lines(file_path: Path) -> int:
    raw = file_path.read_bytes()
    count = raw.count(b"\n")
    return count + 1 if raw and not raw.endswith(b"\n") else count


def _find_line_violations(py_files: list[Path], root_dir: Path) -> list[str]:
    violations: list[str] = []
    for f in py_files:
        lines = _count_physical_lines(f)
        if lines > MAX_LINES_PER_FILE:
            rel = f.relative_to(root_dir)
            violations.append(f"{rel} has {lines} lines (ceiling: {MAX_LINES_PER_FILE})")
    return violations


def check_lines(root_dir: Path) -> int:
    target = root_dir / SCAN_DIR
    if not target.is_dir():
        if not root_dir.is_dir():
            print(f"❌ PHYSICAL LINE AUDIT FAILED: '{root_dir}' not found.")
            return 1
        target = root_dir

    py_files = sorted(target.rglob("*.py"))
    if not py_files:
        print(f"❌ PHYSICAL LINE AUDIT FAILED: 0 Python files in '{target}'.")
        return 1

    violations = _find_line_violations(py_files, root_dir)
    if violations:
        print(f"❌ PHYSICAL LINE AUDIT FAILED (Ceiling: {MAX_LINES_PER_FILE} lines):")
        for v in violations:
            print(f"  • {v}")
        return 1

    print(f"✅ Line audit passed ({len(py_files)} files, all ≤ {MAX_LINES_PER_FILE} lines).")
    return 0


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
    sys.exit(check_lines(target))
