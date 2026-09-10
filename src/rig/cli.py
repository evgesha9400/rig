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

SECRET_NAME_PATTERN = re.compile(
    r"TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|_KEY$|^KEY$|APIKEY|PRIVATE",
    re.IGNORECASE,
)
REDACTED = "***"


class RigError(RuntimeError):
    """A rig operation cannot proceed safely."""


StackError = RigError


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
def exclusive_lock(path: Path, timeout: float = LOCK_TIMEOUT_SECS):
    """Hold an exclusive advisory lock on ``path`` or raise ``TimeoutError``.

    ``timeout`` bounds acquisition only. Work performed inside the lock carries its
    own deadlines. The lock file is opened with ``O_NOFOLLOW`` and never unlinked, so
    every caller contends for one inode.
    """
    try:
        fd = os.open(
            path,
            os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EMLINK):
            raise StackError(f"{path} is a symlink; refusing to lock it") from None
        raise StackError(f"cannot open lock file {path}: {exc}") from None

    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise StackError(f"{path} is not a regular file")
        if info.st_uid != os.getuid():
            raise StackError(f"{path} is owned by another user")
        if info.st_nlink != 1:
            raise StackError(f"{path} has {info.st_nlink} links; refusing to lock it")

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
                    f"another stack command holds {path}; timed out after {timeout:g}s"
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


