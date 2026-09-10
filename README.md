# rig

A zero-dependency local dev environment and process runner for multi-service repositories, featuring dynamic port allocation, zero-race socket inheritance, and verified lifecycle management.

---

## Why `rig`?

Modern multi-service local development often suffers from:
- **Port collisions**: Multiple developers or multiple checkouts of the same repo colliding on static ports like `3000` or `8000`.
- **Zombie processes**: Dev servers left orphaned after an interrupted test run, keeping ports bound and blocking subsequent runs.
- **Race conditions on port binding**: Allocating an ephemeral port, closing the probe socket, and having another process grab it before the service can bind.
- **Heavy dependencies**: Needing Docker Compose for pure local code, or requiring complex Node/Ruby process supervisors just to launch a Python API and a frontend dev server.

`rig` solves this with:
1. **Zero External Runtime Dependencies**: Standard library Python 3.10+ only (`socket`, `subprocess`, `os`, `signal`, `json`, `fcntl`, `shlex`).
2. **Zero-Race Socket Inheritance (`type: "fd"`)**: Binds listening sockets on kernel port `0`, holds them open, and passes the descriptors directly into child processes (`--fd {fd}`). The port is never released between allocation and service start.
3. **Strict Ephemeral Ports (`type: "port"`)**: Passes dynamically allocated free ports (`--port {port}`) and verifies listener identity via positive `lsof` matching on the recorded PID/PGID.
4. **Per-Checkout Process & State Isolation**: State recorded in `.local-run/state.json`, logs in `.local-run/logs/`, and a monotonic `flock` in `.local-run/checkout.lock`.
5. **Reverse Topological Teardown**: Dependents are shut down before dependencies, escalating cleanly from `SIGTERM` to `SIGKILL` with socket-release verification.

---

## Installation

### Add to a Project (Recommended)

Using `uv`:
```bash
uv add "git+https://github.com/evgesha9400/rig.git"
```

Using `pip`:
```bash
pip install "git+https://github.com/evgesha9400/rig.git"
```

### Install as a Standalone Global Tool

```bash
uv tool install "git+https://github.com/evgesha9400/rig.git"
```

### Direct Drop-in (Zero Installation)
Since `rig` is a single-module implementation with zero third-party dependencies, you can also copy `src/rig/cli.py` directly into any repository (e.g. `scripts/rig.py`):
```bash
curl -fsSL https://raw.githubusercontent.com/evgesha9400/rig/main/src/rig/cli.py -o scripts/rig.py
```

---

## Quick Start

### 1. Define `rig.json`

Create `rig.json` (or `scripts/rig.json`) in your repository root:

```json
{
  "project": "my-app",
  "services": {
    "backend": {
      "type": "fd",
      "cwd": "backend",
      "command": ".venv/bin/python -m myapp.server --fd {fd}",
      "health": "/api/health"
    },
    "frontend": {
      "type": "port",
      "cwd": "frontend",
      "command": "node node_modules/vite/bin/vite.js --host 127.0.0.1 --port {port} --strictPort",
      "aliases": ["ui"],
      "depends_on": ["backend"],
      "health": "/",
      "env": {
        "VITE_BACKEND_PORT": "{backend_port}"
      }
    }
  }
}
```

### 2. Symmetrical `Makefile` Integration

Add standard targets to your project's `Makefile`:

```makefile
RIG ?= rig

up:
	@$(RIG) up --scope full

down:
	@$(RIG) down --scope full

status:
	@$(RIG) status

backend-up:
	@$(RIG) up --scope backend

backend-down:
	@$(RIG) down --scope backend

ui-up:
	@$(RIG) up --scope ui

ui-down:
	@$(RIG) down --scope ui

logs:
	@tail -n 200 -F .local-run/logs/*.log
```

---

## CLI Usage

```bash
# Start all services (default scope: full)
rig up

# Check status, allocated ports, PIDs, and health
rig status

# Start only backend services
rig up --scope backend

# Start UI and its required dependencies
rig up --scope ui

# Stop UI only (leaves backend running)
rig down --scope ui

# Stop all services cleanly
rig down
```

---

## Manifest Reference (`rig.json`)

| Field | Type | Description |
|---|---|---|
| `project` | string | Project identifier slug used for process isolation and Docker Compose project naming. |
| `services` | object | Map of service name to service configuration. |

### Service Configuration Fields

| Field | Type | Required | Description |
|---|---|---|---|
| `type` | `"fd"` \| `"port"` \| `"compose"` | Yes | Port allocation strategy. |
| `command` | string | For `fd` and `port` | Command line to execute. Supports `{fd}`, `{port}`, and `{<service>_port}` placeholders. |
| `cwd` | string | No | Working directory relative to repository root (defaults to `.`). |
| `health` | string | No | HTTP path to poll for 200 OK (e.g. `/healthz`, `/`). |
| `depends_on` | array of strings | No | Services that must be healthy before this service starts. |
| `aliases` | array of strings | No | Alternative names for scope targeting (e.g. `["ui"]` for `frontend`). |
| `env` | map of string -> string | No | Custom environment variables. Supports `{<service>_port}` placeholders. |
| `inherit` | array of strings | No | Ambient environment variables to pass through beyond the base safe allowlist. |

---

## How Socket Inheritance Works (`type: "fd"`)

When a service specifies `type: "fd"`, `rig`:
1. Creates a TCP socket bound to `127.0.0.1:0`. The OS kernel allocates a free ephemeral port immediately.
2. Marks the socket listening (`listen(128)`).
3. Keeps the descriptor open and passes it via `subprocess.Popen(pass_fds=[fd])`.
4. Passes the integer descriptor to the command line via `--fd {fd}`.

### Python / Uvicorn Server Example:

```python
import argparse
import socket
import uvicorn

parser = argparse.ArgumentParser()
parser.add_argument("--fd", type=int, default=None)
args = parser.parse_args()

if args.fd is not None:
    sock = socket.fromfd(args.fd, socket.AF_INET, socket.SOCK_STREAM)
    uvicorn.run("myapp.main:app", fd=sock.fileno())
else:
    uvicorn.run("myapp.main:app", host="127.0.0.1", port=8000)
```

---

## Development & Testing

```bash
# Clone the repository
git clone https://github.com/evgesha9400/rig.git
cd rig

# Run full test suite with uv
uv run --with pytest pytest tests/

# Or using Make
make test
```

## License

MIT
