"""Nested and competing configuration checks."""

from __future__ import annotations

from pathlib import Path

COMPETING_CONFIGS = (".flake8", "setup.cfg", ".pylintrc")
NESTED_CONFIG_NAMES = frozenset({"ruff.toml", ".ruff.toml", ".jscpd.json"})


def _scan_dir_for_nested(d: Path) -> list[str]:
    violations: list[str] = []
    for p in d.rglob("*"):
        if p.is_file() and p.name in NESTED_CONFIG_NAMES:
            violations.append(f"Nested linter config forbidden: {p}")
    return violations


def check_nested_configs(dirs: list[Path]) -> list[str]:
    violations: list[str] = []
    for d in dirs:
        if d.is_dir():
            violations.extend(_scan_dir_for_nested(d))
    return violations


def check_competing_configs(root: Path) -> list[str]:
    violations: list[str] = []
    for cfg in COMPETING_CONFIGS:
        if (root / cfg).exists():
            violations.append(f"Competing linter configuration forbidden: {cfg}")
    return violations
