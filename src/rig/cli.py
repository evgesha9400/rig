#!/usr/bin/env python3
"""Generic local stack orchestrator.

Brings a repository's local services up and down with dynamically allocated ports,
no cross-checkout linkage, and verified teardown. Uses only the Python standard
library so any checkout can run it with a bare interpreter.

Design contracts
----------------
Zero-race port ownership
    Services that can inherit a descriptor (``type: "fd"``) receive a socket this
    script binds to ``127.0.0.1:0`` and hands over through ``pass_fds``. The port is
    never released between allocation and service start, so no other process can
    take it. Services that cannot inherit a descriptor (``type: "port"``) get a
    reserved port plus a strict-port contract: a collision fails loudly and the
    orchestrator retries with a fresh port instead of silently drifting. Container
    services (``type: "compose"``) let the Docker daemon allocate the host port and
    discover it afterwards with ``docker compose port``.

Per-checkout lifecycle mutex
    Every mutating command holds an exclusive ``flock`` on ``.local-run/checkout.lock``
    inside an owner-only runtime directory. The lock file is never unlinked, so its
    inode identity is stable. Acquisition is bounded by a monotonic deadline.

Verified teardown
    Services are spawned with ``start_new_session=True`` so each leads its own process
    group. State records the pid, process group, resolved binary, argument vector and
    kernel start time. Teardown refuses to signal anything whose identity cannot be
    re-verified, preferring a suspected orphan over killing an unrelated process.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import fcntl
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import signal
import socket
import stat
import string
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

RUNTIME_DIR_NAME = ".local-run"
LOCK_FILE_NAME = "checkout.lock"
STATE_FILE_NAME = "state.json"
LOG_DIR_NAME = "logs"

LOCK_TIMEOUT_SECS = 10.0
HEALTH_TIMEOUT_SECS = 45.0
TEARDOWN_TIMEOUT_SECS = 5.0
PORT_RELEASE_TIMEOUT_SECS = 5.0
PORT_RETRY_ATTEMPTS = 3

SERVICE_TYPES = ("fd", "port", "compose")
SCOPES = ("full", "local", "backend", "ui")

# Only these ambient variables reach a service by default. Everything else must be
# named in the service's `inherit` list, which keeps stray COMPOSE_*, DOCKER_*, VITE_*,
# proxy and application endpoint variables from selecting another project's resources.
BASE_ENV_ALLOWLIST = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "TERM",
    "TMPDIR",
    "TZ",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
)

# The Docker client settings that decide which daemon answers and with which
# credentials. A compose service is handed its declared environment, which is an
# allowlist and excludes every DOCKER_* name, so these are added back
# explicitly: without them a TLS or rootless setup cannot reach its own daemon.
DOCKER_CLIENT_ENV_PASSTHROUGH = (
    "DOCKER_CONFIG",
    "DOCKER_CERT_PATH",
    "DOCKER_TLS_VERIFY",
)

SECRET_NAME_PATTERN = re.compile(
    r"TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|_KEY$|^KEY$|APIKEY|PRIVATE",
    re.IGNORECASE,
)
REDACTED = "***"


EXIT_OK = 0
EXIT_OP_FAILED = 1
EXIT_USAGE = 2
EXIT_MUTEX_CONFLICT = 3
EXIT_NOT_FOUND = 4
EXIT_REFUSED = 5
EXIT_EXTERNAL_TOOL = 6
EXIT_INTERRUPTED = 130


class RigError(RuntimeError):
    """A rig operation cannot proceed safely."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "E_GENERIC",
        exit_code: int = EXIT_OP_FAILED,
        hint: str | None = None,
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.code = code
        self.exit_code = exit_code
        self.hint = hint
        self.details = details or {}


StackError = RigError


def manifest_error(message: str, *, hint: str | None = None) -> RigError:
    """Return the error for a manifest that cannot be used as written.

    Syntax, schema and structure faults are the caller's input, not a runtime
    failure, so they exit with ``EXIT_USAGE`` and never look like a stack that
    merely failed to start.
    """
    return RigError(message, code="E_USAGE", exit_code=EXIT_USAGE, hint=hint)



# --------------------------------------------------------------------------------------
# Instance identification
# --------------------------------------------------------------------------------------


def instance_id(project: str, root: Path | str) -> str:
    """Return a stable identifier unique to this project *and* this checkout path.

    Two checkouts of the same repository must never share Docker projects, volumes,
    networks or runtime state, so the canonical path is part of the identity.
    """
    canonical = str(Path(root).resolve())
    digest = hashlib.sha256(canonical.encode()).hexdigest()[:8]
    slug = re.sub(r"[^a-z0-9_-]+", "-", project.lower()).strip("-_") or "stack"
    if not slug[0].isalnum():
        slug = f"s{slug}"
    return f"{slug}-{digest}"


def get_state_home() -> Path:
    """Return root directory for rig global state.

    Defaults to $XDG_STATE_HOME/rig (or ~/.local/state/rig).
    Can be overridden via RIG_STATE_HOME for isolated testing.
    """
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
    inst_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    if stat.S_IMODE(inst_dir.stat().st_mode) != 0o700:
        os.chmod(inst_dir, 0o700)
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
        fd = os.open(target, os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        except (BlockingIOError, InterruptedError):
            return True
    finally:
        os.close(fd)


# --------------------------------------------------------------------------------------
# Runtime directory and lifecycle mutex
# --------------------------------------------------------------------------------------


def ensure_runtime_dir(root: Path) -> Path:
    """Create (or tighten) an owner-only runtime directory under the checkout."""
    runtime = Path(root) / RUNTIME_DIR_NAME
    if runtime.is_symlink():
        raise StackError(f"{runtime} is a symlink; refusing to use it as a runtime directory")
    runtime.mkdir(mode=0o700, exist_ok=True)
    info = runtime.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise StackError(f"{runtime} is not a directory")
    if info.st_uid != os.getuid():
        raise StackError(f"{runtime} is owned by another user")
    if stat.S_IMODE(info.st_mode) != 0o700:
        os.chmod(runtime, 0o700)
    (runtime / LOG_DIR_NAME).mkdir(mode=0o700, exist_ok=True)
    (Path(root) / "data").mkdir(parents=True, exist_ok=True)
    return runtime


@contextlib.contextmanager
def exclusive_lock(
    path: Path, timeout: float = LOCK_TIMEOUT_SECS, blocking: bool = True
):
    """Hold an exclusive advisory lock on ``path`` or raise ``TimeoutError``.

    ``timeout`` bounds acquisition only. Work performed inside the lock carries its
    own deadlines. The lock file is opened with ``O_NOFOLLOW`` and never unlinked, so
    every caller contends for one inode.
    """
    path_obj = Path(path)
    path_obj.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    try:
        fd = os.open(
            path_obj,
            os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EMLINK):
            raise StackError(f"{path_obj} is a symlink; refusing to lock it") from None
        raise StackError(f"cannot open lock file {path_obj}: {exc}") from None

    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise StackError(f"{path_obj} is not a regular file")
        if info.st_uid != os.getuid():
            raise StackError(f"{path_obj} is owned by another user")
        if info.st_nlink != 1:
            raise StackError(f"{path_obj} has {info.st_nlink} links; refusing to lock it")

        if not blocking:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, InterruptedError):
                raise BlockingIOError(f"{path_obj} is currently locked by another process")
            yield fd
            return

        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (BlockingIOError, InterruptedError):
                pass
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"another stack command holds {path_obj}; timed out after {timeout:g}s"
                )
            time.sleep(min(0.05, remaining))
        yield fd
    finally:
        os.close(fd)



# --------------------------------------------------------------------------------------
# State file
# --------------------------------------------------------------------------------------


def empty_state() -> dict[str, Any]:
    return {"generation": 0, "services": {}}


def read_state(path: Path) -> dict[str, Any]:
    """Return persisted state, or an empty stack when it is absent or unreadable."""
    try:
        raw = Path(path).read_text()
    except OSError:
        return empty_state()
    try:
        state = json.loads(raw)
    except json.JSONDecodeError:
        return empty_state()
    if not isinstance(state, dict):
        return empty_state()
    state.setdefault("generation", 0)
    services = state.get("services")
    state["services"] = services if isinstance(services, dict) else {}
    return state


def resolve_state_file(path: Path) -> Path:
    p = Path(path)
    if p.is_symlink():
        try:
            return p.resolve()
        except OSError:
            pass
    return p


def write_state(path: Path, state: Mapping[str, Any]) -> None:
    """Publish state atomically so no reader observes a partial generation."""
    target_path = resolve_state_file(path)
    target_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    # A predictable temp name is a symlink target another user can plant, so the
    # name is unique and created with O_CREAT | O_EXCL by tempfile.
    tmp: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=target_path.parent,
            delete=False,
            prefix=f".{target_path.name}.tmp.",
        ) as handle:
            tmp = handle.name
            json.dump(state, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, target_path)
    except BaseException:
        if tmp is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
        raise


def redact(value: Any) -> Any:
    """Return ``value`` with secret-looking mapping entries masked."""
    if isinstance(value, Mapping):
        masked: dict[str, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and SECRET_NAME_PATTERN.search(key):
                masked[key] = REDACTED
            else:
                masked[key] = redact(item)
        return masked
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


# --------------------------------------------------------------------------------------
# Ports and sockets
# --------------------------------------------------------------------------------------


def allocate_listener() -> tuple[socket.socket, int]:
    """Bind and listen on an ephemeral loopback port, keeping ownership of it.

    The returned socket stays open so the port cannot be taken by another process
    before the service that will serve on it starts. ``SO_REUSEPORT`` is deliberately
    not enabled: sharing the port would defeat the purpose.
    """
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind(("127.0.0.1", 0))
        listener.listen(socket.SOMAXCONN)
    except BaseException:
        listener.close()
        raise
    return listener, listener.getsockname()[1]


def reserve_port() -> int:
    """Return a currently free loopback port for a service that cannot inherit a socket."""
    listener, port = allocate_listener()
    listener.close()
    return port


def port_is_free(port: int) -> bool:
    """Return ``True`` when nothing is listening on ``port`` on loopback.

    ``SO_REUSEADDR`` is set on the probe so that connections lingering in
    ``TIME_WAIT`` from a service that has already exited do not read as a service
    still holding the port. A live listening socket still refuses the bind.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    with probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def wait_for_port_release(port: int, timeout: float = PORT_RELEASE_TIMEOUT_SECS) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if port_is_free(port):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def port_listener_matches(
    port: int, pgid: int | None = None, pid: int | None = None
) -> bool:
    """Return ``True`` only when a listener on ``port`` is positively verified to belong to ``pid`` or ``pgid``.

    Returns ``False`` if ``lsof`` is unavailable, times out, returns an error, detects no listener,
    or detects listeners that do not match ``pid`` or ``pgid``.
    """
    if pid is None and pgid is None:
        return False
    lsof_bin = shutil.which("lsof")
    if not lsof_bin:
        return False
    try:
        proc = subprocess.run(
            [lsof_bin, "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            capture_output=True,
            text=True,
            check=False,
            timeout=1.0,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    if proc.returncode != 0 or not proc.stdout.strip():
        return False
    pids = [int(line.strip()) for line in proc.stdout.splitlines() if line.strip().isdigit()]
    if not pids:
        return False
    owned = False
    for p in pids:
        is_p_owned = False
        if pid is not None and p == pid:
            is_p_owned = True
        elif pgid is not None:
            try:
                if os.getpgid(p) == pgid:
                    is_p_owned = True
            except (ProcessLookupError, PermissionError, OSError):
                pass
        if not is_p_owned:
            return False
        owned = True
    return owned


# --------------------------------------------------------------------------------------
# Health checks
# --------------------------------------------------------------------------------------


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def _health_opener() -> urllib.request.OpenerDirector:
    # An ambient proxy must never stand between the orchestrator and a loopback
    # service, and a redirect must never move the check to another origin.
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _RefuseRedirects()
    )


def wait_for_http(
    port: int,
    path: str,
    timeout: float = HEALTH_TIMEOUT_SECS,
    poll: float = 0.15,
    pid: int | None = None,
    pgid: int | None = None,
) -> bool:
    """Poll ``http://127.0.0.1:<port><path>`` until it answers 2xx or the deadline passes."""
    opener = _health_opener()
    url = f"http://127.0.0.1:{port}{path if path.startswith('/') else '/' + path}"
    deadline = time.monotonic() + timeout
    while True:
        if pid is not None and not pid_alive(pid):
            return False
        try:
            with opener.open(url, timeout=min(2.0, max(0.2, timeout))) as response:
                if 200 <= response.status < 300:
                    if pid is None and pgid is None:
                        return True
                    if (pid is None or pid_alive(pid)) and port_listener_matches(
                        port, pgid=pgid, pid=pid
                    ):
                        return True
        except (urllib.error.URLError, OSError, ValueError):
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll)


# --------------------------------------------------------------------------------------
# Process identity and teardown
# --------------------------------------------------------------------------------------

_OWN_CHILDREN: dict[int, subprocess.Popen] = {}


