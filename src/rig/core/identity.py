"""Instance identity and filesystem path resolution."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
from pathlib import Path

from rig.core.constants import DIR_MODE_PRIVATE, FILE_MODE_PRIVATE


def instance_id(project: str, root: Path | str) -> str:
    """Return a stable identifier unique to this project *and* this checkout path."""
    canonical = str(Path(root).resolve())
    digest = hashlib.sha256(canonical.encode()).hexdigest()[:8]
    slug = re.sub(r"[^a-z0-9_-]+", "-", project.lower()).strip("-_") or "stack"
    if not slug[0].isalnum():
        slug = f"s{slug}"
    return f"{slug}-{digest}"


def get_state_home() -> Path:
    """Return root directory for rig global state."""
    override = os.environ.get("RIG_STATE_HOME")
    if override:
        return Path(override).expanduser().resolve()
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return (Path(xdg).expanduser() / "rig").resolve()
    return (Path.home() / ".local" / "state" / "rig").resolve()


def get_instances_dir() -> Path:
    """Return directory containing all machine-wide instance registries."""
    return get_state_home() / "instances"


def get_instance_dir(ident: str) -> Path:
    """Return state directory path for a specific instance."""
    return get_instances_dir() / ident


def ensure_instance_dir(ident: str) -> Path:
    """Create and secure owner-only state directory for an instance."""
    inst_dir = get_instance_dir(ident)
    inst_dir.mkdir(parents=True, mode=DIR_MODE_PRIVATE, exist_ok=True)
    if stat.S_IMODE(inst_dir.stat().st_mode) != DIR_MODE_PRIVATE:
        os.chmod(inst_dir, DIR_MODE_PRIVATE)
    return inst_dir


def get_boot_id() -> str:
    """Return OS boot identifier to detect system reboots."""
    linux_boot = Path("/proc/sys/kernel/random/boot_id")
    if linux_boot.exists():
        try:
            return linux_boot.read_text().strip()
        except OSError:
            pass
    try:
        res = subprocess.run(
            ["sysctl", "-n", "kern.boottime"],
            capture_output=True,
            text=True,
            timeout=1.0,
            check=False,
        )
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
    except (subprocess.SubprocessError, OSError):
        pass
    return "unknown"


def is_locked(path: Path) -> bool:
    """Return True if path is currently held under exclusive advisory lock."""
    target = Path(path)
    if not target.exists():
        return False
    try:
        fd = os.open(target, os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, FILE_MODE_PRIVATE)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
        except (BlockingIOError, InterruptedError):
            return True
        else:
            return False
    finally:
        os.close(fd)


_PROJECT_CANDIDATES = (
    "rig.json",
    "scripts/rig.json",
    ".config/rig.json",
    "stack.json",
    "scripts/stack.json",
    ".config/stack.json",
)


def find_project_root(start: Path | None = None) -> Path:
    """Find project root by walking upward from current working directory."""
    current = (start or Path.cwd()).resolve()
    for parent in [current, *current.parents]:
        for candidate in (*_PROJECT_CANDIDATES, ".git"):
            if (parent / candidate).exists():
                return parent
    return current


def find_default_manifest(root: Path) -> Path:
    """Resolve default manifest path, checking rig.json then stack.json candidates."""
    for rel in _PROJECT_CANDIDATES:
        c = root / rel
        if c.is_file():
            return c
    return root / _PROJECT_CANDIDATES[0]


def _get_project_name(root: Path) -> str:
    root = Path(root).resolve()
    for rel in _PROJECT_CANDIDATES:
        manifest_path = root / rel
        if manifest_path.is_file():
            try:
                data = json.loads(manifest_path.read_text())
                if isinstance(data, dict) and data.get("project"):
                    return str(data["project"])
            except (OSError, json.JSONDecodeError):
                pass
    return root.name
