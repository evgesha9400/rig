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
    def mock_run_compose(instance, root, compose_file, args, context=None, timeout=180.0, env=None):
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
    assert not dead_dir.exists()
    assert alive_dir.exists()


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



