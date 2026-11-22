"""world.diff: two saves compared unit by unit, through a canonical view of each unit's NBT.

The view sorts compound keys; keys the lists whose items say who they are (entities by UUID, block
entities and scheduled ticks by position, sections by Y, items by Slot, others by Name or id), so their
order does not count; and stands in for each section's block arrays the state name of every cell, named
through its own world's registry, so worlds that number their blocks differently still compare. Fields an
ignore pattern matches are left out: a bare name matches that key anywhere, a dotted pattern the path of
keys from the unit's root (Level.Entities); both take shell wildcards.
"""

from __future__ import annotations

import fnmatch
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field

from . import nbt, units
from .region import LAYOUTS
from .save import Pair, Save, Selection, level_facts, paired
from .units import Namer, SectionError

BLOCK_KEYS = ("Blocks", "Add", "Add2", "Data", "Palette", "BlockStates", "block_states")
LEVEL_FIELDS = ("seed", "generator", "options", "version", "dataVersion")
SHOWN = 50       # differences described for one unit
DETAILED = 200   # units described difference by difference


class Blocks:
    """A section's block arrays. Under one registry, equal raw arrays settle equality; otherwise the cells
    are decoded to state names."""

    def __init__(self, section: nbt.Compound, namer: Namer):
        self.section, self.namer = section, namer
        self.raw = b"".join(key.encode() + nbt.canonical(section[key]) for key in BLOCK_KEYS if key in section)
        self._states: list[str] | None = None

    def states(self) -> list[str]:
        if self._states is None:
            self._states = units.states(self.section, self.namer)
        return self._states


class Keyed(dict):
    """A list whose items are compared by who they are rather than where they stand."""


@dataclass(frozen=True)
class Rules:
    patterns: tuple[str, ...]
    namer: Namer

    def ignored(self, path: tuple[str, ...]) -> bool:
        dotted = ".".join(path)
        return any(fnmatch.fnmatchcase(dotted if "." in pattern else path[-1], pattern) for pattern in self.patterns)


class Differences:
    """One unit's differences: every one counted, the first SHOWN described, and the fields they fall in."""

    def __init__(self):
        self.count = 0
        self.shown: list[dict] = []
        self.fields: set[str] = set()

    def add(self, path: str, field_: str, detail: dict) -> None:
        self.count += 1
        self.fields.add(field_)
        if len(self.shown) < SHOWN:
            self.shown.append({"path": path, **detail})


@dataclass
class Tally:
    compared: int = 0
    identical: int = 0
    differing: list[str] = field(default_factory=list)
    only_a: list[str] = field(default_factory=list)
    only_b: list[str] = field(default_factory=list)
    undecodable: list[str] = field(default_factory=list)
    fields: Counter = field(default_factory=Counter)
    details: dict[str, dict] = field(default_factory=dict)


def diff_worlds(a: Save, b: Save, selection: Selection, patterns: tuple[str, ...],
                check: Callable[[], None]) -> dict:
    same = a.registry == b.registry
    rules = Rules(patterns, Namer(a.registry)), Rules(patterns, Namer(b.registry))
    tally = Tally()
    for layout in LAYOUTS:
        for pair in paired(a, b, layout, selection):
            check()
            name = f"{layout.kind} {','.join(map(str, pair.pos))}"
            if not (pair.in_a and pair.in_b):
                (tally.only_a if pair.in_a else tally.only_b).append(name)
            elif pair.a is not None and pair.b is not None:  # an unreadable side is already a problem
                _compare(name, pair, rules, same, tally)
    return {"level": level_differences(a, b), "registry": registry_differences(a, b), "ignore": list(patterns),
            "compared": tally.compared, "identical": tally.identical, "differing": tally.differing,
            "onlyInA": tally.only_a, "onlyInB": tally.only_b, "fields": dict(tally.fields.most_common()),
            "details": tally.details, "undecodable": tally.undecodable}


def _compare(name: str, pair: Pair, rules: tuple[Rules, Rules], same: bool, tally: Tally) -> None:
    tally.compared += 1
    try:
        found = unit_differences(pair.a, pair.b, rules, same)
    except SectionError as error:
        tally.undecodable.append(f"{name}: a section {error}")
        return
    if not found.count:
        tally.identical += 1
        return
    tally.differing.append(name)
    tally.fields.update(found.fields)
    if len(tally.details) < DETAILED:
        tally.details[name] = {"differences": found.count, "shown": found.shown}


def unit_differences(a: units.Unit, b: units.Unit, rules: tuple[Rules, Rules], same: bool) -> Differences:
    found = Differences()
    if not (same and a.raw == b.raw):
        walk(canon(a.tree, (), rules[0]), canon(b.tree, (), rules[1]), "", "", same, found)
    return found


