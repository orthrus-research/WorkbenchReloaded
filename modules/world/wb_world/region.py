"""Region files, where a save keeps its units: vanilla Anvil, and the RegionLib layout of cubic saves.

Both open with a table of entries, each ``(first sector << 8) | sector count``, and keep every payload
behind a 4-byte big-endian length. They differ in the rest:

- Anvil, ``region/r.X.Z.mca``: 32 x 32 chunks in 4 KiB sectors, a second 4 KiB table of save times, and a
  compression byte before each payload (1 gzip, 2 zlib, 3 none; plus 128 when the chunk is kept whole in a
  ``c.X.Z.mcc`` file beside the region).
- RegionLib, ``region3d/X.Y.Z.3dr`` (16 x 16 x 16 cubes) and ``region2d/X.Z.2dr`` (32 x 32 columns): 512-byte
  sectors, no time table, gzip payloads. An entry too big for 255 sectors is kept in ``<file>.ext/<entry>``
  behind a zero table entry; 0xFFFFFFFF marks a special entry, which this reader refuses.

The sector count is a single byte. A payload longer than its sectors means the count overflowed or the entry
is corrupt: such an entry is reported and never read on into the sectors of the next one.
"""

from __future__ import annotations

import os
import re
import struct
from dataclasses import dataclass
from pathlib import Path

from wb_core.module import EnvironmentProblem

from . import nbt

SPECIAL = 0xFFFFFFFF


class EntryError(Exception):
    """One entry that cannot be read; the rest of its file still can."""


@dataclass(frozen=True)
class Layout:
    kind: str        # what one entry holds: chunk, cube or column
    folder: str
    suffix: str
    pattern: str     # the file name, capturing the region's coordinates
    edge: int        # units along each side of a region
    dims: int        # 2 (x, z) or 3 (x, y, z)
    sector: int
    table: int       # header bytes, before the first sector a payload may start in

    @property
    def entries(self) -> int:
        return self.edge ** self.dims

    def coords(self, name: str) -> tuple[int, ...] | None:
        match = re.fullmatch(self.pattern, name)
        return tuple(int(value) for value in match.groups()) if match else None

    def position(self, region: tuple[int, ...], index: int) -> tuple[int, ...]:
        local = []
        for _ in range(self.dims):
            local.append(index % self.edge)
            index //= self.edge
        if self.kind != "chunk":
            local.reverse()  # RegionLib runs the last axis fastest; Anvil runs x fastest
        return tuple(r * self.edge + value for r, value in zip(region, local))


ANVIL = Layout("chunk", "region", ".mca", r"r\.(-?\d+)\.(-?\d+)\.mca", 32, 2, 4096, 8192)
CUBES = Layout("cube", "region3d", ".3dr", r"(-?\d+)\.(-?\d+)\.(-?\d+)\.3dr", 16, 3, 512, 16384)
COLUMNS = Layout("column", "region2d", ".2dr", r"(-?\d+)\.(-?\d+)\.2dr", 32, 2, 512, 4096)
LAYOUTS = (ANVIL, CUBES, COLUMNS)


