#!/usr/bin/env python3
"""Physical Line Budget Audit (Invariant 3: Modularity)

Ceiling: Maximum 150 physical lines per file in src/.
"""

from __future__ import annotations

import sys
from pathlib import Path

MAX_LINES_PER_FILE = 150
SCAN_DIR = "src"


def check_lines(root_dir: Path) -> int:
    target_path = root_dir / SCAN_DIR
    if not target_path.is_dir():
        print(f"❌ PHYSICAL LINE AUDIT FAILED: '{target_path}' directory not found.")
        return 1

    violations: list[tuple[Path, int]] = []
    for file_path in target_path.rglob("*.py"):
        line_count = len(file_path.read_text(encoding="utf-8").splitlines())
        if line_count > MAX_LINES_PER_FILE:
            violations.append((file_path, line_count))

    if violations:
        print(f"❌ PHYSICAL LINE AUDIT FAILED (Ceiling: {MAX_LINES_PER_FILE} lines):")
        for file_path, count in violations:
            rel_path = file_path.relative_to(root_dir)
            print(f"  • {rel_path} has {count} lines (exceeds {MAX_LINES_PER_FILE})")
        print("\nAction: Refactor large files into single-responsibility modules.")
        return 1

    print(f"✅ Physical line audit passed (All files ≤ {MAX_LINES_PER_FILE} lines).")
    return 0


if __name__ == "__main__":
    sys.exit(check_lines(Path.cwd()))
