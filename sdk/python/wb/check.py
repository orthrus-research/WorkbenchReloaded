"""What ``wb check`` runs where: the container checks it was asked for go to ci.run, the host checks to ci.host-run.

A host check (``on = "host"``, or ``gpu = true``) never runs in the container and is never in the gate, so ``wb check``
with no names runs the gate as it always has. Named checks and workflows (``all`` names every check) are expanded
by the commit's own project.toml, with what they need, and split: the host checks among them run on the host, the
rest in the container. When none of them is a host check, the names go to ci.run as given.

A commit that predates its project's conversion (it carries actions.toml and no project.toml, while the project's
set-named branch carries project.toml) is answered before it runs: it predates the set, and this is its own setup
command, read from its own tree. Its checks still run from its actions.toml for the grace period; only a commit with
no checks file at all is not run.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path


def split(workspace, project, commit: str, names: list[str]) -> dict[str, list[str]]:
    """``{"container": [...], "host": [...]}`` for ``names`` at ``commit``. Without a jobs file to read, or with a
    name it does not know, everything goes to ci.run, which says what is wrong."""
    names = [str(name) for name in names]
    if not names:
        return {"container": [], "host": []}
    actions = workspace.engine()
    with tempfile.TemporaryDirectory(prefix="wb-check-") as scratch:
        try:
            config = workspace.config_at(project, commit, Path(scratch))
        except actions.ToolError:
            config = None
        if config is None:
            return {"container": names, "host": []}
        try:
            ordered = actions.resolve(config, names)
        except actions.ToolError:
            return {"container": names, "host": []}
        jobs = config.jobs
    host = [name for name in ordered if actions.on_host(jobs[name])]
    if not host:
        return {"container": names, "host": []}
    return {"container": [name for name in ordered if name not in host], "host": host}


def _env() -> dict:
    return {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}


def _show(repo: Path, commit: str, name: str) -> str | None:
    done = subprocess.run(["git", "show", f"{commit}:{name}"], cwd=repo, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=_env())
    return done.stdout if done.returncode == 0 else None


def _carries(repo: Path, commit: str, name: str) -> bool:
    return subprocess.run(["git", "cat-file", "-e", f"{commit}:{name}"], cwd=repo, capture_output=True,
                          env=_env()).returncode == 0


HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
SECTION = re.compile(r"build|setup|set up|install|getting started", re.IGNORECASE)
NOT_SETUP = re.compile(r"^(git\s+clone|cd\s|#|//|rem\s)", re.IGNORECASE)


def readme_setup(text: str) -> tuple[str, list[str]] | None:
    """The commands in the first code block of a README's build (or setup) section, without the clone and cd lines
    that only reach the checkout: ``(heading, commands)``, or None."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        heading = HEADING.match(line)
        if not heading or not SECTION.search(heading.group(2)):
            continue
        level, commands, fence = len(heading.group(1)), [], None
        for body in lines[index + 1:]:
            other = HEADING.match(body)
            if fence is None and other and len(other.group(1)) <= level:
                break
            stripped = body.strip()
            if stripped.startswith(("```", "~~~")):
                if fence is not None:
                    if commands:
                        return heading.group(2), commands
                    fence = None
                else:
                    fence = stripped[:3]
                continue
            if fence is not None and stripped and not NOT_SETUP.match(stripped):
                commands.append(stripped.lstrip("$> ").strip())
    return None


def own_setup(repo: Path, commit: str) -> tuple[str, list[str]] | None:
    """A commit's own setup command, from its own tree: its README's build section, else a tools/setup.py, else a
    root PowerShell script with a setup action. ``(where it was read, commands)``, or None when it names none."""
    for name in ("README.md", "README", "README.txt"):
        text = _show(repo, commit, name)
        found = readme_setup(text) if text else None
        if found:
            return f"its {name}'s section {found[0]!r}", found[1]
    if _carries(repo, commit, "tools/setup.py"):
        return "its tools/setup.py", ["python tools/setup.py"]
    listed = subprocess.run(["git", "ls-tree", "--name-only", commit], cwd=repo, capture_output=True, text=True,
                            env=_env())
    for name in sorted(listed.stdout.splitlines()) if listed.returncode == 0 else []:
        if name.lower().endswith(".ps1") and re.search(r"['\"]setup['\"]", _show(repo, commit, name) or ""):
            return f"its {name}", [f".\\{name} setup"]
    return None


def predates(workspace, project, commit: str) -> dict | None:
    """The answer for a commit from before its project's conversion: ``{"runs", "message"}``, where ``runs`` says
    whether its checks can still run (it carries actions.toml); None for any other commit. A project that names its
    own checks file, or whose set-named branch has no project.toml yet, has no conversion to predate."""
    if (project.entry or {}).get("config"):
        return None
    manifest, legacy = workspace.engine().config_names()
    if _carries(project.repo, commit, manifest):
        return None
    branch, ref, tip = workspace.set_branch(project)
    if tip is None or not _carries(project.repo, tip, manifest):
        return None
    runs = _carries(project.repo, commit, legacy)
    shown = ref.removeprefix("refs/remotes/").removeprefix("refs/heads/")
    found = own_setup(project.repo, commit)
    carries = f"it carries {legacy} and no {manifest}" if runs else f"it carries neither {manifest} nor {legacy}"
    message = (f"{project.name} at {commit[:12]} predates the set: {carries}, while {project.name}'s {branch} "
               f"({shown} at {tip[:12]}) carries {manifest}.")
    if found:
        where, commands = found
        first = next((c for c in commands if "setup" in c.lower()), commands[0])
        rest = [c for c in commands if c != first]
        message += f" Its own setup command, from {where}: {first}" + (f" (then {', '.join(rest)})." if rest else ".")
    else:
        message += (" It names no setup command of its own: no build section in its README, no tools/setup.py, no "
                    "setup action in a root script.")
    cache = workspace.folder("pin_cache")
    message += (" Outside Workbench it builds with its own in-tree scripts"
                + (f" and the pin cache folder (WB_PIN_CACHE={cache})." if cache else "."))
    if runs:
        message += f" Its checks still run here from its {legacy}, for the grace period."
        ancestor = subprocess.run(["git", "merge-base", "--is-ancestor", commit, tip], cwd=project.repo,
                                  capture_output=True, env=_env()).returncode == 0
        if not ancestor:
            message += f" A branch cut before the conversion is rebased onto {branch} to be checked the set's way."
    else:
        message += " Workbench has no checks to run on it."
    return {"runs": runs, "message": message, "setup": found}
