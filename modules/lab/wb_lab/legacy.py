"""lab.freeze and lab.thaw: the old route's instances kept as evidence, read-only, beside the new route's.

A legacy instance is a folder the project launchers prepared (``.local/cubic-standalone/<name>`` of a checkout,
with standalone.json and server/ and client/ in it) and lab.build did not: it has no instance.json receipt, so
nothing can play it again and retention never touches it. Freezing one, with ``apply=true``:

1. records a manifest of every file: its size, its modification time and its SHA-256 (a file larger than
   ``hash_limit`` MiB by size and time only), with whether it was read-only already and its link count;
2. imports its world (``server/world``, ``server/world.cubature-cache`` and the Distant Horizons server data) into
   the lab's world store (``<instances>/.store/worlds``) by its world key: the seed, the layout scale and the
   SHA-256 of the generator's definitions, written by the Cubature JAR standalone.json names. The store keeps a copy
   by content; without a world store the key and the world's path are recorded instead;
3. imports its ``tours/`` and ``runs/`` folders, copied, into one session folder (``kind = "legacy"``) of the
   lab's sessions and keeps that session with lab.keep, so retention never removes it;
4. marks every file read-only (the file attribute; a folder's attribute does not stop writes on Windows) and
   writes the manifest to ``<lab home>/legacy/<id>/frozen.json``, where ``<id>`` names the instance by its path.

Nothing is moved or deleted, and no link is followed: a link inside an instance is recorded, not entered, and a
folder that is a link or lies under one is refused, because freezing it would freeze what another checkout's link
points at. Without ``apply`` the command only inventories (counts, sizes, world keys, what would be imported),
hashing nothing. A second run of a frozen instance changes nothing.

The undo is lab.thaw: it clears the read-only mark from exactly the files the freeze marked (those that were not
read-only before and still have the size and time the manifest recorded). The imported world and session are
copies; they stay until their session is un-kept (its kept.json removed) and retention removes them.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import time
from datetime import UTC, datetime
from pathlib import Path

from wb_core.module import Context, ParamError

from . import instance as instances
from . import machine as machines
from . import run as runs
from .store import Layout, Store, StoreError
from .worlds import WORLD_PARTS

SCHEMA = "wb.lab.frozen/1"
MANIFEST = "frozen.json"
DESCRIPTION = "standalone.json"
IMPORTED = ("tours", "runs")
KIND = "legacy"


def is_link(path) -> bool:
    path = Path(path)
    if path.is_symlink() or os.path.isjunction(path):
        return True
    try:
        return bool(getattr(os.lstat(path), "st_reparse_tag", 0))
    except OSError:
        return False


def _under_link(path: Path) -> Path | None:
    """The first link among ``path`` and its parents, if any."""
    for probe in [path, *path.parents]:
        if is_link(probe):
            return probe
    return None


def walk(root: Path):
    """Every file under ``root`` as (path, relative name, lstat), and every link as (path, relative name, None);
    links are never entered."""
    stack = [root]
    while stack:
        folder = stack.pop()
        with os.scandir(folder) as entries:
            for entry in sorted(entries, key=lambda e: e.name):
                path = Path(entry.path)
                rel = path.relative_to(root).as_posix()
                if entry.is_symlink() or entry.is_junction() or is_link(path):
                    yield path, rel, None
                elif entry.is_dir(follow_symlinks=False):
                    stack.append(path)
                else:
                    yield path, rel, entry.stat(follow_symlinks=False)


def is_legacy(folder: Path) -> bool:
    """A folder the project launchers prepared: no receipt, and the old route's description or its two sides."""
    if not folder.is_dir() or is_link(folder) or (folder / instances.RECEIPT).exists():
        return False
    return (folder / DESCRIPTION).is_file() or ((folder / "server").is_dir() and (folder / "client").is_dir())


def legacy_instances(folder: Path) -> tuple[list[Path], list[dict]]:
    """The legacy instances ``folder`` is or holds, and what was passed over (links, receipts, other folders)."""
    if is_legacy(folder):
        return [folder], []
    found, skipped = [], []
    for entry in sorted(folder.iterdir(), key=lambda p: p.name):
        if is_link(entry):
            skipped.append({"name": entry.name, "why": "a link (not entered)"})
        elif not entry.is_dir():
            continue
        elif (entry / instances.RECEIPT).exists():
            skipped.append({"name": entry.name, "why": f"lab.build made it ({instances.RECEIPT})"})
        elif is_legacy(entry):
            found.append(entry)
        else:
            skipped.append({"name": entry.name, "why": "not an instance (no standalone.json, server/ and client/)"})
    return found, skipped


