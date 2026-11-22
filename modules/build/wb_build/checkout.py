"""A checkout as the build and toolchain commands read it: its git directory, HEAD, tree and whether it is clean, the
files git tracks, and a shared clone at a commit.

Checkouts are only ever read. Git runs with optional locks off, so even ``status`` writes nothing into them, and with
every directory trusted, since a mounted checkout belongs to another user. A linked worktree names its git directory
in its ``.git`` file as the host wrote it (on Windows a drive path the container cannot open); that path is
translated through the configured roots.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from wb_core.module import Context, EnvironmentProblem, ParamError

GIT_ENV = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "safe.directory", "GIT_CONFIG_VALUE_0": "*",
           "GIT_OPTIONAL_LOCKS": "0"}


def _absolute(named: str) -> bool:
    return named.startswith(("/", "\\")) or (len(named) > 2 and named[1] == ":" and named[2] in "\\/")


def _seen(ctx: Context, named: str) -> Path:
    """A path a host wrote into a git file, as this machine sees it."""
    if Path(named).exists():
        return Path(named)
    from wb_core.paths import PathError, PathMap
    from wb_core.settings import Root
    roots = [Root(**r) for r in ctx.spec.get("roots", [])]
    if not roots or ctx.machine == "host":
        return Path(named)
    try:
        return Path(PathMap(roots).resolve(named).container)
    except PathError as error:
        raise EnvironmentProblem(f"git names {named}, which is under no configured root ({error})") from None


def git(args: list[str], *, check: bool = True, cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Runs a git query (stdout and stderr apart); a failure is an environment problem unless ``check`` is off."""
    try:
        done = subprocess.run(["git", *args], cwd=cwd, env=dict(os.environ, **GIT_ENV), capture_output=True,
                              stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace", timeout=900)
    except FileNotFoundError:
        raise EnvironmentProblem("git is not on PATH") from None
    except subprocess.TimeoutExpired:
        raise EnvironmentProblem(f"git {' '.join(args[:4])} took more than 15 minutes") from None
    if check and done.returncode != 0:
        raise EnvironmentProblem(f"git {' '.join(args[:4])} failed: {(done.stderr or done.stdout).strip()[-400:]}")
    return done


def git_dir(ctx: Context, checkout: Path) -> Path:
    """The git directory of a checkout (its top folder): ``.git``, or the one a linked worktree's ``.git`` names."""
    dot = checkout / ".git"
    if dot.is_dir():
        return dot
    if dot.is_file():
        line = dot.read_text(encoding="utf-8", errors="replace").strip()
        if line.startswith("gitdir:"):
            named = line.split(":", 1)[1].strip()
            return _seen(ctx, named) if _absolute(named) else (checkout / named).resolve()
    raise ParamError(f"{ctx.host_path(checkout)} is not the top folder of a git checkout")


def common_dir(ctx: Context, gitdir: Path) -> Path:
    """The repository's shared git directory: a linked worktree's objects and refs live there."""
    marker = gitdir / "commondir"
    if not marker.is_file():
        return gitdir
    named = marker.read_text(encoding="utf-8").strip()
    return _seen(ctx, named) if _absolute(named) else (gitdir / named).resolve()


def top(folder: Path) -> Path | None:
    """The checkout a folder is in: the nearest folder at or above it that holds ``.git``."""
    for candidate in (folder, *folder.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


@dataclass
class State:
    commit: str
    tree: str
    changes: list[str] = field(default_factory=list)  # git status --porcelain lines

    @property
    def dirty(self) -> bool:
        return bool(self.changes)


def state(ctx: Context, checkout: Path, *, untracked: bool = True) -> State:
    """HEAD's commit and tree, and the changes ``git status`` sees; untracked files count unless ``untracked`` is off
    (staging reads build outputs, which a checkout does not track)."""
    base = ["--git-dir", str(git_dir(ctx, checkout)), "--work-tree", str(checkout)]
    head = git([*base, "rev-parse", "--verify", "--quiet", "HEAD^{commit}"], check=False)
    if head.returncode != 0:
        raise ParamError(f"{ctx.host_path(checkout)} has no commit at HEAD")
    tree = git([*base, "rev-parse", "--verify", "HEAD^{tree}"]).stdout.strip()
    status = git([*base, "status", "--porcelain", f"--untracked-files={'normal' if untracked else 'no'}"])
    return State(commit=head.stdout.strip(), tree=tree,
                 changes=[line for line in status.stdout.splitlines() if line.strip()])


def tracked(ctx: Context, folder: Path) -> set[str]:
    """The files git tracks under ``folder``, as POSIX paths relative to it."""
    root = top(folder)
    if root is None:
        raise ParamError(f"{ctx.host_path(folder)} is in no git checkout, so nothing in it is tracked")
    listed = git(["--git-dir", str(git_dir(ctx, root)), "--work-tree", str(root), "ls-files", "-z", "--full-name",
                  "--", folder.relative_to(root).as_posix() if folder != root else "."], cwd=root)
    prefix = "" if folder == root else folder.relative_to(root).as_posix() + "/"
    return {name[len(prefix):] for name in listed.stdout.split("\0") if name and name.startswith(prefix)}


def clone_at(ctx: Context, checkout: Path, commit: str, target: Path) -> tuple[str, str]:
    """A clean tree of exactly ``commit`` in ``target``: a clone that borrows the checkout's objects and writes nothing
    into it, checked out without line-ending conversion, so the tree is the commit's own bytes on every machine.
    Returns the full commit and its tree."""
    source = common_dir(ctx, git_dir(ctx, checkout))
    found = git(["--git-dir", str(source), "rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}"], check=False)
    if found.returncode != 0:
        raise ParamError(f"no commit {commit} in {ctx.host_path(checkout)} (fetch it there first)")
    sha = found.stdout.strip()
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    ctx.run(["git", "clone", "--quiet", "--shared", "--no-checkout", "-c", "core.autocrlf=false", str(source),
             str(target)], env=GIT_ENV, check=True, log_output=False)
    ctx.run(["git", "-C", str(target), "-c", "advice.detachedHead=false", "checkout", "--quiet", "--detach", sha],
            env=GIT_ENV, check=True)
    tree = git(["-C", str(target), "rev-parse", f"{sha}^{{tree}}"]).stdout.strip()
    return sha, tree
