"""Index what JARs patch and declare, for conflict triage across a pack (``jar.index``).

Per archive: its Mixin configurations, each Mixin's targets, handlers (with the methods and injection
points they name, through the refmap) and overwrites; the engine systems those targets belong to; the
events its methods subscribe to; the threads it starts; the mods it declares and requires. Across the
archives: every target class with the Mixins that patch it, and collisions (two Mixins overwriting the
same method, or redirecting the same call in the same method), which Mixin resolves by dropping one.

A reference is not a call made at runtime, and a handler's target is what its annotation names, not
proof that it applies.
"""

from __future__ import annotations

import re
import tomllib
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import classes, mixins
from .archive import Archive, archives

SUBSCRIBERS = [
    "Lnet/minecraftforge/fml/common/eventhandler/SubscribeEvent;",
    "Lnet/minecraftforge/eventbus/api/SubscribeEvent;",
    "Lnet/neoforged/bus/api/SubscribeEvent;",
]
MOD = "Lnet/minecraftforge/fml/common/Mod;"
# A dependency string's required entries: required-after:id@[1,), required:id, required-client:id
REQUIRED = re.compile(r"(?:^|;)\s*required(?:-(?:after|before|client|server))?:\s*([\w.\-]+)")
THREADS = {
    ("java/lang/Thread", "<init>"), ("java/lang/Thread", "startVirtualThread"), ("java/lang/Thread", "ofPlatform"),
    ("java/lang/Thread", "ofVirtual"), ("java/util/concurrent/ThreadPoolExecutor", "<init>"),
    ("java/util/concurrent/ScheduledThreadPoolExecutor", "<init>"), ("java/util/concurrent/ForkJoinPool", "<init>"),
}
EXECUTOR_FACTORY = re.compile(r"^new\w*(ThreadPool|Executor)\w*$")
MANIFEST_KEYS = ("MixinConfigs", "FMLCorePlugin", "TweakClass", "Automatic-Module-Name")
SAMPLE = 20  # classes named per thread entry point


@dataclass
class Record:
    jar: str
    classes: int = 0
    unreadable: list[str] = field(default_factory=list)
    mods: list[str] = field(default_factory=list)
    requires: set[str] = field(default_factory=set)
    configs: list[mixins.Config] = field(default_factory=list)
    mixins: list[tuple[str, str, mixins.Mixin]] = field(default_factory=list)  # (config, side, mixin)
    events: Counter = field(default_factory=Counter)
    threads: dict[str, list[str]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    manifest: dict[str, str] = field(default_factory=dict)

    def systems(self, groups: list[tuple[str, str]]) -> Counter:
        return Counter(system_of(t, groups) for _, _, m in self.mixins for t in m.targets)

    def as_json(self, groups: list[tuple[str, str]]) -> dict:
        return {"jar": self.jar, "mods": self.mods, "requires": sorted(self.requires - set(self.mods)),
                "classes": self.classes, "unreadable": self.unreadable[:SAMPLE],
                "configs": [c.as_json() for c in self.configs],
                "mixins": [dict(m.as_json(), config=config, side=side) for config, side, m in self.mixins],
                "systems": dict(self.systems(groups).most_common()), "events": dict(self.events.most_common()),
                "threads": self.threads, "errors": self.errors, "notes": self.notes, "manifest": self.manifest}


def groups_of(systems: dict | None) -> list[tuple[str, str]]:
    """``{prefix: system}`` as (internal prefix, system), longest prefix first: the most specific wins."""
    pairs = [(str(prefix).replace(".", "/"), str(label)) for prefix, label in (systems or {}).items()]
    return sorted(pairs, key=lambda pair: -len(pair[0]))


def system_of(target: str, groups: list[tuple[str, str]]) -> str:
    """The system a target class belongs to; without groups, its package."""
    if not groups:
        return target.rsplit("/", 1)[0] if "/" in target else "(default package)"
    return next((label for prefix, label in groups if target.startswith(prefix)), "other")


def _ids(text: str) -> str:
    return text.split("@", 1)[0].strip().lower()


def _metadata(archive: Archive, record: Record) -> None:
    """Mod ids and required mods from the loaders' metadata files. A loader reads its JSON leniently, so
    JSON this cannot read is a note, not an error."""
    info, error = archive.json("mcmod.info") if "mcmod.info" in archive.entries else (None, None)
    if error:
        record.notes.append(f"mcmod.info: {error}")
    entries = info.get("modList") if isinstance(info, dict) else info
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, dict) and isinstance(entry.get("modid"), str):
            record.mods.append(entry["modid"].lower())
            record.requires.update(_ids(r) for r in entry.get("requiredMods") or [] if isinstance(r, str))
    for name in ("META-INF/mods.toml", "META-INF/neoforge.mods.toml"):
        data = archive.read(name, 2**20)
        if data is None:
            continue
        try:
            table = tomllib.loads(data.decode("utf-8", "replace"))
        except tomllib.TOMLDecodeError as error:
            record.errors.append(f"{name}: {error}")
            continue
        record.mods += [m["modId"].lower() for m in table.get("mods") or []
                        if isinstance(m, dict) and isinstance(m.get("modId"), str)]
        for dependencies in (table.get("dependencies") or {}).values():
            for dependency in dependencies if isinstance(dependencies, list) else []:
                if isinstance(dependency, dict) and isinstance(dependency.get("modId"), str) and (
                        dependency.get("mandatory") is True or dependency.get("type") == "required"):
                    record.requires.add(dependency["modId"].lower())
    fabric, error = archive.json("fabric.mod.json") if "fabric.mod.json" in archive.entries else (None, None)
    if error:
        record.notes.append(f"fabric.mod.json: {error}")
    if isinstance(fabric, dict) and isinstance(fabric.get("id"), str):
        record.mods.append(fabric["id"].lower())
        record.requires.update(str(k).lower() for k in (fabric.get("depends") or {}))


