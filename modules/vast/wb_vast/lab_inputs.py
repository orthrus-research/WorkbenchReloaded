"""Lab inputs (``vast.lab-inputs``): the archives a template's inputs name, built from pinned commits.

An input with a recipe is built in a throwaway clean tree: a shared clone of its repository at the pinned commit, so
the checkout is only read (as ``runs.at-commit`` does), where the recipe's steps run in
order. A recipe's ``beside`` checkouts are clean trees too, each at its own pinned commit in the folder next to the
tree it names (``../Engine``), for steps that read a sibling checkout the way a project's own tools expect. A step
may name ``{python}`` (Workbench's interpreter), ``{tree}`` (the clean tree) and ``{beside.<name>}``; it gets every JDK as
``JAVA_HOME_<major>_X64`` (and ``JAVA_HOME`` for the recipe's ``jdk``), ``WB_PIN_CACHE`` and the recipe's ``env``.
The files the recipe's ``artifacts`` globs match, relative to its ``base`` folder and less its ``exclude``, are
packed into a tar whose bytes depend on their paths, modes and contents and on the commit alone: sorted entries, no
folder entries, the commit's time, owner 0, and the mode git records (0755 or 0644; a file the steps made is 0755
when it is executable, which a Windows machine cannot tell). Links are refused.

Beside ``<name>`` go ``<name>.sha256`` and the receipt ``<name>.receipt.json`` (``wb.lab-input.v1``): the recipe and
its digest, the commit and tree, each step and its exit code, every file's size, mode and SHA-256, the archive's
SHA-256 and the pin it was checked against. An archive that is not its pinned SHA-256 is not kept.

The pin cache ``ci`` fills, a folder of files named by their SHA-256 (``WB_PIN_CACHE``,
else ``[modules.toolchain] pin_cache``, else ``<state>/cache/pins``), is read first and filled after: an archive the
cache holds under its pinned SHA-256, or under the digest an earlier build of the same recipe made (the receipts of
builds are kept by recipe in ``<state>/vast/lab-inputs``), is taken from it instead of being built again.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shlex
import shutil
import sys
import tarfile
from pathlib import Path

from wb_build import checkout as checkouts
from wb_build.files import copy_verified, set_digest, write_atomic, write_sidecar
from wb_core.module import Context, EnvironmentProblem, ParamError, canonical, sha256_file

from .template import Input, Recipe, collect, glob_regex

RECEIPT_SCHEMA = "wb.lab-input.v1"
FORMAT = "tar (pax): entries sorted, no folders, the commit's time, owner 0, git's modes"
LINK = 0o120000  # the mode git records for a link


# ---------------------------------------------------------------------------------------------
# the pin cache

def pin_cache(ctx: Context) -> Path:
    """The folder of pinned files by SHA-256 that ``ci`` fills."""
    value = os.environ.get("WB_PIN_CACHE") or _configured_pin_cache(ctx)
    if value:
        return Path(value)
    return Path(os.environ.get("WB_STATE_DIR") or "/var/lib/wb") / "cache" / "pins"


def _configured_pin_cache(ctx: Context) -> str | None:
    """``[modules.toolchain] pin_cache``. A job is given only its own module's config, so this is read from the
    running Workbench's."""
    url = os.environ.get("WB_URL")
    if not url:
        return None
    try:
        from wb.client import Client
        values = Client(url, os.environ.get("WB_TOKEN")).config().get("values") or {}
    except Exception as error:  # noqa: BLE001 - the default cache still works; only reuse across folders is lost
        ctx.log.warning(f"could not read [modules.toolchain] pin_cache ({error}); using the default pin cache")
        return None
    value = ((values.get("modules") or {}).get("toolchain") or {}).get("pin_cache")
    return str(value) if value else None


def _from_cache(cache: Path, digest: str, target: Path) -> bool:
    stored = cache / digest
    if not stored.is_file() or sha256_file(stored) != digest:
        return False
    copy_verified(stored, target, digest)
    return True


def _to_cache(ctx: Context, cache: Path, path: Path, digest: str) -> None:
    stored = cache / digest
    try:
        if stored.is_file() and sha256_file(stored) == digest:
            return
        copy_verified(path, stored, digest)  # new, or in place of a damaged entry
    except OSError as error:
        ctx.log.warning(f"could not keep {digest[:12]} in the pin cache {cache}: {error}")


def _index(ctx: Context) -> Path:
    """Receipts of builds by recipe: how an archive made from the same recipe is found again."""
    return Path(os.environ.get("WB_STATE_DIR") or ctx.work) / "vast" / "lab-inputs"


