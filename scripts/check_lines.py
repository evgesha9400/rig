#!/usr/bin/env python3
"""Physical Line Budget Audit (Invariant 3: Modularity)

Audits that no Python file exceeds the physical line ceiling.
Counts literal newline bytes (b'\\n') to prevent Unicode separator distortions.
Fail-closed: Exits 1 if target directory does not exist or contains 0 Python files.
"""

from __future__ import annotations

import sys
from pathlib import Path

DEFAULT_CEILING = 150
DEFAULT_TARGET = "src"
MIN_ARGS_FOR_TARGET = 2
MIN_ARGS_FOR_CEILING = 3


def _count_physical_lines(file_path: Path) -> int:
    raw = file_path.read_bytes()
    count = raw.count(b"\n")
    return count + 1 if raw and not raw.endswith(b"\n") else count


def _relative_display(file_path: Path) -> str:
    cwd = Path.cwd()
    return str(file_path.relative_to(cwd)) if file_path.is_relative_to(cwd) else str(file_path)


def _find_line_violations(py_files: list[Path], ceiling: int) -> list[str]:
    violations: list[str] = []
    for f in py_files:
        lines = _count_physical_lines(f)
        if lines > ceiling:
            disp = _relative_display(f)
            violations.append(f"{disp} has {lines} lines (ceiling: {ceiling})")
    return violations


def _resolve_target_dir(target_arg: str) -> Path | None:
    path = Path(target_arg)
    if path.is_dir():
        return path
    resolved = Path.cwd() / target_arg
    return resolved if resolved.is_dir() else None


def check_lines(target_path: Path, ceiling: int = DEFAULT_CEILING) -> int:
    py_files = sorted(target_path.rglob("*.py"))
    if not py_files:
        print(f"❌ PHYSICAL LINE AUDIT FAILED: 0 Python files in '{target_path}'.")
        return 1

    violations = _find_line_violations(py_files, ceiling)
    if violations:
        print(f"❌ PHYSICAL LINE AUDIT FAILED (Ceiling: {ceiling} lines):")
        for v in violations:
            print(f"  • {v}")
        return 1

    disp = _relative_display(target_path)
    print(f"✅ Line audit passed for '{disp}' ({len(py_files)} files, all ≤ {ceiling} lines).")
    return 0


def main() -> int:
    target_str = sys.argv[1] if len(sys.argv) >= MIN_ARGS_FOR_TARGET else DEFAULT_TARGET
    ceiling = int(sys.argv[2]) if len(sys.argv) >= MIN_ARGS_FOR_CEILING else DEFAULT_CEILING
    target_dir = _resolve_target_dir(target_str)
    if target_dir is None:
        print(f"❌ PHYSICAL LINE AUDIT FAILED: Directory '{target_str}' not found.")
        return 1
    return check_lines(target_dir, ceiling)


if __name__ == "__main__":
    sys.exit(main())
