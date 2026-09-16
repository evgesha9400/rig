"""Command dispatching and top-level CLI error handling."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rig import commands
from rig.core import constants
from rig.core.errors import RigError, format_human_error, print_json_error
from rig.core.identity import find_default_manifest, find_project_root
from rig.core.terminal import get_theme


def _dispatch_command(cmd: str, args: argparse.Namespace) -> int:
    root, mf, j = args.root_dir, args.manifest_file, args.as_json
    dispatch = {
        "up": lambda: commands.cmd_up(
            root, mf, scope=args.scope, mode=args.mode, switch=args.switch, as_json=j
        ),
        "down": lambda: commands.cmd_down(
            root,
            mf,
            scope=args.scope,
            target=args.target,
            all_instances=args.all_instances,
            as_json=j,
        ),
        "status": lambda: commands.cmd_status(root, mf, as_json=j),
        "ps": lambda: commands.cmd_ps(
            health=args.health, as_json=j, wide=getattr(args, "wide", False)
        ),
        "ls": lambda: commands.cmd_ps(
            health=args.health, as_json=j, wide=getattr(args, "wide", False)
        ),
        "list": lambda: commands.cmd_ps(
            health=args.health, as_json=j, wide=getattr(args, "wide", False)
        ),
        "prune": lambda: commands.cmd_prune(force=args.force, as_json=j),
        "check": lambda: commands.cmd_check(root, mf, mode=args.mode, as_json=j),
        "init": lambda: commands.cmd_init(
            root, dry_run=args.dry_run, force=args.force, up=args.up, as_json=j
        ),
        "schema": lambda: commands.cmd_schema(as_json=j),
        "logs": lambda: commands.cmd_logs(
            root, mf, service=args.service, tail=args.tail, mode=args.mode, as_json=j
        ),
    }
    action = dispatch.get(cmd)
    return action() if action else constants.EXIT_OK


def _print_rig_error(exc: RigError) -> None:
    th = get_theme()
    print(format_human_error(exc, th), end="", file=sys.stderr)


def _handle_exception(exc: Exception, cmd: str, as_json: bool) -> int:
    if isinstance(exc, RigError):
        if as_json:
            print_json_error(exc, command=cmd)
        else:
            _print_rig_error(exc)
        return exc.exit_code
    if isinstance(exc, TimeoutError):
        err = RigError(str(exc), code="E_LOCK_TIMEOUT", exit_code=constants.EXIT_MUTEX_CONFLICT)
        return _handle_exception(err, cmd, as_json)
    if isinstance(exc, KeyboardInterrupt):
        err = RigError(
            "operation cancelled by user",
            code="E_INTERRUPTED",
            exit_code=constants.EXIT_INTERRUPTED,
        )
        return _handle_exception(err, cmd, as_json)
    err = RigError(
        f"unexpected error: {exc}", code="E_INTERNAL", exit_code=constants.EXIT_OP_FAILED
    )
    return _handle_exception(err, cmd, as_json)


def _prepare_args(args: argparse.Namespace, as_json: bool) -> None:
    args.as_json = as_json
    r_arg = getattr(args, "root", None)
    args.root_dir = Path(r_arg).resolve() if r_arg else find_project_root()
    m_arg = getattr(args, "manifest", None)
    args.manifest_file = Path(m_arg).resolve() if m_arg else find_default_manifest(args.root_dir)
