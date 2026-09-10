"""Tests for rig (local dev environment and process runner)."""

import errno
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from rig import cli as rig

stack = rig
REPO_ROOT = Path(__file__).resolve().parents[1]
STACK_PATH = Path(rig.__file__).resolve()
# Bound before any fixture stubs the module attribute, so the tests for the
# resolver itself exercise the real function.
RESOLVE_DOCKER_CONTEXT = rig.resolve_current_docker_context


@pytest.fixture(autouse=True)
def _no_ambient_docker_context(monkeypatch):
    """Keep the suite off this machine's own Docker context.

    Starting a Compose service resolves the Docker context in force, which runs
    a real ``docker context show``. Every compose test would then depend on the
    Docker installation of the machine running it, so the resolver answers
    'no context' by default and the tests that care about the pinned context
    override this stub.
    """
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    monkeypatch.setattr(rig, "resolve_current_docker_context", lambda *a, **k: None)


def _write_compose_file(root, name="compose.yml"):
    """Place a compose file in a checkout so Compose, not Docker, answers for it."""
    path = Path(root) / name
    path.write_text("services: {}\n")
    return path




# --------------------------------------------------------------------------------------
# Instance identification
# --------------------------------------------------------------------------------------


def test_instance_id_is_stable_for_the_same_checkout(tmp_path):
    first = stack.instance_id("deltalytic", tmp_path)
    second = stack.instance_id("deltalytic", tmp_path)
    assert first == second


def test_instance_id_differs_between_checkouts_of_the_same_project(tmp_path):
    left = tmp_path / "checkout-a"
    right = tmp_path / "checkout-b"
    left.mkdir()
    right.mkdir()

    assert stack.instance_id("deltalytic", left) != stack.instance_id("deltalytic", right)


def test_instance_id_uses_project_prefix_and_eight_hex_digits(tmp_path):
    ident = stack.instance_id("deltalytic", tmp_path)
    project, _, digest = ident.rpartition("-")

    assert project == "deltalytic"
    assert len(digest) == 8
    assert all(char in "0123456789abcdef" for char in digest)


def test_instance_id_resolves_symlinked_checkouts_to_one_identity(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)

    assert stack.instance_id("deltalytic", link) == stack.instance_id("deltalytic", real)


def test_instance_id_is_a_valid_compose_project_name(tmp_path):
    ident = stack.instance_id("Deltalytic_Pilot", tmp_path)

    assert ident == ident.lower()
    assert all(char.isalnum() or char in "-_" for char in ident)
    assert ident[0].isalnum()


# --------------------------------------------------------------------------------------
# Runtime directory
# --------------------------------------------------------------------------------------


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


# --------------------------------------------------------------------------------------
# Lifecycle mutex
# --------------------------------------------------------------------------------------


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


def test_lock_contention_within_one_process_times_out(tmp_path):
    runtime = stack.ensure_runtime_dir(tmp_path)
    lock = runtime / "checkout.lock"

    with stack.exclusive_lock(lock, timeout=1.0):
        with pytest.raises(TimeoutError):
            with stack.exclusive_lock(lock, timeout=0.2):
                pytest.fail("the second acquisition must not succeed")


def test_lock_contention_respects_a_monotonic_deadline(tmp_path):
    runtime = stack.ensure_runtime_dir(tmp_path)
    lock = runtime / "checkout.lock"

    with stack.exclusive_lock(lock, timeout=1.0):
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            with stack.exclusive_lock(lock, timeout=0.3):
                pytest.fail("the second acquisition must not succeed")
        elapsed = time.monotonic() - started

    assert 0.25 <= elapsed < 3.0


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
        with pytest.raises(TimeoutError):
            with stack.exclusive_lock(lock, timeout=0.3):
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


def test_lock_rejects_a_symlinked_lock_path(tmp_path):
    runtime = stack.ensure_runtime_dir(tmp_path)
    victim = runtime / "victim"
    victim.write_text("")
    link = runtime / "checkout.lock"
    link.symlink_to(victim)

    with pytest.raises(stack.StackError):
        with stack.exclusive_lock(link, timeout=0.2):
            pytest.fail("a symlinked lock path must be refused")


def test_lock_descriptor_is_close_on_exec(tmp_path):
    runtime = stack.ensure_runtime_dir(tmp_path)
    lock = runtime / "checkout.lock"

    with stack.exclusive_lock(lock, timeout=1.0) as fd:
        assert os.get_inheritable(fd) is False


# --------------------------------------------------------------------------------------
# Zero-race socket transfer
# --------------------------------------------------------------------------------------


def test_allocate_listener_returns_a_bound_loopback_socket():
    listener, port = stack.allocate_listener()
    try:
        host, bound_port = listener.getsockname()
        assert host == "127.0.0.1"
        assert bound_port == port
        assert port > 0
    finally:
        listener.close()


def test_allocate_listener_holds_the_port_so_it_cannot_be_stolen():
    listener, port = stack.allocate_listener()
    try:
        thief = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        with thief:
            with pytest.raises(OSError) as raised:
                thief.bind(("127.0.0.1", port))
        assert raised.value.errno in (errno.EADDRINUSE, errno.EACCES)
    finally:
        listener.close()


def test_allocate_listener_does_not_enable_so_reuseport():
    listener, _ = stack.allocate_listener()
    try:
        reuseport = getattr(socket, "SO_REUSEPORT", None)
        if reuseport is None:
            pytest.skip("SO_REUSEPORT is unavailable on this platform")
        assert listener.getsockopt(socket.SOL_SOCKET, reuseport) == 0
    finally:
        listener.close()


ACCEPTOR = textwrap.dedent(
    """
    import socket, sys
    fd = int(sys.argv[sys.argv.index("--fd") + 1])
    listener = socket.socket(fileno=fd)
    listener.settimeout(20)
    conn, _ = listener.accept()
    conn.sendall(b"inherited")
    conn.close()
    """
)


def test_spawn_fd_service_transfers_the_listening_socket_to_the_child(tmp_path):
    script = tmp_path / "acceptor.py"
    script.write_text(ACCEPTOR)
    log = tmp_path / "acceptor.log"

    record = stack.spawn_fd_service(
        name="acceptor",
        argv=[sys.executable, str(script), "--fd", "{fd}"],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", "")},
        log_path=log,
    )
    try:
        with socket.create_connection(("127.0.0.1", record["port"]), timeout=20) as client:
            assert client.recv(64) == b"inherited"
    finally:
        stack.terminate_record(record, timeout=5.0)


def test_spawn_fd_service_never_releases_the_port_between_bind_and_start(tmp_path):
    script = tmp_path / "acceptor.py"
    script.write_text(ACCEPTOR)

    record = stack.spawn_fd_service(
        name="acceptor",
        argv=[sys.executable, str(script), "--fd", "{fd}"],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", "")},
        log_path=tmp_path / "acceptor.log",
    )
    try:
        thief = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        with thief:
            with pytest.raises(OSError) as raised:
                thief.bind(("127.0.0.1", record["port"]))
        assert raised.value.errno in (errno.EADDRINUSE, errno.EACCES)
    finally:
        stack.terminate_record(record, timeout=5.0)


def test_spawn_fd_service_closes_the_parent_descriptor_after_popen(tmp_path):
    script = tmp_path / "acceptor.py"
    script.write_text(ACCEPTOR)

    record = stack.spawn_fd_service(
        name="acceptor",
        argv=[sys.executable, str(script), "--fd", "{fd}"],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", "")},
        log_path=tmp_path / "acceptor.log",
    )
    try:
        with pytest.raises(OSError):
            os.fstat(record["fd"])
    finally:
        stack.terminate_record(record, timeout=5.0)


def test_spawn_fd_service_records_group_and_identity_evidence(tmp_path):
    script = tmp_path / "acceptor.py"
    script.write_text(ACCEPTOR)

    record = stack.spawn_fd_service(
        name="acceptor",
        argv=[sys.executable, str(script), "--fd", "{fd}"],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", "")},
        log_path=tmp_path / "acceptor.log",
    )
    try:
        assert record["pid"] > 0
        assert record["pgid"] == record["pid"], "the child must lead its own process group"
        assert record["binary"] == os.path.realpath(sys.executable)
        assert record["argv"][0] == sys.executable
        assert record["start_time"]
        assert record["url"] == f"http://127.0.0.1:{record['port']}"
    finally:
        stack.terminate_record(record, timeout=5.0)


def test_spawn_fd_service_records_a_service_that_exited_instantly(tmp_path):
    """A crash-on-startup service must produce a usable record, not an exception."""
    script = tmp_path / "dies.py"
    script.write_text("import sys\nsys.exit(3)\n")

    record = stack.spawn_fd_service(
        name="dies",
        argv=[sys.executable, str(script), "--fd", "{fd}"],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", "")},
        log_path=tmp_path / "dies.log",
    )

    assert record["pid"] > 0
    assert record["pgid"] == record["pid"]
    assert stack.identity_matches(record) is False
    assert stack.terminate_record(record, timeout=1.0) == "stale"


def test_spawn_fd_service_closes_the_listener_when_the_child_cannot_start(tmp_path):
    with pytest.raises(stack.StackError):
        stack.spawn_fd_service(
            name="missing",
            argv=[str(tmp_path / "does-not-exist"), "--fd", "{fd}"],
            cwd=tmp_path,
            env={"PATH": os.environ.get("PATH", "")},
            log_path=tmp_path / "missing.log",
        )


def test_uvicorn_argv_uses_fd_transfer_and_the_environment_interpreter():
    argv = stack.uvicorn_argv(python="/venv/bin/python", app="pkg.mod:app", factory=False)

    assert argv[:4] == ["/venv/bin/python", "-m", "uvicorn", "pkg.mod:app"]
    assert "--fd" in argv
    assert argv[argv.index("--fd") + 1] == "{fd}"
    assert "--factory" not in argv
    assert "--port" not in argv


def test_uvicorn_argv_supports_application_factories():
    argv = stack.uvicorn_argv(python="/venv/bin/python", app="pkg.mod:create_app", factory=True)

    assert "--factory" in argv


# --------------------------------------------------------------------------------------
# Port allocation for services that cannot inherit a descriptor
# --------------------------------------------------------------------------------------


def test_reserve_port_returns_a_free_port():
    port = stack.reserve_port()
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    with probe:
        probe.bind(("127.0.0.1", port))


def test_reserve_port_avoids_ports_already_listening():
    holder, taken = stack.allocate_listener()
    try:
        for _ in range(20):
            assert stack.reserve_port() != taken
    finally:
        holder.close()


def test_port_is_free_ignores_connections_lingering_in_time_wait():
    """A closed service can leave TIME_WAIT sockets on its port.

    Those must not read as "still held", or teardown reports a false failure.
    """
    listener, port = stack.allocate_listener()
    client = socket.create_connection(("127.0.0.1", port), timeout=5)
    served, _ = listener.accept()
    client.close()
    served.close()
    listener.close()

    assert stack.port_is_free(port) is True


def test_port_is_free_detects_a_live_listener():
    listener, port = stack.allocate_listener()
    try:
        assert stack.port_is_free(port) is False
    finally:
        listener.close()
    assert stack.port_is_free(port) is True


# --------------------------------------------------------------------------------------
# State file serialization
# --------------------------------------------------------------------------------------


def test_state_round_trips_through_an_atomic_write(tmp_path):
    path = tmp_path / "state.json"
    state = {
        "instance": "deltalytic-abcd1234",
        "generation": 3,
        "services": {"backend": {"pid": 42, "port": 5000}},
    }

    stack.write_state(path, state)

    assert stack.read_state(path) == state


def test_state_write_leaves_no_temporary_file_behind(tmp_path):
    path = tmp_path / "state.json"

    stack.write_state(path, {"services": {}})

    assert list(tmp_path.iterdir()) == [path]


def test_state_write_replaces_the_previous_generation_wholesale(tmp_path):
    path = tmp_path / "state.json"
    stack.write_state(path, {"generation": 1, "services": {"backend": {"pid": 1}}})

    stack.write_state(path, {"generation": 2, "services": {}})

    assert stack.read_state(path) == {"generation": 2, "services": {}}


def test_state_write_is_owner_readable_only(tmp_path):
    path = tmp_path / "state.json"

    stack.write_state(path, {"services": {}})

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_state_read_of_a_missing_file_returns_an_empty_stack(tmp_path):
    state = stack.read_state(tmp_path / "absent.json")

    assert state["services"] == {}
    assert state["generation"] == 0


