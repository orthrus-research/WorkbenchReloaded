"""The .set file: the one set revision a worktree builds against.

``.set`` ({set, label, created}) sits untracked at a worktree's top, or once at a Work folder for all its members. It
is written by the first wb verb run in a folder that has none (the head, and the verb says so), and moved only by
``wb work sync`` and by a successful ``wb land`` from that folder. Nobody else's landing changes what a worktree
resolves. A folder is behind when its revision is not the head of this machine's Set; every verb says so first:

    set 3f2a91c, 2 behind (cubature, orthlib moved): wb work sync
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from .workspace import Workspace, WorkspaceError, common_dir, git

NAME = ".set"


def _say(text: str):
    print(f"wb: {text}", file=sys.stderr)


def find(folder: Path) -> tuple[Path, dict] | None:
    """The nearest .set at or above ``folder``, with what it says."""
    for place in [folder, *folder.parents]:
        path = place / NAME
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and isinstance(data.get("set"), str):
                return path, data
    return None


def home_of(workspace: Workspace, folder: Path) -> Path:
    """Where a folder's .set belongs: the Work folder when the worktree is a member of a Work, else its top."""
    from .work import work_root
    top = Path(git(folder, "rev-parse", "--show-toplevel"))
    try:
        root = work_root(workspace)
    except WorkspaceError:
        return top
    return top.parent if top.parent.parent.resolve() == root.resolve() else top


def write(place: Path, revision: str, label: str = "") -> Path:
    path = place / NAME
    path.write_text(json.dumps({"set": revision, "label": label or os.environ.get("WB_CALLER", ""),
                                "created": time.strftime("%Y-%m-%dT%H:%M:%S")}) + "\n", encoding="utf-8")
    _exclude(place)
    return path


def _exclude(place: Path):
    """Keeps /.set out of the repository's status: wb setup's line in info/exclude, added where it is missing."""
    try:
        common = common_dir(place)
    except Exception:
        return
    if common is None:
        return
    exclude = common / "info" / "exclude"
    try:
        lines = exclude.read_text(encoding="utf-8").splitlines() if exclude.is_file() else []
        if "/.set" not in lines and ".set" not in lines:
            exclude.parent.mkdir(parents=True, exist_ok=True)
            with open(exclude, "a", encoding="utf-8") as handle:
                handle.write(("\n" if lines and lines[-1] else "") + "/.set\n")
    except OSError:
        pass


def head(workspace: Workspace) -> str | None:
    home = workspace.set_home()
    return git(home, "rev-parse", "--verify", "refs/heads/main", check=False) or None if home else None


def moved(workspace: Workspace, revision: str, newer: str) -> list[str]:
    """The projects whose lines differ between two revisions of the Set."""
    home = workspace.set_home()
    if home is None:
        return []
    old = json.loads(git(home, "show", f"{revision}:set.json") or "{}").get("projects") or {}
    new = json.loads(git(home, "show", f"{newer}:set.json") or "{}").get("projects") or {}
    return sorted(n for n in set(old) | set(new) if (old.get(n) or {}).get("commit") != (new.get(n) or {}).get("commit"))


def resolve(workspace: Workspace, folder: Path, *, say=_say) -> str | None:
    """This folder's revision, writing the head as its .set when it has none, and the line every verb prints first.
    None when this machine has no Set."""
    current = head(workspace)
    if current is None:
        return None
    found = find(folder)
    if found is None:
        place = home_of(workspace, folder)
        write(place, current)
        say(f"set {current[:7]} (head), written to {place / NAME}")
        return current
    revision = found[1]["set"]
    if revision == current:
        say(f"set {revision[:7]} (head)")
        return revision
    home = workspace.set_home()
    count = git(home, "rev-list", "--count", f"{revision}..{current}", check=False) or "?"
    names = moved(workspace, revision, current)
    say(f"set {revision[:7]}, {count} behind ({', '.join(names) or 'nothing'} moved): wb work sync")
    return revision


def sync(workspace: Workspace, folder: Path) -> int:
    """Moves this folder's .set to the head, finishes any landing a publish left open, and says what this project's
    gate now lacks and whether its own line moved past HEAD (with the rebase that takes it; nothing is rebased)."""
    from .cli import _client
    from .land import Lander
    lander = Lander(workspace, _client(), say=_say)
    lander.reconcile()
    revision, text = lander.head()
    lander.export(revision, text, head=True)
    found = find(folder)
    place = found[0].parent if found else home_of(workspace, folder)
    before = found[1]["set"] if found else None
    write(place, revision)
    _say(f"set {revision[:7]} (head)" + (f", was {before[:7]}" if before and before != revision else ""))
    try:
        project, top = workspace.project_for(folder)
    except WorkspaceError:
        return 0
    line = workspace.set_line(project.name)
    commit = git(top, "rev-parse", "HEAD")
    if line and line["commit"] != commit and not _is_ancestor(top, line["commit"], commit):
        _say(f"{project.name} moved: git rebase {line['commit'][:7]}")
    verdict = workspace.gate(project, [f"HEAD {commit} HEAD {'0' * 40}"])
    if verdict["gated"] and verdict["refused"]:
        _say(f"{project.name} at {commit[:7]} lacks a pass: {verdict['refused'][0]['reason']} (wb check)")
    return 0


def _is_ancestor(top: Path, older: str, newer: str) -> bool:
    import subprocess
    return subprocess.run(["git", "merge-base", "--is-ancestor", older, newer], cwd=top,
                          capture_output=True).returncode == 0
