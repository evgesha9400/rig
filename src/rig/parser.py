"""CLI argument parser setup and error handling for rig."""

from __future__ import annotations

import argparse
import sys
from typing import Any

from rig.core.constants import EXIT_USAGE, __version__
from rig.core.errors import RigError, print_json_error


class RigArgumentParser(argparse.ArgumentParser):
    """ArgumentParser that outputs structured JSON errors when requested."""

    def __init__(self, *args: Any, as_json: bool = False, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.as_json = as_json

    def error(self, message: str) -> None:
        if self.as_json:
            err = RigError(message, code="E_USAGE", exit_code=EXIT_USAGE)
            print_json_error(err, command="cli")
            sys.exit(EXIT_USAGE)
        super().error(message)


def _add_service_parsers(sub: Any) -> None:
    p_up = sub.add_parser("up", help="Start services defined in manifest")
    p_up.add_argument("--scope", metavar="<name>", default="full", help="Scope to start [full]")
    p_up.add_argument("--mode", metavar="<name>", default=None, help="Environment mode overlay")
    p_up.add_argument("--switch", action="store_true", help="Stop conflicting modes before startup")

    p_down = sub.add_parser("down", help="Stop active services or instances")
    p_down.add_argument(
        "target", nargs="?", default=None, metavar="<target>", help="Target to stop"
    )
    p_down.add_argument(
        "--all", action="store_true", dest="all_instances", help="Stop all machine instances"
    )
    p_down.add_argument("--scope", metavar="<name>", default="full", help="Scope to stop [full]")

    p_logs = sub.add_parser("logs", help="Tail log output for a service")
    p_logs.add_argument("service", nargs="?", default=None, metavar="<svc>", help="Service name")
    p_logs.add_argument(
        "-n", "--tail", type=int, default=50, metavar="<n>", help="Lines to show [50]"
    )
    p_logs.add_argument("--mode", metavar="<name>", default=None, help="Mode overlay")


def _add_inspect_parsers(sub: Any) -> None:
    sub.add_parser("status", help="Show service state, ports, and health for current checkout")
    p_ps = sub.add_parser(
        "ps", aliases=["ls", "list"], help="List active and recorded instances across machine"
    )
    p_ps.add_argument("--health", action="store_true", help="Probe HTTP health check endpoints")
    p_ps.add_argument("-w", "--wide", action="store_true", help="Show full IDs and paths")
    p_chk = sub.add_parser("check", help="Validate manifest syntax, binaries, and prerequisites")
    p_chk.add_argument("--mode", metavar="<name>", default=None, help="Mode overlay to validate")
    sub.add_parser("schema", help="Print JSON Schema for rig.json validation")


def _add_manage_parsers(sub: Any) -> None:
    p_prune = sub.add_parser("prune", help="Remove stale registrations for deleted checkouts")
    p_prune.add_argument(
        "--force", action="store_true", help="Force removal of lingering processes"
    )
    p_init = sub.add_parser("init", help="Auto-detect stack and scaffold a new rig.json")
    p_init.add_argument("--dry-run", action="store_true", help="Print without writing to disk")
    p_init.add_argument("--force", action="store_true", help="Overwrite existing rig.json")
    p_init.add_argument("--up", action="store_true", help="Start services after initialization")


def build_parser(as_json: bool = False) -> argparse.ArgumentParser:
    """Construct the command line parser for rig."""
    parser = RigArgumentParser(
        prog="rig",
        description="Local dev environment and multi-service process runner.",
        epilog="Use 'rig <command> --help' for details on a specific command.",
        as_json=as_json,
    )
    parser.add_argument(
        "-v", "--version", action="version", version=f"%(prog)s {__version__}", help="Show version"
    )
    parser.add_argument(
        "--root", metavar="<path>", default=None, help="Root directory [current dir]"
    )
    parser.add_argument(
        "--manifest", metavar="<path>", default=None, help="Manifest file [rig.json]"
    )
    parser.add_argument("--json", action="store_true", help="Format output as JSON envelope")
    sub = parser.add_subparsers(
        dest="command",
        title="commands",
        metavar="<command>",
        required=True,
        parser_class=lambda **kw: RigArgumentParser(as_json=as_json, **kw),
    )
    _add_service_parsers(sub)
    _add_inspect_parsers(sub)
    _add_manage_parsers(sub)
    return parser