def instance_id(instance: Path) -> str:
    """A stable name for an instance by its path: its folder name and a hash of where it is."""
    where = os.path.normcase(os.path.abspath(instance))
    return f"{instance.name}-{hashlib.sha256(where.encode('utf-8')).hexdigest()[:10]}"


def world_key(description: dict) -> tuple[dict | None, str | None]:
    """What a legacy world depends on, as the old route's store keyed it: the seed, the layout scale and the SHA-256
    of the generator's definitions; and the build that wrote it (the Cubature JAR's SHA-256)."""
    generator = description.get("generator") or {}
    if "seed" not in description:
        return None, None
    definitions = json.dumps(generator.get("definitions") or {}, sort_keys=True)
    key = {"seed": description.get("seed"), "scale": generator.get("layoutScale"),
           "definitions": hashlib.sha256(definitions.encode()).hexdigest()}
    return key, ((description.get("cubicJar") or {}).get("sha256") or None)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_only(st) -> bool:
    return not (st.st_mode & stat.S_IWRITE)


def _stamp(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, UTC).isoformat(timespec="seconds")


def inventory(instance: Path, hash_limit: int | None) -> dict:
    """Every file of the instance (hashed up to ``hash_limit`` bytes; None hashes nothing) and every link in it."""
    files, links, total = {}, [], 0
    for path, rel, st in walk(instance):
        if st is None:
            links.append({"path": rel, "target": os.readlink(path) if path.is_symlink() else _target(path)})
            continue
        total += st.st_size
        entry = {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "read_only": _read_only(st)}
        if st.st_nlink > 1:
            entry["links"] = st.st_nlink
        if hash_limit is not None:
            entry["sha256"] = _sha256(path) if st.st_size <= hash_limit else None
        files[rel] = entry
    return {"files": files, "links": links, "bytes": total}


def _target(path: Path) -> str:
    try:
        return os.readlink(path)
    except OSError:
        return ""


