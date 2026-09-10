# rig

A zero-dependency, zero-daemon developer environment supervisor and process runner for multi-service repositories. Featuring machine-wide supervision, dynamic port allocation, zero-race socket inheritance, multi-stack modes (`native`, `container`), and verified lifecycle management.

---

## Why `rig`?

Modern multi-service local development often suffers from:
- **Port collisions**: Multiple developers or multiple checkouts of the same repo colliding on static ports like `3000` or `8000`.
- **Zombie processes**: Dev servers left orphaned after an interrupted test run, keeping ports bound and blocking subsequent runs.
- **Race conditions on port binding**: Allocating an ephemeral port, closing the probe socket, and having another process grab it before the service can bind.
- **Hidden global state**: No way to see what services or test instances are running across all checkouts on your machine.
- **Heavy or brittle supervisors**: Requiring background daemons (systemd/dockerd/supervisord) or complex Node/Ruby process supervisors just to launch a Python API and a frontend dev server.

`rig` solves this with:
1. **Zero External Runtime Dependencies**: Standard library Python 3.10+ only (`socket`, `subprocess`, `os`, `signal`, `json`, `fcntl`, `shlex`, `dataclasses`, `pathlib`).
2. **Zero Persistent Daemons**: Fully file-backed atomic registry (`~/.local/state/rig/instances/`) and non-blocking file locks (`flock`). Fast, crash-resilient, and stateless.
3. **Machine-Wide Supervision**: Inspect all active projects across your machine (`rig ps`), stop any named project (`rig down <project>`), or tear down all active instances at once (`rig down --all`).
4. **Multi-Stack Modes (`native` vs `container`)**: Define base services and mode overlays in a single `rig.json`. Switch modes cleanly with mutex collision protection (`rig up --mode container --switch`).
5. **Zero-Race Socket Inheritance (`type: "fd"`)**: Binds listening sockets on kernel port `0`, holds them open, and passes the descriptors directly into child processes (`--fd {fd}`). The port is never released between allocation and service start.
6. **Strict Ephemeral Ports (`type: "port"`)**: Passes dynamically allocated free ports (`--port {port}`) and verifies listener identity via positive matching on the recorded PID/PGID.
7. **One-Command Setup**: `rig init [--up]` automatically scans your repository for Docker Compose, FastAPI, Flask, Django, Vite, or Next.js and generates a validated `rig.json`.
8. **AI-Friendly Protocol**: Universal `--json` output envelope (`ok`, `schema`, `data`/`error`) and deterministic exit codes (`0` to `6`, `130`) designed for autonomous agents and CLI automation.

---

## Installation

### Install as a Standalone Global Tool (Recommended)

Using `uv`:
```bash
uv tool install --force "git+https://github.com/evgesha9400/rig.git"
```

Using `pipx` / `pip`:
```bash
pip install --user "git+https://github.com/evgesha9400/rig.git"
```

### Add to a Specific Project

```bash
uv add "git+https://github.com/evgesha9400/rig.git"
```

### Direct Drop-in (Zero Installation)
Since `rig` is a single self-contained module with zero third-party dependencies, you can copy `src/rig/cli.py` directly into any repository (e.g. `scripts/rig.py`):
```bash
curl -fsSL https://raw.githubusercontent.com/evgesha9400/rig/main/src/rig/cli.py -o scripts/rig.py
```

---

## Quick Start

### 1. Initialize a Project

Run `rig init` in your repository root. `rig` inspects your files, detects existing backends, frontends, and Docker Compose configurations, and writes a tailored `rig.json`:

```bash
rig init
# Or initialize and start services immediately:
rig init --up
```

You can preview the detected configuration without writing files:
```bash
rig init --dry-run
```

### 2. Verify Your Environment

Run pre-flight static verification to ensure working directories exist, binaries are executable, Docker Compose files are present, and dependency graphs contain no cycles:

```bash
rig check
```

### 3. Start & Supervise Services

```bash
# Start all services in the active or default mode
rig up

# Check status of the local checkout
rig status

# View all running projects and instances across your machine
rig ps

# Stop all services in the local checkout
rig down
```

---

## Global Machine-Wide Supervision

`rig` maintains a machine-wide state registry under `$XDG_STATE_HOME/rig/instances/` (default: `~/.local/state/rig/instances/`). Every project instance records its directory, PID, PGID, active mode, and allocated ports.

### Inspect All Projects (`rig ps`)

```bash
rig ps
```
Example output:
```text
PROJECT         INSTANCE      MODE       STATUS    ACTIVE  PORTS                    ROOT
deltalytic      68d374ab9c34  native     running   2/2     backend:54123, ui:54124  /Users/alice/projects/deltalytic
my-api          a1b2c3d4e5f6  container  running   1/1     db:5432                  /Users/alice/work/my-api
```

`STATUS` reports the instance as a whole: `running` when every recorded service is up, `partial` when only some are, `stopped` when none are, and `orphaned` when the checkout directory no longer exists.