def index_archive(archive: Archive, subscribers: list[str]) -> Record:
    record = Record(archive.label)
    manifest = archive.manifest()
    record.manifest = {k: manifest[k] for k in MANIFEST_KEYS if k in manifest}
    configs, record.errors = mixins.configs(archive)
    record.configs = configs
    for config in configs:
        # Mixin stops the game over a required configuration's defect and only logs another's.
        problems = record.errors if config.required else record.notes
        table: dict = {}
        if config.refmap:
            if config.refmap not in archive.entries:
                problems.append(f"{config.name}: its refmap {config.refmap} is not packaged")
            else:
                table, error = mixins.refmap(archive, config.refmap)
                if error:
                    problems.append(f"{config.name}: {error}")
        for side, name in config.classes():
            info = archive.read_class(name + ".class")
            if info is None:
                problems.append(f"{config.name}: lists {name.replace('/', '.')}, which is not in the JAR")
                continue
            mixin = mixins.mixin_of(info, table)
            if mixin is None:
                problems.append(f"{config.name}: lists {name.replace('/', '.')}, which has no @Mixin")
                continue
            record.mixins.append((config.name, side, mixin))
    subscribed = set(subscribers)
    threads: dict[str, set[str]] = defaultdict(set)
    for entry, info in archive.each_class():
        record.classes += 1
        if not info.complete:
            record.unreadable.append(entry)
        mod = info.annotation(MOD)
        if mod is not None:
            modid = mod.get("modid", mod.get("value"))
            if isinstance(modid, str):
                record.mods.append(modid.lower())
            dependencies = mod.get("dependencies")
            if isinstance(dependencies, str):
                record.requires.update(m.lower() for m in REQUIRED.findall(dependencies))
        for method in info.declared_methods:
            if any(a.type in subscribed for a in method.annotations):
                parameters = classes.parameters(method.descriptor)
                if parameters and parameters[0].startswith("L"):
                    record.events[classes.descriptor_class(parameters[0]).replace("/", ".")] += 1
        for owner, member in info.methods:
            if (owner, member) in THREADS or (owner == "java/util/concurrent/Executors"
                                              and EXECUTOR_FACTORY.match(member)):
                threads[f"{owner.rsplit('/', 1)[-1]}.{member}"].add(info.this_class.replace("/", "."))
    record.threads = {label: sorted(names)[:SAMPLE] for label, names in sorted(threads.items())}
    _metadata(archive, record)
    record.mods = list(dict.fromkeys(record.mods))
    return record


