"""Health check HTTP polling without external proxies or redirects."""

from __future__ import annotations

import os
import time
import urllib.error
import urllib.request

from rig.core.constants import HEALTH_TIMEOUT_SECS
from rig.net.probe import port_listener_matches

HTTP_STATUS_OK = 200
HTTP_STATUS_REDIRECT = 300


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _health_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _RefuseRedirects())


def _pid_is_running(pid: int | None) -> bool:
    if pid is None:
        return True
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    else:
        return True


def _check_http_response(
    opener: urllib.request.OpenerDirector,
    url: str,
    target: tuple[int, int | None, int | None, float],
) -> bool:
    port, pid, pgid, timeout = target
    try:
        with opener.open(url, timeout=min(2.0, max(0.2, timeout))) as response:
            if HTTP_STATUS_OK <= response.status < HTTP_STATUS_REDIRECT:
                if pid is None and pgid is None:
                    return True
                if _pid_is_running(pid) and port_listener_matches(port, pgid=pgid, pid=pid):
                    return True
    except (urllib.error.URLError, OSError, ValueError):
        pass
    return False


def wait_for_http(
    port: int,
    path: str,
    timeout: float | tuple[float, int | None, int | None] = HEALTH_TIMEOUT_SECS,
) -> bool:
    """Poll ``http://127.0.0.1:<port><path>`` until it answers 2xx or deadline passes."""
    t_val = timeout[0] if isinstance(timeout, tuple) else float(timeout)
    pid = timeout[1] if isinstance(timeout, tuple) else None
    pgid = timeout[2] if isinstance(timeout, tuple) else None
    opener = _health_opener()
    clean_path = path if path.startswith("/") else f"/{path}"
    url = f"http://127.0.0.1:{port}{clean_path}"
    target = (port, pid, pgid, t_val)
    deadline = time.monotonic() + t_val
    while True:
        if pid is not None and not _pid_is_running(pid):
            return False
        if _check_http_response(opener, url, target):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.15)
