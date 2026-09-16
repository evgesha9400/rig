# rig

<p align="center">
  <strong>Zero-dependency, zero-daemon developer environment supervisor and process runner for multi-service repositories.</strong>
</p>

<p align="center">
  <a href="https://pypi.org/project/rig-cli/"><img src="https://img.shields.io/pypi/v/rig-cli.svg?color=007ec6" alt="PyPI version"></a>
  <a href="https://pypi.org/project/rig-cli/"><img src="https://img.shields.io/pypi/pyversions/rig-cli.svg" alt="Python Versions"></a>
  <a href="https://github.com/evgesha9400/rig/actions/workflows/publish.yml"><img src="https://github.com/evgesha9400/rig/actions/workflows/publish.yml/badge.svg" alt="CI Status"></a>
  <a href="https://github.com/evgesha9400/rig/blob/main/LICENSE"><img src="https://img.shields.io/badge/License-MIT-green.svg" alt="License: MIT"></a>
  <a href="#zero-runtime-dependencies"><img src="https://img.shields.io/badge/dependencies-0-brightgreen.svg" alt="Zero Dependencies"></a>
</p>

<p align="center">
  <a href="https://pypi.org/project/rig-cli/">PyPI Package</a> •
  <a href="#the-3-step-fast-track">Quick Start</a> •
  <a href="#feature-comparison">Comparison</a> •
  <a href="#zero-race-socket-inheritance-type-fd">Socket Inheritance</a> •
  <a href="#machine-wide-supervision-rig-ps">Machine Supervision</a> •
  <a href="#ai-agent--automation-protocol">AI Agent Protocol</a> •
  <a href="#cli-command-reference">CLI Reference</a>
</p>

---

## The Acute Friction

Local multi-service development across multiple git branches and checkouts is routinely broken by five recurring headaches:

| Problem | Traditional Workaround | The `rig` Solution |
|---|---|---|
| **Port Collisions** | `lsof -i :3000` & `kill -9` | **Dynamic & Sticky Port Leasing**: Auto-assigns friendly ports (`3000`, `8000`), increments on collision, and remembers ports across restarts. |
| **Port Binding Race Conditions** | Probe port, close socket, hope child binds before another process grabs it | **Zero-Race Socket Inheritance (`type: "fd"`)**: Binds kernel port `0`, holds the socket open, and passes descriptor directly to child processes (`--fd {fd}`). |
| **Orphaned Zombie Processes** | `killall node` / `pkill python` | **Atomic File-Backed Registry**: Tracks PID and PGID per instance in `~/.local/state/rig/`. Cleans up orphaned services even if the directory was deleted (`rm -rf`). |
| **Heavy Supervisor Daemons** | Docker Compose for everything, systemd, supervisord, Procfile wrappers | **Zero Runtime Dependencies & Zero Daemons**: Standard library Python 3.10+ only. Starts instantaneously, uses kernel `flock` locks, and exits cleanly. |
| **Invisible Machine State** | No single view of what processes or checkouts are running | **Machine-Wide Visibility**: Run `rig ps` from anywhere to inspect all active checkouts, their allocated ports, health, and status across your entire machine. |

---

## Feature Comparison

How `rig` compares against standard development orchestrators:

