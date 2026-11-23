"""The workspace as the host sees it: which registered project a folder belongs to, the tree a worktree would
commit (written by the host's own git, so line endings, file modes and excludes are the host's), and the jobs file
and run records a commit is judged by. Everything here runs on the host with no daemon.

A project is found by its repository, not by a folder: every linked worktree of a registered repository shares its
git common dir, so a parked main checkout stops mattering."""

from __future__ import annotations

import ntpath
import json
import os
import posixpath
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import config as wbconfig

REPO = Path(__file__).resolve().parents[3]  # the Workbench checkout this CLI runs from
TOOLS = REPO / "modules" / "tools" / "src"
GIT_ENV = {"GIT_OPTIONAL_LOCKS": "0"}  # reading never refreshes anyone's index


class WorkspaceError(Exception):
    """Something about the workspace a person can fix; the message names the fix."""


def git(cwd: Path, *args: str, env: dict | None = None, check: bool = True, text: bool = True):
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=text,
                          env={**os.environ, **GIT_ENV, **(env or {})})
    if check and done.returncode != 0:
        detail = (done.stderr if text else done.stderr.decode("utf-8", "replace")).strip()
        raise WorkspaceError(f"git {' '.join(args[:3])} failed in {cwd}: {detail[-300:]}")
    return done.stdout.strip() if text else done.stdout


def common_dir(path: Path) -> Path | None:
    """The git common dir of the repository ``path`` is in, or None outside any repository."""
    done = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=path,
                          capture_output=True, text=True, env={**os.environ, **GIT_ENV})
    return Path(done.stdout.strip()).resolve() if done.returncode == 0 and done.stdout.strip() else None


@dataclass
class Project:
    name: str
    entry: dict          # its [modules.ci.projects.<name>] table
    repo: Path           # the registered checkout, as the host names it
    common: Path | None  # its git common dir


