"""What a saved unit holds. A unit is what one region entry keeps: a chunk (vanilla), or a cube or a column
(cubic saves). Chunks come as 1.12 (block ids and metadata), 1.13 to 1.17 (a palette per section) or 1.18
and later (``block_states``); every format reads into the same sections of 16 x 16 x 16 cells, named
block states, light arrays, a heightmap, block entities and entities.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from . import nbt

CELLS = 4096
AIR = frozenset({"minecraft:air", "minecraft:cave_air", "minecraft:void_air"})
LEFT_OUT = ["minecraft:air"] * CELLS  # the cells of a section a save leaves out; shared, never changed
EXAMPLES = 8
_WORD = (1 << 64) - 1
_LOW = bytes(value & 15 for value in range(256))
_HIGH = bytes(value >> 4 for value in range(256))
_HEIGHTMAPS = ("HeightMap", "Heightmaps", "OpacityIndex")
_LIT = ("LightPopulated", "initLightDone", "isLightOn")
_CLAIMS = {"chunk": ("xPos", "zPos"), "cube": ("x", "y", "z"), "column": ("x", "z")}


class SectionError(ValueError):
    """A section whose block or light arrays do not hold together."""


class Problems(list):
    """What could not be read, as "where: what" lines, each once; reading goes on past them."""

    def __init__(self):
        super().__init__()
        self._seen: set[str] = set()

    def add(self, where: str, what: str) -> None:
        line = f"{where}: {what}"
        if line not in self._seen:
            self._seen.add(line)
            self.append(line)


@dataclass(frozen=True)
class Unit:
    kind: str                  # chunk, cube or column
    pos: tuple[int, ...]       # in units: chunk and column (x, z), cube (x, y, z)
    where: str                 # its file and entry, for messages
    tree: nbt.Compound
    raw: bytes                 # the inflated document, so equal units can be told apart without decoding


@dataclass
class Section:
    pos: tuple[int, int, int]  # in sections of 16 blocks
    states: list[str] | None   # one state name per cell in y-z-x order; None when no blocks are kept (air)
    block_light: bytes | None  # 2048 bytes of 4-bit values, or None when none is kept
    sky_light: bytes | None


@dataclass
class View:
    unit: Unit
    sections: list[Section]
    heightmap: tuple[str, bytes] | None  # the saved tag's name and its canonical bytes
    lit: bool | None                     # the unit's own light-done flag, where its format keeps one
    block_entities: list[nbt.Compound]
    entities: list[nbt.Compound]


class Namer:
    """State names for 1.12 block ids: the save's registry name, or #id where the save names none, with the
    metadata after a colon when it is not 0 (minecraft:stone:1)."""

    def __init__(self, registry: dict[int, str]):
        self.registry = registry
        self.air = registry.get(0) or "minecraft:air"
        self.cells = _Names(self)

    def legacy(self, block: int, meta: int) -> str:
        if block == 0:
            return self.air  # the game reads id 0 as air whatever metadata is left in the cell
        name = self.registry.get(block) or f"#{block}"
        return f"{name}:{meta}" if meta else name


class _Names(dict):
    """(id, metadata) pairs to state names, each named the first time it turns up."""

    def __init__(self, namer: Namer):
        super().__init__()
        self.namer = namer

    def __missing__(self, key: tuple[int, int]) -> str:
        name = self[key] = self.namer.legacy(*key)
        return name


def body(tree: nbt.Compound) -> nbt.Compound:
    """The compound that holds a unit's fields: ``Level`` up to 1.17 and in cubic saves, the root after."""
    level = tree.get("Level")
    return level if isinstance(level, dict) else tree


def claimed(kind: str, tree: nbt.Compound) -> tuple[int, ...] | None:
    """Where the unit's own fields say it is."""
    values = [body(tree).get(key) for key in _CLAIMS[kind]]
    return tuple(int(value) for value in values) if all(isinstance(v, int) for v in values) else None


def view(unit: Unit, namer: Namer, problems: Problems) -> View:
    level = body(unit.tree)
    return View(unit, _sections(unit, level, namer, problems), _heightmap(level), _lit(level),
                compounds(level, "TileEntities", "block_entities"), _entities(level))


