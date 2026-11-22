"""build: freezing built artifacts under immutable labels, and staging another project's artifacts as inputs.

A freeze store is a folder of labels, one folder each, and ``manifest.json`` beside them::

    <store>/<label>/<file>              # a frozen artifact: the same label and name always mean the same bytes
    <store>/<label>/<file>.sha256       # its digest, in sha256sum's format
    <store>/manifest.json               # {"schema": "wb.freeze.v1", "labels": {<label>: {"artifacts": {<file>:
                                        #   {"sha256", "bytes", "commit", "dirty", "frozen"}}}}}
    <store>/manifest.lock               # every freeze into the store holds it while it writes

A label is ``<name>-<commit>[-dirty-<hash>]`` by default (the ``label`` template; ``hash`` is the digest of the
artifacts frozen together), so a clean build is named by its commit, and a build with uncommitted changes, whose
bytes the commit does not fix, also by its own bytes. A label can hold artifacts from several builds; an artifact
already frozen under it is never replaced by other bytes.

A stage lock is JSON, kept in the project that consumes the artifacts::

    {
      "repository": "https://example.invalid/lib.git",  # optional: where the artifacts come from, for the record
      "commit": "<40 hex>",                             # the other checkout must be at this commit, clean
      "tree": "<40 hex>",                               # optional: and have this tree
      "artifacts": [
        {"artifact": "build/libs/lib-1.0.jar",          # relative to that checkout
         "sha256": "<64 hex>",                          # its exact bytes
         "destination": "inputs/lib-1.0.jar"}           # relative to the folder staged into
      ]
    }

Staging places every artifact or none: each is copied beside its destination and read back first, and only then do
the copies replace the destinations; if a replacement fails, the destinations already replaced get their previous
bytes back.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import string
import time
import uuid
from pathlib import Path

from wb_core.module import Context, EnvironmentProblem, ParamError, sha256_file

from . import checkout as checkouts
from .files import copy_verified, locked, set_digest, write_atomic, write_sidecar

FREEZE_SCHEMA = "wb.freeze.v1"
LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}")
LABEL_FIELDS = ("name", "commit", "hash", "dirty")
COMMIT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
SHA256 = re.compile(r"[0-9a-f]{64}")
LOCK_KEYS = {"repository", "commit", "tree", "artifacts"}
ARTIFACT_KEYS = {"artifact", "sha256", "destination"}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------------------------------------
# freeze

def _label(template: str, values: dict) -> str:
    """The label a template names, from {name}, {commit}, {hash} and {dirty} only (a format spec such as
    {commit:.12} shortens one); it must make one folder name."""
    try:
        fields = [(field, conversion) for _, field, _, conversion in string.Formatter().parse(template)
                  if field is not None]
    except ValueError as error:
        raise ParamError(f"label {template!r}: {error}") from None
    for field, conversion in fields:
        if field not in LABEL_FIELDS or conversion:
            raise ParamError(f"label {template!r}: {{{field}}} is not one of {{name}}, {{commit}}, {{hash}}, "
                             "{dirty}")
        if field == "name" and not values["name"]:
            raise ParamError("the label uses {name}: give name=, or a label without it")
        if field == "commit" and not values["commit"]:
            raise ParamError("the label names the commit: give repo= (the checkout built) or commit=")
    try:
        label = template.format(**values)
    except (ValueError, KeyError, IndexError) as error:
        raise ParamError(f"label {template!r}: {error}") from None
    if not LABEL.fullmatch(label):
        raise ParamError(f"label {label!r} is not a folder name (letters, digits, . _ + -; 128 at most)")
    return label


def _manifest(path: Path) -> dict:
    if not path.is_file():
        return {"schema": FREEZE_SCHEMA, "labels": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise EnvironmentProblem(f"{path} cannot be read as JSON ({error}); a freeze would lose it") from None
    if not isinstance(data, dict) or data.get("schema") != FREEZE_SCHEMA or not isinstance(data.get("labels"), dict):
        raise ParamError(f"{path} is not a {FREEZE_SCHEMA} manifest: the store is another kind of folder")
    return data


def freeze(ctx: Context, artifacts: list, store: str, name: str, label: str, repo=None, commit=None,
           dirty: bool = False):
    files = [Path(a) for a in artifacts]
    if not files:
        raise ParamError("artifacts: name at least one built file")
    names = [f.name for f in files]
    twice = sorted({n for n in names if names.count(n) > 1})
    if twice:
        raise ParamError(f"two artifacts are named {twice[0]}; a label holds one file of each name")
    for f in files:
        if not f.is_file():
            raise ParamError(f"{ctx.host_path(f)} is not a file")
    if repo and commit:
        raise ParamError("give repo (the checkout built) or commit, not both")
    built_from, changed = (commit.lower() if commit else None), bool(dirty)
    if repo:
        found = checkouts.state(ctx, Path(repo))
        built_from, changed = found.commit, found.dirty
        if changed:
            ctx.log.info(f"{ctx.host_path(repo)} has {len(found.changes)} uncommitted change(s), first: "
                         f"{found.changes[0].strip()}")
    digests = {f.name: sha256_file(f) for f in files}
    together = set_digest(digests)
    chosen = _label(label, {"name": name, "commit": built_from or "", "hash": together,
                            "dirty": f"-dirty-{together[:12]}" if changed else ""})
    root = Path(store)
    folder = root / chosen
    ctx.progress("freeze", msg=chosen)
    with locked(root / "manifest.lock", ctx):
        manifest = _manifest(root / "manifest.json")
        known = dict((manifest["labels"].get(chosen) or {}).get("artifacts") or {})
        refused = []
        for f in files:
            target = folder / f.name
            if target.exists() and not target.is_file():
                raise ParamError(f"{ctx.host_path(target)} is a folder, not a frozen artifact")
            recorded = (known.get(f.name) or {}).get("sha256")
            held = sha256_file(target) if target.is_file() else None
            other = next((d for d in (recorded, held) if d and d != digests[f.name]), None)
            if other:
                refused.append({"name": f.name, "sha256": digests[f.name], "frozen": other})
        if refused:
            for item in refused:
                ctx.finding(f"{chosen}/{item['name']} is already frozen as other bytes ({item['frozen'][:12]}, "
                            f"not {item['sha256'][:12]}): a label never changes")
            ctx.log.warning("nothing was frozen")
            return {"label": chosen, "store": ctx.host_path(root), "commit": built_from, "dirty": changed,
                    "hash": together, "artifacts": [], "refused": refused}
        results = []
        for f in files:
            target = folder / f.name
            if target.is_file():
                how = "present"
            else:
                copy_verified(f, target, digests[f.name])
                how = "frozen"
            write_sidecar(target, digests[f.name])
            known[f.name] = known.get(f.name) or {"sha256": digests[f.name], "bytes": target.stat().st_size,
                                                  "commit": built_from, "dirty": changed, "frozen": _now()}
            results.append({"name": f.name, "sha256": digests[f.name], "bytes": target.stat().st_size,
                            "path": ctx.host_path(target), "state": how})
        manifest["labels"][chosen] = {"artifacts": dict(sorted(known.items()))}
        manifest["labels"] = dict(sorted(manifest["labels"].items()))
        write_atomic(root / "manifest.json", (json.dumps(manifest, indent=2) + "\n").encode("utf-8"))
    for item in results:
        ctx.output(item["name"], folder / item["name"])
    new = sum(1 for r in results if r["state"] == "frozen")
    if not new:
        ctx.summary(f"{chosen} already holds these {len(results)} artifact(s)")
    else:
        ctx.summary(f"{new} artifact(s) frozen as {chosen}"
                    + (f", {len(results) - new} already there" if new < len(results) else ""))
    return {"label": chosen, "store": ctx.host_path(root), "commit": built_from, "dirty": changed, "hash": together,
            "artifacts": results}


# ---------------------------------------------------------------------------------------------
# stage

def _relative(base: Path, value, what: str) -> Path:
    """``base / value`` for a relative path that stays inside ``base``."""
    if not isinstance(value, str) or not value or value.startswith(("/", "\\")) or ":" in value \
            or ".." in re.split(r"[\\/]", value):
        raise ParamError(f"{what} {value!r} must be a relative path inside its folder")
    path = (base / value).resolve()
    if not path.is_relative_to(base.resolve()):
        raise ParamError(f"{what} {value!r} leaves its folder")
    return path


def _lock(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        raise ParamError(f"{path.name} is not a JSON lock: {error}") from None
    if not isinstance(data, dict):
        raise ParamError(f"{path.name}: a lock is a JSON object")
    unknown = sorted(set(data) - LOCK_KEYS)
    if unknown:
        raise ParamError(f"{path.name}: unknown key(s) {', '.join(unknown)} (known: {', '.join(sorted(LOCK_KEYS))})")
    if not isinstance(data.get("commit"), str) or not COMMIT.fullmatch(data["commit"]):
        raise ParamError(f"{path.name}: commit must be a full commit id")
    if data.get("tree") is not None and (not isinstance(data["tree"], str) or not COMMIT.fullmatch(data["tree"])):
        raise ParamError(f"{path.name}: tree must be a full tree id")
    if data.get("repository") is not None and not isinstance(data["repository"], str):
        raise ParamError(f"{path.name}: repository is text (where the artifacts come from)")
    entries = data.get("artifacts")
    if not isinstance(entries, list) or not entries:
        raise ParamError(f"{path.name}: artifacts must list at least one artifact")
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or set(entry) != ARTIFACT_KEYS:
            raise ParamError(f"{path.name}: artifact {index + 1} needs exactly {', '.join(sorted(ARTIFACT_KEYS))}")
        if not isinstance(entry["sha256"], str) or not SHA256.fullmatch(entry["sha256"]):
            raise ParamError(f"{path.name}: artifact {index + 1}: sha256 must be 64 lowercase hex digits")
    return data


def _place(items: list[tuple[Path, Path, str]]) -> list[str]:
    """Copies each (source, destination, sha256) into place, all or none; returns each one's state. Every copy is
    made and read back beside its destination, and every destination it replaces is backed up, before the first
    destination changes."""
    states: list[str] = []
    pending: list[tuple[Path, Path]] = []
    backups: dict[Path, Path | None] = {}
    created: list[Path] = []
    kept: set[Path] = set()
    token = uuid.uuid4().hex[:12]
    ready = False
    try:
        for source, destination, expected in items:
            if destination.is_file() and sha256_file(destination) == expected:
                states.append("unchanged")
                continue
            temporary = destination.with_name(f".{destination.name}.{token}.stage")
            try:
                created.extend(p for p in (destination.parent, *destination.parent.parents) if not p.exists())
                destination.parent.mkdir(parents=True, exist_ok=True)
                pending.append((temporary, destination))
                shutil.copyfile(source, temporary)
                backups[destination] = None
                if destination.exists():
                    backups[destination] = destination.with_name(f".{destination.name}.{token}.backup")
                    shutil.copyfile(destination, backups[destination])
            except OSError as error:
                raise EnvironmentProblem(f"could not prepare {destination}: {error}; nothing was staged") from None
            if sha256_file(temporary) != expected:
                raise EnvironmentProblem(f"{source.name} changed while it was staged; nothing was staged")
            states.append("staged")
        done: list[Path] = []
        try:
            for temporary, destination in pending:
                os.replace(temporary, destination)
                done.append(destination)
        except OSError as error:
            failed = []
            for destination in reversed(done):
                backup = backups[destination]
                try:
                    if backup is None:
                        destination.unlink()
                    else:
                        os.replace(backup, destination)
                except OSError as undo:
                    if backup is not None:
                        kept.add(backup)
                    failed.append(f"{destination}: {undo}" + (f" (its previous bytes are in {backup})" if backup
                                                              else ""))
            if failed:
                raise EnvironmentProblem(f"staging failed ({error}) and could not restore every destination: "
                                         + "; ".join(failed)) from None
            raise EnvironmentProblem(f"staging failed ({error}); every destination has its previous bytes") from None
        ready = True
    finally:
        for temporary, _ in pending:
            temporary.unlink(missing_ok=True)
        for backup in backups.values():
            if backup is not None and backup not in kept:
                backup.unlink(missing_ok=True)
        if not ready:
            for folder in sorted(set(created), key=lambda p: len(p.parts), reverse=True):
                try:
                    folder.rmdir()
                except OSError:
                    pass
    return states


def stage(ctx: Context, lock: str, checkout: str, into=None):
    data = _lock(Path(lock))
    source_root, target_root = Path(checkout), Path(into) if into else ctx.out
    label = data.get("repository") or source_root.name
    items, seen = [], set()
    for entry in data["artifacts"]:
        source = _relative(source_root, entry["artifact"], "artifact")
        destination = _relative(target_root, entry["destination"], "destination")
        key = str(destination).casefold()
        if key in seen:
            raise ParamError(f"two artifacts go to {entry['destination']}")
        seen.add(key)
        if destination.exists() and not destination.is_file():
            raise ParamError(f"destination {entry['destination']} is a folder")
        items.append((entry, source, destination))
    ctx.progress("check", msg=f"{label} at {data['commit'][:12]}")
    found = checkouts.state(ctx, source_root, untracked=False)
    problems = []
    if found.commit != data["commit"]:
        problems.append(f"{source_root.name} is at {found.commit[:12]}, not the locked {data['commit'][:12]}")
    elif data.get("tree") and found.tree != data["tree"]:
        problems.append(f"{source_root.name} has tree {found.tree[:12]}, not the locked {data['tree'][:12]}")
    if found.dirty:
        problems.append(f"{source_root.name} has {len(found.changes)} tracked modification(s), first: "
                        f"{found.changes[0].strip()}")
    for entry, source, _ in items:
        if not source.is_file():
            problems.append(f"{entry['artifact']} is missing from {source_root.name}")
            continue
        actual = sha256_file(source)
        if actual != entry["sha256"]:
            problems.append(f"{entry['artifact']} is {actual[:12]}, not the locked {entry['sha256'][:12]}")
    result = {"repository": data.get("repository"), "commit": data["commit"], "tree": found.tree,
              "checkout": ctx.host_path(source_root), "into": ctx.host_path(target_root), "staged": []}
    if problems:
        for problem in problems:
            ctx.finding(problem)
        ctx.log.warning("nothing was staged")
        return result
    ctx.progress("stage", msg=f"{len(items)} artifact(s)")
    states = _place([(source, destination, entry["sha256"]) for entry, source, destination in items])
    for (entry, source, destination), how in zip(items, states, strict=True):
        result["staged"].append({"artifact": entry["artifact"], "destination": ctx.host_path(destination),
                                 "sha256": entry["sha256"], "bytes": destination.stat().st_size, "state": how})
    placed = states.count("staged")
    ctx.summary(f"staged {placed} artifact(s) from {label} at {data['commit'][:12]}"
                + (f", {len(states) - placed} already in place" if placed < len(states) else ""))
    return result
