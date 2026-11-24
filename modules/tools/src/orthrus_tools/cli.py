"""The command-line contract every orthrus tool follows.

Exit codes are fixed across tools:

    0  ok: the check passed or the command did what it was asked
    1  finding: the command ran and found a problem (a failed check, a missing pin)
    2  usage: bad arguments, or the project is not set up (no lock file, no baseline)
    3  environment: something the machine lacks (a JDK, git, numpy, network, procfs)

With ``--json`` a command prints exactly one JSON object on stdout and nothing else there, also
when it fails::

    {"schema": "orthrus.result.v1", "tool": "pins", "command": "verify", "exitCode": 1,
     "status": "finding", "result": {...}, "messages": [{"level": "error", "text": "..."}]}

``result`` holds the command's data (``null`` when it stopped early); a report with its own
schema carries it as ``result.schema``. Without ``--json`` the same messages print as text,
errors and warnings on stderr. Schema names change only with a new major version.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from typing import Any, NoReturn, TextIO

OK = 0
FINDING = 1
USAGE = 2
ENVIRONMENT = 3
STATUS = {OK: "ok", FINDING: "finding", USAGE: "usage", ENVIRONMENT: "environment"}
EXIT_CODES = {
    "0": "ok: passed, or did what was asked",
    "1": "finding: ran and found a problem",
    "2": "usage: bad arguments or the project is not set up",
    "3": "environment: the machine lacks something the tool needs",
}
RESULT_SCHEMA = "orthrus.result.v1"


class ToolError(Exception):
    """Stops a command with an exit code and a message; ``run`` turns it into output."""

    def __init__(self, code: int, message: str, result: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.result = result


class Parser(argparse.ArgumentParser):
    """An ArgumentParser whose errors become a usage ToolError, so ``--json`` still applies."""

    def error(self, message: str) -> NoReturn:
        raise ToolError(USAGE, f"{self.prog}: {message} (see {self.prog} --help)")


def parser(prog: str, description: str | None, **kwargs: Any) -> Parser:
    built = Parser(prog=prog, description=description, **kwargs)
    add_json_flag(built)
    return built


def add_json_flag(target: argparse.ArgumentParser) -> None:
    """``--json`` is accepted before or after a subcommand."""
    target.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="print one JSON result object on stdout (see docs/contract.md)",
    )


def subcommands(built: argparse.ArgumentParser) -> argparse._SubParsersAction[Parser]:
    commands = built.add_subparsers(dest="command", required=True, parser_class=Parser)
    original = commands.add_parser

    def add_parser(name: str, **kwargs: Any) -> Parser:
        sub = original(name, **kwargs)
        add_json_flag(sub)
        return sub

    commands.add_parser = add_parser  # type: ignore[method-assign]
    return commands


class Output:
    """Collects what a command reports; prints it as text or as one JSON object at the end."""

    def __init__(self, tool: str, json_mode: bool, stdout: TextIO, stderr: TextIO) -> None:
        self.tool = tool
        self.json_mode = json_mode
        self.stdout = stdout
        self.stderr = stderr
        self.command: str | None = None
        self.result: Any = None
        self.messages: list[dict[str, str]] = []

    def line(self, text: str = "") -> None:
        """Plain text output; dropped in JSON mode, where ``result`` carries the data."""
        if not self.json_mode:
            print(text, file=self.stdout)

    def note(self, text: str, level: str = "info") -> None:
        """A message kept in JSON mode too: info prints on stdout, warn and error on stderr."""
        self.messages.append({"level": level, "text": text})
        if not self.json_mode:
            print(text, file=self.stdout if level == "info" else self.stderr)

    def warn(self, text: str) -> None:
        self.note(text, "warn")

    def error(self, text: str) -> None:
        self.note(text, "error")

    def document(self, code: int) -> dict[str, Any]:
        """The JSON document of a command that ended with ``code``, as plain JSON values (what ``--json`` prints)."""
        document = {
            "schema": RESULT_SCHEMA,
            "tool": self.tool,
            "command": self.command,
            "exitCode": code,
            "status": STATUS.get(code, "error"),
            "result": self.result,
            "messages": self.messages,
        }
        loaded: dict[str, Any] = json.loads(json.dumps(document, default=str))
        return loaded

    def finish(self, code: int) -> int:
        if self.json_mode:
            print(json.dumps(self.document(code), indent=2), file=self.stdout)
        return code


def run(
    tool: str,
    build: Callable[[], argparse.ArgumentParser],
    command: Callable[[argparse.Namespace, Output], int],
    argv: list[str] | None,
) -> int:
    """Parse ``argv``, run ``command`` and apply the output contract. Returns the exit code."""
    arguments = sys.argv[1:] if argv is None else list(argv)
    out = Output(tool, "--json" in arguments, sys.stdout, sys.stderr)
    try:
        args = build().parse_args(arguments)
    except ToolError as error:
        out.error(error.message)
        return out.finish(error.code)
    except SystemExit as exit_:  # --help and --version exit through argparse
        return int(exit_.code or 0)
    out.json_mode = bool(getattr(args, "json", False))
    out.command = getattr(args, "command", None)
    try:
        code = command(args, out)
    except ToolError as error:
        if error.result is not None:
            out.result = error.result
        out.error(error.message)
        code = error.code
    except KeyboardInterrupt:
        out.error("interrupted")
        code = USAGE
    return out.finish(code)
