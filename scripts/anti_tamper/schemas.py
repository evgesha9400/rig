"""Closed configuration schema validation for Ruff and JSCPD."""

from __future__ import annotations

import fnmatch
import json
import os
from pathlib import Path
from typing import Any

import tomllib

MAX_JSCPD_LINES = 5
MAX_JSCPD_TOKENS = 40
REQUIRED_RULES = frozenset(
    {"E", "F", "C90", "B", "BLE", "TRY", "SIM", "UP", "PLR", "PIE", "RUF", "PT"}
)
ALLOWED_IGNORES = frozenset({"TRY003"})
ALLOWED_JSCPD_IGNORES = (
    "**/node_modules/**",
    "**/.venv/**",
    "**/.pytest_cache/**",
    "**/.ruff_cache/**",
)
ALLOWED_RUFF_TOP_KEYS = frozenset({"target-version", "line-length", "lint", "format"})
ALLOWED_RUFF_LINT_KEYS = frozenset({"select", "ignore", "mccabe", "pylint", "per-file-ignores"})
ALLOWED_RUFF_PYLINT_KEYS = frozenset(
    {"max-statements", "max-args", "max-positional-args", "max-branches", "max-returns"}
)
ALLOWED_RUFF_MCCABE_KEYS = frozenset({"max-complexity"})
ALLOWED_RUFF_FORMAT_KEYS = frozenset(
    {"quote-style", "indent-style", "skip-magic-trailing-comma", "line-ending"}
)
ALLOWED_JSCPD_KEYS = frozenset(
    {"threshold", "minLines", "minTokens", "reporters", "ignore", "absolute"}
)
ALLOWED_PER_FILE_PATTERNS = frozenset({"tests/**"})


def _check_ruff_schema(data: dict[str, Any], lint: dict[str, Any]) -> list[str]:
    violations: list[str] = []
    if bad_top := set(data.keys()) - ALLOWED_RUFF_TOP_KEYS:
        violations.append(f"ruff.toml defines unauthorized top-level keys: {sorted(bad_top)}")
    if bad_lint := set(lint.keys()) - ALLOWED_RUFF_LINT_KEYS:
        violations.append(f"ruff.toml [lint] defines unauthorized keys: {sorted(bad_lint)}")
    if bad_pylint := set(lint.get("pylint", {}).keys()) - ALLOWED_RUFF_PYLINT_KEYS:
        violations.append(f"ruff.toml [lint.pylint] extra keys: {sorted(bad_pylint)}")
    if bad_mccabe := set(lint.get("mccabe", {}).keys()) - ALLOWED_RUFF_MCCABE_KEYS:
        violations.append(f"ruff.toml [lint.mccabe] extra keys: {sorted(bad_mccabe)}")
    if bad_format := set(data.get("format", {}).keys()) - ALLOWED_RUFF_FORMAT_KEYS:
        violations.append(f"ruff.toml [format] extra keys: {sorted(bad_format)}")
    return violations


def _match_pattern_against_sources(pat: str, src_files: list[Path], root: Path) -> bool:
    norm = os.path.normpath(pat)
    for f in src_files:
        rel = str(f.relative_to(root))
        if f.match(pat) or fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(rel, norm):
            return True
    return False


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
        elif _match_pattern_against_sources(pat, src_files, root):
            violations.append(f"ruff.toml per-file-ignores matches source file: {pat}")

    return violations


def check_ruff_config(root: Path, src: Path) -> list[str]:
    cfg = root / "ruff.toml"
    if not cfg.is_file():
        return ["ruff.toml not found"]
    try:
        data = tomllib.loads(cfg.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError) as exc:
        return [f"ruff.toml is not valid TOML: {exc}"]

    lint = data.get("lint", {})
    violations = _check_scope_exclusions((data, lint), (root, src))
    selected = set(lint.get("select", []))
    if missing := REQUIRED_RULES - selected:
        violations.append(f"ruff.toml missing required rule categories: {sorted(missing)}")
    if unauth := set(lint.get("ignore", [])) - ALLOWED_IGNORES:
        violations.append(f"ruff.toml defines unauthorized ignored rules: {sorted(unauth)}")
    return violations


def _check_jscpd_limits(data: dict[str, Any]) -> list[str]:
    violations: list[str] = []
    if bad_keys := set(data.keys()) - ALLOWED_JSCPD_KEYS:
        violations.append(f".jscpd.json defines unauthorized keys: {sorted(bad_keys)}")
    threshold = data.get("threshold")
    if threshold != 0 or isinstance(threshold, bool):
        violations.append(f".jscpd.json threshold must be 0 (current: {threshold})")

    min_lines = data.get("minLines")
    if (
        not isinstance(min_lines, int)
        or isinstance(min_lines, bool)
        or (min_lines > MAX_JSCPD_LINES)
    ):
        violations.append(f".jscpd.json minLines invalid: requires integer <= {MAX_JSCPD_LINES}")

    min_tokens = data.get("minTokens")
    if (
        not isinstance(min_tokens, int)
        or isinstance(min_tokens, bool)
        or (min_tokens > MAX_JSCPD_TOKENS)
    ):
        violations.append(f".jscpd.json minTokens invalid: requires integer <= {MAX_JSCPD_TOKENS}")
    return violations


def _check_jscpd_ignores(ignores: list[str], paths: tuple[Path, Path]) -> list[str]:
    root, src = paths
    violations: list[str] = []
    src_files = [f for f in src.rglob("*") if f.is_file()]
    for pat in ignores:
        if pat not in ALLOWED_JSCPD_IGNORES:
            violations.append(f".jscpd.json unauthorized ignore pattern: {pat}")
        elif _match_pattern_against_sources(pat, src_files, root):
            violations.append(f".jscpd.json ignore pattern matches source file: {pat}")
    return violations


def check_jscpd_config(root: Path, src: Path) -> list[str]:
    cfg = root / ".jscpd.json"
    if not cfg.is_file():
        return [".jscpd.json not found"]
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return [f".jscpd.json invalid JSON: {exc}"]
    return _check_jscpd_limits(data) + _check_jscpd_ignores(data.get("ignore", []), (root, src))
