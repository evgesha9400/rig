# Improvement Tasks

This document tracks planned improvements and architectural enhancements for `rig`.

---

## Task 1: Process Log Inspection (`rig logs`)

### Objective
Design and implement a native log inspection and streaming mechanism allowing developers and agents to view and follow logs for each running process in a `rig` instance.

### Current State
- Native background services started via `spawn_fd_service` or `spawn_port_service` write their standard output and standard error to `~/.local/state/rig/instances/<instance-id>/logs/<service-name>.log`.
- Docker Compose services run via Docker, storing logs inside the container runtime.
- There is currently no CLI command in `rig` to inspect or stream these logs; developers must inspect the filesystem directly or run external shell commands (`tail -f`).

### Technical Requirements

1. **CLI Command & Arguments (`rig logs`)**:
   - `rig logs [service]`: Target a specific service or all active services if omitted.
   - `-f, --follow`: Follow/stream log output in real-time until interrupted (`Ctrl+C`).
   - `-n, --tail <lines>`: Output the specified number of lines from the end of the log (default: 50 lines).
   - `--timestamps, -t`: Prepend or display timestamps for log lines.
   - `--mode <mode>`: Scope service resolution to a specific mode overlay.
   - `--json`: Emit structured JSON log records or file descriptor/path metadata when called with the `--json` envelope.

2. **Native Process Log Streaming**:
   - Stream from `~/.local/state/rig/instances/<instance-id>/logs/<service>.log`.
   - Implement non-blocking polling or file descriptor seeking (`seek()` / `sleep()` polling loop) with standard library only.
   - When viewing multiple services concurrently, multiplex output by prefixing lines with distinct service tags (e.g., `[api]`, `[worker]`) and distinct terminal colors.

3. **Container Service Log Streaming**:
   - For `type: "compose"` services, stream logs via the Docker client (`docker compose logs [-f] [--tail N] <service>`).

4. **Safety & Zero-Dependency Constraints**:
   - Rely strictly on Python standard library (`pathlib`, `time`, `sys`, `os`, `signal`, `select`).
   - Clean signal handling: Gracefully exit on `SIGINT` / `SIGTERM` without leaving dangling file descriptors or subprocesses.
   - Ensure file size limits or line length limits adhere to codebase standards (<= 150 lines per module).

### Implementation Scope
- [ ] Add `logs` subparser in `src/rig/parser.py`.
- [ ] Implement log retrieval, tailing, and multiplexing in `src/rig/commands/logs.py`.
- [ ] Connect dispatch and CLI entry points in `src/rig/commands/dispatch.py` and `src/rig/cli.py`.
- [ ] Add unit and integration tests under `tests/commands/test_logs.py`.
- [ ] Update `README.md` documentation with `rig logs` examples.

---

## Task 2: Formatted Terminal Representation for Status and Command Output [Completed]

### Objective
Design and implement a visually clear, formatted terminal interface for `rig status`, `rig ps`, and lifecycle command outputs (`rig up`, `rig down`, `rig check`) while strictly preserving standard library zero-dependency constraints and JSON envelope compatibility.

### Current State
- `rig status` prints unaligned plain text lines with status, PID, port, and health check state.
- `rig ps` prints a basic fixed-width table.
- Lifecycle commands (`rig up`, `rig down`) output raw step-by-step progress strings.
- Non-interactive and agent consumers rely on the `--json` envelope, which must remain unformatted and deterministic.

### Technical Requirements

1. **Terminal Formatting Architecture (`src/rig/core/terminal.py`)**:
   - Build a lightweight standard-library formatting helper using standard ANSI escape codes.
   - Implement strict environment detection:
     - Check `sys.stdout.isatty()`.
     - Respect `NO_COLOR` environment variable ([no-color.org](https://no-color.org/)).
     - Respect `TERM=dumb`.
     - Automatically downgrade to clean plain text without ANSI escape sequences when running in non-TTY or uncolored environments.

2. **Status Dashboard (`rig status`)**:
   - **Header Card**: Display project name, instance ID, active mode, generation, and lock state in a structured header block.
   - **Service Table**: Render a grid with box-drawing glyphs (`│`, `─`, `┌`, `┬`, `┐`, `└`, `┴`, `┘` with ASCII fallback) and dynamic column width calculation:
     - Columns: `SERVICE`, `STATUS`, `TYPE`, `PID / CONTAINER`, `URL / PORT`, `HEALTH`.
   - **Semantic Color Coding**:
     - Green: `running`, `healthy`
     - Red: `stopped`, `unhealthy`, `failed`
     - Yellow: `starting`, `degraded`, `partial`
     - Cyan / Bold: Localhost URLs and port bindings
     - Dim / Gray: Metadata, instance IDs, generation counters

3. **Machine-Wide Overview (`rig ps`)**:
   - Aligned tabular display with colored status badges (`RUNNING`, `PARTIAL`, `STOPPED`, `ORPHANED`).
   - Clear indicators for deleted/stale working directories.

4. **Lifecycle Command Output (`rig up`, `rig down`, `rig check`)**:
   - Consistent step glyphs (`✓` success, `✖` failure, `ℹ` info, `•` pending).
   - Healthcheck waiting feedback (transient step progress indicator).
   - Structured completion summary card listing active URLs and services.

5. **Machine & Automation Contract**:
   - The `--json` envelope format and exit codes (0–6, 130) must remain completely unaffected.
   - All ANSI styling must be isolated from structured data channels.
   - Abide by the 150-line file limit per module.

### Implementation Scope
- [x] Create ANSI color and box-drawing utilities in `src/rig/core/terminal.py`.
- [x] Refactor `_print_status` in `src/rig/commands/status.py` to use structured table formatting.
- [x] Refactor `_print_table` in `src/rig/commands/ps.py` with enhanced badges and alignment.
- [x] Upgrade lifecycle feedback in `src/rig/commands/up/runner.py` and `src/rig/commands/down/runner.py`.
- [x] Add tests verifying:
  - [x] ANSI code emission on TTY.
  - [x] Clean plain-text fallback on non-TTY / `NO_COLOR=1`.
  - [x] JSON output untouched by formatting changes (`--json`).
