"""Worlds: a played world kept among the lab's worlds by its world key and writer build, and restored into a fresh
instance, so a session starts on a settled world instead of generating one.

A world's **key** is what it was generated from: the seed (the launch's ``seed`` param), the layout scale (the
configure script's record ``layoutScale``, else the ``scale`` param) and the SHA-256 of the generator's definitions
(the configure record's ``definitions`` as JSON with sorted keys, the digest the old route's standalone.json gave, so
a world lab.freeze imported is found by it too). Its **writer** is the build that wrote it: the one member of the
base's project (Cubature's JAR, for a launch on ``cubature:standalone``), else every member together. A world is
the instance's ``WORLD_PARTS``.

The store (``<instances>/.store/worlds/<label>/world.json``, every file kept once by content in the store's blobs)
holds one world per key and writer, the latest settled one: an open or tour session whose server stopped by itself
keeps its world there, as lab.build does before it replaces an instance. A measure or driver session never writes
it, so measurements repeated on fresh copies start on the same world. Restoring a world is a private copy, since the
game writes region files in place.

Which world a session plays (``choose``), by its choice:

- default: the instance's own world when its key and writer are the session's; a world the instance only has from its
  template (a priming start's) gives way to the store's settled world of that key and writer. A world of another key
  or writer build is kept in the store and set aside, and the session starts on the store's world of its own key and
  writer, or a fresh one; the session says that ``keep_world`` (``wb play --keep-world``) plays on the one set aside.
  An instance without a world gets the store's world of its key and writer, when there is one.
- ``keep``: the world set aside, else the instance's own whatever wrote it, else the store's newest world of the key
  from any writer build.
- ``fresh``: no world: the server generates one (every part removed by the unlink-first routine).

What an instance's world is goes into its ``.wb-instance.json`` (``world``), and what the session chose and kept into
its session.json.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from .store import MARKER, Layout, Store, StoreError, recipe_of, remove_tree

# The parts of an instance that are its world: the level, Cubature's cache of it and the Distant Horizons data.
WORLD_PARTS = ["server/world", "server/world.cubature-cache", "client/Distant_Horizons_server_data"]
CHOICES = ("", "keep", "fresh")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _project(spec: str) -> str:
    return spec.split("@", 1)[0].split(":", 1)[0]


def identity(receipt: dict, launch: dict) -> dict:
    """The world key and writer of an instance's world: ``{"key": {seed, scale, definitions}, "writer": {member,
    sha256}}``, from its receipt's params and configure record and the launch's base and members."""
    params = receipt.get("params") or {}
    record = (receipt.get("configure") or {}).get("record") or {}
    definitions = json.dumps(record.get("definitions") or {}, sort_keys=True)
    key = {"seed": params.get("seed"), "scale": record.get("layoutScale", params.get("scale")),
           "definitions": _sha256(definitions)}
    members = (receipt.get("launch") or {}).get("members") or {}
    project = (launch.get("base") or {}).get("project")
    own = sorted(spec for spec in members if project and _project(spec) == project)
    if len(own) == 1:
        writer = {"member": own[0], "sha256": members[own[0]]}
    else:  # no one build of the base's project: every member together wrote it
        writer = {"member": None, "sha256": _sha256(json.dumps(members, sort_keys=True))}
    return {"key": key, "writer": writer}


def same(one: dict | None, other: dict) -> bool:
    """Whether a recorded world (``one``) has the key and writer build of ``other``."""
    return bool(one) and one.get("key") == other["key"] and \
        (one.get("writer") or {}).get("sha256") == other["writer"]["sha256"]


def store_of(root: Path) -> Store:
    """Where a lab's instances folder keeps its worlds (the store lab.build keeps its templates in)."""
    return Store(Path(root) / ".store", Layout(world_parts=list(WORLD_PARTS)))


def label_of(ident: dict) -> str:
    return "w-" + recipe_of({"key": ident["key"], "writer": ident["writer"]["sha256"]})[:20]


