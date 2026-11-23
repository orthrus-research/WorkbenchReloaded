"""``wb work``: worktrees with a lifecycle, so nobody makes or clears them by hand.

    wb work                                 this folder: its project, worktree, branch, changes, and whether it passed
    wb work --all                           every registered project's worktrees
    wb work start <branch> <project>...     a Work: <work root>/<branch as a folder>/<project>, each on <branch>
    wb work add <project>                   another member in the Work you stand in
    wb work adopt <path>                    move a worktree made by hand into a Work (git worktree move)
    wb work finish                          remove the Work's members that hold nothing unsaved

A member is cut from the project's default branch on its remote (origin/HEAD), fetched first. The work root is
[workspace] work in machine config, else a folder named work under the projects root. Nothing here moves a main
checkout or touches another worktree's files."""

from __future__ import annotations

import ntpath
import posixpath
import shutil
import sys
from pathlib import Path

from .workspace import Project, Workspace, WorkspaceError, common_dir, git

SCRATCH = ("build/", ".gradle/", "out/")  # what finish may throw away with a member


def _say(text: str):
    print(text, flush=True)


def work_root(workspace: Workspace) -> Path:
    configured = ((workspace.values.get("workspace") or {}).get("work"))
    if configured:
        return Path(str(configured))
    root = str(workspace.ci.get("projects_root") or "")
    if not root:
        raise WorkspaceError("no work root: set [workspace] work in machine config (or [modules.ci] projects_root)")
    return Path(ntpath.join(root, "work") if len(root) > 1 and root[1] == ":" else posixpath.join(root, "work"))


def topic_folder(branch: str) -> str:
    return branch.replace("/", "-")