def _sections(unit: Unit, level: nbt.Compound, namer: Namer, problems: Problems) -> list[Section]:
    if unit.kind == "column":
        return []
    raw = level.get("Sections") if "Sections" in level else level.get("sections")
    found: dict[tuple[int, int, int], Section] = {}
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict):
            continue
        y = entry.get("Y")
        pos = unit.pos if unit.kind == "cube" else (unit.pos[0], int(y) if isinstance(y, int) else 0, unit.pos[1])
        try:
            found[pos] = Section(pos, states(entry, namer), _light(entry, "BlockLight"), _light(entry, "SkyLight"))
        except SectionError as error:
            problems.add(unit.where, f"section {pos[1]} {error}")
    # A cube without a section, and a 1.17-or-earlier chunk's missing sections, are air the save leaves out.
    absent = [unit.pos] if unit.kind == "cube" else \
        [(unit.pos[0], y, unit.pos[1]) for y in range(16)] if "Level" in unit.tree else []
    for pos in absent:
        found.setdefault(pos, Section(pos, None, None, None))
    return [found[pos] for pos in sorted(found, key=lambda p: p[1])]


def states(section: nbt.Compound, namer: Namer) -> list[str] | None:
    """The state name of every cell, or None for a section that keeps only light."""
    if "Blocks" in section:
        return _legacy(section, namer)
    if "Palette" in section:
        return _paletted(section["Palette"], section.get("BlockStates"))
    block_states = section.get("block_states")
    if isinstance(block_states, dict):
        return _paletted(block_states.get("palette"), block_states.get("data"))
    return None


