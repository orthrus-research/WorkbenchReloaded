"""How 16 x 16 faces read when they tile across a world, for assets.texture-metrics.

Lightness is OKLab L (0 to 1); transparent pixels are left out. Per face:

    opaque            the share of opaque pixels
    mean, spread      the mean and standard deviation of lightness
    edge_density      the share of neighbouring pixel pairs, across the tile's seams too, whose lightness differs by
                      more than `step_gap`: how busy the face reads
    accent_px         pixels brighter than the mean by more than `accent_above_mean`
    quadrant          the largest difference between a quadrant's mean and the face's: how unbalanced it is
    opening_quadrant  the same for transparency (leaves): openings bunched in one quadrant
    diagonal_chain    the longest diagonal run of the darkest pixels, tiled 3 x 3: a line that reads across blocks
    column_band       how far the rows of the face differ at 4 x 4 (after two mipmaps): stripes up a column of blocks

With an alternate face (``<stem><alternate_suffix>.png`` beside ``<stem>.png``):

    alternate_mean    the difference of their mean lightness
    field             the lag-one-block autocorrelation of lightness over a 64 x 64 field of the two faces placed
                      by a seeded hash, at full size and after two mipmaps (``mip2``): how visibly the pair repeats

Limits (TOML or JSON) judge the faces of a kind:

    step_gap = 0.135
    accent_above_mean = 0.15
    [kinds.stone]
    spread = [0.04, 0.12]          # a range; the other limits are maxima
    edge_density = 0.35
    quadrant = 0.05
    alternate_mean = 0.03
    field_mip2 = 0.25              # with field_rise: the most mip2 may exceed full size
    [assign]                       # the kind of each face, by glob over its path in the folder
    "stone_*.png" = "stone"
"""

from __future__ import annotations

import fnmatch
import hashlib
from pathlib import Path

RULES_MAX = ("edge_density", "accent_px", "quadrant", "diagonal_chain", "column_band", "opening_quadrant")
KIND_KEYS = {"spread", "alternate_mean", "field_mip2", "field_rise", *RULES_MAX}
TOP_KEYS = {"step_gap", "accent_above_mean", "kinds", "assign"}


def numpy_and_pillow():
    import numpy
    from PIL import Image
    return numpy, Image


def check_limits(limits: dict) -> dict:
    unknown = set(limits) - TOP_KEYS
    if unknown:
        raise ValueError(f"unknown keys {sorted(unknown)} (known: {', '.join(sorted(TOP_KEYS))})")
    kinds = limits.get("kinds", {})
    if not isinstance(kinds, dict):
        raise ValueError("kinds must be a table of kinds")
    for kind, spec in kinds.items():
        if not isinstance(spec, dict) or set(spec) - KIND_KEYS:
            raise ValueError(f"kinds.{kind}: limits are {', '.join(sorted(KIND_KEYS))}")
        spread = spec.get("spread")
        if spread is not None and (not isinstance(spread, list) or len(spread) != 2):
            raise ValueError(f"kinds.{kind}.spread must be [low, high]")
    assign = limits.get("assign", {})
    if not isinstance(assign, dict) or not all(isinstance(v, str) for v in assign.values()):
        raise ValueError("assign must map glob patterns to kinds")
    missing = sorted(set(assign.values()) - set(kinds))
    if missing:
        raise ValueError(f"assign names kinds with no limits: {', '.join(missing)}")
    return limits