def _key_name(text: str) -> str:
    return mixins.selector(text)[1]


def _at_key(text: str) -> str:
    owner, name, descriptor = mixins.selector(text)
    return f"L{owner};{name}{descriptor}" if owner else name + descriptor


def _overlapping(by: list[dict]) -> list[dict]:
    """The entries that hook the same occurrence as an entry of another JAR: equal ordinals, or one that
    hooks every occurrence. Mixins of one JAR do not collide here: a mod gates its own alternatives."""
    return [a for a in by if any(b["jar"] != a["jar"] and (a["ordinal"] == b["ordinal"] or -1 in (a["ordinal"], b["ordinal"]))
                                 for b in by)]


def cross(records: list[Record]) -> tuple[dict[str, list[dict]], list[dict]]:
    """Every target class with what patches it, and the collisions between Mixins of different JARs:
    two overwriting the same method, or redirecting the same call in the same method."""
    targets: dict[str, list[dict]] = defaultdict(list)
    overwrites: dict[tuple, list[dict]] = defaultdict(list)
    redirects: dict[tuple, list[dict]] = defaultdict(list)
    for record in records:
        for _config, _side, mixin in record.mixins:
            by = {"jar": record.jar, "mixin": mixin.name.replace("/", "."), "priority": mixin.priority}
            for target in mixin.targets:
                targets[target].append(dict(by, handlers=Counter(h.kind for h in mixin.handlers),
                                            overwrites=[n + d for n, d in mixin.overwrites]))
                for name, descriptor in mixin.overwrites:
                    overwrites[(target, name, descriptor)].append(dict(by, ordinal=-1))
                for handler in mixin.handlers:
                    if handler.kind != "redirect":
                        continue
                    for method in handler.methods:
                        for at, ordinal in zip(handler.at, handler.ordinals, strict=True):
                            redirects[(target, _key_name(method), _at_key(at))].append(dict(by, ordinal=ordinal))
    collisions = []
    for (target, name, descriptor), by in sorted(overwrites.items()):
        if _overlapping(by):
            collisions.append({"kind": "overwrite", "target": target.replace("/", "."), "member": name + descriptor,
                               "by": [{k: v for k, v in b.items() if k != "ordinal"} for b in by]})
    for (target, method, at), by in sorted(redirects.items()):
        involved = _overlapping(by)
        if involved:
            collisions.append({"kind": "redirect", "target": target.replace("/", "."), "member": method, "at": at,
                               "by": involved})
    table = {t.replace("/", "."): [dict(row, handlers=dict(row["handlers"])) for row in rows]
             for t, rows in sorted(targets.items())}
    return table, collisions


def index(root: Path, *, subscribers: list[str], systems: dict | None, platform: list[str]) -> dict[str, Any]:
    """The full report of jar.index over a JAR or a folder of JARs."""
    errors: list[dict] = []
    records = [index_archive(archive, subscribers) for archive in archives(root, errors)]
    groups = groups_of(systems)
    targets, collisions = cross(records)
    provided = {m for r in records for m in r.mods} | {p.lower() for p in platform}
    missing = [{"jar": r.jar, "requires": mod} for r in records for mod in sorted(r.requires - provided)]
    by_system: dict[str, set[str]] = defaultdict(set)
    for record in records:
        for system in record.systems(groups):
            by_system[system].add(record.jar)
    for record in records:
        errors += [{"jar": record.jar, "error": e} for e in record.errors]
    return {"schema": "wb.jar.index/1", "jars": [r.as_json(groups) for r in records], "targets": targets,
            "systems": {s: sorted(j) for s, j in sorted(by_system.items())}, "collisions": collisions,
            "missing": missing, "errors": errors}
