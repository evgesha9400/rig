#!/usr/bin/env python3
"""Zero-Cycle Dependency Audit (Invariant 5: Structural Integrity & Cycle Bans)

Builds the complete import graph using Grimp and asserts zero circular dependencies
via standard-library graphlib.TopologicalSorter.
"""

from __future__ import annotations

import graphlib
import sys
from pathlib import Path

import grimp

SCAN_DIR = "src"


def _discover_top_packages(src_dir: Path) -> list[str]:
    packages: list[str] = []
    for item in sorted(src_dir.iterdir()):
        if item.name.startswith((".", "_")):
            continue
        if item.is_dir() and (item / "__init__.py").exists():
            packages.append(item.name)
        elif item.is_file() and item.suffix == ".py":
            packages.append(item.stem)
    return packages


def _run_grimp_check(top_packages: list[str]) -> tuple[int, str | None]:
    try:
        graph = grimp.build_graph(
            *top_packages,
            cache_dir=None,
            exclude_type_checking_imports=False,
        )
        dependencies = {m: graph.find_modules_directly_imported_by(m) for m in graph.modules}
        graphlib.TopologicalSorter(dependencies).prepare()
        return len(graph.modules), None
    except graphlib.CycleError as err:
        path = " -> ".join(err.args[1]) if len(err.args) > 1 else str(err)
        return 0, f"CIRCULAR DEPENDENCY DETECTED:\n  • {path}"
    except (grimp.exceptions.GrimpException, ValueError) as err:
        return 0, f"CYCLE AUDIT FAILED: {err}"


def check_cycles(root_dir: Path) -> int:
    src_dir = (root_dir / SCAN_DIR).resolve()
    if not src_dir.is_dir():
        print(f"❌ CYCLE AUDIT FAILED: '{src_dir}' directory not found.")
        return 1

    top_packages = _discover_top_packages(src_dir)
    if not top_packages:
        print(f"❌ CYCLE AUDIT FAILED: No packages found in '{src_dir}'.")
        return 1

    mod_count, error = _run_grimp_check(top_packages)
    if error:
        print(f"❌ {error}")
        return 1

    print(f"✅ Zero circular imports detected ({mod_count} modules analyzed via Grimp).")
    return 0


if __name__ == "__main__":
    sys.exit(check_cycles(Path.cwd()))