def _description(instance: Path) -> dict:
    try:
        return json.loads((instance / DESCRIPTION).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _world_store(ctx: Context) -> Store | None:
    """The lab's world store (the store lab.build keeps its templates in), if there is one."""
    try:
        root = instances._lab_root(ctx, instances.Machine()) / ".store"
    except Exception:  # noqa: BLE001 - no lab home: no store, the key is recorded instead
        return None
    return Store(root, Layout(world_parts=list(WORLD_PARTS))) if root.is_dir() else None


def _import_world(ctx: Context, instance: Path, ident: str, key: dict | None, writer: str | None) -> dict:
    world = instance / WORLD_PARTS[0]
    record = {"key": key, "writer": writer, "path": str(world) if world.is_dir() else None, "label": None,
              "store": None}
    if not world.is_dir() or key is None:
        record["why"] = "no world" if not world.is_dir() else "no world key (standalone.json names no seed)"
        return record
    store = _world_store(ctx)
    if store is None:
        record["why"] = "no world store: the key and the world's path are recorded"
        return record
    label = f"legacy-{ident}"
    record.update(label=label, store=str(store.root))
    if (store.worlds / label / "world.json").is_file():
        record["files"] = len(store.world(label)["files"])
        return record
    try:
        made = store.snapshot(instance, label, {"key": key, "cubature": writer, "from": str(instance)})
    except StoreError as error:
        record.update(label=None, why=str(error))
        return record
    record["files"], record["bytes"] = len(made["files"]), made["bytes"]
    return record


def _copy_tree(source: Path, destination: Path) -> tuple[int, int, list[str]]:
    """Copies a folder, never entering a link (a link is recorded, not copied)."""
    count = size = 0
    passed = []
    for path, rel, st in walk(source):
        if st is None:
            passed.append(rel)
            continue
        target = destination / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        os.chmod(target, stat.S_IREAD | stat.S_IWRITE)
        count, size = count + 1, size + st.st_size
    return count, size, passed


def _session_id(instance: Path, parts: list[Path]) -> str:
    stamps = [p.stat().st_mtime for part in parts for p in [part, *part.iterdir()]]
    when = time.strftime("%Y%m%d-%H%M%S", time.localtime(min(stamps)))
    return f"{when}-{hashlib.sha256(instance_id(instance).encode()).hexdigest()[:6]}"


def _import_sessions(ctx: Context, instance: Path, ident: str, description: dict, manifest_path: Path) -> dict | None:
    parts = [instance / name for name in IMPORTED if (instance / name).is_dir() and not is_link(instance / name)]
    if not parts:
        return None
    root = runs.sessions_root(ctx)
    session = _session_id(instance, parts)
    folder = root / session
    record_path = folder / runs.SESSION
    if record_path.is_file():
        record = json.loads(record_path.read_text(encoding="utf-8"))
        if (record.get("legacy") or {}).get("id") != ident:
            raise ParamError(f"session {session} exists and was not imported from {instance}")
        return {"session": session, "folder": str(folder), "kept": bool(record.get("kept")),
                "copied": record["legacy"]["copied"]}
    staging = root / f".{session}.importing"
    if staging.exists():
        from wb.platform import remove_tree
        remove_tree(staging)  # a copy an interrupted import left; never the instance
    copied = {}
    for part in parts:
        count, size, passed = _copy_tree(part, staging / part.name)
        copied[part.name] = {"files": count, "bytes": size, "links_passed": passed}
    launch = description.get("launch") or {}  # no template, maybe no launch file: "" (kept.json names strings)
    stamps = [p.stat().st_mtime for part in parts for p in part.iterdir()] or [part.stat().st_mtime for part in parts]
    record = {
        "schema": runs.SCHEMA, "id": session, "kind": KIND, "state": "imported",
        "started": _stamp(min(stamps)), "ended": _stamp(max(stamps)), "job": ctx.job,
        "evidence": False, "not_evidence": "imported from a legacy instance, which has no instance.json receipt",
        "instance": {"name": instance.name, "path": str(instance), "template": "", "receipt_sha256": None},
        "launch": {"sha256": launch.get("sha256") or "", "file": None,
                   **{k: launch.get(k) for k in ("project", "launch", "commit", "set")}},
        "members": description.get("modHashes") or {},
        "jdk": (description.get("javaRuntime") or {}).get("version"),
        "legacy": {"id": ident, "instance": str(instance), "manifest": str(manifest_path),
                   "description_sha256": _sha256(instance / DESCRIPTION) if (instance / DESCRIPTION).is_file()
                   else None, "copied": copied, "purpose": description.get("purpose")},
        "kept": None,
    }
    if (instance / DESCRIPTION).is_file():
        shutil.copy2(instance / DESCRIPTION, staging / DESCRIPTION)
        os.chmod(staging / DESCRIPTION, stat.S_IREAD | stat.S_IWRITE)
    runs.write_json(staging / runs.SESSION, record)
    os.replace(staging, folder)
    runs.keep(ctx, session, f"evidence from the legacy instance {instance.name} ({instance}), frozen read-only")
    return {"session": session, "folder": str(folder), "kept": True, "copied": copied}


def _manifest_path(ctx: Context, instance: Path) -> Path:
    return machines.home(ctx) / "legacy" / instance_id(instance) / MANIFEST


def _mark(instance: Path, files: dict) -> int:
    marked = 0
    for rel, entry in files.items():
        if entry["read_only"]:
            continue
        os.chmod(instance / rel, stat.S_IREAD)
        entry["marked"] = True
        marked += 1
    return marked


def freeze_one(ctx: Context, instance: Path, hash_limit: int) -> dict:
    ident = instance_id(instance)
    manifest_path = _manifest_path(ctx, instance)
    earlier = {}
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("state") == "frozen":
            return {"instance": str(instance), "id": ident, "state": "frozen already", "manifest": str(manifest_path),
                    "bytes": manifest["bytes"], "files": len(manifest["files"]),
                    "world": manifest.get("world"), "session": manifest.get("session")}
        earlier = manifest.get("files") or {}  # an interrupted freeze: what was writable then is still ours to mark
    ctx.progress("freeze", msg=f"{instance.name}: manifest")
    found = inventory(instance, hash_limit)
    for rel, entry in found["files"].items():
        if rel in earlier and not earlier[rel]["read_only"]:
            entry["read_only"] = False
    description = _description(instance)
    key, writer = world_key(description)
    ctx.progress("freeze", msg=f"{instance.name}: world and sessions")
    world = _import_world(ctx, instance, ident, key, writer)
    session = _import_sessions(ctx, instance, ident, description, manifest_path)
    manifest = {
        "schema": SCHEMA, "id": ident, "instance": str(instance), "name": instance.name, "state": "freezing",
        "frozen": runs.now(), "job": ctx.job, "hash_limit": hash_limit,
        "files": found["files"], "links": found["links"], "bytes": found["bytes"],
        "world": world, "session": session,
        "undo": (f"wb run lab.thaw folder={instance}: clears the read-only mark from the files this manifest marks "
                 "(marked = true), each only while its size and time are the ones recorded. The world "
                 f"({world.get('label') or 'not imported'}) and the session "
                 f"({(session or {}).get('session') or 'none'}) are copies: remove the session's kept.json and "
                 "retention removes them."),
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    runs.write_json(manifest_path, manifest)  # first, so an interrupted freeze can be undone
    manifest["marked"] = _mark(instance, manifest["files"])
    manifest["state"] = "frozen"
    runs.write_json(manifest_path, manifest)
    return {"instance": str(instance), "id": ident, "state": "frozen", "manifest": str(manifest_path),
            "bytes": found["bytes"], "files": len(found["files"]), "marked": manifest["marked"],
            "links": len(found["links"]), "world": world, "session": session}


def survey_one(instance: Path) -> dict:
    found = inventory(instance, None)
    description = _description(instance)
    key, writer = world_key(description)
    return {"instance": str(instance), "id": instance_id(instance), "state": "inventoried",
            "bytes": found["bytes"], "files": len(found["files"]), "links": len(found["links"]),
            "read_only": sum(1 for e in found["files"].values() if e["read_only"]),
            "world": {"key": key, "writer": writer, "present": (instance / WORLD_PARTS[0]).is_dir()},
            "imports": {name: (instance / name).is_dir() for name in IMPORTED},
            "launch": (description.get("launch") or {}).get("sha256")}


def freeze(ctx: Context, folder: str, apply: bool = False, hash_limit: int = 256):
    root = Path(folder)
    if not root.is_dir():
        raise ParamError(f"folder: {root} is not a folder")
    found, skipped = legacy_instances(root)
    if apply:
        link = _under_link(root.absolute())
        if link is not None:
            raise ParamError(f"folder: {root} is reached through the link {link}; a freeze through a link would mark "
                             "what it points at, which other checkouts share. Inventory it (apply=false), or name "
                             "the folder itself")
    rows = []
    for index, instance in enumerate(found):
        ctx.check_cancel()
        ctx.progress("freeze", index / max(1, len(found)), instance.name)
        rows.append(freeze_one(ctx, instance, hash_limit << 20) if apply else survey_one(instance))
    total = sum(r["bytes"] for r in rows)
    worlds = sum(1 for r in rows if (r.get("world") or {}).get("label")) if apply else \
        sum(1 for r in rows if r["world"]["present"])
    sessions = sum(1 for r in rows if r.get("session")) if apply else \
        sum(1 for r in rows if any(r["imports"].values()))
    verb = "frozen" if apply else "found"
    ctx.summary(f"{len(rows)} legacy instance(s) {verb} under {root} ({total / 2**30:.2f} GiB, "
                f"{sum(r['files'] for r in rows)} files); worlds {'imported' if apply else 'present'} {worlds}, "
                f"{'sessions imported and kept' if apply else 'with tours/ or runs/'} {sessions}"
                + (f"; {len(skipped)} passed over" if skipped else ""))
    return {"instances": rows, "skipped": skipped, "bytes": total, "applied": apply}


def thaw(ctx: Context, folder: str):
    """Undoes lab.freeze: clears the read-only mark from the files a freeze marked, from its manifests."""
    root = Path(folder)
    if not root.is_dir():
        raise ParamError(f"folder: {root} is not a folder")
    found, _ = legacy_instances(root)
    rows = []
    for instance in found:
        manifest_path = _manifest_path(ctx, instance)
        if not manifest_path.is_file():
            continue
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        cleared, changed = 0, []
        for rel, entry in manifest["files"].items():
            if not entry.get("marked"):
                continue
            path = instance / rel
            try:
                st = os.lstat(path)
            except FileNotFoundError:
                changed.append(rel)
                continue
            if st.st_size != entry["size"] or st.st_mtime_ns != entry["mtime_ns"]:
                changed.append(rel)
                continue
            os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
            entry["marked"] = False
            cleared += 1
        manifest.update(state="thawed" if not changed else "partly thawed", thawed=runs.now(),
                        not_thawed=changed)
        runs.write_json(manifest_path, manifest)
        rows.append({"instance": str(instance), "cleared": cleared, "not_thawed": changed,
                     "manifest": str(manifest_path)})
        if changed:
            ctx.finding(f"{instance.name}: {len(changed)} file(s) changed since the freeze kept their mark")
    ctx.summary(f"{len(rows)} frozen instance(s) thawed under {root}: "
                f"{sum(r['cleared'] for r in rows)} read-only mark(s) cleared")
    return {"instances": rows}
