"""``wb store``: what CI built, on the host.

    wb store [<project>] [<ref>]                       what the Store holds for a project at a commit (default: where you
                                                       stand, at HEAD)
    wb store path <project> <ref> <artifact>           the kept file's path, re-hashed first (for a script or a launcher)
    wb store add <file> --as <label> --source <where> --reason <why>
                                                       keep bytes no container can build, with where they came from

The Store is [modules.ci] store in machine config (an rw root's folder, so the host reads it)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .workspace import Workspace, WorkspaceError, git


def _root(workspace: Workspace) -> Path:
    configured = workspace.ci.get("store")
    if not configured:
        raise WorkspaceError("modules.ci.store is not set, so the host cannot read the Store (set it to a folder in an "
                             "rw root, such as the out root's store)")
    return workspace.host_path(str(configured))


def _pins(workspace: Workspace) -> Path | None:
    configured = workspace.ci.get("pin_cache")
    return workspace.host_path(str(configured)) if configured else None


def _resolve(workspace: Workspace, project: str | None, ref: str | None):
    from wb_core import artifacts
    if project:
        projects = workspace.projects()
        if project not in projects:
            raise WorkspaceError(f"unknown project {project} (registered: {', '.join(sorted(projects))})")
        found, where = projects[project], projects[project].repo
    else:
        found, where = workspace.project_for(Path.cwd())
    commit = git(where, "rev-parse", "--verify", f"{ref or 'HEAD'}^{{commit}}")
    tree = git(where, "rev-parse", f"{commit}^{{tree}}")
    return found, commit, tree, artifacts.find(_root(workspace), found.name, tree)


def main(args) -> int:
    from wb_core import artifacts
    try:
        workspace = Workspace()
        if args.action == "add":
            if not (args.names and args.label and args.source and args.reason):
                raise WorkspaceError("wb store add <file> --as <project:artifact@commit> --source <where> --reason <why>")
            note = artifacts.seed(_root(workspace), Path(args.names[0]), args.label, args.source, args.reason,
                                  _pins(workspace))
            print(json.dumps(note, indent=1) if args.json else f"kept {note['name']} {note['sha256']} as {note['as']}")
            return 0
        if args.action == "path":
            if len(args.names) != 3:
                raise WorkspaceError("wb store path <project> <ref> <artifact>")
            project, ref, name = args.names
            _, commit, tree, index = _resolve(workspace, project, ref)
            item = (index or {}).get("artifacts", {}).get(name)
            if not item:
                raise WorkspaceError(f"the Store has no {name} of {project} at {commit[:12]} (tree {tree[:12]}): run "
                                     f"its checks there (wb check)")
            print(artifacts.path_of(_root(workspace), item["sha256"], name))
            return 0
        names = list(args.names)
        if args.action not in (None, "show"):
            names.insert(0, args.action)
        project, commit, tree, index = _resolve(workspace, names[0] if names else None, names[1] if len(names) > 1
                                                else None)
        if args.json:
            print(json.dumps({"project": project.name, "commit": commit, "tree": tree, "index": index}, indent=1))
            return 0 if index else 1
        if not index:
            print(f"{project.name} at {commit[:12]} (tree {tree[:12]}): nothing in the Store; run its checks there "
                  "(wb check)")
            return 1
        print(f"{project.name} at {commit[:12]} (tree {tree[:12]}):")
        for name, item in sorted(index["artifacts"].items()):
            print(f"  {name}  {item['sha256']}  {item.get('bytes') or '?'} bytes  from {item['job']} in run {item['run']}")
        for disagreement in index.get("disagreements") or []:
            print(f"  ! {disagreement['name']}: another build of this tree made {disagreement['other'][:12]}")
        return 0
    except (WorkspaceError, artifacts.StoreError, OSError) as error:
        print(f"wb: {error}", file=sys.stderr)
        return 2