| Capability | `rig` | Docker Compose | Foreman / Overmind | systemd / supervisord |
|---|:---:|:---:|:---:|:---:|
| **Zero Third-Party Dependencies** | ✅ (Standard Library) | ❌ (Docker Engine) | ❌ (Ruby/Go toolchains) | ❌ (System-level packages) |
| **Zero Background Daemons** | ✅ (File-mutex lock) | ❌ (Requires `dockerd`) | ❌ (Requires background tmux/daemon) | ❌ (Requires system daemon) |
| **Dynamic & Sticky Port Allocation** | ✅ Built-in | ❌ Manual config | ❌ Hardcoded ports | ❌ Hardcoded ports |
| **Zero-Race Socket Inheritance (`type: "fd"`)** | ✅ Kernel socket pass | ❌ Bridge network NAT | ❌ No | ⚠️ Systemd socket units only |
| **Machine-Wide Multi-Project View** | ✅ `rig ps` | ⚠️ Per-project compose | ❌ Checkout-isolated only | ⚠️ Global service list |
| **Cross-Checkout Targeted Teardown** | ✅ `rig down <slug>` | ❌ Must `cd` to folder | ❌ Must `cd` to folder | ⚠️ System unit names |
| **Typed JSON Envelopes for AI Agents** | ✅ `--json` on every cmd | ⚠️ Untyped CLI json | ❌ Plain text output | ❌ Plain text output |
| **Deterministic Error Codes** | ✅ Typed error codes | ❌ Generic 0 or 1 | ❌ Generic 0 or 1 | ❌ Generic exit status |

---

## The 3-Step Fast Track

### 1. Install Globally (User Space)

Install `rig-cli` from [PyPI](https://pypi.org/project/rig-cli/) using standard, isolated tool runners:

```bash
# Recommended (pipx)
pipx install rig-cli

# Ultra-fast alternative (uv)
uv tool install rig-cli
```

> [!NOTE]
> The PyPI distribution package is named **`rig-cli`** (the short name `rig` belongs to an unrelated legacy library). Once installed, both `rig` and `rig-cli` commands are available globally on your `$PATH`.

### 2. Initialize Any Repository

Run `rig init` inside your project root. `rig` inspects your directory, detects Docker Compose, FastAPI, Flask, Django, Vite, or Next.js, and generates a tailored `rig.json`:

```bash
rig init
```

*Preview detected services without writing to disk:*
```bash
rig init --dry-run
```

### 3. Launch and Supervise

```bash
# Start all services in the active mode
rig up

# Check status of the local checkout
rig status

# Inspect service logs
rig logs backend -n 50

# Stop services in this checkout
rig down
```

---

## Machine-Wide Supervision (`rig ps`)

`rig` maintains a machine-wide state registry under `$XDG_STATE_HOME/rig/instances/` (`~/.local/state/rig/instances/`). Every project instance records its directory, PID, PGID, active mode, and allocated ports.

```bash
rig ps
```

```text
PROJECT         INSTANCE      MODE       STATUS    ACTIVE  PORTS                    ROOT
pinpoint-leads  e9297fec      native     running   3/3     postgres:5432, web:3004  /Users/dev/code/pinpoint-leads
winnow-post     74376561      default    stopped   0/1     -                        /Users/dev/code/winnow-post
tertia-club     d7bfeefc      native     running   2/2     worker:8004, ui:3003     /Users/dev/code/tertia-club
```

Add `--health` to probe HTTP endpoints for real-time health checks:
```bash
rig ps --health
```

### Targeted Teardown & Garbage Collection

Stop projects from anywhere on your machine, even outside their checkout directory:

```bash
# Stop by project name slug
rig down pinpoint-leads

# Stop by specific instance ID
rig down e9297fec

# Stop ALL running instances across the entire machine
rig down --all

# Clean up stale instance state files
rig prune

# Force-kill lingering orphaned processes and prune
rig prune --force
```

---

## Zero-Race Socket Inheritance (`type: "fd"`)

When a service specifies `type: "fd"`, `rig` eliminates the classic time-of-check to time-of-use (TOCTOU) port race:

```
┌─────────┐      1. socket(AF_INET, SOCK_STREAM)
│   rig   │ ──── 2. bind("127.0.0.1", 0)  ───► OS Kernel assigns port
│         │ ──── 3. listen(128)
└────┬────┘
     │           4. subprocess.Popen(..., pass_fds=[fd])
     ▼
┌─────────┐
│ Uvicorn │ ──── 5. uvicorn.run(..., fd=sock.fileno())
└─────────┘      (Socket is NEVER closed between allocation and server startup)
```

### Python / Uvicorn Example

```python
import argparse
import socket
import uvicorn

parser = argparse.ArgumentParser()
parser.add_argument("--fd", type=int, default=None)
args, _ = parser.parse_known_args()

if args.fd is not None:
    sock = socket.fromfd(args.fd, socket.AF_INET, socket.SOCK_STREAM)
    uvicorn.run("main:app", fd=sock.fileno())
else:
    uvicorn.run("main:app", host="127.0.0.1", port=8000)
```

---

## Multi-Stack Modes (`native` vs `container`)

Define base infrastructure and mode overlays in a single `rig.json`:

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
        }
      }
    }
  }
}
```

### Collision-Safe Mode Switching

`rig` prevents conflicting processes from running simultaneously:

```bash
# Blocked with exit code 3 (E_MODE_CONFLICT) to prevent colliding ports/services:
rig up --mode container