def test_state_read_of_malformed_json_returns_an_empty_stack(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{ this is not json")

    state = stack.read_state(path)

    assert state["services"] == {}


def test_state_never_records_secret_values(tmp_path):
    path = tmp_path / "state.json"
    record = stack.redact(
        {
            "pid": 7,
            "port": 5000,
            "env": {"PLATFORM_TOKEN": "super-secret", "BACKEND_PORT": "5000"},
        }
    )

    stack.write_state(path, {"services": {"backend": record}})

    assert "super-secret" not in path.read_text()
    assert "5000" in path.read_text()


# --------------------------------------------------------------------------------------
# Process identity verification and teardown
# --------------------------------------------------------------------------------------


def _spawn_sleeper(tmp_path, ignore_sigterm=False):
    body = "import signal, time\n"
    if ignore_sigterm:
        body += "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    body += "time.sleep(300)\n"
    script = tmp_path / ("ignorer.py" if ignore_sigterm else "sleeper.py")
    script.write_text(body)
    proc = subprocess.Popen(
        [sys.executable, str(script)],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc, {
        "name": "sleeper",
        "pid": proc.pid,
        "pgid": os.getpgid(proc.pid),
        "binary": os.path.realpath(sys.executable),
        "argv": [sys.executable, str(script)],
        "start_time": stack.process_start_time(proc.pid),
        "port": None,
        "url": None,
    }


def test_identity_verification_accepts_the_recorded_process(tmp_path):
    proc, record = _spawn_sleeper(tmp_path)
    try:
        assert stack.identity_matches(record) is True
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_identity_verification_survives_a_launcher_that_replaces_itself(tmp_path):
    """A shebang script is replaced by its interpreter, exactly as `npm` becomes `node`.

    Verification must compare against what the kernel reported at spawn time, not
    against the requested argument vector, or a healthy service is pruned and orphaned.
    """
    script = tmp_path / "wrapper.py"
    script.write_text(f"#!{sys.executable}\n" + ACCEPTOR)
    script.chmod(0o700)

    record = stack.spawn_fd_service(
        name="wrapper",
        argv=[str(script), "--fd", "{fd}"],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", "")},
        log_path=tmp_path / "wrapper.log",
    )
    try:
        observed = stack.process_args(record["pid"])
        assert observed != " ".join(record["argv"]), (
            "this test is only meaningful when the kernel rewrites the command line"
        )
        assert record["identity"] == observed
        assert stack.identity_matches(record) is True
    finally:
        assert stack.terminate_record(record, timeout=5.0) in ("terminated", "killed")


def test_identity_baseline_prefers_the_observed_command_line():
    record = {"identity": "node /path/vite --port 1", "argv": ["npm", "run", "dev"]}

    assert stack.identity_baseline(record) == "node /path/vite --port 1"


def test_identity_baseline_falls_back_to_the_requested_argv():
    record = {"argv": ["/bin/sleep", "300"]}

    assert stack.identity_baseline(record) == "/bin/sleep 300"


def test_identity_verification_rejects_a_mismatched_start_time(tmp_path):
    proc, record = _spawn_sleeper(tmp_path)
    try:
        record["start_time"] = "1999-01-01T00:00:00"
        assert stack.identity_matches(record) is False
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_identity_verification_rejects_a_mismatched_binary(tmp_path):
    proc, record = _spawn_sleeper(tmp_path)
    try:
        record["binary"] = "/usr/bin/totally-different"
        assert stack.identity_matches(record) is False
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_identity_verification_rejects_a_dead_pid(tmp_path):
    proc, record = _spawn_sleeper(tmp_path)
    proc.kill()
    proc.wait(timeout=5)

    assert stack.identity_matches(record) is False


def test_identity_verification_rejects_a_reused_pid_running_something_else(tmp_path):
    proc, record = _spawn_sleeper(tmp_path)
    try:
        record["argv"] = [sys.executable, str(tmp_path / "some-other-script.py")]
        assert stack.identity_matches(record) is False
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_identity_verification_rejects_a_record_without_evidence():
    assert stack.identity_matches({"pid": os.getpid()}) is False
    assert stack.identity_matches({}) is False


def test_terminate_stops_the_recorded_process_group(tmp_path):
    proc, record = _spawn_sleeper(tmp_path)

    outcome = stack.terminate_record(record, timeout=5.0)

    assert outcome == "terminated"
    assert proc.wait(timeout=5) is not None
    assert stack.identity_matches(record) is False


def test_terminate_escalates_to_sigkill_when_sigterm_is_ignored(tmp_path):
    proc, record = _spawn_sleeper(tmp_path, ignore_sigterm=True)

    outcome = stack.terminate_record(record, timeout=0.5)

    assert outcome == "killed"
    assert proc.wait(timeout=5) is not None


def test_terminate_refuses_to_signal_an_unverifiable_record(tmp_path):
    proc, record = _spawn_sleeper(tmp_path)
    try:
        record["binary"] = "/usr/bin/totally-different"

        outcome = stack.terminate_record(record, timeout=1.0)

        assert outcome == "refused"
        assert proc.poll() is None, "an unverified process must be left running"
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_terminate_reports_an_already_dead_process_as_stale(tmp_path):
    proc, record = _spawn_sleeper(tmp_path)
    proc.kill()
    proc.wait(timeout=5)

    assert stack.terminate_record(record, timeout=1.0) == "stale"


def test_terminate_never_signals_the_orchestrator_own_group():
    record = {
        "name": "self",
        "pid": os.getpid(),
        "pgid": os.getpgid(0),
        "binary": os.path.realpath(sys.executable),
        "argv": sys.argv,
        "start_time": stack.process_start_time(os.getpid()),
    }

    assert stack.terminate_record(record, timeout=0.2) == "refused"


def test_terminate_releases_the_service_port(tmp_path):
    script = tmp_path / "acceptor.py"
    script.write_text(ACCEPTOR)
    record = stack.spawn_fd_service(
        name="acceptor",
        argv=[sys.executable, str(script), "--fd", "{fd}"],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", "")},
        log_path=tmp_path / "acceptor.log",
    )
    port = record["port"]

    assert stack.terminate_record(record, timeout=5.0) in ("terminated", "killed")

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and not stack.port_is_free(port):
        time.sleep(0.05)
    assert stack.port_is_free(port) is True


# --------------------------------------------------------------------------------------
# Compose scoping
# --------------------------------------------------------------------------------------


def test_compose_argv_scopes_every_call_to_this_checkout(tmp_path):
    argv = stack.compose_argv(
        instance="deltalytic-abcd1234",
        root=tmp_path,
        compose_file=tmp_path / "docker-compose.yml",
        args=["up", "-d"],
    )

    assert argv[:2] == ["docker", "compose"]
    assert "--project-directory" in argv
    assert argv[argv.index("--project-directory") + 1] == str(tmp_path)
    assert argv[argv.index("-p") + 1] == "deltalytic-abcd1234"
    assert argv[argv.index("-f") + 1] == str(tmp_path / "docker-compose.yml")
    assert argv[-2:] == ["up", "-d"]


def test_compose_argv_scopes_a_port_lookup_the_same_way(tmp_path):
    argv = stack.compose_argv(
        instance="deltalytic-abcd1234",
        root=tmp_path,
        compose_file=tmp_path / "docker-compose.yml",
        args=["port", "db", "5432"],
    )

    assert "-p" in argv and "-f" in argv and "--project-directory" in argv
    assert argv[-3:] == ["port", "db", "5432"]


def test_compose_argv_passes_an_explicit_docker_context(tmp_path):
    argv = stack.compose_argv(
        instance="deltalytic-abcd1234",
        root=tmp_path,
        compose_file=tmp_path / "docker-compose.yml",
        args=["ps"],
        context="colima",
    )

    assert argv[:4] == ["docker", "--context", "colima", "compose"]


def test_compose_port_output_is_parsed_into_a_host_port():
    assert stack.parse_compose_port("127.0.0.1:54321\n") == 54321
    assert stack.parse_compose_port("0.0.0.0:5432") == 5432
    assert stack.parse_compose_port("[::1]:5555") == 5555


def test_compose_port_output_without_a_mapping_is_rejected():
    with pytest.raises(stack.StackError):
        stack.parse_compose_port("\n")


# --------------------------------------------------------------------------------------
# Environment isolation
# --------------------------------------------------------------------------------------


def test_service_env_drops_hostile_ambient_variables(monkeypatch, tmp_path):
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "someone-else")
    monkeypatch.setenv("DOCKER_HOST", "tcp://evil:2375")
    monkeypatch.setenv("VITE_API_URL", "http://evil")
    monkeypatch.setenv("HTTP_PROXY", "http://evil:8080")
    monkeypatch.setenv("http_proxy", "http://evil:8080")
    monkeypatch.setenv("DATABASE_URL", "postgres://evil/db")

    env = stack.build_service_env({}, inherit=[], root=tmp_path, values={})

    for hostile in (
        "COMPOSE_PROJECT_NAME",
        "DOCKER_HOST",
        "VITE_API_URL",
        "HTTP_PROXY",
        "http_proxy",
        "DATABASE_URL",
    ):
        assert hostile not in env


def test_service_env_keeps_the_executable_search_path(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    env = stack.build_service_env({}, inherit=[], root=tmp_path, values={})

    assert env["PATH"] == "/usr/bin:/bin"


def test_service_env_inherits_only_explicitly_named_variables(monkeypatch, tmp_path):
    monkeypatch.setenv("WANTED", "yes")
    monkeypatch.setenv("UNWANTED", "no")

    env = stack.build_service_env({}, inherit=["WANTED"], root=tmp_path, values={})

    assert env["WANTED"] == "yes"
    assert "UNWANTED" not in env


def test_service_env_applies_orchestrator_values_last(monkeypatch, tmp_path):
    monkeypatch.setenv("BACKEND_PORT", "9999")

    env = stack.build_service_env(
        {"BACKEND_PORT": "{backend_port}"},
        inherit=["BACKEND_PORT"],
        root=tmp_path,
        values={"backend_port": 5123},
    )

    assert env["BACKEND_PORT"] == "5123"


def test_service_env_resolves_path_placeholders_against_the_project_root(tmp_path):
    env = stack.build_service_env(
        {"DATABASE_URL": "sqlite:///{root}/data/deltalytic.db"},
        inherit=[],
        root=tmp_path,
        values={},
    )

    assert env["DATABASE_URL"] == f"sqlite:///{tmp_path}/data/deltalytic.db"


def test_service_env_rejects_an_unknown_placeholder(tmp_path):
    with pytest.raises(stack.StackError):
        stack.build_service_env(
            {"X": "{no_such_value}"}, inherit=[], root=tmp_path, values={}
        )


def test_service_env_reads_a_declared_env_file_relative_to_the_project_root(tmp_path):
    (tmp_path / ".env").write_text("PLATFORM_TOKEN=from-file\n# comment\nEMPTY=\n")

    env = stack.build_service_env(
        {}, inherit=[], root=tmp_path, values={}, env_files=[".env"]
    )

    assert env["PLATFORM_TOKEN"] == "from-file"
    assert env["EMPTY"] == ""


def test_service_env_lets_manifest_values_override_the_env_file(tmp_path):
    (tmp_path / ".env").write_text("PLATFORM_TOKEN=from-file\n")

    env = stack.build_service_env(
        {"PLATFORM_TOKEN": "from-manifest"},
        inherit=[],
        root=tmp_path,
        values={},
        env_files=[".env"],
    )

    assert env["PLATFORM_TOKEN"] == "from-manifest"


# --------------------------------------------------------------------------------------
# Manifest and scopes
# --------------------------------------------------------------------------------------


def _manifest(tmp_path, payload):
    path = tmp_path / "stack.json"
    path.write_text(json.dumps(payload))
    return stack.load_manifest(path)


SAMPLE = {
    "project": "sample",
    "services": {
        "backend": {"type": "fd", "cwd": ".", "command": ["true", "--fd", "{fd}"]},
        "frontend": {
            "type": "port",
            "cwd": ".",
            "command": ["true", "--port", "{port}"],
            "depends_on": ["backend"],
        },
    },
    "scopes": {
        "full": ["backend", "frontend"],
        "local": ["backend", "frontend"],
        "backend": ["backend"],
        "ui": ["frontend"],
    },
}


def test_manifest_exposes_project_and_services(tmp_path):
    manifest = _manifest(tmp_path, SAMPLE)

    assert manifest.project == "sample"
    assert set(manifest.services) == {"backend", "frontend"}


def test_manifest_rejects_a_scope_naming_an_unknown_service(tmp_path):
    payload = json.loads(json.dumps(SAMPLE))
    payload["scopes"]["full"] = ["backend", "ghost"]

    with pytest.raises(stack.StackError):
        _manifest(tmp_path, payload)


def test_manifest_rejects_a_dependency_on_an_unknown_service(tmp_path):
    payload = json.loads(json.dumps(SAMPLE))
    payload["services"]["frontend"]["depends_on"] = ["ghost"]

    with pytest.raises(stack.StackError):
        _manifest(tmp_path, payload)


def test_manifest_rejects_a_dependency_cycle(tmp_path):
    payload = json.loads(json.dumps(SAMPLE))
    payload["services"]["backend"]["depends_on"] = ["frontend"]

    with pytest.raises(stack.StackError):
        _manifest(tmp_path, payload)


def test_manifest_rejects_an_unknown_service_type(tmp_path):
    payload = json.loads(json.dumps(SAMPLE))
    payload["services"]["backend"]["type"] = "telepathy"

    with pytest.raises(stack.StackError):
        _manifest(tmp_path, payload)


def test_manifest_rejects_a_missing_file(tmp_path):
    with pytest.raises(stack.StackError):
        stack.load_manifest(tmp_path / "absent.json")


def test_scope_resolution_orders_dependencies_before_dependents(tmp_path):
    manifest = _manifest(tmp_path, SAMPLE)

    assert manifest.resolve_scope("full") == ["backend", "frontend"]


def test_scope_resolution_pulls_in_transitive_dependencies(tmp_path):
    manifest = _manifest(tmp_path, SAMPLE)

    assert manifest.resolve_scope("ui") == ["backend", "frontend"]


def test_scope_resolution_of_a_leaf_scope_stays_narrow(tmp_path):
    manifest = _manifest(tmp_path, SAMPLE)

    assert manifest.resolve_scope("backend") == ["backend"]


def test_scope_resolution_rejects_an_unknown_scope(tmp_path):
    manifest = _manifest(tmp_path, SAMPLE)

    with pytest.raises(stack.StackError):
        manifest.resolve_scope("sideways")


def test_teardown_scope_is_reversed_and_excludes_dependencies(tmp_path):
    manifest = _manifest(tmp_path, SAMPLE)

    assert manifest.teardown_scope("full") == ["frontend", "backend"]
    assert manifest.teardown_scope("ui") == ["frontend"]


def test_dependents_of_a_service_are_reported(tmp_path):
    manifest = _manifest(tmp_path, SAMPLE)

    assert manifest.dependents("backend") == ["frontend"]
    assert manifest.dependents("frontend") == []


# --------------------------------------------------------------------------------------
# Health checks
# --------------------------------------------------------------------------------------


HTTP_PROBE = textwrap.dedent(
    """
    import http.server, sys
    from pathlib import Path

    status = int(sys.argv[1])
    body = sys.argv[2].encode()
    report = Path(sys.argv[3])

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    report.write_text(str(server.server_port))
    server.serve_forever()
    """
)


@pytest.fixture()
def http_probe(tmp_path):
    """Start a throwaway HTTP server in a separate process and return its port."""
    script = tmp_path / "probe.py"
    script.write_text(HTTP_PROBE)
    started = []
    counter = [0]

    def start(status=200, body='{"status":"ok"}'):
        counter[0] += 1
        report = tmp_path / f"probe-{counter[0]}.port"
        proc = subprocess.Popen(
            [sys.executable, str(script), str(status), body, str(report)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        started.append(proc)
        assert _await_file(report), f"probe never reported a port: {proc.communicate()[0]}"
        return int(report.read_text().strip())

    yield start
    for proc in started:
        proc.kill()
        proc.wait(timeout=5)


def test_health_check_succeeds_on_a_two_hundred_response(http_probe):
    port = http_probe()

    assert stack.wait_for_http(port, "/api/v1/health", timeout=10.0) is True


def test_health_check_fails_on_a_service_error_response(http_probe):
    port = http_probe(status=503, body="down")

    assert stack.wait_for_http(port, "/api/v1/health", timeout=1.0) is False


def test_health_check_fails_fast_when_nothing_listens():
    port = stack.reserve_port()
    started = time.monotonic()

    assert stack.wait_for_http(port, "/api/v1/health", timeout=0.6) is False
    assert time.monotonic() - started < 5.0


def test_health_check_ignores_ambient_proxy_settings(monkeypatch, http_probe):
    port = http_probe()
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:1")

    assert stack.wait_for_http(port, "/api/v1/health", timeout=10.0) is True


# --------------------------------------------------------------------------------------
# Command surface
# --------------------------------------------------------------------------------------


def test_cli_exposes_up_down_and_status_with_every_scope():
    parser = stack.build_parser()

    for command in ("up", "down"):
        for scope in ("full", "local", "backend", "ui"):
            args = parser.parse_args([command, "--scope", scope])
            assert args.command == command
            assert args.scope == scope

    assert parser.parse_args(["status"]).command == "status"


def test_cli_defaults_to_the_full_scope():
    parser = stack.build_parser()

    assert parser.parse_args(["up"]).scope == "full"


def test_cli_rejects_an_unknown_scope(tmp_path):
    manifest_path = tmp_path / "stack.json"
    manifest_path.write_text(json.dumps(SAMPLE))
    ret = stack.main(["--root", str(tmp_path), "--manifest", str(manifest_path), "up", "--scope", "sideways"])
    assert ret != 0


def test_status_of_an_empty_checkout_reports_no_services(tmp_path, capsys):
    manifest_path = tmp_path / "stack.json"
    manifest_path.write_text(json.dumps(SAMPLE))

    exit_code = stack.cmd_status(root=tmp_path, manifest_path=manifest_path)

    assert exit_code == 0
    assert "backend" in capsys.readouterr().out


def test_pruning_warns_when_an_unverifiable_record_still_holds_its_port(tmp_path, capsys):
    listener, port = stack.allocate_listener()
    try:
        state = {
            "generation": 1,
            "services": {
                "ghost": {
                    "name": "ghost",
                    "pid": 999999,
                    "pgid": 999999,
                    "binary": "/nonexistent",
                    "argv": ["/nonexistent"],
                    "start_time": "1999-01-01T00:00:00",
                    "port": port,
                }
            },
        }

        assert stack.prune_state(state, tmp_path) == ["ghost"]

        captured = capsys.readouterr()
        assert "warning" in captured.err
        assert str(port) in captured.err
        assert "orphaned" in captured.err
    finally:
        listener.close()


def test_status_prunes_a_stale_record_and_leaves_the_stack_empty(tmp_path, capsys):
    manifest_path = tmp_path / "stack.json"
    manifest_path.write_text(json.dumps(SAMPLE))
    runtime = stack.ensure_runtime_dir(tmp_path)
    stack.write_state(
        runtime / "state.json",
        {
            "generation": 1,
            "services": {
                "backend": {
                    "name": "backend",
                    "pid": 999999,
                    "pgid": 999999,
                    "binary": "/nonexistent",
                    "argv": ["/nonexistent"],
                    "start_time": "1999-01-01T00:00:00",
                    "port": 1,
                    "url": "http://127.0.0.1:1",
                }
            },
        },
    )

    exit_code = stack.cmd_status(root=tmp_path, manifest_path=manifest_path)

    assert exit_code == 0
    assert stack.read_state(runtime / "state.json")["services"] == {}


def test_down_on_an_empty_checkout_is_idempotent(tmp_path):
    manifest_path = tmp_path / "stack.json"
    manifest_path.write_text(json.dumps(SAMPLE))

    first = stack.cmd_down(root=tmp_path, manifest_path=manifest_path, scope="full")
    second = stack.cmd_down(root=tmp_path, manifest_path=manifest_path, scope="full")

    assert first == 0
    assert second == 0


def test_down_refuses_to_remove_a_dependency_a_running_service_still_needs(tmp_path):
    manifest_path = tmp_path / "stack.json"
    manifest_path.write_text(json.dumps(SAMPLE))
    runtime = stack.ensure_runtime_dir(tmp_path)
    proc, record = _spawn_sleeper(tmp_path)
    record["name"] = "frontend"
    stack.write_state(
        runtime / "state.json", {"generation": 1, "services": {"frontend": record}}
    )
    try:
        exit_code = stack.cmd_down(
            root=tmp_path, manifest_path=manifest_path, scope="backend"
        )

        assert exit_code != 0
        assert proc.poll() is None
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_up_is_idempotent_for_an_already_running_verified_service(tmp_path):
    script = tmp_path / "acceptor.py"
    script.write_text(ACCEPTOR)
    manifest_path = tmp_path / "stack.json"
    manifest_path.write_text(
        json.dumps(
            {
                "project": "sample",
                "services": {
                    "backend": {
                        "type": "fd",
                        "cwd": ".",
                        "command": [sys.executable, str(script), "--fd", "{fd}"],
                        "healthcheck_path": None,
                    }
                },
                "scopes": {"full": ["backend"], "backend": ["backend"]},
            }
        )
    )
    runtime = stack.ensure_runtime_dir(tmp_path)
    try:
        assert stack.cmd_up(root=tmp_path, manifest_path=manifest_path, scope="full") == 0
        first = stack.read_state(runtime / "state.json")["services"]["backend"]

        assert stack.cmd_up(root=tmp_path, manifest_path=manifest_path, scope="full") == 0
        second = stack.read_state(runtime / "state.json")["services"]["backend"]

        assert first["pid"] == second["pid"]
        assert first["port"] == second["port"]
    finally:
        stack.cmd_down(root=tmp_path, manifest_path=manifest_path, scope="full")


def test_up_then_down_leaves_no_process_and_frees_the_port(tmp_path):
    script = tmp_path / "acceptor.py"
    script.write_text(ACCEPTOR)
    manifest_path = tmp_path / "stack.json"
    manifest_path.write_text(
        json.dumps(
            {
                "project": "sample",
                "services": {
                    "backend": {
                        "type": "fd",
                        "cwd": ".",
                        "command": [sys.executable, str(script), "--fd", "{fd}"],
                        "healthcheck_path": None,
                    }
                },
                "scopes": {"full": ["backend"], "backend": ["backend"]},
            }
        )
    )
    runtime = stack.ensure_runtime_dir(tmp_path)

    assert stack.cmd_up(root=tmp_path, manifest_path=manifest_path, scope="full") == 0
    record = stack.read_state(runtime / "state.json")["services"]["backend"]
    port = record["port"]

    assert stack.cmd_down(root=tmp_path, manifest_path=manifest_path, scope="full") == 0
    assert stack.read_state(runtime / "state.json")["services"] == {}
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and not stack.port_is_free(port):
        time.sleep(0.05)
    assert stack.port_is_free(port) is True
    with pytest.raises(ProcessLookupError):
        os.kill(record["pid"], 0)


STRICT_BINDER = textwrap.dedent(
    """
    import socket, sys, time
    port = int(sys.argv[sys.argv.index("--port") + 1])
    listener = socket.socket()
    listener.bind(("127.0.0.1", port))
    listener.listen(8)
    time.sleep(300)
    """
)


def _port_service_manifest(tmp_path, script):
    manifest_path = tmp_path / "stack.json"
    manifest_path.write_text(
        json.dumps(
            {
                "project": "sample",
                "services": {
                    "ui": {
                        "type": "port",
                        "cwd": ".",
                        "command": [sys.executable, str(script), "--port", "{port}"],
                        "healthcheck_path": None,
                    }
                },
                "scopes": {"full": ["ui"], "ui": ["ui"]},
            }
        )
    )
    return manifest_path


def test_up_retries_a_port_service_whose_port_was_taken(tmp_path, monkeypatch):
    """A strict-port service must move to a new port, never drift onto a neighbour."""
    script = tmp_path / "binder.py"
    script.write_text(STRICT_BINDER)
    manifest_path = _port_service_manifest(tmp_path, script)
    runtime = stack.ensure_runtime_dir(tmp_path)

    squatter, taken = stack.allocate_listener()
    free = stack.reserve_port()
    handed_out = iter([taken, free])
    monkeypatch.setattr(stack, "reserve_port", lambda: next(handed_out))
    try:
        assert stack.cmd_up(root=tmp_path, manifest_path=manifest_path, scope="full") == 0

        record = stack.read_state(runtime / "state.json")["services"]["ui"]
        assert record["port"] == free, "the service must not keep the contended port"
        assert record["port"] != taken
    finally:
        stack.cmd_down(root=tmp_path, manifest_path=manifest_path, scope="full")
        squatter.close()


def test_up_gives_up_after_the_retry_budget_is_exhausted(tmp_path, monkeypatch):
    script = tmp_path / "binder.py"
    script.write_text(STRICT_BINDER)
    manifest_path = _port_service_manifest(tmp_path, script)
    runtime = stack.ensure_runtime_dir(tmp_path)

    squatter, taken = stack.allocate_listener()
    monkeypatch.setattr(stack, "reserve_port", lambda: taken)
    try:
        assert stack.cmd_up(root=tmp_path, manifest_path=manifest_path, scope="full") != 0
        assert stack.read_state(runtime / "state.json")["services"] == {}
    finally:
        squatter.close()


def test_fd_services_are_not_retried_because_they_cannot_collide(tmp_path, monkeypatch):
    calls = []
    original = stack.allocate_listener

    def counting():
        calls.append(1)
        return original()

    script = tmp_path / "dies.py"
    script.write_text("import sys\nsys.exit(1)\n")
    manifest_path = tmp_path / "stack.json"
    manifest_path.write_text(
        json.dumps(
            {
                "project": "sample",
                "services": {
                    "backend": {
                        "type": "fd",
                        "cwd": ".",
                        "command": [sys.executable, str(script), "--fd", "{fd}"],
                        "healthcheck_path": None,
                    }
                },
                "scopes": {"full": ["backend"]},
            }
        )
    )
    monkeypatch.setattr(stack, "allocate_listener", counting)

    assert stack.cmd_up(root=tmp_path, manifest_path=manifest_path, scope="full") != 0
    assert len(calls) == 1, "an inherited socket cannot be contended, so do not retry"


def test_up_rolls_back_services_it_started_when_a_later_service_fails(tmp_path):
    script = tmp_path / "acceptor.py"
    script.write_text(ACCEPTOR)
    manifest_path = tmp_path / "stack.json"
    manifest_path.write_text(
        json.dumps(
            {
                "project": "sample",
                "services": {
                    "backend": {
                        "type": "fd",
                        "cwd": ".",
                        "command": [sys.executable, str(script), "--fd", "{fd}"],
                        "healthcheck_path": None,
                    },
                    "broken": {
                        "type": "fd",
                        "cwd": ".",
                        "command": [str(tmp_path / "does-not-exist"), "--fd", "{fd}"],
                        "depends_on": ["backend"],
                    },
                },
                "scopes": {"full": ["backend", "broken"]},
            }
        )
    )
    runtime = stack.ensure_runtime_dir(tmp_path)

    exit_code = stack.cmd_up(root=tmp_path, manifest_path=manifest_path, scope="full")

    assert exit_code != 0
    assert stack.read_state(runtime / "state.json")["services"] == {}


def test_up_leaves_a_pre_existing_service_running_when_a_later_service_fails(tmp_path):
    script = tmp_path / "acceptor.py"
    script.write_text(ACCEPTOR)
    manifest_path = tmp_path / "stack.json"
    manifest_path.write_text(
        json.dumps(
            {
                "project": "sample",
                "services": {
                    "backend": {
                        "type": "fd",
                        "cwd": ".",
                        "command": [sys.executable, str(script), "--fd", "{fd}"],
                        "healthcheck_path": None,
                    },
                    "broken": {
                        "type": "fd",
                        "cwd": ".",
                        "command": [str(tmp_path / "does-not-exist"), "--fd", "{fd}"],
                        "depends_on": ["backend"],
                    },
                },
                "scopes": {"full": ["backend", "broken"], "backend": ["backend"]},
            }
        )
    )
    runtime = stack.ensure_runtime_dir(tmp_path)
    try:
        assert stack.cmd_up(root=tmp_path, manifest_path=manifest_path, scope="backend") == 0
        started = stack.read_state(runtime / "state.json")["services"]["backend"]

        assert stack.cmd_up(root=tmp_path, manifest_path=manifest_path, scope="full") != 0

        survivors = stack.read_state(runtime / "state.json")["services"]
        assert survivors["backend"]["pid"] == started["pid"]
        assert stack.identity_matches(survivors["backend"]) is True
    finally:
        stack.cmd_down(root=tmp_path, manifest_path=manifest_path, scope="full")


def test_up_records_the_dependency_port_for_dependent_services(tmp_path):
    script = tmp_path / "acceptor.py"
    script.write_text(ACCEPTOR)
    reporter = tmp_path / "reporter.py"
    reporter.write_text(
        textwrap.dedent(
            """
            import os, socket, sys, time
            port = int(sys.argv[sys.argv.index("--port") + 1])
            listener = socket.socket()
            listener.bind(("127.0.0.1", port))
            listener.listen(8)
            open(os.environ["REPORT_TO"], "w").write(os.environ.get("BACKEND_PORT", ""))
            time.sleep(300)
            """
        )
    )
    report = tmp_path / "report.txt"
    manifest_path = tmp_path / "stack.json"
    manifest_path.write_text(
        json.dumps(
            {
                "project": "sample",
                "services": {
                    "backend": {
                        "type": "fd",
                        "cwd": ".",
                        "command": [sys.executable, str(script), "--fd", "{fd}"],
                        "healthcheck_path": None,
                    },
                    "frontend": {
                        "type": "port",
                        "cwd": ".",
                        "command": [sys.executable, str(reporter), "--port", "{port}"],
                        "depends_on": ["backend"],
                        "healthcheck_path": None,
                        "env": {
                            "BACKEND_PORT": "{backend_port}",
                            "REPORT_TO": str(report),
                        },
                    },
                },
                "scopes": {"full": ["backend", "frontend"], "ui": ["frontend"]},
            }
        )
    )
    runtime = stack.ensure_runtime_dir(tmp_path)
    try:
        assert stack.cmd_up(root=tmp_path, manifest_path=manifest_path, scope="full") == 0
        services = stack.read_state(runtime / "state.json")["services"]

        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and not report.exists():
            time.sleep(0.05)
        assert report.exists(), "the dependent service never reported its configuration"
        assert report.read_text() == str(services["backend"]["port"])
    finally:
        stack.cmd_down(root=tmp_path, manifest_path=manifest_path, scope="full")


# --------------------------------------------------------------------------------------
# Deltalytic manifest bindings
# --------------------------------------------------------------------------------------


def test_repository_manifest_matches_the_repository_layout():
    manifest = stack.load_manifest(REPO_ROOT / "examples" / "stack.json")

    assert manifest.project == "deltalytic"
    assert set(manifest.services) >= {"backend", "frontend"}
    assert manifest.resolve_scope("full")
    for scope in ("full", "local", "backend", "ui"):
        assert manifest.resolve_scope(scope)


def test_repository_manifest_uses_the_real_health_endpoint():
    manifest = stack.load_manifest(REPO_ROOT / "examples" / "stack.json")

    assert manifest.services["backend"].healthcheck_path == "/api/v1/health"


def test_repository_manifest_transfers_a_socket_to_the_backend():
    manifest = stack.load_manifest(REPO_ROOT / "examples" / "stack.json")
    backend = manifest.services["backend"]

    assert backend.type == "fd"
    assert "{fd}" in backend.command
    assert "--fd" in backend.command


def test_repository_manifest_points_the_backend_at_the_project_interpreter():
    manifest = stack.load_manifest(REPO_ROOT / "examples" / "stack.json")
    backend = manifest.services["backend"]
    assert "--fd" in backend.command
    assert "{fd}" in backend.command


def test_stable_process_args_returns_a_settled_command_line(tmp_path):
    script = tmp_path / "sleeper.py"
    script.write_text("import time\ntime.sleep(60)\n")
    proc = subprocess.Popen(
        [sys.executable, str(script)],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        settled = stack.stable_process_args(proc.pid, settle=1.0)

        assert settled == f"{sys.executable} {script}"
        assert settled == stack.process_args(proc.pid)
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_stable_process_args_tolerates_a_process_that_exits():
    assert stack.stable_process_args(999999, settle=0.2) is None


def test_repository_manifest_frontend_is_the_process_that_actually_serves():
    manifest = stack.load_manifest(REPO_ROOT / "examples" / "stack.json")
    command = manifest.services["frontend"].command

    assert "npm" not in command
    assert command[0] == "node"
    assert "--strictPort" in command
    assert "{port}" in command


def test_repository_manifest_hands_the_backend_port_to_the_frontend():
    manifest = stack.load_manifest(REPO_ROOT / "examples" / "stack.json")
    frontend = manifest.services["frontend"]

    assert "backend" in frontend.depends_on
    assert frontend.env.get("BACKEND_PORT") == "{backend_port}"


def test_repository_manifest_keeps_the_backend_on_sqlite():
    manifest = stack.load_manifest(REPO_ROOT / "examples" / "stack.json")
    backend = manifest.services["backend"]

    assert backend.env["DATABASE_URL"].startswith("sqlite:")
    assert not any(
        service.type == "compose" for service in manifest.services.values()
    )


def test_repository_makefile_include_exposes_every_symmetrical_target():
    text = (REPO_ROOT / "examples" / "stack.mk").read_text()

    for target in (
        "up",
        "down",
        "local-up",
        "local-down",
        "backend-up",
        "backend-down",
        "ui-up",
        "ui-down",
        "status",
    ):
        assert f"\n{target}:" in text, f"Makefile is missing target {target}"
    phony_lines = [line for line in text.splitlines() if ".PHONY" in line or line.startswith(" ")]
    phony_all = " ".join(phony_lines)
    for target in ("up", "down", "local-up", "ui-down", "status"):
        assert target in phony_all


def test_repository_makefile_includes_the_stack_targets():
    text = (REPO_ROOT / "examples" / "stack.mk").read_text()

    assert "STACK_RUNNER" in text
    assert "scripts/stack.py" in text


def test_runtime_directory_is_ignored_by_version_control():
    assert ".local-run" in (REPO_ROOT / ".gitignore").read_text()


def test_makefile_default_goal_is_help():
    text = (REPO_ROOT / "Makefile").read_text()
    assert ".DEFAULT_GOAL := help" in text


def test_ensure_runtime_dir_creates_data_directory(tmp_path: Path):
    runtime = stack.ensure_runtime_dir(tmp_path)
    assert runtime.is_dir()
    assert (tmp_path / "data").is_dir()


def test_terminate_record_refuses_when_actual_pgid_mismatches(monkeypatch):
    record = {
        "pid": 12345,
        "pgid": 99999,
        "binary": "/bin/sh",
        "identity": "sh",
        "start_time": "12:00",
    }
    monkeypatch.setattr(stack, "pid_alive", lambda pid: True)
    monkeypatch.setattr(stack, "identity_matches", lambda rec: True)
    monkeypatch.setattr(os, "getpgid", lambda pid: 12345)  # actual pgid is 12345, recorded is 99999
    assert stack.terminate_record(record) == "refused"


def test_await_ready_fails_when_pid_dies_despite_http_success(monkeypatch):
    service = stack.Service(
        name="test",
        type="port",
        cwd=Path("."),
        command=["echo"],
        healthcheck_path="/health",
        healthcheck_timeout=1.0,
    )
    record = {"pid": 12345, "port": 8080}
    # PID is dead
    monkeypatch.setattr(stack, "pid_alive", lambda pid: False)
    assert not stack._await_ready(service, record)


def test_start_with_retry_preserves_record_if_cleanup_fails(monkeypatch, tmp_path):
    service = stack.Service(
        name="backend",
        type="fd",
        cwd=Path("."),
        command=["echo"],
        healthcheck_path="/health",
        healthcheck_timeout=0.1,
    )
    state = {"services": {}}
    state_path = tmp_path / "state.json"

    # Mock service startup so it returns a dummy record
    dummy_record = {"name": "backend", "pid": 12345, "port": 8080}
    monkeypatch.setattr(stack, "_start_service", lambda *args: dummy_record)
    # Mock readiness failure
    monkeypatch.setattr(stack, "_await_ready", lambda *args: False)
    # Mock cleanup returning "failed"
    monkeypatch.setattr(stack, "_stop_record", lambda *args: "failed")

    result = stack._start_with_retry(service, tmp_path, tmp_path, "inst-1", state, state_path)
    assert result is None
    # Record must be preserved in state because cleanup failed!
    assert "backend" in state["services"]
    assert state["services"]["backend"]["pid"] == 12345


def test_cmd_up_aborts_and_preserves_state_when_dependent_stop_fails(monkeypatch, tmp_path):
    manifest_data = {
        "project": "testproj",
        "scopes": {"backend": ["backend"], "full": ["backend", "frontend"]},
        "services": {
            "backend": {
                "type": "fd",
                "cwd": ".",
                "command": ["echo", "backend"],
                "healthcheck_path": "/health",
            },
            "frontend": {
                "type": "port",
                "cwd": ".",
                "command": ["echo", "frontend"],
                "depends_on": ["backend"],
                "healthcheck_path": "/",
            },
        },
    }
    manifest_path = tmp_path / "stack.json"
    manifest_path.write_text(json.dumps(manifest_data))

    # Pre-populate state with running frontend, missing backend
    state_path = tmp_path / ".local-run" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "instance": "test",
        "generation": 1,
        "services": {
            "frontend": {
                "name": "frontend",
                "pid": 5555,
                "pgid": 5555,
                "port": 5173,
                "start_time": "12:00",
                "binary": "/bin/echo",
                "identity": "echo",
            }
        },
    }
    state_path.write_text(json.dumps(state))

    monkeypatch.setattr(stack, "pid_alive", lambda pid: True)
    monkeypatch.setattr(stack, "identity_matches", lambda rec: True)
    # Simulate failed teardown of frontend
    monkeypatch.setattr(stack, "_stop_record", lambda *args: "refused")

    code = stack.cmd_up(root=tmp_path, manifest_path=manifest_path, scope="full")
    assert code == 1

    # Frontend record must STILL be in state!
    saved_state = stack.read_state(state_path)
    assert "frontend" in saved_state["services"]
    assert saved_state["services"]["frontend"]["pid"] == 5555


def test_cmd_up_scoped_recovery_restarts_stopped_dependents(monkeypatch, tmp_path):
    manifest_data = {
        "project": "testproj",
        "scopes": {"backend": ["backend"], "full": ["backend", "frontend"]},
        "services": {
            "backend": {
                "type": "fd",
                "cwd": ".",
                "command": ["echo", "backend"],
                "healthcheck_path": "/health",
            },
            "frontend": {
                "type": "port",
                "cwd": ".",
                "command": ["echo", "frontend"],
                "depends_on": ["backend"],
                "healthcheck_path": "/",
            },
        },
    }
    manifest_path = tmp_path / "stack.json"
    manifest_path.write_text(json.dumps(manifest_data))

    state_path = tmp_path / ".local-run" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "instance": "test",
        "generation": 1,
        "services": {
            "frontend": {
                "name": "frontend",
                "pid": 5555,
                "pgid": 5555,
                "port": 5173,
                "start_time": "12:00",
                "binary": "/bin/echo",
                "identity": "echo",
            }
        },
    }
    state_path.write_text(json.dumps(state))

    monkeypatch.setattr(stack, "pid_alive", lambda pid: True)
    monkeypatch.setattr(stack, "identity_matches", lambda rec: True)
    monkeypatch.setattr(stack, "_stop_record", lambda *args: "terminated")

    started = []

    def mock_start(service, root, runtime, instance, state, state_path):
        rec = {"name": service.name, "pid": 6000 + len(started), "port": 8000 + len(started)}
        started.append(service.name)
        state["services"][service.name] = rec
        return rec

    monkeypatch.setattr(stack, "_start_with_retry", mock_start)

    # Run scoped backend-up: backend was missing, frontend was running
    code = stack.cmd_up(root=tmp_path, manifest_path=manifest_path, scope="backend")
    assert code == 0

    # Scoped recovery should have restarted backend THEN frontend!
    assert started == ["backend", "frontend"]
    saved_state = stack.read_state(state_path)
    assert "backend" in saved_state["services"]
    assert "frontend" in saved_state["services"]


def test_await_ready_rejects_foreign_port_listener(monkeypatch):
    service = stack.Service(
        name="frontend",
        type="port",
        cwd=Path("."),
        command=["echo"],
        healthcheck_path="/",
        healthcheck_timeout=1.0,
    )
    record = {"name": "frontend", "pid": 1234, "pgid": 1234, "port": 5173}

    monkeypatch.setattr(stack, "pid_alive", lambda pid: True)
    monkeypatch.setattr(stack, "identity_matches", lambda rec: True)
    monkeypatch.setattr(stack, "wait_for_http", lambda *args, **kwargs: True)
    # Foreign listener detected on port 5173!
    monkeypatch.setattr(stack, "port_listener_matches", lambda *args, **kwargs: False)

    assert not stack._await_ready(service, record)


def test_port_listener_matches_filters_foreign_pids(monkeypatch):
    # Foreign process 9999 listening on port
    mock_run = subprocess.CompletedProcess(
        args=["lsof"], returncode=0, stdout="9999\n", stderr=""
    )
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: mock_run)
    monkeypatch.setattr(os, "getpgid", lambda pid: 9999)

    # Our process is 1234 with pgid 1234
    assert not stack.port_listener_matches(5173, pgid=1234, pid=1234)

    # If pid matches
    assert stack.port_listener_matches(5173, pgid=1234, pid=9999)

    # If child in same process group
    monkeypatch.setattr(os, "getpgid", lambda pid: 1234)
    assert stack.port_listener_matches(5173, pgid=1234, pid=1234)


def test_terminate_record_escalates_to_kill_on_surviving_child_in_pg(monkeypatch):
    record = {
        "name": "backend",
        "pid": 1234,
        "pgid": 1234,
        "binary": "/bin/echo",
        "identity": "echo",
        "start_time": "12:00",
    }
    monkeypatch.setattr(stack, "pid_alive", lambda pid: True)
    monkeypatch.setattr(stack, "identity_matches", lambda rec: True)
    # Ensure os.getpgid(0) does not collide with the tested pgid
    monkeypatch.setattr(os, "getpgid", lambda target: 99999 if target == 0 else 1234)

    signals_sent = []

    def mock_killpg(pgid, sig):
        signals_sent.append((pgid, sig))

    monkeypatch.setattr(os, "killpg", mock_killpg)

    # First call (after SIGTERM) returns False (child surviving), second call (after SIGKILL) returns True
    call_count = [0]

    def mock_await_pg_exit(pgid, timeout):
        call_count[0] += 1
        return call_count[0] > 1

    monkeypatch.setattr(stack, "_await_pg_exit", mock_await_pg_exit)

    outcome = stack.terminate_record(record, timeout=0.01)
    assert outcome == "killed"
    assert (1234, signal.SIGTERM) in signals_sent
    assert (1234, signal.SIGKILL) in signals_sent


def test_port_listener_matches_requires_positive_ownership(monkeypatch):
    import shutil

    # Missing lsof returns False
    monkeypatch.setattr(shutil, "which", lambda *_: None)
    assert not stack.port_listener_matches(8080, pgid=1234, pid=1234)

    # Subprocess error/timeout returns False
    monkeypatch.setattr(shutil, "which", lambda *_: "/usr/bin/lsof")

    def mock_run_error(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="lsof", timeout=1.0)

    monkeypatch.setattr(subprocess, "run", mock_run_error)
    assert not stack.port_listener_matches(8080, pgid=1234, pid=1234)

    # Empty stdout / no listeners returns False
    mock_empty = subprocess.CompletedProcess(args=["lsof"], returncode=0, stdout="", stderr="")
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: mock_empty)
    assert not stack.port_listener_matches(8080, pgid=1234, pid=1234)


def test_terminate_record_refuses_when_leader_dead_but_pg_alive(monkeypatch):
    record = {
        "name": "backend",
        "pid": 1234,
        "pgid": 1234,
        "binary": "/bin/echo",
        "identity": "echo",
        "start_time": "12:00",
    }
    # Leader is dead
    monkeypatch.setattr(stack, "pid_alive", lambda pid: False)
    # Surviving children in pgid
    monkeypatch.setattr(stack, "pgid_alive", lambda pgid: True)
    monkeypatch.setattr(os, "getpgid", lambda target: 99999 if target == 0 else 1234)

    # Must refuse teardown of unverifiable surviving pg rather than declaring it "stale"
    assert stack.terminate_record(record) == "refused"


def test_cmd_down_preserves_unverifiable_record_and_reports_failure(monkeypatch, tmp_path):
    manifest_data = {
        "project": "testproj",
        "scopes": {"full": ["backend"]},
        "services": {
            "backend": {
                "type": "fd",
                "cwd": ".",
                "command": ["echo", "backend"],
            },
        },
    }
    manifest_path = tmp_path / "stack.json"
    manifest_path.write_text(json.dumps(manifest_data))

    state_path = tmp_path / ".local-run" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "instance": "test",
        "generation": 1,
        "services": {
            "backend": {
                "name": "backend",
                "pid": 1234,
                "pgid": 1234,
                "port": 8080,
            }
        }
    }
    state_path.write_text(json.dumps(state))

    # Leader is dead but pg is alive, so terminate_record returns refused
    monkeypatch.setattr(stack, "pid_alive", lambda pid: False)
    monkeypatch.setattr(stack, "pgid_alive", lambda pgid: True)
    monkeypatch.setattr(os, "getpgid", lambda target: 99999 if target == 0 else 1234)

    exit_code = stack.cmd_down(root=tmp_path, manifest_path=manifest_path, scope="full")
    # Must report failure (exit code 1) rather than exiting 0 and emptying state
    assert exit_code == 1
    saved_state = stack.read_state(state_path)
    assert "backend" in saved_state["services"]


def test_cmd_down_preserves_dependencies_when_dependent_stop_fails(monkeypatch, tmp_path):
    manifest_data = {
        "project": "testproj",
        "scopes": {"full": ["backend", "frontend"]},
        "services": {
            "backend": {
                "type": "fd",
                "cwd": ".",
                "command": ["echo", "backend"],
            },
            "frontend": {
                "type": "port",
                "cwd": ".",
                "command": ["echo", "frontend"],
                "depends_on": ["backend"],
            },
        },
    }
    manifest_path = tmp_path / "stack.json"
    manifest_path.write_text(json.dumps(manifest_data))

    state_path = tmp_path / ".local-run" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "instance": "test",
        "generation": 1,
        "services": {
            "backend": {
                "name": "backend",
                "pid": 1111,
                "pgid": 1111,
                "port": 8080,
                "start_time": "12:00",
                "binary": "/bin/echo",
                "identity": "echo",
            },
            "frontend": {
                "name": "frontend",
                "pid": 2222,
                "pgid": 2222,
                "port": 5173,
                "start_time": "12:00",
                "binary": "/bin/echo",
                "identity": "echo",
            },
        },
    }
    state_path.write_text(json.dumps(state))

    monkeypatch.setattr(stack, "pid_alive", lambda pid: True)
    monkeypatch.setattr(stack, "identity_matches", lambda rec: True)

    stopped = []

    def mock_stop(record, root):
        name = record["name"]
        stopped.append(name)
        if name == "frontend":
            return "refused"  # frontend fails to stop!
        return "terminated"

    monkeypatch.setattr(stack, "_stop_record", mock_stop)

    exit_code = stack.cmd_down(root=tmp_path, manifest_path=manifest_path, scope="full")
    assert exit_code == 1

    # Frontend was attempted, but backend was NOT stopped because frontend is still active!
    assert "backend" not in stopped
    saved_state = stack.read_state(state_path)
    assert "backend" in saved_state["services"]
    assert "frontend" in saved_state["services"]