def kept(store: Store, key: dict, writer: str | None = None) -> list[dict]:
    """The store's worlds of ``key`` (written by ``writer``'s SHA-256, when given), newest first; a world lab.freeze
    imported names its writer as ``cubature``."""
    out = []
    for path in sorted(store.worlds.glob("*/world.json")) if store.worlds.is_dir() else []:
        if path.parent.name.endswith(".new"):
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        meta = record.get("key") or {}
        made_by = meta.get("writer") or meta.get("cubature")
        if meta.get("key") != key or (writer is not None and made_by != writer):
            continue
        out.append({**record, "label": path.parent.name, "writer": made_by})
    return sorted(out, key=lambda r: r.get("created") or 0, reverse=True)


def present(instance: Path) -> bool:
    return (Path(instance) / WORLD_PARTS[0]).is_dir()


def remove(instance: Path):
    """Removes every world part of an instance by the unlink-first routine: a link in it is removed, never what it
    points at."""
    from wb_core import retention
    for part in WORLD_PARTS:
        path = Path(instance) / part
        if path.exists() or retention.is_link(path):
            retention.remove(path)


def keep(store: Store, instance: Path, ident: dict, source: dict) -> dict:
    """Keeps the instance's world as the store's world of its key and writer, in place of the one kept before."""
    label = label_of(ident)
    staging = f"{label}.new"
    if (store.worlds / staging).exists():
        remove_tree(store.worlds / staging)  # one an interrupted keep left
    record = store.snapshot(instance, staging, {"key": ident["key"], "writer": ident["writer"]["sha256"],
                                                "member": ident["writer"]["member"], **source})
    final = store.worlds / label
    if final.exists():
        remove_tree(final)  # its files stay in the blobs until gc finds them unnamed
    os.replace(store.worlds / staging, final)
    record["label"] = label
    (final / "world.json").write_text(json.dumps(record, indent=1), encoding="utf-8")
    return record


def restore(store: Store, instance: Path, label: str) -> int:
    """The store's world ``label`` as the instance's own world (a private copy); its size in bytes."""
    remove(instance)
    return store.restore(Path(instance), label)


def marker(instance: Path) -> dict:
    path = Path(instance) / MARKER
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, ValueError):
        return {}


def mark(instance: Path, world: dict | None):
    """Records what the instance's world is in its ``.wb-instance.json``."""
    path = Path(instance) / MARKER
    record = marker(instance) or {"schema": 1}
    record["world"] = world
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(record, indent=1), encoding="utf-8")
    os.replace(temporary, path)


def recorded(instance: Path, primed: dict | None = None) -> dict | None:
    """What the instance's world is: its marker's record, else (a world it has only from its template) the
    receipt's record of a priming start's world, else None (a world nothing recorded)."""
    world = marker(instance).get("world")
    if world is None and present(instance):
        return dict(primed) if primed else None
    return world


