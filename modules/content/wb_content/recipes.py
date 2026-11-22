"""content.recipe-graph: a recipe dump read as a graph of items.

The dump is JSON lines (or one JSON array, or either gzipped), one recipe per record:

    {"type": "minecraft:crafting", "id": "minecraft:torch", "source": "postInit/main.groovy:12",
     "inputs": ["minecraft:coal", {"any": ["minecraft:stick", "ore:stickWood"]}],
     "outputs": [{"id": "minecraft:torch", "count": 4}], "removed": false}

An input or output is an id (``minecraft:iron_ingot``, ``minecraft:wool:3``, ``ore:ingotIron``,
``fluid:water``) or a table naming one (``id``, ``item``, ``fluid``, ``ore`` or ``tag``, with ``meta``,
``count`` or ``amount``; ``"consumed": false`` for a catalyst or tool not used up). An input that accepts
alternatives is a list or ``{"any": [...]}``. An id ending in ``:*`` stands for every id under it
(``minecraft:wool:*``). A removed recipe (``"removed": true``) makes nothing.

What is found: dead ends (inputs no active recipe makes), cycles (items that make each other) and whether
anything outside each one leads in, items only removed recipes made, and items no chain of recipes reaches
from what the world supplies.
"""

from __future__ import annotations

import gzip
import json
from collections import defaultdict
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path

from .graph import components, cycle_in

ID_KEYS = (("id", ""), ("item", ""), ("name", ""), ("fluid", "fluid:"), ("ore", "ore:"), ("tag", "tag:"))
ALTERNATIVES = ("any", "oneOf", "alternatives")
SHOWN = 5


@dataclass
class Recipe:
    number: int  # its record in the dump, from 1
    type: str
    id: str
    source: str
    removed: bool
    slots: list[list[str]]  # each input's accepted ids
    consumed: list[bool]
    outputs: list[str]

    def ref(self) -> str:
        name = " ".join(part for part in (self.type, self.id) if part) or "a recipe"
        return f"{name} ({self.source or f'record {self.number}'})"


@dataclass
class Dump:
    recipes: list[Recipe] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)
    skipped: int = 0  # records of types not read


@dataclass
class Found:
    report: dict
    findings: list[str]


def _records(path: Path):
    opener = gzip.open if path.suffix.lower() == ".gz" else open
    with opener(path, "rt", encoding="utf-8-sig", errors="replace") as handle:
        text = handle.read()
    stripped = text.lstrip()
    if stripped.startswith("["):
        try:
            for number, record in enumerate(json.loads(stripped), 1):
                yield number, record, None
            return
        except ValueError:
            pass  # not one array: read it as lines
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            yield number, json.loads(line), None
        except ValueError as error:
            yield number, None, f"line {number}: not JSON ({error.msg})"


def _id(value) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if not isinstance(value, dict):
        return None
    for key, prefix in ID_KEYS:
        if isinstance(value.get(key), str) and value[key].strip():
            name = value[key].strip()
            if prefix and not name.startswith(prefix):
                name = prefix + name
            meta = value.get("meta", value.get("damage"))
            if isinstance(meta, int) and not isinstance(meta, bool) and name.count(":") == 1:
                name = f"{name}:{'*' if meta == 32767 else meta}"
            return name
    return None


def _ingredient(value) -> tuple[list[str], bool] | None:
    """An input's accepted ids and whether it is used up."""
    consumed = True
    if isinstance(value, dict):
        consumed = value.get("consumed", True) is not False and value.get("catalyst") is not True
        listed = next((value[k] for k in ALTERNATIVES if isinstance(value.get(k), list)), None)
        if listed is not None:
            value = listed
    if isinstance(value, list):
        ids = [_id(v) for v in value]
        return ([i for i in ids if i], consumed) if any(ids) else None
    single = _id(value)
    return ([single], consumed) if single else None


