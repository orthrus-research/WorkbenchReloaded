"""The checkouts a lab job reads, each fixed at a commit for the whole job.

A profile's ``checkout`` (the launcher's) and each of its ``checkouts`` is either a folder, read as it is, or a table
that names a repository and the ref to read::

    checkout = { repo = '<checkout>', ref = "origin/main", fetch = true }    # a branch, fetched first
    [checkouts]
    library = { repo = "../library", ref = "4c1d0e2" }                      # a commit

A relative ``repo`` is relative to the launcher's repository.

Each commit is read from a snapshot: a linked worktree of the repository, detached at exactly that commit, under the
snapshot root (``modules.lab.snapshots``, else ``checkouts`` in the lab home). Being a worktree, it finds what the
repository's main checkout has beside it as the main checkout does, and adds only a worktree entry to the repository;
the checkout itself is not touched. A snapshot is never moved: a job that resolved a branch keeps reading its commit while a later job reads a newer one, and a push
needs no checkout moved by hand. The launcher's snapshot links the profile's ``lab_items`` from the lab home (or, for
an item not moved there and for the ``stays``, from the launcher repository's ``lab_root``), so instances and caches
are shared, never copied. The commits a job read are kept as its ``checkouts`` output. ``lab.checkouts`` shows them
and removes snapshots no job has used for a while."""

from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from wb_core.module import Context, EnvironmentProblem, ParamError

CHECKOUT_KEYS = {"repo", "ref", "fetch"}


@dataclass
class Checkout:
    name: str              # "" for the launcher's
    path: str              # what the job reads
    repo: str | None       # the repository a snapshot comes from; None: a folder read as it is
    commit: str | None
    via: str               # how the commit was found


def check(profile: dict, error) -> None:
    """Refuses a checkout table that names no repository or no ref, or has unknown keys. ``error`` makes the
    exception."""
    def one(label: str, value):
        if value is None or isinstance(value, str):
            return
        if not isinstance(value, dict) or set(value) - CHECKOUT_KEYS or not isinstance(value.get("repo"), str):
            raise error(f"the instance profile's {label}: a folder, or a table with repo and ref "
                        f"(keys: {', '.join(sorted(CHECKOUT_KEYS))})")
        if not isinstance(value.get("ref"), str) or not value["ref"]:
            raise error(f"the instance profile's {label}: ref names a branch, tag or commit")
        if "fetch" in value and not isinstance(value["fetch"], bool):
            raise error(f"the instance profile's {label}: fetch is true or false")

    one("checkout", profile.get("checkout"))
    if not isinstance(profile.get("checkouts") or {}, dict):
        raise error("the instance profile's checkouts is a table of checkouts by name")
    for name, value in (profile.get("checkouts") or {}).items():
        one(f"checkouts.{name}", value)


def resolve(ctx: Context) -> dict[str, Checkout]:
    """Every checkout of the job's profile, by name ("" the launcher's), resolved once per job and kept as its output."""
    from . import commands
    cached = ctx.__dict__.setdefault("_lab_checkouts", {})  # by profile: a turn's steps may name different ones
    harness = commands._profile_name(ctx)
    if harness in cached:
        return cached[harness]
    profile = commands._profile(ctx)
    found = {"": _one(ctx, "", profile.get("checkout") or ctx.config.get("launcher"), {}, None, profile)}
    if not found[""].path:
        raise EnvironmentProblem("modules.lab.launcher is not set (the checkout whose launcher builds instances), "
                                 "and the instance profile names no checkout")
    base = Path(found[""].repo or found[""].path)
    for name, value in (profile.get("checkouts") or {}).items():
        found[name] = _one(ctx, name, value, found, base, profile)
    cached[harness] = found
    if any(c.repo for c in found.values()):
        record = ctx.out / (f"checkouts-{harness}.json" if harness else "checkouts.json")
        record.write_text(json.dumps({n or "launcher": asdict(c) for n, c in found.items()}, indent=1),
                          encoding="utf-8")
        ctx.output(record.stem, record)
        ctx.log.info("checkouts: " + ", ".join(f"{n or 'launcher'} {c.commit[:12]} ({c.via})"
                                               for n, c in found.items() if c.commit))
    return found


def launcher(ctx: Context) -> Path:
    return Path(resolve(ctx)[""].path)


def checkout(ctx: Context, name: str) -> Path:
    found = resolve(ctx)
    if name not in found:
        raise EnvironmentProblem(f"the instance profile uses {{{name}}} but its checkouts name no {name}")
    return Path(found[name].path)


def _absolute(value: str, base: Path | None) -> Path:
    from wb_core.service import REPO_ROOT
    path = Path(value)
    if path.is_absolute():
        return path
    return ((base if base is not None else REPO_ROOT) / path).resolve()