def test_status_and_prune_state_preserves_dead_leader_with_living_pg(monkeypatch, tmp_path):
    manifest_path = tmp_path / "stack.json"
    manifest_path.write_text(json.dumps(SAMPLE))
    runtime = stack.ensure_runtime_dir(tmp_path)
    state = {
        "generation": 1,
        "services": {
            "backend": {
                "name": "backend",
                "pid": 1111,
                "pgid": 1111,
                "port": 8080,
                "start_time": "12:00",
                "binary": "/bin/echo",
                "identity": "echo",
            }
        },
    }
    stack.write_state(runtime / "state.json", state)

    # Leader dead, but surviving children in pgid
    monkeypatch.setattr(stack, "record_alive", lambda rec, root: False)
    monkeypatch.setattr(stack, "pgid_alive", lambda pgid: True)

    assert stack.prune_state(state, tmp_path) == []
    assert "backend" in state["services"]

    code = stack.cmd_status(root=tmp_path, manifest_path=manifest_path)
    assert code == 0
    # State must NOT have had backend removed!
    saved = stack.read_state(runtime / "state.json")
    assert "backend" in saved["services"]


def test_rollback_preserves_dependencies_when_dependent_cleanup_fails(monkeypatch, tmp_path):
    manifest = stack.Manifest(
        project="sample",
        services={
            "backend": stack.Service("backend", "fd", Path("."), ["echo"]),
            "frontend": stack.Service("frontend", "port", Path("."), ["echo"], depends_on=["backend"]),
        },
        scopes={"full": ["backend", "frontend"]},
        path=tmp_path / "stack.json",
    )
    state_path = tmp_path / "state.json"
    state = {
        "services": {
            "backend": {"name": "backend", "pid": 1111, "pgid": 1111},
            "frontend": {"name": "frontend", "pid": 2222, "pgid": 2222},
        }
    }
    stack.write_state(state_path, state)

    stopped = []

    def mock_stop(record, root):
        name = record["name"]
        stopped.append(name)
        if name == "frontend":
            return "refused"
        return "terminated"

    monkeypatch.setattr(stack, "_stop_record", mock_stop)

    # Rollback started services [backend, frontend]
    stack._rollback(state, state_path, ["backend", "frontend"], tmp_path, manifest)

    # Frontend cleanup was attempted and failed; backend must NOT be stopped!
    assert "backend" not in stopped
    assert "backend" in state["services"]
    assert "frontend" in state["services"]


def test_compose_service_with_healthcheck_evaluates_readiness(monkeypatch, tmp_path):
    service = stack.Service(
        name="db",
        type="compose",
        cwd=Path("."),
        command=[],
        healthcheck_path="/health",
        healthcheck_timeout=1.0,
    )
    record = {"name": "db", "type": "compose", "port": 5432, "container": "c123"}

    monkeypatch.setattr(stack, "wait_for_http", lambda *args, **kwargs: True)
    monkeypatch.setattr(stack, "compose_record_alive", lambda *args, **kwargs: True)

    assert stack._await_ready(service, record, tmp_path) is True


def test_up_rejects_unverifiable_existing_services(monkeypatch, tmp_path):
    manifest_path = tmp_path / "stack.json"
    manifest_path.write_text(
        json.dumps(
            {
                "project": "sample",
                "services": {
                    "backend": {
                        "type": "fd",
                        "cwd": ".",
                        "command": ["echo"],
                    },
                    "frontend": {
                        "type": "port",
                        "cwd": ".",
                        "command": ["echo"],
                        "depends_on": ["backend"],
                    },
                },
                "scopes": {"full": ["backend", "frontend"]},
            }
        )
    )
    state = {
        "instance": "sample-inst",
        "services": {
            "backend": {
                "name": "backend",
                "type": "fd",
                "pid": 999999,
                "pgid": 999999,
                "port": 8000,
            }
        },
    }
    state_file = stack._state_path(tmp_path)
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(state))

    # Backend leader is dead and unverifiable
    monkeypatch.setattr(stack, "pid_alive", lambda pid: False)
    monkeypatch.setattr(stack, "pgid_alive", lambda pgid: True)

    ret = stack.cmd_up(root=tmp_path, manifest_path=manifest_path, scope="full")
    assert ret == 1
    # State should still preserve backend ownership evidence
    reloaded = json.loads(state_file.read_text())
    assert "backend" in reloaded["services"]


def test_cmd_up_recovery_stops_dependents_before_dependencies_and_preserves(monkeypatch, tmp_path):
    manifest_path = tmp_path / "stack.json"
    manifest_path.write_text(
        json.dumps(
            {
                "project": "sample",
                "services": {
                    "svc_a": {"type": "port", "cwd": ".", "command": ["echo"]},
                    "svc_b": {"type": "port", "cwd": ".", "command": ["echo"], "depends_on": ["svc_a"]},
                    "svc_c": {"type": "port", "cwd": ".", "command": ["echo"], "depends_on": ["svc_b"]},
                },
                "scopes": {"full": ["svc_a", "svc_b", "svc_c"]},
            }
        )
    )
    # A is missing from state; B and C are running
    state = {
        "instance": "sample-inst",
        "services": {
            "svc_b": {"name": "svc_b", "type": "port", "pid": 102, "pgid": 102, "port": 8002},
            "svc_c": {"name": "svc_c", "type": "port", "pid": 103, "pgid": 103, "port": 8003},
        },
    }
    state_file = stack._state_path(tmp_path)
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(state))

    monkeypatch.setattr(stack, "pid_alive", lambda pid: True)
    monkeypatch.setattr(stack, "pgid_alive", lambda pgid: True)
    monkeypatch.setattr(stack, "identity_matches", lambda rec: True)
    monkeypatch.setattr(stack, "is_service_verifiable_alive", lambda rec, root: True)

    stopped_order = []

    def mock_stop(record, root):
        name = record["name"]
        stopped_order.append(name)
        if name == "svc_c":
            return "refused"  # C fails to stop
        return "terminated"

    monkeypatch.setattr(stack, "_stop_record", mock_stop)

    ret = stack.cmd_up(root=tmp_path, manifest_path=manifest_path, scope="full")
    assert ret == 1
    # C should have been attempted before B, and since C failed, B should be preserved!
    assert stopped_order == ["svc_c"]
    reloaded = json.loads(state_file.read_text())
    assert "svc_b" in reloaded["services"]
    assert "svc_c" in reloaded["services"]


def test_compose_record_status_error_preserves_record(monkeypatch, tmp_path):
    _write_compose_file(tmp_path, "docker-compose.yml")
    record = {
        "name": "db",
        "type": "compose",
        "instance": "inst1",
        "compose_file": "docker-compose.yml",
        "compose_service": "db",
        "container": "cont123",
    }
    # run_compose returns non-zero exit code (daemon error)
    mock_res = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="daemon down")
    monkeypatch.setattr(stack, "run_compose", lambda *args, **kwargs: mock_res)
    # The same daemon outage leaves the Docker fallback unable to answer either,
    # so nothing proves the container is gone.
    monkeypatch.setattr(
        stack,
        "run_docker",
        lambda *a, **k: subprocess.CompletedProcess(
            ["docker"], 1, "", "cannot connect to the Docker daemon"
        ),
    )

    status = stack.compose_record_status(record, tmp_path)
    assert status == "error"
    outcome = stack._stop_record(record, tmp_path)
    assert outcome == "failed"


def test_start_compose_service_cleans_up_on_port_discovery_failure(monkeypatch, tmp_path):
    service = stack.Service(
        name="db",
        type="compose",
        cwd=Path("."),
        command=[],
        compose_file="docker-compose.yml",
        compose_service="db",
        compose_port=5432,
    )
    stopped = []
    def mock_run_compose(instance, root, compose_file, args, context=None, timeout=180.0, env=None, **kwargs):
        if "up" in args:
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="started", stderr="")
        if "ps" in args:
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="c123\n", stderr="")
        if "port" in args:
            raise RuntimeError("port inspection failed")
        if "stop" in args:
            stopped.append(args)
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="stopped", stderr="")
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    monkeypatch.setattr(stack, "run_compose", mock_run_compose)

    with pytest.raises(stack.StackError, match="compose failed to resolve port"):
        stack._start_compose_service(service, tmp_path, "inst1")

    assert len(stopped) == 1
    assert "stop" in stopped[0]


def test_manifest_string_command_parsing(tmp_path):
    manifest_path = tmp_path / "stack.json"
    manifest_path.write_text(
        json.dumps(
            {
                "project": "sample",
                "services": {
                    "backend": {
                        "type": "fd",
                        "command": 'python -m myapp --title "hello world" --fd {fd}',
                        "health": "/health",
                    }
                },
            }
        )
    )
    manifest = stack.load_manifest(manifest_path)
    backend = manifest.services["backend"]
    assert backend.command == ["python", "-m", "myapp", "--title", "hello world", "--fd", "{fd}"]
    assert backend.healthcheck_path == "/health"


def test_manifest_string_command_syntax_error(tmp_path):
    manifest_path = tmp_path / "stack.json"
    manifest_path.write_text(
        json.dumps(
            {
                "project": "sample",
                "services": {
                    "backend": {
                        "type": "fd",
                        "command": 'python -m myapp "unclosed quote',
                    }
                },
            }
        )
    )
    with pytest.raises(stack.StackError, match="invalid command syntax"):
        stack.load_manifest(manifest_path)


def test_manifest_auto_derived_scopes_and_aliases(tmp_path):
    manifest_path = tmp_path / "stack.json"
    manifest_path.write_text(
        json.dumps(
            {
                "project": "sample",
                "services": {
                    "backend": {"type": "fd", "command": "python server.py --fd {fd}"},
                    "frontend": {
                        "type": "port",
                        "command": "node vite.js --port {port}",
                        "aliases": ["ui"],
                        "depends_on": ["backend"],
                    },
                },
            }
        )
    )
    manifest = stack.load_manifest(manifest_path)
    assert set(manifest.scopes["full"]) == {"backend", "frontend"}
    assert set(manifest.scopes["local"]) == {"backend", "frontend"}
    assert manifest.scopes["backend"] == ["backend"]
    assert manifest.scopes["frontend"] == ["frontend"]
    assert manifest.scopes["ui"] == ["frontend"]

    # up ui resolves dependencies: backend then frontend
    assert manifest.resolve_scope("ui") == ["backend", "frontend"]

    # down ui tears down direct members only: frontend
    assert manifest.teardown_scope("ui") == ["frontend"]


def test_manifest_conflicting_health_and_healthcheck_path(tmp_path):
    manifest_path = tmp_path / "stack.json"
    manifest_path.write_text(
        json.dumps(
            {
                "project": "sample",
                "services": {
                    "backend": {
                        "type": "fd",
                        "command": "echo",
                        "health": "/health1",
                        "healthcheck_path": "/health2",
                    }
                },
            }
        )
    )
    with pytest.raises(stack.StackError, match="conflicting 'health' and 'healthcheck_path'"):
        stack.load_manifest(manifest_path)


def test_single_pass_rendering_tolerates_literal_braces_in_paths(monkeypatch, tmp_path):
    root_with_brace = tmp_path / "repo_{dev}"
    root_with_brace.mkdir()
    log_path = root_with_brace / "test.log"

    raw_argv = ["python", "-m", "app", "--root", "{root}", "--fd", "{fd}"]
    values = {"root": str(root_with_brace)}

    monkeypatch.setattr(stack, "allocate_listener", lambda: (socket.socket(), 8000))
    recorded = []

    def mock_popen(argv, *args, **kwargs):
        recorded.append(argv)
        class MockProc:
            pid = 12345
        return MockProc()

    monkeypatch.setattr(subprocess, "Popen", mock_popen)
    monkeypatch.setattr(stack, "_record", lambda *a, **kw: {"name": "backend"})

    stack.spawn_fd_service("backend", raw_argv, root_with_brace, {}, log_path, values=values)

    assert len(recorded) == 1
    assert str(root_with_brace) in recorded[0]


# --------------------------------------------------------------------------------------
# Rig v2 Machine-Wide Supervisor Tests
# --------------------------------------------------------------------------------------


def test_rig_state_home_override(monkeypatch, tmp_path):
    custom_state = tmp_path / "custom_rig_state"
    monkeypatch.setenv("RIG_STATE_HOME", str(custom_state))

    assert stack.get_state_home() == custom_state.resolve()
    assert stack.get_instances_dir() == custom_state.resolve() / "instances"
    assert stack.get_instance_dir("my-inst") == custom_state.resolve() / "instances" / "my-inst"

    ensured = stack.ensure_instance_dir("my-inst")
    assert ensured.is_dir()
    assert stat.S_IMODE(ensured.stat().st_mode) == 0o700


def test_manifest_with_modes_and_for_mode(tmp_path):
    manifest_path = tmp_path / "rig.json"
    manifest_data = {
        "project": "multi-stack",
        "default_mode": "native",
        "services": {
            "postgres": {
                "type": "compose",
                "compose_file": "docker-compose.yml",
                "compose_service": "postgres",
                "compose_port": 5432,
            }
        },
        "modes": {
            "native": {
                "services": {
                    "backend": {
                        "type": "fd",
                        "command": "python -m app",
                        "depends_on": ["postgres"],
                    }
                }
            },
            "container": {
                "services": {
                    "backend": {
                        "type": "compose",
                        "compose_file": "docker-compose.yml",
                        "compose_service": "backend",
                        "compose_port": 8000,
                        "depends_on": ["postgres"],
                    }
                }
            },
        },
    }
    manifest_path.write_text(json.dumps(manifest_data))

    manifest = stack.load_manifest(manifest_path)
    assert manifest.project == "multi-stack"
    assert manifest.default_mode == "native"
    assert "postgres" in manifest.base_services
    assert "native" in manifest.modes
    assert "container" in manifest.modes

    # Default mode is native
    assert manifest.active_mode == "native"
    assert manifest.services["backend"].type == "fd"

    # Switching to container mode
    container_manifest = manifest.for_mode("container")
    assert container_manifest.active_mode == "container"
    assert container_manifest.services["backend"].type == "compose"
    assert "postgres" in container_manifest.services

    # Invalid mode
    with pytest.raises(stack.StackError, match="unknown mode 'cloud'"):
        manifest.for_mode("cloud")


def test_cmd_up_mode_conflict_requires_switch(monkeypatch, tmp_path):
    monkeypatch.setenv("RIG_STATE_HOME", str(tmp_path / "state"))
    manifest_path = tmp_path / "rig.json"
    manifest_data = {
        "project": "mode-test",
        "default_mode": "native",
        "modes": {
            "native": {
                "services": {
                    "backend": {"type": "port", "cwd": ".", "command": ["echo"]}
                }
            },
            "container": {
                "services": {
                    "backend": {"type": "port", "cwd": ".", "command": ["echo"]}
                }
            },
        },
    }
    manifest_path.write_text(json.dumps(manifest_data))

    monkeypatch.setattr(stack, "pid_alive", lambda pid: True)
    monkeypatch.setattr(stack, "identity_matches", lambda rec: True)
    monkeypatch.setattr(stack, "_await_ready", lambda *args, **kwargs: True)

    # Bring up in native mode
    dummy_native = {"name": "backend", "type": "port", "pid": 1001, "pgid": 1001, "port": 8000}
    monkeypatch.setattr(stack, "_start_service", lambda *args, **kwargs: dummy_native)

    ret = stack.cmd_up(tmp_path, manifest_path, mode="native")
    assert ret == 0

    # Try to bring up in container mode without --switch
    dummy_container = {"name": "backend", "type": "port", "pid": 1002, "pgid": 1002, "port": 8000}
    monkeypatch.setattr(stack, "_start_service", lambda *args, **kwargs: dummy_container)

    with pytest.raises(stack.RigError) as exc_info:
        stack.cmd_up(tmp_path, manifest_path, mode="container", switch=False)
    assert exc_info.value.code == "E_MODE_CONFLICT"
    assert exc_info.value.exit_code == stack.EXIT_MUTEX_CONFLICT

    # Bring up with switch=True
    stopped = []
    monkeypatch.setattr(stack, "_stop_record", lambda rec, root: stopped.append(rec["name"]) or "terminated")
    ret_switch = stack.cmd_up(tmp_path, manifest_path, mode="container", switch=True)
    assert ret_switch == 0
    assert "backend" in stopped

    # State now reflects container mode
    state = stack.read_state(stack._state_path(tmp_path))
    assert state["mode"] == "container"


def test_cmd_ps_empty_and_populated(monkeypatch, tmp_path, capsys):
    state_home = tmp_path / "rig_state"
    monkeypatch.setenv("RIG_STATE_HOME", str(state_home))

    # Empty registry
    ret = stack.cmd_ps(as_json=True)
    assert ret == 0
    captured = capsys.readouterr()
    res = json.loads(captured.out)
    assert res["schema"] == "rig.ps/1"
    assert res["ok"] is True
    assert res["data"]["instances"] == []

    # Populate an instance
    inst_dir = stack.ensure_instance_dir("proj-12345678")
    state = {
        "instance": "proj-12345678",
        "project": "proj",
        "mode": "native",
        "root": str(tmp_path),
        "services": {
            "api": {
                "name": "api",
                "type": "fd",
                "pid": 55555,
                "pgid": 55555,
                "port": 8080,
                "url": "http://127.0.0.1:8080",
                "binary": "/usr/bin/python",
                "argv": ["python", "app.py"],
                "start_time": "Thu Jan 1 00:00:00 2026",
                "identity": "python app.py",
            }
        },
    }
    stack.write_state(inst_dir / "state.json", state)

    monkeypatch.setattr(stack, "pid_alive", lambda pid: True)
    monkeypatch.setattr(stack, "identity_matches", lambda rec: True)

    # Human table
    ret_table = stack.cmd_ps(as_json=False)
    assert ret_table == 0
    table_out = capsys.readouterr().out
    assert "proj" in table_out
    assert "proj-12345678" in table_out
    assert "running" in table_out

    # JSON output
    ret_json = stack.cmd_ps(as_json=True)
    assert ret_json == 0
    json_out = json.loads(capsys.readouterr().out)
    assert json_out["ok"] is True
    assert len(json_out["data"]["instances"]) == 1
    inst_data = json_out["data"]["instances"][0]
    assert inst_data["project"] == "proj"
    assert inst_data["status"] == "running"
    assert inst_data["services"]["api"]["status"] == "running"


def test_cmd_down_by_instance_id_and_project_slug(monkeypatch, tmp_path):
    state_home = tmp_path / "rig_state"
    monkeypatch.setenv("RIG_STATE_HOME", str(state_home))

    inst_dir = stack.ensure_instance_dir("alpha-11223344")
    state = {
        "instance": "alpha-11223344",
        "project": "alpha",
        "root": str(tmp_path),
        "services": {
            "web": {"name": "web", "type": "port", "pid": 1234, "pgid": 1234, "port": 3000}
        },
    }
    stack.write_state(inst_dir / "state.json", state)

    stopped = []
    monkeypatch.setattr(stack, "_stop_record", lambda rec, root: stopped.append(rec["name"]) or "terminated")

    # Stop by project slug "alpha"
    ret = stack.cmd_down(target="alpha")
    assert ret == 0
    assert stopped == ["web"]

    # Stopping again when empty
    ret2 = stack.cmd_down(target="alpha-11223344")
    assert ret2 == 0

    # Unknown target
    with pytest.raises(stack.RigError) as exc_info:
        stack.cmd_down(target="nonexistent")
    assert exc_info.value.code == "E_NOT_FOUND"
    assert exc_info.value.exit_code == stack.EXIT_NOT_FOUND


def test_cmd_down_ambiguous_slug_error(monkeypatch, tmp_path):
    state_home = tmp_path / "rig_state"
    monkeypatch.setenv("RIG_STATE_HOME", str(state_home))

    dir1 = stack.ensure_instance_dir("beta-11111111")
    stack.write_state(dir1 / "state.json", {"instance": "beta-11111111", "project": "beta", "services": {}})

    dir2 = stack.ensure_instance_dir("beta-22222222")
    stack.write_state(dir2 / "state.json", {"instance": "beta-22222222", "project": "beta", "services": {}})

    with pytest.raises(stack.RigError) as exc_info:
        stack.cmd_down(target="beta")
    assert exc_info.value.code == "E_AMBIGUOUS"
    assert exc_info.value.exit_code == stack.EXIT_NOT_FOUND


