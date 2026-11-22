"""Block galleries: blocks laid out in rows on a platform for review, the commands that build them, and the views
that capture each row, as a tour.

Blocks are given as id, id state or id[state] (minecraft:stone, minecraft:stone 1, minecraft:log axis=y,variant=oak,
minecraft:oak_log[axis=y]), each optionally after a label and = (granite=minecraft:stone 1); or in a catalog, a JSON
or TOML list of such strings or tables {id, state, label}, or a table with blocks. A block without a label is named
by its id and state values (log-y-oak).

The cameras look the way the gallery faces. Row 0 stands on the origin, centred on it (to the nearest block), and
the platform lies under the gallery, one block below. The rows after it step away from the cameras by the row
spacing; every row is centred on the origin's line whatever its count, and reads left to right in the order
given. A row's camera stands the capture distance in
front of it (measured flat), its eye high enough that the pitch looks at the middle of the row. The tour arrives
first (a view not captured, above and behind the gallery, so the area is loaded before anything is built), builds
the gallery on its way to row 0 (clear, platform, blocks), captures each row, and last an overview of them all.

The spacing check places each row in its camera's frame (the field of view is the vertical one, as the game sets
it): the row must fit across with a margin, a block must cover enough pixels to show its texture, the gaps between
blocks must show, the row before must stand behind the camera, and the camera's eye must be above the platform.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from .tour import EYE, Finding, forward, look, tidy

FACINGS = {"south": (0, 1), "west": (-1, 0), "north": (0, -1), "east": (1, 0)}
SYNTAXES = ("legacy", "flattened")
BLOCK = re.compile(r"^(?:(?P<label>[A-Za-z0-9_-]+)=)?(?P<id>[a-z0-9_.-]+(?::[a-z0-9_./-]+)?)"
                   r"(?:\[(?P<bracket>[^\]]*)\]|\s+(?P<state>\S+))?$")
PAIRS = re.compile(r"^[a-z0-9_]+=[a-z0-9_.-]+(?:,[a-z0-9_]+=[a-z0-9_.-]+)*$")
MARGIN = 0.05          # of the frame's width, kept clear either side of a row
PLATFORM_MARGIN = 2    # blocks of platform beyond the blocks and the cameras
OVERVIEW_PITCH = 40.0


class GalleryError(ValueError):
    """Blocks or a layout that cannot be built as given."""


@dataclass(frozen=True)
class Block:
    id: str
    state: str
    label: str

    def spec(self, syntax: str) -> str:
        """The block as a setblock or fill command names it."""
        if not self.state:
            return self.id
        if syntax == "flattened":
            if self.state.isdigit():
                raise GalleryError(f"{self.id} {self.state}: the flattened syntax has no data values; name the state")
            return f"{self.id}[{self.state}]"
        return f"{self.id} {self.state}"


def _label(block_id: str, state: str) -> str:
    values = [pair.split("=", 1)[1] for pair in state.split(",")] if "=" in state else ([state] if state else [])
    text = "-".join([block_id.split(":")[-1], *values]).lower()
    return re.sub(r"[^a-z0-9_-]+", "-", text).strip("-") or "block"


def parse_block(text: str, label: str | None = None) -> Block:
    match = BLOCK.match(text.strip())
    if not match:
        raise GalleryError(f"{text!r} is not a block: id, id state or id[state] (minecraft:log axis=y,variant=oak)")
    state = match.group("bracket") if match.group("bracket") is not None else (match.group("state") or "")
    if state and not (state.isdigit() or PAIRS.match(state)):
        raise GalleryError(f"{text!r}: the state is a data value or key=value pairs, not {state!r}")
    return Block(match.group("id"), state, label or match.group("label") or _label(match.group("id"), state))


def catalog(document) -> list[Block]:
    """Blocks from a catalog: a list (or a table with blocks) of strings or {id, state, label} tables."""
    items = document.get("blocks") if isinstance(document, dict) else document
    if not isinstance(items, list):
        raise GalleryError("a catalog is a list of blocks, or a table with blocks")
    out = []
    for index, item in enumerate(items):
        if isinstance(item, str):
            out.append(parse_block(item))
        elif isinstance(item, dict) and isinstance(item.get("id"), str):
            unknown = set(item) - {"id", "state", "label"}
            if unknown:
                raise GalleryError(f"catalog block {index} has unknown keys {', '.join(sorted(unknown))}")
            state = item.get("state", "")
            text = item["id"] + (f" {state}" if state not in ("", None) else "")
            out.append(parse_block(text, item.get("label")))
        else:
            raise GalleryError(f"catalog block {index} is not a string or a table with an id")
    return out


def unique(blocks: list[Block]) -> tuple[list[Block], list[Finding]]:
    """The blocks with labels made unique (a -2 for the second of a name), and a finding for each block given
    twice."""
    findings, seen, labels, out = [], set(), set(), []
    for block in blocks:
        if (block.id, block.state) in seen:
            findings.append(Finding("blocks", f"{block.id}{' ' + block.state if block.state else ''} is listed more "
                                              "than once"))
        seen.add((block.id, block.state))
        label, n = block.label, 2
        while label in labels:
            label, n = f"{block.label}-{n}", n + 1
        labels.add(label)
        out.append(Block(block.id, block.state, label))
    return out, findings


def fills(low: tuple[int, int, int], high: tuple[int, int, int], spec: str, limit: int) -> list[str]:
    """fill commands for a box, split so that none changes more than limit blocks (0: no limit)."""
    low, high = tuple(map(min, low, high)), tuple(map(max, low, high))
    size = [b - a + 1 for a, b in zip(low, high)]
    volume = size[0] * size[1] * size[2]
    if not limit or volume <= limit or max(size) == 1:
        return [f"fill {low[0]} {low[1]} {low[2]} {high[0]} {high[1]} {high[2]} {spec}"]
    axis = max(range(3), key=lambda i: size[i])
    step = max(1, limit // (volume // size[axis]))
    out = []
    for start in range(low[axis], high[axis] + 1, step):
        a, b = list(low), list(high)
        a[axis], b[axis] = start, min(start + step - 1, high[axis])
        out += fills(tuple(a), tuple(b), spec, limit)
    return out


@dataclass
class Placed:
    block: Block
    row: int
    column: int
    pos: tuple[int, int, int]


@dataclass
class Camera:
    label: str
    pos: tuple[float, float, float]       # feet
    yaw: float
    pitch: float

    @property
    def eye(self) -> tuple[float, float, float]:
        return self.pos[0], self.pos[1] + EYE, self.pos[2]


@dataclass
class Layout:
    origin: tuple[int, int, int]
    facing: str
    spacing: int
    row_spacing: int
    distance: float
    pitch: float
    placed: list[Placed]
    rows: list[list[Placed]]
    cameras: list[Camera]
    overview: Camera
    footprint: tuple[int, int, int, int]  # x1, z1, x2, z2 of the platform
    stats: list[dict] = field(default_factory=list)


def layout(blocks: list[Block], origin, facing: str, columns: int, spacing: int, row_spacing: int,
           distance: float, pitch: float, fov: float, aspect: float, label: str) -> Layout:
    if facing not in FACINGS:
        raise GalleryError(f"facing is one of {', '.join(FACINGS)}")
    if not blocks:
        raise GalleryError("there are no blocks to lay out")
    ox, oy, oz = origin
    fx, fz = FACINGS[facing]
    rx, rz = -fz, fx                       # the camera's right, as it looks along the facing
    yaw = look(fx, 0, fz)[0]
    rows: list[list[Placed]] = []
    for start in range(0, len(blocks), columns):
        k, chunk = len(rows), blocks[start:start + columns]
        shift = (len(chunk) - 1) * spacing // 2
        rows.append([Placed(block, k, c, (ox + rx * (c * spacing - shift) + fx * k * row_spacing, oy,
                                          oz + rz * (c * spacing - shift) + fz * k * row_spacing))
                     for c, block in enumerate(chunk)])
    lift = distance * math.tan(math.radians(pitch))
    cameras = []
    width = max(2, len(str(len(rows) - 1)))
    for k, row in enumerate(rows):
        cx = sum(p.pos[0] + 0.5 for p in row) / len(row)
        cz = sum(p.pos[2] + 0.5 for p in row) / len(row)
        eye = (cx - fx * distance, oy + 0.5 + lift, cz - fz * distance)
        cameras.append(Camera(f"{label}-{k:0{width}d}", (eye[0], eye[1] - EYE, eye[2]), yaw, pitch))
    placed = [p for row in rows for p in row]
    centre = tuple(sum(p.pos[i] + 0.5 for p in placed) / len(placed) for i in range(3))
    across = max((len(row) - 1) * spacing + 1 for row in rows)
    deep = (len(rows) - 1) * row_spacing + 1
    radius = 0.5 * math.hypot(across, deep) + 1
    vertical = math.radians(fov)
    horizontal = 2 * math.atan(math.tan(vertical / 2) * aspect)
    reach = radius / math.sin(min(vertical, horizontal) / 2)
    ahead = forward(yaw, OVERVIEW_PITCH)
    eye = tuple(c - a * reach for c, a in zip(centre, ahead))
    overview = Camera(f"{label}-overview", (eye[0], eye[1] - EYE, eye[2]), yaw, OVERVIEW_PITCH)
    xs = [p.pos[0] for p in placed] + [math.floor(c.pos[0]) for c in cameras]
    zs = [p.pos[2] for p in placed] + [math.floor(c.pos[2]) for c in cameras]
    footprint = (min(xs) - PLATFORM_MARGIN, min(zs) - PLATFORM_MARGIN, max(xs) + PLATFORM_MARGIN,
                 max(zs) + PLATFORM_MARGIN)
    return Layout((ox, oy, oz), facing, spacing, row_spacing, distance, pitch, placed, rows, cameras, overview,
                  footprint)


def spacing_check(plan: Layout, fov: float, width: int, height: int, min_gap_px: float,
                  min_block_px: float) -> list[Finding]:
    """Whether each row reads in its camera's frame (see the module's notes); fills plan.stats."""
    findings = []
    vertical = math.radians(fov)
    horizontal = 2 * math.atan(math.tan(vertical / 2) * width / height)
    reach = plan.distance / math.cos(math.radians(plan.pitch))      # along the view, to the row's middle
    visible = 2 * reach * math.tan(horizontal / 2)
    block_px = width / visible
    gap_px = (plan.spacing - 1) * block_px
    platform_top = plan.origin[1]
    plan.stats = []
    for k, (row, camera) in enumerate(zip(plan.rows, plan.cameras)):
        span = (len(row) - 1) * plan.spacing + 1
        plan.stats.append({"row": k, "blocks": [p.block.label for p in row], "span": span,
                           "camera": {"x": tidy(camera.pos[0]), "y": tidy(camera.pos[1]), "z": tidy(camera.pos[2]),
                                      "yaw": tidy(camera.yaw, 2), "pitch": tidy(camera.pitch, 2)},
                           "reach": round(reach, 3), "visible": round(visible, 3), "blockPx": round(block_px, 1),
                           "gapPx": round(gap_px, 1) if len(row) > 1 else None})
        if span > visible * (1 - 2 * MARGIN):
            findings.append(Finding("spacing", f"row {k}: {len(row)} blocks span {span} blocks; {reach:.1f} blocks "
                                               f"away the view is {visible:.1f} wide, so the row does not fit with "
                                               f"{MARGIN:.0%} either side"))
        if len(row) > 1 and gap_px < min_gap_px:
            findings.append(Finding("spacing", f"row {k}: blocks {plan.spacing} apart leave {gap_px:.0f} px between "
                                               f"them {reach:.1f} blocks away; {min_gap_px:g} px keeps them apart"))
        if block_px < min_block_px:
            findings.append(Finding("spacing", f"row {k}: a block is {block_px:.0f} px across {reach:.1f} blocks "
                                               f"away; {min_block_px:g} px shows its texture"))
        if k and plan.row_spacing - plan.distance < 1:
            findings.append(Finding("spacing", f"row {k}: its camera stands {plan.distance:g} blocks in front of it, "
                                               f"row {k - 1} only {plan.row_spacing}, so that row stands in or "
                                               f"before the camera; set the rows at least "
                                               f"{math.ceil(plan.distance) + 1} apart"))
        if camera.eye[1] < platform_top:
            findings.append(Finding("spacing", f"row {k}: its camera's eye is at y {camera.eye[1]:.2f}, inside the "
                                               f"platform (its top is y {platform_top})"))
    return findings


def commands(plan: Layout, platform: Block | None, clear: int, syntax: str, limit: int) -> list[str]:
    """Clear the gallery's volume, lay the platform, place the blocks."""
    x1, z1, x2, z2 = plan.footprint
    y = plan.origin[1]
    out = []
    if clear:
        out += fills((x1, y, z1), (x2, y + clear - 1, z2), "minecraft:air", limit)
    if platform is not None:
        out += fills((x1, y - 1, z1), (x2, y - 1, z2), platform.spec(syntax), limit)
    for p in plan.placed:
        out.append(f"setblock {p.pos[0]} {p.pos[1]} {p.pos[2]} {p.block.spec(syntax)}")
    return out
