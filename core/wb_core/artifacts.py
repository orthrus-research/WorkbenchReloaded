"""The Store: what CI built, kept by content and found by project and tree.

    <store>/files/<sha256>/<name>          the bytes under their real name, written once, never replaced
    <store>/index/<project>/<tree>.json    {project, tree, commit, artifacts: {name: {sha256, bytes, job, run}}}
    <store>/seeded/<sha256>.json           where a file no container builds came from, and why it was added

Only Workbench writes it: ``ci.run`` after a producing job passed, from the copy the run record keeps, and
``wb store add`` for seeded bytes. Every file also goes into the pin cache under its bare SHA-256, so the tools of
commits that read WB_PIN_CACHE find it. Reads re-hash before they hand a path out."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from pathlib import Path


class StoreError(Exception):
    pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_once(source: Path, target: Path, digest: str):
    if target.is_file():
        if sha256(target) != digest:
            raise StoreError(f"{target} holds other bytes than {digest}; the Store never replaces a file")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(f"{target.name}.{os.getpid()}.part")
    shutil.copy2(source, part)
    if sha256(part) != digest:
        part.unlink(missing_ok=True)
        raise StoreError(f"{source} changed while it was copied into the Store")
    part.replace(target)


def put(store: Path, source: Path, digest: str | None = None, pins: Path | None = None) -> Path:
    """Keeps ``source`` by its SHA-256 (and in the pin cache ``pins``); returns the kept file."""
    digest = digest or sha256(source)
    if sha256(source) != digest:
        raise StoreError(f"{source} is not {digest}")
    kept = store / "files" / digest / source.name
    _write_once(source, kept, digest)
    if pins is not None:
        try:
            _write_once(source, pins / digest, digest)
        except OSError:
            pass  # a pin cache that cannot be written only means a tool builds the file itself
    return kept


def record(store: Path, project: str, tree: str, commit: str, artifacts: dict) -> Path:
    """Adds ``artifacts`` ({name: {sha256, bytes, job, run}}) to the index of ``project`` at ``tree``."""
    path = store / "index" / project / f"{tree}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        current = {"project": project, "tree": tree, "commits": [], "artifacts": {}}
    if commit not in current["commits"]:
        current["commits"].append(commit)
    for name, item in artifacts.items():
        known = current["artifacts"].get(name)
        if known and known["sha256"] != item["sha256"]:
            # Two builds of one tree differ: a bytes problem worth seeing, never a silent replacement.
            current.setdefault("disagreements", []).append({"name": name, "kept": known["sha256"],
                                                            "other": item["sha256"], "run": item.get("run")})
            continue
        current["artifacts"][name] = item
    part = path.with_name(path.name + f".{os.getpid()}.part")
    part.write_text(json.dumps(current, indent=1), encoding="utf-8")
    part.replace(path)
    return path


def find(store: Path, project: str, tree: str) -> dict | None:
    """The index of ``project`` at ``tree``, or None when CI built nothing there."""
    path = store / "index" / project / f"{tree}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def path_of(store: Path, digest: str, name: str) -> Path:
    """The kept file for ``digest``, re-hashed before it is handed out."""
    kept = store / "files" / digest / name
    if not kept.is_file():
        raise StoreError(f"the Store has no {name} with SHA-256 {digest}")
    if sha256(kept) != digest:
        raise StoreError(f"the Store's {kept} no longer hashes to {digest}; it was changed outside Workbench")
    return kept


def seed(store: Path, source: Path, label: str, origin: str, reason: str, pins: Path | None = None) -> dict:
    """Adds bytes no container can build, with where they came from and why; returns their record."""
    digest = sha256(source)
    kept = put(store, source, digest, pins)
    note = {"sha256": digest, "name": source.name, "as": label, "source": origin, "reason": reason,
            "bytes": kept.stat().st_size, "added": time.strftime("%Y-%m-%dT%H:%M:%S")}
    path = store / "seeded" / f"{digest}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(note, indent=1), encoding="utf-8")
    return note