def test_cmd_down_all(monkeypatch, tmp_path):
    state_home = tmp_path / "rig_state"
    monkeypatch.setenv("RIG_STATE_HOME", str(state_home))

    dir1 = stack.ensure_instance_dir("proj1-11111111")
    stack.write_state(
        dir1 / "state.json",
        {"instance": "proj1-11111111", "project": "proj1", "services": {"s1": {"name": "s1", "type": "port", "pid": 11, "pgid": 11}}},
    )

    dir2 = stack.ensure_instance_dir("proj2-22222222")
    stack.write_state(
        dir2 / "state.json",
        {"instance": "proj2-22222222", "project": "proj2", "services": {"s2": {"name": "s2", "type": "port", "pid": 22, "pgid": 22}}},
    )

    stopped = []
    monkeypatch.setattr(stack, "_stop_record", lambda rec, root: stopped.append(rec["name"]) or "terminated")

    ret = stack.cmd_down(all_instances=True)
    assert ret == 0
    assert "s1" in stopped
    assert "s2" in stopped


def test_cmd_down_orphaned_instance(monkeypatch, tmp_path):
    state_home = tmp_path / "rig_state"
    monkeypatch.setenv("RIG_STATE_HOME", str(state_home))

    # Checkout was deleted: root path does not exist
    deleted_root = tmp_path / "deleted_repo"
    inst_dir = stack.ensure_instance_dir("orphan-99999999")
    stack.write_state(
        inst_dir / "state.json",
        {
            "instance": "orphan-99999999",
            "project": "orphan",
            "root": str(deleted_root),
            "services": {
                "orphan_svc": {"name": "orphan_svc", "type": "port", "pid": 999, "pgid": 999}
            },
        },
    )

    stopped = []
    monkeypatch.setattr(stack, "_stop_record", lambda rec, root: stopped.append(rec["name"]) or "terminated")

    ret = stack.cmd_down(target="orphan-99999999")
    assert ret == 0
    assert "orphan_svc" in stopped


def test_cmd_prune(monkeypatch, tmp_path):
    state_home = tmp_path / "rig_state"
    monkeypatch.setenv("RIG_STATE_HOME", str(state_home))

    # Instance with dead root and empty services
    dead_dir = stack.ensure_instance_dir("dead-00000000")
    stack.write_state(
        dead_dir / "state.json",
        {"instance": "dead-00000000", "project": "dead", "root": str(tmp_path / "nonexistent"), "services": {}},
    )

    # Active instance
    alive_dir = stack.ensure_instance_dir("alive-11111111")
    stack.write_state(
        alive_dir / "state.json",
        {"instance": "alive-11111111", "project": "alive", "root": str(tmp_path), "services": {"s": {"name": "s", "type": "port", "pid": 123, "pgid": 123}}},
    )
    monkeypatch.setattr(stack, "pid_alive", lambda pid: True)
    monkeypatch.setattr(stack, "identity_matches", lambda rec: True)

    ret = stack.cmd_prune()
    assert ret == 0
    # The instance contents are reclaimed, but the lock inode every command
    # contends for is deliberately left in place.
    assert not (dead_dir / stack.STATE_FILE_NAME).exists()
    assert (dead_dir / stack.LOCK_FILE_NAME).exists()
    assert (alive_dir / stack.STATE_FILE_NAME).is_file()


def test_cmd_check(tmp_path):
    manifest_path = tmp_path / "rig.json"
    manifest_path.write_text(
        json.dumps(
            {
                "project": "check-test",
                "services": {
                    "valid_svc": {"type": "port", "cwd": ".", "command": ["echo"]}
                },
            }
        )
    )

    ret_ok = stack.cmd_check(tmp_path, manifest_path)
    assert ret_ok == stack.EXIT_OK

    # Bad working directory
    bad_manifest = tmp_path / "bad_rig.json"
    bad_manifest.write_text(
        json.dumps(
            {
                "project": "bad-test",
                "services": {
                    "bad_cwd": {"type": "port", "cwd": "nonexistent_dir", "command": ["echo"]}
                },
            }
        )
    )
    ret_fail = stack.cmd_check(tmp_path, bad_manifest)
    assert ret_fail == stack.EXIT_USAGE


def test_cmd_init_fastapi_and_package_json(tmp_path):
    proj_dir = tmp_path / "sample_app"
    proj_dir.mkdir()
    (proj_dir / "pyproject.toml").write_text("[project]\nname = 'sample_app'\ndependencies = ['fastapi', 'uvicorn']\n")
    (proj_dir / "package.json").write_text('{"name": "frontend", "scripts": {"dev": "vite"}}\n')

    ret = stack.cmd_init(proj_dir, dry_run=False)
    assert ret == 0
    manifest_file = proj_dir / "rig.json"
    assert manifest_file.is_file()
    data = json.loads(manifest_file.read_text())
    assert data["project"] == "sample-app"
    assert "native" in data["modes"]
    services = data["modes"]["native"]["services"]
    assert "backend" in services
    assert "frontend" in services
    assert services["frontend"]["depends_on"] == ["backend"]

    # Re-running without --force fails
    with pytest.raises(stack.RigError) as exc_info:
        stack.cmd_init(proj_dir)
    assert exc_info.value.code == "E_USAGE"


def test_cmd_schema(capsys):
    ret = stack.cmd_schema()
    assert ret == 0
    captured = capsys.readouterr()
    schema = json.loads(captured.out)
    assert schema["title"] == "RigManifest"
    assert "modes" in schema["properties"]
    assert "services" in schema["properties"]