class Faces:
    def __init__(self):
        self.np, self.image = numpy_and_pillow()

    def load(self, path: Path):
        np = self.np
        with self.image.open(path) as opened:
            picture = opened.convert("RGBA")
        width, height = picture.size
        if width != 16 or height % 16:
            raise ValueError(f"{width} x {height} is not a 16 x 16 face or a strip of them")
        return np.array(picture)[:16, :16]

    def oklab(self, rgb):
        np = self.np
        c = rgb.astype(np.float64) / 255.0
        c = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
        lms = np.cbrt(c @ np.array([[0.4122214708, 0.5363325363, 0.0514459929],
                                    [0.2119034982, 0.6806995451, 0.1073969566],
                                    [0.0883024619, 0.2817188376, 0.6299787005]]).T)
        return lms @ np.array([[0.2104542553, 0.7936177850, -0.0040720468],
                               [1.9779984951, -2.4285922050, 0.4505937099],
                               [0.0259040371, 0.7827717662, -0.8086757660]]).T

    def lightness(self, face, fill: bool = False):
        """OKLab L per pixel; with fill, a transparent pixel takes the face's darkest lightness (what an opening
        shows behind it)."""
        np = self.np
        light = self.oklab(face[..., :3])[..., 0]
        if fill:
            opaque = face[..., 3] > 0
            if opaque.any() and not opaque.all():
                light = np.where(opaque, light, light[opaque].min())
        return light

    @staticmethod
    def mip(x):
        return (x[0::2, 0::2] + x[1::2, 0::2] + x[0::2, 1::2] + x[1::2, 1::2]) / 4

    def autocorrelation(self, field, lag: int) -> float:
        np = self.np
        f = field - field.mean()
        denominator = (f * f).sum() or 1.0
        return float(((f * np.roll(f, lag, 1)).sum() + (f * np.roll(f, lag, 0)).sum()) / (2 * denominator))

    def field(self, tiles: list, n: int = 64, seed: int = 1) -> dict:
        np = self.np
        plane = np.zeros((n * 16, n * 16))
        for y in range(n):
            for x in range(n):
                pick = int.from_bytes(hashlib.sha256(repr((seed, x, y)).encode()).digest()[:4], "little")
                plane[y * 16:(y + 1) * 16, x * 16:(x + 1) * 16] = tiles[pick % len(tiles)]
        return {"full": round(self.autocorrelation(plane, 16), 4),
                "mip2": round(self.autocorrelation(self.mip(self.mip(plane)), 4), 4)}

    def diagonal_chain(self, face) -> int:
        np = self.np
        light = self.lightness(face, fill=True)
        if light.max() - light.min() < 0.15:
            return 0
        dark = np.tile(light <= light.min() + 0.01, (3, 3))
        best = 0
        for step in (1, -1):
            run = np.zeros(dark.shape, int)
            columns = range(dark.shape[1]) if step == 1 else range(dark.shape[1] - 1, -1, -1)
            for y in range(dark.shape[0]):
                for x in columns:
                    if dark[y, x]:
                        previous = x - step
                        run[y, x] = 1 + (run[y - 1, previous] if y and 0 <= previous < dark.shape[1] else 0)
                        best = max(best, int(run[y, x]))
        return best

    def measure(self, face, step_gap: float = 0.135, accent_above_mean: float = 0.15) -> dict:
        np = self.np
        opaque = face[..., 3] > 0
        if not opaque.any():
            raise ValueError("the face is fully transparent")
        light = self.lightness(face)
        lit = light[opaque]
        out = {"opaque": round(float(opaque.mean()), 4), "mean": round(float(lit.mean()), 4),
               "spread": round(float(lit.std()), 4)}
        pairs = edges = 0
        for axis in (0, 1):
            neighbour, neighbour_opaque = np.roll(light, -1, axis), np.roll(opaque, -1, axis)
            both = opaque & neighbour_opaque
            pairs += int(both.sum())
            edges += int((both & (np.abs(light - neighbour) > step_gap)).sum())
        out["edge_density"] = round(edges / max(pairs, 1), 4)
        out["accent_px"] = int((opaque & (light > lit.mean() + accent_above_mean)).sum())
        quadrants = [(slice(0, 8), slice(0, 8)), (slice(0, 8), slice(8, 16)), (slice(8, 16), slice(0, 8)),
                     (slice(8, 16), slice(8, 16))]
        deviations = [abs(float(light[q][opaque[q]].mean()) - float(lit.mean())) for q in quadrants if opaque[q].any()]
        out["quadrant"] = round(max(deviations), 4) if deviations else 0.0
        opening = 1 - float(opaque.mean())
        out["opening_quadrant"] = round(max(abs((1 - float(opaque[q].mean())) - opening) for q in quadrants), 4)
        out["diagonal_chain"] = self.diagonal_chain(face)
        small = self.mip(self.mip(self.lightness(face, fill=True)))
        out["column_band"] = round(float(small.mean(1).max() - small.mean(1).min()), 4)
        return out

    def pair(self, main, alternate) -> dict:
        light = self.lightness(main)[main[..., 3] > 0].mean()
        other = self.lightness(alternate)[alternate[..., 3] > 0].mean()
        return {"alternate_mean": round(abs(float(light) - float(other)), 4),
                "field": self.field([self.lightness(main, fill=True), self.lightness(alternate, fill=True)])}


def judge(measured: dict, spec: dict) -> dict:
    """{rule: {passed, value, limit}} for one face (and its pair) against one kind's limits."""
    rules = {}
    if "spread" in spec:
        low, high = spec["spread"]
        rules["spread"] = {"passed": low <= measured["spread"] <= high, "value": measured["spread"],
                           "limit": [low, high]}
    for name in RULES_MAX:
        if name in spec:
            rules[name] = {"passed": measured[name] <= spec[name], "value": measured[name], "limit": spec[name]}
    if "alternate_mean" in spec and "alternate_mean" in measured:
        rules["alternate_mean"] = {"passed": measured["alternate_mean"] <= spec["alternate_mean"],
                                   "value": measured["alternate_mean"], "limit": spec["alternate_mean"]}
    if "field_mip2" in spec and "field" in measured:
        field = measured["field"]
        passed = field["mip2"] <= spec["field_mip2"] and field["mip2"] - field["full"] <= spec.get("field_rise", 1.0)
        rules["field"] = {"passed": passed, "value": field, "limit": {"mip2": spec["field_mip2"],
                                                                      "rise": spec.get("field_rise")}}
    return rules


def kind_of(relative: str, assign: dict, default: str) -> str:
    name = relative.rsplit("/", 1)[-1]
    for pattern, kind in assign.items():
        if fnmatch.fnmatchcase(relative, pattern) or fnmatch.fnmatchcase(name, pattern):
            return kind
    return default