def read(path: Path, *, ignore: list[str], types: list[str]) -> Dump:
    dump = Dump()

    def kept(item: str) -> bool:
        return not any(fnmatchcase(item, pattern) for pattern in ignore)

    for number, record, error in _records(path):
        if error:
            dump.unreadable.append(error)
            continue
        if not isinstance(record, dict) or not isinstance(record.get("outputs", []), list) \
                or not isinstance(record.get("inputs", []), list):
            dump.unreadable.append(f"record {number}: not a recipe (a table with inputs and outputs lists)")
            continue
        kind = str(record.get("type") or "")
        if types and not any(fnmatchcase(kind, pattern) for pattern in types):
            dump.skipped += 1
            continue
        slots, consumed = [], []
        for value in record.get("inputs", []):
            ingredient = _ingredient(value)
            if ingredient is None:
                dump.unreadable.append(f"record {number}: an input names no id: {json.dumps(value)[:80]}")
                continue
            accepted = [i for i in ingredient[0] if kept(i)]
            if accepted:
                slots.append(accepted)
                consumed.append(ingredient[1])
        outputs = [o for o in (_id(v) for v in record.get("outputs", [])) if o and kept(o)]
        dump.recipes.append(Recipe(number, kind, str(record.get("id") or ""), str(record.get("source") or ""),
                                   record.get("removed") is True, slots, consumed, outputs))
    return dump


def _base(item: str) -> str | None:
    """The id an id with a meta or wildcard belongs to: minecraft:wool:3 and minecraft:wool:* to minecraft:wool."""
    head, _, tail = item.rpartition(":")
    return head if head.count(":") >= 1 and (tail.isdigit() or tail == "*") else None


