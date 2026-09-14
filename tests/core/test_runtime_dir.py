"""Tests for the owner-only runtime directory."""

import stat

import pytest

from rig import cli as rig

stack = rig


def test_runtime_dir_is_created_owner_only(tmp_path):
    runtime = stack.ensure_runtime_dir(tmp_path)

    assert runtime == tmp_path / ".local-run"
    assert runtime.is_dir()
    assert stat.S_IMODE(runtime.stat().st_mode) == 0o700


def test_runtime_dir_tightens_permissions_on_an_existing_loose_directory(tmp_path):
    loose = tmp_path / ".local-run"
    loose.mkdir(mode=0o755)

    runtime = stack.ensure_runtime_dir(tmp_path)

    assert stat.S_IMODE(runtime.stat().st_mode) == 0o700


def test_runtime_dir_rejects_a_symlinked_runtime_path(tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    (tmp_path / ".local-run").symlink_to(target)

    with pytest.raises(stack.StackError):
        stack.ensure_runtime_dir(tmp_path)
