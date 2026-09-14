"""Health checks: 200 vs service error, fast-fail, ambient proxy settings."""

import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from rig import cli as rig

stack = rig

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


def _await_file(path, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if Path(path).exists():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
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
