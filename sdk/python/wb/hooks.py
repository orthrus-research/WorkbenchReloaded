"""The git hooks, answered on the host in this process: no daemon, no queue, no job.

``pre-commit`` scrubs the staged changes, ``commit-msg`` the message, and ``pre-push`` the commits and ref names being
pushed and then asks the push gate: each pushed commit of a registered project must have passed its own required
checks, which for a project.toml commit is the gate record of its tree in the Store (for this release a tree without
one is still judged by CI's run summaries, with a note), and for an actions.toml commit the run summaries. A
repository that is not a registered project, or one whose ``gate`` is false, is only scrubbed. The gate reads files
and git only, never the daemon or the GPU lease, so a running game cannot hold a push up. The scrub reads the
machine's rules file; without one it says that it ran on its built-in rules only.

Each verdict is one line in the machine's hook log. ``git push --no-verify`` still skips everything."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from .workspace import TOOLS, Workspace, WorkspaceError, git

NAMES = ("pre-commit", "commit-msg", "pre-push")
REPOSITORY_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_PREFIX",
                  "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES")
MARKER = "# Written by Workbench (wb setup)"


def _scrub():
    if str(TOOLS) not in sys.path:
        sys.path.insert(0, str(TOOLS))
    from orthrus_tools import scrub
    return scrub


def _say(text: str):
    print(f"Workbench: {text}", file=sys.stderr)


def _log(workspace: Workspace | None, root: Path, name: str, verdict: str):
    try:
        folder = Path(workspace.config.folder) if workspace else Path.home() / ".config" / "wb"
        (folder / "run").mkdir(parents=True, exist_ok=True)
        with open(folder / "run" / "hooks.log", "a", encoding="utf-8") as log:
            log.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {root} {name} {verdict}\n")
    except OSError:
        pass  # a log line never decides a commit


def rules_note(scrub) -> str | None:
    named = os.environ.get("ORTHRUS_SCRUB_RULES_FILE")
    path = Path(named) if named else scrub.RULES_FILE
    return None if path.is_file() else f"no scrub rules file at {path}: the scrub ran on its built-in rules only"


def run(name: str, args: list[str], stdin: str | None = None) -> int:
    scrub = _scrub()
    root = Path(git(Path.cwd(), "rev-parse", "--show-toplevel"))
    try:
        rules = scrub.Rules.load(root)
    except scrub.ToolError as error:
        _say(f"the scrub cannot load its rules: {error.message if hasattr(error, 'message') else error}")
        return 1
    note = rules_note(scrub)
    if note:
        _say(note)
    if name == "commit-msg":
        if not args:
            _say("commit-msg needs the message file")
            return 1
        text = Path(args[0]).read_text(encoding="utf-8", errors="replace")
        body = "\n".join(line for line in text.splitlines() if not line.startswith("#"))
        findings = list(scrub.check_text(body, "message", rules))
        return _verdict(None, root, name, findings, [])
    if name == "pre-commit":
        return _verdict(None, root, name, scrub.check_staged(root, rules), [])
    lines = [line for line in (stdin if stdin is not None else sys.stdin.read()).splitlines() if line.strip()]
    findings = scrub.pre_push(root, "\n".join(lines), rules)
    for line in lines:
        fields = line.split()
        if len(fields) == 4:
            for ref in (fields[0], fields[2]):
                findings += list(scrub.check_line(ref, f"ref name {ref}", rules))
    refused: list[dict] = []
    workspace = None
    # git runs a hook with GIT_DIR (and others) naming the pushing worktree's own repository. The gate reads other
    # folders (the registered checkout, pinned siblings), each by its own path, so these must not redirect it.
    for name_ in REPOSITORY_ENV:
        os.environ.pop(name_, None)
    try:
        workspace = Workspace()
        project = workspace.find(root)
        if project is not None:
            answer = workspace.gate(project, lines)
            refused = answer["refused"]
            for note in answer.get("notes") or []:
                _say(note)
            if answer["gated"] and not refused and answer["required"]:
                _say(f"{project.name}: every pushed commit has passed {', '.join(answer['required'])}")
    except (WorkspaceError, OSError) as error:
        _say(f"the push gate could not answer: {error}. Fix that, or push with --no-verify to skip the gate.")
        _log(workspace, root, name, "error")
        return 1
    for item in refused:
        _say(f"{item['ref']} {item['commit'][:12]}: {item['reason']}")
    if any("moves only by landing" not in item["reason"] for item in refused):  # a landing refusal names its own fix
        _say("run `wb check` in the worktree of that commit, then push again")
    return _verdict(workspace, root, name, findings, refused)


def _verdict(workspace, root: Path, name: str, findings: list, refused: list) -> int:
    for finding in findings:
        _say(f"scrub: {finding}")
    if findings:
        _say(f"{len(findings)} scrub finding(s). Remove them, or mark a line that must keep its text with "
             "'scrub: allow'.")
    verdict = "refused" if (findings or refused) else "ok"
    # Each refused ref with its commit and the gate's reason, so a refusal can be explained afterwards. A scrub
    # finding is counted only: its text may quote what the scrub caught, which a log must not keep.
    why = "".join(f" | {item['ref']} {str(item['commit'])[:12]}: {' '.join(str(item['reason']).split())[:240]}"
                  for item in refused)
    _log(workspace, root, name, f"{verdict} findings={len(findings)} refused={len(refused)}{why}")
    return 1 if (findings or refused) else 0


STUB = """#!/bin/sh
{marker}: the scrub{gate}, answered on the host.
wb="$(git config --get wb.cli)"
if [ -z "$wb" ]; then
  echo "Workbench: git config wb.cli is not set, so this hook cannot run; run wb setup (or commit with --no-verify)" >&2
  exit 1
fi
exec $wb hook {name} "$@"
"""


def stub(name: str) -> str:
    return STUB.format(marker=MARKER, gate=", and the push gate" if name == "pre-push" else "", name=name)
