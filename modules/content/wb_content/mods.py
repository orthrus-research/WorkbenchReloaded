"""The mods a pack has, for checking what its scripts refer to: read from a mods folder of JARs (their
metadata files, and their @Mod annotations when those name nothing) or from a list of ids; and, when
GroovyScript's own JAR is among them, the names it answers to under ``mods.`` (each compat container's mod id
and its aliases, read from its static initializer).

Forge loads the JARs and zips at the top of the mods folder and in a folder named for the game version
(``mods/1.12.2``); those are the ones read.
"""

from __future__ import annotations

import json
import re
import tomllib
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import classfile

SUPPORT = "com/cleanroommc/groovyscript/compat/mods/ModSupport.class"
CONTAINER = "/InternalModContainer"
MAX_CLASS = 4 * 2**20
_ID = re.compile(r"[A-Za-z0-9_.\-]+")


@dataclass
class Mods:
    ids: dict[str, list[str]] = field(default_factory=dict)  # mod id -> where it was found
    aliases: dict[str, str] = field(default_factory=dict)  # a name under mods. -> its mod id
    jars: int = 0
    alias_source: str | None = None
    problems: list[str] = field(default_factory=list)

    def add(self, mod: str, where: str) -> None:
        if mod and _ID.fullmatch(mod):
            self.ids.setdefault(mod, [])
            if where not in self.ids[mod]:
                self.ids[mod].append(where)

    def describe(self) -> dict:
        return {"ids": len(self.ids), "jars": self.jars, "aliases": len(self.aliases),
                "aliasesFrom": self.alias_source, "problems": self.problems}


def read(path: Path | None, extra: list[str], scan: str, cancel: Callable[[], None] = lambda: None) -> Mods:
    mods = Mods()
    for mod in extra:
        mods.add(mod.strip(), "mod_ids")
    if path is None:
        return mods
    if path.is_dir():
        for jar in archives(path):
            cancel()
            _jar(mods, jar, jar.relative_to(path).as_posix(), scan)
    elif path.suffix.lower() in (".jar", ".zip"):
        _jar(mods, path, path.name, scan)
    else:
        _listed(mods, path)
    return mods


def archives(folder: Path) -> list[Path]:
    found = []
    for entry in sorted(folder.iterdir()):
        if entry.is_file() and entry.suffix.lower() in (".jar", ".zip"):
            found.append(entry)
        elif entry.is_dir() and re.fullmatch(r"\d+(\.\d+)+", entry.name):
            found += [p for p in sorted(entry.iterdir()) if p.is_file() and p.suffix.lower() in (".jar", ".zip")]
    return found


def _listed(mods: Mods, path: Path) -> None:
    """Ids from a file: JSON (a list of ids or of objects with an id, or an object keyed by id), or text with
    one id per line (modid, modid@version or 'modid version'; # starts a comment)."""
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    if path.suffix.lower() == ".json" or text.lstrip()[:1] in ("[", "{"):
        try:
            data = json.loads(text)
        except ValueError as error:
            raise ValueError(f"{path.name} is not JSON: {error}") from None
        items = data.items() if isinstance(data, dict) else enumerate(data if isinstance(data, list) else [])
        for key, value in items:
            if isinstance(value, str):
                mods.add(value, path.name)
            elif isinstance(value, dict):
                mod = value.get("modid", value.get("modId", value.get("id")))
                mods.add(mod if isinstance(mod, str) else str(key), path.name)
            elif isinstance(key, str):
                mods.add(key, path.name)
        return
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            mods.add(re.split(r"[\s@,;=]", line, maxsplit=1)[0], path.name)


def _jar(mods: Mods, path: Path, label: str, scan: str) -> None:
    mods.jars += 1
    try:
        archive = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as error:
        mods.problems.append(f"{label}: not a readable JAR: {error}")
        return
    with archive:
        entries = {info.filename: info for info in archive.infolist() if not info.is_dir()}
        found = []
        try:
            found += [mod for mod in _metadata(archive, entries) if _ID.fullmatch(mod)]
        except (zipfile.BadZipFile, OSError, ValueError) as error:
            mods.problems.append(f"{label}: {error}")
        if scan == "always" or (scan == "auto" and not found):
            for name, info in entries.items():
                if name.endswith(".class") and info.file_size <= MAX_CLASS:
                    try:
                        found += classfile.mod_ids(archive.read(info))
                    except (classfile.ClassError, zipfile.BadZipFile, OSError):
                        continue
        for mod in found:
            mods.add(mod, label)
        if SUPPORT in entries and not mods.aliases:
            try:
                containers = classfile.ClassFile(archive.read(SUPPORT)).constructed("<clinit>", CONTAINER)
            except (classfile.ClassError, zipfile.BadZipFile, OSError) as error:
                mods.problems.append(f"{label}: GroovyScript's mod containers could not be read: {error}")
                return
            for strings in containers:
                if not strings or not _ID.fullmatch(strings[0]):
                    continue
                for name in [strings[0], *strings[2:]]:
                    if _ID.fullmatch(name):
                        mods.aliases.setdefault(name, strings[0])
            mods.alias_source = label


def _metadata(archive: zipfile.ZipFile, entries: dict) -> list[str]:
    found = []
    if "mcmod.info" in entries:
        text = archive.read("mcmod.info").decode("utf-8-sig", "replace")
        try:
            info = json.loads(text)
            listed = info.get("modList") if isinstance(info, dict) else info
            found += [e["modid"] for e in listed or [] if isinstance(e, dict) and isinstance(e.get("modid"), str)]
        except (ValueError, TypeError, AttributeError):
            found += re.findall(r'"modid"\s*:\s*"([^"]+)"', text)  # Forge reads mcmod.info leniently too
    for name in ("META-INF/mods.toml", "META-INF/neoforge.mods.toml"):
        if name in entries:
            try:
                table = tomllib.loads(archive.read(name).decode("utf-8", "replace"))
            except tomllib.TOMLDecodeError as error:
                raise ValueError(f"{name}: {error}") from None
            found += [m["modId"] for m in table.get("mods") or [] if isinstance(m, dict)
                      and isinstance(m.get("modId"), str)]
    if "fabric.mod.json" in entries:
        try:
            fabric = json.loads(archive.read("fabric.mod.json").decode("utf-8-sig", "replace"))
        except ValueError:
            fabric = None
        if isinstance(fabric, dict) and isinstance(fabric.get("id"), str):
            found.append(fabric["id"])
    return found