def _stored(ctx: Context, key: str) -> dict | None:
    try:
        receipt = json.loads((_index(ctx) / f"{key}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return receipt if isinstance(receipt, dict) and receipt.get("schema") == RECEIPT_SCHEMA else None


# ---------------------------------------------------------------------------------------------
# a recipe

def resolve(ctx: Context, repo: Path, commit: str) -> tuple[str, str, int]:
    """The full commit, its tree and its committer time, read from the checkout's objects (nothing is written)."""
    source = checkouts.common_dir(ctx, checkouts.git_dir(ctx, repo))
    found = checkouts.git(["--git-dir", str(source), "rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}"],
                          check=False)
    if found.returncode != 0:
        raise ParamError(f"no commit {commit} in {ctx.host_path(repo)} (fetch it there first)")
    sha = found.stdout.strip()
    tree = checkouts.git(["--git-dir", str(source), "rev-parse", f"{sha}^{{tree}}"]).stdout.strip()
    moment = int(checkouts.git(["--git-dir", str(source), "show", "-s", "--format=%ct", sha]).stdout.strip())
    return sha, tree, moment


def recipe_key(name: str, recipe: Recipe, commit: str, beside: dict[str, str] | None = None) -> str:
    """What an archive is made from: its name, the full commit, the full commits of the checkouts beside it, and the
    recipe's steps, globs, JDK and environment. (A recipe with nothing beside keeps the key it always had.)"""
    material = {"schema": RECEIPT_SCHEMA, "input": name, "commit": commit, "steps": recipe.steps,
                "base": recipe.base, "artifacts": recipe.artifacts, "exclude": recipe.exclude, "jdk": recipe.jdk,
                "env": recipe.env}
    if beside:
        material["beside"] = dict(sorted(beside.items()))
    return hashlib.sha256(canonical(material).encode("utf-8")).hexdigest()


def _split(step: str, replacements: dict[str, str]) -> list[str]:
    args = shlex.split(step, posix=sys.platform != "win32")
    if sys.platform == "win32":  # non-POSIX splitting keeps the quotes around a token
        args = [a[1:-1] if len(a) > 1 and a[0] == a[-1] and a[0] in "\"'" else a for a in args]
    for key, value in replacements.items():
        args = [a.replace(key, value) for a in args]
    return args


def _env(ctx: Context, recipe: Recipe, cache: Path) -> dict[str, str]:
    jdks = ctx.spec.get("jdks") or {}
    env = {f"JAVA_HOME_{key}_X64": str(home) for key, home in jdks.items() if str(key).isdigit()}
    if recipe.jdk:
        home = ctx.jdk(recipe.jdk)
        env["JAVA_HOME"] = str(home)
        env["PATH"] = os.pathsep.join([str(home / "bin"), os.environ.get("PATH", "")])
    return {**env, "WB_PIN_CACHE": str(cache), **recipe.env}


def _modes(tree: Path, base: str) -> dict[str, int]:
    """The modes git records for the tree's files, by path relative to ``base``."""
    listed = checkouts.git(["-C", str(tree), "ls-files", "-s", "-z"]).stdout
    prefix = base + "/" if base else ""
    modes = {}
    for record in listed.split("\0"):
        meta, _, path = record.partition("\t")
        if path and path.startswith(prefix):
            modes[path[len(prefix):]] = int(meta.split()[0], 8)
    return modes


def _mode(path: Path, tracked: int | None) -> int:
    if tracked is not None:
        return 0o755 if tracked & 0o111 else 0o644
    return 0o755 if os.name != "nt" and path.stat().st_mode & 0o111 else 0o644


# ---------------------------------------------------------------------------------------------
# the archive

class _Reading:
    """A file as tarfile reads it, hashed on the way, so each file is read once."""

    def __init__(self, handle):
        self.handle, self.digest = handle, hashlib.sha256()

    def read(self, size: int = -1) -> bytes:
        data = self.handle.read(size)
        self.digest.update(data)
        return data


def pack(target: Path, base: Path, files: list[str], modes: dict[str, int], moment: int) -> list[dict]:
    """Packs ``files`` (sorted POSIX paths relative to ``base``) into the tar ``target``; returns each entry."""
    entries = []
    target.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(target, "w", format=tarfile.PAX_FORMAT, encoding="utf-8") as archive:
        for relative in files:
            path = base / relative
            info = tarfile.TarInfo(relative)
            info.size = path.stat().st_size
            info.mtime = moment
            info.mode = modes[relative]
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            with open(path, "rb") as handle:
                reading = _Reading(handle)
                archive.addfile(info, reading)
            entries.append({"path": relative, "bytes": info.size, "mode": f"{info.mode:04o}",
                            "sha256": reading.digest.hexdigest()})
    return entries


def listing(archive: Path) -> list[dict]:
    """An archive's files as a receipt lists them (for one taken from the pin cache without its receipt)."""
    entries = []
    with tarfile.open(archive) as handle:
        for member in handle:
            if not member.isfile():
                continue
            digest = hashlib.sha256()
            with handle.extractfile(member) as data:
                for block in iter(lambda: data.read(1 << 20), b""):
                    digest.update(block)
            entries.append({"path": member.name, "bytes": member.size, "mode": f"{member.mode:04o}",
                            "sha256": digest.hexdigest()})
    return entries


# ---------------------------------------------------------------------------------------------
# building one input

def build(ctx: Context, template: str, item: Input, repo: Path, cache: Path, *, cached: bool, keep: bool,
          beside: dict[str, Path] | None = None) -> dict:
    """Makes ``item``'s archive in the out folder by its recipe, or takes it from the pin cache; refusals are
    findings, and then nothing is kept. ``beside`` names the checkout of each of the recipe's ``beside`` entries."""
    recipe = item.recipe
    commit, tree_hash, moment = resolve(ctx, repo, recipe.commit)
    besides = {}
    for name, entry in recipe.beside.items():
        path = (beside or {}).get(name)
        if path is None:
            raise ParamError(f"{item.name}: no checkout given for beside.{name}")
        full, tree_of, _ = resolve(ctx, path, entry.commit)
        besides[name] = {"path": path, "commit": full, "tree": tree_of}
    key = recipe_key(item.name, recipe, commit, {n: b["commit"] for n, b in besides.items()})
    target = ctx.out / item.name
    result = {"name": item.name, "commit": commit, "tree": tree_hash, "pinned": item.sha256, "recipe_sha256": key,
              "sha256": None, "bytes": None, "cached": False, "matched": None, "archive": None, "receipt": None,
              "steps": []}
    if besides:
        result["beside"] = {n: {"commit": b["commit"], "tree": b["tree"]} for n, b in besides.items()}
    stored = _stored(ctx, key)
    if cached:
        digest = item.sha256 or ((stored or {}).get("archive") or {}).get("sha256")
        if digest and _from_cache(cache, digest, target):
            if stored and (stored.get("archive") or {}).get("sha256") == digest:
                receipt = dict(stored)
            else:  # pinned and cached, built elsewhere: the receipt is made from the archive itself
                receipt = _receipt(ctx, template, item, repo, key, (commit, tree_hash, moment),
                                   [{"step": s, "exit": None} for s in recipe.steps], listing(target), target,
                                   besides)
            receipt.update(cached=True, pinned=item.sha256, matched=None if item.sha256 is None else True)
            ctx.log.info(f"{item.name}: {digest[:12]} from the pin cache, not built")
            return _publish(ctx, item, target, receipt, result)
    parent = ctx.work / "trees" / item.name  # the tree and the checkouts beside it, as siblings
    tree = parent / recipe.folder
    made = ctx.work / "archives" / item.name
    ctx.progress("clone", msg=f"{item.name}: {commit[:12]} from {ctx.host_path(repo)}")
    try:
        shutil.rmtree(parent, ignore_errors=True)  # a tree kept by an earlier keep=true
        checkouts.clone_at(ctx, repo, commit, tree)
        replacements = {"{python}": sys.executable, "{tree}": str(tree)}
        for name, entry in besides.items():
            ctx.progress("clone", msg=f"{item.name}: beside.{name} {entry['commit'][:12]} from "
                                      f"{ctx.host_path(entry['path'])}")
            checkouts.clone_at(ctx, entry["path"], entry["commit"], parent / name)
            replacements[f"{{beside.{name}}}"] = str(parent / name)
        env = _env(ctx, recipe, cache)
        steps = result["steps"]
        for index, step in enumerate(recipe.steps, start=1):
            ctx.check_cancel()
            ctx.progress(f"{item.name} step {index}", msg=step)
            try:
                done = ctx.run(_split(step, replacements), cwd=tree, env=env)
            except OSError as error:
                raise EnvironmentProblem(f"{item.name}: step {index} ({step}) could not start: {error}") from None
            log = ctx.out / "logs" / f"{item.name}.step-{index}.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text(done.stdout, encoding="utf-8")
            ctx.output(log.name, log)
            steps.append({"step": step, "exit": done.returncode})
            if done.returncode != 0:
                ctx.finding(f"{item.name}: step {index} ({step}) exited {done.returncode} at {commit[:12]}; "
                            "nothing was kept")
                return result
        base = tree / recipe.base if recipe.base else tree
        if not base.is_dir():
            ctx.finding(f"{item.name}: the tree at {commit[:12]} has no folder {recipe.base} (the recipe's base)")
            return result
        files, refused = collect(base, [glob_regex(g, "artifacts") for g in recipe.artifacts],
                                 [glob_regex(g, "exclude") for g in recipe.exclude])
        tracked = _modes(tree, recipe.base)
        refused = sorted(set(refused) | {f for f in files if tracked.get(f) == LINK})
        if refused:
            for path in refused:
                ctx.finding(f"{item.name}: {path} is a link or a special file; an archive holds regular files only "
                            "(exclude it)")
            return result
        if not files:
            ctx.finding(f"{item.name}: at {commit[:12]} no file matches {', '.join(recipe.artifacts)}; nothing was "
                        "kept")
            return result
        ctx.progress("pack", msg=f"{item.name}: {len(files)} file(s)")
        entries = pack(made, base, files, {f: _mode(base / f, tracked.get(f)) for f in files}, moment)
        digest = sha256_file(made)
        if item.sha256 and digest != item.sha256:
            ctx.finding(f"{item.name} builds at {commit[:12]} to SHA-256 {digest[:12]}, not the pinned "
                        f"{item.sha256[:12]}; nothing was kept")
            return {**result, "sha256": digest, "matched": False}
        copy_verified(made, target, digest)
        receipt = _receipt(ctx, template, item, repo, key, (commit, tree_hash, moment), steps, entries, target,
                           besides)
        _to_cache(ctx, cache, target, digest)
        write_atomic(_index(ctx) / f"{key}.json", (json.dumps(receipt, indent=1) + "\n").encode("utf-8"))
        return {**_publish(ctx, item, target, receipt, result), "kept": ctx.host_path(tree) if keep else None}
    finally:
        made.unlink(missing_ok=True)
        if not keep:
            shutil.rmtree(parent, ignore_errors=True)


def _receipt(ctx: Context, template: str, item: Input, repo: Path, key: str, at: tuple[str, str, int],
             steps: list[dict], entries: list[dict], archive: Path, besides: dict | None = None) -> dict:
    recipe = item.recipe
    commit, tree, moment = at
    digest = sha256_file(archive)
    beside = {name: {"repo": ctx.host_path(b["path"]), "commit": b["commit"], "tree": b["tree"]}
              for name, b in (besides or {}).items()}
    named = {"beside": {n: {"repo": b.repo, "commit": b.commit} for n, b in recipe.beside.items()}} \
        if recipe.beside else {}
    return {"schema": RECEIPT_SCHEMA, "template": template, "input": item.name,
            "recipe": {"repo": ctx.host_path(repo), "commit": recipe.commit, "steps": recipe.steps,
                       "base": recipe.base, "artifacts": recipe.artifacts, "exclude": recipe.exclude,
                       "jdk": recipe.jdk, "env": recipe.env, **named},
            "recipe_sha256": key, "commit": commit, "tree": tree, "time": moment, "steps": steps,
            **({"beside": beside} if beside else {}),
            "files": entries, "files_sha256": set_digest({e["path"]: e["sha256"] for e in entries}),
            "archive": {"name": item.name, "sha256": digest, "bytes": archive.stat().st_size, "format": FORMAT},
            "pinned": item.sha256, "matched": None if item.sha256 is None else digest == item.sha256,
            "made_with": {"python": platform.python_version()}, "cached": False}


def _publish(ctx: Context, item: Input, target: Path, receipt: dict, result: dict) -> dict:
    digest = receipt["archive"]["sha256"]
    write_sidecar(target, digest)
    path = target.with_name(target.name + ".receipt.json")
    write_atomic(path, (json.dumps(receipt, indent=1) + "\n").encode("utf-8"))
    ctx.output(item.name, target)
    ctx.output(path.name, path)
    return {**result, "sha256": digest, "bytes": receipt["archive"]["bytes"], "cached": receipt["cached"],
            "matched": receipt["matched"], "files": len(receipt["files"]), "archive": ctx.host_path(target),
            "receipt": ctx.host_path(path), "steps": receipt["steps"]}
