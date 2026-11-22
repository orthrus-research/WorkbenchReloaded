"""content.placement: placement statistics of points (x, z, kind) on a rectangle, per kind and for all points.

- density: points per chunk (256 square blocks) and per hectare (10,000);
- nearest-neighbour distances, each point to the nearest other point of its kind: mean, median, the spread
  (standard deviation, coefficient of variation, 10th and 90th percentiles) and coincident points;
- the Clark-Evans ratio R, the mean nearest-neighbour distance over its expectation for a random pattern of the
  same density (below 1 clumped, about 1 random, above 1 regular), with Donnelly's correction for a
  rectangle's edges by default, its z-score and two-sided p;
- the index of dispersion of counts in the whole quadrats that fit the rectangle (sample variance over mean:
  above 1 patchy, about 1 random, below 1 regular), with the chi-square test on n - 1 degrees of freedom.
"""

from __future__ import annotations

import csv
import math
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

MEASURES = ("points", "per_chunk", "per_hectare", "nn_mean", "nn_median", "clark_evans", "dispersion")
EVERY = "*"  # in expect: every kind
COMBINED = "(all)"  # the row of all points together
SIGNIFICANCE = 0.05


@dataclass
class Points:
    by_kind: dict[str, list[tuple[float, float]]] = field(default_factory=lambda: defaultdict(list))
    unreadable: list[str] = field(default_factory=list)
    integers: bool = True


@dataclass
class Found:
    report: dict
    findings: list[str]
    summary: str


def ranges(expect) -> dict[str, dict[str, tuple[float | None, float | None]]]:
    """expect = { kind (or *) = { measure = [low, high] } }, a bound null or left out for none."""
    if not isinstance(expect, dict):
        raise ValueError("expect: a table of kinds, each a table of measure = [low, high]")
    out = {}
    for kind, measures in expect.items():
        if not isinstance(measures, dict):
            raise ValueError(f"expect.{kind}: a table of measure = [low, high]")
        out[kind] = {}
        for measure, bounds in measures.items():
            if measure not in MEASURES:
                raise ValueError(f"expect.{kind}.{measure}: unknown measure (known: {', '.join(MEASURES)})")
            if not isinstance(bounds, list) or len(bounds) != 2 or not all(
                    b is None or (isinstance(b, (int, float)) and not isinstance(b, bool)) for b in bounds):
                raise ValueError(f"expect.{kind}.{measure}: [low, high], numbers or null")
            out[kind][measure] = (bounds[0], bounds[1])
    return out


def read(path: Path, *, x: str, z: str, kind: str, kinds: list[str]) -> Points:
    points = Points()
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as handle:
        sample = handle.read(4096)
        handle.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        rows = csv.DictReader(handle, dialect=dialect)
        header = [h.strip() for h in rows.fieldnames or []]
        rows.fieldnames = header
        for column in (x, z):
            if column not in header:
                raise ValueError(f"points: no column {column} (columns: {', '.join(header) or 'none'})")
        for number, row in enumerate(rows, 2):
            try:
                px, pz = float(row[x]), float(row[z])
            except (TypeError, ValueError):
                points.unreadable.append(f"line {number}: {x}={row.get(x)!r}, {z}={row.get(z)!r}")
                continue
            if not (math.isfinite(px) and math.isfinite(pz)):
                points.unreadable.append(f"line {number}: coordinates are not finite")
                continue
            name = (row.get(kind) or "").strip() if kind in header else "all"
            name = name or "(none)"
            if kinds and name not in kinds:
                continue
            points.integers &= px.is_integer() and pz.is_integer()
            points.by_kind[name].append((px, pz))
    return points


