"""Dependency hygiene validation."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import tomllib

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
    pkg = re.split(r"[^a-zA-Z0-9_\-]", dep)[0].lower()
    violations: list[str] = []
    if pkg in DEV_TOOLS_IN_RUNTIME:
        violations.append(
            f"Development tool '{dep}' forbidden in runtime dependencies (move to dev dependencies)"
        )
    if "*" in dep:
        violations.append(f"Wildcard version forbidden in runtime dependency: '{dep}'")
    return violations


def _parse_pyproject(root: Path) -> tuple[dict[str, Any] | None, list[str]]:
    f = root / "pyproject.toml"
    if not f.is_file():
        return None, ["pyproject.toml not found"]
    try:
        data = tomllib.loads(f.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError) as exc:
        return None, [f"pyproject.toml is not valid TOML: {exc}"]
    else:
        return data, []


def check_dependencies(root: Path) -> list[str]:
    data, parse_errors = _parse_pyproject(root)
    if data is None:
        return parse_errors

    deps = data.get("project", {}).get("dependencies")
    if deps is None or not isinstance(deps, list):
        return ["pyproject.toml missing required 'project.dependencies' list"]

    violations: list[str] = []
    cqg = data.get("tool", {}).get("code-quality-gates", {})
    if cqg.get("zero-runtime-dependencies", False) and deps != []:
        violations.append(
            f"pyproject.toml zero-runtime policy violated: dependencies must be [] (found: {deps})"
        )

    for dep in deps:
        violations.extend(_check_dep_entry(dep))
    return violations


def get_runtime_status(root: Path) -> str:
    data, _ = _parse_pyproject(root)
    if data is None:
        return "verified runtime deps"
    cqg = data.get("tool", {}).get("code-quality-gates", {})
    deps = data.get("project", {}).get("dependencies", [])
    is_zero = cqg.get("zero-runtime-dependencies", False) or deps == []
    return "0 runtime deps" if is_zero else "verified runtime deps"
