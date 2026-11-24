"""The Store: what CI built, kept by content and found by project and tree.

    <store>/files/<sha256>/<name>          the bytes under their real name, written once, never replaced
    <store>/index/<project>/<tree>.json    {project, tree, commit, artifacts: {name: {sha256, bytes, job, run}}}
    <store>/seeded/<sha256>.json           where a file no container builds came from, and why it was added

Only Workbench writes it: ``ci.run`` after a producing job passed, from the copy the run record keeps, and
``wb store add`` for seeded bytes. Every file also goes into the pin cache under its bare SHA-256, so the tools of
commits that read WB_PIN_CACHE find it. Reads re-hash before they hand a path out.

Two runs of one project may write at once. A file is written under a name of its own and moved into place, and two
writers of one SHA-256 write the same bytes; an index entry is read, merged and written under its own lock, so one
run's commit or artifact is never lost to another's write."""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from pathlib import Path

from . import filelock

INDEX_LOCK_STALE = 60.0  # seconds: an index entry's read, merge and write take milliseconds


class StoreError(Exception):
    pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _settled_sha256(path: Path, attempts: int = 40) -> str:
    """``sha256(path)``, retried while Windows denies the read because another writer is putting the same file in
    place (a write-once file is replaced only by its own bytes, so the wait is short)."""
    for attempt in range(attempts):
        try:
            return sha256(path)
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.02 * (attempt + 1))
    raise AssertionError("unreachable")


def _write_once(source: Path, target: Path, digest: str):
    if target.is_file():
        if _settled_sha256(target) != digest:
            raise StoreError(f"{target} holds other bytes than {digest}; the Store never replaces a file")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    part = filelock.part_of(target)
    try:
        shutil.copy2(source, part)
        if sha256(part) != digest:
            raise StoreError(f"{source} changed while it was copied into the Store")
        try:
            filelock.replace(part, target)
        except PermissionError:
            # Another writer put the same file in place and a reader holds it open: the bytes are there already.
            if not (target.is_file() and _settled_sha256(target) == digest):
                raise
    finally:
        part.unlink(missing_ok=True)


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
    with _entry_lock(path):
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            current = {"project": project, "tree": tree, "commits": [], "artifacts": {}}
        except (OSError, ValueError) as error:
            raise StoreError(f"the Store's index entry {path} cannot be read ({error}); it was changed outside "
                             "Workbench") from None
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
        filelock.write_json(path, current)
    return path


def _entry_lock(path: Path):
    """The lock one index entry's read, merge and write hold."""
    return filelock.exclusive(path.with_name(path.name + ".lock"), stale=INDEX_LOCK_STALE)


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
    filelock.write_json(store / "seeded" / f"{digest}.json", note)
    return note


def _write_json(path: Path, value: dict):
    filelock.write_json(path, value)


def entries(store: Path, project: str | None = None) -> list[dict]:
    """Every index entry, or one project's, oldest tree first by when it was recorded."""
    folder = store / "index"
    paths = sorted((folder / project).glob("*.json") if project else folder.glob("*/*.json"),
                   key=lambda p: p.stat().st_mtime)
    found = []
    for path in paths:
        try:
            found.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return found


def note_verified(store: Path, project: str, tree: str, verdict: dict) -> dict:
    """Records a cold rebuild's verdict on ``project`` at ``tree``: {at, job, reproduced, differ: [...]}."""
    path = store / "index" / project / f"{tree}.json"
    with _entry_lock(path):
        current = json.loads(path.read_text(encoding="utf-8"))
        current["verified"] = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), **verdict}
        _write_json(path, current)
    return current


def export(store: Path, target: Path, project: str | None = None, trees: set[str] | None = None) -> dict:
    """Copies index entries (one project's, or only ``trees`` of it) with the files they name and every seeded
    note into ``target``, laid out as a Store, so ``import_from`` can take them on another machine."""
    files = notes = 0
    chosen = [e for e in entries(store, project) if not trees or e["tree"] in trees]
    for entry in chosen:
        for name, item in entry["artifacts"].items():
            _write_once(path_of(store, item["sha256"], name), target / "files" / item["sha256"] / name,
                        item["sha256"])
            files += 1
        _write_json(target / "index" / entry["project"] / f"{entry['tree']}.json", entry)
    for note in sorted((store / "seeded").glob("*.json")):
        meta = json.loads(note.read_text(encoding="utf-8"))
        _write_once(path_of(store, meta["sha256"], meta["name"]), target / "files" / meta["sha256"] / meta["name"],
                    meta["sha256"])
        _write_json(target / "seeded" / note.name, meta)
        notes += 1
    return {"entries": len(chosen), "files": files, "seeded": notes}


def import_from(store: Path, source: Path, pins: Path | None = None) -> dict:
    """Takes a folder ``export`` wrote: every file re-hashed into the Store (a file that is not its SHA-256 is
    refused), its index entries merged as ``record`` merges a build, and seeded notes this Store lacks."""
    files = 0
    for entry in entries(source):
        for name, item in entry["artifacts"].items():
            put(store, source / "files" / item["sha256"] / name, item["sha256"], pins)
            files += 1
        for commit in entry.get("commits") or [None]:
            if commit:
                record(store, entry["project"], entry["tree"], commit, entry["artifacts"])
    notes = 0
    for note in sorted((source / "seeded").glob("*.json")):
        meta = json.loads(note.read_text(encoding="utf-8"))
        put(store, source / "files" / meta["sha256"] / meta["name"], meta["sha256"], pins)
        if not (store / "seeded" / note.name).is_file():
            _write_json(store / "seeded" / note.name, meta)
            notes += 1
    return {"files": files, "seeded": notes}