class Region:
    """One open region file. Closing it checks the file did not change meanwhile: a game may be writing it."""

    def __init__(self, path: Path, layout: Layout):
        self.path, self.layout = path, layout
        self.coords = layout.coords(path.name)
        self.problems: list[str] = []  # about the file as a whole
        self.handle = open(path, "rb")
        self.opened = os.fstat(self.handle.fileno())
        self.size = self.opened.st_size
        self.table = self._table()
        self.external = self._external()

    def __enter__(self) -> Region:
        return self

    def __exit__(self, kind, error, trace) -> None:
        now = os.fstat(self.handle.fileno())
        self.handle.close()
        if kind is None and (now.st_size, now.st_mtime_ns) != (self.opened.st_size, self.opened.st_mtime_ns):
            raise EnvironmentProblem(f"{self.path.name} changed while it was read: a game is writing the world")

    def indices(self) -> list[int]:
        return sorted(set(self.table) | set(self.external))

    def read(self, index: int) -> bytes:
        """The entry's NBT document, inflated."""
        packed = self.table.get(index, 0)
        if index in self.external:
            if packed:
                raise EntryError("has both sectors and an external file")
            return _inflate(_read_whole(self.external[index]))
        if packed == SPECIAL and self.layout.kind != "chunk":
            raise EntryError("is a special entry, which this reader does not support")
        start, length = self._locate(packed)
        self.handle.seek(start)
        data = self.handle.read(length)
        return self._anvil(index, data) if self.layout.kind == "chunk" else _inflate(data)

    def _table(self) -> dict[int, int]:
        if self.size == 0:
            return {}  # created and never written: nothing is saved in it
        head = self.handle.read(self.layout.table)
        if len(head) < self.layout.table:
            self.problems.append(f"its header is cut short at {len(head)} of {self.layout.table} bytes")
            return {}
        packed = struct.unpack(f">{self.layout.entries}I", head[:self.layout.entries * 4])
        table = {index: value for index, value in enumerate(packed) if value}
        self._check_overlaps(table)
        return table

    def _check_overlaps(self, table: dict[int, int]) -> None:
        spans = sorted((value >> 8, value & 0xFF, index) for index, value in table.items() if value != SPECIAL)
        for (start, count, first), (after, _, second) in zip(spans, spans[1:]):
            if start + count > after:
                self.problems.append(f"entries {first} and {second} share sectors")

    def _external(self) -> dict[int, Path]:
        folder = self.path.with_name(self.path.name + ".ext")
        if self.layout.kind == "chunk" or not folder.is_dir():
            return {}
        return {int(child.name): child for child in folder.iterdir()
                if child.name.isdigit() and int(child.name) < self.layout.entries and child.is_file()}

    def _locate(self, packed: int) -> tuple[int, int]:
        """Where the entry's payload lies, checked against its sectors and the file."""
        first, count = packed >> 8, packed & 0xFF
        start = first * self.layout.sector
        if not count:
            raise EntryError("has no sectors")
        if start < self.layout.table:
            raise EntryError("points into the header")
        if start + 4 > self.size:
            raise EntryError("starts past the end of the file")
        self.handle.seek(start)
        length, = struct.unpack(">I", self.handle.read(4))
        room = count * self.layout.sector - 4
        if not length:
            raise EntryError("is empty")
        if length > room:
            raise EntryError(f"declares {length} bytes, more than the {room} its sectors hold ({count} x "
                             f"{self.layout.sector} bytes): the one-byte sector count overflowed, or the entry "
                             "is corrupt")
        if start + 4 + length > self.size:
            raise EntryError("runs past the end of the file")
        return start + 4, length

    def _anvil(self, index: int, data: bytes) -> bytes:
        scheme, body = data[0], data[1:]
        if scheme & 0x80:
            x, z = self.layout.position(self.coords, index)
            body = _read_whole(self.path.with_name(f"c.{x}.{z}.mcc"))
            scheme &= 0x7F
        if scheme in (1, 2):
            return _inflate(body)
        if scheme == 3:
            return body
        raise EntryError(f"uses compression {scheme}, which this reader does not support")


def _inflate(data: bytes) -> bytes:
    try:
        return nbt.inflate(data)
    except nbt.NbtError as error:
        raise EntryError(str(error)) from None


def _read_whole(path: Path) -> bytes:
    try:
        with open(path, "rb") as handle:
            data = handle.read(nbt.MAX_BYTES + 1)
    except OSError as error:
        raise EntryError(f"keeps its data in {path.name}, which cannot be read ({error.strerror})") from None
    if len(data) > nbt.MAX_BYTES:
        raise EntryError(f"keeps more than {nbt.MAX_BYTES >> 20} MiB in {path.name}")
    return data