class Workspace:
    def __init__(self, config=None):
        self.config = config or wbconfig.load()
        self.values = self.config.values
        ci = (self.values.get("modules") or {}).get("ci") or {}
        self.ci = ci
        self._projects: dict[str, Project] | None = None

    # -- paths ------------------------------------------------------------------------------------------------
    def host_path(self, value: str) -> Path:
        """A configured path as the host names it: a container path under a root is translated back."""
        for root in ((self.values.get("paths") or {}).get("roots") or {}).values():
            container = str(root.get("container") or "").rstrip("/")
            if container and (value == container or value.startswith(container + "/")):
                rest = value[len(container):].lstrip("/")
                return Path(str(root["host"])) / rest if rest else Path(str(root["host"]))
        return Path(value)

    def records(self, project: str) -> Path:
        configured = self.ci.get("records")
        if not configured:
            raise WorkspaceError("modules.ci.records is not set, so the host cannot read CI's records "
                                 "(set it to a folder in an rw root, such as the out root's ci/records)")
        return self.host_path(str(configured)) / project

    # -- projects ---------------------------------------------------------------------------------------------
    def projects(self) -> dict[str, Project]:
        if self._projects is None:
            found = {}
            root = str(self.ci.get("projects_root") or "")
            for name, entry in (self.ci.get("projects") or {}).items():
                repo = str(entry.get("repo") or "")
                if not repo:
                    continue
                absolute = (len(repo) > 2 and repo[1] == ":") or repo.startswith(("/", "\\\\"))
                if not absolute and root:
                    repo = ntpath.join(root, repo) if len(root) > 1 and root[1] == ":" else posixpath.join(root, repo)
                path = Path(repo)
                found[name] = Project(name, dict(entry), path, common_dir(path) if path.is_dir() else None)
            self._projects = found
        return self._projects

    def project_for(self, folder: Path) -> tuple[Project, Path]:
        """The registered project ``folder`` belongs to, and the top of the worktree it is in."""
        common = common_dir(folder)
        if common is None:
            raise WorkspaceError(f"{folder} is not in a git repository")
        top = Path(git(folder, "rev-parse", "--show-toplevel"))
        for project in self.projects().values():
            if project.common == common:
                return project, top
        names = ", ".join(sorted(self.projects())) or "none"
        raise WorkspaceError(f"{top} is not a registered project (registered: {names}); register its repository "
                             "under [modules.ci.projects] in the organisation config")

    def find(self, folder: Path) -> Project | None:
        try:
            return self.project_for(folder)[0]
        except WorkspaceError:
            return None

    # -- trees ------------------------------------------------------------------------------------------------
    def would_be(self, worktree: Path) -> dict:
        """The commit a check of ``worktree`` should test: HEAD when it is clean, else a commit object holding the
        tree the host's git would commit now (staged and unstaged changes, untracked files not ignored), parented
        on HEAD. The object goes into the repository's object store and nothing else changes: no ref, no index,
        no file in the worktree. Its tree is what a pass vouches for, so the pass stands for the commit to come."""
        head = git(worktree, "rev-parse", "--verify", "HEAD^{commit}")
        index = Path(git(worktree, "rev-parse", "--path-format=absolute", "--git-path", "index"))
        with tempfile.TemporaryDirectory(prefix="wb-tree-") as scratch:
            copy = Path(scratch) / "index"
            if index.is_file():
                copy.write_bytes(index.read_bytes())  # its stat data makes add --all read only what changed
            env = {"GIT_INDEX_FILE": str(copy)}
            git(worktree, "add", "--all", ".", env=env)
            tree = git(worktree, "write-tree", env=env)
        if tree == git(worktree, "rev-parse", f"{head}^{{tree}}"):
            return {"commit": head, "tree": tree, "dirty": False, "head": head}
        commit = git(worktree, "commit-tree", tree, "-p", head, "-m", "the working tree, as wb check tests it",
                     env={"GIT_AUTHOR_NAME": "wb", "GIT_AUTHOR_EMAIL": "wb@example.invalid",
                          "GIT_COMMITTER_NAME": "wb", "GIT_COMMITTER_EMAIL": "wb@example.invalid"})
        return {"commit": commit, "tree": tree, "dirty": True, "head": head}

    # -- the gate ---------------------------------------------------------------------------------------------
    def engine(self):
        if str(TOOLS) not in sys.path:
            sys.path.insert(0, str(TOOLS))
        from orthrus_tools import actions
        return actions

    def jobs_name(self, project: Project) -> str:
        return str(project.entry.get("config") or "project.toml or actions.toml").replace("\\", "/")

    def config_at(self, project: Project, sha: str, scratch: Path):
        """The engine's config from the jobs file in commit ``sha``, with CI's records; None when it has none."""
        actions = self.engine()
        configured = (project.entry or {}).get("config")
        for name in [str(configured)] if configured else actions.config_names():
            shown = subprocess.run(["git", "show", f"{sha}:{name}"], cwd=project.repo,
                                   capture_output=True, env={**os.environ, **GIT_ENV})
            if shown.returncode == 0:
                break
        else:
            return None
        copy = scratch / f"jobs-{sha[:12]}" / Path(name).name
        copy.parent.mkdir(parents=True, exist_ok=True)
        copy.write_bytes(shown.stdout)
        config = actions.load(project.repo, copy)
        config.runs = self.records(project.name)
        return config

    # -- the Set ----------------------------------------------------------------------------------------------
    def set_home(self) -> Path | None:
        configured = str((self.values.get("workspace") or {}).get("set") or "")
        return Path(configured) if configured and (Path(configured) / "HEAD").is_file() else None

    def set_line(self, name: str) -> dict | None:
        """The project's line at the head of this machine's Set, or None when it has none (or there is no Set)."""
        home = self.set_home()
        if home is None:
            return None
        shown = subprocess.run(["git", "show", "refs/heads/main:set.json"], cwd=home, capture_output=True,
                               text=True, encoding="utf-8")
        if shown.returncode != 0:
            return None
        try:
            return (json.loads(shown.stdout).get("projects") or {}).get(name)
        except ValueError:
            return None

    def store_root(self) -> Path | None:
        value = self.ci.get("store")
        return self.host_path(str(value)) if value else None

    def landing_rule(self, project: Project, commit: str, remote_ref: str) -> str | None:
        """For a push to a landing ref or to the project's set-named branch: '' when a publishing landing names the
        commit, else why not. None for any other ref, which the ordinary gate judges."""
        from wb_core import landing as landings
        line = self.set_line(project.name)
        store = self.store_root()
        if remote_ref.startswith("refs/landed/"):
            states = ("prepared", "refs-pushed", "set-pushed")
        elif line and remote_ref == f"refs/heads/{line.get('branch')}":
            states = ("refs-pushed", "set-pushed")
        else:
            return None
        if store is not None and landings.naming(store, project.name, commit, states):
            return ""
        what = remote_ref.removeprefix("refs/heads/")
        return (f"{what} is {project.name}'s set-named branch, which moves only by landing: wb land in a worktree "
                f"at {commit[:12]} (or wb land {project.name}={commit[:12]})")

    def gate(self, project: Project, lines: list[str]) -> dict:
        """The push gate, answered on the host from git and CI's records. Each pushed commit is judged by its own
        jobs file and the trees it pins; a project whose ``gate`` is false, or a commit with no jobs file, is not
        gated."""
        actions = self.engine()
        if project.entry.get("gate") is False:
            return {"refused": [], "required": [], "gated": False}
        refused, required = [], {}
        with tempfile.TemporaryDirectory(prefix="wb-gate-") as scratch:
            for line in lines:
                fields = line.split()
                if len(fields) != 4 or set(fields[1]) == {"0"}:
                    continue
                landed = self.landing_rule(project, fields[1], fields[2])
                if landed is not None:
                    if landed:
                        refused.append({"ref": fields[0], "commit": fields[1], "reason": landed})
                    continue
                config = self.config_at(project, fields[1], Path(scratch))
                if config is None:
                    refused.append({"ref": fields[0], "commit": fields[1],
                                    "reason": f"it has no {self.jobs_name(project)}, so Workbench has no checks to "
                                              "judge it by (merge the CI setup into it, or push with --no-verify)"})
                    continue
                refused += actions.gate_check(config, config.names[0], [line])
                required |= dict.fromkeys(config.required())
        return {"refused": refused, "required": list(required), "gated": True}
