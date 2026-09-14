"""Tests for basic exclusive-lock acquire/release lifecycle."""

import os
import stat

import pytest

from rig import cli as rig

stack = rig


def test_lock_is_acquired_and_released(tmp_path):
    runtime = stack.ensure_runtime_dir(tmp_path)
    lock = runtime / "checkout.lock"

    with stack.exclusive_lock(lock, timeout=1.0):
        assert lock.exists()

    with stack.exclusive_lock(lock, timeout=1.0):
        pass


def test_lock_file_is_never_unlinked(tmp_path):
    runtime = stack.ensure_runtime_dir(tmp_path)
    lock = runtime / "checkout.lock"

    with stack.exclusive_lock(lock, timeout=1.0):
        inode = lock.stat().st_ino

    assert lock.exists()
    assert lock.stat().st_ino == inode


def test_lock_is_created_owner_readable_only(tmp_path):
    runtime = stack.ensure_runtime_dir(tmp_path)
    lock = runtime / "checkout.lock"

    with stack.exclusive_lock(lock, timeout=1.0):
        pass

    assert stat.S_IMODE(lock.stat().st_mode) == 0o600


def test_lock_rejects_a_symlinked_lock_path(tmp_path):
    runtime = stack.ensure_runtime_dir(tmp_path)
    victim = runtime / "victim"
    victim.write_text("")
    link = runtime / "checkout.lock"
    link.symlink_to(victim)

    with pytest.raises(stack.StackError), stack.exclusive_lock(link, timeout=0.2):
        pytest.fail("a symlinked lock path must be refused")


def test_lock_descriptor_is_close_on_exec(tmp_path):
    runtime = stack.ensure_runtime_dir(tmp_path)
    lock = runtime / "checkout.lock"

    with stack.exclusive_lock(lock, timeout=1.0) as fd:
        assert os.get_inheritable(fd) is False
