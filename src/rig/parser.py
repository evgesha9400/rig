"""CLI argument parser setup and error handling for rig."""

from __future__ import annotations

import argparse
import sys
from typing import Any

from rig.core.constants import EXIT_USAGE
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
    p_up = sub.add_parser("up", help="start services")
    p_up.add_argument("--scope", default="full")
    p_up.add_argument("--mode", default=None)
    p_up.add_argument("--switch", action="store_true")

    p_down = sub.add_parser("down", help="stop services")
    p_down.add_argument("target", nargs="?", default=None)
    p_down.add_argument("--all", action="store_true", dest="all_instances")
    p_down.add_argument("--scope", default="full")


def _add_admin_parsers(sub: Any) -> None:
    sub.add_parser("status", help="status")
    p_ps = sub.add_parser("ps", aliases=["ls", "list"], help="ps")
    p_ps.add_argument("--health", action="store_true")
    sub.add_parser("prune", help="prune").add_argument("--force", action="store_true")
    sub.add_parser("check", help="check").add_argument("--mode", default=None)
    p_init = sub.add_parser("init", help="init")
    p_init.add_argument("--dry-run", action="store_true")
    p_init.add_argument("--force", action="store_true")
    p_init.add_argument("--up", action="store_true")
    sub.add_parser("schema", help="schema")


def build_parser(as_json: bool = False) -> argparse.ArgumentParser:
    """Construct the command line parser for rig."""
    parser = RigArgumentParser(prog="rig", as_json=as_json)
    parser.add_argument("--root", default=None)
    parser.add_argument("--manifest", default=None)
    parser.add_argument("--json", action="store_true")
    sub = parser.add_subparsers(
        dest="command",
        required=True,
        parser_class=lambda **kw: RigArgumentParser(as_json=as_json, **kw),
    )
    _add_service_parsers(sub)
    _add_admin_parsers(sub)
    return parser
