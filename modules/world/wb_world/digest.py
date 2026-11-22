"""World digests: hashes of a world's state at one tick, cube by cube, to compare two worlds without their
saves. A digest is a JSON file in the format ``wb/world-digest/v1``; world.read writes one from a save
(``digest=true``), and a program inside the game can write one of the loaded world the same way.

    {
      "format": "wb/world-digest/v1",
      "writer": "world.read",                      what wrote it, for people
      "tick": 72000,                               the game tick of the state (a save's level.dat Time), or null
      "cubes": {                                   16 x 16 x 16 units by cube coordinates; the sections of a
        "0,4,-1": {                                vanilla chunk are its cubes, y being the section index
          "blocks": "<SHA-256 hex>",               of the 4096 state names in y-z-x order, each followed by "\\n"
          "blockLight": "<SHA-256 hex>" | null,    of the 2048 bytes of 4-bit values; null where none is kept
          "skyLight": "<SHA-256 hex>" | null
        }
      },
      "columns": {                                 16 x 16 block columns by chunk coordinates
        "0,-1": {"heightmap": "<SHA-256 hex>" | null}   of the saved heightmap tag's canonical NBT
      },
      "entities": [                                id a UUID (or anything unique), pos to the hundredth
        {"id": "8c2f...", "type": "minecraft:pig", "pos": [8.5, 70.0, -3.25]}
      ],
      "dumps": {                                   optional: cubes kept whole, compared cell by cell
        "0,4,-1": {"palette": ["minecraft:air", "minecraft:stone"],
                   "states": "<base64 of 4096 big-endian 16-bit palette indexes, y-z-x order>",
                   "blockLight": "<base64 of 2048 bytes>", "skyLight": "<base64 of 2048 bytes>"}
      }
    }

A state name is a registry name: minecraft:stone:1 for a 1.12 block with metadata 1, minecraft:oak_log[axis=y]
for a later state (properties in name order), so worlds that number their blocks differently still compare.
A cube a save leaves out is air. Hashes compare as strings: both digests must follow these definitions.

Entities count only inside cubes both digests hold: the game draws an entity from the cube it is in, so one
outside every held cube cannot be seen. Those are reported apart, and do not count against agreement.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import struct
from pathlib import Path

from wb_core.module import ParamError

from .save import Selection
from .units import CELLS, LEFT_OUT, Section, View, entity_pos, entity_uuid, light_changes, many, state_changes

FORMAT = "wb/world-digest/v1"
PARTS = ("blocks", "blockLight", "skyLight")


def states_hash(states: list[str]) -> str:
    return hashlib.sha256(("\n".join(states) + "\n").encode("utf-8")).hexdigest()


class DigestBuilder:
    """A save's digest, built unit by unit as world.read reads them."""

    def __init__(self, selection: Selection, tick: int | None, dumps: set[tuple[int, int, int]]):
        self.selection, self.tick, self.wanted = selection, tick, dumps
        self.cubes: dict[str, dict] = {}
        self.columns: dict[str, dict] = {}
        self.dumps: dict[str, dict] = {}
        self.entities: list[dict] = []

    def add(self, seen: View) -> None:
        for section in seen.sections:
            if self.selection.takes_cube(section.pos):
                key = _key(section.pos)
                self.cubes[key] = {"blocks": states_hash(section.states or LEFT_OUT),
                                   "blockLight": _sha(section.block_light), "skyLight": _sha(section.sky_light)}
                if section.pos in self.wanted:
                    self.dumps[key] = _dump(section)
        if seen.unit.kind != "cube":
            self.columns[_key(seen.unit.pos)] = {"heightmap": _sha(seen.heightmap[1]) if seen.heightmap else None}
        for entity in seen.entities:
            pos = entity_pos(entity)
            if pos is not None and self.selection.holds(pos):
                kind = str(entity.get("id", "?"))
                self.entities.append({"id": entity_uuid(entity) or f"{kind}@{_key(pos)}", "type": kind,
                                      "pos": [round(value, 2) for value in pos]})

    def document(self) -> dict:
        return {"format": FORMAT, "writer": "world.read", "tick": self.tick,
                "cubes": _ordered(self.cubes), "columns": _ordered(self.columns),
                "entities": sorted(self.entities, key=lambda entity: entity["id"]), "dumps": _ordered(self.dumps)}