def _one(ctx: Context, name: str, value, found: dict[str, Checkout], base: Path | None, profile: dict) -> Checkout:
    if not value:
        return Checkout(name, "", None, None, "unset")
    if isinstance(value, str):
        return Checkout(name, str(_absolute(value, base)), None, None, "folder")
    repo = _absolute(value["repo"], base)
    if value.get("fetch"):
        _fetch(ctx, repo, value["ref"])
    commit, via = _commit(ctx, repo, value["ref"]), f"ref {value['ref']}"
    path = materialize(ctx, repo, commit, launcher_profile=profile if name == "" else None)
    return Checkout(name, str(path), str(repo), commit, via)


def _git(args: list[str], *, check: bool = True):
    from wb_build import checkout as checkouts
    return checkouts.git(args, check=check)


def _common(ctx: Context, repo: Path) -> Path:
    from wb_build import checkout as checkouts
    if not repo.is_dir():
        raise EnvironmentProblem(f"no checkout at {repo}")
    return checkouts.common_dir(ctx, checkouts.git_dir(ctx, repo))


def _fetch(ctx: Context, repo: Path, ref: str) -> None:
    """Fetches the branch a remote-tracking ref names (``origin/main``), so the job reads what was pushed. A fetch that
    fails leaves the ref as it was, with a warning: the job reads the last commit fetched."""
    remotes = _git(["--git-dir", str(_common(ctx, repo)), "remote"]).stdout.split()
    remote, _, branch = ref.partition("/")
    if remote not in remotes or not branch:
        raise ParamError(f"fetch: {ref} names no branch of a remote of {repo} (remotes: {', '.join(remotes) or 'none'})")
    done = _git(["--git-dir", str(_common(ctx, repo)), "fetch", "--quiet", remote, branch], check=False)
    if done.returncode != 0:
        ctx.log.warning(f"could not fetch {ref} into {repo}, so it reads the last one fetched: "
                        f"{(done.stderr or done.stdout).strip()[-200:]}")


