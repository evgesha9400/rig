"""Tests for exclusive-lock contention, timeouts, and dead-holder recovery."""

import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from rig import cli as rig

stack = rig
STACK_PATH = Path(rig.__file__).resolve()

LOCK_HOLDER = textwrap.dedent(
    """
    import importlib.util, sys, time
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("child_stack", sys.argv[1])
    module = importlib.util.module_from_spec(spec)
    sys.modules["child_stack"] = module
    spec.loader.exec_module(module)
    with module.exclusive_lock(Path(sys.argv[2]), timeout=10.0):
        Path(sys.argv[3]).write_text("held")
        time.sleep(120)
    """
)


def _await_file(path, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if Path(path).exists():
            return True
        time.sleep(0.02)
    return False


def test_lock_contention_within_one_process_times_out(tmp_path):
    runtime = stack.ensure_runtime_dir(tmp_path)
    lock = runtime / "checkout.lock"

    with (
        stack.exclusive_lock(lock, timeout=1.0),
        pytest.raises(TimeoutError),
        stack.exclusive_lock(lock, timeout=0.2),
    ):
        pytest.fail("the second acquisition must not succeed")


def test_lock_contention_respects_a_monotonic_deadline(tmp_path):
    runtime = stack.ensure_runtime_dir(tmp_path)
    lock = runtime / "checkout.lock"

    with stack.exclusive_lock(lock, timeout=1.0):
        started = time.monotonic()
        with pytest.raises(TimeoutError), stack.exclusive_lock(lock, timeout=0.3):
            pytest.fail("the second acquisition must not succeed")
        elapsed = time.monotonic() - started

    assert 0.25 <= elapsed < 3.0


def test_lock_contention_across_processes_times_out(tmp_path):
    runtime = stack.ensure_runtime_dir(tmp_path)
    lock = runtime / "checkout.lock"
    holder_script = tmp_path / "holder.py"
    holder_script.write_text(LOCK_HOLDER)
    ready = tmp_path / "held.flag"
    child = subprocess.Popen(
        [sys.executable, str(holder_script), str(STACK_PATH), str(lock), str(ready)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        assert _await_file(ready), f"child never took the lock: {child.communicate()[0]}"
        with pytest.raises(TimeoutError), stack.exclusive_lock(lock, timeout=0.3):
            pytest.fail("the parent must not acquire a lock the child holds")
    finally:
        child.kill()
        child.wait(timeout=5)


def test_lock_is_released_when_the_holding_process_dies(tmp_path):
    runtime = stack.ensure_runtime_dir(tmp_path)
    lock = runtime / "checkout.lock"
    holder_script = tmp_path / "holder.py"
    holder_script.write_text(LOCK_HOLDER)
    ready = tmp_path / "held.flag"
    child = subprocess.Popen(
        [sys.executable, str(holder_script), str(STACK_PATH), str(lock), str(ready)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    assert _await_file(ready), "child never took the lock"
    child.kill()
    child.wait(timeout=5)

    with stack.exclusive_lock(lock, timeout=5.0):
        pass
