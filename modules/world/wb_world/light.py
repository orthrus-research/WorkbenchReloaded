"""world.light-diff: saved light compared only where it has settled, by the interior rule.

Light at the edge of what a world holds may still be waiting on a neighbour it does not have, so it says
nothing about whether two worlds light alike. A unit takes part only when it and every neighbour are saved in
both worlds: the 8 chunks around a vanilla chunk, the 26 cubes around a cube. Its sections are then compared
array by array, and each difference says whether the blocks there are the same in both worlds (light that
differs over the same blocks) and whether each world marks the unit's light done.
"""

from __future__ import annotations

import itertools
from collections import Counter
from collections.abc import Callable

from wb_core.module import ParamError

from .region import ANVIL, CUBES, Layout
from .save import Save, Selection, paired
from .units import LEFT_OUT, Namer, Section, View, light_changes, view

LISTED = 500  # differing sections the report describes one by one


def layout_of(a: Save, b: Save) -> Layout:
    """Where both worlds keep their blocks: in cubes, or in vanilla chunks."""
    kinds = [CUBES if CUBES in save.layouts else ANVIL if ANVIL in save.layouts else None for save in (a, b)]
    if None in kinds:
        raise ParamError(f"{'a' if kinds[0] is None else 'b'} keeps no blocks: it has no region or region3d folder")
    if kinds[0] is not kinds[1]:
        raise ParamError("the worlds keep their blocks differently: one in cubes, the other in vanilla chunks")
    return kinds[0]


def interior(held: set[tuple[int, ...]], dims: int) -> set[tuple[int, ...]]:
    """The positions whose every neighbour is held too."""
    steps = [step for step in itertools.product((-1, 0, 1), repeat=dims) if any(step)]
    return {pos for pos in held if all(tuple(p + s for p, s in zip(pos, step)) in held for step in steps)}


def light_diff(a: Save, b: Save, selection: Selection, check: Callable[[], None]) -> dict:
    layout = layout_of(a, b)
    held = a.held(layout), b.held(layout)
    inner = interior(held[0], layout.dims), interior(held[1], layout.dims)
    wanted = {pos for pos in inner[0] & inner[1] if selection.takes_unit(layout, pos)}
    namers = Namer(a.registry), Namer(b.registry)
    edges = _Edges(held[0])
    tally = Counter(compared=0, sections=0, blocksDiffer=0, differingSameBlocks=0, differingUnlit=0)
    listed, by_y, by_edge = [], Counter(), Counter()
    for pair in paired(a, b, layout, selection, wanted):
        check()
        if pair.a is None or pair.b is None:
            continue  # unreadable: already among its world's problems
        tally["compared"] += 1
        for found in _differing(view(pair.a, namers[0], a.problems), view(pair.b, namers[1], b.problems), tally):
            by_y[found["pos"][1]] += 1
            by_edge[edges.distance(pair.pos)] += 1
            if len(listed) < LISTED:
                listed.append(found)
    return {"layout": layout.kind, "a": {"held": len(held[0]), "interior": len(inner[0])},
            "b": {"held": len(held[1]), "interior": len(inner[1])}, **tally, "differing": sum(by_y.values()),
            "listed": listed, "byY": {str(y): n for y, n in sorted(by_y.items())},
            "byEdgeDistance": {str(d): n for d, n in sorted(by_edge.items())}}


def _differing(first: View, second: View, tally: Counter) -> list[dict]:
    """The unit's sections whose light differs, tallying every section compared."""
    sections_a = {section.pos: section for section in first.sections}
    sections_b = {section.pos: section for section in second.sections}
    lit = [first.lit, second.lit]
    found = []
    for pos in sorted(sections_a.keys() | sections_b.keys(), key=lambda p: p[1]):
        a, b = sections_a.get(pos), sections_b.get(pos)
        if not (_kept(a) or _kept(b)):
            continue
        tally["sections"] += 1
        same_blocks = _states(a) == _states(b)
        tally["blocksDiffer"] += not same_blocks
        block = _part(_array(a, "block_light"), _array(b, "block_light"))
        sky = _part(_array(a, "sky_light"), _array(b, "sky_light"))
        if block or sky:
            tally["differingSameBlocks"] += same_blocks
            tally["differingUnlit"] += False in lit
            found.append({"pos": list(pos), "blockLight": block, "skyLight": sky, "sameBlocks": same_blocks,
                          "lit": lit})
    return found


def _kept(section: Section | None) -> bool:
    """Whether the save keeps anything of the section, rather than leaving it out as air."""
    return section is not None and (section.states is not None or section.block_light is not None
                                    or section.sky_light is not None)


def _states(section: Section | None) -> list[str]:
    return section.states if section and section.states is not None else LEFT_OUT


def _array(section: Section | None, name: str) -> bytes | None:
    return getattr(section, name) if section else None


def _part(a: bytes | None, b: bytes | None) -> dict | None:
    if a == b:
        return None
    if a is None or b is None:
        return {"present": [a is not None, b is not None]}
    return light_changes(a, b)


class _Edges:
    """How far inside the box around everything a world holds a position lies, in units."""

    def __init__(self, held: set[tuple[int, ...]]):
        axes = list(zip(*held))
        self.low = [min(values) for values in axes]
        self.high = [max(values) for values in axes]

    def distance(self, pos: tuple[int, ...]) -> int:
        return min(min(value - low, high - value) for value, low, high in zip(pos, self.low, self.high))