Add `--health` to probe HTTP endpoints for live health checks:
```bash
rig ps --health
```

### Targeted Teardown

Stop a project from anywhere on your machine, even if you are not inside its directory:

```bash
# Stop by project name slug
rig down deltalytic

# Stop by specific instance ID
rig down 68d374ab9c34

# Stop ALL running instances across the entire machine
rig down --all
```

`rig` stores process group IDs (`PGID`) and Docker Compose project references in its state registry, allowing it to cleanly terminate orphaned services even if the original working tree was deleted (`rm -rf`).

### Cleanup Stale Instances

```bash
# Clean up dead instances whose processes are no longer running
rig prune

# Force-kill any lingering processes in unmanaged instances and prune
rig prune --force
```

`prune` reclaims an instance's recorded state but keeps its `checkout.lock` file,
so a concurrent `rig` command can never take a lock on a file nobody else can
see. `prune --force` stops services dependents-first and exits `1` while
preserving any dependency whose dependent refused to stop.

---

## Multi-Stack Modes (`native` vs `container`)

`rig` supports multi-stack modes within a single `rig.json`. For example, you can run database dependencies in containers while developing application code natively, or run the entire stack in containers.

### Example `rig.json` with Modes:

```json
{
  "$schema": "https://raw.githubusercontent.com/evgesha9400/rig/main/rig.schema.json",
  "project": "my-app",
  "default_mode": "native",
  "services": {
    "db": {
      "type": "compose",
      "compose_file": "docker-compose.yml",
      "compose_service": "postgres",
      "health_tcp": 5432
    }
  },
  "modes": {
    "native": {
      "services": {
        "backend": {
          "type": "fd",
          "cwd": "backend",
          "command": ".venv/bin/python -m app.main --fd {fd}",
          "health": "/healthz",
          "depends_on": ["db"]
        },
        "frontend": {
          "type": "port",
          "cwd": "frontend",
          "command": "npm run dev -- --port {port}",
          "health": "/",
          "depends_on": ["backend"],
          "env": {
            "VITE_API_PORT": "{backend_port}"
          }
        }
      }
    },
    "container": {
      "services": {
        "backend": {
          "type": "compose",
          "compose_file": "docker-compose.yml",
          "compose_service": "backend",
          "health": "http://127.0.0.1:8000/healthz",
          "depends_on": ["db"]
        },
        "frontend": {
          "type": "compose",
          "compose_file": "docker-compose.yml",
          "compose_service": "frontend",
          "health": "http://127.0.0.1:3000/",
          "depends_on": ["backend"]
        }
      }
    }
  }
}
```

### Switching Modes Safely

`rig` prevents accidental multi-mode conflicts. If services are currently running in `native` mode, attempting to start `container` mode without stopping the old services will be safely rejected:

```bash
# Fails with exit code 3 (E_MODE_CONFLICT) to prevent colliding processes:
rig up --mode container

# Cleanly tears down native services first and boots container mode:
rig up --mode container --switch
```

---

## AI Agent & Automation Protocol

`rig` is designed from the ground up for reliable operation by AI coding assistants, orchestrators, and CI pipelines:

### Universal `--json` Envelope

Every command accepts `--json` and emits a predictable schema:

```json
{
  "schema": "rig.ps/1",
  "ok": true,
  "data": [
    {
      "project": "my-app",
      "instance_id": "68d374ab9c34",
      "mode": "native",
      "state": "running",
      "services_count": 2,
      "services_active": 2,
      "root": "/path/to/my-app",
      "ports": {"backend": 54123, "frontend": 54124}
    }
  ]
}
```

Errors emit structured details with recovery hints:
```json
{
  "schema": "rig.error/1",
  "ok": false,
  "error": {
    "code": "E_MODE_CONFLICT",
    "message": "Instance is running in mode 'native'; cannot start mode 'container'",
    "hint": "Pass --switch to stop the active mode first, or run 'rig down' before starting a new mode.",
    "details": {"active_mode": "native", "requested_mode": "container"}
  }
}
```

### Deterministic Exit Codes

| Exit Code | Constant | Meaning |
|---|---|---|
| `0` | `EXIT_OK` | Command completed successfully. |
| `1` | `EXIT_OP_FAILED` | Service failed to start, healthcheck timed out, or teardown failed. |
| `2` | `EXIT_USAGE` | Invalid command line arguments or invalid manifest syntax. |
| `3` | `EXIT_MUTEX_CONFLICT` | Instance lock busy (`checkout.lock`) or mode conflict without `--switch`. |
| `4` | `EXIT_NOT_FOUND` | Project, service, or instance target not found. |
| `5` | `EXIT_REFUSED` | Operation refused (e.g. destructive action without confirmation). |
| `6` | `EXIT_EXTERNAL_TOOL` | Missing external requirement (`docker`, `compose`, `lsof`). |
| `130` | `EXIT_INTERRUPTED` | Interrupted by signal (`SIGINT`, `SIGTERM`). |