def test_main_json_envelope_success_and_error(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("RIG_STATE_HOME", str(tmp_path / "state"))

    # Successful command with --json
    code = stack.main(["schema", "--json"])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["schema"] == "rig.schema/1"
    assert out["ok"] is True

    # Error command with --json
    code_err = stack.main(["down", "nonexistent_target", "--json"])
    assert code_err == stack.EXIT_NOT_FOUND
    err_out = json.loads(capsys.readouterr().out)
    assert err_out["schema"] == "rig.error/1"
    assert err_out["ok"] is False
    assert err_out["error"]["code"] == "E_NOT_FOUND"
    assert err_out["error"]["exit_code"] == stack.EXIT_NOT_FOUND


def test_main_argument_parsing_error_emits_json_envelope(capsys):
    # Missing required command with --json
    code = stack.main(["--json"])
    assert code == stack.EXIT_USAGE
    err = json.loads(capsys.readouterr().out)
    assert err["schema"] == "rig.error/1"
    assert err["ok"] is False
    assert err["error"]["code"] == "E_USAGE"
    assert err["error"]["exit_code"] == stack.EXIT_USAGE

    # Unknown argument with --json
    code2 = stack.main(["status", "--invalid-flag", "--json"])
    assert code2 == stack.EXIT_USAGE
    err2 = json.loads(capsys.readouterr().out)
    assert err2["schema"] == "rig.error/1"
    assert err2["error"]["code"] == "E_USAGE"


def test_manifest_for_mode_scopes_isolation(tmp_path):
    manifest_data = {
        "project": "scope-iso",
        "default_mode": "native",
        "modes": {
            "native": {
                "services": {
                    "backend": {"type": "port", "cwd": ".", "command": ["echo"]}
                }
            },
            "container": {
                "services": {
                    "backend": {"type": "port", "cwd": ".", "command": ["echo"]},
                    "db": {"type": "compose", "compose_file": "compose.yml", "compose_service": "db"},
                }
            },
        },
    }
    manifest_path = tmp_path / "rig.json"
    manifest_path.write_text(json.dumps(manifest_data))

    raw = stack.load_manifest(manifest_path)
    native_m = raw.for_mode("native")
    container_m = raw.for_mode("container")

    assert native_m.scopes["full"] == ["backend"]
    assert sorted(container_m.scopes["full"]) == ["backend", "db"]
    # Verify native_m scopes were not contaminated by container_m
    assert "db" not in native_m.scopes["full"]


def test_cmd_up_switch_aborts_and_preserves_active_mode_when_stop_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("RIG_STATE_HOME", str(tmp_path / "state"))
    manifest_path = tmp_path / "rig.json"
    manifest_data = {
        "project": "switch-fail",
        "default_mode": "native",
        "modes": {
            "native": {
                "services": {
                    "backend": {"type": "port", "cwd": ".", "command": ["echo"]}
                }
            },
            "container": {
                "services": {
                    "backend": {"type": "port", "cwd": ".", "command": ["echo"]}
                }
            },
        },
    }
    manifest_path.write_text(json.dumps(manifest_data))

    monkeypatch.setattr(stack, "pid_alive", lambda pid: True)
    monkeypatch.setattr(stack, "pgid_alive", lambda pgid: True)
    monkeypatch.setattr(stack, "identity_matches", lambda rec: True)
    monkeypatch.setattr(stack, "is_service_verifiable_alive", lambda rec, root: True)
    monkeypatch.setattr(stack, "_await_ready", lambda *args, **kwargs: True)

    dummy_native = {"name": "backend", "type": "port", "pid": 4321, "pgid": 4321, "port": 8000}
    monkeypatch.setattr(stack, "_start_service", lambda *args, **kwargs: dummy_native)

    # Bring up in native mode first
    assert stack.cmd_up(tmp_path, manifest_path, mode="native") == 0

    state_file = stack._state_path(tmp_path)
    monkeypatch.setattr(stack, "_stop_record", lambda rec, root: "refused")

    with pytest.raises(stack.RigError) as exc_info:
        stack.cmd_up(tmp_path, manifest_path, mode="container", switch=True, as_json=True)
    assert exc_info.value.code == "E_SWITCH_FAILED"
    assert exc_info.value.exit_code == stack.EXIT_REFUSED

    # Mode must still be native in state!
    saved_state = stack.read_state(state_file)
    assert saved_state["mode"] == "native"
    assert "backend" in saved_state["services"]


def test_cmd_prune_respects_living_pgid(monkeypatch, tmp_path, capsys):
    state_home = tmp_path / "rig_state"
    monkeypatch.setenv("RIG_STATE_HOME", str(state_home))

    inst_dir = stack.ensure_instance_dir("prune-test-1234")
    state = {
        "instance": "prune-test-1234",
        "project": "prune-test",
        "mode": "native",
        "root": str(tmp_path / "deleted_repo"),
        "services": {
            "worker": {
                "name": "worker",
                "type": "port",
                "pid": 99999,  # Leader PID is dead
                "pgid": 99999,  # Process group still has living child
                "port": 9000,
            }
        },
    }
    stack.write_state(inst_dir / "state.json", state)

    # Leader dead, but PGID alive!
    monkeypatch.setattr(stack, "pid_alive", lambda pid: False)
    monkeypatch.setattr(stack, "pgid_alive", lambda pgid: True)

    ret = stack.cmd_prune(force=False, as_json=True)
    assert ret == 0
    out = json.loads(capsys.readouterr().out)
    assert out["data"]["pruned"] == []
    assert inst_dir.exists(), "Instance directory must not be pruned when pgid is still alive"


def test_manifest_validation_rejects_malformed_structures(tmp_path):
    p = tmp_path / "invalid.json"

    # Services is not a mapping
    p.write_text(json.dumps({"project": "x", "services": ["not", "a", "dict"]}))
    with pytest.raises(stack.StackError, match="must be a JSON object"):
        stack.load_manifest(p)

    # env is not a dict
    p.write_text(json.dumps({"project": "x", "services": {"s": {"type": "port", "command": ["echo"], "env": "foo"}}}))
    with pytest.raises(stack.StackError, match="must be a JSON object"):
        stack.load_manifest(p)

    # depends_on is not a list
    p.write_text(json.dumps({"project": "x", "services": {"s": {"type": "port", "command": ["echo"], "depends_on": "other"}}}))
    with pytest.raises(stack.StackError, match="'depends_on' must be a list"):
        stack.load_manifest(p)


def test_cmd_init_dangling_symlink(tmp_path):
    manifest_link = tmp_path / "rig.json"
    manifest_link.symlink_to(tmp_path / "nonexistent.json")

    with pytest.raises(stack.RigError) as exc_info:
        stack.cmd_init(tmp_path, dry_run=False, force=False)
    assert exc_info.value.code == "E_USAGE"




# --------------------------------------------------------------------------------------
# Round 2 review remediations
# --------------------------------------------------------------------------------------


def _write_instance(ident: str, state: dict) -> Path:
    inst_dir = stack.ensure_instance_dir(ident)
    stack.write_state(inst_dir / stack.STATE_FILE_NAME, state)
    return inst_dir


def test_prune_keeps_lock_inode_and_is_idempotent(monkeypatch, tmp_path, capsys):
    """R2-1: pruning must never unlink the lock file other processes contend for."""
    monkeypatch.setenv("RIG_STATE_HOME", str(tmp_path / "rig_state"))
    inst_dir = _write_instance(
        "dead-00000000",
        {
            "instance": "dead-00000000",
            "project": "dead",
            "root": str(tmp_path / "gone"),
            "services": {},
        },
    )
    lock_file = inst_dir / stack.LOCK_FILE_NAME
    with stack.exclusive_lock(lock_file):
        pass
    lock_inode = lock_file.stat().st_ino

    assert stack.cmd_prune(as_json=True) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["data"]["pruned"] == ["dead-00000000"]
    assert not (inst_dir / stack.STATE_FILE_NAME).exists()
    assert lock_file.exists()
    assert lock_file.stat().st_ino == lock_inode

    # A second prune finds nothing left to reclaim.
    assert stack.cmd_prune(as_json=True) == 0
    out2 = json.loads(capsys.readouterr().out)
    assert out2["data"]["pruned"] == []


def test_force_prune_preserves_dependency_when_dependent_refuses(monkeypatch, tmp_path, capsys):
    """R2-2: a dependency must survive when its dependent cannot be stopped."""
    monkeypatch.setenv("RIG_STATE_HOME", str(tmp_path / "rig_state"))
    inst_dir = _write_instance(
        "stuck-00000000",
        {
            "instance": "stuck-00000000",
            "project": "stuck",
            "root": str(tmp_path),
            "services": {
                "db": {"name": "db", "type": "port", "pid": 111, "pgid": 111, "depends_on": []},
                "api": {
                    "name": "api",
                    "type": "port",
                    "pid": 222,
                    "pgid": 222,
                    "depends_on": ["db"],
                },
            },
        },
    )
    monkeypatch.setattr(stack, "pid_alive", lambda pid: True)
    monkeypatch.setattr(stack, "pgid_alive", lambda pgid: True)
    monkeypatch.setattr(stack, "identity_matches", lambda rec: True)

    stopped: list[str] = []

    def fake_stop(record, root, remove=False):
        stopped.append(record["name"])
        assert remove is True, "a forced prune must reclaim the container, not just stop it"
        return "failed" if record["name"] == "api" else "terminated"

    monkeypatch.setattr(stack, "_stop_record", fake_stop)

    ret = stack.cmd_prune(force=True, as_json=True)
    out = json.loads(capsys.readouterr().out)

    assert ret == stack.EXIT_OP_FAILED
    assert out["ok"] is False
    assert out["data"]["pruned"] == []
    assert stopped == ["api"], "db must not be stopped once its dependent failed"
    saved = stack.read_state(inst_dir / stack.STATE_FILE_NAME)
    assert set(saved["services"]) == {"db", "api"}
    assert any("api" in entry for entry in out["data"]["failed"][0]["failed"])


def _mode_manifest(tmp_path, project: str) -> Path:
    manifest_path = tmp_path / "rig.json"
    manifest_path.write_text(
        json.dumps(
            {
                "project": project,
                "default_mode": "native",
                "modes": {
                    "native": {"services": {"backend": {"type": "port", "command": ["echo"]}}},
                    "container": {"services": {"backend": {"type": "port", "command": ["echo"]}}},
                },
            }
        )
    )
    return manifest_path


def test_mode_switch_required_when_only_pgid_survives(monkeypatch, tmp_path):
    """R2-3: an unverifiable but living process group still blocks a mode change."""
    monkeypatch.setenv("RIG_STATE_HOME", str(tmp_path / "rig_state"))
    manifest_path = _mode_manifest(tmp_path, "pgid-mode")
    root = tmp_path
    instance = stack.instance_id("pgid-mode", root)
    state_path = stack._state_path(root, instance=instance)
    stack.write_state(
        state_path,
        {
            "instance": instance,
            "project": "pgid-mode",
            "root": str(root),
            "mode": "native",
            "services": {
                "backend": {"name": "backend", "type": "port", "pid": 4321, "pgid": 4321}
            },
        },
    )
    monkeypatch.setattr(stack, "pid_alive", lambda pid: False)
    monkeypatch.setattr(stack, "pgid_alive", lambda pgid: True)

    with pytest.raises(stack.RigError) as exc_info:
        stack.cmd_up(root, manifest_path, mode="container", as_json=True)
    assert exc_info.value.code == "E_MODE_CONFLICT"
    assert exc_info.value.exit_code == stack.EXIT_MUTEX_CONFLICT


def test_invalid_scope_rejected_before_any_teardown(monkeypatch, tmp_path):
    """R2-4: an unknown scope must be a usage error, not a reason to stop the stack."""
    monkeypatch.setenv("RIG_STATE_HOME", str(tmp_path / "rig_state"))
    manifest_path = _mode_manifest(tmp_path, "scope-guard")
    root = tmp_path
    instance = stack.instance_id("scope-guard", root)
    state_path = stack._state_path(root, instance=instance)
    stack.write_state(
        state_path,
        {
            "instance": instance,
            "project": "scope-guard",
            "root": str(root),
            "mode": "native",
            "services": {
                "backend": {"name": "backend", "type": "port", "pid": 4321, "pgid": 4321}
            },
        },
    )
    monkeypatch.setattr(stack, "is_service_verifiable_alive", lambda rec, root: True)

    def refuse(record, root):
        raise AssertionError("no service may be stopped for an invalid scope")

    monkeypatch.setattr(stack, "_stop_record", refuse)

    with pytest.raises(stack.RigError) as exc_info:
        stack.cmd_up(root, manifest_path, scope="nope", mode="container", switch=True, as_json=True)
    assert exc_info.value.code == "E_USAGE"
    assert exc_info.value.exit_code == stack.EXIT_USAGE

    saved = stack.read_state(state_path)
    assert saved["mode"] == "native"
    assert "backend" in saved["services"]


def test_recovery_stops_state_only_consumer_of_shared_dependency(monkeypatch, tmp_path):
    """R2-5: every recorded consumer of a restarted dependency must be stopped."""
    monkeypatch.setenv("RIG_STATE_HOME", str(tmp_path / "rig_state"))
    manifest_path = tmp_path / "rig.json"
    manifest_path.write_text(
        json.dumps(
            {
                "project": "shared-dep",
                "services": {
                    "db": {"type": "port", "command": ["echo", "db"]},
                    "api": {"type": "port", "command": ["echo", "api"], "depends_on": ["db"]},
                },
            }
        )
    )
    root = tmp_path
    instance = stack.instance_id("shared-dep", root)
    state_path = stack._state_path(root, instance=instance)
    stack.write_state(
        state_path,
        {
            "instance": instance,
            "project": "shared-dep",
            "root": str(root),
            "mode": "default",
            "services": {
                "db": {"name": "db", "type": "port", "pid": 11, "pgid": 11, "depends_on": []},
                "api": {
                    "name": "api",
                    "type": "port",
                    "pid": 22,
                    "pgid": 22,
                    "depends_on": ["db"],
                },
                "worker": {
                    "name": "worker",
                    "type": "port",
                    "pid": 33,
                    "pgid": 33,
                    "depends_on": ["db"],
                },
            },
        },
    )
    # db's leader is gone; api and worker are still running against its old port.
    monkeypatch.setattr(stack, "pid_alive", lambda pid: pid != 11)
    monkeypatch.setattr(stack, "pgid_alive", lambda pgid: pgid != 11)
    monkeypatch.setattr(stack, "identity_matches", lambda rec: rec.get("pid") != 11)
    monkeypatch.setattr(stack, "_await_ready", lambda *a, **k: True)

    stopped: list[str] = []

    def fake_stop(record, root):
        stopped.append(record["name"])
        return "terminated"

    monkeypatch.setattr(stack, "_stop_record", fake_stop)
    monkeypatch.setattr(
        stack,
        "_start_service",
        lambda service, *a, **k: {
            "name": service.name,
            "type": "port",
            "pid": 900,
            "pgid": 900,
            "port": 9000,
            "depends_on": list(service.depends_on),
        },
    )

    assert stack.cmd_up(root, manifest_path, scope="db") == stack.EXIT_OK
    assert "worker" in stopped, "state-only consumer of db was left running"
    saved = stack.read_state(state_path)
    assert "worker" not in saved["services"]
    assert "db" in saved["services"]


def test_compose_partial_start_persists_container_when_cleanup_fails(monkeypatch, tmp_path):
    """R2-6: an undiscoverable Compose container must stay recorded for teardown."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text("services:\n  db:\n    image: postgres:16\n")
    service = stack.Service(
        name="db",
        type="compose",
        compose_file="compose.yml",
        compose_service="db",
        compose_port=5432,
    )
    calls: list[list[str]] = []

    def fake_run_compose(instance, root, cfile, args, context=None, timeout=180.0, env=None, **kwargs):
        calls.append(list(args))
        verb = args[0]
        if verb == "up":
            return subprocess.CompletedProcess(["docker"], 0, "", "")
        if verb == "ps":
            return subprocess.CompletedProcess(["docker"], 0, "deadbeefcafe\n", "")
        if verb == "port":
            return subprocess.CompletedProcess(["docker"], 1, "", "no such port")
        return subprocess.CompletedProcess(["docker"], 1, "", "cleanup refused")

    monkeypatch.setattr(stack, "run_compose", fake_run_compose)

    with pytest.raises(stack.RigError) as exc_info:
        stack._start_compose_service(service, tmp_path, "inst-1")

    verbs = [c[0] for c in calls]
    assert "stop" in verbs and "rm" in verbs
    partial = exc_info.value.details.get("partial_record")
    assert partial is not None
    assert partial["container"] == "deadbeefcafe"

    # _start_with_retry must publish that partial record so `down`/`prune` can find it.
    state = {"generation": 0, "services": {}}
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(
        stack, "_start_service", lambda *a, **k: (_ for _ in ()).throw(exc_info.value)
    )
    with pytest.raises(stack.RigError):
        stack._start_with_retry(service, tmp_path, tmp_path, "inst-1", state, state_path)
    assert state["services"]["db"]["container"] == "deadbeefcafe"


def test_healthcheck_field_validation(tmp_path):
    """R2-7: healthcheck_timeout and healthcheck_path are validated as usage errors."""
    manifest_path = tmp_path / "rig.json"

    def load(spec):
        manifest_path.write_text(json.dumps({"project": "hc", "services": {"s": spec}}))
        return stack.load_manifest(manifest_path)

    for bad in (0, -1, "fast", float("inf")):
        with pytest.raises(stack.RigError) as exc_info:
            load({"type": "port", "command": ["echo"], "healthcheck_timeout": bad})
        assert exc_info.value.code == "E_USAGE"
        assert "healthcheck_timeout" in exc_info.value.message

    with pytest.raises(stack.RigError) as exc_info:
        load({"type": "port", "command": ["echo"], "healthcheck_path": 5})
    assert exc_info.value.code == "E_USAGE"
    with pytest.raises(stack.RigError):
        load({"type": "port", "command": ["echo"], "healthcheck_path": ""})

    manifest = load({"type": "port", "command": ["echo"], "healthcheck_timeout": 2.5})
    assert manifest.services["s"].healthcheck_timeout == 2.5


def test_reverse_dependency_order_tolerates_duplicate_dependencies():
    """R2-8: a repeated depends_on entry must not corrupt the teardown order."""
    services = {
        "db": {"depends_on": []},
        "api": {"depends_on": ["db", "db", "db"]},
    }
    order = stack.reverse_dependency_order(services)
    assert order.index("api") < order.index("db")

    deeper = {
        "db": {"depends_on": []},
        "cache": {"depends_on": ["db", "db"]},
        "api": {"depends_on": ["cache", "cache", "db"]},
    }
    deep_order = stack.reverse_dependency_order(deeper)
    assert deep_order.index("api") < deep_order.index("cache") < deep_order.index("db")

    # A self-referential record must not hang or vanish.
    assert set(stack.reverse_dependency_order({"a": {"depends_on": ["a"]}})) == {"a"}


def test_down_json_envelope_reports_failure(monkeypatch, tmp_path, capsys):
    """R2-9: a failed teardown must not be reported as ok in the JSON envelope."""
    monkeypatch.setenv("RIG_STATE_HOME", str(tmp_path / "rig_state"))
    _write_instance(
        "fail-00000000",
        {
            "instance": "fail-00000000",
            "project": "fail",
            "root": str(tmp_path),
            "services": {"api": {"name": "api", "type": "port", "pid": 7, "pgid": 7}},
        },
    )
    monkeypatch.setattr(stack, "_stop_record", lambda rec, root: "failed")

    assert stack.cmd_down(target="fail-00000000", as_json=True) == stack.EXIT_OP_FAILED
    single = json.loads(capsys.readouterr().out)
    assert single["ok"] is False

    assert stack.cmd_down(all_instances=True, as_json=True) == stack.EXIT_OP_FAILED
    every = json.loads(capsys.readouterr().out)
    assert every["ok"] is False


def test_down_json_envelope_reports_local_failure(monkeypatch, tmp_path, capsys):
    """R2-9: the local checkout path must also mark a failed teardown."""
    monkeypatch.setenv("RIG_STATE_HOME", str(tmp_path / "rig_state"))
    manifest_path = tmp_path / "rig.json"
    manifest_path.write_text(
        json.dumps({"project": "local-fail", "services": {"api": {"type": "port", "command": ["echo"]}}})
    )
    instance = stack.instance_id("local-fail", tmp_path)
    stack.write_state(
        stack._state_path(tmp_path, instance=instance),
        {
            "instance": instance,
            "project": "local-fail",
            "root": str(tmp_path),
            "services": {"api": {"name": "api", "type": "port", "pid": 7, "pgid": 7}},
        },
    )
    monkeypatch.setattr(stack, "_stop_record", lambda rec, root: "failed")

    assert stack.cmd_down(tmp_path, manifest_path, as_json=True) == stack.EXIT_OP_FAILED
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["ok"] is False
    assert envelope["data"]["failures"]


def test_invalid_manifest_reports_usage_exit_code(tmp_path):
    """R2-10: manifest syntax, schema and scope errors exit with EXIT_USAGE."""
    manifest_path = tmp_path / "rig.json"

    manifest_path.write_text("{not json")
    with pytest.raises(stack.RigError) as exc_info:
        stack.load_manifest(manifest_path)
    assert (exc_info.value.code, exc_info.value.exit_code) == ("E_USAGE", stack.EXIT_USAGE)

    manifest_path.write_text(json.dumps({"services": {"s": {"type": "port", "command": ["echo"]}}}))
    with pytest.raises(stack.RigError) as exc_info:
        stack.load_manifest(manifest_path)
    assert exc_info.value.exit_code == stack.EXIT_USAGE

    manifest_path.write_text(
        json.dumps(
            {
                "project": "p",
                "services": {"s": {"type": "port", "command": ["echo"]}},
                "scopes": {"weird": [7]},
            }
        )
    )
    with pytest.raises(stack.RigError) as exc_info:
        stack.load_manifest(manifest_path)
    assert (exc_info.value.code, exc_info.value.exit_code) == ("E_USAGE", stack.EXIT_USAGE)

    manifest_path.write_text(
        json.dumps({"project": "p", "services": {"s": {"type": "port", "command": ["echo"]}}})
    )
    manifest = stack.load_manifest(manifest_path)
    with pytest.raises(stack.RigError) as exc_info:
        manifest.resolve_scope("missing")
    assert (exc_info.value.code, exc_info.value.exit_code) == ("E_USAGE", stack.EXIT_USAGE)


def test_fd_service_requires_lsof(monkeypatch, tmp_path):
    """R2-11: fd services verify their listener with lsof, so it must be present."""
    monkeypatch.setattr(stack.shutil, "which", lambda name: None if name == "lsof" else f"/usr/bin/{name}")
    service = stack.Service(name="api", type="fd", app="app:app")
    runtime = stack.ensure_runtime_dir(tmp_path)
    with pytest.raises(stack.RigError) as exc_info:
        stack._start_service(service, tmp_path, runtime, "inst", {"root": str(tmp_path)})
    assert exc_info.value.code == "E_EXTERNAL_TOOL"
    assert exc_info.value.exit_code == stack.EXIT_EXTERNAL_TOOL


def test_check_resolves_binaries_against_service_cwd(tmp_path):
    """R2-12: a relative command is resolved against root/cwd, not the process cwd."""
    sub = tmp_path / "sub"
    (sub / "bin").mkdir(parents=True)
    binary = sub / "bin" / "app"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)

    manifest_path = tmp_path / "rig.json"
    manifest_path.write_text(
        json.dumps(
            {
                "project": "cwdcheck",
                "services": {
                    "app": {"type": "port", "cwd": "sub", "command": ["./bin/app", "{port}"]}
                },
            }
        )
    )
    assert stack.cmd_check(tmp_path, manifest_path) == stack.EXIT_OK

    binary.chmod(0o644)
    assert stack.cmd_check(tmp_path, manifest_path) == stack.EXIT_USAGE

    # A path that only exists relative to the repository root is not runnable
    # from the service working directory.
    (tmp_path / "bin").mkdir()
    root_only = tmp_path / "bin" / "other"
    root_only.write_text("#!/bin/sh\nexit 0\n")
    root_only.chmod(0o755)
    manifest_path.write_text(
        json.dumps(
            {
                "project": "cwdcheck",
                "services": {
                    "app": {"type": "port", "cwd": "sub", "command": ["./bin/other"]}
                },
            }
        )
    )
    assert stack.cmd_check(tmp_path, manifest_path) == stack.EXIT_USAGE


def test_init_compose_detection_uses_service_names_and_images(tmp_path):
    """R2-13: an application that merely mentions postgres is not a database."""
    proj = tmp_path / "app"
    proj.mkdir()
    (proj / "compose.yml").write_text(
        textwrap.dedent(
            """\
            services:
              web:
                build: .
                environment:
                  DATABASE_URL: postgres://user:pw@database:5432/app
                  REDIS_URL: redis://cache:6379/0
              database:
                image: postgres:16-alpine
                ports:
                  - "5432:5432"
              cache:
                image: valkey/valkey:8
            """
        )
    )
    assert stack.cmd_init(proj, dry_run=False) == stack.EXIT_OK
    data = json.loads((proj / "rig.json").read_text())
    services = data["services"]
    assert services["postgres"]["compose_service"] == "database"
    assert services["redis"]["compose_service"] == "cache"


def test_init_compose_detection_ignores_non_database_image(tmp_path):
    """R2-13: a service named db running something else is not treated as postgres."""
    proj = tmp_path / "mysqlapp"
    proj.mkdir()
    (proj / "compose.yml").write_text(
        "services:\n  db:\n    image: mysql:8\n    ports:\n      - \"3306:3306\"\n"
    )
    assert stack.cmd_init(proj, dry_run=False) == stack.EXIT_OK
    data = json.loads((proj / "rig.json").read_text())
    assert "postgres" not in data.get("services", {})


def test_write_state_does_not_follow_predictable_temp_symlink(tmp_path):
    """R2-14: a planted temp-file symlink must not divert the state write."""
    target_dir = tmp_path / "state"
    target_dir.mkdir()
    state_file = target_dir / "state.json"
    victim = tmp_path / "victim.txt"
    victim.write_text("untouched")
    (target_dir / "state.json.tmp").symlink_to(victim)

    stack.write_state(state_file, {"generation": 1, "services": {}})

    assert victim.read_text() == "untouched"
    assert json.loads(state_file.read_text())["generation"] == 1


def test_init_force_does_not_follow_predictable_temp_symlink(tmp_path):
    """R2-14: `init --force` must not write through a planted temp symlink."""
    proj = tmp_path / "forced"
    proj.mkdir()
    (proj / "pyproject.toml").write_text("[project]\nname='forced'\ndependencies=['fastapi']\n")
    (proj / "rig.json").write_text("{}\n")
    victim = tmp_path / "victim.txt"
    victim.write_text("untouched")
    (proj / f".rig.json.tmp.{os.getpid()}").symlink_to(victim)

    assert stack.cmd_init(proj, force=True) == stack.EXIT_OK
    assert victim.read_text() == "untouched"
    assert json.loads((proj / "rig.json").read_text())["project"] == "forced"


# --------------------------------------------------------------------------------------
# Round 3 remediations
# --------------------------------------------------------------------------------------


def _compose_service(name="db", **kwargs):
    return stack.Service(
        name=name,
        type="compose",
        compose_file="compose.yml",
        compose_service=name,
        **kwargs,
    )


def test_compose_up_failure_records_partial_when_cleanup_fails(monkeypatch, tmp_path):
    """R3-1: a timed-out `up` whose cleanup fails must still publish the partial record."""
    service = _compose_service()
    calls: list[list[str]] = []

    def fake_run_compose(instance, root, cfile, args, context=None, timeout=180.0, env=None, **kwargs):
        calls.append(list(args))
        if args[0] == "up":
            raise stack.StackError("compose command timed out: up")
        # Both `stop` and `rm` refuse, so the container cannot be reclaimed.
        return subprocess.CompletedProcess(["docker"], 1, "", "cleanup refused")

    monkeypatch.setattr(stack, "run_compose", fake_run_compose)

    with pytest.raises(stack.RigError) as exc_info:
        stack._start_compose_service(service, tmp_path, "inst-1")

    partial = exc_info.value.details.get("partial_record")
    assert partial is not None
    assert partial["name"] == "db"
    assert partial["type"] == "compose"
    assert exc_info.value.__cause__ is not None
    assert [c[0] for c in calls].count("up") == 1


def test_compose_up_failure_reraises_when_cleanup_succeeds(monkeypatch, tmp_path):
    """R3-1: a clean reclaim keeps the original failure and records nothing."""
    service = _compose_service()

    def fake_run_compose(instance, root, cfile, args, context=None, timeout=180.0, env=None, **kwargs):
        if args[0] == "up":
            raise stack.StackError("compose command timed out: up")
        return subprocess.CompletedProcess(["docker"], 0, "", "")

    monkeypatch.setattr(stack, "run_compose", fake_run_compose)

    with pytest.raises(stack.StackError) as exc_info:
        stack._start_compose_service(service, tmp_path, "inst-1")
    assert "timed out" in str(exc_info.value)
    assert exc_info.value.details.get("partial_record") is None


def _undiscovered_record():
    return {
        "name": "db",
        "type": "compose",
        "instance": "inst-1",
        "compose_file": "compose.yml",
        "compose_service": "db",
        "container": "",
    }


def test_compose_status_probes_service_when_container_unknown(monkeypatch, tmp_path):
    """R3-2: an empty container ID must not be read as `absent` without asking Docker."""
    _write_compose_file(tmp_path)
    monkeypatch.setattr(
        stack,
        "run_compose",
        lambda *a, **k: subprocess.CompletedProcess(["docker"], 0, "abc123\n", ""),
    )
    inspected: list[list[str]] = []

    def fake_run_docker(args, context=None, timeout=60.0, **kwargs):
        inspected.append(list(args))
        return subprocess.CompletedProcess(["docker"], 0, "running\n", "")

    monkeypatch.setattr(stack, "run_docker", fake_run_docker)

    assert stack.compose_record_status(_undiscovered_record(), tmp_path) == "alive"
    assert inspected and inspected[0][0] == "inspect"
    assert "abc123" in inspected[0]


def test_compose_status_absent_only_when_docker_confirms(monkeypatch, tmp_path):
    """R3-2: `absent` requires a successful Compose query that found no container."""
    _write_compose_file(tmp_path)
    monkeypatch.setattr(
        stack,
        "run_compose",
        lambda *a, **k: subprocess.CompletedProcess(["docker"], 0, "\n", ""),
    )
    assert stack.compose_record_status(_undiscovered_record(), tmp_path) == "absent"

    monkeypatch.setattr(
        stack,
        "run_compose",
        lambda *a, **k: subprocess.CompletedProcess(["docker"], 1, "", "daemon down"),
    )
    # Compose cannot answer, and neither can the Docker fallback, so the state
    # stays unknown rather than being read as absence.
    monkeypatch.setattr(
        stack,
        "run_docker",
        lambda *a, **k: subprocess.CompletedProcess(
            ["docker"], 1, "", "cannot connect to the Docker daemon"
        ),
    )
    assert stack.compose_record_status(_undiscovered_record(), tmp_path) == "error"

    monkeypatch.setattr(
        stack,
        "run_compose",
        lambda *a, **k: subprocess.CompletedProcess(["docker"], 0, "abc123\n", ""),
    )
    monkeypatch.setattr(
        stack,
        "run_docker",
        lambda *a, **k: subprocess.CompletedProcess(["docker"], 1, "", "no such object"),
    )
    assert stack.compose_record_status(_undiscovered_record(), tmp_path) == "error"


def test_stop_record_reclaims_undiscovered_compose_container(monkeypatch, tmp_path):
    """R3-2: an untracked container is stopped and removed by service name."""
    _write_compose_file(tmp_path)
    calls: list[list[str]] = []

    def fake_run_compose(instance, root, cfile, args, context=None, timeout=180.0, env=None, **kwargs):
        calls.append(list(args))
        return subprocess.CompletedProcess(["docker"], 0, "abc123\n", "")

    monkeypatch.setattr(stack, "run_compose", fake_run_compose)
    monkeypatch.setattr(
        stack,
        "run_docker",
        lambda *a, **k: subprocess.CompletedProcess(["docker"], 0, "running\n", ""),
    )

    assert stack._stop_record(_undiscovered_record(), tmp_path) == "terminated"
    verbs = [c[0] for c in calls]
    assert "stop" in verbs and "rm" in verbs
    assert ["rm", "-f", "db"] in calls


def test_cmd_down_refuses_when_only_state_records_the_dependency(monkeypatch, tmp_path):
    """R3-3: a dependent known only to state must still block teardown."""
    monkeypatch.setenv("RIG_STATE_HOME", str(tmp_path / "state"))
    manifest_path = tmp_path / "rig.json"
    manifest_path.write_text(
        json.dumps(
            {
                "project": "deps",
                "scopes": {"full": ["db"]},
                "services": {"db": {"type": "port", "cwd": ".", "command": ["echo"]}},
            }
        )
    )

    state_path = stack._state_path(tmp_path)
    stack.write_state(
        state_path,
        {
            "instance": "deps",
            "generation": 1,
            "services": {
                "db": {"name": "db", "type": "port", "pid": 11, "pgid": 11},
                # `api` was started in another mode: the manifest no longer knows it.
                "api": {
                    "name": "api",
                    "type": "port",
                    "pid": 12,
                    "pgid": 12,
                    "depends_on": ["db"],
                },
            },
        },
    )

    stopped: list[str] = []
    monkeypatch.setattr(
        stack, "_stop_record", lambda rec, root: stopped.append(rec["name"]) or "terminated"
    )

    exit_code = stack.cmd_down(root=tmp_path, manifest_path=manifest_path, scope="full")
    assert exit_code == stack.EXIT_REFUSED
    assert stopped == []
    assert "db" in stack.read_state(state_path)["services"]


def test_cmd_up_mode_conflict_holds_when_compose_status_is_unknown(monkeypatch, tmp_path):
    """R3-4: a Compose record Docker cannot inspect still occupies the active mode."""
    monkeypatch.setenv("RIG_STATE_HOME", str(tmp_path / "state"))
    _write_compose_file(tmp_path)
    manifest_path = tmp_path / "rig.json"
    manifest_path.write_text(
        json.dumps(
            {
                "project": "mode-compose",
                "default_mode": "native",
                "modes": {
                    "native": {
                        "services": {"api": {"type": "port", "cwd": ".", "command": ["echo"]}}
                    },
                    "container": {
                        "services": {"api": {"type": "port", "cwd": ".", "command": ["echo"]}}
                    },
                },
            }
        )
    )

    state_path = stack._state_path(tmp_path)
    stack.write_state(
        state_path,
        {
            "instance": stack.instance_id("mode-compose", tmp_path),
            "generation": 1,
            "mode": "native",
            "services": {
                "db": {
                    "name": "db",
                    "type": "compose",
                    "pid": None,
                    "pgid": None,
                    "instance": "inst-1",
                    "compose_file": str(tmp_path / "compose.yml"),
                    "compose_service": "db",
                    "container": "abc123",
                }
            },
        },
    )

    # Docker cannot answer, so the record's liveness is unknown, not dead.
    monkeypatch.setattr(
        stack,
        "run_compose",
        lambda *a, **k: subprocess.CompletedProcess(["docker"], 1, "", "daemon down"),
    )
    monkeypatch.setattr(
        stack,
        "run_docker",
        lambda *a, **k: subprocess.CompletedProcess(
            ["docker"], 1, "", "cannot connect to the Docker daemon"
        ),
    )

    with pytest.raises(stack.RigError) as exc_info:
        stack.cmd_up(tmp_path, manifest_path, mode="container", switch=False)
    assert exc_info.value.code == "E_MODE_CONFLICT"
    assert exc_info.value.exit_code == stack.EXIT_MUTEX_CONFLICT


def test_cmd_down_preserves_dependency_for_recorded_only_dependent(monkeypatch, tmp_path):
    """R3-3: a recorded dependency edge also protects a dependency mid-teardown."""
    monkeypatch.setenv("RIG_STATE_HOME", str(tmp_path / "state"))
    manifest_path = tmp_path / "rig.json"
    manifest_path.write_text(
        json.dumps(
            {
                "project": "deps2",
                "scopes": {"full": ["db", "api"]},
                "services": {
                    # The manifest no longer declares api -> db; only state does.
                    "db": {"type": "port", "cwd": ".", "command": ["echo"]},
                    "api": {"type": "port", "cwd": ".", "command": ["echo"]},
                },
            }
        )
    )

    state_path = stack._state_path(tmp_path)
    stack.write_state(
        state_path,
        {
            "instance": "deps2",
            "generation": 1,
            "services": {
                "db": {"name": "db", "type": "port", "pid": 11, "pgid": 11},
                "api": {
                    "name": "api",
                    "type": "port",
                    "pid": 12,
                    "pgid": 12,
                    "depends_on": ["db"],
                },
            },
        },
    )

    attempted: list[str] = []

    def fake_stop(record, root):
        attempted.append(record["name"])
        return "failed" if record["name"] == "api" else "terminated"

    monkeypatch.setattr(stack, "_stop_record", fake_stop)

    exit_code = stack.cmd_down(root=tmp_path, manifest_path=manifest_path, scope="full")
    assert exit_code == stack.EXIT_OP_FAILED
    # `db` must never be stopped while its recorded dependent survives.
    assert attempted == ["api"]
    saved = stack.read_state(state_path)["services"]
    assert "db" in saved and "api" in saved


def test_compose_up_passes_no_deps(monkeypatch, tmp_path):
    """R4-1: `compose up` must pass `--no-deps` to prevent starting unrecorded dependencies."""
    compose_calls: list[list[str]] = []

    def fake_compose(instance, root, cfile, args, context=None, timeout=180.0, env=None, **kwargs):
        compose_calls.append(list(args))
        if "up" in args:
            return subprocess.CompletedProcess(["docker"], 0, "", "")
        if "ps" in args:
            return subprocess.CompletedProcess(["docker"], 0, "c123\n", "")
        if "port" in args:
            return subprocess.CompletedProcess(["docker"], 0, "0.0.0.0:5432\n", "")
        return subprocess.CompletedProcess(["docker"], 0, "", "")

    monkeypatch.setattr(stack, "run_compose", fake_compose)
    monkeypatch.setattr(stack, "port_is_free", lambda p: True)

    service = stack.Service(
        name="db",
        type="compose",
        cwd=Path("."),
        compose_file="docker-compose.yml",
        compose_service="db",
        compose_port=5432,
    )
    manifest = stack.Manifest(
        project="proj",
        services={"db": service},
        scopes={"full": ["db"]},
        path=tmp_path / "rig.json",
    )
    record = stack._start_compose_service(
        service,
        tmp_path,
        "inst1",
    )
    assert record["container"] == "c123"
    up_calls = [c for c in compose_calls if "up" in c]
    assert up_calls, "Expected a compose up call"
    assert "--no-deps" in up_calls[0]


def test_compose_status_queries_all_and_reports_stopped(monkeypatch, tmp_path):
    """R4-2: `compose_record_status` queries with `-a` and inspects container status."""
    _write_compose_file(tmp_path)
    ps_args: list[list[str]] = []

    def fake_compose(instance, root, cfile, args, context=None, timeout=60.0, env=None, **kwargs):
        ps_args.append(list(args))
        return subprocess.CompletedProcess(["docker"], 0, "c123\n", "")

    def fake_docker(args, context=None, timeout=60.0, **kwargs):
        # Container exists but is stopped (exited)
        return subprocess.CompletedProcess(["docker"], 0, "exited\n", "")

    monkeypatch.setattr(stack, "run_compose", fake_compose)
    monkeypatch.setattr(stack, "run_docker", fake_docker)

    record = {
        "name": "db",
        "type": "compose",
        "instance": "inst1",
        "compose_file": "compose.yml",
        "compose_service": "db",
        "container": "c123",
    }
    status = stack.compose_record_status(record, tmp_path)
    assert status == "stopped"
    assert any("-a" in c or "--all" in c for c in ps_args)


def test_cmd_down_local_orders_by_merged_dependencies(monkeypatch, tmp_path):
    """R4-3: `cmd_down` orders targets using merged dependency graph from state and manifest."""
    manifest_path = tmp_path / "rig.json"
    manifest_path.write_text(
        json.dumps(
            {
                "project": "merged_down",
                "scopes": {"full": ["db", "api"]},
                "services": {
                    "db": {"type": "port", "cwd": ".", "command": ["echo"]},
                    "api": {"type": "port", "cwd": ".", "command": ["echo"]},
                },
            }
        )
    )

    state_path = stack._state_path(tmp_path)
    stack.write_state(
        state_path,
        {
            "instance": "merged_down",
            "generation": 1,
            "services": {
                "db": {"name": "db", "type": "port", "pid": 201, "pgid": 201},
                "api": {
                    "name": "api",
                    "type": "port",
                    "pid": 202,
                    "pgid": 202,
                    "depends_on": ["db"],
                },
            },
        },
    )

    stopped_order: list[str] = []

    def fake_stop(record, root):
        stopped_order.append(record["name"])
        return "terminated"

    monkeypatch.setattr(stack, "_stop_record", fake_stop)

    exit_code = stack.cmd_down(root=tmp_path, manifest_path=manifest_path, scope="full")
    assert exit_code == 0
    # Dependent `api` must be stopped before dependency `db`
    assert stopped_order == ["api", "db"]



# --------------------------------------------------------------------------------------
# Round 5: reclaim, recovery and instance status
# --------------------------------------------------------------------------------------


def _reclaimable_compose_state(instance: str = "recl-00000000") -> dict:
    return {
        "instance": instance,
        "project": "recl",
        "services": {
            "db": {
                "name": "db",
                "type": "compose",
                "instance": instance,
                "compose_file": "compose.yml",
                "compose_service": "db",
                "container": "c0ffee",
            }
        },
    }


def test_force_stop_removes_compose_container_before_dropping_record(monkeypatch, tmp_path):
    """R5-1: a reclaimed compose service must lose its container, not just be stopped."""
    _write_compose_file(tmp_path)
    calls: list[list[str]] = []

    def fake_run_compose(instance, root, cfile, args, context=None, timeout=180.0, env=None, **kwargs):
        calls.append(list(args))
        if args and args[0] == "ps":
            return subprocess.CompletedProcess(["docker"], 0, "c0ffee\n", "")
        return subprocess.CompletedProcess(["docker"], 0, "", "")

    monkeypatch.setattr(stack, "run_compose", fake_run_compose)
    monkeypatch.setattr(
        stack,
        "run_docker",
        lambda *a, **k: subprocess.CompletedProcess(["docker"], 0, "exited\n", ""),
    )

    state = _reclaimable_compose_state()
    failures = stack._force_stop_instance(state, tmp_path / "state.json", tmp_path)

    assert failures == []
    # The record may only be dropped once the container it owns is gone.
    assert state["services"] == {}
    assert ["stop", "db"] in calls
    assert ["rm", "-f", "db"] in calls
    # Volumes must survive a reclaim: local data is not the supervisor's to destroy.
    assert not any("-v" in args for args in calls)


def test_force_stop_retains_record_when_compose_removal_fails(monkeypatch, tmp_path):
    """R5-1: a container that cannot be removed keeps its ownership record."""
    _write_compose_file(tmp_path)
    calls: list[list[str]] = []

    def fake_run_compose(instance, root, cfile, args, context=None, timeout=180.0, env=None, **kwargs):
        calls.append(list(args))
        if args and args[0] == "ps":
            return subprocess.CompletedProcess(["docker"], 0, "c0ffee\n", "")
        if args and args[0] == "rm":
            return subprocess.CompletedProcess(["docker"], 1, "", "device or resource busy")
        return subprocess.CompletedProcess(["docker"], 0, "", "")

    def fake_run_docker(args, context=None, timeout=60.0, **kwargs):
        # Docker is the fallback for a refused Compose teardown, so the removal
        # must refuse here too for the container to stay unreclaimed.
        args = list(args)
        if args[0] == "rm":
            return subprocess.CompletedProcess(["docker"], 1, "", "device or resource busy")
        if args[0] == "ps":
            return subprocess.CompletedProcess(["docker"], 0, "c0ffee\n", "")
        return subprocess.CompletedProcess(["docker"], 0, "exited\n", "")

    monkeypatch.setattr(stack, "run_compose", fake_run_compose)
    monkeypatch.setattr(stack, "run_docker", fake_run_docker)

    state = _reclaimable_compose_state()
    state_file = tmp_path / "state.json"
    failures = stack._force_stop_instance(state, state_file, tmp_path)

    assert failures == ["db: failed"]
    assert "db" in state["services"]
    assert "db" in stack.read_state(state_file)["services"]
    assert ["rm", "-f", "db"] in calls


def test_cmd_up_recovers_exited_compose_dependency(monkeypatch, tmp_path, capsys):
    """R5-2: an exited compose dependency must be restarted, not rejected as unhealthy."""
    monkeypatch.setenv("RIG_STATE_HOME", str(tmp_path / "rig_state"))
    (tmp_path / "compose.yml").write_text("services:\n  db:\n    image: postgres\n")
    manifest_path = tmp_path / "rig.json"
    manifest_path.write_text(
        json.dumps(
            {
                "project": "recover",
                "services": {
                    "db": {
                        "type": "compose",
                        "compose_file": "compose.yml",
                        "compose_service": "db",
                    },
                    "api": {
                        "type": "port",
                        "cwd": ".",
                        "command": ["echo"],
                        "depends_on": ["db"],
                    },
                },
                "scopes": {"full": ["db", "api"]},
            }
        )
    )
    instance = stack.instance_id("recover", tmp_path)
    state_path = stack._state_path(tmp_path, instance=instance)
    stack.write_state(
        state_path,
        {
            "instance": instance,
            "project": "recover",
            "root": str(tmp_path),
            "services": {
                "db": {
                    "name": "db",
                    "type": "compose",
                    "instance": instance,
                    "compose_file": str(tmp_path / "compose.yml"),
                    "compose_service": "db",
                    "container": "c0ffee",
                },
                "api": {
                    "name": "api",
                    "type": "port",
                    "pid": 4242,
                    "pgid": 4242,
                    "port": 8000,
                    "depends_on": ["db"],
                },
            },
        },
    )

    # The recorded container exists but has exited; the dependent is still healthy.
    monkeypatch.setattr(stack, "compose_record_status", lambda rec, root: "stopped")
    monkeypatch.setattr(stack, "pid_alive", lambda pid: True)
    monkeypatch.setattr(stack, "pgid_alive", lambda pgid: True)
    monkeypatch.setattr(stack, "identity_matches", lambda rec: True)

    stop_calls: list[tuple[str, bool]] = []

    def fake_stop(record, root, remove=False):
        stop_calls.append((record["name"], remove))
        return "terminated"

    started: list[str] = []

    def fake_start(service, root, runtime, instance, state, state_path):
        started.append(service.name)
        record = {
            "name": service.name,
            "type": service.type,
            "port": 9000 + len(started),
            "url": f"http://127.0.0.1:{9000 + len(started)}",
        }
        state["services"][service.name] = record
        stack.write_state(state_path, state)
        return record

    monkeypatch.setattr(stack, "_stop_record", fake_stop)
    monkeypatch.setattr(stack, "_start_with_retry", fake_start)

    ret = stack.cmd_up(root=tmp_path, manifest_path=manifest_path, scope="full", as_json=True)
    envelope = json.loads(capsys.readouterr().out)

    assert ret == stack.EXIT_OK
    assert envelope["ok"] is True
    # The exited dependency is reclaimed (container removed) and started again,
    # then the dependent that was stopped to re-link comes back with it.
    assert ("db", True) in stop_calls
    assert started == ["db", "api"]
    assert set(stack.read_state(state_path)["services"]) == {"db", "api"}


def test_cmd_up_reports_unreclaimable_record_and_preserves_it(monkeypatch, tmp_path):
    """R5-2: recovery still fails loudly when the stale record cannot be reclaimed."""
    monkeypatch.setenv("RIG_STATE_HOME", str(tmp_path / "rig_state"))
    manifest_path = tmp_path / "rig.json"
    manifest_path.write_text(
        json.dumps(
            {
                "project": "stuck",
                "services": {"api": {"type": "port", "cwd": ".", "command": ["echo"]}},
                "scopes": {"full": ["api"]},
            }
        )
    )
    instance = stack.instance_id("stuck", tmp_path)
    state_path = stack._state_path(tmp_path, instance=instance)
    stack.write_state(
        state_path,
        {
            "instance": instance,
            "project": "stuck",
            "root": str(tmp_path),
            "services": {"api": {"name": "api", "type": "port", "pid": 4242, "pgid": 4242}},
        },
    )

    monkeypatch.setattr(stack, "pid_alive", lambda pid: False)
    monkeypatch.setattr(stack, "pgid_alive", lambda pgid: True)
    monkeypatch.setattr(stack, "identity_matches", lambda rec: False)
    monkeypatch.setattr(stack, "_stop_record", lambda record, root, remove=False: "refused")

    ret = stack.cmd_up(root=tmp_path, manifest_path=manifest_path, scope="full")

    assert ret == stack.EXIT_OP_FAILED
    assert "api" in stack.read_state(state_path)["services"]


def test_cmd_ps_reports_partial_and_orphaned_instances(monkeypatch, tmp_path, capsys):
    """R5-3: `ps` must distinguish partial and orphaned instances from running ones."""
    monkeypatch.setenv("RIG_STATE_HOME", str(tmp_path / "rig_state"))
    _write_instance(
        "partial-00000000",
        {
            "instance": "partial-00000000",
            "project": "partial",
            "root": str(tmp_path),
            "services": {
                "up": {"name": "up", "type": "port", "pid": 11, "pgid": 11},
                "down": {"name": "down", "type": "port", "pid": 22, "pgid": 22},
            },
        },
    )
    _write_instance(
        "orphan-00000000",
        {
            "instance": "orphan-00000000",
            "project": "orphan",
            "root": str(tmp_path / "deleted-checkout"),
            "services": {"up": {"name": "up", "type": "port", "pid": 33, "pgid": 33}},
        },
    )
    _write_instance(
        "idle-00000000",
        {
            "instance": "idle-00000000",
            "project": "idle",
            "root": str(tmp_path),
            "services": {"down": {"name": "down", "type": "port", "pid": 22, "pgid": 22}},
        },
    )

    monkeypatch.setattr(stack, "pid_alive", lambda pid: pid != 22)
    monkeypatch.setattr(stack, "identity_matches", lambda rec: True)

    assert stack.cmd_ps(as_json=True) == stack.EXIT_OK
    instances = {
        item["instance"]: item
        for item in json.loads(capsys.readouterr().out)["data"]["instances"]
    }

    assert instances["partial-00000000"]["status"] == "partial"
    assert instances["partial-00000000"]["services_running"] == 1
    assert instances["orphan-00000000"]["status"] == "orphaned"
    assert instances["idle-00000000"]["status"] == "stopped"


# --------------------------------------------------------------------------------------
# Round 6 remediations
# --------------------------------------------------------------------------------------


def _compose_record(root, container="abc123", compose_file="compose.yml", name="db"):
    """Build a compose state record rooted at ``root``."""
    return {
        "name": name,
        "type": "compose",
        "instance": "inst-1",
        "compose_file": str(Path(root) / compose_file),
        "compose_service": name,
        "container": container,
    }


class _FakeDocker:
    """Answer plain ``docker`` calls from a script and record every invocation."""

    def __init__(
        self, inspect="running", ps_ids=("abc123",), failing=(), failure_stderr=None
    ):
        self.inspect = inspect
        self.ps_ids = list(ps_ids)
        self.failing = set(failing)
        self.failure_stderr = failure_stderr
        self.calls: list[list[str]] = []

    def __call__(self, args, context=None, timeout=60.0, **kwargs):
        args = list(args)
        self.calls.append(args)
        verb = args[0]
        if verb in self.failing:
            stderr = self.failure_stderr or f"{verb} refused"
            return subprocess.CompletedProcess(["docker"], 1, "", stderr)
        if verb == "inspect":
            return subprocess.CompletedProcess(["docker"], 0, f"{self.inspect}\n", "")
        if verb == "ps":
            return subprocess.CompletedProcess(
                ["docker"], 0, "".join(f"{i}\n" for i in self.ps_ids), ""
            )
        return subprocess.CompletedProcess(["docker"], 0, "", "")

    def verbs(self):
        return [c[0] for c in self.calls]


def _forbid_compose(monkeypatch):
    """Fail the test if Compose is invoked without its file on disk."""

    def _never(*args, **kwargs):
        raise AssertionError("run_compose must not run without a compose file")

    monkeypatch.setattr(stack, "run_compose", _never)


# R6-1: teardown removes the container before the record is dropped.


def test_cmd_down_removes_the_compose_container(monkeypatch, tmp_path):
    """R6-1: `down` must reclaim the container, not leave it exited and unowned."""
    monkeypatch.setenv("RIG_STATE_HOME", str(tmp_path / "rig_state"))
    (tmp_path / "compose.yml").write_text("services: {}\n")
    manifest_path = tmp_path / "rig.json"
    manifest_path.write_text(
        json.dumps(
            {
                "project": "shop",
                "services": {
                    "db": {
                        "type": "compose",
                        "compose_file": "compose.yml",
                        "compose_service": "db",
                    }
                },
                "scopes": {"full": ["db"]},
            }
        )
    )
    instance = stack.instance_id("shop", tmp_path)
    state_path = stack._state_path(tmp_path, instance=instance)
    stack.write_state(
        state_path,
        {
            "instance": instance,
            "project": "shop",
            "root": str(tmp_path),
            "services": {"db": _compose_record(tmp_path)},
        },
    )

    compose_calls: list[list[str]] = []

    def fake_run_compose(instance, root, cfile, args, context=None, timeout=180.0, env=None, **kwargs):
        compose_calls.append(list(args))
        return subprocess.CompletedProcess(["docker"], 0, "abc123\n", "")

    monkeypatch.setattr(stack, "run_compose", fake_run_compose)
    monkeypatch.setattr(stack, "run_docker", _FakeDocker())

    assert stack.cmd_down(root=tmp_path, manifest_path=manifest_path) == stack.EXIT_OK
    assert ["rm", "-f", "db"] in compose_calls
    assert stack.read_state(state_path)["services"] == {}


def test_rollback_removes_the_compose_container(monkeypatch, tmp_path):
    """R6-1: rollback drops the record, so it must remove the container too."""
    monkeypatch.setenv("RIG_STATE_HOME", str(tmp_path / "rig_state"))
    (tmp_path / "compose.yml").write_text("services: {}\n")
    manifest_path = tmp_path / "rig.json"
    manifest_path.write_text(
        json.dumps(
            {
                "project": "shop",
                "services": {
                    "db": {
                        "type": "compose",
                        "compose_file": "compose.yml",
                        "compose_service": "db",
                    }
                },
                "scopes": {"full": ["db"]},
            }
        )
    )
    manifest = stack.load_manifest(manifest_path)
    state_path = tmp_path / "state.json"
    state = {"services": {"db": _compose_record(tmp_path)}}

    compose_calls: list[list[str]] = []

    def fake_run_compose(instance, root, cfile, args, context=None, timeout=180.0, env=None, **kwargs):
        compose_calls.append(list(args))
        return subprocess.CompletedProcess(["docker"], 0, "abc123\n", "")

    monkeypatch.setattr(stack, "run_compose", fake_run_compose)
    monkeypatch.setattr(stack, "run_docker", _FakeDocker())

    stack._rollback(state, state_path, ["db"], tmp_path, manifest)

    assert ["rm", "-f", "db"] in compose_calls
    assert state["services"] == {}


def test_stop_record_keeps_the_record_when_removal_fails(monkeypatch, tmp_path):
    """R6-1: a container this rig cannot remove keeps its ownership record."""
    (tmp_path / "compose.yml").write_text("services: {}\n")

    def fake_run_compose(instance, root, cfile, args, context=None, timeout=180.0, env=None, **kwargs):
        if args[0] == "rm":
            return subprocess.CompletedProcess(["docker"], 1, "", "rm refused")
        return subprocess.CompletedProcess(["docker"], 0, "abc123\n", "")

    monkeypatch.setattr(stack, "run_compose", fake_run_compose)
    # Docker is the fallback for a refused Compose removal, so it must refuse
    # too: only then is the container genuinely unreclaimed.
    monkeypatch.setattr(stack, "run_docker", _FakeDocker(failing=("rm",)))

    assert stack._stop_record(_compose_record(tmp_path), tmp_path) == "failed"


# R6-2: a deleted checkout falls back to plain Docker.


def test_compose_status_falls_back_to_docker_inspect_when_the_file_is_gone(
    monkeypatch, tmp_path
):
    """R6-2: with no compose file, the recorded container ID is inspected directly."""
    _forbid_compose(monkeypatch)
    record = _compose_record(tmp_path / "deleted")

    docker = _FakeDocker(inspect="running")
    monkeypatch.setattr(stack, "run_docker", docker)
    assert stack.compose_record_status(record, tmp_path / "deleted") == "alive"
    # The labels are queried first, because a scaled service holds more than the
    # one recorded container, and then every discovered container is inspected.
    assert docker.calls[0][0] == "ps"
    assert docker.calls[1][:2] == ["inspect", "--format"]
    assert "abc123" in docker.calls[1]

    monkeypatch.setattr(stack, "run_docker", _FakeDocker(inspect="exited"))
    assert stack.compose_record_status(record, tmp_path / "deleted") == "stopped"

    monkeypatch.setattr(stack, "run_docker", _FakeDocker(failing=("inspect",)))
    assert stack.compose_record_status(record, tmp_path / "deleted") == "error"


def test_compose_status_falls_back_to_compose_labels_when_the_container_is_unknown(
    monkeypatch, tmp_path
):
    """R6-2: with no compose file and no container ID, Docker is queried by label."""
    _forbid_compose(monkeypatch)
    record = _compose_record(tmp_path / "deleted", container="")

    docker = _FakeDocker(inspect="running", ps_ids=("cafe01",))
    monkeypatch.setattr(stack, "run_docker", docker)
    assert stack.compose_record_status(record, tmp_path / "deleted") == "alive"
    assert docker.calls[0][0] == "ps"
    assert "label=com.docker.compose.project=inst-1" in docker.calls[0]
    assert "label=com.docker.compose.service=db" in docker.calls[0]
    assert "cafe01" in docker.calls[1]

    monkeypatch.setattr(stack, "run_docker", _FakeDocker(ps_ids=()))
    assert stack.compose_record_status(record, tmp_path / "deleted") == "absent"


def test_stop_record_falls_back_to_plain_docker_when_the_file_is_gone(monkeypatch, tmp_path):
    """R6-2: a deleted checkout must not block stopping and removing the container."""
    _forbid_compose(monkeypatch)
    docker = _FakeDocker(inspect="running")
    monkeypatch.setattr(stack, "run_docker", docker)

    outcome = stack._stop_record(_compose_record(tmp_path / "deleted"), tmp_path / "deleted")

    assert outcome == "terminated"
    assert ["stop", "abc123"] in docker.calls
    assert ["rm", "-f", "abc123"] in docker.calls


def test_stop_record_fallback_finds_the_container_by_label(monkeypatch, tmp_path):
    """R6-2: an undiscovered container is still reclaimed through compose labels."""
    _forbid_compose(monkeypatch)
    docker = _FakeDocker(inspect="running", ps_ids=("cafe01",))
    monkeypatch.setattr(stack, "run_docker", docker)

    record = _compose_record(tmp_path / "deleted", container="")
    assert stack._stop_record(record, tmp_path / "deleted") == "terminated"
    assert ["stop", "cafe01"] in docker.calls
    assert ["rm", "-f", "cafe01"] in docker.calls


def test_stop_record_fallback_reports_a_refused_removal(monkeypatch, tmp_path):
    """R6-2: a failed plain-Docker removal is reported, never silently accepted."""
    _forbid_compose(monkeypatch)
    monkeypatch.setattr(stack, "run_docker", _FakeDocker(failing=("rm",)))

    record = _compose_record(tmp_path / "deleted")
    assert stack._stop_record(record, tmp_path / "deleted") == "failed"


# R7-1: a Docker outage never discards container ownership.


def test_docker_status_reports_error_when_the_daemon_is_unreachable(monkeypatch, tmp_path):
    """R7-1: an unreachable daemon must not be read as a missing container."""
    _forbid_compose(monkeypatch)
    monkeypatch.setattr(
        stack,
        "run_docker",
        _FakeDocker(
            failing=("inspect",),
            failure_stderr="Cannot connect to the Docker daemon at unix:///var/run/docker.sock.",
        ),
    )

    record = _compose_record(tmp_path / "deleted")
    assert stack.docker_record_status(record) == "error"


def test_docker_status_reports_absent_only_when_docker_confirms_no_such_object(
    monkeypatch, tmp_path
):
    """R7-1: only Docker's own 'no such object' answer proves the container is gone."""
    _forbid_compose(monkeypatch)
    monkeypatch.setattr(
        stack,
        "run_docker",
        _FakeDocker(failing=("inspect",), failure_stderr="Error: No such object: abc123"),
    )

    record = _compose_record(tmp_path / "deleted")
    assert stack.docker_record_status(record) == "absent"


def test_stop_record_keeps_the_record_when_docker_is_unreachable(monkeypatch, tmp_path):
    """R7-1: teardown during an outage reports failure, so ownership is retained."""
    _forbid_compose(monkeypatch)
    monkeypatch.setattr(
        stack,
        "run_docker",
        _FakeDocker(
            failing=("inspect",),
            failure_stderr="Cannot connect to the Docker daemon at unix:///var/run/docker.sock.",
        ),
    )

    record = _compose_record(tmp_path / "deleted")
    assert stack._stop_record(record, tmp_path / "deleted") == "failed"


# R7-2: every replica of a scaled service is reclaimed, not just the recorded one.


def test_docker_status_reports_alive_when_any_replica_still_runs(monkeypatch, tmp_path):
    """R7-2: one running replica keeps the whole service alive."""
    _forbid_compose(monkeypatch)

    def fake_docker(args, context=None, timeout=60.0, **kwargs):
        args = list(args)
        if args[0] == "ps":
            return subprocess.CompletedProcess(["docker"], 0, "abc123\nbeef02\n", "")
        state = "running" if args[-1] == "beef02" else "exited"
        return subprocess.CompletedProcess(["docker"], 0, f"{state}\n", "")

    monkeypatch.setattr(stack, "run_docker", fake_docker)
    assert stack.docker_record_status(_compose_record(tmp_path / "deleted")) == "alive"


def test_stop_record_reclaims_every_replica_of_a_scaled_service(monkeypatch, tmp_path):
    """R7-2: a replica outside the state record must still be stopped and removed."""
    _forbid_compose(monkeypatch)
    docker = _FakeDocker(inspect="running", ps_ids=("abc123", "beef02"))
    monkeypatch.setattr(stack, "run_docker", docker)

    record = _compose_record(tmp_path / "deleted")
    assert stack._stop_record(record, tmp_path / "deleted") == "terminated"
    assert ["stop", "abc123"] in docker.calls
    assert ["rm", "-f", "abc123"] in docker.calls
    assert ["stop", "beef02"] in docker.calls
    assert ["rm", "-f", "beef02"] in docker.calls


def test_stop_record_reports_failure_when_one_replica_survives(monkeypatch, tmp_path):
    """R7-2: a replica this rig cannot remove fails the teardown, and the rest still go."""
    _forbid_compose(monkeypatch)
    calls: list[list[str]] = []

    def fake_docker(args, context=None, timeout=60.0, **kwargs):
        args = list(args)
        calls.append(args)
        if args[0] == "ps":
            return subprocess.CompletedProcess(["docker"], 0, "abc123\nbeef02\n", "")
        if args[0] == "inspect":
            return subprocess.CompletedProcess(["docker"], 0, "running\n", "")
        if args[0] == "rm" and args[-1] == "beef02":
            return subprocess.CompletedProcess(["docker"], 1, "", "rm refused")
        return subprocess.CompletedProcess(["docker"], 0, "", "")

    monkeypatch.setattr(stack, "run_docker", fake_docker)

    record = _compose_record(tmp_path / "deleted")
    assert stack._stop_record(record, tmp_path / "deleted") == "failed"
    assert ["rm", "-f", "abc123"] in calls


def test_cmd_down_keeps_the_record_when_a_replica_survives(monkeypatch, tmp_path, capsys):
    """R7-2: a service with a surviving replica keeps its ownership record."""
    monkeypatch.setenv("RIG_STATE_HOME", str(tmp_path / "rig_state"))
    gone = tmp_path / "deleted-checkout"
    _write_instance(
        "orphan-00000000",
        {
            "instance": "orphan-00000000",
            "project": "orphan",
            "root": str(gone),
            "services": {"db": _compose_record(gone)},
        },
    )
    _forbid_compose(monkeypatch)

    def fake_docker(args, context=None, timeout=60.0, **kwargs):
        args = list(args)
        if args[0] == "ps":
            return subprocess.CompletedProcess(["docker"], 0, "abc123\nbeef02\n", "")
        if args[0] == "inspect":
            return subprocess.CompletedProcess(["docker"], 0, "running\n", "")
        if args[0] == "rm" and args[-1] == "beef02":
            return subprocess.CompletedProcess(["docker"], 1, "", "rm refused")
        return subprocess.CompletedProcess(["docker"], 0, "", "")

    monkeypatch.setattr(stack, "run_docker", fake_docker)

    stack.cmd_down(all_instances=True, as_json=True)
    capsys.readouterr()

    state_file = stack.get_instances_dir() / "orphan-00000000" / stack.STATE_FILE_NAME
    assert "db" in stack.read_state(state_file)["services"]



# R6-3: orphaned instances whose checkout was deleted are reclaimable.


def test_cmd_down_all_reclaims_an_instance_whose_checkout_was_deleted(
    monkeypatch, tmp_path, capsys
):
    """R6-3: `down --all` must stop compose services of a deleted checkout."""
    monkeypatch.setenv("RIG_STATE_HOME", str(tmp_path / "rig_state"))
    gone = tmp_path / "deleted-checkout"
    _write_instance(
        "orphan-00000000",
        {
            "instance": "orphan-00000000",
            "project": "orphan",
            "root": str(gone),
            "services": {"db": _compose_record(gone)},
        },
    )
    _forbid_compose(monkeypatch)
    docker = _FakeDocker(inspect="running")
    monkeypatch.setattr(stack, "run_docker", docker)

    assert stack.cmd_down(all_instances=True, as_json=True) == stack.EXIT_OK
    envelope = json.loads(capsys.readouterr().out)

    assert envelope["data"]["instances"][0]["stopped"] == ["db"]
    assert ["rm", "-f", "abc123"] in docker.calls
    state_file = stack.get_instances_dir() / "orphan-00000000" / stack.STATE_FILE_NAME
    assert stack.read_state(state_file)["services"] == {}


def test_cmd_prune_reclaims_an_instance_whose_checkout_was_deleted(
    monkeypatch, tmp_path, capsys
):
    """R6-3: `prune --force` must reclaim a live container of a deleted checkout."""
    monkeypatch.setenv("RIG_STATE_HOME", str(tmp_path / "rig_state"))
    gone = tmp_path / "deleted-checkout"
    _write_instance(
        "orphan-00000000",
        {
            "instance": "orphan-00000000",
            "project": "orphan",
            "root": str(gone),
            "services": {"db": _compose_record(gone)},
        },
    )
    _forbid_compose(monkeypatch)
    docker = _FakeDocker(inspect="running")
    monkeypatch.setattr(stack, "run_docker", docker)

    assert stack.cmd_prune(force=True, as_json=True) == stack.EXIT_OK
    envelope = json.loads(capsys.readouterr().out)

    assert envelope["data"]["pruned"] == ["orphan-00000000"]
    assert envelope["data"]["failed"] == []
    assert ["rm", "-f", "abc123"] in docker.calls


# R8-1: a failed label query never proves a service holds no container.


def _unanswered_labels_docker(calls: list[list[str]]):
    """Refuse the label query, and answer every inspect with 'no such object'.

    This is the outage shape that matters: Docker cannot enumerate the service's
    containers, yet the one recorded container is genuinely gone. The service
    still holds every replica the failed query never listed.
    """

    def fake_docker(args, context=None, timeout=60.0, **kwargs):
        args = list(args)
        calls.append(args)
        if args[0] == "ps":
            return subprocess.CompletedProcess(
                ["docker"], 1, "", "Cannot connect to the Docker daemon at unix:///var/run/docker.sock."
            )
        if args[0] == "inspect":
            return subprocess.CompletedProcess(
                ["docker"], 1, "", "Error: No such object: abc123"
            )
        return subprocess.CompletedProcess(["docker"], 0, "", "")

    return fake_docker


def test_docker_status_reports_error_when_the_label_query_fails(monkeypatch, tmp_path):
    """R8-1: an unanswered label query must not report `absent` for a gone container."""
    _forbid_compose(monkeypatch)
    calls: list[list[str]] = []
    monkeypatch.setattr(stack, "run_docker", _unanswered_labels_docker(calls))

    record = _compose_record(tmp_path / "deleted")
    assert stack.docker_record_status(record) == "error"
    assert ["ps"] == [c[0] for c in calls if c[0] == "ps"]


def test_docker_stop_reports_failure_when_the_label_query_fails(monkeypatch, tmp_path):
    """R8-1: teardown cannot claim success while un-queried replicas may survive."""
    _forbid_compose(monkeypatch)
    calls: list[list[str]] = []
    monkeypatch.setattr(stack, "run_docker", _unanswered_labels_docker(calls))

    record = _compose_record(tmp_path / "deleted")
    assert stack.docker_record_stop(record, remove=True) == "failed"
    # The recorded container is still attempted: best effort, but never reported
    # as a full reclamation.
    assert ["stop", "abc123"] in calls
    assert ["rm", "-f", "abc123"] in calls


# R8-2: a surviving replica keeps its record even when the recorded container is gone.


def _replica_docker(alive_state: str, survivor: str = "beef02"):
    """Answer 'no such object' for the recorded container and ``alive_state`` for a replica."""

    def fake_docker(args, context=None, timeout=60.0, **kwargs):
        args = list(args)
        if args[0] == "inspect" and args[-1] == "abc123":
            return subprocess.CompletedProcess(
                ["docker"], 1, "", "Error: No such object: abc123"
            )
        if args[0] == "inspect" and args[-1] == survivor:
            return subprocess.CompletedProcess(["docker"], 0, f"{alive_state}\n", "")
        return subprocess.CompletedProcess(["docker"], 0, "", "")

    return fake_docker


def test_compose_status_reports_alive_when_only_an_unrecorded_replica_survives(
    monkeypatch, tmp_path
):
    """R8-2: a deleted recorded container must not hide a running replica."""
    _write_compose_file(tmp_path)
    monkeypatch.setattr(
        stack,
        "run_compose",
        lambda *a, **k: subprocess.CompletedProcess(["docker"], 0, "beef02\n", ""),
    )
    monkeypatch.setattr(stack, "run_docker", _replica_docker("running"))

    assert stack.compose_record_status(_compose_record(tmp_path), tmp_path) == "alive"


def test_compose_status_reports_stopped_when_only_an_unrecorded_replica_remains(
    monkeypatch, tmp_path
):
    """R8-2: an exited replica is still this record's container, not an absence."""
    _write_compose_file(tmp_path)
    monkeypatch.setattr(
        stack,
        "run_compose",
        lambda *a, **k: subprocess.CompletedProcess(["docker"], 0, "beef02\n", ""),
    )
    monkeypatch.setattr(stack, "run_docker", _replica_docker("exited"))

    assert stack.compose_record_status(_compose_record(tmp_path), tmp_path) == "stopped"


def test_stop_record_reclaims_a_replica_when_the_recorded_container_is_gone(
    monkeypatch, tmp_path
):
    """R8-2: teardown reclaims the surviving replica instead of reporting a stale record."""
    _write_compose_file(tmp_path)
    compose_calls: list[list[str]] = []

    def fake_run_compose(instance, root, cfile, args, context=None, timeout=180.0, env=None, **kwargs):
        compose_calls.append(list(args))
        return subprocess.CompletedProcess(["docker"], 0, "beef02\n", "")

    monkeypatch.setattr(stack, "run_compose", fake_run_compose)
    monkeypatch.setattr(stack, "run_docker", _replica_docker("running"))

    outcome = stack._stop_record(_compose_record(tmp_path), tmp_path)

    assert outcome == "terminated"
    assert ["stop", "db"] in compose_calls
    assert ["rm", "-f", "db"] in compose_calls


# --------------------------------------------------------------------------------------
# R9-1: an interrupted Compose startup leaves no unrecorded container
# --------------------------------------------------------------------------------------


def _interrupted_compose(step: str, cleanup_ok: bool, error=KeyboardInterrupt):
    """Answer Compose normally until ``step``, which raises ``error``.

    ``cleanup_ok`` decides whether the reclaim that follows succeeds, which is
    what separates "the container is gone" from "the container must be
    recorded".
    """

    def fake_run_compose(
        instance, root, cfile, args, context=None, timeout=180.0, env=None, **kwargs
    ):
        verb = args[0]
        if verb == step:
            raise error()
        if verb == "up":
            return subprocess.CompletedProcess(["docker"], 0, "", "")
        if verb == "ps":
            return subprocess.CompletedProcess(["docker"], 0, "abc123\n", "")
        if verb == "port":
            return subprocess.CompletedProcess(["docker"], 0, "0.0.0.0:54321\n", "")
        return subprocess.CompletedProcess(
            ["docker"], 0 if cleanup_ok else 1, "", "" if cleanup_ok else "cleanup refused"
        )

    return fake_run_compose


def _failed_compose_start(service, root, instance="inst-1"):
    """Return whatever exception `_start_compose_service` raises, of any class.

    An interrupt that escapes the call would abort the entire pytest session, so
    it is caught here and reported as this test's failure instead.
    """
    try:
        stack._start_compose_service(service, root, instance)
    except BaseException as exc:  # noqa: BLE001 - catching every class is the point
        return exc
    raise AssertionError("expected _start_compose_service to fail")


def test_compose_container_discovery_interrupt_records_the_container(monkeypatch, tmp_path):
    """R9-1: an interrupt whose reclaim fails must publish the partial record."""
    monkeypatch.setattr(stack, "run_compose", _interrupted_compose("ps", cleanup_ok=False))

    err = _failed_compose_start(_compose_service(), tmp_path)

    assert isinstance(err, stack.RigError), f"the interrupt escaped unrecorded: {err!r}"
    partial = err.details.get("partial_record")
    assert partial is not None
    assert partial["name"] == "db"
    assert partial["type"] == "compose"
    assert partial["instance"] == "inst-1"
    assert isinstance(err.__cause__, KeyboardInterrupt)
    assert "prune" in (err.hint or "")


def test_compose_container_discovery_interrupt_reraises_after_a_clean_reclaim(
    monkeypatch, tmp_path
):
    """R9-1: a proven reclaim lets the interrupt travel on and records nothing."""
    calls: list[list[str]] = []

    fake = _interrupted_compose("ps", cleanup_ok=True)

    def recording(instance, root, cfile, args, context=None, timeout=180.0, env=None, **kwargs):
        calls.append(list(args))
        return fake(instance, root, cfile, args, context, timeout, env, **kwargs)

    monkeypatch.setattr(stack, "run_compose", recording)

    with pytest.raises(KeyboardInterrupt):
        stack._start_compose_service(_compose_service(), tmp_path, "inst-1")

    # The container `up` created was stopped and removed, not abandoned.
    assert ["stop", "db"] in calls
    assert ["rm", "-f", "db"] in calls


def test_compose_port_discovery_interrupt_records_the_discovered_container(
    monkeypatch, tmp_path
):
    """R9-1: an interrupt during port discovery keeps the container ID it found."""
    monkeypatch.setattr(stack, "run_compose", _interrupted_compose("port", cleanup_ok=False))

    err = _failed_compose_start(_compose_service(compose_port=5432), tmp_path)

    assert isinstance(err, stack.RigError), f"the interrupt escaped unrecorded: {err!r}"
    partial = err.details.get("partial_record")
    assert partial is not None
    assert partial["container"] == "abc123"
    assert partial["port"] is None
    assert isinstance(err.__cause__, KeyboardInterrupt)


def test_compose_discovery_system_exit_records_the_container(monkeypatch, tmp_path):
    """R9-1: every BaseException, not only an interrupt, must account for the container."""
    monkeypatch.setattr(
        stack, "run_compose", _interrupted_compose("ps", cleanup_ok=False, error=SystemExit)
    )

    err = _failed_compose_start(_compose_service(), tmp_path)

    assert isinstance(err, stack.RigError), f"the exit escaped unrecorded: {err!r}"
    assert err.details.get("partial_record") is not None
    assert isinstance(err.__cause__, SystemExit)


def test_compose_cleanup_interrupted_twice_still_records_the_container(monkeypatch, tmp_path):
    """R9-1: an interrupt during the reclaim itself must not lose the container."""

    def fake_run_compose(
        instance, root, cfile, args, context=None, timeout=180.0, env=None, **kwargs
    ):
        if args[0] == "up":
            return subprocess.CompletedProcess(["docker"], 0, "", "")
        # Discovery and every cleanup attempt are cut short.
        raise KeyboardInterrupt()

    monkeypatch.setattr(stack, "run_compose", fake_run_compose)

    err = _failed_compose_start(_compose_service(), tmp_path)
    assert isinstance(err, stack.RigError), f"the interrupt escaped unrecorded: {err!r}"
    assert err.details.get("partial_record") is not None


def _compose_manifest(tmp_path, project="ints", port=None):
    """Write a one-compose-service manifest and its compose file."""
    _write_compose_file(tmp_path)
    spec = {"type": "compose", "compose_file": "compose.yml", "compose_service": "db"}
    if port:
        spec["compose_port"] = port
    manifest_path = tmp_path / "rig.json"
    manifest_path.write_text(
        json.dumps({"project": project, "services": {"db": spec}, "scopes": {"full": ["db"]}})
    )
    return manifest_path


def test_cmd_up_persists_the_partial_container_after_an_interrupt(monkeypatch, tmp_path):
    """R9-1: `up` interrupted mid-discovery leaves the container recorded for `down`."""
    monkeypatch.setenv("RIG_STATE_HOME", str(tmp_path / "rig_state"))
    manifest_path = _compose_manifest(tmp_path)
    monkeypatch.setattr(stack, "run_compose", _interrupted_compose("ps", cleanup_ok=False))

    try:
        exit_code = stack.cmd_up(tmp_path, manifest_path, scope="full")
    except BaseException as exc:  # noqa: BLE001 - an escaping interrupt is the defect
        pytest.fail(f"the interrupt escaped `up`: {exc!r}")

    assert exit_code == stack.EXIT_OP_FAILED
    instance = stack.instance_id("ints", tmp_path)
    saved = stack.read_state(stack._state_path(tmp_path, instance=instance))["services"]
    assert "db" in saved, "the container up created must stay recorded"
    assert saved["db"]["type"] == "compose"
    assert saved["db"]["compose_service"] == "db"


def test_cmd_up_writes_state_before_an_interrupt_unwinds(monkeypatch, tmp_path):
    """R9-1: state changes made before an interrupt reach disk, then it re-raises."""
    monkeypatch.setenv("RIG_STATE_HOME", str(tmp_path / "rig_state"))
    manifest_path = _compose_manifest(tmp_path)
    partial = {
        "name": "db",
        "type": "compose",
        "instance": "inst-1",
        "compose_file": str(tmp_path / "compose.yml"),
        "compose_service": "db",
        "container": "abc123",
    }

    def interrupted_start(service, root, runtime, instance, state, state_path):
        # The record exists in memory only: nothing has been written yet.
        state["services"][service.name] = dict(partial)
        raise KeyboardInterrupt()

    monkeypatch.setattr(stack, "_start_with_retry", interrupted_start)

    with pytest.raises(KeyboardInterrupt):
        stack.cmd_up(tmp_path, manifest_path, scope="full")

    instance = stack.instance_id("ints", tmp_path)
    saved = stack.read_state(stack._state_path(tmp_path, instance=instance))["services"]
    assert saved.get("db", {}).get("container") == "abc123"


# --------------------------------------------------------------------------------------
# R9-2: the Docker endpoint is pinned at startup, so teardown reaches it
# --------------------------------------------------------------------------------------


def _healthy_compose(calls=None):
    def fake_run_compose(
        instance, root, cfile, args, context=None, timeout=180.0, env=None, **kwargs
    ):
        if calls is not None:
            calls.append(
                {"args": list(args), "context": context, "env": env, "kwargs": dict(kwargs)}
            )
        if args[0] == "ps":
            return subprocess.CompletedProcess(["docker"], 0, "abc123\n", "")
        if args[0] == "port":
            return subprocess.CompletedProcess(["docker"], 0, "0.0.0.0:54321\n", "")
        return subprocess.CompletedProcess(["docker"], 0, "", "")

    return fake_run_compose


def test_compose_start_records_the_docker_host_in_force(monkeypatch, tmp_path):
    """R9-2: the daemon that created the container is recorded with it."""
    monkeypatch.setenv("DOCKER_HOST", "tcp://remote:2375")
    calls: list[dict] = []
    monkeypatch.setattr(stack, "run_compose", _healthy_compose(calls))

    record = stack._start_compose_service(_compose_service(), tmp_path, "inst-1")

    assert record["docker_host"] == "tcp://remote:2375"
    # Startup itself is pinned to the same endpoint it records.
    assert all(call["kwargs"]["docker_host"] == "tcp://remote:2375" for call in calls)


def test_compose_start_records_the_absence_of_a_docker_host(monkeypatch, tmp_path):
    """R9-2: no ambient endpoint is recorded as such, pinning the local daemon."""
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setattr(stack, "run_compose", _healthy_compose())

    record = stack._start_compose_service(_compose_service(), tmp_path, "inst-1")

    assert "docker_host" in record
    assert record["docker_host"] is None


def _capture_docker_env(monkeypatch):
    """Record the environment every plain ``docker`` command is given."""
    seen: list[dict[str, str]] = []

    def fake_run(argv, **kwargs):
        seen.append(dict(kwargs.get("env") or {}))
        # A pinned context puts `--context <name>` before the verb, so the verb
        # is recognised by membership rather than by position.
        if "ps" in argv:
            return subprocess.CompletedProcess(argv, 0, "abc123\n", "")
        if "inspect" in argv:
            return subprocess.CompletedProcess(argv, 0, "running\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return seen


def test_docker_status_queries_the_daemon_recorded_at_startup(monkeypatch, tmp_path):
    """R9-2: a changed ambient endpoint must not redirect the status query."""
    monkeypatch.setenv("DOCKER_HOST", "tcp://somewhere-else:2375")
    seen = _capture_docker_env(monkeypatch)
    record = dict(_compose_record(tmp_path / "deleted"), docker_host="tcp://remote:2375")

    assert stack.docker_record_status(record) == "alive"
    assert seen, "docker must have been called"
    assert all(env.get("DOCKER_HOST") == "tcp://remote:2375" for env in seen)


def test_docker_teardown_reaches_the_daemon_recorded_at_startup(monkeypatch, tmp_path):
    """R9-2: teardown stops the container on the daemon that holds it."""
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    seen = _capture_docker_env(monkeypatch)
    record = dict(_compose_record(tmp_path / "deleted"), docker_host="tcp://remote:2375")

    assert stack.docker_record_stop(record, remove=True) == "terminated"
    assert all(env.get("DOCKER_HOST") == "tcp://remote:2375" for env in seen)


def test_a_record_pinned_to_the_local_daemon_ignores_an_ambient_docker_host(
    monkeypatch, tmp_path
):
    """R9-2: a record started without DOCKER_HOST keeps asking the local daemon."""
    monkeypatch.setenv("DOCKER_HOST", "tcp://appeared-later:2375")
    seen = _capture_docker_env(monkeypatch)
    record = dict(_compose_record(tmp_path / "deleted"), docker_host=None)

    assert stack.docker_record_status(record) == "alive"
    assert seen and all("DOCKER_HOST" not in env for env in seen)


def test_a_record_without_a_pinned_endpoint_inherits_the_ambient_daemon(monkeypatch, tmp_path):
    """R9-2: a record written before pinning existed keeps its old behaviour."""
    monkeypatch.setenv("DOCKER_HOST", "tcp://ambient:2375")
    seen = _capture_docker_env(monkeypatch)
    record = _compose_record(tmp_path / "deleted")
    assert "docker_host" not in record

    assert stack.docker_record_status(record) == "alive"
    assert seen and all(env.get("DOCKER_HOST") == "tcp://ambient:2375" for env in seen)


def test_compose_status_and_teardown_pin_the_recorded_endpoint(monkeypatch, tmp_path):
    """R9-2: Compose queries carry the recorded endpoint too, not the ambient one."""
    _write_compose_file(tmp_path)
    calls: list[dict] = []

    def fake_run_compose(
        instance, root, cfile, args, context=None, timeout=180.0, env=None, **kwargs
    ):
        calls.append({"args": list(args), "kwargs": dict(kwargs)})
        return subprocess.CompletedProcess(["docker"], 0, "abc123\n", "")

    monkeypatch.setattr(stack, "run_compose", fake_run_compose)
    monkeypatch.setattr(
        stack,
        "run_docker",
        lambda *a, **k: subprocess.CompletedProcess(["docker"], 0, "running\n", ""),
    )
    record = dict(
        _compose_record(tmp_path), docker_host="tcp://remote:2375", docker_context="colima"
    )

    assert stack.compose_record_status(record, tmp_path) == "alive"
    assert stack._stop_record(record, tmp_path) == "terminated"
    assert calls
    assert all(call["kwargs"]["docker_host"] == "tcp://remote:2375" for call in calls)


def test_stop_record_pins_the_endpoint_of_a_deleted_checkout(monkeypatch, tmp_path):
    """R9-2: a deleted checkout falls back to plain Docker on the recorded daemon."""
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    _forbid_compose(monkeypatch)
    seen = _capture_docker_env(monkeypatch)
    record = dict(_compose_record(tmp_path / "deleted"), docker_host="tcp://remote:2375")

    assert stack._stop_record(record, tmp_path / "deleted") == "terminated"
    assert seen and all(env.get("DOCKER_HOST") == "tcp://remote:2375" for env in seen)


# --------------------------------------------------------------------------------------
# R9-3: a Compose service receives the environment its manifest declares
# --------------------------------------------------------------------------------------


def _start_compose_through_start_service(monkeypatch, tmp_path, service, calls):
    monkeypatch.setattr(stack, "run_compose", _healthy_compose(calls))
    values = {
        "root": str(tmp_path),
        "instance": "inst-1",
        "data_dir": str(tmp_path / "data"),
        "python": sys.executable,
    }
    return stack._start_service(service, tmp_path, tmp_path / ".local-run", "inst-1", values)


def test_compose_service_receives_its_declared_environment(monkeypatch, tmp_path):
    """R9-3: declared `env` must reach Compose, not be silently dropped."""
    monkeypatch.setenv("UNRELATED_AMBIENT", "leaked")
    calls: list[dict] = []
    service = _compose_service(
        env={"POSTGRES_PASSWORD": "s3cret", "PGDATA": "{root}/data/pg"},
    )

    record = _start_compose_through_start_service(monkeypatch, tmp_path, service, calls)

    assert record["container"] == "abc123"
    assert calls, "compose must have been invoked"
    env = calls[0]["env"]
    assert env is not None, "compose received no environment at all"
    assert env["POSTGRES_PASSWORD"] == "s3cret"
    assert env["PGDATA"] == f"{tmp_path}/data/pg"
    # The environment stays an allowlist: ambient variables do not leak through.
    assert "UNRELATED_AMBIENT" not in env
    assert "PATH" in env


def test_compose_service_receives_its_env_file(monkeypatch, tmp_path):
    """R9-3: `env_files` must reach Compose so its interpolation resolves."""
    (tmp_path / ".env.db").write_text("POSTGRES_USER=shop\n# comment\nPOSTGRES_DB=shop_dev\n")
    calls: list[dict] = []
    service = _compose_service(env_files=[".env.db"], env={"TZ_OVERRIDE": "UTC"})

    _start_compose_through_start_service(monkeypatch, tmp_path, service, calls)

    env = calls[0]["env"]
    assert env["POSTGRES_USER"] == "shop"
    assert env["POSTGRES_DB"] == "shop_dev"
    assert env["TZ_OVERRIDE"] == "UTC"


def test_compose_service_inherits_only_what_it_declares(monkeypatch, tmp_path):
    """R9-3: `inherit` is the only route for an ambient variable into Compose."""
    monkeypatch.setenv("REGISTRY_MIRROR", "mirror.internal")
    monkeypatch.setenv("SECRET_AMBIENT", "nope")
    calls: list[dict] = []
    service = _compose_service(inherit=["REGISTRY_MIRROR"])

    _start_compose_through_start_service(monkeypatch, tmp_path, service, calls)

    env = calls[0]["env"]
    assert env["REGISTRY_MIRROR"] == "mirror.internal"
    assert "SECRET_AMBIENT" not in env


def test_compose_environment_keeps_the_docker_client_settings(monkeypatch, tmp_path):
    """R9-3: the declared environment must not cut Compose off from its daemon."""
    monkeypatch.setenv("DOCKER_CONFIG", "/home/dev/.docker")
    monkeypatch.setenv("DOCKER_TLS_VERIFY", "1")
    monkeypatch.setenv("DOCKER_CERT_PATH", "/home/dev/.docker/certs")
    monkeypatch.setenv("DOCKER_HOST", "tcp://remote:2375")
    calls: list[dict] = []

    _start_compose_through_start_service(
        monkeypatch, tmp_path, _compose_service(env={"A": "b"}), calls
    )

    env = calls[0]["env"]
    assert env["DOCKER_CONFIG"] == "/home/dev/.docker"
    assert env["DOCKER_TLS_VERIFY"] == "1"
    assert env["DOCKER_CERT_PATH"] == "/home/dev/.docker/certs"
    # The endpoint travels as the pinned value, which `run_compose` applies.
    assert calls[0]["kwargs"]["docker_host"] == "tcp://remote:2375"


def test_run_compose_applies_the_pinned_endpoint_to_a_declared_environment(monkeypatch, tmp_path):
    """R9-3: a declared environment and a pinned endpoint reach docker together."""
    captured: dict[str, str] = {}

    def fake_run(argv, **kwargs):
        captured.update(kwargs.get("env") or {})
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    stack.run_compose(
        "inst-1",
        tmp_path,
        tmp_path / "compose.yml",
        ["ps"],
        None,
        env={"PATH": "/usr/bin", "POSTGRES_PASSWORD": "s3cret"},
        docker_host="tcp://remote:2375",
    )

    assert captured["POSTGRES_PASSWORD"] == "s3cret"
    assert captured["DOCKER_HOST"] == "tcp://remote:2375"


# --------------------------------------------------------------------------------------
# R9-4: an already-deleted recorded container never fails a successful reclaim
# --------------------------------------------------------------------------------------


def _deleted_recorded_container_docker(calls: list[list[str]], survivor="beef02", refuse=()):
    """Report the recorded container gone while ``survivor`` still exists.

    This is the shape a deleted checkout leaves behind: the ID recorded at start
    was removed elsewhere, and the label query lists only the replica that
    survived.
    """

    def fake_docker(args, context=None, timeout=60.0, **kwargs):
        args = list(args)
        calls.append(args)
        verb = args[0]
        if verb == "ps":
            return subprocess.CompletedProcess(["docker"], 0, f"{survivor}\n", "")
        if args[-1] == "abc123":
            return subprocess.CompletedProcess(
                ["docker"], 1, "", "Error response from daemon: No such container: abc123"
            )
        if verb in refuse:
            return subprocess.CompletedProcess(["docker"], 1, "", "permission denied")
        if verb == "inspect":
            return subprocess.CompletedProcess(["docker"], 0, "running\n", "")
        return subprocess.CompletedProcess(["docker"], 0, "", "")

    return fake_docker


def test_docker_teardown_treats_an_already_deleted_container_as_reclaimed(
    monkeypatch, tmp_path
):
    """R9-4: a gone recorded container must not fail the replica's reclamation."""
    _forbid_compose(monkeypatch)
    calls: list[list[str]] = []
    monkeypatch.setattr(stack, "run_docker", _deleted_recorded_container_docker(calls))

    outcome = stack.docker_record_stop(_compose_record(tmp_path / "deleted"), remove=True)

    assert outcome == "terminated"
    # The surviving replica was genuinely stopped and removed.
    assert ["stop", "beef02"] in calls
    assert ["rm", "-f", "beef02"] in calls
    # Once Docker answered that the recorded container is gone, `rm` is pointless.
    assert ["rm", "-f", "abc123"] not in calls


def test_docker_teardown_still_fails_when_a_live_container_refuses(monkeypatch, tmp_path):
    """R9-4: idempotent removal must not mask a container that cannot be stopped."""
    _forbid_compose(monkeypatch)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        stack, "run_docker", _deleted_recorded_container_docker(calls, refuse=("stop",))
    )

    assert stack.docker_record_stop(_compose_record(tmp_path / "deleted"), remove=True) == "failed"


def test_docker_teardown_reports_stale_when_every_target_is_already_gone(
    monkeypatch, tmp_path
):
    """R9-4: nothing left to reclaim is a clean outcome, not a failure."""
    _forbid_compose(monkeypatch)
    calls: list[list[str]] = []

    def fake_docker(args, context=None, timeout=60.0, **kwargs):
        args = list(args)
        calls.append(args)
        if args[0] == "ps":
            return subprocess.CompletedProcess(["docker"], 0, "", "")
        return subprocess.CompletedProcess(
            ["docker"], 1, "", "Error response from daemon: No such container: abc123"
        )

    monkeypatch.setattr(stack, "run_docker", fake_docker)

    assert stack.docker_record_stop(_compose_record(tmp_path / "deleted"), remove=True) == "terminated"
    assert ["stop", "abc123"] in calls


def test_stop_record_reclaims_a_replica_when_the_checkout_and_container_are_gone(
    monkeypatch, tmp_path
):
    """R9-4: a deleted checkout plus a deleted recorded ID still reclaims the replica."""
    _forbid_compose(monkeypatch)
    calls: list[list[str]] = []
    monkeypatch.setattr(stack, "run_docker", _deleted_recorded_container_docker(calls))

    record = _compose_record(tmp_path / "deleted")
    assert stack._stop_record(record, tmp_path / "deleted") == "terminated"
    assert ["rm", "-f", "beef02"] in calls


def test_cmd_prune_reclaims_a_replica_when_the_recorded_container_is_gone(
    monkeypatch, tmp_path, capsys
):
    """R9-4: `prune` drops the instance instead of reporting a failure it cannot fix."""
    monkeypatch.setenv("RIG_STATE_HOME", str(tmp_path / "rig_state"))
    _forbid_compose(monkeypatch)
    deleted_root = tmp_path / "deleted"
    instance = "gone-inst"
    inst_dir = stack.ensure_instance_dir(instance)
    stack.write_state(
        inst_dir / "state.json",
        {
            "instance": instance,
            "project": "shop",
            "root": str(deleted_root),
            "services": {"db": _compose_record(deleted_root)},
        },
    )
    calls: list[list[str]] = []
    monkeypatch.setattr(stack, "run_docker", _deleted_recorded_container_docker(calls))

    assert stack.cmd_prune(force=True) == stack.EXIT_OK
    assert ["rm", "-f", "beef02"] in calls
    assert not (inst_dir / "state.json").exists()


# --------------------------------------------------------------------------------------
# R9-5: the status report tells a running service from a stopped one
# --------------------------------------------------------------------------------------


def _status_manifest(tmp_path, service):
    return stack.Manifest(
        project="shop",
        services={service.name: service},
        scopes={"full": [service.name]},
        path=tmp_path / "rig.json",
    )


def _print_compose_status(monkeypatch, tmp_path, container_state, service=None, record=None):
    _write_compose_file(tmp_path)
    monkeypatch.setattr(
        stack,
        "run_compose",
        lambda *a, **k: subprocess.CompletedProcess(["docker"], 0, "abc123\n", ""),
    )
    if container_state == "unreachable":
        monkeypatch.setattr(
            stack,
            "run_docker",
            lambda *a, **k: subprocess.CompletedProcess(
                ["docker"], 1, "", "Cannot connect to the Docker daemon"
            ),
        )
    else:
        monkeypatch.setattr(
            stack,
            "run_docker",
            lambda *a, **k: subprocess.CompletedProcess(["docker"], 0, f"{container_state}\n", ""),
        )
    service = service or _compose_service()
    state = {"generation": 3, "services": {"db": record or _compose_record(tmp_path)}}
    stack._print_status(_status_manifest(tmp_path, service), state, tmp_path)


def _status_line(capsys, name: str) -> str:
    """Return the reported line for one service."""
    return next(
        line for line in capsys.readouterr().out.splitlines() if name in line
    )


def test_status_reports_a_running_compose_container_as_running(monkeypatch, tmp_path, capsys):
    """R9-5: a live container is still reported as running."""
    _print_compose_status(monkeypatch, tmp_path, "running")
    line = _status_line(capsys, "db")
    assert "running" in line
    assert "container=abc123" in line


def test_status_reports_a_stopped_compose_container_as_stopped(monkeypatch, tmp_path, capsys):
    """R9-5: a retained record whose container exited must not read as running."""
    _print_compose_status(monkeypatch, tmp_path, "exited")
    line = _status_line(capsys, "db")
    assert "stopped" in line
    assert "running" not in line


def test_status_reports_an_unreachable_compose_service_as_error(monkeypatch, tmp_path, capsys):
    """R9-5: an unanswered question is reported as an error, never as running."""
    _print_compose_status(monkeypatch, tmp_path, "unreachable")
    line = _status_line(capsys, "db")
    assert "error" in line
    assert "running" not in line


def test_status_skips_the_health_probe_of_a_stopped_service(monkeypatch, tmp_path, capsys):
    """R9-5: a stopped service is not probed, so it cannot be labelled healthy."""

    def forbidden(*args, **kwargs):
        raise AssertionError("a stopped service must not be health-probed")

    monkeypatch.setattr(stack, "wait_for_http", forbidden)
    service = _compose_service(compose_port=5432, healthcheck_path="/health")
    record = dict(_compose_record(tmp_path), port=5432, url="http://127.0.0.1:5432")

    _print_compose_status(monkeypatch, tmp_path, "exited", service=service, record=record)

    out = capsys.readouterr().out
    assert "stopped" in out
    assert "healthy" not in out


def test_status_reports_a_dead_process_service_as_stopped(monkeypatch, tmp_path, capsys):
    """R9-5: a retained process record whose identity no longer matches is stopped."""
    service = stack.Service(name="api", type="port", command=["echo"])
    state = {
        "generation": 1,
        "services": {
            "api": {
                "name": "api",
                "type": "port",
                "pid": 999999,
                "port": 8000,
                "url": "http://127.0.0.1:8000",
            }
        },
    }

    stack._print_status(_status_manifest(tmp_path, service), state, tmp_path)

    line = _status_line(capsys, "api")
    assert "stopped" in line
    assert "running" not in line


def test_status_reports_a_live_process_service_as_running(monkeypatch, tmp_path, capsys):
    """R9-5: a verifiable process record is still reported as running."""
    monkeypatch.setattr(stack, "identity_matches", lambda record: True)
    service = stack.Service(name="api", type="port", command=["echo"])
    state = {
        "generation": 1,
        "services": {
            "api": {
                "name": "api",
                "type": "port",
                "pid": 4242,
                "port": 8000,
                "url": "http://127.0.0.1:8000",
            }
        },
    }

    stack._print_status(_status_manifest(tmp_path, service), state, tmp_path)

    line = _status_line(capsys, "api")
    assert "running" in line
    assert "pid=4242" in line


# --------------------------------------------------------------------------------------
# R10-1: an interrupted `up` leaves no unrecorded container
# --------------------------------------------------------------------------------------


def test_compose_up_interrupt_records_the_container(monkeypatch, tmp_path):
    """R10-1: `--wait` makes `up` interruptible, so its container must be recorded."""
    monkeypatch.setattr(stack, "run_compose", _interrupted_compose("up", cleanup_ok=False))

    err = _failed_compose_start(_compose_service(), tmp_path)

    assert isinstance(err, stack.RigError), f"the interrupt escaped unrecorded: {err!r}"
    partial = err.details.get("partial_record")
    assert partial is not None
    assert partial["name"] == "db"
    assert partial["type"] == "compose"
    assert partial["instance"] == "inst-1"
    # `up` never returned, so no container ID was ever discovered. The record
    # still names the project and service, which is what a later teardown needs.
    assert partial["container"] == ""
    assert partial["compose_service"] == "db"
    assert isinstance(err.__cause__, KeyboardInterrupt)
    assert "prune" in (err.hint or "")


def test_compose_up_interrupt_reraises_after_a_clean_reclaim(monkeypatch, tmp_path):
    """R10-1: a proven reclaim after an interrupted `up` records nothing."""
    calls: list[list[str]] = []
    fake = _interrupted_compose("up", cleanup_ok=True)

    def recording(instance, root, cfile, args, context=None, timeout=180.0, env=None, **kwargs):
        calls.append(list(args))
        return fake(instance, root, cfile, args, context, timeout, env, **kwargs)

    monkeypatch.setattr(stack, "run_compose", recording)

    with pytest.raises(KeyboardInterrupt):
        stack._start_compose_service(_compose_service(), tmp_path, "inst-1")

    # Whatever `up` had already created was stopped and removed, not abandoned.
    assert ["stop", "db"] in calls
    assert ["rm", "-f", "db"] in calls


def test_compose_up_system_exit_records_the_container(monkeypatch, tmp_path):
    """R10-1: every BaseException out of `up`, not only an interrupt, is accounted for."""
    monkeypatch.setattr(
        stack, "run_compose", _interrupted_compose("up", cleanup_ok=False, error=SystemExit)
    )

    err = _failed_compose_start(_compose_service(), tmp_path)

    assert isinstance(err, stack.RigError), f"the exit escaped unrecorded: {err!r}"
    assert err.details.get("partial_record") is not None
    assert isinstance(err.__cause__, SystemExit)


def test_compose_up_interrupt_is_recorded_by_cmd_up(monkeypatch, tmp_path):
    """R10-1: the partial record from an interrupted `up` reaches the state file."""
    monkeypatch.setenv("RIG_STATE_HOME", str(tmp_path / "rig_state"))
    manifest_path = _compose_manifest(tmp_path)
    monkeypatch.setattr(stack, "run_compose", _interrupted_compose("up", cleanup_ok=False))

    ret = stack.cmd_up(tmp_path, manifest_path)

    assert ret == stack.EXIT_OP_FAILED
    state = stack.read_state(stack._state_path(tmp_path, instance=stack.instance_id("ints", tmp_path)))
    assert "db" in state["services"], "the interrupted container was left unrecorded"
    assert state["services"]["db"]["type"] == "compose"


# --------------------------------------------------------------------------------------
# R10-2: a compose file declaring a required variable stays checkable and stoppable
# --------------------------------------------------------------------------------------


REQUIRED_VAR = "DB_PASSWORD"


def _write_required_variable_compose_file(root, name="compose.yml"):
    """Place a compose file that refuses to be evaluated without one variable."""
    path = Path(root) / name
    path.write_text(
        "services:\n"
        "  db:\n"
        "    image: postgres\n"
        f"    environment:\n      POSTGRES_PASSWORD: ${{{REQUIRED_VAR}:?required}}\n"
    )
    return path


def _compose_requiring_variable(seen=None):
    """Answer Compose only when ``REQUIRED_VAR`` is defined, as Compose itself does.

    ``${VAR:?message}`` is a declaration that Compose must not proceed without,
    so every command -- ``ps`` and ``stop`` included -- exits non-zero while the
    variable is undefined, whatever the command was asked to do.
    """

    def fake_run_compose(
        instance, root, cfile, args, context=None, timeout=180.0, env=None, **kwargs
    ):
        if seen is not None:
            seen.append((list(args), None if env is None else dict(env)))
        ambient = os.environ if env is None else env
        if not ambient.get(REQUIRED_VAR):
            return subprocess.CompletedProcess(
                ["docker"],
                1,
                "",
                f'required variable {REQUIRED_VAR} is missing a value: required',
            )
        return subprocess.CompletedProcess(["docker"], 0, "abc123\n", "")

    return fake_run_compose


def test_compose_status_survives_a_missing_required_variable(monkeypatch, tmp_path):
    """R10-2: a compose file Compose cannot evaluate must not hide a live container."""
    _write_required_variable_compose_file(tmp_path)
    monkeypatch.delenv(REQUIRED_VAR, raising=False)
    monkeypatch.setattr(stack, "run_compose", _compose_requiring_variable())
    docker = _FakeDocker(inspect="running")
    monkeypatch.setattr(stack, "run_docker", docker)

    assert stack.compose_record_status(_compose_record(tmp_path), tmp_path) == "alive"
    # Docker answered from the Compose labels, so no compose file was parsed.
    assert "ps" in docker.verbs()
    assert "inspect" in docker.verbs()


def test_compose_teardown_survives_a_missing_required_variable(monkeypatch, tmp_path):
    """R10-2: Compose refusing the teardown must not strand the container."""
    _write_required_variable_compose_file(tmp_path)
    monkeypatch.delenv(REQUIRED_VAR, raising=False)
    monkeypatch.setattr(stack, "run_compose", _compose_requiring_variable())
    docker = _FakeDocker(inspect="running")
    monkeypatch.setattr(stack, "run_docker", docker)

    assert stack._stop_record(_compose_record(tmp_path), tmp_path) == "terminated"

    # The container was stopped and reclaimed through Docker itself.
    assert ["stop", "abc123"] in docker.calls
    assert ["rm", "-f", "abc123"] in docker.calls
    # Volumes are not the supervisor's to destroy, on this path either.
    assert not any("-v" in args for args in docker.calls)


def test_compose_stop_only_survives_a_missing_required_variable(monkeypatch, tmp_path):
    """R10-2: a `remove=False` teardown falls back without reclaiming the container."""
    _write_required_variable_compose_file(tmp_path)
    monkeypatch.delenv(REQUIRED_VAR, raising=False)
    monkeypatch.setattr(stack, "run_compose", _compose_requiring_variable())
    docker = _FakeDocker(inspect="running")
    monkeypatch.setattr(stack, "run_docker", docker)

    outcome = stack._stop_record(_compose_record(tmp_path), tmp_path, remove=False)

    assert outcome == "terminated"
    assert ["stop", "abc123"] in docker.calls
    assert not any(args[0] == "rm" for args in docker.calls)


def test_compose_record_keeps_the_environment_its_compose_file_requires(monkeypatch, tmp_path):
    """R10-2: the environment that satisfied `up` is recorded for every later command."""
    _write_required_variable_compose_file(tmp_path)
    monkeypatch.setattr(
        stack,
        "run_compose",
        lambda *a, **k: subprocess.CompletedProcess(["docker"], 0, "abc123\n", ""),
    )

    record = stack._start_compose_service(
        _compose_service(), tmp_path, "inst-1", env={REQUIRED_VAR: "s3cret", "DB_USER": "app"}
    )

    assert REQUIRED_VAR in record["compose_env"]
    # A secret-looking value is masked in state exactly as it is for a process
    # service: a later `ps` or `stop` needs the variable defined, not its value.
    assert record["compose_env"][REQUIRED_VAR] == stack.REDACTED
    assert record["compose_env"]["DB_USER"] == "app"


def test_compose_status_replays_the_recorded_environment(monkeypatch, tmp_path):
    """R10-2: a recorded environment lets Compose itself answer, with no fallback."""
    _write_required_variable_compose_file(tmp_path)
    monkeypatch.delenv(REQUIRED_VAR, raising=False)
    seen: list[tuple[list[str], dict | None]] = []
    monkeypatch.setattr(stack, "run_compose", _compose_requiring_variable(seen))

    def fake_run_docker(args, context=None, timeout=60.0, **kwargs):
        args = list(args)
        # The label query belongs to the Docker fallback alone, so reaching it
        # would mean Compose was never handed the environment it needs.
        assert args[0] != "ps", "the recorded environment did not reach Compose"
        return subprocess.CompletedProcess(["docker"], 0, "running\n", "")

    monkeypatch.setattr(stack, "run_docker", fake_run_docker)

    record = dict(_compose_record(tmp_path), compose_env={REQUIRED_VAR: stack.REDACTED})
    assert stack.compose_record_status(record, tmp_path) == "alive"
    assert seen and seen[0][1] is not None
    assert seen[0][1][REQUIRED_VAR] == stack.REDACTED


def test_compose_teardown_replays_the_recorded_environment(monkeypatch, tmp_path):
    """R10-2: a recorded environment lets Compose itself carry out the teardown."""
    _write_required_variable_compose_file(tmp_path)
    monkeypatch.delenv(REQUIRED_VAR, raising=False)
    seen: list[tuple[list[str], dict | None]] = []
    monkeypatch.setattr(stack, "run_compose", _compose_requiring_variable(seen))
    docker = _FakeDocker(inspect="running")
    monkeypatch.setattr(stack, "run_docker", docker)

    record = dict(_compose_record(tmp_path), compose_env={REQUIRED_VAR: stack.REDACTED})
    assert stack._stop_record(record, tmp_path) == "terminated"

    # Compose stopped and removed its own container, so Docker never had to.
    assert ["stop", "db"] in [args for args, _ in seen]
    assert ["rm", "-f", "db"] in [args for args, _ in seen]
    assert not any(args[0] in ("stop", "rm") for args in docker.calls)


def test_compose_env_is_optional_for_records_written_before_it(monkeypatch, tmp_path):
    """R10-2: a record with no `compose_env` still uses the ambient environment."""
    _write_compose_file(tmp_path)
    captured: list[dict | None] = []

    def fake_run_compose(instance, root, cfile, args, context=None, timeout=180.0, env=None, **kwargs):
        captured.append(None if env is None else dict(env))
        return subprocess.CompletedProcess(["docker"], 0, "abc123\n", "")

    monkeypatch.setattr(stack, "run_compose", fake_run_compose)
    monkeypatch.setattr(stack, "run_docker", _FakeDocker(inspect="running"))

    record = _compose_record(tmp_path)
    assert "compose_env" not in record
    assert stack.compose_record_status(record, tmp_path) == "alive"
    assert captured == [None]


# --------------------------------------------------------------------------------------
# R11-1: the Docker context in force at startup is pinned with the record
# --------------------------------------------------------------------------------------


def test_resolve_docker_context_prefers_the_context_named_in_the_environment(monkeypatch):
    """R11-1: `DOCKER_CONTEXT` already names the context, so Docker is not asked."""
    monkeypatch.setenv("DOCKER_CONTEXT", "orbstack")

    def _never(*args, **kwargs):
        raise AssertionError("docker must not be run while DOCKER_CONTEXT names a context")

    monkeypatch.setattr(subprocess, "run", _never)

    assert RESOLVE_DOCKER_CONTEXT() == "orbstack"


def test_resolve_docker_context_asks_docker_which_context_is_active(monkeypatch):
    """R11-1: the active context comes from `docker context show`."""
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        assert kwargs.get("timeout", 0) <= 5.0, "the probe must be bounded"
        return subprocess.CompletedProcess(argv, 0, "colima\n", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert RESOLVE_DOCKER_CONTEXT() == "colima"
    assert calls == [["docker", "context", "show"]]


@pytest.mark.parametrize(
    "outcome",
    [
        FileNotFoundError("docker"),
        subprocess.TimeoutExpired(["docker"], 5.0),
        subprocess.CompletedProcess(["docker"], 1, "", "cannot connect"),
        subprocess.CompletedProcess(["docker"], 0, "\n", ""),
    ],
)
def test_resolve_docker_context_reports_no_context_when_docker_cannot_answer(
    monkeypatch, outcome
):
    """R11-1: an unanswered probe leaves the record inheriting the ambient context."""
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)

    def fake_run(argv, **kwargs):
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert RESOLVE_DOCKER_CONTEXT() is None


def test_compose_start_records_the_active_docker_context(monkeypatch, tmp_path):
    """R11-1: a service that declares no context is pinned to the one in force."""
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setattr(stack, "resolve_current_docker_context", lambda *a, **k: "colima")
    calls: list[dict] = []
    monkeypatch.setattr(stack, "run_compose", _healthy_compose(calls))

    record = stack._start_compose_service(_compose_service(), tmp_path, "inst-1")

    assert record["docker_context"] == "colima"
    # Startup itself is pinned to the same context it records.
    assert calls and all(call["context"] == "colima" for call in calls)


def test_compose_start_keeps_the_context_the_manifest_declares(monkeypatch, tmp_path):
    """R11-1: a declared context is already explicit, so nothing is resolved."""
    monkeypatch.delenv("DOCKER_HOST", raising=False)

    def _never(*args, **kwargs):
        raise AssertionError("a declared context must not be resolved again")

    monkeypatch.setattr(stack, "resolve_current_docker_context", _never)
    calls: list[dict] = []
    monkeypatch.setattr(stack, "run_compose", _healthy_compose(calls))

    record = stack._start_compose_service(
        _compose_service(docker_context="orbstack"), tmp_path, "inst-1"
    )

    assert record["docker_context"] == "orbstack"
    assert calls and all(call["context"] == "orbstack" for call in calls)


def test_compose_start_pins_no_context_when_docker_host_decides_the_daemon(
    monkeypatch, tmp_path
):
    """R11-1: `--context` outranks `DOCKER_HOST`, so a pinned host stays alone."""
    monkeypatch.setenv("DOCKER_HOST", "tcp://remote:2375")

    def _never(*args, **kwargs):
        raise AssertionError("DOCKER_HOST already decides the daemon")

    monkeypatch.setattr(stack, "resolve_current_docker_context", _never)
    calls: list[dict] = []
    monkeypatch.setattr(stack, "run_compose", _healthy_compose(calls))

    record = stack._start_compose_service(_compose_service(), tmp_path, "inst-1")

    assert record["docker_context"] is None
    assert record["docker_host"] == "tcp://remote:2375"
    assert calls and all(call["context"] is None for call in calls)


def test_compose_start_keeps_the_ambient_docker_context(monkeypatch, tmp_path):
    """R12-1: `DOCKER_CONTEXT=colima rig up` must survive the environment allowlist."""
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setenv("DOCKER_CONTEXT", "colima")

    def _never(*args, **kwargs):
        raise AssertionError("an ambient DOCKER_CONTEXT already names the context")

    monkeypatch.setattr(stack, "resolve_current_docker_context", _never)
    calls: list[dict] = []
    # Started the way `rig up` starts it, so the declared environment is built
    # by the allowlist that drops every ambient `DOCKER_*` variable.
    record = _start_compose_through_start_service(
        monkeypatch, tmp_path, _compose_service(env={"PGDATA": "{root}/data/pg"}), calls
    )

    assert record["docker_context"] == "colima"
    assert calls and all(call["context"] == "colima" for call in calls)
    # The allowlist still governs the environment Compose itself receives.
    assert calls[0]["env"] is not None and "DOCKER_CONTEXT" not in calls[0]["env"]


def test_compose_start_prefers_the_ambient_context_over_the_ambient_host(
    monkeypatch, tmp_path
):
    """R12-1: Docker ranks `DOCKER_CONTEXT` above `DOCKER_HOST`, and so must the record."""
    monkeypatch.setenv("DOCKER_CONTEXT", "colima")
    monkeypatch.setenv("DOCKER_HOST", "tcp://remote:2375")

    def _never(*args, **kwargs):
        raise AssertionError("an ambient DOCKER_CONTEXT already names the context")

    monkeypatch.setattr(stack, "resolve_current_docker_context", _never)
    calls: list[dict] = []
    monkeypatch.setattr(stack, "run_compose", _healthy_compose(calls))

    record = stack._start_compose_service(_compose_service(), tmp_path, "inst-1")

    assert record["docker_context"] == "colima"
    # `--context` outranks `DOCKER_HOST`, so recording the host would name an
    # endpoint that decided nothing.
    assert record["docker_host"] is None
    assert calls and all(call["context"] == "colima" for call in calls)
    assert all(call["kwargs"]["docker_host"] is None for call in calls)


def test_compose_start_prefers_the_declared_context_over_the_ambient_one(
    monkeypatch, tmp_path
):
    """R12-1: the manifest is explicit, so an ambient context must not displace it."""
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setenv("DOCKER_CONTEXT", "desktop-linux")
    calls: list[dict] = []
    monkeypatch.setattr(stack, "run_compose", _healthy_compose(calls))

    record = stack._start_compose_service(
        _compose_service(docker_context="orbstack"), tmp_path, "inst-1"
    )

    assert record["docker_context"] == "orbstack"
    assert calls and all(call["context"] == "orbstack" for call in calls)


def test_compose_start_resolves_the_active_context_only_as_a_last_resort(
    monkeypatch, tmp_path
):
    """R12-1: `docker context show` decides only when nothing else names an endpoint."""
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    resolved: list[object] = []

    def _resolve(env=None, *args, **kwargs):
        resolved.append(env)
        return "desktop-linux"

    monkeypatch.setattr(stack, "resolve_current_docker_context", _resolve)
    calls: list[dict] = []
    monkeypatch.setattr(stack, "run_compose", _healthy_compose(calls))

    record = stack._start_compose_service(_compose_service(), tmp_path, "inst-1")

    assert len(resolved) == 1
    assert record["docker_context"] == "desktop-linux"
    assert record["docker_host"] is None
    assert calls and all(call["context"] == "desktop-linux" for call in calls)


def _capture_docker_argv(monkeypatch, port="0.0.0.0:54321", active=None):
    """Answer every real ``docker`` invocation, recording the full argv.

    ``active`` is a one-key dict holding the context ``docker context show``
    reports, so a test can switch Docker's active context in mid-run exactly as
    ``docker context use`` does.
    """
    argvs: list[list[str]] = []

    def fake_run(argv, **kwargs):
        argv = list(argv)
        argvs.append(argv)
        if argv[1:] == ["context", "show"]:
            name = (active or {}).get("context", "")
            return subprocess.CompletedProcess(argv, 0, f"{name}\n", "")
        if "port" in argv:
            return subprocess.CompletedProcess(argv, 0, f"{port}\n", "")
        if "ps" in argv:
            return subprocess.CompletedProcess(argv, 0, "abc123\n", "")
        if "inspect" in argv:
            return subprocess.CompletedProcess(argv, 0, "running\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return argvs


def test_docker_status_and_teardown_reach_the_recorded_docker_context(monkeypatch, tmp_path):
    """R11-1: a context switch after startup must not strand the container."""
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    # The user switched Docker's active context after the service started.
    monkeypatch.setenv("DOCKER_CONTEXT", "desktop-linux")
    _forbid_compose(monkeypatch)
    argvs = _capture_docker_argv(monkeypatch)
    record = dict(
        _compose_record(tmp_path / "deleted"), docker_context="colima", docker_host=None
    )

    assert stack.docker_record_status(record) == "alive"
    assert stack.docker_record_stop(record, remove=True) == "terminated"

    assert argvs
    assert all(argv[:3] == ["docker", "--context", "colima"] for argv in argvs)


def test_a_pinned_context_outranks_the_context_named_in_the_environment(
    monkeypatch, tmp_path
):
    """R11-1: an ambient `DOCKER_CONTEXT` must not compete with the pinned one."""
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setenv("DOCKER_CONTEXT", "desktop-linux")
    seen = _capture_docker_env(monkeypatch)
    record = dict(
        _compose_record(tmp_path / "deleted"), docker_context="colima", docker_host=None
    )

    assert stack.docker_record_status(record) == "alive"
    assert seen and all("DOCKER_CONTEXT" not in env for env in seen)


def test_compose_status_and_teardown_reach_the_recorded_docker_context(monkeypatch, tmp_path):
    """R11-1: Compose commands carry the recorded context, not the active one."""
    _write_compose_file(tmp_path)
    monkeypatch.setenv("DOCKER_CONTEXT", "desktop-linux")
    calls: list[dict] = []
    monkeypatch.setattr(stack, "run_compose", _healthy_compose(calls))
    monkeypatch.setattr(stack, "run_docker", _FakeDocker(inspect="running"))
    record = dict(_compose_record(tmp_path), docker_context="colima", docker_host=None)

    assert stack.compose_record_status(record, tmp_path) == "alive"
    assert stack._stop_record(record, tmp_path) == "terminated"

    assert calls and all(call["context"] == "colima" for call in calls)


def test_a_context_switch_between_up_and_down_does_not_strand_the_container(
    monkeypatch, tmp_path
):
    """R11-1: `down` reaches the context `up` used, so no record is discarded."""
    monkeypatch.setenv("RIG_STATE_HOME", str(tmp_path / "rig_state"))
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setattr(stack, "resolve_current_docker_context", RESOLVE_DOCKER_CONTEXT)
    active = {"context": "colima"}
    _write_compose_file(tmp_path)
    manifest_path = tmp_path / "rig.json"
    manifest_path.write_text(
        json.dumps(
            {
                "project": "shop",
                "services": {
                    "db": {
                        "type": "compose",
                        "compose_file": "compose.yml",
                        "compose_service": "db",
                    }
                },
                "scopes": {"full": ["db"]},
            }
        )
    )
    argvs = _capture_docker_argv(monkeypatch, active=active)

    assert stack.cmd_up(tmp_path, manifest_path, scope="full") == stack.EXIT_OK
    instance = stack.instance_id("shop", tmp_path)
    state_path = stack._state_path(tmp_path, instance=instance)
    assert stack.read_state(state_path)["services"]["db"]["docker_context"] == "colima"

    # `docker context use` switches the active context, and every later command
    # must ignore the switch.
    active["context"] = "desktop-linux"
    argvs.clear()

    assert stack.cmd_down(root=tmp_path, manifest_path=manifest_path) == stack.EXIT_OK

    assert argvs
    assert all(argv[:3] == ["docker", "--context", "colima"] for argv in argvs)
    assert stack.read_state(state_path)["services"] == {}


# --------------------------------------------------------------------------------------
# R12-1: plain Docker commands keep the recorded Docker client configuration
# --------------------------------------------------------------------------------------


class _EnvRecordingDocker(_FakeDocker):
    """A ``_FakeDocker`` that also keeps the environment each call carried."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.envs: list[dict | None] = []

    def __call__(self, args, context=None, timeout=60.0, **kwargs):
        self.envs.append(kwargs.get("env"))
        return super().__call__(args, context=context, timeout=timeout, **kwargs)


def _capture_docker_argv_and_env(monkeypatch, inspect="running"):
    """Answer every real ``docker`` invocation, recording argv and environment."""
    seen: list[tuple[list[str], dict]] = []

    def fake_run(argv, **kwargs):
        argv = list(argv)
        seen.append((argv, dict(kwargs.get("env") or {})))
        if "ps" in argv:
            return subprocess.CompletedProcess(argv, 0, "abc123\n", "")
        if "inspect" in argv:
            return subprocess.CompletedProcess(argv, 0, f"{inspect}\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return seen


def test_run_docker_replays_the_recorded_docker_client_settings(monkeypatch, tmp_path):
    """R12-1: a recorded config directory reaches a plain `docker` command."""
    monkeypatch.delenv("DOCKER_CONFIG", raising=False)
    seen = _capture_docker_argv_and_env(monkeypatch)

    stack.run_docker(
        ["inspect", "abc123"],
        "colima",
        env={"DOCKER_CONFIG": "/srv/rig/.docker", "DB_USER": "app"},
    )

    argv, env = seen[0]
    assert argv[:3] == ["docker", "--context", "colima"]
    assert env["DOCKER_CONFIG"] == "/srv/rig/.docker"
    # Only the client settings are replayed: a plain `docker` command needs the
    # ambient environment it runs in, not the service's declared variables.
    assert "DB_USER" not in env
    assert "PATH" in env


def test_run_docker_ignores_a_client_setting_the_record_never_held(monkeypatch, tmp_path):
    """R12-1: a setting that appeared after startup must not redirect the client."""
    monkeypatch.setenv("DOCKER_CONFIG", "/home/dev/.docker")
    monkeypatch.setenv("DOCKER_TLS_VERIFY", "1")
    seen = _capture_docker_argv_and_env(monkeypatch)

    stack.run_docker(["ps", "-q"], None, env={"DB_USER": "app"})

    _, env = seen[0]
    assert "DOCKER_CONFIG" not in env
    assert "DOCKER_TLS_VERIFY" not in env


def test_run_docker_without_a_recorded_environment_uses_the_ambient_settings(
    monkeypatch, tmp_path
):
    """R12-1: a record written before the environment was kept still works."""
    monkeypatch.setenv("DOCKER_CONFIG", "/home/dev/.docker")
    seen = _capture_docker_argv_and_env(monkeypatch)

    stack.run_docker(["ps", "-q"], None)

    _, env = seen[0]
    assert env["DOCKER_CONFIG"] == "/home/dev/.docker"


def test_docker_status_carries_the_recorded_client_configuration(monkeypatch, tmp_path):
    """R12-1: the Docker fallback inspects through the recorded config directory."""
    monkeypatch.setenv("DOCKER_CONFIG", "/home/dev/.docker")
    # The checkout is gone, so plain Docker, not Compose, answers for the record.
    seen = _capture_docker_argv_and_env(monkeypatch)
    record = dict(
        _compose_record(tmp_path),
        compose_env={"DOCKER_CONFIG": "/srv/rig/.docker", "DB_USER": "app"},
    )

    assert stack.compose_record_status(record, tmp_path) == "alive"

    assert [argv for argv, _ in seen]
    assert all(env.get("DOCKER_CONFIG") == "/srv/rig/.docker" for _, env in seen)
    # Both the label query and the inspection travel through the same config.
    verbs = {argv[1] for argv, _ in seen}
    assert {"ps", "inspect"} <= verbs


def test_docker_teardown_carries_the_recorded_client_configuration(monkeypatch, tmp_path):
    """R12-1: `stop` and `rm` reach the daemon the record was started against."""
    monkeypatch.delenv("DOCKER_CONFIG", raising=False)
    seen = _capture_docker_argv_and_env(monkeypatch)
    record = dict(
        _compose_record(tmp_path),
        compose_env={"DOCKER_CONFIG": "/srv/rig/.docker", "DOCKER_TLS_VERIFY": "1"},
    )

    assert stack._stop_record(record, tmp_path, remove=True) == "terminated"

    verbs = {argv[1] for argv, _ in seen}
    assert {"stop", "rm"} <= verbs
    assert all(env.get("DOCKER_CONFIG") == "/srv/rig/.docker" for _, env in seen)
    assert all(env.get("DOCKER_TLS_VERIFY") == "1" for _, env in seen)


def test_compose_status_hands_plain_docker_the_recorded_environment(monkeypatch, tmp_path):
    """R12-1: the compose path passes the record's environment to every probe."""
    _write_compose_file(tmp_path)
    # Compose lists a replica the record never named, so both containers are
    # inspected through plain Docker while the compose file is still present.
    monkeypatch.setattr(
        stack,
        "run_compose",
        lambda *a, **k: subprocess.CompletedProcess(["docker"], 0, "def456\n", ""),
    )
    docker = _EnvRecordingDocker(inspect="running")
    monkeypatch.setattr(stack, "run_docker", docker)
    record = dict(_compose_record(tmp_path), compose_env={"DOCKER_CONFIG": "/srv/rig/.docker"})

    assert stack.compose_record_status(record, tmp_path) == "alive"

    assert docker.envs
    assert all(env and env["DOCKER_CONFIG"] == "/srv/rig/.docker" for env in docker.envs)