def choose(store: Store, instance: Path, ident: dict, choice: str = "", primed: dict | None = None) -> dict:
    """Readies the instance's world for a session by ``choice`` (``""``, ``keep`` or ``fresh``) and returns what was
    done: ``from`` (instance, restored, generated), the ``label`` restored, a world ``passed_over`` and set aside, a
    ``hint`` for the person, ``unrecorded`` when the instance's world names no key."""
    if choice not in CHOICES:
        raise ValueError(f"world: {choice!r} is none of keep, fresh or empty")
    instance = Path(instance)
    out = {"choice": choice or "default", "key": ident["key"], "writer": ident["writer"], "from": None,
           "label": None, "passed_over": None, "set_aside": None, "hint": None, "unrecorded": False}
    here, mine = present(instance), recorded(instance, primed)

    def restore_newest(found: list[dict]) -> bool:
        if not found:
            return False
        restore(store, instance, found[0]["label"])
        out.update({"from": "restored", "label": found[0]["label"]})
        return True

    if choice == "fresh":
        remove(instance)
        out["from"] = "generated"
        return out
    if choice == "keep":
        aside = (mine or {}).get("set_aside")
        if aside and (store.worlds / aside / "world.json").is_file():
            restore(store, instance, aside)
            out.update({"from": "restored", "label": aside})
        elif here and (mine is None or mine.get("played")):
            out["from"] = "instance"
            out["unrecorded"] = mine is None
        elif restore_newest(kept(store, ident["key"])):
            pass  # over a world the instance has only from its template
        else:
            out["from"] = "instance" if here else "generated"
        return out
    if here and mine is None:
        out.update({"from": "instance", "unrecorded": True,
                    "hint": "the instance's world names no world key or writer build (it was played before they "
                            "were recorded); fresh_world (wb play --fresh-world) starts a fresh one"})
        return out
    if here and same(mine, ident):
        if not mine.get("played") and restore_newest(kept(store, ident["key"], ident["writer"]["sha256"])):
            return out  # a settled world over the one the template's priming start made
        out["from"] = "instance"
        others = [] if mine.get("played") else kept(store, ident["key"])
        if others:
            out["hint"] = (f"the store keeps a world of this key written by another build ({others[0]['label']}); "
                           "this session plays the template's primed world, and keep_world (wb play --keep-world) "
                           "plays on that one")
        return out
    if here:  # another key or another writer build: kept and set aside
        before = {"key": mine.get("key"), "writer": mine.get("writer") or {"member": None, "sha256": None}}
        label = None
        if mine.get("played") and before["key"] is not None and before["writer"].get("sha256"):
            try:
                label = keep(store, instance, before, {"from": instance.name, "set_aside": True})["label"]
            except StoreError:
                label = None
        remove(instance)
        out.update({"passed_over": {**before, "label": label}, "set_aside": label})
        member = before["writer"].get("member") or "the members"
        out["hint"] = (f"the instance's world was written by another build ({member} "
                       f"{str(before['writer'].get('sha256') or '')[:12]}) or from another world key, so this session "
                       "starts on another world" + (f"; the old one is kept as {label}, and keep_world (wb play "
                                                    "--keep-world) plays on it" if label else ""))
    if not restore_newest(kept(store, ident["key"], ident["writer"]["sha256"])):
        out["from"] = "generated"
        others = kept(store, ident["key"])
        if others and not out["hint"]:
            out["hint"] = (f"the store keeps a world of this key written by another build ({others[0]['label']}); "
                           "this session generates a fresh one, and keep_world (wb play --keep-world) plays on that "
                           "one")
    return out


def after(store: Store, instance: Path, ident: dict, chosen: dict, *, session: str, settled: bool,
          keeps: bool) -> dict:
    """Records the instance's world after a session, and keeps it in the store when the session ``keeps`` (an open or
    tour session) and its server ``settled`` (stopped by itself). Returns ``chosen`` with ``kept`` (a label or
    None) and ``why`` it was not kept."""
    out = {**chosen, "kept": None}
    if not present(instance):
        mark(instance, None)
        return out
    # A world set aside stays named until a keep session plays it.
    earlier = (marker(instance).get("world") or {}).get("set_aside")
    set_aside = None if chosen.get("choice") == "keep" else chosen.get("set_aside") or earlier
    if chosen.get("unrecorded"):
        out["why"] = "the world names no world key or writer build"
        return out  # left as it was: nothing says what it is
    mark(instance, {"key": ident["key"], "writer": ident["writer"], "played": True, "session": session,
                    "from": chosen.get("from"), "label": chosen.get("label"), "set_aside": set_aside})
    if not keeps:
        out["why"] = "a measure or driver session never keeps its world"
    elif not settled:
        out["why"] = "the server did not stop by itself, so its world may be half written"
    else:
        try:
            out["kept"] = keep(store, instance, ident, {"from": Path(instance).name, "session": session})["label"]
        except StoreError as error:
            out["why"] = str(error)
    return out