def nearest(points: list[tuple[float, float]]) -> list[float]:
    """Each point's distance to its nearest other point, by a grid searched ring by ring until no unsearched
    cell can hold a nearer point."""
    n = len(points)
    if n < 2:
        return []
    xs, zs = [p[0] for p in points], [p[1] for p in points]
    x0, z0 = min(xs), min(zs)
    width, height = max(xs) - x0, max(zs) - z0
    span = max(width, height)
    if span == 0:
        return [0.0] * n
    cell = max(math.sqrt(width * height / n) if width and height else span / n, span / 4096)
    grid: dict[tuple[int, int], list[int]] = defaultdict(list)
    for index, (px, pz) in enumerate(points):
        grid[(int((px - x0) // cell), int((pz - z0) // cell))].append(index)
    limit = int(span // cell) + 2
    out = []
    for index, (px, pz) in enumerate(points):
        ci, cj = int((px - x0) // cell), int((pz - z0) // cell)
        best = math.inf
        for ring in range(limit + 1):
            for di in range(-ring, ring + 1):
                for dj in (range(-ring, ring + 1) if abs(di) == ring else (-ring, ring)):
                    for other in grid.get((ci + di, cj + dj), ()):
                        if other != index:
                            ox, oz = points[other]
                            distance = math.hypot(ox - px, oz - pz)
                            if distance < best:
                                best = distance
            if best <= ring * cell:
                break
        out.append(best)
    return out


def _upper_gamma(a: float, x: float) -> float:
    """The regularized upper incomplete gamma Q(a, x) (series below a + 1, continued fraction above)."""
    if x <= 0:
        return 1.0
    log_front = -x + a * math.log(x) - math.lgamma(a)
    if x < a + 1:
        term = total = 1.0 / a
        n = a
        for _ in range(10000):
            n += 1
            term *= x / n
            total += term
            if abs(term) < abs(total) * 1e-15:
                break
        return max(0.0, 1.0 - total * math.exp(log_front))
    b, c, d = x + 1 - a, 1 / 1e-300, 1 / (x + 1 - a)
    h = d
    for i in range(1, 10000):
        an = -i * (i - a)
        b += 2
        d = an * d + b
        d = 1e-300 if abs(d) < 1e-300 else d
        c = b + an / c
        c = 1e-300 if abs(c) < 1e-300 else c
        d = 1 / d
        delta = d * c
        h *= delta
        if abs(delta - 1) < 1e-15:
            break
    return min(1.0, math.exp(log_front) * h)


def _percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    k = (len(ordered) - 1) * q
    low, high = math.floor(k), math.ceil(k)
    return ordered[low] + (ordered[high] - ordered[low]) * (k - low)


def measure(points: list[tuple[float, float]], box: tuple[float, float, float, float], quadrat: float, edge: str,
            min_points: int) -> dict:
    x1, z1, x2, z2 = box
    width, height = x2 - x1, z2 - z1
    area = width * height
    n = len(points)
    row: dict = {"points": n, "per_chunk": round(n / area * 256, 6) if area else None,
                 "per_hectare": round(n / area * 10000, 4) if area else None}
    distances = nearest(points)
    if distances:
        mean = statistics.fmean(distances)
        spread = statistics.pstdev(distances)
        row.update(nn_mean=round(mean, 4), nn_median=round(statistics.median(distances), 4),
                   nn_sd=round(spread, 4), nn_cv=round(spread / mean, 4) if mean else None,
                   nn_p10=round(_percentile(distances, 0.1), 4), nn_p90=round(_percentile(distances, 0.9), 4),
                   nn_min=round(min(distances), 4), coincident=sum(1 for d in distances if d == 0))
    if n >= max(min_points, 2) and area > 0 and distances:
        perimeter = 2 * (width + height)
        if edge == "donnelly":
            expected = 0.5 * math.sqrt(area / n) + (0.0514 + 0.041 / math.sqrt(n)) * perimeter / n
            error = math.sqrt(0.0703 * area / n ** 2 + 0.037 * perimeter * math.sqrt(area / n ** 5))
        else:
            expected = 0.5 / math.sqrt(n / area)
            error = 0.26136 / math.sqrt(n * n / area)
        observed = statistics.fmean(distances)
        ratio = observed / expected
        score = (observed - expected) / error
        row.update(clark_evans=round(ratio, 4), clark_evans_expected=round(expected, 4), clark_evans_z=round(score, 4),
                   clark_evans_p=round(math.erfc(abs(score) / math.sqrt(2)), 6),
                   pattern=("random" if math.erfc(abs(score) / math.sqrt(2)) >= SIGNIFICANCE
                            else "clumped" if ratio < 1 else "regular"))
    across, down = int(width // quadrat), int(height // quadrat)
    if across * down >= 2 and n >= max(min_points, 1):
        counts = [0] * (across * down)
        for px, pz in points:
            i, j = int((px - x1) // quadrat), int((pz - z1) // quadrat)
            if 0 <= i < across and 0 <= j < down:
                counts[j * across + i] += 1
        mean = statistics.fmean(counts)
        if mean > 0:
            index = statistics.variance(counts) / mean
            freedom = len(counts) - 1
            chi = index * freedom
            larger = _upper_gamma(freedom / 2, chi / 2)  # the chance of a larger chi-square if placement is random
            pattern = "patchy" if larger < SIGNIFICANCE / 2 else "regular" if 1 - larger < SIGNIFICANCE / 2 \
                else "random"
            row.update(quadrats=len(counts), quadrat_mean=round(mean, 4), dispersion=round(index, 4),
                       dispersion_chi2=round(chi, 4), dispersion_p=round(min(1.0, 2 * min(larger, 1 - larger)), 6),
                       dispersion_pattern=pattern)
    return row


def analyse(data: Points, *, area: list | None, quadrat: float, edge: str, ranges: dict, min_points: int) -> Found:
    findings: list[str] = []
    every = [p for points in data.by_kind.values() for p in points]
    if not every:
        raise ValueError("points: no point could be read" + (f" ({len(data.unreadable)} unreadable rows, the first: "
                                                             f"{data.unreadable[0]})" if data.unreadable else ""))
    if area:
        x1, x2 = sorted((float(area[0]), float(area[2])))
        z1, z2 = sorted((float(area[1]), float(area[3])))
        assumed = False
    else:
        pad = 1 if data.integers else 0  # a block coordinate stands for the block's square
        x1, z1 = min(p[0] for p in every), min(p[1] for p in every)
        x2, z2 = max(p[0] for p in every) + pad, max(p[1] for p in every) + pad
        assumed = True
    if x2 <= x1 or z2 <= z1:
        raise ValueError("area: the rectangle has no area")
    box = (x1, z1, x2, z2)
    outside = 0
    kinds: dict[str, dict] = {}
    inside_all = []
    for name in sorted(data.by_kind):
        inside = [p for p in data.by_kind[name] if x1 <= p[0] < x2 and z1 <= p[1] < z2]
        outside += len(data.by_kind[name]) - len(inside)
        inside_all += inside
        kinds[name] = measure(inside, box, quadrat, edge, min_points)
    if len(kinds) > 1:
        kinds[COMBINED] = measure(inside_all, box, quadrat, edge, min_points)
    for name, row in kinds.items():
        wanted = {**(ranges.get(EVERY, {}) if name != COMBINED else {}), **ranges.get(name, {})}
        for measure_name, (low, high) in wanted.items():
            value = row.get(measure_name)
            if value is None:
                findings.append(f"{name}: {measure_name} could not be measured ({row['points']} points), but a range "
                                "is expected")
            elif (low is not None and value < low) or (high is not None and value > high):
                side = "below" if low is not None and value < low else "above"
                bound = low if side == "below" else high
                findings.append(f"{name}: {measure_name} {value} is {side} {bound}"
                                + (f" ({row['pattern']}, p {row['clark_evans_p']})" if measure_name == "clark_evans"
                                   else f" ({row['dispersion_pattern']}, p {row['dispersion_p']})"
                                   if measure_name == "dispersion" else ""))
    for missing in sorted(set(ranges) - set(kinds) - {EVERY, COMBINED}):
        findings.append(f"{missing}: no point of this kind, but ranges are expected")
    if data.unreadable:
        findings.append(f"{len(data.unreadable)} row{'s' if len(data.unreadable) != 1 else ''} could not be read, the "
                        f"first: {data.unreadable[0]}")
    report = {"area": {"x1": x1, "z1": z1, "x2": x2, "z2": z2, "width": x2 - x1, "height": z2 - z1,
                       "assumed": assumed},
              "quadrat": quadrat, "edge": edge, "points": len(inside_all), "outside": outside,
              "unreadable": data.unreadable[:100], "kinds": kinds}
    parts = []
    for name, row in kinds.items():
        if name == COMBINED:
            continue
        text = f"{name} {row['points']}"
        if "clark_evans" in row:
            text += f" R {row['clark_evans']:.2f} ({row['pattern']})"
        if "dispersion" in row:
            text += f" ID {row['dispersion']:.2f}"
        parts.append(text)
    summary = (f"{len(inside_all)} points of {len(data.by_kind)} kind{'s' if len(data.by_kind) != 1 else ''} on "
               f"{x2 - x1:g} x {z2 - z1:g} blocks{' (their bounding box)' if assumed else ''}: " + "; ".join(parts[:6])
               + (f"; and {len(parts) - 6} more kinds" if len(parts) > 6 else ""))
    return Found(report, findings, summary)
