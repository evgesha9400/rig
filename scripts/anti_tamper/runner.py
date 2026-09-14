"""Anti-tamper audit coordinator."""

from __future__ import annotations

from pathlib import Path

from .configs import check_competing_configs, check_nested_configs
from .deps import check_dependencies, get_runtime_status
from .pragmas import check_suppressions
from .schemas import check_jscpd_config, check_ruff_config


def run_audit(root: Path) -> int:
    src = root / "src"
    scripts = root / "scripts"
    if not src.is_dir():
        print("❌ ANTI-TAMPER AUDIT FAILED: src/ directory not found.")
        return 1

    scan_dirs = [d for d in (src, scripts) if d.is_dir()]
    file_count, suppression_issues = check_suppressions(scan_dirs, root)
    if file_count == 0:
        print("❌ ANTI-TAMPER AUDIT FAILED: Scanned 0 Python files.")
        return 1

    issues = (
        suppression_issues
        + check_nested_configs(scan_dirs)
        + check_competing_configs(root)
        + check_ruff_config(root, src)
        + check_jscpd_config(root, src)
        + check_dependencies(root)
    )

    if issues:
        print("❌ ANTI-TAMPER AUDIT FAILED:")
        for issue in issues:
            print(f"  • {issue}")
        return 1

    dep_status = get_runtime_status(root)
    print(
        f"✅ Anti-tamper audit passed ({file_count} files scanned, "
        f"0 suppressions, strict ceilings, {dep_status})."
    )
    return 0