def _ps(pid: int, fields: str) -> str | None:
    try:
        completed = subprocess.run(
            ["ps", "-ww", "-p", str(pid), "-o", fields],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    line = completed.stdout.strip()
    return line or None


def process_start_time(pid: int) -> str | None:
    """Return the kernel-reported start time of ``pid``, the strongest reuse guard."""
    value = _ps(pid, "lstart=")
    return " ".join(value.split()) if value else None


def process_args(pid: int) -> str | None:
    value = _ps(pid, "args=")
    return " ".join(value.split()) if value else None


def stable_process_args(pid: int, settle: float = 1.0, interval: float = 0.06) -> str | None:
    """Return the command line once two consecutive reads agree.

    A launcher can replace itself, and some runtimes rewrite their process title
    shortly after starting. Recording the first value seen would make the service
    look stale moments later, so wait for it to settle before using it as evidence.
    """
    previous = process_args(pid)
    deadline = time.monotonic() + max(settle, 0.0)
    while time.monotonic() < deadline:
        time.sleep(interval)
        current = process_args(pid)
        if current is None or current == previous:
            return current if current is not None else previous
        previous = current
    return previous


def _is_zombie(pid: int) -> bool:
    state = _ps(pid, "state=")
    return bool(state) and state.lstrip().upper().startswith("Z")


def _collect(pid: int) -> bool:
    """Reap a child this process started. Returns ``True`` when it has exited."""
    child = _OWN_CHILDREN.get(pid)
    if child is None or child.poll() is None:
        return False
    _OWN_CHILDREN.pop(pid, None)
    return True


def pid_alive(pid: int) -> bool:
    """Return ``True`` when ``pid`` names a live, non-zombie process."""
    if _collect(pid):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    if _is_zombie(pid):
        # Reading process state costs a few milliseconds, so a child can die between
        # the poll above and here. Collect it now, otherwise the pid stays allocated
        # to an unreapable zombie and later ownership checks see a live pid.
        _collect(pid)
        return False
    return True


def resolve_binary(argv0: str) -> str:
    """Return the canonical executable path for ``argv0``."""
    if os.sep in argv0:
        return os.path.realpath(argv0)
    import shutil

    found = shutil.which(argv0)
    return os.path.realpath(found) if found else argv0


def _binary_matches(recorded: str, observed_argv0: str) -> bool:
    if os.sep in observed_argv0:
        return os.path.realpath(observed_argv0) == recorded
    # Some kernels report only a truncated executable name.
    return os.path.basename(recorded).startswith(observed_argv0)


def identity_baseline(record: Mapping[str, Any]) -> str:
    """Return the command line that ``pid`` must still report to be considered ours.

    The baseline is what the kernel reported just after the spawn, not what was
    requested. A launcher such as ``npm`` replaces itself with ``node`` before it
    serves anything, so comparing against the requested argument vector would
    misidentify a perfectly healthy service as stale and orphan it.
    """
    observed = record.get("identity")
    if observed:
        return str(observed)
    argv = record.get("argv") or []
    return " ".join(str(item) for item in argv)


def identity_matches(record: Mapping[str, Any]) -> bool:
    """Return ``True`` only when ``pid`` still runs exactly the recorded program.

    A recorded pid can be reused by an unrelated process, so start time, command
    line and executable are all compared. A record without that evidence never
    matches; the orchestrator would rather leave an orphan than kill a stranger.
    """
    pid = record.get("pid")
    recorded_start = record.get("start_time")
    recorded_binary = record.get("binary")
    baseline = identity_baseline(record)
    if not isinstance(pid, int) or pid <= 0:
        return False
    if not recorded_start or not baseline or not recorded_binary:
        return False
    if not pid_alive(pid):
        return False
    if process_start_time(pid) != recorded_start:
        return False
    observed = process_args(pid)
    if not observed or observed != baseline:
        return False
    return _binary_matches(str(recorded_binary), observed.split()[0])


def terminate_record(
    record: Mapping[str, Any], timeout: float = TEARDOWN_TIMEOUT_SECS
) -> str:
    """Stop the recorded process group.

    Returns ``"terminated"``, ``"killed"``, ``"stale"`` when the process was already
    gone, ``"refused"`` when ownership could not be re-established, or ``"failed"``
    when the process survived ``SIGKILL``.
    """
    pid = record.get("pid")
    pgid = record.get("pgid")
    if not isinstance(pid, int) or not isinstance(pgid, int) or pid <= 0 or pgid <= 0:
        return "refused"
    # Never signal the orchestrator's own process group: that would kill the caller
    # and, in a Make recipe, the whole build.
    if pid == os.getpid() or pgid == os.getpgid(0):
        return "refused"
    if not pid_alive(pid):
        if not pgid_alive(pgid):
            return "stale"
        # The leader died, but other processes remain active in its process group.
        # Ownership cannot be independently re-verified without a live leader.
        return "refused"
    if not identity_matches(record):
        return "refused"
    try:
        actual_pgid = os.getpgid(pid)
    except (ProcessLookupError, PermissionError):
        return "refused"
    if actual_pgid != pgid:
        return "refused"

    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return "stale"
    except PermissionError:
        if not pgid_alive(pgid):
            return "stale"
        return "refused"

    if _await_pg_exit(pgid, timeout):
        _collect(pid)
        return "terminated"

    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        _collect(pid)
        return "terminated"
    except PermissionError:
        if not pgid_alive(pgid):
            _collect(pid)
            return "terminated"
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(["pkill", "-9", "-g", str(pgid)], check=False)
        if not pgid_alive(pgid):
            _collect(pid)
            return "killed"
        return "refused"

    if _await_pg_exit(pgid, timeout):
        _collect(pid)
        return "killed"
    return "failed"


def pgid_alive(pgid: int) -> bool:
    """Return ``True`` when at least one process in process group ``pgid`` is alive."""
    _collect(pgid)
    try:
        os.killpg(pgid, 0)
        if _is_zombie(pgid):
            _collect(pgid)
            return _has_active_pg_members(pgid)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        _collect(pgid)
        return _has_active_pg_members(pgid)


def _has_active_pg_members(pgid: int) -> bool:
    """Return ``True`` if any non-zombie process belongs to process group ``pgid``."""
    try:
        proc = subprocess.run(
            ["ps", "-o", "stat=", "-g", str(pgid)],
            capture_output=True,
            text=True,
            check=False,
            timeout=1.0,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    if proc.returncode != 0 or not proc.stdout.strip():
        return False
    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    return any(not line.startswith("Z") for line in lines)


def _await_pg_exit(pgid: int, timeout: float) -> bool:
    deadline = time.monotonic() + max(timeout, 0.1)
    while True:
        if not pgid_alive(pgid):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def _await_exit(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + max(timeout, 0.1)
    while True:
        if not pid_alive(pid):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


# --------------------------------------------------------------------------------------
# Placeholder rendering
# --------------------------------------------------------------------------------------


class _StrictValues(dict):
    def __missing__(self, key):  # noqa: D105
        raise StackError(f"unknown placeholder {{{key}}}")


_FORMATTER = string.Formatter()


def render(value: Any, values: Mapping[str, Any]) -> Any:
    """Substitute ``{name}`` placeholders in strings, lists and mappings."""
    strict = _StrictValues(values)
    if isinstance(value, str):
        try:
            return _FORMATTER.vformat(value, (), strict)
        except (IndexError, KeyError) as exc:
            raise StackError(f"cannot render {value!r}: {exc}") from None
    if isinstance(value, list):
        return [render(item, values) for item in value]
    if isinstance(value, Mapping):
        return {key: render(item, values) for key, item in value.items()}
    return value


# --------------------------------------------------------------------------------------
# Service environment
# --------------------------------------------------------------------------------------


def parse_env_file(path: Path) -> dict[str, str]:
    """Return ``KEY=VALUE`` pairs from a dotenv-style file, ignoring comments."""
    values: dict[str, str] = {}
    try:
        text = Path(path).read_text()
    except OSError:
        return values
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, raw = stripped.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        item = raw.strip()
        if len(item) >= 2 and item[0] == item[-1] and item[0] in "\"'":
            item = item[1:-1]
        values[key] = item
    return values


def build_service_env(
    declared: Mapping[str, Any],
    inherit: Sequence[str],
    root: Path,
    values: Mapping[str, Any],
    env_files: Sequence[str] = (),
) -> dict[str, str]:
    """Compose a service environment from an allowlist, env files and manifest values.

    Inheritance is an allowlist rather than a filter, so ambient ``COMPOSE_*``,
    ``DOCKER_*``, ``VITE_*``, proxy and application endpoint variables cannot leak in
    and point a service at another project's resources. Manifest values, which carry
    the orchestrator's freshly allocated endpoints, are applied last.
    """
    env: dict[str, str] = {}
    for name in list(BASE_ENV_ALLOWLIST) + list(inherit):
        if name in os.environ:
            env[name] = os.environ[name]
    for relative in env_files:
        env.update(parse_env_file(Path(root) / relative))
    merged = {**values, "root": str(root)}
    for key, raw in declared.items():
        env[str(key)] = str(render(raw, merged))
    return env


# --------------------------------------------------------------------------------------
# Spawning
# --------------------------------------------------------------------------------------


def uvicorn_argv(python: str, app: str, factory: bool = False) -> list[str]:
    """Return an argv that runs uvicorn on an inherited socket descriptor.

    The environment's interpreter is invoked directly. A wrapper is not assumed to
    preserve arbitrary file descriptors, and ``--port`` is never passed: the socket
    is already bound.
    """
    argv = [python, "-m", "uvicorn", app]
    if factory:
        argv.append("--factory")
    argv += ["--fd", "{fd}"]
    return argv


def _open_log(log_path: Path):
    Path(log_path).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    return open(log_path, "ab", buffering=0)


def _record(
    name: str,
    kind: str,
    proc: subprocess.Popen,
    argv: Sequence[str],
    port: int | None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    # Capture the kernel's own view of the child now, while its identity is certain.
    observed = stable_process_args(proc.pid)
    argv0 = observed.split()[0] if observed else str(argv[0])
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        # The service exited before it could be recorded. It was spawned with
        # start_new_session=True, so it led its own group and its group id was its
        # pid. Recording that keeps the failure path free of exceptions; readiness
        # will fail next and the caller will report it.
        pgid = proc.pid
    record = {
        "name": name,
        "type": kind,
        "pid": proc.pid,
        "pgid": pgid,
        "binary": resolve_binary(argv0),
        "argv": list(argv),
        "identity": observed,
        "start_time": process_start_time(proc.pid),
        "port": port,
        "url": f"http://127.0.0.1:{port}" if port else None,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    if extra:
        record.update(extra)
    return record


def spawn_fd_service(
    name: str,
    argv: Sequence[str],
    cwd: Path,
    env: Mapping[str, str],
    log_path: Path,
    values: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Start a service on a listening socket this process binds and hands over.

    The listener is created, its port read, and its descriptor passed to the child
    through ``pass_fds``. The parent descriptor closes only after ``Popen`` returns,
    so the port is continuously owned and cannot be stolen in between.
    """
    log = _open_log(log_path)
    listener: socket.socket | None = None
    try:
        listener, port = allocate_listener()
        fd = listener.fileno()
        os.set_inheritable(fd, True)
        render_values = {**(values or {}), "fd": fd, "port": port}
        resolved = [str(render(item, render_values)) for item in argv]
        proc = None
        try:
            proc = subprocess.Popen(
                resolved,
                cwd=str(cwd),
                env=dict(env),
                pass_fds=(fd,),
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            # Only now is the descriptor safe to release: the child holds its own copy.
            listener.close()
            listener = None
            _OWN_CHILDREN[proc.pid] = proc
            return _record(name, "fd", proc, resolved, port, {"fd": fd})
        except BaseException as exc:
            if proc is not None:
                with contextlib.suppress(OSError):
                    os.killpg(proc.pid, signal.SIGKILL)
            if isinstance(exc, OSError):
                raise StackError(f"cannot start service {name}: {exc}") from None
            raise
    finally:
        if listener is not None:
            listener.close()
        log.close()


def spawn_port_service(
    name: str,
    argv: Sequence[str],
    cwd: Path,
    env: Mapping[str, str],
    log_path: Path,
    port: int,
    values: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Start a service that binds a port itself, such as a Vite dev server.

    The port is reserved immediately before the spawn and the service is expected to
    treat it strictly. A collision must fail loudly so the caller can retry with a
    fresh port; it must never silently move to a neighbouring port that another
    project may own.
    """
    log = _open_log(log_path)
    try:
        render_values = {**(values or {}), "port": port}
        resolved = [str(render(item, render_values)) for item in argv]
        proc = None
        try:
            proc = subprocess.Popen(
                resolved,
                cwd=str(cwd),
                env=dict(env),
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            _OWN_CHILDREN[proc.pid] = proc
            return _record(name, "port", proc, resolved, port)
        except BaseException as exc:
            if proc is not None:
                with contextlib.suppress(OSError):
                    os.killpg(proc.pid, signal.SIGKILL)
            if isinstance(exc, OSError):
                raise StackError(f"cannot start service {name}: {exc}") from None
            raise
    finally:
        log.close()


# --------------------------------------------------------------------------------------
# Docker Compose
# --------------------------------------------------------------------------------------


def compose_argv(
    instance: str,
    root: Path,
    compose_file: Path,
    args: Sequence[str],
    context: str | None = None,
) -> list[str]:
    """Build a Compose command scoped to this checkout.

    Every call carries ``--project-directory``, ``-p`` and ``-f`` so a command can
    never inspect or destroy another checkout's containers, volumes or networks.
    """
    argv = ["docker"]
    if context:
        argv += ["--context", context]
    argv += [
        "compose",
        "--project-directory",
        str(root),
        "-p",
        instance,
        "-f",
        str(compose_file),
    ]
    return argv + list(args)


def parse_compose_port(output: str) -> int:
    """Extract the host port from ``docker compose port`` output."""
    line = output.strip().splitlines()[-1].strip() if output.strip() else ""
    _, sep, port = line.rpartition(":")
    if not sep or not port.isdigit():
        raise StackError(f"no published host port in compose output {output!r}")
    return int(port)


# A record written before the Docker endpoint was pinned carries no
# ``docker_host`` key at all, which is not the same as a recorded ``None``: the
# first inherits whatever endpoint is ambient, the second pins the default local
# daemon the service was actually started against.
INHERIT_DOCKER_HOST: Any = object()


def record_docker_endpoint(record: Mapping[str, Any]) -> tuple[Any, Any]:
    """Return the Docker context and host one record was started against."""
    return record.get("docker_context"), record.get("docker_host", INHERIT_DOCKER_HOST)


def resolve_current_docker_context(env: Mapping[str, str] | None = None) -> str | None:
    """Return the name of the Docker context that is active right now.

    ``docker context use`` rebinds every later unqualified ``docker`` command to
    another daemon, machine-wide and for good. A record that names no context
    follows that switch, so it would look for its container on a daemon that
    never held it: the new daemon answers 'no such object', the record is
    discarded as stale, and the container stays behind on the old context with
    nothing left that knows about it. Naming the active context at startup keeps
    every later query and teardown on the daemon that holds the container.

    ``None`` means Docker could not answer, and the record then inherits the
    ambient context exactly as it did before.
    """
    cmd_env = dict(os.environ) if env is None else dict(env)
    named = cmd_env.get("DOCKER_CONTEXT")
    if named:
        return named
    try:
        probe = subprocess.run(
            ["docker", "context", "show"],
            capture_output=True,
            text=True,
            timeout=5.0,
            env=cmd_env,
        )
    except (OSError, subprocess.SubprocessError):
        # Docker missing, unreachable or too slow to answer must not fail a
        # startup that Compose itself may still complete.
        return None
    if probe.returncode != 0:
        return None
    return probe.stdout.strip() or None


def _pin_docker_endpoint(
    cmd_env: dict[str, str], context: Any, docker_host: Any
) -> dict[str, str]:
    """Point one Docker command at the endpoint its record was started against.

    A container exists only on the daemon that created it, so an ambient
    ``DOCKER_HOST`` that changed since startup must never decide where a status
    query or a teardown looks: the wrong daemon answers 'no such object', and
    that answer would discard a live record and strand its container.
    """
    # Every caller passes a pinned context as ``--context``, which outranks this
    # variable anyway, so it is dropped in both cases: a context named in the
    # environment must never redirect a command away from its record's daemon.
    cmd_env.pop("DOCKER_CONTEXT", None)
    if docker_host is INHERIT_DOCKER_HOST:
        return cmd_env
    if docker_host:
        cmd_env["DOCKER_HOST"] = str(docker_host)
    else:
        # The record pins the default local daemon, so an ambient override that
        # appeared afterwards must not redirect this command.
        cmd_env.pop("DOCKER_HOST", None)
    return cmd_env


def run_compose(
    instance: str,
    root: Path,
    compose_file: Path,
    args: Sequence[str],
    context: str | None = None,
    timeout: float = 180.0,
    env: Mapping[str, str] | None = None,
    docker_host: Any = INHERIT_DOCKER_HOST,
) -> subprocess.CompletedProcess:
    argv = compose_argv(instance, root, compose_file, args, context)
    cmd_env = dict(os.environ) if env is None else dict(env)
    _pin_docker_endpoint(cmd_env, context, docker_host)
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=cmd_env)
    except FileNotFoundError:
        raise RigError("docker is not installed or not on PATH", code="E_EXTERNAL_TOOL", exit_code=EXIT_EXTERNAL_TOOL) from None
    except subprocess.TimeoutExpired:
        raise StackError(f"compose command timed out: {' '.join(args)}") from None


def run_docker(
    args: Sequence[str],
    context: str | None = None,
    timeout: float = 60.0,
    docker_host: Any = INHERIT_DOCKER_HOST,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Run one plain ``docker`` command against one explicit Docker endpoint.

    ``context`` and ``docker_host`` come from the record being inspected, so the
    command reaches the daemon that holds its container.

    ``env`` is the environment the record was started with. A plain ``docker``
    command needs the ambient environment it runs in, not a service's declared
    variables, so only the Docker client settings are taken from it -- the same
    ones Compose was handed. They decide which config directory and certificates
    the client reads, so without them an inspection or a teardown looks in the
    default directory and cannot find its own context. A setting the record never
    held is dropped for the same reason ``DOCKER_CONTEXT`` is: one that appeared
    in the terminal afterwards must never redirect this command. ``None`` means
    the record predates the recorded environment, and the ambient settings stand.
    """
    argv = ["docker"]
    if context:
        argv += ["--context", str(context)]
    argv += list(args)
    cmd_env = dict(os.environ)
    if env is not None:
        for name in DOCKER_CLIENT_ENV_PASSTHROUGH:
            value = env.get(name)
            if value is None:
                cmd_env.pop(name, None)
            else:
                cmd_env[name] = str(value)
    _pin_docker_endpoint(cmd_env, context, docker_host)
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=cmd_env)
    except FileNotFoundError:
        raise RigError("docker is not installed or not on PATH", code="E_EXTERNAL_TOOL", exit_code=EXIT_EXTERNAL_TOOL) from None
    except subprocess.TimeoutExpired:
        raise StackError(f"docker command timed out: {' '.join(args)}") from None


def compose_file_present(record: Mapping[str, Any], root: Path) -> bool:
    """Return ``True`` when this record's compose file is still on disk."""
    compose_file = record.get("compose_file")
    if not compose_file:
        return False
    return (Path(root) / str(compose_file)).is_file()


DOCKER_ABSENT_MARKERS = ("no such object", "no such container")


def docker_label_container_ids(record: Mapping[str, Any]) -> list[str] | None:
    """Return the container IDs Compose labelled with this record's project and service.

    ``None`` means Docker refused to answer, which is not the same as an empty
    list: only an answered query proves no container exists.
    """
    context, docker_host = record_docker_endpoint(record)
    try:
        result = run_docker(
            [
                "ps",
                "-q",
                "-a",
                "--filter",
                f"label=com.docker.compose.project={record.get('instance')}",
                "--filter",
                f"label=com.docker.compose.service={record.get('compose_service')}",
            ],
            context,
            docker_host=docker_host,
            env=record_compose_env(record),
        )
    except (StackError, RigError):
        return None
    if result.returncode != 0:
        return None
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def docker_reports_no_such_object(probe: subprocess.CompletedProcess) -> bool:
    """Return ``True`` only when Docker itself answered that the container is gone.

    Every other failure — an unreachable daemon above all — leaves the question
    unanswered, and an unanswered question must never be read as absence.
    """
    answer = f"{probe.stderr or ''}\n{probe.stdout or ''}".lower()
    return any(marker in answer for marker in DOCKER_ABSENT_MARKERS)


def docker_container_status(
    container: str,
    context: Any = None,
    docker_host: Any = INHERIT_DOCKER_HOST,
    env: Mapping[str, str] | None = None,
) -> str:
    """Return 'alive', 'stopped', 'absent', or 'error' for one container ID.

    ``env`` is the record's own environment, which carries the Docker client
    settings the container was started against.
    """
    try:
        probe = run_docker(
            ["inspect", "--format", "{{.State.Status}}", str(container)],
            context,
            docker_host=docker_host,
            env=env,
        )
    except (StackError, RigError):
        return "error"
    if probe.returncode != 0:
        return "absent" if docker_reports_no_such_object(probe) else "error"
    return "alive" if probe.stdout.strip().lower() in ("running", "restarting") else "stopped"


def docker_record_targets(record: Mapping[str, Any]) -> tuple[list[str], bool]:
    """Return every container Docker holds for this record, and whether it answered.

    Compose scales one service to several containers, so the single recorded ID
    is only ever one of them and the Compose labels are the authority on the
    rest. The second value is ``False`` when Docker refused the label query,
    which is not proof that the service holds no container.
    """
    ids = docker_label_container_ids(record)
    targets = list(ids or [])
    recorded = str(record.get("container") or "")
    if recorded and not any(
        recorded.startswith(found) or found.startswith(recorded) for found in targets
    ):
        targets.insert(0, recorded)
    return targets, ids is not None


def docker_record_status(record: Mapping[str, Any]) -> str:
    """Report a compose record's state through plain Docker, with no compose file.

    Docker keeps the container and its Compose labels long after the checkout
    that declared it is deleted, so a missing compose file never hides a
    container. Only Docker's own 'no such object' answer reports 'absent': an
    unreachable daemon reports 'error', so an outage never discards ownership.
    A scaled service is judged by its liveliest container.

    An unanswered label query leaves the target set unknown, so a recorded
    container that is genuinely gone still reports 'error': the replicas the
    query never listed may be running, and 'absent' would discard them.
    """
    targets, answered = docker_record_targets(record)
    if not targets:
        return "absent" if answered else "error"
    context, docker_host = record_docker_endpoint(record)
    compose_env = record_compose_env(record)
    states = [
        docker_container_status(target, context, docker_host, compose_env)
        for target in targets
    ]
    for state in ("alive", "stopped", "error"):
        if state in states:
            return state
    return "absent" if answered else "error"


def docker_record_stop(record: Mapping[str, Any], remove: bool) -> str:
    """Stop, and with ``remove`` also reclaim, every container of one compose service.

    A scaled service owns more containers than the one recorded at start, so the
    Compose labels decide the target set. Every target is attempted even after a
    failure, and any failure is reported so the caller keeps its ownership record
    and a later teardown can finish the job.

    A target Docker itself reports gone is already reclaimed, so removal is
    idempotent: a recorded container deleted by someone else must never make the
    successful reclamation of a surviving replica look like a failure.

    An unanswered label query is itself a failure: the recorded container is
    still attempted, but the service cannot be reported as reclaimed while the
    replicas the query never listed may still be running.
    """
    targets, answered = docker_record_targets(record)
    if not targets:
        return "stale" if answered else "failed"
    context, docker_host = record_docker_endpoint(record)
    compose_env = record_compose_env(record)
    failed = False
    for target in targets:
        # Volumes are preserved: `rm -f` without `-v` reclaims the container only.
        commands = [["stop", target]] + ([["rm", "-f", target]] if remove else [])
        for args in commands:
            try:
                result = run_docker(
                    args, context, docker_host=docker_host, env=compose_env
                )
            except (StackError, RigError):
                failed = True
                break
            if result.returncode == 0:
                continue
            if docker_reports_no_such_object(result):
                # Docker answered that this container is already gone, which is
                # the state the command asked for. Nothing is left to reclaim,
                # so the remaining commands for this target are skipped.
                break
            failed = True
            break
    return "failed" if failed or not answered else "terminated"


def record_compose_env(record: Mapping[str, Any]) -> dict[str, str] | None:
    """Return the environment one compose record was started with, if it was recorded.

    A compose file may declare a variable as required -- ``${VAR:?message}`` --
    and Compose then refuses every command, ``ps`` and ``stop`` included, while
    that variable is undefined. The service was started with an environment that
    satisfied the compose file, so the same names are replayed for every later
    status query and teardown instead of whatever the terminal happens to hold.

    Secret-looking values are masked in the record, exactly as they are for a
    process service, so what is replayed proves a variable exists rather than
    carrying its real value: that is all Compose needs to evaluate the file and
    reach a container by project and service name. ``None`` means the record was
    written before the environment was kept, and the ambient one is used.
    """
    env = record.get("compose_env")
    if not isinstance(env, Mapping):
        return None
    return {str(key): str(value) for key, value in env.items()}


def compose_record_status(record: Mapping[str, Any], root: Path) -> str:
    """Return 'alive', 'stopped', 'absent', or 'error' for one compose record.

    A record owns every container Compose lists for its service, not just the
    one recorded at start, so the service is judged by its liveliest container
    and a deleted recorded ID never hides a surviving replica. An empty
    ``container`` means discovery failed while starting, not that the container
    is gone, so Compose is asked by service name instead. Only an answer from
    Docker itself can report 'absent'. A deleted checkout takes Compose out of
    reach, so Docker is asked directly instead.

    Compose refusing to answer is not the container's state: a compose file that
    declares a required variable cannot even be parsed while that variable is
    undefined. Docker keeps the container and its Compose labels and needs no
    compose file, so it is asked instead of reporting an unusable 'error'.
    """
    container = record.get("container")
    instance = record.get("instance")
    compose_file = record.get("compose_file")
    service = record.get("compose_service")
    if not (instance and compose_file and service):
        return "absent"
    if not compose_file_present(record, root):
        return docker_record_status(record)
    context, docker_host = record_docker_endpoint(record)
    compose_env = record_compose_env(record)
    try:
        result = run_compose(
            str(instance),
            Path(root),
            Path(compose_file),
            ["ps", "-q", "-a", str(service)],
            context,
            timeout=60.0,
            env=compose_env,
            docker_host=docker_host,
        )
    except (StackError, RigError):
        return docker_record_status(record)
    if result.returncode != 0:
        return docker_record_status(record)
    ids = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    recorded = str(container or "")
    if not ids:
        # Compose answered that the service holds no container. A recorded
        # container is still probed: one that lost its project labels stays this
        # record's responsibility until Docker itself reports it gone.
        return (
            docker_container_status(recorded, context, docker_host, compose_env)
            if recorded
            else "absent"
        )
    # Compose scales one service to several containers, so every listed ID
    # belongs to this record and the recorded ID is only ever one of them. A
    # deleted recorded container must never hide a surviving replica.
    states = [
        docker_container_status(found, context, docker_host, compose_env) for found in ids
    ]
    if recorded and not any(
        recorded.startswith(found) or found.startswith(recorded) for found in ids
    ):
        states.append(docker_container_status(recorded, context, docker_host, compose_env))
    for state in ("alive", "stopped"):
        if state in states:
            return state
    # Compose listed at least one container, so an inspection that answers
    # 'absent' contradicts Compose instead of proving the service is gone.
    return "error"


def compose_record_alive(record: Mapping[str, Any], root: Path) -> bool:
    """Return ``True`` when the recorded container is still running under this instance."""
    return compose_record_status(record, root) == "alive"


# --------------------------------------------------------------------------------------
# Manifest
# --------------------------------------------------------------------------------------


@dataclass
class Service:
    name: str
    type: str
    cwd: str = "."
    command: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    app: str | None = None
    factory: bool = False
    python: str | None = None
    env: dict[str, Any] = field(default_factory=dict)
    inherit: list[str] = field(default_factory=list)
    env_files: list[str] = field(default_factory=list)
    healthcheck_path: str | None = None
    healthcheck_timeout: float = HEALTH_TIMEOUT_SECS
    depends_on: list[str] = field(default_factory=list)
    compose_file: str | None = None
    compose_service: str | None = None
    compose_port: int | None = None
    docker_context: str | None = None
    description: str = ""


@dataclass
class Manifest:
    project: str
    services: dict[str, Service]
    scopes: dict[str, list[str]]
    path: Path
    base_services: dict[str, Service] = field(default_factory=dict)
    modes: dict[str, dict[str, Service]] = field(default_factory=dict)
    default_mode: str | None = None
    active_mode: str | None = None
    explicit_scopes: dict[str, list[str]] = field(default_factory=dict)

    def resolve_services(self, names: Iterable[str]) -> list[str]:
        """Return the given services plus their transitive dependencies, in start order."""
        ordered: list[str] = []
        for name in names:
            self._visit(name, ordered, set())
        return ordered

    def resolve_scope(self, scope: str) -> list[str]:
        """Return the scope's services plus their transitive dependencies, in start order."""
        return self.resolve_services(self._members(scope))

    def teardown_scope(self, scope: str) -> list[str]:
        """Return only the scope's declared services, in reverse start order.

        Dependencies pulled in implicitly by ``up`` are not torn down here: another
        scope may still need them, and ``down`` refuses to break a running dependent.
        """
        declared = set(self._members(scope))
        return [name for name in reversed(self.resolve_scope(scope)) if name in declared]

    def dependents(self, name: str) -> list[str]:
        return [
            other for other, service in self.services.items() if name in service.depends_on
        ]

    def _members(self, scope: str) -> list[str]:
        if scope not in self.scopes:
            known = ", ".join(sorted(self.scopes))
            raise manifest_error(f"unknown scope {scope!r}; manifest declares {known}")
        return list(self.scopes[scope])

    def _visit(self, name: str, ordered: list[str], seen: set[str]) -> None:
        if name in ordered:
            return
        if name in seen:
            raise manifest_error(f"dependency cycle through service {name!r}")
        seen.add(name)
        for dependency in self.services[name].depends_on:
            self._visit(dependency, ordered, seen)
        ordered.append(name)

    def for_mode(self, mode_name: str | None = None) -> Manifest:
        if not self.modes:
            return self
        target_mode = mode_name or self.default_mode or next(iter(self.modes.keys()))
        if target_mode not in self.modes:
            known = ", ".join(sorted(self.modes.keys()))
            raise manifest_error(
                f"unknown mode {target_mode!r}; manifest declares modes: {known}"
            )
        mode_services = dict(self.base_services)
        mode_services.update(self.modes[target_mode])

        derived_scopes: dict[str, list[str]] = {
            "full": list(mode_services.keys()),
            "local": list(mode_services.keys()),
        }
        for sname, s in mode_services.items():
            derived_scopes[sname] = [sname]
            for alias in s.aliases:
                derived_scopes[alias] = [sname]

        for sc_name, members in self.explicit_scopes.items():
            valid_members = [m for m in members if m in mode_services]
            if valid_members:
                derived_scopes[sc_name] = valid_members

        m = Manifest(
            project=self.project,
            services=mode_services,
            scopes=derived_scopes,
            path=self.path,
            base_services=self.base_services,
            modes=self.modes,
            default_mode=self.default_mode,
            active_mode=target_mode,
            explicit_scopes=self.explicit_scopes,
        )
        for scope in derived_scopes:
            m.resolve_scope(scope)
        return m


def _parse_service(name: str, raw_spec: Any) -> Service:
    if not isinstance(raw_spec, dict):
        raise manifest_error(f"service {name!r} must be a JSON object")
    spec = dict(raw_spec)
    kind = spec.get("type")
    if kind not in SERVICE_TYPES:
        raise manifest_error(
            f"service {name!r} has unknown type {kind!r}; expected one of {SERVICE_TYPES}"
        )

    if "health" in spec:
        if "healthcheck_path" in spec and spec["health"] != spec["healthcheck_path"]:
            raise manifest_error(
                f"service {name!r} defines conflicting 'health' and 'healthcheck_path'"
            )
        spec["healthcheck_path"] = spec.pop("health")

    if "env" in spec:
        if not isinstance(spec["env"], dict) or not all(isinstance(k, str) for k in spec["env"]):
            raise manifest_error(f"service {name!r} 'env' must be a JSON object mapping strings to values")
    if "env_files" in spec:
        if not isinstance(spec["env_files"], list) or not all(isinstance(f, str) for f in spec["env_files"]):
            raise manifest_error(f"service {name!r} 'env_files' must be a list of strings")
    if "depends_on" in spec:
        if not isinstance(spec["depends_on"], list) or not all(isinstance(d, str) for d in spec["depends_on"]):
            raise manifest_error(f"service {name!r} 'depends_on' must be a list of strings")
    if "aliases" in spec:
        if not isinstance(spec["aliases"], list) or not all(isinstance(a, str) for a in spec["aliases"]):
            raise manifest_error(f"service {name!r} 'aliases' must be a list of strings")
    if "inherit" in spec:
        if not isinstance(spec["inherit"], list) or not all(isinstance(i, str) for i in spec["inherit"]):
            raise manifest_error(f"service {name!r} 'inherit' must be a list of strings")
    if "healthcheck_timeout" in spec:
        timeout = spec["healthcheck_timeout"]
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise manifest_error(
                f"service {name!r} 'healthcheck_timeout' must be a positive finite number"
            )
    if "healthcheck_path" in spec and spec["healthcheck_path"] is not None:
        health_path = spec["healthcheck_path"]
        if not isinstance(health_path, str) or not health_path:
            raise manifest_error(
                f"service {name!r} 'healthcheck_path' must be a non-empty string"
            )
        if not health_path.startswith("/"):
            raise manifest_error(
                f"service {name!r} 'healthcheck_path' must start with '/'"
            )
    if "cwd" in spec:
        if not isinstance(spec["cwd"], str):
            raise manifest_error(f"service {name!r} 'cwd' must be a string")

    raw_cmd = spec.get("command")
    if isinstance(raw_cmd, str):
        cmd_str = raw_cmd.strip()
        if not cmd_str:
            raise manifest_error(f"service {name!r} 'command' string cannot be empty")
        if "\0" in cmd_str:
            raise manifest_error(f"service {name!r} 'command' contains NUL characters")
        try:
            tokens = shlex.split(cmd_str, comments=False, posix=True)
        except ValueError as exc:
            raise manifest_error(f"service {name!r} invalid command syntax: {exc}") from None
        if not tokens:
            raise manifest_error(f"service {name!r} 'command' cannot be empty")
        spec["command"] = tokens
    elif isinstance(raw_cmd, list):
        if not all(isinstance(t, str) for t in raw_cmd):
            raise manifest_error(f"service {name!r} 'command' must be a list of strings")
    elif raw_cmd is not None:
        raise manifest_error(f"service {name!r} 'command' must be a string or list of strings")

    known = {f.name for f in Service.__dataclass_fields__.values()} - {"name"}
    unknown = set(spec) - known
    if unknown:
        raise manifest_error(f"service {name!r} has unknown keys: {sorted(unknown)}")
    return Service(name=name, **spec)


def _validate_service_integrity(services: Mapping[str, Service]) -> None:
    for name, service in services.items():
        for dependency in service.depends_on:
            if dependency not in services:
                raise manifest_error(
                    f"service {name!r} depends on unknown service {dependency!r}"
                )
        if service.type == "fd" and not (service.command or service.app):
            raise manifest_error(f"service {name!r} needs a 'command' or an 'app'")
        if service.type == "port" and not service.command:
            raise manifest_error(f"service {name!r} needs a 'command'")
        if service.type == "compose" and not (service.compose_file and service.compose_service):
            raise manifest_error(
                f"service {name!r} needs 'compose_file' and 'compose_service'"
            )


def load_manifest(path: Path) -> Manifest:
    """Read and validate a stack manifest."""
    path = Path(path)
    try:
        raw = json.loads(path.read_text())
    except OSError:
        raise StackError(f"manifest not found: {path}") from None
    except json.JSONDecodeError as exc:
        raise manifest_error(f"manifest {path} is not valid JSON: {exc}") from None
    if not isinstance(raw, dict):
        raise manifest_error(f"manifest {path} must be a JSON object")

    project = raw.get("project")
    if not isinstance(project, str) or not project:
        raise manifest_error(f"manifest {path} must declare a non-empty 'project'")

    declared = raw.get("services")
    raw_modes = raw.get("modes")

    if (declared is None or not declared) and (raw_modes is None or not raw_modes):
        raise manifest_error(f"manifest {path} must declare at least one service")

    base_services: dict[str, Service] = {}
    if declared is not None:
        if not isinstance(declared, dict):
            raise manifest_error(f"manifest {path} 'services' must be a JSON object")
        for name, raw_spec in declared.items():
            base_services[name] = _parse_service(name, raw_spec)

    modes: dict[str, dict[str, Service]] = {}
    if raw_modes is not None:
        if not isinstance(raw_modes, dict):
            raise manifest_error(f"manifest {path} 'modes' must be a JSON object")
        for mode_name, mode_obj in raw_modes.items():
            if not isinstance(mode_obj, dict):
                raise manifest_error(f"mode {mode_name!r} must be a JSON object")
            mode_svcs_raw = mode_obj.get("services")
            if not isinstance(mode_svcs_raw, dict):
                raise manifest_error(f"mode {mode_name!r} must declare a 'services' object")
            mode_svcs = {}
            for name, raw_spec in mode_svcs_raw.items():
                mode_svcs[name] = _parse_service(name, raw_spec)
            modes[mode_name] = mode_svcs

    default_mode = raw.get("default_mode")
    if default_mode and default_mode not in modes:
        raise manifest_error(f"default_mode {default_mode!r} not declared in modes: {list(modes.keys())}")

    # Determine initial services
    if modes:
        init_mode = default_mode or next(iter(modes.keys()))
        initial_services = dict(base_services)
        initial_services.update(modes[init_mode])
        active_mode = init_mode
    else:
        initial_services = dict(base_services)
        active_mode = None

    # Validate integrity of base services and each mode
    if not modes:
        _validate_service_integrity(initial_services)
    else:
        for m_name, m_svcs in modes.items():
            combined = dict(base_services)
            combined.update(m_svcs)
            _validate_service_integrity(combined)

    derived_scopes: dict[str, list[str]] = {
        "full": list(initial_services.keys()),
        "local": list(initial_services.keys()),
    }
    for sname, s in initial_services.items():
        derived_scopes[sname] = [sname]
        for alias in s.aliases:
            if not isinstance(alias, str) or not alias:
                raise manifest_error(f"service {sname!r} has invalid alias {alias!r}")
            if alias in initial_services and alias != sname:
                raise manifest_error(
                    f"alias {alias!r} for service {sname!r} conflicts with another service"
                )
            derived_scopes[alias] = [sname]

    explicit_scopes: dict[str, list[str]] = {}
    scopes_raw = raw.get("scopes")
    if scopes_raw is not None:
        if not isinstance(scopes_raw, dict):
            raise manifest_error(f"manifest {path} 'scopes' must be a JSON object")
        for scope, members in scopes_raw.items():
            if not isinstance(members, list):
                raise manifest_error(f"scope {scope!r} must be a list of service names")
            for member in members:
                if not isinstance(member, str):
                    raise manifest_error(
                        f"scope {scope!r} must list service names as strings, got {member!r}"
                    )
                if member not in initial_services:
                    raise manifest_error(f"scope {scope!r} names unknown service {member!r}")
            derived_scopes[scope] = list(members)
            explicit_scopes[scope] = list(members)

    manifest = Manifest(
        project=project,
        services=initial_services,
        scopes=derived_scopes,
        path=path,
        base_services=base_services,
        modes=modes,
        default_mode=default_mode,
        active_mode=active_mode,
        explicit_scopes=explicit_scopes,
    )

    if modes:
        for m_name in modes:
            manifest.for_mode(m_name)
    else:
        for scope in derived_scopes:
            manifest.resolve_scope(scope)

    return manifest


# --------------------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------------------


def _get_project_name(root: Path) -> str:
    root = Path(root).resolve()
    candidates = (
        root / "rig.json",
        root / "scripts" / "rig.json",
        root / ".config" / "rig.json",
        root / "stack.json",
        root / "scripts" / "stack.json",
        root / ".config" / "stack.json",
    )
    for cand in candidates:
        if cand.is_file():
            try:
                data = json.loads(cand.read_text())
                if isinstance(data, dict) and data.get("project"):
                    return str(data["project"])
            except Exception:
                pass
    return root.name or "stack"


def _sync_state_symlink(local_state: Path, authoritative_state: Path) -> None:
    try:
        if local_state.is_symlink():
            if local_state.resolve() != authoritative_state.resolve():
                local_state.unlink()
                local_state.symlink_to(authoritative_state)
        elif local_state.is_file():
            if not authoritative_state.exists():
                shutil.copy2(local_state, authoritative_state)
            local_state.unlink()
            local_state.symlink_to(authoritative_state)
        elif not local_state.exists():
            local_state.symlink_to(authoritative_state)
    except OSError:
        pass


def _state_path(root: Path | str, instance: str | None = None) -> Path:
    root_path = Path(root).resolve()
    if instance is None:
        proj = _get_project_name(root_path)
        instance = instance_id(proj, root_path)
    inst_dir = ensure_instance_dir(instance)
    authoritative_state = inst_dir / STATE_FILE_NAME

    runtime = root_path / RUNTIME_DIR_NAME
    if runtime.is_dir():
        local_state = runtime / STATE_FILE_NAME
        _sync_state_symlink(local_state, authoritative_state)
    return authoritative_state


def _lock_path(target: Path | str, instance: str | None = None) -> Path:
    if isinstance(target, str) and "/" not in target and "\\" not in target:
        return ensure_instance_dir(target) / LOCK_FILE_NAME
    if instance is not None:
        return ensure_instance_dir(instance) / LOCK_FILE_NAME
    root_path = Path(target).resolve()
    proj = _get_project_name(root_path)
    inst = instance_id(proj, root_path)
    return ensure_instance_dir(inst) / LOCK_FILE_NAME



def record_alive(record: Mapping[str, Any], root: Path) -> bool:
    if record.get("type") == "compose":
        return compose_record_alive(record, root)
    return identity_matches(record)


def record_status(record: Mapping[str, Any], root: Path) -> str:
    """Return 'running', 'stopped' or 'error' for one recorded service.

    A record is retained in state whenever teardown could not prove the service
    was reclaimed, so presence in state is never evidence that it still runs.
    'error' means the question could not be answered -- a Docker outage above
    all -- and never that the service is gone.
    """
    if record.get("type") == "compose":
        status = compose_record_status(record, root)
        if status == "alive":
            return "running"
        return "error" if status == "error" else "stopped"
    return "running" if identity_matches(record) else "stopped"


def prune_state(state: dict[str, Any], root: Path) -> list[str]:
    """Drop records whose ownership can no longer be established. Returns their names.

    A pruned record whose port is still occupied means something is running that this
    checkout can no longer claim. That is reported loudly rather than killed: the
    port may now belong to an unrelated process.
    """
    dropped = []
    for name, record in list(state["services"].items()):
        stype = record.get("type")
        if stype == "compose":
            status = compose_record_status(record, root)
            if status == "absent":
                dropped.append(name)
        else:
            is_pid_alive = identity_matches(record)
            is_pgid_alive = isinstance(record.get("pgid"), int) and pgid_alive(record["pgid"])
            if not is_pid_alive and not is_pgid_alive:
                dropped.append(name)
    for name in dropped:
        record = state["services"].pop(name, None) or {}
        port = record.get("port")
        if port and not port_is_free(int(port)):
            print(
                f"  warning: {name} is no longer verifiable but port {port} is still in "
                f"use (pid {record.get('pid')} may be orphaned); inspect it manually",
                file=sys.stderr,
            )
    return dropped


def _values_for(state: Mapping[str, Any], root: Path, instance: str) -> dict[str, Any]:
    values: dict[str, Any] = {
        "root": str(root),
        "instance": instance,
        "data_dir": str(Path(root) / "data"),
        "python": sys.executable,
    }
    for name, record in state["services"].items():
        if record.get("port"):
            values[f"{name}_port"] = record["port"]
            values[f"{name}_url"] = record.get("url") or f"http://127.0.0.1:{record['port']}"
    return values


def _start_service(
    service: Service,
    root: Path,
    runtime: Path,
    instance: str,
    values: Mapping[str, Any],
) -> dict[str, Any]:
    cwd = (Path(root) / service.cwd).resolve()
    if not cwd.is_dir():
        raise StackError(f"service {service.name!r} working directory {cwd} does not exist")
    log_path = runtime / LOG_DIR_NAME / f"{service.name}.log"

    if service.type in ("port", "fd") and not shutil.which("lsof"):
        raise RigError(
            "'lsof' is required for port/fd service verification but is not found on PATH",
            code="E_EXTERNAL_TOOL",
            exit_code=EXIT_EXTERNAL_TOOL,
            hint="install lsof (macOS: preinstalled; Debian/Ubuntu: 'apt install lsof')",
        )

    svc_values = dict(values)
    svc_values["cwd"] = str(cwd)

    env = build_service_env(
        service.env, service.inherit, Path(root), svc_values, service.env_files
    )

    if service.type == "compose":
        # Compose is handed the declared environment too, so `env`, `env_files`
        # and manifest interpolation reach the compose file and its containers
        # instead of being silently dropped.
        return _start_compose_service(service, root, instance, env=env)

    if service.type == "fd":
        raw_argv = service.command or uvicorn_argv(
            python=str(render(service.python or sys.executable, svc_values)),
            app=service.app or "",
            factory=service.factory,
        )
        record = spawn_fd_service(service.name, raw_argv, cwd, env, log_path, values=svc_values)
    else:
        record = spawn_port_service(
            service.name, service.command, cwd, env, log_path, reserve_port(), values=svc_values
        )
    record["log"] = str(log_path)
    record["env"] = redact(env)
    record["depends_on"] = list(service.depends_on)
    record["health"] = service.healthcheck_path
    record["healthcheck_path"] = service.healthcheck_path
    return record


def _start_compose_service(
    service: Service,
    root: Path,
    instance: str,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Start one Compose service, leaving no container this rig cannot account for.

    ``up`` can succeed while discovery of the container or its published port
    fails. The container is then running and untracked, so it is stopped and
    removed here; when that cleanup itself fails the partial record travels on
    the raised error so the caller can persist it for ``down`` or ``prune``. An
    interrupt is treated exactly the same way: a container this rig created is
    never left both running and unrecorded.

    The Docker endpoint in force at startup -- both the host and the active
    context -- is recorded, so every later status query and teardown reaches the
    daemon that actually holds the container even after the machine-wide default
    context changes.
    """
    compose_file = Path(root) / str(service.compose_file)
    docker_host = os.environ.get("DOCKER_HOST")

    # The declared environment is an allowlist, so the Docker client settings
    # that decide which daemon answers are added back explicitly: a TLS or
    # rootless setup must still reach its own daemon.
    cmd_env: dict[str, str] | None = None
    if env is not None:
        cmd_env = dict(env)
        for name in DOCKER_CLIENT_ENV_PASSTHROUGH:
            if name not in cmd_env and name in os.environ:
                cmd_env[name] = os.environ[name]

    # Docker's own order of precedence decides which daemon holds this
    # container, and the record must name the same one: a context the manifest
    # declares, then the ambient ``DOCKER_CONTEXT``, then ``DOCKER_HOST``. The
    # ambient context is read from this process's environment, never from
    # ``cmd_env``: the declared environment is an allowlist that drops it, so
    # `DOCKER_CONTEXT=colima rig up` would otherwise be recorded against the
    # machine's default context and lose its container there.
    docker_context = service.docker_context or os.environ.get("DOCKER_CONTEXT") or None
    if docker_context is None and not docker_host:
        # Nothing names the daemon, so the container would be reached through
        # whichever context is active at the time -- and `docker context use`
        # can change that at any moment. The active context is therefore named
        # now.
        docker_context = resolve_current_docker_context(cmd_env)
    if docker_context:
        # Every later query and teardown names this context with ``--context``,
        # which Docker ranks above ``DOCKER_HOST``. Recording a host as well
        # would name an endpoint that never decided anything and would only
        # mislead a reader of the record.
        docker_host = None

    record: dict[str, Any] = {
        "name": service.name,
        "type": "compose",
        "pid": None,
        "pgid": None,
        "instance": instance,
        "compose_file": str(compose_file),
        "compose_service": service.compose_service,
        "docker_context": docker_context,
        "docker_host": docker_host,
        "container": "",
        "port": None,
        "url": None,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "depends_on": list(service.depends_on),
        "health": service.healthcheck_path,
        "healthcheck_path": service.healthcheck_path,
    }

    if cmd_env is not None:
        # A compose file may declare a variable as required -- `${VAR:?message}`
        # -- and Compose then refuses every later `ps` and `stop` while that
        # variable is undefined, leaving its own container beyond its reach. The
        # environment that satisfied the file at startup is therefore recorded,
        # with secret-looking values masked exactly as they are for a process
        # service: a status query and a teardown need each variable to exist,
        # not to carry its real value.
        record["compose_env"] = redact(cmd_env)

    def _compose(args: list[str], timeout: float = 180.0):
        return run_compose(
            instance,
            Path(root),
            compose_file,
            args,
            docker_context,
            timeout=timeout,
            env=cmd_env,
            docker_host=docker_host,
        )

    def _cleanup_partial() -> bool:
        """Return True only when the partially started container is gone.

        An interrupt during the cleanup itself is caught and reported as an
        incomplete reclaim: the caller must be able to record the container it
        could not remove rather than lose it.
        """
        removed = True
        for args in (["stop", str(service.compose_service)], ["rm", "-f", str(service.compose_service)]):
            try:
                result = _compose(args, timeout=30.0)
            except BaseException:  # noqa: BLE001 - an interrupt must not lose the container
                return False
            if result.returncode != 0:
                removed = False
        return removed

    def _stranded(message: str) -> RigError:
        """Publish a record for a container this rig created but cannot reclaim."""
        return RigError(
            f"{message}; the partial container could not be removed and stays recorded",
            code="E_COMPOSE_FAILED",
            exit_code=EXIT_OP_FAILED,
            hint="run 'rig down' or 'rig prune --force' to reclaim it",
            details={"partial_record": record},
        )

    def _fail(message: str) -> RigError:
        if _cleanup_partial():
            return RigError(message, code="E_COMPOSE_FAILED", exit_code=EXIT_OP_FAILED)
        return _stranded(message)

    try:
        result = _compose(["up", "-d", "--no-deps", "--wait", str(service.compose_service)])
    except (RigError, OSError) as exc:
        # `up` can time out with containers already created, so reclaim them.
        # A reclaim that fails must publish the record: the timeout itself
        # carries no ownership evidence, and the container would be stranded.
        if not _cleanup_partial():
            raise _stranded(f"compose could not start {service.name!r}: {exc}") from exc
        raise
    except BaseException as exc:
        # `--wait` blocks until the container reports healthy, so a `Ctrl-C`
        # lands here with that container already created and not yet recorded.
        # It is reclaimed, or recorded so a later teardown can reclaim it; only
        # a proven reclaim lets the interrupt travel on untouched.
        if not _cleanup_partial():
            raise _stranded(
                f"the start of {service.name!r} was interrupted by {type(exc).__name__}"
            ) from exc
        raise
    if result.returncode != 0:
        raise _fail(
            f"compose could not start {service.name!r}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )

    # `up` returned, so a container now exists. Every failure from here on --
    # an interrupt above all -- must either reclaim that container or record it.
    try:
        try:
            ids = _compose(["ps", "-q", str(service.compose_service)], timeout=60.0)
        except (RigError, OSError) as exc:
            raise _fail(f"compose failed to list containers for {service.name!r}: {exc}") from exc
        if ids.returncode != 0:
            raise _fail(
                f"compose failed to list containers for {service.name!r}: "
                f"{ids.stderr.strip() or ids.stdout.strip()}"
            )
        record["container"] = (
            ids.stdout.strip().splitlines()[0].strip() if ids.stdout.strip() else ""
        )
        if not record["container"]:
            raise _fail(f"compose reported no container for {service.name!r}")

        if service.compose_port:
            try:
                published = _compose(
                    ["port", str(service.compose_service), str(service.compose_port)],
                    timeout=60.0,
                )
                if published.returncode != 0:
                    raise StackError(published.stderr.strip() or published.stdout.strip() or "no output")
                port = parse_compose_port(published.stdout)
            except Exception as exc:
                raise _fail(
                    f"compose failed to resolve port for {service.name!r}: {exc}"
                ) from exc
            record["port"] = port
            record["url"] = f"http://127.0.0.1:{port}"
    except RigError:
        # Already handled: cleanup was attempted, and the record travels on the
        # error whenever that cleanup could not finish.
        raise
    except BaseException as exc:
        # A `KeyboardInterrupt` or `SystemExit` between `up` and a complete
        # record: reclaim the container, or record it and let the caller persist
        # it. Only a proven reclaim lets the interrupt travel on untouched.
        if not _cleanup_partial():
            raise _stranded(
                f"discovery for {service.name!r} was interrupted by {type(exc).__name__}"
            ) from exc
        raise
    return record


def _stop_record(record: Mapping[str, Any], root: Path, remove: bool = True) -> str:
    """Stop one recorded service and, by default, reclaim its container outright.

    Every caller drops the ownership record once this reports success, and a
    Compose ``stop`` leaves an exited container behind, so a record dropped after
    a mere stop would strand its container beyond the reach of every later
    prune. A caller that keeps its record can pass ``remove=False``. Volumes are
    never removed, so local data survives either path.

    Compose refusing the teardown must not end it. A compose file that declares
    a required variable cannot be evaluated while that variable is undefined, so
    Compose can never reclaim the container it started; Docker holds that
    container and its Compose labels and finishes the job without the file.
    """
    if record.get("type") == "compose":
        status = compose_record_status(record, root)
        if status == "absent":
            return "stale"
        if status == "error":
            return "failed"
        if not compose_file_present(record, root):
            # The checkout is gone, so Compose cannot be scoped to it. Docker
            # still holds the container and can stop and reclaim it by ID.
            return docker_record_stop(record, remove)
        instance = str(record["instance"])
        compose_file = Path(str(record["compose_file"]))
        compose_service = str(record["compose_service"])
        context, docker_host = record_docker_endpoint(record)
        compose_env = record_compose_env(record)

        def _compose_teardown(args: list[str]) -> bool:
            """Return ``True`` only when Compose itself carried out one teardown step."""
            try:
                result = run_compose(
                    instance, Path(root), compose_file, args, context,
                    env=compose_env, docker_host=docker_host,
                )
            except (StackError, RigError):
                return False
            return result.returncode == 0

        # Volumes are preserved throughout: an ordinary `down` must not destroy local data.
        if remove or not record.get("container"):
            # Either the caller is about to drop the record, or no container ID
            # was ever recorded, so nothing else can track this container. It is
            # reclaimed by service name and removed outright.
            for args in (["stop", compose_service], ["rm", "-f", compose_service]):
                if not _compose_teardown(args):
                    # Compose refused, and a compose file it cannot evaluate --
                    # one declaring a required variable above all -- it will
                    # never evaluate. Docker holds the container and its Compose
                    # labels, needs no compose file, and reclaims every replica
                    # of the service, so the reclaim finishes there.
                    return docker_record_stop(record, remove)
            return "terminated"
        if _compose_teardown(["stop", compose_service]):
            return "terminated"
        return docker_record_stop(record, remove)
    return terminate_record(record, TEARDOWN_TIMEOUT_SECS)


def is_service_verifiable_alive(record: Mapping[str, Any], root: Path) -> bool:
    """Return ``True`` when a recorded service is verifiably alive and running."""
    if record.get("type") == "compose":
        return compose_record_alive(record, root)
    pid = record.get("pid")
    if not isinstance(pid, int):
        return False
    return pid_alive(pid) and identity_matches(record)


def is_service_active_in_mode(record: Mapping[str, Any], root: Path) -> bool:
    """Return ``True`` when a recorded service still occupies the active mode.

    A Compose service holds its ports and container name until Docker reports it
    gone, so an inspection error keeps the mode occupied. A process service is
    held by its PID or by its surviving process group.
    """
    if record.get("type") == "compose":
        return compose_record_status(record, root) != "absent"
    return (
        is_service_verifiable_alive(record, root)
        or (isinstance(record.get("pgid"), int) and pgid_alive(record["pgid"]))
        or (isinstance(record.get("pid"), int) and pid_alive(record["pid"]))
    )


def cmd_up(
    root: Path,
    manifest_path: Path,
    scope: str = "full",
    mode: str | None = None,
    switch: bool = False,
    as_json: bool = False,
) -> int:
    raw_manifest = load_manifest(manifest_path)
    if mode or raw_manifest.modes:
        manifest = raw_manifest.for_mode(mode)
        selected_mode = manifest.active_mode or "default"
    else:
        manifest = raw_manifest
        selected_mode = "default"

    # The scope is validated first: an unknown scope is a usage error and must
    # never be discovered after a mode switch has already torn the stack down.
    order = manifest.resolve_scope(scope)

    root = Path(root).resolve()
    runtime = ensure_runtime_dir(root)
    instance = instance_id(manifest.project, root)
    state_path = _state_path(root, instance=instance)
    lock_path = _lock_path(root, instance=instance)

    with exclusive_lock(lock_path, LOCK_TIMEOUT_SECS):
        state = read_state(state_path)
        state["instance"] = instance
        state["project"] = manifest.project
        state["root"] = str(root)
        state["boot_id"] = get_boot_id()

        current_mode = state.get("mode")
        # A dead leader PID does not mean the service is gone: its process group
        # or the PID itself may still hold the ports the other mode needs.
        active_count = sum(
            1
            for rec in state.get("services", {}).values()
            if is_service_active_in_mode(rec, root)
        )
        if current_mode and current_mode != selected_mode and active_count > 0:
            if not switch:
                raise RigError(
                    f"stack is currently running in mode {current_mode!r}; "
                    f"cannot start in mode {selected_mode!r} without switching.",
                    code="E_MODE_CONFLICT",
                    exit_code=EXIT_MUTEX_CONFLICT,
                    hint=f"run 'rig up --mode {selected_mode} --switch' to stop the active mode and switch",
                )
            if not as_json:
                print(
                    f"  switching mode from {current_mode!r} to {selected_mode!r}: "
                    f"stopping active services..."
                )
            services = dict(state.get("services", {}))
            stop_order = reverse_dependency_order(services)
            switch_failed = []
            failed_services: set[str] = set()
            for sname in stop_order:
                deps_failed = [
                    d for d in failed_services
                    if sname in services[d].get("depends_on", [])
                ]
                if deps_failed:
                    switch_failed.append(f"{sname}: refused (needed by {', '.join(deps_failed)})")
                    failed_services.add(sname)
                    continue
                srec = services[sname]
                outcome = _stop_record(srec, root)
                if outcome in ("terminated", "killed", "stale"):
                    state["services"].pop(sname, None)
                else:
                    failed_services.add(sname)
                    switch_failed.append(f"{sname}: {outcome}")

            write_state(state_path, state)
            if switch_failed:
                code_val = EXIT_REFUSED if any("refused" in f for f in switch_failed) else EXIT_OP_FAILED
                err = RigError(
                    f"failed to stop active mode {current_mode!r} during switch: {', '.join(switch_failed)}",
                    code="E_SWITCH_FAILED",
                    exit_code=code_val,
                    hint="inspect running services with 'rig status' or stop them manually before switching modes",
                )
                if as_json:
                    raise err
                print(f"rig: error [{err.code}]: {err.message}", file=sys.stderr)
                if err.hint:
                    print(f"  hint: {err.hint}", file=sys.stderr)
                return err.exit_code

        state["mode"] = selected_mode
        for name in prune_state(state, root):
            if not as_json:
                print(f"  pruned stale record for {name}")
        write_state(state_path, state)

        # If any dependency is down or missing, stop running dependents so they re-link.
        # Affected dependents must be stopped in reverse dependency order (dependents before dependencies).
        missing = [
            name
            for name in order
            if name not in state["services"]
            or not is_service_verifiable_alive(state["services"][name], root)
        ]
        # Consumers are collected from the manifest *and* from the recorded state:
        # a service the current mode or scope no longer declares can still be
        # running against the port the restart is about to move.
        affected: set[str] = set()
        queue = list(missing)
        seen = set(missing)
        while queue:
            curr = queue.pop(0)
            for dep in _consumers_of(curr, state["services"], manifest):
                if dep in state["services"]:
                    affected.add(dep)
                if dep not in seen:
                    seen.add(dep)
                    queue.append(dep)

        if affected:
            stop_order = reverse_dependency_order(
                {
                    name: {"depends_on": sorted(_merged_depends_on(name, state["services"][name], manifest))}
                    for name in affected
                }
            )
            failed_stops: set[str] = set()
            for dep_name in stop_order:
                if any(
                    child in failed_stops
                    for child in _consumers_of(dep_name, state["services"], manifest)
                ):
                    if not as_json:
                        print(
                            f"  {dep_name}: preserving because dependent failed to stop",
                            file=sys.stderr,
                        )
                    failed_stops.add(dep_name)
                    continue
                dep_record = state["services"][dep_name]
                if not as_json:
                    print(f"  {dep_name}: stopping to re-link against missing dependencies")
                outcome = _stop_record(dep_record, root)
                if outcome not in ("terminated", "killed", "stale"):
                    if not as_json:
                        print(
                            f"  {dep_name}: cleanup failed ({outcome}); preserving record in state",
                            file=sys.stderr,
                        )
                    failed_stops.add(dep_name)
                else:
                    state["services"].pop(dep_name, None)
                    write_state(state_path, state)
            if failed_stops:
                err = RigError(
                    f"cleanup failed for dependent services: {', '.join(failed_stops)}",
                    code="E_CLEANUP_FAILED",
                    exit_code=EXIT_OP_FAILED,
                )
                if as_json:
                    raise err
                return err.exit_code
            restartable = [name for name in affected if name in manifest.services]
            for name in sorted(affected - set(restartable)):
                if not as_json:
                    print(
                        f"  {name}: stopped and dropped from state; "
                        f"the active manifest no longer declares it",
                        file=sys.stderr,
                    )
            order = manifest.resolve_services(order + restartable)

        started: list[str] = []
        for name in order:
            service = manifest.services[name]
            existing = state["services"].get(name)
            if existing is not None and is_service_verifiable_alive(existing, root):
                if not as_json:
                    print(f"  {name}: already running on {existing.get('url') or 'n/a'}")
                continue
            if existing is not None:
                # `name` is scheduled to start, so a record that is merely stopped
                # is not a conflict: it is exactly what this start replaces. An
                # exited Compose container is removed first so the new one cannot
                # collide with it. Only a reclaim this rig cannot complete is
                # fatal, and it keeps the record for `down` or `prune` to retry.
                if not as_json:
                    print(f"  {name}: recorded but not running; reclaiming before start")
                outcome = _stop_record(existing, root, remove=True)
                if outcome not in ("terminated", "killed", "stale"):
                    _rollback(state, state_path, started, root, manifest)
                    err = RigError(
                        f"service {name!r} is recorded in state and could not be reclaimed "
                        f"before start ({outcome}); cannot proceed",
                        code="E_SERVICE_UNHEALTHY",
                        exit_code=EXIT_OP_FAILED,
                        hint="run 'rig down' to clear stale services, or 'rig status' to inspect",
                    )
                    if as_json:
                        raise err
                    print(f"rig: error [{err.code}]: {err.message}", file=sys.stderr)
                    if err.hint:
                        print(f"  hint: {err.hint}", file=sys.stderr)
                    return err.exit_code
                state["services"].pop(name, None)
                write_state(state_path, state)
            try:
                record = _start_with_retry(
                    service, root, runtime, instance, state, state_path
                )
            except (StackError, RigError) as exc:
                _rollback(state, state_path, started, root, manifest)
                if as_json:
                    if isinstance(exc, RigError):
                        raise
                    raise RigError(
                        f"failed to start {name}: {exc}",
                        code="E_START_FAILED",
                        exit_code=EXIT_OP_FAILED,
                    ) from None
                if isinstance(exc, RigError):
                    print(f"rig: error [{exc.code}]: {exc.message}", file=sys.stderr)
                    return exc.exit_code
                if not as_json:
                    print(f"  {name}: {exc}", file=sys.stderr)
                return EXIT_OP_FAILED
            except BaseException:
                # `KeyboardInterrupt` and `SystemExit`: every service started so
                # far, and any partial container the failure published, is
                # written to state before the interrupt travels on. Nothing is
                # rolled back here -- a reclaim needs further subprocess work
                # that the same interrupt would cut short, and a dropped record
                # would strand whatever it owns.
                write_state(state_path, state)
                raise
            if record is None:
                _rollback(state, state_path, started, root, manifest)
                err = RigError(
                    f"service {name!r} failed to reach healthy state after start attempts",
                    code="E_START_TIMEOUT",
                    exit_code=EXIT_OP_FAILED,
                    hint=f"check service logs in .local-run/logs/{name}.log",
                )
                if as_json:
                    raise err
                print(f"rig: error [{err.code}]: {err.message}", file=sys.stderr)
                return err.exit_code
            started.append(name)
            if not as_json:
                print(f"  {name}: up on {record.get('url') or 'n/a'}")

        state["generation"] = int(state.get("generation", 0)) + 1
        write_state(state_path, state)

    if as_json:
        print_json_envelope(
            "up",
            {
                "instance": instance,
                "project": manifest.project,
                "mode": selected_mode,
                "generation": state.get("generation", 0),
                "services": state.get("services", {}),
            },
        )
    else:
        _print_status(manifest, read_state(state_path), root)
    return EXIT_OK


def _start_with_retry(
    service: Service,
    root: Path,
    runtime: Path,
    instance: str,
    state: dict[str, Any],
    state_path: Path,
) -> dict[str, Any] | None:
    """Start one service, retrying a port collision with a freshly allocated port.

    Only ``port`` services can collide: an ``fd`` service is handed a socket that was
    never released, and a ``compose`` host port is allocated by the Docker daemon. A
    strict-port service that fails to come up is therefore retried rather than left
    to drift onto a port another project may own.
    """
    attempts = PORT_RETRY_ATTEMPTS if service.type == "port" else 1
    for attempt in range(1, attempts + 1):
        values = _values_for(state, root, instance)
        try:
            record = _start_service(service, root, runtime, instance, values)
        except BaseException as exc:
            # A start that leaked something this rig could not clean up publishes
            # the evidence, so `down` and `prune` can still reach it. An
            # interrupt takes the same path: the record is written before the
            # exception travels on.
            partial = exc.details.get("partial_record") if isinstance(exc, RigError) else None
            if isinstance(partial, dict):
                state["services"][service.name] = partial
                write_state(state_path, state)
            raise
        # State is published before readiness is confirmed so an interrupted startup
        # still leaves ownership evidence for the next `down`.
        state["services"][service.name] = record
        write_state(state_path, state)

        if _await_ready(service, record, root):
            return record

        outcome = _stop_record(record, root)
        if outcome in ("terminated", "killed", "stale"):
            state["services"].pop(service.name, None)
            write_state(state_path, state)
        else:
            print(
                f"  {service.name}: cleanup failed ({outcome}); preserving record in state",
                file=sys.stderr,
            )
            return None
        if attempt < attempts:
            print(
                f"  {service.name}: did not come up on port {record.get('port')}; "
                f"retrying with a new port",
                file=sys.stderr,
            )
        else:
            print(
                f"  {service.name}: did not become ready on port {record.get('port')} "
                f"after {attempts} attempt(s); see {record.get('log')}",
                file=sys.stderr,
            )
    return None


def _await_ready(
    service: Service, record: Mapping[str, Any], root: Path = Path(".")
) -> bool:
    pid = record.get("pid")
    pgid = record.get("pgid")
    if isinstance(pid, int) and not pid_alive(pid):
        return False
    port = record.get("port")
    port_int = (
        int(port)
        if (isinstance(port, int) or (isinstance(port, str) and str(port).isdigit()))
        else None
    )
    if service.healthcheck_path and port_int is not None:
        ok = wait_for_http(
            port_int,
            service.healthcheck_path,
            service.healthcheck_timeout,
            pid=pid if isinstance(pid, int) else None,
            pgid=pgid if isinstance(pgid, int) else None,
        )
        if not ok:
            return False
        if service.type == "compose":
            return compose_record_alive(record, root)
        if not port_listener_matches(
            port_int,
            pgid=pgid if isinstance(pgid, int) else None,
            pid=pid if isinstance(pid, int) else None,
        ):
            return False
        if isinstance(pid, int):
            return pid_alive(pid) and identity_matches(record)
        return True
    if service.type == "compose":
        return compose_record_alive(record, root)
    if isinstance(pid, int):
        # No health path declared: confirm the process survived its own startup.
        time.sleep(0.3)
        if port_int is not None and not port_listener_matches(
            port_int,
            pgid=pgid if isinstance(pgid, int) else None,
            pid=pid if isinstance(pid, int) else None,
        ):
            return False
        return pid_alive(pid) and identity_matches(record)
    return True


def _rollback(
    state: dict[str, Any],
    state_path: Path,
    started: Sequence[str],
    root: Path,
    manifest: Manifest,
) -> None:
    """Undo only what this operation created, leaving pre-existing services alone."""
    failed_services: set[str] = set()
    for name in reversed(list(started)):
        record = state["services"].get(name)
        if record is None:
            continue
        dependents_failed = [
            dep
            for dep in manifest.dependents(name)
            if dep in failed_services or dep in state["services"]
        ]
        if dependents_failed:
            print(
                f"  rollback of {name} skipped: dependent(s) {', '.join(dependents_failed)} are still active",
                file=sys.stderr,
            )
            continue
        outcome = _stop_record(record, root)
        if outcome in ("terminated", "killed", "stale"):
            state["services"].pop(name, None)
        else:
            failed_services.add(name)
            print(f"  rollback of {name} returned {outcome}", file=sys.stderr)
        write_state(state_path, state)


def record_depends_on(record: Mapping[str, Any]) -> list[str]:
    """Return the dependencies a state record declares, ignoring malformed entries."""
    raw = record.get("depends_on")
    if not isinstance(raw, (list, tuple, set)):
        return []
    seen: list[str] = []
    for item in raw:
        if isinstance(item, str) and item not in seen:
            seen.append(item)
    return seen


def _merged_depends_on(
    name: str, record: Mapping[str, Any], manifest: Manifest | None = None
) -> set[str]:
    """Return every dependency of ``name`` known to the record or the manifest."""
    deps = set(record_depends_on(record))
    if manifest is not None and name in manifest.services:
        deps.update(manifest.services[name].depends_on)
    return deps - {name}


def _consumers_of(
    name: str,
    services: Mapping[str, Mapping[str, Any]],
    manifest: Manifest | None = None,
) -> set[str]:
    """Return every service that depends on ``name``, recorded or declared."""
    consumers = {
        other
        for other, record in services.items()
        if name in _merged_depends_on(other, record, manifest)
    }
    if manifest is not None:
        consumers.update(manifest.dependents(name))
    return consumers - {name}


def reverse_dependency_order(services: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """Compute reverse topological order (dependents before dependencies) for stopping."""
    in_degree: dict[str, int] = {k: 0 for k in services}
    dependents_map: dict[str, set[str]] = {k: set() for k in services}

    for name, rec in services.items():
        # A duplicate entry must not raise the in-degree twice: the decrement below
        # happens once per edge, so a double count would strand the dependency.
        deps = {d for d in record_depends_on(rec) if d in services and d != name}
        for d in deps:
            dependents_map[name].add(d)
            in_degree[d] += 1

    queue = [k for k, deg in in_degree.items() if deg == 0]
    order: list[str] = []
    while queue:
        curr = queue.pop(0)
        order.append(curr)
        for dep in dependents_map[curr]:
            in_degree[dep] -= 1
            if in_degree[dep] == 0:
                queue.append(dep)

    for k in services:
        if k not in order:
            order.append(k)
    return order


def _stop_instance(inst_dir: Path) -> dict[str, Any]:
    lock_file = inst_dir / LOCK_FILE_NAME
    state_file = inst_dir / STATE_FILE_NAME
    if not state_file.exists():
        return {"instance": inst_dir.name, "status": "no_state", "stopped": [], "failed": []}
    with exclusive_lock(lock_file, LOCK_TIMEOUT_SECS):
        state = read_state(state_file)
        root_str = state.get("root")
        root_path = Path(root_str).resolve() if root_str else inst_dir
        services = dict(state.get("services", {}))
        stop_order = reverse_dependency_order(services)
        stopped = []
        failed = []
        failed_services: set[str] = set()

        for name in stop_order:
            rec = services[name]
            deps_failed = [
                d for d in failed_services
                if name in services[d].get("depends_on", [])
            ]
            if deps_failed:
                failed.append(f"{name}: refused (needed by running dependent {', '.join(deps_failed)})")
                failed_services.add(name)
                continue

            outcome = _stop_record(rec, root_path)
            if outcome in ("terminated", "killed", "stale"):
                state["services"].pop(name, None)
                stopped.append(name)
            else:
                failed_services.add(name)
                failed.append(f"{name}: {outcome}")

        state["generation"] = int(state.get("generation", 0)) + 1
        write_state(state_file, state)
        return {
            "instance": inst_dir.name,
            "project": state.get("project", inst_dir.name),
            "stopped": stopped,
            "failed": failed,
        }


def cmd_down(
    root: Path | None = None,
    manifest_path: Path | None = None,
    scope: str = "full",
    target: str | None = None,
    all_instances: bool = False,
    as_json: bool = False,
) -> int:
    instances_dir = get_instances_dir()

    if all_instances:
        if not instances_dir.is_dir():
            if as_json:
                print_json_envelope("down", {"instances": [], "all": True})
            else:
                print("No active rig instances to stop.")
            return EXIT_OK
        results = []
        any_failed = False
        for inst_dir in sorted(instances_dir.iterdir()):
            if not inst_dir.is_dir() or not (inst_dir / STATE_FILE_NAME).is_file():
                continue
            res = _stop_instance(inst_dir)
            if res.get("failed"):
                any_failed = True
            results.append(res)
            if not as_json:
                stopped_str = ", ".join(res["stopped"]) or "none"
                print(f"  {res['instance']} ({res['project']}): stopped {stopped_str}")
                for fail in res.get("failed", []):
                    print(f"    failed: {fail}", file=sys.stderr)
        if as_json:
            print_json_envelope(
                "down", {"instances": results, "all": True}, ok=not any_failed
            )
        return EXIT_OP_FAILED if any_failed else EXIT_OK

    if target:
        if not instances_dir.is_dir():
            raise RigError(
                f"no instance found matching {target!r}",
                code="E_NOT_FOUND",
                exit_code=EXIT_NOT_FOUND,
                hint="run 'rig ps' to view all registered instances",
            )
        matched_dirs = []
        for inst_dir in sorted(instances_dir.iterdir()):
            if not inst_dir.is_dir() or not (inst_dir / STATE_FILE_NAME).is_file():
                continue
            if inst_dir.name == target:
                matched_dirs = [inst_dir]
                break
            state = read_state(inst_dir / STATE_FILE_NAME)
            proj = state.get("project") or inst_dir.name.rsplit("-", 1)[0]
            if target.lower() == proj.lower() or target.lower() == inst_dir.name.lower():
                matched_dirs.append(inst_dir)

        if not matched_dirs:
            raise RigError(
                f"no instance found matching {target!r}",
                code="E_NOT_FOUND",
                exit_code=EXIT_NOT_FOUND,
                hint="run 'rig ps' to view all registered instances",
            )
        if len(matched_dirs) > 1:
            ids = [d.name for d in matched_dirs]
            raise RigError(
                f"ambiguous target {target!r}; matches multiple instances: {', '.join(ids)}",
                code="E_AMBIGUOUS",
                exit_code=EXIT_NOT_FOUND,
                hint="specify the exact instance ID instead of the project name",
            )
        res = _stop_instance(matched_dirs[0])
        if as_json:
            print_json_envelope("down", res, ok=not res.get("failed"))
        else:
            stopped_str = ", ".join(res["stopped"]) or "none"
            print(f"  {res['instance']} ({res['project']}): stopped {stopped_str}")
            for fail in res.get("failed", []):
                print(f"    failed: {fail}", file=sys.stderr)
        return EXIT_OP_FAILED if res.get("failed") else EXIT_OK

    # Local checkout down
    if root is None or manifest_path is None or not Path(manifest_path).is_file():
        raise RigError(
            "cannot run local 'rig down': not inside a rig project; specify a target or pass --all",
            code="E_USAGE",
            exit_code=EXIT_USAGE,
            hint="run 'rig down <project>' or 'rig down --all'",
        )

    raw_manifest = load_manifest(manifest_path)
    root = Path(root).resolve()
    ensure_runtime_dir(root)
    instance = instance_id(raw_manifest.project, root)
    state_path = _state_path(root, instance=instance)
    lock_path = _lock_path(root, instance=instance)

    with exclusive_lock(lock_path, LOCK_TIMEOUT_SECS):
        state = read_state(state_path)
        active_mode = state.get("mode")
        manifest = raw_manifest.for_mode(active_mode) if raw_manifest.modes else raw_manifest
        targets = reverse_dependency_order(
            {
                name: {"depends_on": sorted(_merged_depends_on(name, state["services"].get(name, {}), manifest))}
                for name in manifest.teardown_scope(scope)
            }
        )
        blocked: list[str] = []
        for name in targets:
            # State is consulted as well as the manifest: a service started in
            # another mode, or since dropped from the manifest, still needs its
            # dependency.
            for dependent in _consumers_of(name, state["services"], manifest):
                if dependent in state["services"] and dependent not in targets:
                    blocked.append(f"{name} is still needed by running service {dependent}")
        if blocked:
            if as_json:
                raise RigError(
                    "; ".join(blocked),
                    code="E_REFUSED",
                    exit_code=EXIT_REFUSED,
                    hint="stop the dependent service first, or use --scope full",
                )
            for message in blocked:
                print(f"  refused: {message}", file=sys.stderr)
            print("  stop the dependent service first, or use --scope full", file=sys.stderr)
            return EXIT_REFUSED

        failures: list[str] = []
        failed_services: set[str] = set()
        stopped_names: list[str] = []
        for name in targets:
            dependents_failed = [
                dep
                for dep in _consumers_of(name, state["services"], manifest)
                if dep in failed_services or dep in state["services"]
            ]
            if dependents_failed:
                msg = f"{name}: preserved because dependent(s) {', '.join(dependents_failed)} are still active"
                if not as_json:
                    print(f"  {msg}", file=sys.stderr)
                failures.append(msg)
                continue

            record = state["services"].get(name)
            if record is None:
                if not as_json:
                    print(f"  {name}: not running")
                continue
            outcome = _stop_record(record, root)
            if outcome in ("terminated", "killed", "stale"):
                port = record.get("port")
                state["services"].pop(name, None)
                write_state(state_path, state)
                stopped_names.append(name)
                if port and not wait_for_port_release(int(port)):
                    msg = f"{name}: port {port} is still held"
                    if not as_json:
                        print(f"  {msg}", file=sys.stderr)
                    failures.append(msg)
                if not as_json:
                    print(f"  {name}: {outcome}")
            else:
                failures.append(f"{name}: {outcome}")
                failed_services.add(name)
                if not as_json:
                    print(
                        f"  {name}: {outcome}; ownership could not be confirmed, "
                        f"leaving it untouched",
                        file=sys.stderr,
                    )

        state["generation"] = int(state.get("generation", 0)) + 1
        write_state(state_path, state)

    if as_json:
        print_json_envelope(
            "down",
            {
                "instance": instance,
                "project": manifest.project,
                "stopped": stopped_names,
                "failures": failures,
            },
            ok=not failures,
        )
    return EXIT_OP_FAILED if failures else EXIT_OK


def cmd_status(root: Path, manifest_path: Path, as_json: bool = False) -> int:
    raw_manifest = load_manifest(manifest_path)
    root = Path(root).resolve()
    ensure_runtime_dir(root)
    instance = instance_id(raw_manifest.project, root)
    state_path = _state_path(root, instance=instance)
    lock_path = _lock_path(root, instance=instance)

    with exclusive_lock(lock_path, LOCK_TIMEOUT_SECS):
        state = read_state(state_path)
        if prune_state(state, root):
            write_state(state_path, state)
        active_mode = state.get("mode")
        manifest = raw_manifest.for_mode(active_mode) if raw_manifest.modes else raw_manifest

        if as_json:
            services_info = {}
            for sname in sorted(manifest.services):
                rec = state["services"].get(sname)
                alive = is_service_verifiable_alive(rec, root) if rec else False
                services_info[sname] = {
                    "running": alive,
                    "type": manifest.services[sname].type,
                    "port": rec.get("port") if rec else None,
                    "url": rec.get("url") if rec else None,
                    "pid": rec.get("pid") if rec else None,
                }
            print_json_envelope(
                "status",
                {
                    "project": manifest.project,
                    "instance": instance,
                    "mode": active_mode or "default",
                    "generation": state.get("generation", 0),
                    "services": services_info,
                },
            )
        else:
            _print_status(manifest, state, root)
    return EXIT_OK


def _print_status(manifest: Manifest, state: Mapping[str, Any], root: Path) -> None:
    instance = instance_id(manifest.project, root)
    mode_str = f"  mode={manifest.active_mode}" if manifest.active_mode else ""
    print(f"{manifest.project}  instance={instance}{mode_str}  generation={state.get('generation', 0)}")
    width = max((len(name) for name in manifest.services), default=8)
    for name in sorted(manifest.services):
        record = state["services"].get(name)
        if record is None:
            print(f"  {name.ljust(width)}  stopped")
            continue
        # A retained record is not proof of a running service, so the state is
        # derived rather than assumed: a stopped or unreachable service must
        # never be reported as running.
        status = record_status(record, root)
        health = ""
        service = manifest.services[name]
        if status == "running" and service.healthcheck_path and record.get("port"):
            ready = wait_for_http(
                int(record["port"]),
                service.healthcheck_path,
                timeout=1.0,
                pid=record.get("pid"),
                pgid=record.get("pgid"),
            )
            health = "  healthy" if ready else "  unhealthy"
        pid = record.get("pid")
        pid_text = f"pid={pid}" if pid else f"container={str(record.get('container'))[:12]}"
        print(
            f"  {name.ljust(width)}  {status.ljust(7)}  {pid_text}  "
            f"{record.get('url') or 'no port'}{health}"
        )


def cmd_ps(health: bool = False, as_json: bool = False) -> int:
    instances_dir = get_instances_dir()
    if not instances_dir.is_dir():
        if as_json:
            print_json_envelope("ps", {"instances": []})
        else:
            print("No active or recorded rig instances found.")
        return EXIT_OK

    instances_data: list[dict[str, Any]] = []
    for inst_dir in sorted(instances_dir.iterdir()):
        if not inst_dir.is_dir():
            continue
        state_file = inst_dir / STATE_FILE_NAME
        if not state_file.is_file():
            continue
        state = read_state(state_file)
        instance_id_val = state.get("instance") or inst_dir.name
        project = state.get("project") or instance_id_val.rsplit("-", 1)[0]
        root_str = state.get("root")
        root_path = Path(root_str).resolve() if root_str else None
        root_exists = root_path.is_dir() if root_path else False
        mode = state.get("mode") or "default"
        locked = is_locked(inst_dir / LOCK_FILE_NAME)

        services_info = {}
        running_count = 0
        total_count = len(state.get("services", {}))

        for sname, srec in state.get("services", {}).items():
            stype = srec.get("type", "unknown")
            port = srec.get("port")
            url = srec.get("url")
            pid = srec.get("pid")

            is_alive = False
            if stype == "compose":
                is_alive = compose_record_alive(srec, root_path or inst_dir)
            else:
                is_alive = (
                    isinstance(pid, int)
                    and pid_alive(pid)
                    and identity_matches(srec)
                )

            svc_status = "running" if is_alive else "stopped"
            if is_alive:
                running_count += 1

            health_status = None
            if health and is_alive and port:
                health_path = srec.get("healthcheck_path") or srec.get("health") or "/"
                h_ok = wait_for_http(int(port), health_path, timeout=1.0, pid=pid, pgid=srec.get("pgid"))
                health_status = "healthy" if h_ok else "unhealthy"

            services_info[sname] = {
                "type": stype,
                "status": svc_status,
                "port": port,
                "url": url,
                "pid": pid,
                "health": health_status,
            }

        # A checkout that is gone outranks whatever its services still report:
        # nothing can be managed from a root that no longer exists. Below that,
        # a stack that is only half up must not read as `running`.
        if not root_exists:
            instance_status = "orphaned"
        elif total_count > 0 and running_count == total_count:
            instance_status = "running"
        elif running_count > 0:
            instance_status = "partial"
        else:
            instance_status = "stopped"

        instances_data.append({
            "instance": instance_id_val,
            "project": project,
            "mode": mode,
            "status": instance_status,
            "locked": locked,
            "root": root_str,
            "root_exists": root_exists,
            "services_running": running_count,
            "services_total": total_count,
            "services": services_info,
        })

    if as_json:
        print_json_envelope("ps", {"instances": instances_data})
        return EXIT_OK

    if not instances_data:
        print("No active or recorded rig instances found.")
        return EXIT_OK

    print(
        f"{'PROJECT':<16} {'INSTANCE':<22} {'MODE':<10} {'STATUS':<10} {'SERVICES':<25} {'ROOT'}"
    )
    for item in instances_data:
        svc_summary = ", ".join(
            f"{s}:{info['status']}" for s, info in item["services"].items()
        ) or "none"
        if len(svc_summary) > 24:
            svc_summary = f"{item['services_running']}/{item['services_total']} up"
        root_display = item["root"] or "n/a"
        if not item["root_exists"]:
            root_display += " [deleted]"
        print(
            f"{item['project']:<16} "
            f"{item['instance']:<22} "
            f"{item['mode']:<10} "
            f"{item['status']:<10} "
            f"{svc_summary:<25} "
            f"{root_display}"
        )
    return EXIT_OK


def _instance_live_services(
    services: Mapping[str, Mapping[str, Any]], root: Path
) -> dict[str, Mapping[str, Any]]:
    """Return the recorded services that still show any sign of life."""
    live: dict[str, Mapping[str, Any]] = {}
    for name, record in services.items():
        if record.get("type") == "compose":
            if compose_record_status(record, root) != "absent":
                live[name] = record
            continue
        pid = record.get("pid")
        pgid = record.get("pgid")
        if (isinstance(pid, int) and pid_alive(pid) and identity_matches(record)) or (
            isinstance(pgid, int) and pgid_alive(pgid)
        ):
            live[name] = record
    return live


def _force_stop_instance(
    state: dict[str, Any], state_file: Path, root: Path
) -> list[str]:
    """Stop every recorded service of one instance, dependents before dependencies.

    A dependency is preserved whenever its dependent refused to stop: killing it
    first would break the very consumer that is still running against it.
    """
    services = state.get("services", {})
    failures: list[str] = []
    failed_services: set[str] = set()

    for name in reverse_dependency_order(services):
        record = services.get(name)
        if record is None:
            continue
        blocking = sorted(
            dependent
            for dependent in _consumers_of(name, services)
            if dependent in failed_services
        )
        if blocking:
            failures.append(f"{name}: preserved because {', '.join(blocking)} is still running")
            failed_services.add(name)
            continue
        # The record is dropped below, so the container must go with it: a
        # merely stopped container no record points at is unreclaimable.
        outcome = _stop_record(record, root, remove=True)
        if outcome in ("terminated", "killed", "stale"):
            services.pop(name, None)
        else:
            failures.append(f"{name}: {outcome}")
            failed_services.add(name)

    state["generation"] = int(state.get("generation", 0)) + 1
    write_state(state_file, state)
    return failures


def _clear_instance_dir(inst_dir: Path) -> bool:
    """Delete an instance's contents while keeping the lock inode intact.

    ``checkout.lock`` is deliberately left in place: every command contends for
    that one inode, so unlinking it would let a waiting process acquire a lock on
    a file nobody else can see. Returns True when anything was removed.
    """
    removed = False
    for item in inst_dir.iterdir():
        if item.name == LOCK_FILE_NAME:
            continue
        if item.is_dir() and not item.is_symlink():
            shutil.rmtree(item, ignore_errors=True)
        else:
            with contextlib.suppress(OSError):
                item.unlink()
        removed = True
    return removed


def cmd_prune(force: bool = False, as_json: bool = False) -> int:
    instances_dir = get_instances_dir()
    if not instances_dir.is_dir():
        if as_json:
            print_json_envelope("prune", {"pruned": [], "failed": []})
        else:
            print("No instances to prune.")
        return EXIT_OK

    pruned: list[str] = []
    failed: list[dict[str, Any]] = []
    for inst_dir in sorted(instances_dir.iterdir()):
        if not inst_dir.is_dir():
            continue
        lock_file = inst_dir / LOCK_FILE_NAME
        state_file = inst_dir / STATE_FILE_NAME

        lock_fd = None
        try:
            lock_fd = os.open(
                str(lock_file),
                os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            if lock_fd is not None:
                with contextlib.suppress(OSError):
                    os.close(lock_fd)
            continue

        try:
            state = read_state(state_file) if state_file.is_file() else {}
            services = state.get("services", {})
            root_str = state.get("root")
            root_exists = Path(root_str).is_dir() if root_str else False
            ref_root = Path(root_str) if root_exists else inst_dir

            live = _instance_live_services(services, ref_root)
            if live and not force:
                continue
            if live:
                state.setdefault("services", services)
                failures = _force_stop_instance(state, state_file, ref_root)
                if failures:
                    failed.append({"instance": inst_dir.name, "failed": failures})
                    continue
                services = state.get("services", {})

            if not (force or not root_exists or len(services) == 0):
                continue
            if _clear_instance_dir(inst_dir):
                pruned.append(inst_dir.name)
        finally:
            if lock_fd is not None:
                with contextlib.suppress(OSError):
                    os.close(lock_fd)

    if as_json:
        print_json_envelope(
            "prune", {"pruned": pruned, "failed": failed}, ok=not failed
        )
    else:
        if pruned:
            print(f"Pruned {len(pruned)} dead instance(s):")
            for name in pruned:
                print(f"  - {name}")
        else:
            print("No instances eligible for pruning.")
        for entry in failed:
            for message in entry["failed"]:
                print(f"  failed: {entry['instance']}: {message}", file=sys.stderr)
    return EXIT_OP_FAILED if failed else EXIT_OK


def _resolve_executable(spec: str, cwd: Path) -> Path | None:
    """Return the file a spawn would execute for ``spec``, or None when absent.

    A spec that contains a separator is resolved against the service working
    directory, because that is the directory the child process is spawned in.
    A bare name is looked up on PATH, exactly as ``execvp`` would.
    """
    if os.sep in spec:
        candidate = Path(spec)
        if not candidate.is_absolute():
            candidate = cwd / candidate
        return candidate if candidate.is_file() else None
    found = shutil.which(spec)
    return Path(found) if found else None


def cmd_check(
    root: Path,
    manifest_path: Path,
    mode: str | None = None,
    as_json: bool = False,
) -> int:
    issues = []
    root = Path(root).resolve()

    try:
        manifest = load_manifest(manifest_path)
    except Exception as exc:
        if as_json:
            print_json_envelope(
                "check",
                {"ok": False, "issues": [{"level": "error", "check": "manifest", "message": str(exc)}]},
            )
        else:
            print(f"FAIL manifest: {exc}", file=sys.stderr)
        return EXIT_USAGE

    modes_to_check = [mode] if mode else (list(manifest.modes.keys()) if manifest.modes else [None])

    for m in modes_to_check:
        try:
            m_manifest = manifest.for_mode(m)
        except Exception as exc:
            issues.append({"level": "error", "check": f"mode:{m}", "message": str(exc)})
            continue

        mode_tag = f"[{m}]" if m else ""
        for sname, svc in m_manifest.services.items():
            cwd = (root / svc.cwd).resolve()
            if not cwd.is_dir():
                issues.append({
                    "level": "error",
                    "check": f"{mode_tag} service:{sname}:cwd".strip(),
                    "message": f"working directory '{svc.cwd}' does not exist",
                })
            if svc.type in ("fd", "port"):
                if not shutil.which("lsof"):
                    issues.append({
                        "level": "error",
                        "check": f"{mode_tag} service:{sname}:lsof".strip(),
                        "message": "'lsof' binary not found on PATH (required for port listener verification)",
                    })
                render_values = {"root": str(root), "cwd": str(cwd), "python": sys.executable}
                cmd = svc.command
                if cmd:
                    bin_str = str(render(cmd[0], render_values))
                    actual_bin = _resolve_executable(bin_str, cwd)
                    if actual_bin is None:
                        issues.append({
                            "level": "error",
                            "check": f"{mode_tag} service:{sname}:binary".strip(),
                            "message": f"executable '{cmd[0]}' not found on PATH or under '{svc.cwd}'",
                        })
                    elif not os.access(actual_bin, os.X_OK):
                        issues.append({
                            "level": "error",
                            "check": f"{mode_tag} service:{sname}:binary".strip(),
                            "message": f"file '{cmd[0]}' exists at '{actual_bin}' but is not executable (missing +x permission)",
                        })
                elif svc.type == "fd" and svc.python:
                    py_str = str(render(svc.python, render_values))
                    actual_py = _resolve_executable(py_str, cwd)
                    if actual_py is None:
                        issues.append({
                            "level": "error",
                            "check": f"{mode_tag} service:{sname}:python".strip(),
                            "message": f"python interpreter '{svc.python}' not found on PATH or under '{svc.cwd}'",
                        })
                    elif not os.access(actual_py, os.X_OK):
                        issues.append({
                            "level": "error",
                            "check": f"{mode_tag} service:{sname}:python".strip(),
                            "message": f"python interpreter '{svc.python}' exists at '{actual_py}' but is not executable (missing +x permission)",
                        })
            elif svc.type == "compose":
                if not shutil.which("docker"):
                    issues.append({
                        "level": "error",
                        "check": f"{mode_tag} service:{sname}:docker".strip(),
                        "message": "'docker' binary not found on PATH",
                    })
                if svc.compose_file:
                    cfile = root / svc.compose_file
                    if not cfile.is_file():
                        issues.append({
                            "level": "error",
                            "check": f"{mode_tag} service:{sname}:compose_file".strip(),
                            "message": f"compose file '{svc.compose_file}' does not exist",
                        })

    has_errors = any(i["level"] == "error" for i in issues)
    if as_json:
        print_json_envelope(
            "check",
            {"ok": not has_errors, "project": manifest.project, "issues": issues},
        )
    else:
        if not issues:
            print(f"OK check passed: manifest '{manifest_path}' is valid for {len(modes_to_check)} mode(s).")
        else:
            for issue in issues:
                prefix = "FAIL" if issue["level"] == "error" else "WARN"
                print(f"{prefix} {issue['check']}: {issue['message']}", file=sys.stderr)
    return EXIT_USAGE if has_errors else EXIT_OK


# Images and service names that identify a datastore. Anything else - including
# an application whose environment merely mentions a database URL - is not one.
POSTGRES_IMAGES = ("postgres", "postgresql", "postgis", "timescaledb")
POSTGRES_NAMES = ("db", "database", "postgres", "postgresql")
REDIS_IMAGES = ("redis", "valkey")
REDIS_NAMES = ("redis", "valkey", "cache")


def _extract_compose_services(content: str) -> dict[str, str]:
    """Return each top-level Compose service name mapped to its own block."""
    services: dict[str, list[str]] = {}
    in_services = False
    current_svc = None

    for line in content.splitlines():
        trimmed = line.strip()
        if not trimmed or trimmed.startswith("#"):
            continue
        if re.match(r"^services\s*:\s*$", line):
            in_services = True
            current_svc = None
            continue
        elif in_services and re.match(r"^[a-zA-Z0-9_-]+\s*:\s*$", line) and not line.startswith(" "):
            in_services = False
            current_svc = None
            continue

        if in_services:
            m = re.match(r"^ {2}([a-zA-Z0-9_-]+)\s*:\s*$", line)
            if m:
                current_svc = m.group(1)
                services[current_svc] = []
            elif current_svc and (line.startswith("   ") or line.startswith("\t")):
                services[current_svc].append(line)

    return {k: "\n".join(v).lower() for k, v in services.items()}


def _compose_service_image(block: str) -> str | None:
    """Return the image name declared in one Compose service block."""
    match = re.search(r"^\s{2,}image\s*:\s*[\"']?([^\"'\s#]+)", block, re.MULTILINE)
    if not match:
        return None
    reference = match.group(1)
    return reference.rsplit("/", 1)[-1].split(":", 1)[0]


def classify_compose_service(name: str, block: str) -> str | None:
    """Return "postgres", "redis" or None for one Compose service.

    The decision uses the declared image first and the service name only as a
    fallback. An image that names a different engine is never overridden by a
    suggestive service name, so a ``db`` service running MySQL stays unmatched.
    """
    image = _compose_service_image(block)
    if image is not None:
        if image in POSTGRES_IMAGES:
            return "postgres"
        if image in REDIS_IMAGES:
            return "redis"
        return None
    if name.lower() in POSTGRES_NAMES:
        return "postgres"
    if name.lower() in REDIS_NAMES:
        return "redis"
    return None


def cmd_init(
    root: Path,
    dry_run: bool = False,
    force: bool = False,
    up: bool = False,
    as_json: bool = False,
) -> int:
    root = Path(root).resolve()
    target_manifest = root / "rig.json"
    if (target_manifest.is_symlink() or target_manifest.exists()) and not force and not dry_run:
        raise RigError(
            f"'{target_manifest}' already exists. Pass --force to overwrite.",
            code="E_USAGE",
            exit_code=EXIT_USAGE,
            hint="pass --force to overwrite the existing manifest",
        )

    project_name = re.sub(r"[^a-zA-Z0-9]+", "-", root.name.lower()).strip("-") or "app"
    base_services = {}
    native_services = {}

    compose_candidates = ["docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"]
    detected_compose = None
    for cand in compose_candidates:
        if (root / cand).is_file():
            detected_compose = cand
            break

    if detected_compose:
        try:
            content = (root / detected_compose).read_text()
            compose_svcs = _extract_compose_services(content)
            defaults = {
                "postgres": (5432, "PostgreSQL database container"),
                "redis": (6379, "Redis cache container"),
            }
            for svc_name, block in compose_svcs.items():
                kind = classify_compose_service(svc_name, block)
                if kind is None or kind in base_services:
                    continue
                port, description = defaults[kind]
                base_services[kind] = {
                    "type": "compose",
                    "compose_file": detected_compose,
                    "compose_service": svc_name,
                    "compose_port": port,
                    "description": description,
                }
        except OSError:
            pass

    has_pyproject = (root / "pyproject.toml").is_file()
    has_reqs = (root / "requirements.txt").is_file()
    has_manage_py = (root / "manage.py").is_file()

    backend_detected = False
    if has_manage_py:
        native_services["backend"] = {
            "type": "port",
            "cwd": ".",
            "command": ["python", "manage.py", "runserver", "127.0.0.1:{port}"],
            "healthcheck_path": "/",
            "description": "Django web application",
        }
        backend_detected = True
    elif has_pyproject or has_reqs:
        app_target = "main:app"
        if (root / "app" / "main.py").is_file():
            app_target = "app.main:app"
        elif (root / "src" / "main.py").is_file():
            app_target = "src.main:app"

        backend_spec: dict[str, Any] = {
            "type": "fd",
            "cwd": ".",
            "python": sys.executable,
            "app": app_target,
            "healthcheck_path": "/healthz",
            "description": "FastAPI / ASGI backend application",
        }
        if "postgres" in base_services:
            backend_spec["depends_on"] = ["postgres"]
        native_services["backend"] = backend_spec
        backend_detected = True

    has_package_json = (root / "package.json").is_file()
    if has_package_json:
        pm = "npm"
        if (root / "pnpm-lock.yaml").is_file():
            pm = "pnpm"
        elif (root / "yarn.lock").is_file():
            pm = "yarn"
        elif (root / "bun.lockb").is_file():
            pm = "bun"

        frontend_spec: dict[str, Any] = {
            "type": "port",
            "cwd": ".",
            "command": [pm, "run", "dev", "--", "--port", "{port}"],
            "healthcheck_path": "/",
            "description": "Frontend development server",
        }
        if backend_detected:
            frontend_spec["depends_on"] = ["backend"]
        native_services["frontend"] = frontend_spec

    if not base_services and not native_services:
        native_services["web"] = {
            "type": "port",
            "cwd": ".",
            "command": [sys.executable, "-m", "http.server", "--bind", "127.0.0.1", "{port}"],
            "healthcheck_path": "/",
            "description": "Local HTTP static file server",
        }

    manifest_data: dict[str, Any] = {
        "$schema": "https://raw.githubusercontent.com/evgesha9400/rig/main/rig.schema.json",
        "project": project_name,
    }
    if base_services:
        manifest_data["services"] = base_services

    if native_services:
        manifest_data["default_mode"] = "native"
        manifest_data["modes"] = {
            "native": {
                "services": native_services,
            }
        }
    else:
        manifest_data["services"] = base_services

    formatted_json = json.dumps(manifest_data, indent=2) + "\n"

    if dry_run:
        if as_json:
            print_json_envelope("init", {"manifest": manifest_data, "dry_run": True})
        else:
            print(formatted_json, end="")
        return EXIT_OK

    if not force:
        try:
            fd = os.open(
                str(target_manifest),
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o644,
            )
            with os.fdopen(fd, "w") as f:
                f.write(formatted_json)
        except FileExistsError:
            raise RigError(
                f"'{target_manifest}' already exists. Pass --force to overwrite.",
                code="E_USAGE",
                exit_code=EXIT_USAGE,
            )
    else:
        tmp_manifest: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", dir=root, delete=False, prefix=".rig.json.tmp."
            ) as handle:
                tmp_manifest = handle.name
                handle.write(formatted_json)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp_manifest, 0o644)
            os.replace(tmp_manifest, target_manifest)
        except BaseException:
            if tmp_manifest is not None:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_manifest)
            raise

    if up:
        if not as_json:
            print(f"Created {target_manifest}")
            print(f"Starting stack for {project_name}...")
        return cmd_up(root, target_manifest, as_json=as_json)

    if as_json:
        print_json_envelope("init", {"manifest": manifest_data, "path": str(target_manifest), "created": True})
    else:
        print(f"Created {target_manifest}")

    return EXIT_OK


def get_rig_schema() -> dict[str, Any]:
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": "RigManifest",
        "description": "Schema for rig.json (v2) developer environment supervisor manifests.",
        "type": "object",
        "required": ["project"],
        "properties": {
            "$schema": {"type": "string"},
            "project": {"type": "string", "pattern": "^[a-zA-Z0-9_-]+$"},
            "default_mode": {"type": "string"},
            "services": {
                "type": "object",
                "additionalProperties": {"$ref": "#/definitions/Service"},
            },
            "modes": {
                "type": "object",
                "additionalProperties": {
                    "type": "object",
                    "required": ["services"],
                    "properties": {
                        "services": {
                            "type": "object",
                            "additionalProperties": {"$ref": "#/definitions/Service"},
                        }
                    },
                },
            },
            "scopes": {
                "type": "object",
                "additionalProperties": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
        },
        "definitions": {
            "Service": {
                "type": "object",
                "required": ["type"],
                "properties": {
                    "type": {"type": "string", "enum": ["fd", "port", "compose"]},
                    "cwd": {"type": "string", "default": "."},
                    "command": {
                        "oneOf": [
                            {"type": "string"},
                            {"type": "array", "items": {"type": "string"}},
                        ]
                    },
                    "aliases": {"type": "array", "items": {"type": "string"}},
                    "app": {"type": "string"},
                    "factory": {"type": "boolean", "default": False},
                    "python": {"type": "string"},
                    "env": {"type": "object"},
                    "inherit": {"type": "array", "items": {"type": "string"}},
                    "env_files": {"type": "array", "items": {"type": "string"}},
                    "healthcheck_path": {"type": "string"},
                    "healthcheck_timeout": {"type": "number", "default": 45.0},
                    "depends_on": {"type": "array", "items": {"type": "string"}},
                    "compose_file": {"type": "string"},
                    "compose_service": {"type": "string"},
                    "compose_port": {"type": "integer"},
                    "docker_context": {"type": "string"},
                    "description": {"type": "string"},
                },
            }
        },
    }


def cmd_schema(as_json: bool = False) -> int:
    schema = get_rig_schema()
    if as_json:
        print_json_envelope("schema", schema)
    else:
        print(json.dumps(schema, indent=2))
    return EXIT_OK



def print_json_envelope(command: str, data: Any, ok: bool | None = None) -> None:
    if ok is None:
        if isinstance(data, dict) and "ok" in data:
            ok = bool(data["ok"])
        else:
            ok = True
    envelope = {
        "schema": f"rig.{command}/1",
        "ok": ok,
        "data": data,
    }
    print(json.dumps(envelope, indent=2))


def print_json_error(exc: RigError, command: str = "error") -> None:
    envelope = {
        "schema": "rig.error/1",
        "ok": False,
        "error": {
            "code": exc.code,
            "exit_code": exc.exit_code,
            "message": exc.message,
            "hint": exc.hint,
            "details": exc.details,
        },
    }
    print(json.dumps(envelope, indent=2))


# --------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------


class RigArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args, as_json: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self.as_json = as_json

    def error(self, message: str):
        if self.as_json:
            err = RigError(message, code="E_USAGE", exit_code=EXIT_USAGE)
            print_json_error(err, command="cli")
            sys.exit(EXIT_USAGE)
        super().error(message)


def build_parser(as_json: bool = False) -> argparse.ArgumentParser:
    parser = RigArgumentParser(
        prog="rig",
        description="Machine-wide and local development environment supervisor.",
        as_json=as_json,
    )
    parser.add_argument("--root", default=None, help="project root (default: auto-discovered)")
    parser.add_argument("--manifest", default=None, help="path to manifest (e.g. rig.json or stack.json)")
    parser.add_argument("--json", action="store_true", help="output structured JSON response envelope")
    sub = parser.add_subparsers(
        dest="command",
        required=True,
        parser_class=lambda **kwargs: RigArgumentParser(as_json=as_json, **kwargs),
    )

    # up
    p_up = sub.add_parser("up", help="start the selected services")
    p_up.add_argument("--scope", default="full", help="scope to manage (default: full)")
    p_up.add_argument("--mode", default=None, help="stack mode (e.g. native, container)")
    p_up.add_argument("--switch", action="store_true", help="tear down current mode if switching to another mode")

    # down
    p_down = sub.add_parser("down", help="stop services in this checkout or globally")
    p_down.add_argument("target", nargs="?", default=None, help="project name or instance ID to stop")
    p_down.add_argument(
        "--all", action="store_true", dest="all_instances", help="stop all active projects across the entire machine"
    )
    p_down.add_argument("--scope", default="full", help="scope to stop (default: full)")

    # status
    sub.add_parser("status", help="report what is running in this checkout")

    # ps / ls / list
    p_ps = sub.add_parser("ps", aliases=["ls", "list"], help="list all active projects across the machine")
    p_ps.add_argument("--health", action="store_true", help="perform active HTTP health checks on running services")

    # prune
    p_prune = sub.add_parser("prune", help="clean up dead or orphaned instance registry directories")
    p_prune.add_argument("--force", action="store_true", help="force prune unreferenced instance states")

    # check
    p_check = sub.add_parser("check", help="statically verify project configuration and prerequisites")
    p_check.add_argument("--mode", default=None, help="mode to check (default: all)")

    # init
    p_init = sub.add_parser("init", help="detect project structure and generate rig.json")
    p_init.add_argument("--dry-run", action="store_true", help="print generated rig.json without writing")
    p_init.add_argument("--force", action="store_true", help="overwrite existing rig.json")
    p_init.add_argument("--up", action="store_true", help="start services immediately after creating rig.json")

    # schema
    sub.add_parser("schema", help="print JSON schema for rig.json manifests")

    return parser


def find_project_root(start: Path | None = None) -> Path:
    """Find project root by walking upward from current working directory."""
    current = (start or Path.cwd()).resolve()
    for parent in [current, *current.parents]:
        for candidate in (
            "rig.json",
            "scripts/rig.json",
            ".config/rig.json",
            "stack.json",
            "scripts/stack.json",
            ".config/stack.json",
            ".git",
        ):
            if (parent / candidate).exists():
                return parent
    return current


def find_default_manifest(root: Path) -> Path:
    """Resolve default manifest path, checking rig.json then stack.json candidates."""
    candidates = [
        root / "rig.json",
        root / "scripts" / "rig.json",
        root / ".config" / "rig.json",
        root / "scripts" / "stack.json",
        root / ".config" / "stack.json",
        root / "stack.json",
    ]
    for c in candidates:
        if c.is_file():
            return c
    return candidates[0]


def main(argv: Sequence[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in raw_argv
    if as_json:
        raw_argv = [a for a in raw_argv if a != "--json"]

    parser = build_parser(as_json=as_json)
    try:
        args = parser.parse_args(raw_argv)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else EXIT_USAGE
        return code

    cmd = args.command
    root = Path(args.root).resolve() if getattr(args, "root", None) else find_project_root()
    manifest_arg = getattr(args, "manifest", None)
    manifest_path = Path(manifest_arg).resolve() if manifest_arg else find_default_manifest(root)

    try:
        if cmd == "up":
            return cmd_up(
                root,
                manifest_path,
                scope=args.scope,
                mode=getattr(args, "mode", None),
                switch=getattr(args, "switch", False),
                as_json=as_json,
            )
        if cmd == "down":
            return cmd_down(
                root,
                manifest_path,
                scope=getattr(args, "scope", "full"),
                target=getattr(args, "target", None),
                all_instances=getattr(args, "all_instances", False),
                as_json=as_json,
            )
        if cmd == "status":
            return cmd_status(root, manifest_path, as_json=as_json)
        if cmd in ("ps", "ls", "list"):
            return cmd_ps(health=getattr(args, "health", False), as_json=as_json)
        if cmd == "prune":
            return cmd_prune(force=getattr(args, "force", False), as_json=as_json)
        if cmd == "check":
            return cmd_check(root, manifest_path, mode=getattr(args, "mode", None), as_json=as_json)
        if cmd == "init":
            return cmd_init(
                root,
                dry_run=getattr(args, "dry_run", False),
                force=getattr(args, "force", False),
                up=getattr(args, "up", False),
                as_json=as_json,
            )
        if cmd == "schema":
            return cmd_schema(as_json=as_json)
        return EXIT_OK
    except RigError as exc:
        if as_json:
            print_json_error(exc, command=cmd)
        else:
            print(f"rig: error [{exc.code}]: {exc.message}", file=sys.stderr)
            if exc.hint:
                print(f"  hint: {exc.hint}", file=sys.stderr)
        return exc.exit_code
    except TimeoutError as exc:
        err = RigError(str(exc), code="E_LOCK_TIMEOUT", exit_code=EXIT_MUTEX_CONFLICT)
        if as_json:
            print_json_error(err, command=cmd)
        else:
            print(f"rig: error [{err.code}]: {err.message}", file=sys.stderr)
        return err.exit_code
    except KeyboardInterrupt:
        if as_json:
            print_json_error(
                RigError("operation cancelled by user", code="E_INTERRUPTED", exit_code=EXIT_INTERRUPTED),
                command=cmd,
            )
        else:
            print("rig: interrupted", file=sys.stderr)
        return EXIT_INTERRUPTED
    except Exception as exc:
        err = RigError(f"unexpected error: {exc}", code="E_INTERNAL", exit_code=EXIT_OP_FAILED)
        if as_json:
            print_json_error(err, command=cmd)
        else:
            print(f"rig: error [{err.code}]: {err.message}", file=sys.stderr)
        return err.exit_code


if __name__ == "__main__":
    sys.exit(main())


