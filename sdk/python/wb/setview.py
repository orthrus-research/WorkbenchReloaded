"""What wb work --all adds about the Set: each line against its remote branch, landings a publish left open, and each
project not yet converted to project.toml with the legacy pins it carries against the line, with the commit and
SHA-256 to copy until it converts and moves its pin by its own route."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from .workspace import GIT_ENV, Workspace, git

COMMIT = re.compile(r"\b[0-9a-f]{40}\b")
PIN_FILES = ("*.json", "*.toml", "*.properties", "*.gradle")


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env={**__import__("os").environ, **GIT_ENV})


def _default_ref(repo: Path) -> str | None:
    ref = git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD", check=False)
    return ref or None


def lines(workspace: Workspace) -> dict:
    home = workspace.set_home()
    if home is None:
        return {}
    shown = _git(home, "show", "refs/heads/main:set.json")
    return (json.loads(shown.stdout).get("projects") or {}) if shown.returncode == 0 else {}


def legacy_pins(workspace: Workspace, consumer, current: dict) -> list[str]:
    """Each commit a consumer's tracked pin files name that lies on a producer's line history, against that line."""
    ref = _default_ref(consumer.repo)
    if not ref:
        return []
    found = _git(consumer.repo, "grep", "-I", "-n", "-o", "-E", "[0-9a-f]{40}", ref, "--", *PIN_FILES)
    out = []
    projects = workspace.projects()
    for row in found.stdout.splitlines():
        parts = row.split(":", 3)  # <ref>:<file>:<line>:<match>
        if len(parts) != 4:
            continue
        _, file_name, number, commit = parts
        for name, line in current.items():
            producer = projects.get(name)
            if producer is None or name == consumer.name or not producer.repo.is_dir():
                continue
            if _git(producer.repo, "merge-base", "--is-ancestor", commit, line["commit"]).returncode != 0:
                continue
            if commit == line["commit"]:
                out.append(f"    {file_name}:{number} pins {name} {commit[:12]}: at its line")
            else:
                behind = _git(producer.repo, "rev-list", "--count", f"{commit}..{line['commit']}").stdout.strip()
                files = ", ".join(f"{a} {f['sha256']}" for a, f in sorted((line.get("artifacts") or {}).items()))
                out.append(f"    {file_name}:{number} pins {name} {commit[:12]}, {behind} behind its line: copy "
                           f"commit {line['commit']}" + (f" ({files})" if files else ""))
    return out


def report(workspace: Workspace) -> list[str]:
    from wb_core import landing as landings
    current = lines(workspace)
    if not current and workspace.set_home() is None:
        return []
    out = ["set lines:"]
    projects = workspace.projects()
    for name, line in sorted(current.items()):
        project = projects.get(name)
        state = ""
        if project is not None and project.repo.is_dir():
            remote = git(project.repo, "rev-parse", "--verify", "-q", f"refs/remotes/origin/{line['branch']}",
                         check=False)
            state = ("its branch matches" if remote == line["commit"]
                     else f"origin/{line['branch']} is {remote[:12] if remote else 'missing'} (wb land, or a push "
                          "that went round the gate)")
        out.append(f"  {name} {line['commit'][:12]} on {line['branch']}: {state}")
    store = workspace.store_root()
    if store is not None:
        for record in landings.unfinished(store):
            out.append(f"  unfinished landing {record['id']} at {record['state']}: the next wb land finishes or "
                       "cleans it up")
    unconverted = []
    for project in sorted(projects.values(), key=lambda p: p.name):
        if project.name in current or not project.repo.is_dir():
            continue
        ref = _default_ref(project.repo)
        if ref and _git(project.repo, "cat-file", "-e", f"{ref}:project.toml").returncode == 0:
            continue
        pins = legacy_pins(workspace, project, current)
        unconverted.append(f"  {project.name} (no project.toml on {ref or 'its default branch'})"
                           + ("" if pins else ": names no line"))
        unconverted += pins
    if unconverted:
        out.append("not converted (each moves its pins by its own route until it converts):")
        out += unconverted
    return out