# Cleanly tears down native services first and launches container mode:
rig up --mode container --switch
```

---

## AI Agent & Automation Protocol

`rig` is built for autonomous execution by AI agents (Claude Code, AntiGravity, Codex, Cursor) and CI/CD pipelines:

### Why AI Coding Assistants Use `rig`

1. **Deterministic Process Control**: AI agents frequently lose control of background tasks or suffer port collisions when re-running test servers. `rig` manages the full process lifecycle with PID/PGID process groups.
2. **Predictable JSON Responses**: Agents never have to scrape ANSI text or parse unpredictable terminal formatting.
3. **Actionable Resolution Hints**: When an error occurs, `rig` returns an exact diagnostic hint for automated recovery.

### Universal `--json` Output Envelope

Every CLI command supports `--json` and emits a typed, deterministic envelope:

```json
{
  "schema": "rig.status/1",
  "ok": true,
  "data": {
    "project": "pinpoint-leads",
    "instance": "pinpoint-leads-e9297fec",
    "mode": "native",
    "generation": 1,
    "services": {
      "backend": {
        "running": true,
        "type": "fd",
        "port": 8003,
        "url": "http://127.0.0.1:8003",
        "pid": 59608
      }
    }
  }
}
```

Errors provide actionable resolution hints:
```json
{
  "schema": "rig.error/1",
  "ok": false,
  "error": {
    "code": "E_MODE_CONFLICT",
    "message": "Instance is running in mode 'native'; cannot start mode 'container'",
    "hint": "Pass --switch to stop the active mode first, or run 'rig down' before starting a new mode."
  }
}
```

### Deterministic Exit Codes

| Code | Constant | Meaning |
|---|---|---|
| `0` | `EXIT_OK` | Success. |
| `1` | `EXIT_OP_FAILED` | Service failure, healthcheck timeout, or teardown error. |
| `2` | `EXIT_USAGE` | Invalid CLI arguments or schema validation error. |
| `3` | `EXIT_MUTEX_CONFLICT` | Instance lock busy (`checkout.lock`) or unswitched mode conflict. |
| `4` | `EXIT_NOT_FOUND` | Target project, service, or instance not found. |
| `5` | `EXIT_REFUSED` | Destructive action refused without confirmation. |
| `6` | `EXIT_EXTERNAL_TOOL` | Missing system binary (`docker`, `compose`). |
| `130` | `EXIT_INTERRUPTED` | Interrupted by signal (`SIGINT`, `SIGTERM`). |

---

## CLI Command Reference

| Command | Arguments | Description |
|---|---|---|
| `rig init` | `[--dry-run] [--force] [--up]` | Scans repository and generates a validated `rig.json`. |
| `rig up` | `[--mode MODE] [--scope SCOPE] [--switch]` | Starts services in dependency order with healthchecks. |
| `rig down` | `[target] [--all] [--scope SCOPE]` | Gracefully stops services (`SIGTERM` ➜ `SIGKILL`). |
| `rig status` | `[--json]` | Shows tabular or JSON status of services in the current checkout. |
| `rig ps` | `[--health] [-w, --wide] [--json]` | Lists all active and stopped `rig` projects machine-wide. |
| `rig logs` | `[service] [-n TAIL] [--mode MODE]` | Tails service logs from `.local-run/logs/`. |
| `rig check` | `[--mode MODE]` | Validates manifests, working directories, and binary execution. |
| `rig prune` | `[--force] [--json]` | Reclaims stale or orphaned instance metadata across the machine. |
| `rig schema` | `[--json]` | Prints the formal JSON Schema for `rig.json`. |
| `rig -v, --version` | | Displays current installed version (`rig 1.0.0`). |

---

## Configuration Reference (`rig.json`)

To inspect or validate the JSON Schema directly:
```bash
rig schema
```

| Property | Type | Description |
|---|---|---|
| `project` | `string` | **Required.** Slug identifier for instance isolation and Compose project naming. |
| `default_mode` | `string` | Mode to boot when `--mode` is omitted (defaults to `native`). |
| `services` | `object` | Base services active across all modes. |
| `modes` | `object` | Named mode configurations (`native`, `container`, etc.). |
| `type` | `"fd" \| "port" \| "compose"` | **Required.** Port allocation and execution strategy. |
| `command` | `string \| string[]` | Command line to execute. Supports `{fd}`, `{port}`, `{<service>_port}`. |
| `cwd` | `string` | Working directory relative to repository root (defaults to `.`). |
| `health` | `string` | HTTP endpoint path to poll for HTTP 200 OK (e.g. `/healthz`). |
| `health_tcp` | `integer` | TCP port to poll for socket connection (ideal for PostgreSQL/Redis). |
| `depends_on` | `string[]` | Upstream services that must pass healthchecks before boot. |
| `compose_file` | `string` | Relative path to Docker Compose file (for `type: "compose"`). |
| `compose_service` | `string` | Service name within the Docker Compose file. |

---

## Zero Runtime Dependencies

`rig` is committed to **zero third-party runtime dependencies**. It relies exclusively on the Python standard library (`socket`, `subprocess`, `os`, `signal`, `json`, `fcntl`, `shlex`, `dataclasses`, `pathlib`).

- **No background daemons** (`systemd`, `dockerd`, `supervisord`) required to orchestrate native processes.
- **No Node.js or Ruby runtimes** required.
- **Instantaneous startup** with file-backed atomic state.

---

## Direct Drop-in Usage

Because `rig` has zero external dependencies, you can also drop the orchestrator entry point directly into any repository without installing it:

```bash
curl -fsSL https://raw.githubusercontent.com/evgesha9400/rig/main/src/rig/cli.py -o scripts/rig.py
python3 scripts/rig.py up
```

---

## Symmetrical `Makefile` Integration

Drop these targets into your root `Makefile` for zero-friction developer ergonomics:

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
	@$(RIG) logs -n 100
```

---

## Links & Ecosystem

- **PyPI Package**: [https://pypi.org/project/rig-cli/](https://pypi.org/project/rig-cli/)
- **GitHub Repository**: [https://github.com/evgesha9400/rig](https://github.com/evgesha9400/rig)
- **JSON Schema**: [https://raw.githubusercontent.com/evgesha9400/rig/main/rig.schema.json](https://raw.githubusercontent.com/evgesha9400/rig/main/rig.schema.json)
- **Issue Tracker**: [https://github.com/evgesha9400/rig/issues](https://github.com/evgesha9400/rig/issues)
- **Releases & Changelog**: [https://github.com/evgesha9400/rig/releases](https://github.com/evgesha9400/rig/releases)

---

## License

[MIT](https://github.com/evgesha9400/rig/blob/main/LICENSE) © 2026 Evgeny Aleshin