def load(path: Path, side: str) -> dict:
    """A digest file, checked, with its keys normalised and its dumps decoded."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ParamError(f"{side}: {path.name} is not a JSON file ({error})") from None
    if not isinstance(document, dict) or document.get("format") != FORMAT:
        raise ParamError(f"{side}: {path.name} is not a world digest: it lacks \"format\": \"{FORMAT}\"")
    try:
        return {"writer": document.get("writer"), "tick": document.get("tick"),
                "cubes": _table(document, "cubes", 3), "columns": _table(document, "columns", 2),
                "entities": [_entity(entry) for entry in _of(document.get("entities", []), list, "entities")],
                "dumps": {key: _decoded(dump) for key, dump in _table(document, "dumps", 3).items()}}
    except ValueError as error:
        raise ParamError(f"{side}: {path.name}: {error}") from None


def compare(a: dict, b: dict, ignore_tick: bool) -> dict:
    """What differs between two loaded digests, by category; empty categories are left out."""
    held = a["cubes"].keys() & b["cubes"].keys()
    found: dict[str, list] = {}
    if not ignore_tick and a["tick"] != b["tick"]:
        found["tick"] = [a["tick"], b["tick"]]
    found["cubesOnlyInA"] = _sorted(a["cubes"].keys() - b["cubes"].keys())
    found["cubesOnlyInB"] = _sorted(b["cubes"].keys() - a["cubes"].keys())
    for part in PARTS:
        found[part] = _sorted(key for key in held if a["cubes"][key].get(part) != b["cubes"][key].get(part))
    columns = a["columns"].keys() & b["columns"].keys()
    found["heightmaps"] = _sorted(key for key in columns
                                  if a["columns"][key].get("heightmap") != b["columns"][key].get("heightmap"))
    found["columnsOnlyInA"] = _sorted(a["columns"].keys() - b["columns"].keys())
    found["columnsOnlyInB"] = _sorted(b["columns"].keys() - a["columns"].keys())
    inside_a, outside_a = _split(a["entities"], held)
    inside_b, outside_b = _split(b["entities"], held)
    found.update(_entity_changes(inside_a, inside_b))
    outside = sorted({_line(e) for e in outside_a.values()} ^ {_line(e) for e in outside_b.values()})
    dumped = _sorted(a["dumps"].keys() & b["dumps"].keys())
    voxels = {key: changes for key in dumped if (changes := _voxels(a["dumps"][key], b["dumps"][key]))}
    return {"agree": not any(found.values()), "differences": {key: value for key, value in found.items() if value},
            "outsideHeldCubes": {"a": len(outside_a), "b": len(outside_b), "differing": outside},
            "voxels": voxels}


_PHRASES = {"cubesOnlyInA": ("cube", "only in a"), "cubesOnlyInB": ("cube", "only in b"),
            "blocks": ("cube", "with different blocks"), "blockLight": ("cube", "with different block light"),
            "skyLight": ("cube", "with different sky light"), "heightmaps": ("column", "with a different heightmap"),
            "columnsOnlyInA": ("column", "only in a"), "columnsOnlyInB": ("column", "only in b"),
            "entitiesOnlyInA": ("entity", "only in a"), "entitiesOnlyInB": ("entity", "only in b"),
            "entitiesMoved": ("entity", "moved"), "entitiesRetyped": ("entity", "changed type")}


def findings(differences: dict) -> list[str]:
    """One line per differing category, as a person would say it."""
    lines = []
    if "tick" in differences:
        lines.append("the digests are of different ticks ({} and {})".format(*differences["tick"]))
    for key, value in differences.items():
        if key in _PHRASES:
            noun, phrase = _PHRASES[key]
            lines.append(f"{many(len(value), noun)} {phrase}")
    return lines


def _entity_changes(a: dict[str, dict], b: dict[str, dict]) -> dict[str, list]:
    both = sorted(a.keys() & b.keys())
    return {"entitiesOnlyInA": sorted(a.keys() - b.keys()), "entitiesOnlyInB": sorted(b.keys() - a.keys()),
            "entitiesMoved": [{"id": key, "a": a[key]["pos"], "b": b[key]["pos"]} for key in both
                              if a[key]["pos"] != b[key]["pos"]],
            "entitiesRetyped": [{"id": key, "a": a[key]["type"], "b": b[key]["type"]} for key in both
                                if a[key]["type"] != b[key]["type"]]}


def _split(entities: list[dict], held: set[str]) -> tuple[dict, dict]:
    """Entities by id: those inside a cube both digests hold, and the rest."""
    inside, outside = {}, {}
    for entity in entities:
        cube = _key(tuple(math.floor(value / 16) for value in entity["pos"]))
        (inside if cube in held else outside)[entity["id"]] = entity
    return inside, outside


def _voxels(a: dict, b: dict) -> dict:
    found = {}
    if a["states"] is not None and b["states"] is not None and (changes := state_changes(a["states"], b["states"])):
        found["blocks"] = changes
    for part in ("blockLight", "skyLight"):
        if (a[part] is None) != (b[part] is None):
            found[part] = {"present": [a[part] is not None, b[part] is not None]}
        elif a[part] is not None and (changes := light_changes(a[part], b[part])):
            found[part] = changes
    return found


def _dump(section: Section) -> dict:
    states = section.states or LEFT_OUT
    palette = list(dict.fromkeys(states))
    index = {name: number for number, name in enumerate(palette)}
    out = {"palette": palette,
           "states": base64.b64encode(struct.pack(f">{CELLS}H", *(index[s] for s in states))).decode("ascii")}
    for part, value in (("blockLight", section.block_light), ("skyLight", section.sky_light)):
        if value is not None:
            out[part] = base64.b64encode(value).decode("ascii")
    return out


def _decoded(dump: dict) -> dict:
    if not isinstance(dump, dict):
        raise ValueError("a dump is not a JSON object")
    states = None
    if "states" in dump:
        palette = _of(dump.get("palette"), list, "a dump's palette")
        indexes = struct.unpack(f">{CELLS}H", _bytes(dump["states"], CELLS * 2))
        if not palette or max(indexes) >= len(palette):
            raise ValueError("a dump's states point past its palette")
        states = [str(palette[i]) for i in indexes]
    return {"states": states, **{part: _bytes(dump[part], CELLS // 2) if part in dump else None
                                 for part in ("blockLight", "skyLight")}}


def _bytes(text, length: int) -> bytes:
    try:
        raw = base64.b64decode(text, validate=True)
    except (binascii.Error, TypeError, ValueError):
        raise ValueError("a dump holds text that is not base64") from None
    if len(raw) != length:
        raise ValueError(f"a dump holds {len(raw)} bytes where {length} belong")
    return raw


def _table(document: dict, name: str, dims: int) -> dict[str, dict]:
    """A map of coordinates to objects, its keys written the one way ("0,4,-1")."""
    out = {}
    for key, entry in _of(document.get(name, {}), dict, name).items():
        parts = str(key).split(",")
        if len(parts) != dims or not isinstance(entry, dict) or not all(_integer(part) for part in parts):
            raise ValueError(f"{name} has {key!r}, which is not {dims} whole coordinates naming an object")
        out[_key(tuple(int(part) for part in parts))] = entry
    return out


def _integer(text: str) -> bool:
    return text.strip().removeprefix("-").isdigit()


def _entity(entry) -> dict:
    pos = entry.get("pos") if isinstance(entry, dict) else None
    if not (isinstance(pos, list) and len(pos) == 3 and all(isinstance(v, (int, float)) for v in pos)):
        raise ValueError("an entity has no pos of three numbers")
    return {"id": str(entry.get("id")), "type": str(entry.get("type")), "pos": pos}


def _of(value, kind: type, what: str):
    if not isinstance(value, kind):
        raise ValueError(f"{what} is not a JSON {'object' if kind is dict else 'array'}")
    return value


def _line(entity: dict) -> str:
    return f"{entity['id']} {entity['type']} {' '.join(map(str, entity['pos']))}"


def _sha(data: bytes | None) -> str | None:
    return hashlib.sha256(data).hexdigest() if data is not None else None


def _key(pos) -> str:
    return ",".join(str(value) for value in pos)


def _coords(key: str) -> tuple[int, ...]:
    return tuple(int(part) for part in key.split(","))


def _sorted(keys) -> list[str]:
    return sorted(keys, key=_coords)


def _ordered(table: dict[str, dict]) -> dict[str, dict]:
    return {key: table[key] for key in _sorted(table)}
