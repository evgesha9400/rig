"""Lifecycle mutex and runtime directory management."""

from __future__ import annotations

import contextlib
import errno
import fcntl
import os
import stat
import time
from pathlib import Path

from rig.core.constants import (
    DATA_DIR_NAME,
    DIR_MODE_PRIVATE,
    FILE_MODE_PRIVATE,
    LOCK_FILE_NAME,
    LOCK_TIMEOUT_SECS,
    LOG_DIR_NAME,
    RUNTIME_DIR_NAME,
)
from rig.core.errors import RigError
from rig.core.identity import _get_project_name, ensure_instance_dir, instance_id


def ensure_runtime_dir(root: Path) -> Path:
    """Ensure ``.local-run`` exists with private permissions."""
    runtime = Path(root) / RUNTIME_DIR_NAME
    if runtime.is_symlink():
        raise RigError(f"{runtime} is a symlink; refusing to use it as a runtime directory")
    runtime.mkdir(mode=DIR_MODE_PRIVATE, exist_ok=True)
    info = runtime.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise RigError(f"{runtime} is not a directory")
    if info.st_uid != os.getuid():
        raise RigError(f"{runtime} is owned by another user")
    if stat.S_IMODE(info.st_mode) != DIR_MODE_PRIVATE:
        os.chmod(runtime, DIR_MODE_PRIVATE)
    (runtime / LOG_DIR_NAME).mkdir(mode=DIR_MODE_PRIVATE, exist_ok=True)
    (runtime / DATA_DIR_NAME).mkdir(mode=DIR_MODE_PRIVATE, exist_ok=True)
    return runtime


def _lock_path(target: Path | str, instance: str | None = None) -> Path:
    if isinstance(target, str) and "/" not in target and "\\" not in target:
        return ensure_instance_dir(target) / LOCK_FILE_NAME
    if instance is not None:
        return ensure_instance_dir(instance) / LOCK_FILE_NAME
    root_path = Path(target).resolve()
    proj = _get_project_name(root_path)
    inst = instance_id(proj, root_path)
    return ensure_instance_dir(inst) / LOCK_FILE_NAME


def _validate_lock_fd(fd: int, path_obj: Path) -> None:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise RigError(f"{path_obj} is not a regular file")
    if info.st_uid != os.getuid():
        raise RigError(f"{path_obj} is owned by another user")
    if info.st_nlink != 1:
        raise RigError(f"{path_obj} has {info.st_nlink} links; refusing to lock it")


def _acquire_blocking(fd: int, path_obj: Path, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, InterruptedError):
            pass
        else:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            msg = f"another stack command holds {path_obj}; timed out after {timeout:g}s"
            raise TimeoutError(msg)
        time.sleep(min(0.05, remaining))


def _acquire_lock(fd: int, path_obj: Path, timeout: float) -> None:
    if timeout <= 0:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, InterruptedError):
            raise BlockingIOError(f"{path_obj} is currently locked by another process") from None
    else:
        _acquire_blocking(fd, path_obj, timeout)


def _open_lock_fd(path_obj: Path) -> int:
    path_obj.parent.mkdir(parents=True, mode=DIR_MODE_PRIVATE, exist_ok=True)
    try:
        return os.open(
            path_obj, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, FILE_MODE_PRIVATE
        )
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EMLINK):
            raise RigError(f"{path_obj} is a symlink; refusing to lock it") from None
        raise RigError(f"cannot open lock file {path_obj}: {exc}") from None


@contextlib.contextmanager
def exclusive_lock(path: Path, timeout: float = LOCK_TIMEOUT_SECS, blocking: bool = True):
    """Hold an exclusive advisory lock on ``path`` or raise ``TimeoutError``."""
    path_obj = Path(path)
    fd = _open_lock_fd(path_obj)
    try:
        _validate_lock_fd(fd, path_obj)
        _acquire_lock(fd, path_obj, timeout if blocking else 0.0)
        yield fd
    finally:
        os.close(fd)