def _default_branch(project: Project) -> str:
    ref = git(project.repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD", check=False)
    if not ref:
        raise WorkspaceError(f"{project.name} has no origin/HEAD; set it with "
                             f"`git -C {project.repo} remote set-head origin --auto`")
    return ref.split("/", 1)[1]


def _worktrees(project: Project) -> list[dict]:
    text = git(project.repo, "worktree", "list", "--porcelain")
    items, item = [], {}
    for line in text.splitlines() + [""]:
        if not line:
            if item:
                items.append(item)
            item = {}
            continue
        key, _, value = line.partition(" ")
        item[key] = value or True
    return items


def _state(workspace: Workspace, project: Project, path: Path) -> dict:
    branch = git(path, "rev-parse", "--abbrev-ref", "HEAD", check=False) or "?"
    changes = git(path, "status", "--porcelain", check=False)
    head = git(path, "rev-parse", "HEAD", check=False)
    upstream = git(path, "rev-parse", "--abbrev-ref", "@{upstream}", check=False)
    ahead = git(path, "rev-list", "--count", f"{upstream}..HEAD", check=False) if upstream else ""
    gate = "?"
    if head:
        try:
            answer = workspace.gate(project, [f"HEAD {head} HEAD {'0' * 40}"])
            gate = ("not gated" if not answer["gated"] else "passed" if not answer["refused"] else "not passed")
        except (WorkspaceError, OSError) as error:
            gate = f"unknown ({error})"
    return {"path": str(path), "branch": branch, "head": head[:12], "changes": len(changes.splitlines()),
            "ahead": ahead, "upstream": upstream, "checks": gate}


def _line(state: dict) -> str:
    ahead = f", {state['ahead']} ahead of {state['upstream']}" if state["ahead"] not in ("", "0") else ""
    changes = f", {state['changes']} changed file(s)" if state["changes"] else ""
    return f"  {state['path']}  {state['branch']} at {state['head']}{ahead}{changes}; checks {state['checks']}"


def view(workspace: Workspace, everything: bool) -> int:
    if not everything:
        project, top = workspace.project_for(Path.cwd())
        _say(f"{project.name}:")
        _say(_line(_state(workspace, project, top)))
        others = [w for w in _worktrees(project) if Path(w["worktree"]).resolve() != top.resolve()]
        if others:
            _say(f"  ({len(others)} other worktree(s) of this repository: wb work --all)")
        return 0
    for project in sorted(workspace.projects().values(), key=lambda p: p.name):
        if not project.common:
            _say(f"{project.name}: no checkout at {project.repo}")
            continue
        _say(f"{project.name}:")
        for item in _worktrees(project):
            path = Path(item["worktree"])
            if item.get("bare") or not path.is_dir():
                continue
            _say(_line(_state(workspace, project, path)))
    return 0


def _member(workspace: Workspace, project: Project, folder: Path, branch: str) -> Path:
    target = folder / project.name
    if target.exists():
        raise WorkspaceError(f"{target} exists already: cd there to work in it, or choose another branch name")
    base = _default_branch(project)
    git(project.repo, "fetch", "--quiet", "origin", base)
    exists = git(project.repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False)
    if exists:
        raise WorkspaceError(f"{project.name} has a branch {branch} already (git checks a branch out in one worktree "
                             f"only): choose another name, or adopt the worktree that has it")
    git(project.repo, "worktree", "add", "-q", "-b", branch, str(target), f"origin/{base}")
    git(target, "branch", "--quiet", "--unset-upstream", check=False)
    _say(f"{project.name}: {target} on {branch}, from origin/{base} at "
         f"{git(target, 'rev-parse', '--short=12', 'HEAD')}")
    return target


def start(workspace: Workspace, branch: str, names: list[str]) -> int:
    projects = workspace.projects()
    unknown = [n for n in names if n not in projects]
    if unknown or not names:
        raise WorkspaceError(f"name the projects to work on (registered: {', '.join(sorted(projects))})"
                             if not names else f"unknown project {unknown[0]} (registered: {', '.join(sorted(projects))})")
    folder = work_root(workspace) / topic_folder(branch)
    folder.mkdir(parents=True, exist_ok=True)
    made = [_member(workspace, projects[name], folder, branch) for name in names]
    _say(f"cd {made[0] if len(made) == 1 else folder}")
    return 0


def _work_of(workspace: Workspace) -> tuple[Path, str]:
    """The Work folder you stand in (or in a member of), and its branch."""
    here = Path.cwd().resolve()
    root = work_root(workspace).resolve()
    if root not in here.parents and here != root:
        raise WorkspaceError(f"{here} is not in a Work under {root}: start one with `wb work start <branch> <project>`")
    relative = here.relative_to(root)
    if not relative.parts:
        raise WorkspaceError(f"stand in a Work folder under {root}, not the root itself")
    folder = root / relative.parts[0]
    members = [p for p in folder.iterdir() if p.is_dir() and (p / ".git").exists()]
    branch = git(members[0], "rev-parse", "--abbrev-ref", "HEAD") if members else relative.parts[0]
    return folder, branch


def add(workspace: Workspace, name: str) -> int:
    projects = workspace.projects()
    if name not in projects:
        raise WorkspaceError(f"unknown project {name} (registered: {', '.join(sorted(projects))})")
    folder, branch = _work_of(workspace)
    target = _member(workspace, projects[name], folder, branch)
    _say(f"cd {target}")
    return 0


def adopt(workspace: Workspace, path: str) -> int:
    source = Path(path).resolve()
    if not source.is_dir():
        raise WorkspaceError(f"there is no folder {source}")
    project = workspace.find(source)
    if project is None:
        raise WorkspaceError(f"{source} is not a worktree of a registered project")
    if (source / ".git").is_dir():
        raise WorkspaceError(f"{source} is a main checkout, which stays where it is; make a Work instead: "
                             f"`wb work start <branch> {project.name}`")
    if Path.cwd().resolve() == source or source in Path.cwd().resolve().parents:
        raise WorkspaceError(f"you stand in {source}, which git cannot move while a shell uses it: "
                             f"`cd ..; wb work adopt {source}`")
    branch = git(source, "rev-parse", "--abbrev-ref", "HEAD")
    if branch == "HEAD":
        raise WorkspaceError(f"{source} is detached; check out a branch in it first")
    folder = work_root(workspace) / topic_folder(branch)
    target = folder / project.name
    if target.exists():
        raise WorkspaceError(f"{target} exists already")
    folder.mkdir(parents=True, exist_ok=True)
    done = git(project.repo, "worktree", "move", str(source), str(target), check=False)
    if done is None or not target.exists():
        raise WorkspaceError(f"git could not move {source}: close any shell or editor that uses it, then retry")
    _say(f"{project.name}: moved {source} to {target}")
    _say(f"cd {target}")
    return 0


def _unsaved(member: Path) -> list[str]:
    lines = git(member, "status", "--porcelain", "--ignored", check=False).splitlines()
    return [line for line in lines if not any(line[3:].startswith(s) for s in SCRATCH)]


def finish(workspace: Workspace) -> int:
    folder, branch = _work_of(workspace)
    if Path.cwd().resolve() != folder.resolve() and folder.resolve() in Path.cwd().resolve().parents:
        raise WorkspaceError(f"you stand in a member, which git cannot remove while a shell uses it: "
                             f"`cd {folder}; wb work finish`")
    members = [p for p in folder.iterdir() if p.is_dir() and (p / ".git").exists()]
    kept = 0
    for member in members:
        project = workspace.find(member)
        unsaved = _unsaved(member)
        if unsaved:
            kept += 1
            _say(f"{member.name}: kept, it holds {len(unsaved)} unsaved file(s), for example {unsaved[0][3:]}; "
                 "commit or stash them, or remove them, then run wb work finish again")
            continue
        repo = project.repo if project else member
        base = _default_branch(project) if project else "main"
        merged = not git(repo, "rev-list", f"origin/{base}..{branch}", check=False)  # nothing the remote lacks
        git(repo, "worktree", "remove", "--force", str(member))  # only scratch is left: checked just above
        _say(f"{member.name}: removed {member}")
        if merged:
            git(repo, "branch", "-q", "-d", branch, check=False)
            _say(f"{member.name}: branch {branch} is in origin/{base}, so it was deleted here; on the remote: "
                 f"git -C {repo} push origin --delete {branch}")
        else:
            _say(f"{member.name}: branch {branch} has commits origin/{base} lacks, so it was kept")
    if not kept and not any(folder.iterdir()):
        try:
            folder.rmdir()
            _say(f"removed {folder}")
        except OSError:
            _say(f"{folder} is empty; remove it once no shell stands in it")
    return 1 if kept else 0


def main(args) -> int:
    try:
        workspace = Workspace()
        if args.action in (None, "view"):
            return view(workspace, args.all)
        if args.action == "start":
            if not args.names:
                raise WorkspaceError("wb work start <branch> <project>...")
            return start(workspace, args.names[0], args.names[1:])
        if args.action == "add":
            return add(workspace, args.names[0]) if args.names else _usage("wb work add <project>")
        if args.action == "adopt":
            return adopt(workspace, args.names[0]) if args.names else _usage("wb work adopt <path>")
        if args.action == "finish":
            return finish(workspace)
    except WorkspaceError as error:
        print(f"wb: {error}", file=sys.stderr)
        return 2
    return _usage("wb work [start|add|adopt|finish]")


def _usage(text: str) -> int:
    print(f"wb: {text}", file=sys.stderr)
    return 2
