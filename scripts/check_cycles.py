#!/usr/bin/env python3
"""Zero-Cycle Dependency Audit (Invariant 5: Layer Hierarchy & Cycle Bans)

Builds the complete import graph across src/ and asserts that
no circular dependencies exist anywhere in the codebase.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path


class _CycleFinder:
    def __init__(self, graph: dict[str, set[str]]) -> None:
        self.graph = graph
        self.visited: set[str] = set()
        self.stack: list[str] = []
        self.on_stack: set[str] = set()
        self.cycles: list[list[str]] = []

    def dfs(self, node: str) -> None:
        self.visited.add(node)
        self.stack.append(node)
        self.on_stack.add(node)
        for neighbor in self.graph.get(node, ()):
            if neighbor not in self.visited:
                self.dfs(neighbor)
            elif neighbor in self.on_stack:
                idx = self.stack.index(neighbor)
                self.cycles.append([*self.stack[idx:], neighbor])
        self.stack.pop()
        self.on_stack.remove(node)

    def find(self) -> list[list[str]]:
        for node in sorted(self.graph):
            if node not in self.visited:
                self.dfs(node)
        return self.cycles


def _file_to_mod(f: Path, src_dir: Path) -> str:
    rel = f.relative_to(src_dir)
    if f.stem == "__init__":
        return ".".join(rel.parent.parts)
    return ".".join(rel.with_suffix("").parts)


def _resolve_relative_mod(node: ast.ImportFrom, parts: list[str], is_pkg: bool) -> str:
    if node.level <= 0:
        return node.module or ""
    up = (node.level - 1) if is_pkg else node.level
    base = [] if up >= len(parts) else (parts[:-up] if up > 0 else parts)
    prefix = ".".join(base)
    return f"{prefix}.{node.module}" if node.module and prefix else (node.module or prefix)


def _extract_imports(f: Path, src_dir: Path, all_mods: set[str]) -> set[str]:
    cur_mod = _file_to_mod(f, src_dir)
    is_pkg = f.stem == "__init__"
    parts = cur_mod.split(".")
    tree = ast.parse(f.read_text(encoding="utf-8"))
    results = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            results.update(a.name for a in node.names if a.name in all_mods and a.name != cur_mod)
        elif isinstance(node, ast.ImportFrom):
            mod = _resolve_relative_mod(node, parts, is_pkg)
            if mod in all_mods and mod != cur_mod:
                results.add(mod)
            for a in node.names:
                cand = f"{mod}.{a.name}" if mod else a.name
                if cand in all_mods and cand != cur_mod:
                    results.add(cand)
    return results


def check_cycles(root_dir: Path) -> int:
    src_dir = root_dir / "src"
    if not src_dir.is_dir():
        print("❌ CYCLE AUDIT FAILED: src/ directory not found.")
        return 1

    py_files = [f for f in src_dir.rglob("*.py")]
    all_mods = {_file_to_mod(f, src_dir) for f in py_files}
    graph = {_file_to_mod(f, src_dir): _extract_imports(f, src_dir, all_mods) for f in py_files}

    cycles = _CycleFinder(graph).find()
    if cycles:
        print(f"❌ CIRCULAR DEPENDENCY DETECTED ({len(cycles)} cycles):")
        for cycle in cycles:
            print("  • " + " -> ".join(cycle))
        return 1

    print(f"✅ Zero circular imports detected ({len(graph)} modules analyzed).")
    return 0


if __name__ == "__main__":
    sys.exit(check_cycles(Path.cwd()))
