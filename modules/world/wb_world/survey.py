"""What world.read tallies over the units it reads: block states inside the box, block entities (and the NBT
of the ids asked for), entities, heightmaps and light."""

from __future__ import annotations

from collections import Counter, defaultdict

from . import nbt
from .save import Selection
from .units import AIR, Section, View, block_pos, entity_pos


class Survey:
    def __init__(self, selection: Selection, keep: set[str], nbt_limit: int):
        self.selection = selection
        self.keep = keep            # block-entity ids whose NBT the report keeps; "*" keeps every id
        self.nbt_limit = nbt_limit
        self.units: Counter = Counter()
        self.cubes_by_y: Counter = Counter()
        self.blocks: Counter = Counter()
        self.lowest = self.highest = None   # block y of the lowest and highest layer holding more than air
        self.block_entities: Counter = Counter()
        self.kept: dict[str, list] = defaultdict(list)
        self.kept_count = self.not_kept = 0
        self.entities: Counter = Counter()
        self.heightmaps: Counter = Counter()
        self.light: Counter = Counter()

    def add(self, seen: View) -> None:
        unit = seen.unit
        self.units[unit.kind] += 1
        if unit.kind == "cube":
            self.cubes_by_y[unit.pos[1]] += 1
        else:  # chunks and columns carry the heightmap; cubes leave it to their column
            self.heightmaps[seen.heightmap[0] if seen.heightmap else "none"] += 1
        if seen.lit is not None:
            self.light["lit" if seen.lit else "unlit"] += 1
        for section in seen.sections:
            if self.selection.takes_cube(section.pos):
                self._light(section)
                self._blocks(section)
        self._block_entities(seen)
        for entity in seen.entities:
            pos = entity_pos(entity)
            if pos is None or self.selection.holds(pos):
                self.entities[str(entity.get("id", "?"))] += 1

    def report(self) -> dict:
        return {"units": dict(self.units),
                "cubesByY": {str(y): count for y, count in sorted(self.cubes_by_y.items())},
                "blocks": dict(sorted(self.blocks.items())),
                "nonAirY": [self.lowest, self.highest] if self.lowest is not None else None,
                "blockEntities": {"counts": dict(sorted(self.block_entities.items())), "nbt": dict(self.kept),
                                  "notKept": self.not_kept},
                "entities": dict(sorted(self.entities.items())),
                "heightmaps": dict(sorted(self.heightmaps.items())),
                "light": dict(sorted(self.light.items()))}

    def _light(self, section: Section) -> None:
        if section.states is None and section.block_light is None and section.sky_light is None:
            return  # air the save leaves out: there is no light to have or lack
        self.light["sections"] += 1
        self.light["blockLight"] += section.block_light is not None
        self.light["skyLight"] += section.sky_light is not None

    def _blocks(self, section: Section) -> None:
        if section.states is None:
            return
        spans = self.selection.ranges(section.pos)
        if spans is None:  # the whole section: one count, and layers only to place blocks other than air
            self.blocks.update(section.states)
            if AIR.issuperset(section.states):
                return
        layers = list(_layers(section, spans))
        if spans is not None:
            for _, layer in layers:
                self.blocks.update(layer)
        filled = [y for y, layer in layers if layer and not AIR.issuperset(layer)]
        if filled:
            low, high = section.pos[1] * 16 + filled[0], section.pos[1] * 16 + filled[-1]
            self.lowest = low if self.lowest is None else min(self.lowest, low)
            self.highest = high if self.highest is None else max(self.highest, high)

    def _block_entities(self, seen: View) -> None:
        for entity in seen.block_entities:
            pos = block_pos(entity)
            if pos is not None and not self.selection.holds(pos):
                continue
            kind = str(entity.get("id", "?"))
            self.block_entities[kind] += 1
            if "*" not in self.keep and kind not in self.keep:
                continue
            if self.kept_count < self.nbt_limit:
                self.kept[kind].append({"pos": list(pos) if pos else None, "unit": seen.unit.where,
                                        "nbt": nbt.to_json(entity)})
                self.kept_count += 1
            else:
                self.not_kept += 1


def _layers(section: Section, spans):
    """Each layer's states (local y, then the cells inside the box), whole layers when the box holds all."""
    states = section.states
    if spans is None:
        return ((y, states[y << 8:(y + 1) << 8]) for y in range(16))
    xs, ys, zs = spans
    return ((y, [states[y << 8 | z << 4 | x] for z in zs for x in xs]) for y in ys)
