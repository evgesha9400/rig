#!/usr/bin/env python3
"""Anti-Tamper Audit (Invariant Enforcement & Suppression Ban)

Verifies 0 inline suppressions, strict ceilings, 0 runtime deps, and closed config schemas.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import tomllib

MAX_JSCPD_LINES = 5
MAX_JSCPD_TOKENS = 40
BANNED_PRAGMAS = (
    r"#.*ruff:\s*noqa",
    r"#.*(?<![a-zA-Z0-9_])noqa(?![a-zA-Z0-9_])",
    r"#.*type:\s*ignore",
    r"#.*pragma:\s*no cover",
    r"#.*jscpd:\s*ignore",
    r"#.*pylint:\s*disable",
)
PROTECTED_RULES = (
    "PLR0913",
    "PLR0917",
    "PLR0915",
    "PLR0912",
    "PLR0911",
    "C901",
    "C90",
    "PLR",
    "PL",
    "C",
    "ALL",
)
ALLOWED_JSCPD_IGNORES = (
    "**/node_modules/**",
    "**/.venv/**",
    "**/.pytest_cache/**",
    "**/.ruff_cache/**",
)
ALLOWED_RUFF_TOP_KEYS = frozenset({"target-version", "line-length", "lint", "format"})
ALLOWED_RUFF_LINT_KEYS = frozenset({"select", "ignore", "mccabe", "pylint", "per-file-ignores"})
ALLOWED_JSCPD_KEYS = frozenset(
    {"threshold", "minLines", "minTokens", "reporters", "ignore", "absolute"}
)
ALLOWED_PER_FILE_PATTERNS = frozenset({"tests/**"})


def _check_suppressions(src_dir: Path) -> list[str]:
    violations, pattern = [], re.compile("|".join(BANNED_PRAGMAS), re.IGNORECASE)
    for py_file in src_dir.rglob("*.py"):
        for idx, line in enumerate(py_file.read_text(encoding="utf-8").splitlines(), start=1):
            if pattern.search(line):
                violations.append(f"{py_file.relative_to(src_dir.parent)}:{idx}: {line.strip()}")
    return violations


def _check_nested_configs(src: Path) -> list[str]:
    exts = ("*.toml", "*.yaml", "*.yml")
    return [f"Unauthorized override in source: {f}" for ext in exts for f in src.rglob(ext)]


def _check_competing_configs(root: Path) -> list[str]:
    return [
        f"Unauthorized competing config: {c}"
        for c in (".ruff.toml", "ruff.yaml", "ruff.yml")
        if (root / c).exists()
    ]


def _check_ruff_ceilings(data: dict[str, Any]) -> list[str]:
    pylint = data.get("lint", {}).get("pylint", {})
    mccabe = data.get("lint", {}).get("mccabe", {})
    rules = [
        (pylint.get("max-statements"), 15, "max-statements <= 15"),
        (pylint.get("max-args"), 3, "max-args <= 3"),
        (pylint.get("max-positional-args"), 3, "max-positional-args <= 3"),
        (pylint.get("max-branches"), 6, "max-branches <= 6"),
        (pylint.get("max-returns"), 4, "max-returns <= 4"),
        (mccabe.get("max-complexity"), 8, "max-complexity <= 8"),
        (data.get("line-length"), 100, "line-length <= 100"),
    ]
    return [
        f"ruff.toml invalid ceiling (current: {v}): requires {d}"
        for v, lim, d in rules
        if v is None or not isinstance(v, int) or v > lim
    ]


def _check_ignored_rules(lint: dict[str, Any]) -> list[str]:
    violations = []
    all_ignored = list(lint.get("ignore", [])) + list(lint.get("extend-ignore", []))
    for rule in all_ignored:
        if any(rule == b or b.startswith(rule) or rule.startswith(b) for b in PROTECTED_RULES):
            violations.append(f"ruff.toml illegally ignores quality rule: {rule}")
    selected = set(lint.get("select", [])) | set(lint.get("extend-select", []))
    for req in ("E", "F", "C90", "PLR"):
        if req not in selected:
            violations.append(f"ruff.toml missing required rule selection: {req}")
    return violations


def _check_ruff_schema(data: dict[str, Any], lint: dict[str, Any]) -> list[str]:
    violations = []
    if bad_top := set(data.keys()) - ALLOWED_RUFF_TOP_KEYS:
        violations.append(f"ruff.toml defines unauthorized top-level keys: {sorted(bad_top)}")
    if bad_lint := set(lint.keys()) - ALLOWED_RUFF_LINT_KEYS:
        violations.append(f"ruff.toml [lint] defines unauthorized keys: {sorted(bad_lint)}")
    return violations


def _check_scope_exclusions(
    configs: tuple[dict[str, Any], dict[str, Any]], paths: tuple[Path, Path]
) -> list[str]:
    data, lint = configs
    root, src = paths
    violations = _check_ruff_schema(data, lint)
    pfi = {**lint.get("per-file-ignores", {}), **lint.get("extend-per-file-ignores", {})}
    if bad_pfi := set(pfi.keys()) - ALLOWED_PER_FILE_PATTERNS:
        violations.append(f"ruff.toml per-file-ignores unauthorized patterns: {sorted(bad_pfi)}")
    src_files = list(src.rglob("*.py"))
    for pat in pfi:
        norm = os.path.normpath(pat)
        if ".." in pat or "src" in norm or not norm.startswith("tests"):
            violations.append(f"ruff.toml per-file-ignores targets non-test code: {pat}")
        for f in src_files:
            rel = str(f.relative_to(root))
            if f.match(pat) or fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(rel, norm):
                violations.append(f"ruff.toml per-file-ignores matches source file: {pat}")
                break
    return violations


def _check_ruff_config(root: Path, src: Path) -> list[str]:
    cfg = root / "ruff.toml"
    if not cfg.is_file():
        return ["ruff.toml not found"]
    try:
        data = tomllib.loads(cfg.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError) as exc:
        return [f"ruff.toml is not valid TOML: {exc}"]
    lint = data.get("lint", {})
    return (
        _check_ruff_ceilings(data)
        + _check_ignored_rules(lint)
        + _check_scope_exclusions((data, lint), (root, src))
    )


def _check_jscpd_limits(data: dict[str, Any]) -> list[str]:
    violations = []
    if bad_keys := set(data.keys()) - ALLOWED_JSCPD_KEYS:
        violations.append(f".jscpd.json defines unauthorized keys: {sorted(bad_keys)}")
    if data.get("threshold") != 0:
        violations.append(f".jscpd.json threshold must be 0 (current: {data.get('threshold')})")
    if data.get("minLines", 999) > MAX_JSCPD_LINES or data.get("minTokens", 999) > MAX_JSCPD_TOKENS:
        violations.append(".jscpd.json limits weakened: requires minLines<=5, minTokens<=40")
    return violations


def _check_jscpd_ignores(ignores: list[str], paths: tuple[Path, Path]) -> list[str]:
    root, src = paths
    violations = []
    for pat in ignores:
        if pat not in ALLOWED_JSCPD_IGNORES:
            violations.append(f".jscpd.json unauthorized ignore pattern: {pat}")
    src_files = [f for f in src.rglob("*") if f.is_file()]
    for pat in ignores:
        norm = os.path.normpath(pat)
        for f in src_files:
            rel = str(f.relative_to(root))
            if (
                f.match(pat)
                or fnmatch.fnmatch(rel, pat)
                or fnmatch.fnmatch(rel, norm)
                or fnmatch.fnmatch(str(f), pat)
            ):
                violations.append(f".jscpd.json ignore pattern matches source file {rel}: {pat}")
                break
    return violations


def _check_jscpd_config(root: Path, src: Path) -> list[str]:
    cfg = root / ".jscpd.json"
    if not cfg.is_file():
        return [".jscpd.json not found"]
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return [f".jscpd.json invalid JSON: {exc}"]
    return _check_jscpd_limits(data) + _check_jscpd_ignores(data.get("ignore", []), (root, src))


def _check_dependencies(root: Path) -> list[str]:
    f = root / "pyproject.toml"
    if not f.is_file():
        return ["pyproject.toml not found"]
    try:
        data = tomllib.loads(f.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError) as exc:
        return [f"pyproject.toml is not valid TOML: {exc}"]
    else:
        deps = data.get("project", {}).get("dependencies")
        return [] if deps == [] else [f"pyproject.toml dependencies must be [] (found: {deps})"]


def run_audit(root: Path) -> int:
    src = root / "src"
    if not src.is_dir():
        print("❌ ANTI-TAMPER AUDIT FAILED: src/ directory not found.")
        return 1

    issues = (
        _check_suppressions(src)
        + _check_nested_configs(src)
        + _check_competing_configs(root)
        + _check_ruff_config(root, src)
        + _check_jscpd_config(root, src)
        + _check_dependencies(root)
    )
    if issues:
        print("❌ ANTI-TAMPER AUDIT FAILED:")
        for issue in issues:
            print(f"  • {issue}")
        return 1

    print("✅ Anti-tamper audit passed (0 suppressions, strict ceilings, 0 runtime deps).")
    return 0


if __name__ == "__main__":
    sys.exit(run_audit(Path.cwd()))