def _legacy(section: nbt.Compound, namer: Namer) -> list[str]:
    ids = _array(section, "Blocks", CELLS)
    for key, shift in (("Add", 8), ("Add2", 12)):
        if key in section:
            high = nibbles(_array(section, key, CELLS // 2))
            ids = [low | top << shift for low, top in zip(ids, high)]
    meta = nibbles(_array(section, "Data", CELLS // 2)) if "Data" in section else bytes(CELLS)
    return list(map(namer.cells.__getitem__, zip(ids, meta)))  # one C-level pass over the 4096 cells


def _paletted(palette, data) -> list[str]:
    if not isinstance(palette, list) or not palette:
        raise SectionError("has no palette")
    names = [state_name(entry) for entry in palette]
    if not data:
        if len(names) == 1:
            return names * CELLS
        raise SectionError(f"has a palette of {len(names)} but no block data")
    indexes = unpack(data, max(4, (len(names) - 1).bit_length()), CELLS)
    if max(indexes) >= len(names):
        raise SectionError("points past the end of its palette")
    return [names[index] for index in indexes]


def state_name(entry) -> str:
    """A palette entry as minecraft:oak_log[axis=y]: the block's name, then its properties in name order."""
    if not isinstance(entry, dict) or not isinstance(entry.get("Name"), str):
        raise SectionError("has a palette entry without a name")
    properties = entry.get("Properties")
    if not isinstance(properties, dict) or not properties:
        return str(entry["Name"])
    return f"{entry['Name']}[{','.join(f'{key}={properties[key]}' for key in sorted(properties))}]"


def unpack(words, bits: int, count: int) -> list[int]:
    """Values packed ``bits`` wide into 64-bit words from the low bits up. From 1.16 on no value spans two
    words (each word's spare high bits stay unused); before, values run on across words. The number of
    words tells the two apart, and where every value fits a word exactly the two are the same."""
    mask = (1 << bits) - 1
    per = 64 // bits
    out: list[int] = []
    if len(words) == -(-count // per):
        for word in words:
            word &= _WORD
            for _ in range(per):
                out.append(word & mask)
                word >>= bits
        return out[:count]
    if len(words) == -(-count * bits // 64):
        buffer = held = 0
        for word in words:
            buffer |= (word & _WORD) << held
            held += 64
            while held >= bits:
                out.append(buffer & mask)
                buffer >>= bits
                held -= bits
        return out[:count]
    raise SectionError(f"keeps {len(words)} words of block data, which do not hold {count} values of {bits} bits")


def nibbles(packed: bytes) -> bytes:
    """Two 4-bit values a byte, the low one first, as the game packs metadata and light."""
    out = bytearray(len(packed) * 2)
    out[0::2] = packed.translate(_LOW)
    out[1::2] = packed.translate(_HIGH)
    return bytes(out)


def _array(section: nbt.Compound, key: str, length: int) -> bytes:
    value = section.get(key)
    if not isinstance(value, bytes) or len(value) != length:
        raise SectionError(f"has {key} of {len(value) if isinstance(value, bytes) else 'no'} bytes, not {length}")
    return value


def _light(section: nbt.Compound, key: str) -> bytes | None:
    if key not in section:
        return None
    return bytes(_array(section, key, CELLS // 2))


def _heightmap(level: nbt.Compound) -> tuple[str, bytes] | None:
    for key in _HEIGHTMAPS:
        if key in level:
            return key, nbt.canonical(level[key])
    return None


def _lit(level: nbt.Compound) -> bool | None:
    for key in _LIT:
        if key in level:
            return bool(level[key])
    return None


def _entities(level: nbt.Compound) -> list[nbt.Compound]:
    """Every saved entity, riders included."""
    out, waiting = [], compounds(level, "Entities")
    while waiting:
        entity = waiting.pop()
        out.append(entity)
        waiting += compounds(entity, "Passengers")
    return out


def compounds(tree: nbt.Compound, *keys: str) -> list[nbt.Compound]:
    """The compounds in the first of ``keys`` the tree has as a list."""
    for key in keys:
        value = tree.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def entity_uuid(entity: nbt.Compound) -> str | None:
    most, least = entity.get("UUIDMost"), entity.get("UUIDLeast")
    if isinstance(most, int) and isinstance(least, int):
        return str(uuid.UUID(int=(most & _WORD) << 64 | (least & _WORD)))
    parts = entity.get("UUID")
    if isinstance(parts, nbt.IntArray) and len(parts) == 4:  # 1.16 on: four ints, most significant first
        value = 0
        for part in parts:
            value = value << 32 | (part & 0xFFFFFFFF)
        return str(uuid.UUID(int=value))
    return None


def entity_pos(entity: nbt.Compound) -> tuple[float, float, float] | None:
    pos = entity.get("Pos")
    if isinstance(pos, list) and len(pos) == 3 and all(isinstance(value, float) for value in pos):
        return float(pos[0]), float(pos[1]), float(pos[2])
    return None


def block_pos(entity: nbt.Compound) -> tuple[int, int, int] | None:
    values = [entity.get(axis) for axis in "xyz"]
    return tuple(int(value) for value in values) if all(isinstance(v, int) for v in values) else None


def state_changes(a: list[str], b: list[str]) -> dict | None:
    """Where two sections' blocks differ: how many cells, in which layers, and a few of them."""
    if a == b:
        return None
    changed = [index for index in range(CELLS) if a[index] != b[index]]
    return {"cells": len(changed), "layers": sorted({index >> 8 for index in changed}),
            "examples": [_cell(index, a[index], b[index]) for index in changed[:EXAMPLES]]}


def light_changes(a: bytes, b: bytes) -> dict | None:
    """Where two light arrays differ: how many cells, the largest change, the layers, and a few cells."""
    if a == b:
        return None
    first, second = nibbles(a), nibbles(b)
    changed = [index for index in range(CELLS) if first[index] != second[index]]
    return {"cells": len(changed), "largest": max(abs(first[i] - second[i]) for i in changed),
            "layers": sorted({index >> 8 for index in changed}),
            "examples": [_cell(index, first[index], second[index]) for index in changed[:EXAMPLES]]}


def _cell(index: int, a, b) -> dict:
    return {"x": index & 15, "y": index >> 8, "z": (index >> 4) & 15, "a": a, "b": b}


def many(count: int, noun: str) -> str:
    """1 cube, 2 cubes, 3 entities."""
    if count == 1:
        return f"1 {noun}"
    return f"{count} {noun[:-1] + 'ies' if noun.endswith('y') else noun + 's'}"
