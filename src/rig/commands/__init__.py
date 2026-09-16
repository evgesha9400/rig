"""Command handlers for the rig CLI."""

from rig.commands.check import cmd_check
from rig.commands.down import cmd_down
from rig.commands.init import cmd_init
from rig.commands.logs import cmd_logs
from rig.commands.prune import cmd_prune
from rig.commands.ps import cmd_ps
from rig.commands.status import cmd_status
from rig.commands.up import cmd_up
from rig.manifest.schema import cmd_schema

__all__ = [
    "cmd_check",
    "cmd_down",
    "cmd_init",
    "cmd_logs",
    "cmd_prune",
    "cmd_ps",
    "cmd_schema",
    "cmd_status",
    "cmd_up",
]
