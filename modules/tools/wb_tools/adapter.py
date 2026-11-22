"""Runs an absorbed orthrus-tools tool as a Workbench command (D18).

Every orthrus tool already follows the contract Workbench adopted: one JSON object with ``--json`` and exit
codes 0/1/2/3 (ok, finding, usage, environment). So one adapter serves them all: ``status`` becomes the
outcome, ``result`` becomes ``data``, messages become log lines. Absolute host paths in the arguments are
translated to the container's mounts, so a caller passes the paths it knows.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sys
from pathlib import Path

from wb_core.module import Context, EnvironmentProblem, ParamError

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

_WINDOWS = re.compile(r"^[A-Za-z]:[\\/]")

# The orthrus tool behind each command, where the command's own name differs from the tool's.
TOOL_OF = {"jar.audit": "jar-audit", "jar.survey": "mod-survey", "source.format": "java-format"}


def _translate(ctx: Context, argv: list[str]) -> list[str]:
    """Every absolute path in the arguments is tried against the roots, on any host OS. A Windows path under no
    root cannot exist in the container, so it is refused; a POSIX path under no root may be a container path
    (/opt/jdk/26), so it passes through unchanged."""
    from wb_core.paths import PathError, PathMap
    from wb_core.settings import Root
    roots = [Root(**r) for r in ctx.spec.get("roots", [])]
    if not roots:
        return list(argv)
    mapping = PathMap(roots)
    out = []
    for arg in argv:
        prefix, eq, value = arg.partition("=") if arg.startswith("--") and "=" in arg else ("", "", arg)
        if _WINDOWS.match(value) or value.startswith("/"):
            try:
                value = mapping.resolve(value).container
            except PathError as error:
                if _WINDOWS.match(value):
                    raise ParamError(str(error)) from None
        out.append(f"{prefix}{eq}{value}" if eq else value)
    return out


def _env_value(ctx: Context, value: str) -> str:
    try:
        return _translate(ctx, [value])[0]
    except ParamError:
        return value


def run_tool(ctx: Context, argv: list, cwd=None):
    from orthrus_tools import registry
    name = TOOL_OF.get(ctx.command, ctx.command.split(".", 1)[1])
    spec = next((t for t in registry.TOOLS if t.name == name and t.kind == "command"), None)
    if spec is None:
        raise EnvironmentProblem(f"no orthrus tool {name}")
    arguments = _translate(ctx, [str(a) for a in argv])
    if "--json" not in arguments:
        arguments.append("--json")
    # The module's `tool_env`: environment a tool reads (a scrub's private terms file kept in the machine's config
    # folder, never in a repository). A path under a root is translated like an argument; any other value, such as
    # the config folder's own path in the container (/etc/wb/...), is passed as given.
    env = {str(k): _env_value(ctx, str(v)) for k, v in (ctx.config.get("tool_env") or {}).items()}
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    buffer = io.StringIO()
    previous = os.getcwd()
    try:
        if cwd:
            os.chdir(cwd)
        with contextlib.redirect_stdout(buffer):
            try:
                code = spec.main(arguments)
            except SystemExit as exit_:
                code = int(exit_.code or 0)
    finally:
        os.chdir(previous)
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    text = buffer.getvalue().strip()
    document = None
    if text:
        try:
            document = json.loads(text[text.index("{"):])
        except ValueError:
            ctx.log.info(text[:4000])
    if document is None:
        if code == 0 and "--help" in arguments:
            return {"help": text}
        raise EnvironmentProblem(f"{name} printed no JSON result (exit {code})")
    for message in document.get("messages") or []:
        level = {"warn": "warning", "error": "error"}.get(message.get("level"), "info")
        getattr(ctx.log, level)(message.get("text", ""))
    status = document.get("status")
    texts = [m.get("text") for m in document.get("messages") or [] if m.get("level") in ("warn", "error")]
    summary = "; ".join(t for t in texts if t)[:300] or f"{name} {document.get('command') or ''}".strip() + f": {status}"
    if status == "finding":
        ctx.finding(summary)
    elif status == "usage":
        raise ParamError(summary)
    elif status == "environment":
        raise EnvironmentProblem(summary)
    else:
        ctx.summary(summary)
    result = document.get("result")
    return {"tool": name, "command": document.get("command"), "result": result if result is not None else {}}