def analyse(dump: Dump, *, obtainable: list[str], cancel=lambda: None) -> Found:
    findings: list[str] = []
    active = [r for r in dump.recipes if not r.removed]
    removed = [r for r in dump.recipes if r.removed]
    makers: dict[str, list[Recipe]] = defaultdict(list)
    for recipe in active:
        for output in recipe.outputs:
            makers[output].append(recipe)
    bases = {b for o in makers for b in (_base(o), o) if b}
    gone: dict[str, list[Recipe]] = defaultdict(list)
    for recipe in removed:
        for output in recipe.outputs:
            gone[output].append(recipe)

    def made(item: str) -> bool:
        if item in makers:
            return True
        if item.endswith(":*"):
            return item[:-2] in bases
        base = _base(item)
        return base is not None and f"{base}:*" in makers

    def supplied(item: str) -> bool:
        return any(fnmatchcase(item, pattern) for pattern in obtainable)

    needers: dict[str, list[Recipe]] = defaultdict(list)
    dead: dict[str, dict] = {}
    for recipe in active:
        for slot in recipe.slots:
            for item in slot:
                needers[item].append(recipe)
            if any(made(item) for item in slot):
                continue
            label = " | ".join(slot)
            row = dead.setdefault(label, {"id": label, "accepts": slot, "neededBy": 0, "first": recipe.ref(),
                                          "obtainable": any(supplied(i) for i in slot),
                                          "removedOnly": all(i in gone for i in slot)})
            row["neededBy"] += 1
    items = set(makers) | set(gone) | set(needers)
    removed_only = []
    for item in sorted(i for i in gone if not made(i)):
        need = needers.get(item, [])
        removed_only.append({"id": item, "removedBy": [r.ref() for r in gone[item][:SHOWN]], "neededBy": len(need),
                             "first": need[0].ref() if need else None, "obtainable": supplied(item)})
        if need and not supplied(item):
            findings.append(f"{item} is made only by removed recipes ({gone[item][0].ref()}), but "
                            + (f"{len(need)} active recipes still need it, the first: " if len(need) != 1
                               else "an active recipe still needs it: ") + need[0].ref())
    ranked = sorted(dead.values(), key=lambda r: (-r["neededBy"], r["id"]))
    dead_ends = [row for row in ranked if not row["removedOnly"]]
    if obtainable:
        for row in dead_ends:
            if not row["obtainable"]:
                findings.append(f"dead end: {row['id']} is needed by {row['neededBy']} recipe"
                                f"{'s' if row['neededBy'] != 1 else ''} (the first: {row['first']}), but no recipe "
                                "makes it and it is not obtainable")
    cancel()
    seeds = {i for i in items if supplied(i)} if obtainable else \
        {i for row in dead_ends for i in row["accepts"]}
    reached = _reach(active, seeds)
    edges: dict[str, set[str]] = defaultdict(set)
    for recipe in active:
        for slot, used in zip(recipe.slots, recipe.consumed):
            if used:
                for item in slot:
                    edges[item].update(o for o in recipe.outputs if o != item)
    for output in list(makers):  # an item leads into the wildcard inputs it satisfies
        base = _base(output)
        for wildcard in ([f"{base}:*"] if base else []) + [f"{output}:*"]:
            if wildcard in edges and wildcard != output:
                edges[output].add(wildcard)
    cancel()
    cycles = []
    for component in components(sorted(edges), edges):
        closed = not any(item in reached for item in component)
        path = cycle_in(component, edges)
        cycles.append({"items": component, "size": len(component), "cycle": path, "closed": closed})
        if closed and obtainable:
            findings.append(f"closed cycle: {' -> '.join(path[:6])}{' ...' if len(path) > 6 else ''}: none of its "
                            f"{len(component)} items can be made from anything outside it, nor is one obtainable")
    unreached = sorted(i for i in makers if i not in reached)
    if dump.unreadable:
        findings.append(f"{len(dump.unreadable)} record{'s' if len(dump.unreadable) != 1 else ''} of the dump could "
                        f"not be read, the first: {dump.unreadable[0]}")
    by_type: dict[str, dict] = defaultdict(lambda: {"active": 0, "removed": 0})
    for recipe in dump.recipes:
        by_type[recipe.type or "(none)"]["removed" if recipe.removed else "active"] += 1
    unused = sorted(i for i in makers if i not in needers)
    report = {"counts": {"active": len(active), "removed": len(removed), "items": len(items),
                         "skippedTypes": dump.skipped, "unreadable": len(dump.unreadable)},
              "byType": dict(sorted(by_type.items())), "deadEnds": dead_ends, "removedOnly": removed_only,
              "cycles": cycles, "unreached": unreached, "unusedOutputs": len(unused),
              "seeds": "obtainable" if obtainable else "dead ends (assumed obtainable)",
              "unreadable": dump.unreadable[:100]}
    return Found(report, findings)


def _reach(recipes: list[Recipe], seeds: set[str]) -> set[str]:
    """Every item a chain of recipes makes from the seeds: a recipe fires once each input has an accepted item
    reached (an id ending :* is reached by any item under it)."""
    waiting: dict[str, list[tuple[int, int]]] = defaultdict(list)
    missing = []
    for index, recipe in enumerate(recipes):
        missing.append(len(recipe.slots))
        for slot_index, slot in enumerate(recipe.slots):
            for item in slot:
                key = item[:-2] + ":*" if item.endswith(":*") else item
                waiting[key].append((index, slot_index))
    reached: set[str] = set()
    satisfied: set[tuple[int, int]] = set()
    queue = list(seeds)
    queue += [o for index, recipe in enumerate(recipes) if not recipe.slots for o in recipe.outputs]
    while queue:
        item = queue.pop()
        if item in reached:
            continue
        reached.add(item)
        keys = [item]
        base = _base(item)
        keys += [f"{base}:*"] if base else []
        keys += [f"{item}:*"]
        for key in keys:
            for index, slot_index in waiting.get(key, ()):
                if (index, slot_index) in satisfied:
                    continue
                satisfied.add((index, slot_index))
                missing[index] -= 1
                if missing[index] == 0:
                    queue += [o for o in recipes[index].outputs if o not in reached]
    return reached