def canon(value, path: tuple[str, ...], rules: Rules):
    """The comparable view of a value found at ``path``, the compound keys leading to it."""
    if isinstance(value, dict):
        # Only a section stands for its cells: a mod's block entity may keep a "Blocks" or "Palette" of its own.
        blocky = path[-1:] in (("Sections",), ("sections",)) and \
            any(key in value for key in ("Blocks", "Palette", "block_states"))
        out = {key: canon(item, path + (key,), rules) for key, item in value.items()
               if not (blocky and key in BLOCK_KEYS) and not rules.ignored(path + (key,))}
        if blocky and not rules.ignored(path + ("blocks",)):
            out["blocks"] = Blocks(value, rules.namer)
        return out
    if isinstance(value, list):
        items = [canon(item, path, rules) for item in value]
        names = identities(value)
        return Keyed(zip(names, items)) if names else items
    return value


def identities(items: list) -> list[str] | None:
    """What each item of a list of compounds goes by, under the first rule that names every item once."""
    if not items or not all(isinstance(item, dict) for item in items):
        return None
    for rule in _RULES:
        names = [rule(item) for item in items]
        if None not in names and len(set(names)) == len(names):
            return names
    return None


def _by_position(item: nbt.Compound) -> str | None:
    pos = units.block_pos(item)
    if pos is None:
        return None
    block = item.get("i")  # a scheduled tick: several blocks may wait at one position
    return ",".join(map(str, pos)) + (f" {block}" if block is not None else "")


def _by_field(name: str) -> Callable[[nbt.Compound], str | None]:
    def rule(item: nbt.Compound) -> str | None:
        value = item.get(name)
        return f"{name}={value}" if isinstance(value, (int, str)) else None
    return rule


_RULES = (units.entity_uuid, _by_position, _by_field("Y"), _by_field("Slot"), _by_field("Name"),
          _by_field("id"), _by_field("Id"))


def walk(a, b, path: str, field_: str, same: bool, out: Differences) -> None:
    """Records where two canonical views differ; ``field_`` is the path without list keys, for tallies."""
    if isinstance(a, Blocks) and isinstance(b, Blocks):
        if not (same and a.raw == b.raw) and (changes := units.state_changes(a.states(), b.states())):
            out.add(path, field_, changes)
    elif type(a) is not type(b):
        out.add(path, field_, {"a": describe(a), "b": describe(b), "types": [_type(a), _type(b)]})
    elif isinstance(a, dict):
        _walk_keys(a, b, path, field_, same, out)
    elif isinstance(a, list):
        if len(a) != len(b):
            out.add(path, field_, {"a": describe(a), "b": describe(b)})
            return
        for index, (first, second) in enumerate(zip(a, b)):
            walk(first, second, f"{path}[{index}]", field_, same, out)
    elif a != b:
        out.add(path, field_, _change(a, b, field_))


def _walk_keys(a: dict, b: dict, path: str, field_: str, same: bool, out: Differences) -> None:
    keyed = isinstance(a, Keyed)
    for key in sorted(a.keys() | b.keys(), key=str):
        inner = f"{path}[{key}]" if keyed else f"{path}.{key}" if path else key
        inner_field = field_ if keyed else f"{field_}.{key}" if field_ else key
        if key in a and key in b:
            walk(a[key], b[key], inner, inner_field, same, out)
        else:
            out.add(inner, inner_field, {"a": describe(a[key]) if key in a else "absent",
                                         "b": describe(b[key]) if key in b else "absent"})


def _change(a, b, field_: str) -> dict:
    name = field_.rsplit(".", 1)[-1]
    if isinstance(a, bytes) and name in ("BlockLight", "SkyLight") and len(a) == len(b) == units.CELLS // 2:
        return units.light_changes(a, b)
    if isinstance(a, (bytes, tuple)) and len(a) == len(b):
        return {"values": sum(x != y for x, y in zip(a, b)), "of": len(a)}
    return {"a": describe(a), "b": describe(b)}


def describe(value):
    if isinstance(value, Blocks):
        return "block arrays"
    if isinstance(value, dict):
        return f"{len(value)} entries"
    if isinstance(value, list):
        return f"{len(value)} items"
    if isinstance(value, (bytes, tuple)):
        return f"{len(value)} values"
    if isinstance(value, str) and len(value) > 200:
        return value[:200] + "..."
    return nbt.to_json(value)


def _type(value) -> str:
    if isinstance(value, Keyed):
        return "List (keyed)"
    return {dict: "Compound", list: "List"}.get(type(value), type(value).__name__)


def level_differences(a: Save, b: Save) -> list[dict]:
    facts = level_facts(a.level), level_facts(b.level)
    return [{"field": key, "a": facts[0][key], "b": facts[1][key]} for key in LEVEL_FIELDS
            if facts[0][key] != facts[1][key]]


def registry_differences(a: Save, b: Save) -> dict:
    ids_a = {name: number for number, name in a.registry.items()}
    ids_b = {name: number for number, name in b.registry.items()}
    shared = ids_a.keys() & ids_b.keys()
    return {"same": a.registry == b.registry,
            "renumbered": sorted(name for name in shared if ids_a[name] != ids_b[name]),
            "onlyInA": sorted(ids_a.keys() - ids_b.keys()), "onlyInB": sorted(ids_b.keys() - ids_a.keys())}
