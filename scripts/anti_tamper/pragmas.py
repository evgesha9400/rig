"""Pragma and suppression scanning via Python tokenizer."""

from __future__ import annotations

import io
import re
import tokenize
from pathlib import Path
from typing import Any

BANNED_PRAGMA_PATTERNS = (
    r"ruff:\s*noqa",
    r"(?<![a-zA-Z0-9_])noqa(?![a-zA-Z0-9_])",
    r"type:\s*ignore",
    r"pragma:\s*no cover",
    r"jscpd:\s*ignore",
    r"pylint:\s*(disable|skip-file)",
)


def _check_comment(comment: str, lineno: int, rel_path: Path) -> list[str]:
    violations: list[str] = []
    for pattern in BANNED_PRAGMA_PATTERNS:
        if re.search(pattern, comment, re.IGNORECASE):
            violations.append(f"{rel_path}:{lineno} contains banned pragma: '{comment.strip()}'")
    return violations


def _find_comment_violations(tokens: Any, rel: Path) -> list[str]:
    violations: list[str] = []
    for tok in tokens:
        if tok.type == tokenize.COMMENT:
            violations.extend(_check_comment(tok.string, tok.start[0], rel))
    return violations


def _scan_file_tokens(f: Path, root: Path) -> list[str]:
    try:
        content = f.read_text(encoding="utf-8")
        tokens = tokenize.tokenize(io.BytesIO(content.encode("utf-8")).readline)
        return _find_comment_violations(tokens, f.relative_to(root))
    except (tokenize.TokenError, UnicodeDecodeError, OSError) as err:
        return [f"Failed to tokenize {f.relative_to(root)}: {err}"]


def check_suppressions(directories: list[Path], root: Path) -> tuple[int, list[str]]:
    all_violations: list[str] = []
    files: list[Path] = []
    for directory in directories:
        if directory.is_dir():
            files.extend(sorted(directory.rglob("*.py")))

    for f in files:
        all_violations.extend(_scan_file_tokens(f, root))

    return len(files), all_violations
