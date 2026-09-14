"""Generic local stack orchestrator and command-line entry point."""

from __future__ import annotations

import importlib
import sys
from collections.abc import Sequence
from types import ModuleType
from typing import Any

from rig.commands.dispatch import _dispatch_command, _handle_exception, _prepare_args
from rig.core import constants
from rig.core.errors import RigError
from rig.parser import build_parser

_SUBMODULES = (
    "rig.core.constants",
    "rig.core.errors",
    "rig.core.identity",
    "rig.core.locks",
    "rig.core.state",
    "rig.core.env",
    "rig.net.ports",
    "rig.net.probe",
    "rig.net.health",
    "rig.proc.process",
    "rig.proc.teardown",
    "rig.proc.spawn",
    "rig.proc.record",
    "rig.compose.client",
    "rig.compose.context",
    "rig.compose.docker",
    "rig.compose.discovery",
    "rig.compose.supervisor",
    "rig.compose.starter",
    "rig.compose.stopper",
    "rig.manifest.models",
    "rig.manifest.parser",
    "rig.manifest.detector",
    "rig.manifest.inspect",
    "rig.manifest.loader",
    "rig.manifest.schema",
    "rig.commands.common",
    "rig.commands.dispatch",
    "rig.commands.up",
    "rig.commands.up.context",
    "rig.commands.up.service",
    "rig.commands.up.runner",
    "rig.commands.up.rollback",
    "rig.commands.up.relink",
    "rig.commands.up.loop",
    "rig.commands.down",
    "rig.commands.down.runner",
    "rig.commands.status",
    "rig.commands.ps",
    "rig.commands.prune",
    "rig.commands.check",
    "rig.commands.init",
    "rig.parser",
)

for _mod in _SUBMODULES:
    _m = importlib.import_module(_mod)
    for _k, _v in _m.__dict__.items():
        if not _k.startswith("__"):
            globals()[_k] = _v


class _CliModule(ModuleType):
    def __setattr__(self, name: str, value: Any) -> None:
        super().__setattr__(name, value)
        for mod_name, mod in list(sys.modules.items()):
            if mod_name.startswith("rig.") and mod is not self and name in mod.__dict__:
                setattr(mod, name, value)

    def __delattr__(self, name: str) -> None:
        super().__delattr__(name)
        for mod_name, mod in list(sys.modules.items()):
            if mod_name.startswith("rig.") and mod is not self and name in mod.__dict__:
                mod.__dict__.pop(name, None)


sys.modules[__name__].__class__ = _CliModule


def main(argv: Sequence[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in raw_argv
    if as_json:
        raw_argv = [a for a in raw_argv if a != "--json"]
    parser = build_parser(as_json=as_json)
    try:
        args = parser.parse_args(raw_argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else constants.EXIT_USAGE

    _prepare_args(args, as_json)
    try:
        return _dispatch_command(args.command, args)
    except (RigError, TimeoutError, KeyboardInterrupt, OSError, RuntimeError, ValueError) as exc:
        return _handle_exception(exc, args.command, as_json)


if __name__ == "__main__":
    sys.exit(main())
