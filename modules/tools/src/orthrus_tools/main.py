"""``orthrus-tools``: one command for every orthrus tool.

    orthrus-tools <tool> [arguments]      # run a tool, such as: orthrus-tools pins verify

In Workbench these tools run as the ``tools`` module's commands (``wb run tools.pins argv=verify``).

Every tool exits 0 ok, 1 finding, 2 usage, 3 environment, and takes ``--json`` for one JSON
result object. The standalone commands (``orthrus-pins`` and the rest) behave the same.
"""

from __future__ import annotations

import sys
from collections.abc import Callable

from . import __version__
from .cli import USAGE, Output
from .registry import BY_NAME, COMMANDS

BUILTINS: dict[str, Callable[[list[str]], int]] = {}


def usage() -> str:
    width = max(len(tool.name) for tool in COMMANDS) + 2
    lines = [(__doc__ or "").strip(), "", "Tools:"]
    lines += [f"  {tool.name:{width}}{tool.summary}" for tool in COMMANDS]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    if not args or args[0] in ("-h", "--help", "help"):
        print(usage())
        return 0 if args else 2
    if args[0] == "--version":
        print(f"orthrus-tools {__version__}")
        return 0
    name, rest = args[0], args[1:]
    if name in BUILTINS:
        return BUILTINS[name](rest)
    tool = BY_NAME.get(name)
    if tool is None or tool.kind != "command":
        known = ", ".join([*BUILTINS, *(tool.name for tool in COMMANDS)])
        hint = f"; {name} is a library, not a command" if tool else ""
        message = f"orthrus-tools: unknown command {name!r}{hint}. Commands: {known}"
        out = Output("orthrus-tools", "--json" in rest, sys.stdout, sys.stderr)
        out.error(message)
        return out.finish(USAGE)
    return tool.main(rest)


if __name__ == "__main__":
    sys.exit(main())