def _commit(ctx: Context, repo: Path, ref: str) -> str:
    done = _git(["--git-dir", str(_common(ctx, repo)), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
                check=False)
    if done.returncode != 0:
        raise EnvironmentProblem(f"{repo} has no commit {ref} (fetch it there first)")
    return done.stdout.strip()


# -- snapshots ----------------------------------------------------------------------------------------------------


def root(ctx: Context) -> Path:
    configured = ctx.config.get("snapshots")
    if configured:
        return Path(configured)
    from . import commands
    return commands._lab_home() / "checkouts"


def _label(repo: Path) -> str:
    """The folder a repository's snapshots share: its name, and a digest of its path, so two repositories of one
    name (a clone and a worktree elsewhere) never share one."""
    import hashlib
    return f"{repo.name or 'checkout'}-{hashlib.sha256(str(repo.resolve()).lower().encode()).hexdigest()[:8]}"


def materialize(ctx: Context, repo: Path, commit: str, launcher_profile: dict | None = None) -> Path:
    """The snapshot of ``repo`` at ``commit``, made once: a linked worktree of the repository, detached at the commit,
    so tools that find sibling checkouts from the main checkout a worktree names find the same ones they always do.
    Its record (``<commit>.json`` beside it) is written once it is whole and says when a job last used it; one job
    makes it while any other waits."""
    folder = root(ctx) / _label(repo)
    target = folder / commit[:12]
    record = folder / f"{commit[:12]}.json"
    folder.mkdir(parents=True, exist_ok=True)
    if not record.is_file():
        with _making(ctx, folder / f"{commit[:12]}.making", record):
            if not record.is_file():
                ctx.progress("checkouts", msg=f"{_label(repo)} at {commit[:12]}")
                if target.exists():  # left by a job that stopped while making it
                    remove(target, repo)
                done = _git(["-C", str(repo), "-c", "advice.detachedHead=false", "worktree", "add", "--detach",
                             "--quiet", str(target), commit], check=False)
                if done.returncode != 0:
                    raise EnvironmentProblem(f"git worktree add {target} {commit[:12]} exited {done.returncode}: "
                                             f"{(done.stderr or done.stdout).strip()[-300:] or 'it said nothing'}")
                if launcher_profile is not None:
                    _link_lab(ctx, repo, target, launcher_profile)
    record.write_text(json.dumps({"repo": str(repo), "commit": commit, "used": time.time(), "job": ctx.job}),
                      encoding="utf-8")
    return target


class _making:
    """A lock file only one job holds while it makes a snapshot; another waits for the record (or for the lock to
    go), and a lock older than an hour is taken as left by a job that died."""

    def __init__(self, ctx: Context, lock: Path, record: Path):
        self.ctx, self.lock, self.record, self.held = ctx, lock, record, False

    def __enter__(self):
        deadline = time.monotonic() + 1800
        while not self.record.is_file():  # made meanwhile: nothing to hold
            try:
                os.close(os.open(self.lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                self.held = True
                return self
            except FileExistsError:
                try:
                    stale = time.time() - self.lock.stat().st_mtime > 3600
                except OSError:
                    continue  # released just now
                if stale:
                    self.lock.unlink(missing_ok=True)
                elif time.monotonic() > deadline:
                    raise EnvironmentProblem(f"{self.lock} has been held by another job for half an hour") from None
                else:
                    self.ctx.sleep(1)
        return self

    def __exit__(self, *exc):
        if self.held:
            self.lock.unlink(missing_ok=True)
        return False


def _link_lab(ctx: Context, repo: Path, snapshot: Path, profile: dict) -> None:
    """Links each lab item into the snapshot's lab_root: from the lab home, else (an item not moved there, and the
    stays) from the launcher repository's lab_root. A folder is a junction or symlink, a file a hard link."""
    from wb import platform
    lab_root = profile.get("lab_root")
    if not lab_root:
        return
    home = os.environ.get("WB_LAB_HOME")
    stays = set(profile.get("stays") or [])
    names = [item[0] for item in profile.get("lab_items") or []] + sorted(stays)
    local = snapshot / lab_root
    local.mkdir(parents=True, exist_ok=True)
    for name in dict.fromkeys(names):
        candidates = ([] if name in stays or not home else [Path(home) / name]) + [repo / lab_root / name]
        source = next((c for c in candidates if c.exists()), None)
        if source is None:
            continue
        link = local / name
        if source.is_dir():
            platform.make_link(link, Path(os.path.realpath(source)))
        else:
            try:
                os.link(os.path.realpath(source), link)
            except OSError as error:
                ctx.log.warning(f"{name} is not linked into the snapshot ({error})")


def remove(snapshot: Path, repo: Path | None = None) -> None:
    """Removes a snapshot: every link in it first, without following it, so nothing a link points at is touched;
    then the rest (read-only files too, and paths past Windows' 260 characters), and the repository's record of the
    worktree."""
    from wb import platform
    if not snapshot.exists():
        if repo is not None:
            _git(["-C", str(repo), "worktree", "prune"], check=False)
        return
    folders = [snapshot]
    while folders:
        for entry in os.scandir(folders.pop()):
            path = Path(entry.path)
            if platform.is_link(path):
                platform.remove_link(path)
            elif entry.is_dir(follow_symlinks=False):
                folders.append(path)

    def writable(function, path, _):
        os.chmod(path, 0o700)
        function(path)

    long = "\\\\?\\" + str(snapshot.resolve()) if os.name == "nt" else str(snapshot)
    shutil.rmtree(long, onexc=writable)
    if repo is not None:
        _git(["-C", str(repo), "worktree", "prune"], check=False)


def listed(ctx: Context) -> list[dict]:
    """Every snapshot under the root: repository, commit, folder and when a job last used it."""
    out = []
    base = root(ctx)
    for record in sorted(base.glob("*/*.json")) if base.is_dir() else []:
        try:
            data = json.loads(record.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.append({**data, "label": record.parent.name, "path": str(record.with_suffix(""))})
    return out


def checkouts_command(ctx: Context, prune: bool = False, keep: int = 3, unused_days: float = 7, profile: str = ""):
    """lab.checkouts: what the profile's checkouts resolve to now, and the snapshots kept; with prune, removes the
    snapshots past the newest ``keep`` of each repository that no job has used for ``unused_days``."""
    found = resolve(ctx)
    snapshots = listed(ctx)
    removed = []
    if prune:
        current = {c.path for c in found.values()}
        by_label: dict[str, list[dict]] = {}
        for entry in snapshots:
            by_label.setdefault(entry["label"], []).append(entry)
        cutoff = time.time() - unused_days * 86400
        for entries in by_label.values():
            entries.sort(key=lambda e: e.get("used", 0), reverse=True)
            for entry in entries[keep:]:
                if entry["path"] in current or entry.get("used", 0) > cutoff:
                    continue
                remove(Path(entry["path"]), Path(entry["repo"]))
                Path(entry["path"] + ".json").unlink(missing_ok=True)
                removed.append(entry["path"])
    ctx.summary(", ".join(f"{n or 'launcher'} {c.commit[:12] if c.commit else c.path}" for n, c in found.items())
                + f"; {len(snapshots) - len(removed)} snapshot(s) kept" + (f", {len(removed)} removed" if prune else ""))
    return {"checkouts": {n or "launcher": asdict(c) for n, c in found.items()}, "snapshots": snapshots,
            "removed": removed}
