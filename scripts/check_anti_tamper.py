#!/usr/bin/env python3
"""Anti-Tamper Audit (Invariant Enforcement & Suppression Ban)

Verifies 0 inline suppressions via tokenizer, strict ceilings, 0 runtime deps,
and fully closed configuration schemas.
"""

from __future__ import annotations

import fnmatch
import io
import json
import os
import re
import sys
import tokenize
from pathlib import Path
from typing import Any

import tomllib

MAX_JSCPD_LINES = 5
MAX_JSCPD_TOKENS = 40
BANNED_PRAGMA_PATTERNS = (
    r"ruff:\s*noqa",
    r"(?<![a-zA-Z0-9_])noqa(?![a-zA-Z0-9_])",
    r"type:\s*ignore",
    r"pragma:\s*no cover",
    r"jscpd:\s*ignore",
    r"pylint:\s*(disable|skip-file)",
)
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


def _check_suppressions(src_dir: Path) -> tuple[int, list[str]]:
    violations: list[str] = []
    py_files = sorted(src_dir.rglob("*.py"))
    pattern = re.compile("|".join(BANNED_PRAGMA_PATTERNS), re.IGNORECASE)

    for py_file in py_files:
        try:
            tokens = tokenize.tokenize(io.BytesIO(py_file.read_bytes()).readline)
            for tok in tokens:
                if tok.type == tokenize.COMMENT and pattern.search(tok.string):
                    rel = py_file.relative_to(src_dir.parent)
                    violations.append(f"{rel}:{tok.start[0]}: {tok.string.strip()}")
        except (tokenize.TokenError, IndentationError, SyntaxError) as err:
            violations.append(f"{py_file.relative_to(src_dir.parent)}: Tokenizer failure: {err}")

    return len(py_files), violations


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
    selected = set(lint.get("select", [])) | set(lint.get("extend-select", []))
    missing = REQUIRED_RULES - selected
    if missing:
        violations.append(f"ruff.toml missing required rule selection: {sorted(missing)}")

    all_ignored = list(lint.get("ignore", [])) + list(lint.get("extend-ignore", []))
    for rule in all_ignored:
        if rule not in ALLOWED_IGNORES:
            violations.append(f"ruff.toml illegally ignores quality rule: {rule}")
    return violations


def _check_ruff_schema(data: dict[str, Any], lint: dict[str, Any]) -> list[str]:
    violations = []
    if bad_top := set(data.keys()) - ALLOWED_RUFF_TOP_KEYS:
        violations.append(f"ruff.toml defines unauthorized top-level keys: {sorted(bad_top)}")
    if bad_lint := set(lint.keys()) - ALLOWED_RUFF_LINT_KEYS:
        violations.append(f"ruff.toml [lint] defines unauthorized keys: {sorted(bad_lint)}")
    if bad_pylint := set(lint.get("pylint", {}).keys()) - ALLOWED_RUFF_PYLINT_KEYS:
        violations.append(
            f"ruff.toml [lint.pylint] defines unauthorized keys: {sorted(bad_pylint)}"
        )
    if bad_mccabe := set(lint.get("mccabe", {}).keys()) - ALLOWED_RUFF_MCCABE_KEYS:
        violations.append(
            f"ruff.toml [lint.mccabe] defines unauthorized keys: {sorted(bad_mccabe)}"
        )
    if bad_format := set(data.get("format", {}).keys()) - ALLOWED_RUFF_FORMAT_KEYS:
        violations.append(f"ruff.toml [format] defines unauthorized keys: {sorted(bad_format)}")
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
    threshold = data.get("threshold")
    if threshold != 0 or isinstance(threshold, bool):
        violations.append(f".jscpd.json threshold must be 0 (current: {threshold})")
    min_lines = data.get("minLines")
    if (
        not isinstance(min_lines, int)
        or isinstance(min_lines, bool)
        or min_lines < 1
        or min_lines > MAX_JSCPD_LINES
    ):
        violations.append(f".jscpd.json minLines invalid: requires integer <= {MAX_JSCPD_LINES}")
    min_tokens = data.get("minTokens")
    if (
        not isinstance(min_tokens, int)
        or isinstance(min_tokens, bool)
        or min_tokens < 1
        or min_tokens > MAX_JSCPD_TOKENS
    ):
        violations.append(f".jscpd.json minTokens invalid: requires integer <= {MAX_JSCPD_TOKENS}")
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


DEV_TOOLS_IN_RUNTIME = frozenset(
    {
        "pytest",
        "pytest-cov",
        "ruff",
        "pylint",
        "mypy",
        "black",
        "flake8",
        "isort",
        "deptry",
        "import-linter",
        "tox",
        "nox",
        "pre-commit",
        "grimp",
    }
)


def _check_dep_entry(dep: Any) -> list[str]:
    if not isinstance(dep, str):
        return [f"pyproject.toml invalid dependency specification: {dep}"]
    pkg_name = re.split(r"[^a-zA-Z0-9_\-]", dep)[0].lower()
    violations = []
    if pkg_name in DEV_TOOLS_IN_RUNTIME:
        violations.append(
            f"Development tool '{dep}' forbidden in runtime dependencies (move to dev dependencies)"
        )
    if "*" in dep:
        violations.append(f"Wildcard version forbidden in runtime dependency: '{dep}'")
    return violations


def _check_dependencies(root: Path) -> list[str]:
    f = root / "pyproject.toml"
    if not f.is_file():
        return ["pyproject.toml not found"]
    try:
        data = tomllib.loads(f.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError) as exc:
        return [f"pyproject.toml is not valid TOML: {exc}"]

    deps = data.get("project", {}).get("dependencies")
    if deps is None or not isinstance(deps, list):
        return ["pyproject.toml missing required 'project.dependencies' list"]

    violations = []
    cqg_cfg = data.get("tool", {}).get("code-quality-gates", {})
    if cqg_cfg.get("zero-runtime-dependencies", False) and deps != []:
        violations.append(
            f"pyproject.toml zero-runtime policy violated: dependencies must be [] (found: {deps})"
        )

    for dep in deps:
        violations.extend(_check_dep_entry(dep))
    return violations


def _get_runtime_status(root: Path) -> str:
    f = root / "pyproject.toml"
    if not f.is_file():
        return "verified runtime deps"
    try:
        data = tomllib.loads(f.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError):
        return "verified runtime deps"
    else:
        cqg = data.get("tool", {}).get("code-quality-gates", {})
        is_zero = (
            cqg.get("zero-runtime-dependencies", False)
            or data.get("project", {}).get("dependencies") == []
        )
        return "0 runtime deps" if is_zero else "verified runtime deps"


def run_audit(root: Path) -> int:
    src = root / "src"
    if not src.is_dir():
        print("❌ ANTI-TAMPER AUDIT FAILED: src/ directory not found.")
        return 1

    file_count, suppression_issues = _check_suppressions(src)
    if file_count == 0:
        print("❌ ANTI-TAMPER AUDIT FAILED: Scanned 0 Python files in src/.")
        return 1

    issues = (
        suppression_issues
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

    dep_status = _get_runtime_status(root)
    msg = (
        f"✅ Anti-tamper audit passed ({file_count} files scanned, "
        f"0 suppressions, strict ceilings, {dep_status})."
    )
    print(msg)
    return 0


if __name__ == "__main__":
    sys.exit(run_audit(Path.cwd()))