def write_state(path: Path, state: Mapping[str, Any]) -> None:
    """Publish state atomically so no reader observes a partial generation."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC | os.O_CLOEXEC, 0o600)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
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
    for p in pids:
        if pid is not None and p == pid:
            return True
        if pgid is not None:
            try:
                if os.getpgid(p) == pgid:
                    return True
            except (ProcessLookupError, PermissionError, OSError):
                pass
    return False


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


def run_compose(
    instance: str,
    root: Path,
    compose_file: Path,
    args: Sequence[str],
    context: str | None = None,
    timeout: float = 180.0,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess:
    argv = compose_argv(instance, root, compose_file, args, context)
    cmd_env = dict(os.environ) if env is None else dict(env)
    if not context:
        cmd_env.pop("DOCKER_CONTEXT", None)
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=cmd_env)
    except FileNotFoundError:
        raise StackError("docker is not installed or not on PATH") from None
    except subprocess.TimeoutExpired:
        raise StackError(f"compose command timed out: {' '.join(args)}") from None


def compose_record_status(record: Mapping[str, Any], root: Path) -> str:
    """Return 'alive', 'absent', or 'error' for the recorded compose container."""
    container = record.get("container")
    instance = record.get("instance")
    compose_file = record.get("compose_file")
    service = record.get("compose_service")
    if not (container and instance and compose_file and service):
        return "absent"
    try:
        result = run_compose(
            str(instance),
            Path(root),
            Path(compose_file),
            ["ps", "-q", str(service)],
            record.get("docker_context"),
            timeout=60.0,
        )
    except StackError:
        return "error"
    if result.returncode != 0:
        return "error"
    ids = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if any(str(container).startswith(found) or found.startswith(str(container)) for found in ids):
        return "alive"
    return "absent"


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
            raise StackError(f"unknown scope {scope!r}; manifest declares {known}")
        return list(self.scopes[scope])

    def _visit(self, name: str, ordered: list[str], seen: set[str]) -> None:
        if name in ordered:
            return
        if name in seen:
            raise StackError(f"dependency cycle through service {name!r}")
        seen.add(name)
        for dependency in self.services[name].depends_on:
            self._visit(dependency, ordered, seen)
        ordered.append(name)


def load_manifest(path: Path) -> Manifest:
    """Read and validate a stack manifest."""
    path = Path(path)
    try:
        raw = json.loads(path.read_text())
    except OSError:
        raise StackError(f"manifest not found: {path}") from None
    except json.JSONDecodeError as exc:
        raise StackError(f"manifest {path} is not valid JSON: {exc}") from None
    if not isinstance(raw, dict):
        raise StackError(f"manifest {path} must be a JSON object")

    project = raw.get("project")
    if not isinstance(project, str) or not project:
        raise StackError(f"manifest {path} must declare a non-empty 'project'")

    declared = raw.get("services")
    if not isinstance(declared, dict) or not declared:
        raise StackError(f"manifest {path} must declare at least one service")

    services: dict[str, Service] = {}
    for name, raw_spec in declared.items():
        if not isinstance(raw_spec, dict):
            raise StackError(f"service {name!r} must be a JSON object")
        spec = dict(raw_spec)
        kind = spec.get("type")
        if kind not in SERVICE_TYPES:
            raise StackError(
                f"service {name!r} has unknown type {kind!r}; expected one of {SERVICE_TYPES}"
            )

        if "health" in spec:
            if "healthcheck_path" in spec and spec["health"] != spec["healthcheck_path"]:
                raise StackError(
                    f"service {name!r} defines conflicting 'health' and 'healthcheck_path'"
                )
            spec["healthcheck_path"] = spec.pop("health")

        raw_cmd = spec.get("command")
        if isinstance(raw_cmd, str):
            cmd_str = raw_cmd.strip()
            if not cmd_str:
                raise StackError(f"service {name!r} 'command' string cannot be empty")
            if "\0" in cmd_str:
                raise StackError(f"service {name!r} 'command' contains NUL characters")
            try:
                tokens = shlex.split(cmd_str, comments=False, posix=True)
            except ValueError as exc:
                raise StackError(f"service {name!r} invalid command syntax: {exc}") from None
            if not tokens:
                raise StackError(f"service {name!r} 'command' cannot be empty")
            spec["command"] = tokens
        elif isinstance(raw_cmd, list):
            if not all(isinstance(t, str) for t in raw_cmd):
                raise StackError(f"service {name!r} 'command' must be a list of strings")
        elif raw_cmd is not None:
            raise StackError(f"service {name!r} 'command' must be a string or list of strings")

        known = {f.name for f in Service.__dataclass_fields__.values()} - {"name"}
        unknown = set(spec) - known
        if unknown:
            raise StackError(f"service {name!r} has unknown keys: {sorted(unknown)}")
        services[name] = Service(name=name, **spec)

    for name, service in services.items():
        for dependency in service.depends_on:
            if dependency not in services:
                raise StackError(
                    f"service {name!r} depends on unknown service {dependency!r}"
                )
        if service.type == "fd" and not (service.command or service.app):
            raise StackError(f"service {name!r} needs a 'command' or an 'app'")
        if service.type == "port" and not service.command:
            raise StackError(f"service {name!r} needs a 'command'")
        if service.type == "compose" and not (service.compose_file and service.compose_service):
            raise StackError(
                f"service {name!r} needs 'compose_file' and 'compose_service'"
            )

    derived_scopes: dict[str, list[str]] = {
        "full": list(services.keys()),
        "local": list(services.keys()),
    }
    for sname, s in services.items():
        derived_scopes[sname] = [sname]
        for alias in s.aliases:
            if not isinstance(alias, str) or not alias:
                raise StackError(f"service {sname!r} has invalid alias {alias!r}")
            if alias in services and alias != sname:
                raise StackError(
                    f"alias {alias!r} for service {sname!r} conflicts with another service"
                )
            derived_scopes[alias] = [sname]

    scopes_raw = raw.get("scopes")
    if scopes_raw is not None:
        if not isinstance(scopes_raw, dict):
            raise StackError(f"manifest {path} 'scopes' must be a JSON object")
        for scope, members in scopes_raw.items():
            if not isinstance(members, list):
                raise StackError(f"scope {scope!r} must be a list of service names")
            for member in members:
                if member not in services:
                    raise StackError(f"scope {scope!r} names unknown service {member!r}")
            derived_scopes[scope] = list(members)

    scopes = derived_scopes
    manifest = Manifest(project=project, services=services, scopes=scopes, path=path)
    for scope in scopes:
        manifest.resolve_scope(scope)  # rejects dependency cycles eagerly
    return manifest


# --------------------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------------------


def _state_path(root: Path) -> Path:
    return Path(root) / RUNTIME_DIR_NAME / STATE_FILE_NAME


def _lock_path(root: Path) -> Path:
    return Path(root) / RUNTIME_DIR_NAME / LOCK_FILE_NAME


def record_alive(record: Mapping[str, Any], root: Path) -> bool:
    if record.get("type") == "compose":
        return compose_record_alive(record, root)
    return identity_matches(record)


def prune_state(state: dict[str, Any], root: Path) -> list[str]:
    """Drop records whose ownership can no longer be established. Returns their names.

    A pruned record whose port is still occupied means something is running that this
    checkout can no longer claim. That is reported loudly rather than killed: the
    port may now belong to an unrelated process.
    """
    dropped = [
        name
        for name, record in list(state["services"].items())
        if not record_alive(record, root)
        and not (isinstance(record.get("pgid"), int) and pgid_alive(record["pgid"]))
    ]
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

    if service.type == "compose":
        return _start_compose_service(service, root, instance)

    env = build_service_env(
        service.env, service.inherit, Path(root), values, service.env_files
    )
    if service.type == "fd":
        raw_argv = service.command or uvicorn_argv(
            python=str(render(service.python or sys.executable, values)),
            app=service.app or "",
            factory=service.factory,
        )
        record = spawn_fd_service(service.name, raw_argv, cwd, env, log_path, values=values)
    else:
        record = spawn_port_service(
            service.name, service.command, cwd, env, log_path, reserve_port(), values=values
        )
    record["log"] = str(log_path)
    record["env"] = redact(env)
    return record


def _start_compose_service(
    service: Service, root: Path, instance: str
) -> dict[str, Any]:
    compose_file = Path(root) / str(service.compose_file)
    args = ["up", "-d", "--wait", str(service.compose_service)]
    result = run_compose(instance, Path(root), compose_file, args, service.docker_context)
    if result.returncode != 0:
        raise StackError(
            f"compose could not start {service.name!r}: {result.stderr.strip() or result.stdout.strip()}"
        )
    ids = run_compose(
        instance, Path(root), compose_file, ["ps", "-q", str(service.compose_service)],
        service.docker_context, timeout=60.0,
    )
    container = ids.stdout.strip().splitlines()[0].strip() if ids.stdout.strip() else ""
    port = None
    partial_record = {
        "name": service.name,
        "type": "compose",
        "pid": None,
        "pgid": None,
        "instance": instance,
        "compose_file": str(compose_file),
        "compose_service": service.compose_service,
        "docker_context": service.docker_context,
        "container": container,
        "port": None,
        "url": None,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    if service.compose_port:
        try:
            published = run_compose(
                instance, Path(root), compose_file,
                ["port", str(service.compose_service), str(service.compose_port)],
                service.docker_context, timeout=60.0,
            )
            port = parse_compose_port(published.stdout)
        except Exception as exc:
            _stop_record(partial_record, root)
            raise StackError(
                f"compose failed to resolve port for {service.name!r}: {exc}"
            ) from exc
    partial_record["port"] = port
    partial_record["url"] = f"http://127.0.0.1:{port}" if port else None
    return partial_record


def _stop_record(record: Mapping[str, Any], root: Path) -> str:
    if record.get("type") == "compose":
        status = compose_record_status(record, root)
        if status == "absent":
            return "stale"
        if status == "error":
            return "failed"
        result = run_compose(
            str(record["instance"]),
            Path(root),
            Path(str(record["compose_file"])),
            # Volumes are preserved: an ordinary `down` must not destroy local data.
            ["stop", str(record["compose_service"])],
            record.get("docker_context"),
        )
        return "terminated" if result.returncode == 0 else "failed"
    return terminate_record(record, TEARDOWN_TIMEOUT_SECS)


def is_service_verifiable_alive(record: Mapping[str, Any], root: Path) -> bool:
    """Return ``True`` when a recorded service is verifiably alive and running."""
    if record.get("type") == "compose":
        return compose_record_alive(record, root)
    pid = record.get("pid")
    if not isinstance(pid, int):
        return False
    return pid_alive(pid) and identity_matches(record)


def cmd_up(root: Path, manifest_path: Path, scope: str = "full") -> int:
    manifest = load_manifest(manifest_path)
    root = Path(root).resolve()
    runtime = ensure_runtime_dir(root)
    instance = instance_id(manifest.project, root)
    state_path = _state_path(root)

    with exclusive_lock(_lock_path(root), LOCK_TIMEOUT_SECS):
        state = read_state(state_path)
        state["instance"] = instance
        for name in prune_state(state, root):
            print(f"  pruned stale record for {name}")
        write_state(state_path, state)

        order = manifest.resolve_scope(scope)
        # If any dependency is down or missing, stop running dependents so they re-link.
        # Affected dependents must be stopped in reverse dependency order (dependents before dependencies).
        missing = [
            name
            for name in order
            if name not in state["services"]
            or not is_service_verifiable_alive(state["services"][name], root)
        ]
        affected: set[str] = set()
        queue = list(missing)
        while queue:
            curr = queue.pop(0)
            for dep in manifest.dependents(curr):
                if dep in state["services"] and dep not in affected:
                    affected.add(dep)
                    queue.append(dep)

        if affected:
            stop_order = [
                s
                for s in manifest.resolve_services(list(affected))
                if s in state["services"]
            ]
            stop_order.reverse()
            failed_stops: set[str] = set()
            for dep_name in stop_order:
                if any(child in failed_stops for child in manifest.dependents(dep_name)):
                    print(
                        f"  {dep_name}: preserving because dependent failed to stop",
                        file=sys.stderr,
                    )
                    failed_stops.add(dep_name)
                    continue
                dep_record = state["services"][dep_name]
                print(f"  {dep_name}: stopping to re-link against missing dependencies")
                outcome = _stop_record(dep_record, root)
                if outcome not in ("terminated", "killed", "stale"):
                    print(
                        f"  {dep_name}: cleanup failed ({outcome}); preserving record in state",
                        file=sys.stderr,
                    )
                    failed_stops.add(dep_name)
                else:
                    state["services"].pop(dep_name, None)
                    write_state(state_path, state)
            if failed_stops:
                return 1
            order = manifest.resolve_services(order + list(affected))

        started: list[str] = []
        for name in order:
            service = manifest.services[name]
            existing = state["services"].get(name)
            if existing is not None:
                if not is_service_verifiable_alive(existing, root):
                    print(
                        f"  {name}: recorded in state but not verifiable or running; cannot proceed",
                        file=sys.stderr,
                    )
                    _rollback(state, state_path, started, root, manifest)
                    return 1
                print(f"  {name}: already running on {existing.get('url') or 'n/a'}")
                continue
            try:
                record = _start_with_retry(
                    service, root, runtime, instance, state, state_path
                )
            except StackError as exc:
                print(f"  {name}: {exc}", file=sys.stderr)
                _rollback(state, state_path, started, root, manifest)
                return 1
            if record is None:
                _rollback(state, state_path, started, root, manifest)
                return 1
            started.append(name)
            print(f"  {name}: up on {record.get('url') or 'n/a'}")

        state["generation"] = int(state.get("generation", 0)) + 1
        write_state(state_path, state)

    _print_status(manifest, read_state(state_path), root)
    return 0


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
        record = _start_service(service, root, runtime, instance, values)
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


def cmd_down(root: Path, manifest_path: Path, scope: str = "full") -> int:
    manifest = load_manifest(manifest_path)
    root = Path(root).resolve()
    ensure_runtime_dir(root)
    state_path = _state_path(root)

    with exclusive_lock(_lock_path(root), LOCK_TIMEOUT_SECS):
        state = read_state(state_path)

        targets = manifest.teardown_scope(scope)
        blocked: list[str] = []
        for name in targets:
            for dependent in manifest.dependents(name):
                if dependent in state["services"] and dependent not in targets:
                    blocked.append(f"{name} is still needed by running service {dependent}")
        if blocked:
            for message in blocked:
                print(f"  refused: {message}", file=sys.stderr)
            print("  stop the dependent service first, or use --scope full", file=sys.stderr)
            return 1

        failures: list[str] = []
        failed_services: set[str] = set()
        for name in targets:
            dependents_failed = [
                dep
                for dep in manifest.dependents(name)
                if dep in failed_services or dep in state["services"]
            ]
            if dependents_failed:
                msg = f"{name}: preserved because dependent(s) {', '.join(dependents_failed)} are still active"
                print(f"  {msg}", file=sys.stderr)
                failures.append(msg)
                continue

            record = state["services"].get(name)
            if record is None:
                print(f"  {name}: not running")
                continue
            outcome = _stop_record(record, root)
            if outcome in ("terminated", "killed", "stale"):
                port = record.get("port")
                state["services"].pop(name, None)
                write_state(state_path, state)
                if port and not wait_for_port_release(int(port)):
                    print(f"  {name}: port {port} is still held", file=sys.stderr)
                    failures.append(f"{name}: port {port} is still held")
                print(f"  {name}: {outcome}")
            else:
                failures.append(f"{name}: {outcome}")
                failed_services.add(name)
                print(
                    f"  {name}: {outcome}; ownership could not be confirmed, "
                    f"leaving it untouched",
                    file=sys.stderr,
                )

        state["generation"] = int(state.get("generation", 0)) + 1
        write_state(state_path, state)

    return 1 if failures else 0


def cmd_status(root: Path, manifest_path: Path) -> int:
    manifest = load_manifest(manifest_path)
    root = Path(root).resolve()
    ensure_runtime_dir(root)
    state_path = _state_path(root)

    with exclusive_lock(_lock_path(root), LOCK_TIMEOUT_SECS):
        state = read_state(state_path)
        if prune_state(state, root):
            write_state(state_path, state)
        _print_status(manifest, state, root)
    return 0


def _print_status(manifest: Manifest, state: Mapping[str, Any], root: Path) -> None:
    instance = instance_id(manifest.project, root)
    print(f"{manifest.project}  instance={instance}  generation={state.get('generation', 0)}")
    width = max((len(name) for name in manifest.services), default=8)
    for name in sorted(manifest.services):
        record = state["services"].get(name)
        if record is None:
            print(f"  {name.ljust(width)}  stopped")
            continue
        health = ""
        service = manifest.services[name]
        if service.healthcheck_path and record.get("port"):
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
            f"  {name.ljust(width)}  running  {pid_text}  "
            f"{record.get('url') or 'no port'}{health}"
        )


# --------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rig",
        description="Bring this checkout's local services up and down.",
    )
    parser.add_argument("--root", default=None, help="project root (default: auto-discovered)")
    parser.add_argument("--manifest", default=None, help="path to manifest (e.g. rig.json or stack.json)")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("up", "start the selected services"),
        ("down", "stop the selected services"),
    ):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("--scope", default="full", help="scope to manage (default: full)")
    sub.add_parser("status", help="report what is running in this checkout")
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
    args = build_parser().parse_args(argv)
    root = Path(args.root).resolve() if args.root else find_project_root()
    manifest_path = Path(args.manifest) if args.manifest else find_default_manifest(root)
    try:
        if args.command == "up":
            return cmd_up(root, manifest_path, args.scope)
        if args.command == "down":
            return cmd_down(root, manifest_path, args.scope)
        return cmd_status(root, manifest_path)
    except (RigError, TimeoutError) as exc:
        print(f"rig: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("rig: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())

