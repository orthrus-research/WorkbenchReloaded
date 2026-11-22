"""JARs on disk and the JARs packed inside them, opened as archives for the jar commands.

A folder is searched for JARs the way the survey searches a mods folder. A JAR's nested JARs (jar-in-jar
libraries) are opened too, no deeper than the survey goes, each labelled ``outer.jar!/path/inner.jar``.
Entries over the audit's size limit for a class are not read as classes.
"""

from __future__ import annotations

import io
import json
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import classes
from orthrus_tools.jar_audit import MAX_CLASS_BYTES  # noqa: E402 - classes put orthrus_tools on the path
from orthrus_tools.mod_survey import MAX_NESTING, find_jars  # noqa: E402

MAX_NESTED_BYTES = 256 * 2**20  # a nested JAR is read into memory; a larger one is skipped and reported


@dataclass
class Archive:
    label: str
    zip: zipfile.ZipFile
    depth: int = 0
    _names: dict[str, zipfile.ZipInfo] | None = field(default=None, repr=False)

    @property
    def entries(self) -> dict[str, zipfile.ZipInfo]:
        if self._names is None:
            self._names = {i.filename: i for i in self.zip.infolist() if not i.is_dir()}
        return self._names

    @property
    def names(self) -> list[str]:
        return list(self.entries)

    def read(self, name: str, limit: int | None = None) -> bytes | None:
        info = self.entries.get(name)
        if info is None or (limit is not None and info.file_size > limit):
            return None
        try:
            return self.zip.read(info)
        except (zipfile.BadZipFile, OSError, ValueError, EOFError):
            return None

    def json(self, name: str) -> tuple[Any, str | None]:
        """A JSON entry's value, or None and why it could not be read."""
        data = self.read(name, MAX_CLASS_BYTES)
        if data is None:
            return None, "missing or unreadable"
        try:
            return json.loads(data.decode("utf-8-sig")), None
        except (ValueError, UnicodeDecodeError) as error:
            return None, f"not JSON: {error}"

    def manifest(self) -> dict[str, str]:
        """The main attributes of ``META-INF/MANIFEST.MF`` (continuation lines joined)."""
        data = self.read("META-INF/MANIFEST.MF", MAX_CLASS_BYTES)
        if not data:
            return {}
        lines: list[str] = []
        for line in data.decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            if line.startswith(" ") and lines:
                lines[-1] += line[1:]
            else:
                lines.append(line)
        out = {}
        for line in lines:
            if not line.strip():
                break  # the main section ends at the first blank line
            key, sep, value = line.partition(":")
            if sep:
                out[key.strip()] = value.strip()
        return out

    def class_entries(self) -> list[str]:
        return [n for n in self.entries if n.endswith(".class")]

    def read_class(self, name: str) -> classes.ClassInfo | None:
        data = self.read(name, MAX_CLASS_BYTES)
        return classes.read(data) if data is not None else None

    def each_class(self) -> Iterator[tuple[str, classes.ClassInfo]]:
        """Every class entry with what it declares; an oversized or unreadable entry is skipped."""
        for name in self.class_entries():
            info = self.read_class(name)
            if info is not None:
                yield name, info


def label(jar: Path, root: Path) -> str:
    """How a JAR is named in results: its path under the folder searched, or its file name."""
    return jar.relative_to(root).as_posix() if root.is_dir() else jar.name


def jars(root: Path) -> list[Path]:
    return find_jars(root)


def archives(root: Path, errors: list[dict], nested: bool = True) -> Iterator[Archive]:
    """Each JAR at or under ``root``, then (when ``nested``) the JARs inside it. Archives that cannot
    be opened are added to ``errors`` as ``{"jar", "error"}``."""
    for jar in jars(root):
        name = label(jar, root)
        try:
            archive = zipfile.ZipFile(jar)
        except (zipfile.BadZipFile, OSError) as error:
            errors.append({"jar": name, "error": f"not a readable JAR: {error}"})
            continue
        with archive:
            yield from _walk(Archive(name, archive), errors, nested)


def _walk(archive: Archive, errors: list[dict], nested: bool) -> Iterator[Archive]:
    yield archive
    if not nested or archive.depth >= MAX_NESTING:
        return
    for name in archive.names:
        if not name.endswith(".jar"):
            continue
        inner_label = f"{archive.label}!/{name}"
        data = archive.read(name, MAX_NESTED_BYTES)
        if data is None:
            errors.append({"jar": inner_label, "error": f"nested JAR over {MAX_NESTED_BYTES} bytes or unreadable"})
            continue
        try:
            inner = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as error:
            errors.append({"jar": inner_label, "error": f"not a readable JAR: {error}"})
            continue
        with inner:
            yield from _walk(Archive(inner_label, inner, archive.depth + 1), errors, nested)