---

## Manifest Reference (`rig.json`)

To inspect or validate manifest configurations against the formal JSON Schema:
```bash
rig schema
```

### Root Fields

| Field | Type | Required | Description |
|---|---|---|---|
| `project` | string | Yes | Project identifier slug used for isolation and Docker Compose naming. |
| `default_mode` | string | No | Mode to use when `--mode` is omitted (defaults to first mode in `modes` or `native`). |
| `services` | object | No | Base services active across all modes. |
| `modes` | object | No | Dictionary of mode configurations (`{"native": {"services": {...}}, "container": ...}`). |

### Service Fields

| Field | Type | Required | Description |
|---|---|---|---|
| `type` | `"fd"` \| `"port"` \| `"compose"` | Yes | Port allocation strategy. |
| `command` | string | For `fd` / `port` | Command line to execute. Supports `{fd}`, `{port}`, and `{<service>_port}` placeholders. |
| `cwd` | string | No | Working directory relative to repository root (defaults to `.`). |
| `health` | string | No | HTTP path to poll for 200 OK (e.g. `/healthz`, `/`). |
| `health_tcp` | integer | No | TCP port to poll for socket connection (ideal for databases like Postgres/Redis). |
| `depends_on` | string[] | No | Services that must be healthy before this service starts. |
| `aliases` | string[] | No | Alternative names for scope targeting (e.g. `["ui"]` for `frontend`). |
| `env` | map | No | Environment variables. Supports `{<service>_port}` placeholders. |
| `env_files` | string[] | No | Dotenv-style files, relative to the repository root, loaded before `env`. |
| `inherit` | string[] | No | Ambient environment variables to pass through beyond the base safe allowlist. |
| `compose_file` | string | For `compose` | Path to Docker Compose file. |
| `compose_service`| string | For `compose` | Name of service inside Docker Compose file. |
| `compose_port` | integer | No | Container port whose published host port is recorded as the service URL. |
| `docker_context`| string | No | Docker context every command for this service is pinned to. |

### Environment and Docker Endpoint for `compose` Services

`env`, `env_files` and `inherit` apply to `compose` services as well as to `fd`
and `port` services. The resulting environment is handed to `docker compose`
itself, so it drives `${VAR}` interpolation inside the compose file and reaches
the containers.

That environment is an allowlist, so no ambient `DOCKER_*`, `COMPOSE_*` or
application variable can leak in and point a service at another project's
resources. The Docker client settings (`DOCKER_CONFIG`, `DOCKER_CERT_PATH`,
`DOCKER_TLS_VERIFY`) are the exception: they are passed through so a TLS or
rootless setup can still reach its own daemon.

Those client settings are recorded with the service, and every later plain
`docker` command — the label query, the inspection, `stop` and `rm` — is given
the recorded ones instead of whatever the terminal holds. A service started
against its own `DOCKER_CONFIG` therefore stays reachable for `rig status` and
`rig down`, and a `DOCKER_CONFIG` exported afterwards cannot redirect them.

The Docker endpoint in force at startup — `DOCKER_HOST` and the Docker context
— is recorded with the service. Every later status query and teardown is pinned
to that endpoint, so a `DOCKER_HOST` that changes between `rig up` and `rig
down` can never send the query to a daemon that does not hold the container.

The endpoint is chosen in Docker's own order of precedence:

1. the `docker_context` the manifest declares;
2. the ambient `DOCKER_CONTEXT`, which is read even though the service
   environment is an allowlist, so `DOCKER_CONTEXT=colima rig up` is honoured;
3. the ambient `DOCKER_HOST`, when neither of the above names a context;
4. otherwise the active context, resolved with `docker context show`.

Whenever a context decides, it is recorded alone and no host is recorded with
it, because `--context` outranks `DOCKER_HOST`. A later `docker context use
colima` therefore does not strand the container: `rig status`, `rig down` and
`rig prune` still reach the context that holds it.

---

## How Socket Inheritance Works (`type: "fd"`)

When a service specifies `type: "fd"`, `rig`:
1. Creates a TCP socket bound to `127.0.0.1:0`. The OS kernel allocates a free ephemeral port immediately.
2. Marks the socket listening (`listen(128)`).
3. Keeps the descriptor open and passes it via `subprocess.Popen(pass_fds=[fd])`.
4. Passes the integer descriptor to the command line via `--fd {fd}`.

### Python / Uvicorn Example:

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

## Symmetrical `Makefile` Integration

```makefile
RIG ?= rig

up:
	@$(RIG) up

down:
	@$(RIG) down

status:
	@$(RIG) status

ps:
	@$(RIG) ps

check:
	@$(RIG) check

logs:
	@tail -n 200 -F .local-run/logs/*.log
```

---

## Development & Testing

```bash
# Clone the repository
git clone https://github.com/evgesha9400/rig.git
cd rig

# Run full test suite with uv
uv run --with pytest pytest tests/
```

## License

MIT
