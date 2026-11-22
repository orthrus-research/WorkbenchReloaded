"""GPU samples window by window, device by device: min, median and max of each metric, time in each performance
state and clock event reason, throttling episodes, and the expectations a window breaks.

Each sample stands for the time until its device's next sample, but for at most twice the device's sampling period,
so a gap in the samples stays a gap. Times in a state add those spans, clipped to the window; the min, median and
max are over the samples taken inside it.
"""

from __future__ import annotations

import bisect
import math
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass

from . import gpu
from .windows import Window, iso

SEMANTICS = {
    "scope": "Whole-device telemetry: every process on the device counts, the desktop and other programs too. It "
             "is not a process's memory, frame time or the cost of one shader.",
    "time": "A sample stands for the time until the device's next sample, for at most twice the sampling period; "
            "times in a state add those spans, clipped to the window. Shares are of the time the samples cover.",
    "counters": "Utilisation is the share of the tool's own sampling period the device was busy, and power may be "
                "a one-second average (newer NVIDIA devices): neither is frame timing.",
    "throttle": "Clock event reasons are sampled states: a reason that came and went between two samples is not "
                "seen, and an unknown reason is unknown, not inactive. idle is no limit; clock settings and sync "
                "boost are settings; the other reasons hold the clocks below what the work asks for and make "
                "throttling episodes.",
}
EXPECTATIONS = ("pstate", "throttle", *(f"{bound}_{m.stem}" for m in gpu.METRICS for bound in ("min", "max")))


@dataclass
class Series:
    """One device's samples in time order."""
    device: int
    name: str | None
    times: list[int]
    rows: list[dict]
    spans: list[int]  # the nanoseconds each sample stands for
    period: int       # the median time between samples
    cap: int          # the longest a sample stands for


def series(pairs, period: int | None = None) -> dict[int, Series]:
    """Samples ((epoch nanoseconds, sample) pairs) by device. A device with one sample stands for ``period`` (ns;
    one second when not given)."""
    grouped = defaultdict(list)
    for nanos, sample in pairs:
        grouped[sample["device"]].append((nanos, sample))
    out = {}
    for device, items in sorted(grouped.items()):
        items.sort(key=lambda item: item[0])
        times = [t for t, _ in items]
        steps = [b - a for a, b in zip(times, times[1:]) if b > a]
        typical = int(statistics.median(steps)) if steps else int(period or 1_000_000_000)
        cap = 2 * typical
        spans = [min(b - a, cap) for a, b in zip(times, times[1:])] + [typical]
        rows = [s for _, s in items]
        name = next((r["name"] for r in reversed(rows) if r.get("name")), None)
        out[device] = Series(device, name, times, rows, spans, typical, cap)
    return out


def bounds(devices: dict[int, Series]) -> tuple[int, int]:
    """The time the samples cover: from the first to the end of the last one's span."""
    return (min(s.times[0] for s in devices.values()),
            max(s.times[-1] + max(s.spans[-1], 1) for s in devices.values()))


def expectations(spec: dict) -> dict:
    """An expectation table checked: pstate (a state or a list), throttle (false: none may hold the clocks down),
    min_<metric> and max_<metric>. ValueError names what is wrong."""
    out = {}
    for key, want in (spec or {}).items():
        if key not in EXPECTATIONS:
            raise ValueError(f"unknown expectation {key!r}: pstate, throttle, or min_/max_ of "
                             + ", ".join(m.stem for m in gpu.METRICS))
        if key == "pstate":
            states = [want] if isinstance(want, str) else want
            if not isinstance(states, list) or not states or \
                    not all(isinstance(s, str) and s.upper() in gpu.PSTATES for s in states):
                raise ValueError(f"pstate is a performance state (P0 to P15) or a list of them, not {want!r}")
            out[key] = sorted({s.upper() for s in states}, key=gpu.PSTATES.index)
        elif key == "throttle":
            if not isinstance(want, bool):
                raise ValueError(f"throttle is true (allowed) or false (not allowed), not {want!r}")
            if not want:
                out[key] = False
        else:
            if isinstance(want, bool) or not isinstance(want, (int, float)) or not math.isfinite(want):
                raise ValueError(f"{key} is a number, not {want!r}")
            out[key] = want
    for metric in gpu.METRICS:
        low, high = out.get(f"min_{metric.stem}"), out.get(f"max_{metric.stem}")
        if low is not None and high is not None and low > high:
            raise ValueError(f"min_{metric.stem} ({low}) is above max_{metric.stem} ({high})")
    return out


def _round(value: float, places: int = 3):
    value = round(float(value), places)
    return int(value) if value.is_integer() else value


def _throttling(row: dict) -> list[str] | None:
    reasons = row.get("throttleReasons")
    return None if reasons is None else [r for r in reasons if gpu.kind(r) == "throttle"]


def _breaks(key: str, want, row: dict):
    """Whether a sample breaks an expectation (None: it does not say), and the value that does."""
    if key == "pstate":
        state = row.get("pstate")
        return (None, None) if state is None else (state not in want, state)
    if key == "throttle":
        held = _throttling(row)
        return (None, None) if held is None else (bool(held), held)
    bound, metric = key[:3], gpu.BY_STEM[key[4:]]
    value = row.get(metric.key)
    if value is None:
        return None, None
    return (value < want if bound == "min" else value > want), value


def _violation(key: str, want, spans: list[tuple[dict, int]], tolerance: float) -> dict | None:
    known, bad, worst = 0, 0, []
    count = 0
    for row, part in spans:
        broken, value = _breaks(key, want, row)
        if broken is None:
            continue
        known += part
        if broken:
            bad += part
            count += 1
            worst.append(value)
    entry = {"expect": key, "want": want}
    if not known:
        return {**entry, "unknown": True, "finding": True}
    if not count:
        return None
    if key == "pstate":
        seen = Counter(worst).most_common()
        value = [state for state, _ in seen]
    elif key == "throttle":
        value = sorted({r for held in worst for r in held})
    else:
        value = min(worst) if key.startswith("min_") else max(worst)
    share = bad / known
    return {**entry, "seconds": _round(bad / 1e9), "share": _round(share, 4), "samples": count, "worst": value,
            "finding": share > tolerance}


def device_window(s: Series, window: Window, expect: dict, tolerance: float = 0.0, top: int = 10) -> dict:
    """One device in one window."""
    lo = bisect.bisect_left(s.times, window.start - s.cap)
    hi = bisect.bisect_left(s.times, window.end)
    covered, unknown, throttled = 0, 0, 0
    states, held = Counter(), Counter()
    inside, spans, episodes = [], [], []
    reached, gap, current = window.start, 0, None
    for i in range(lo, hi):
        t, row = s.times[i], s.rows[i]
        a, b = max(t, window.start), min(t + s.spans[i], window.end)
        if t >= window.start:
            inside.append(row)
        if b <= a:
            continue
        part = b - a
        gap, reached = max(gap, a - reached), max(reached, b)
        covered += part
        spans.append((row, part))
        states[row.get("pstate") or "unknown"] += part
        if row.get("throttleReasons") is None:
            unknown += part
        for reason in row.get("throttleReasons") or []:
            held[reason] += part
        limits = _throttling(row)
        if limits:
            throttled += part
            if current and a <= current["to"]:
                current.update(to=b, ns=current["ns"] + part, samples=current["samples"] + 1)
                current["reasons"].update(limits)
            else:
                current = {"from": a, "to": b, "ns": part, "samples": 1, "reasons": set(limits), "clock": None}
                episodes.append(current)
            clock = row.get("graphicsClockMhz", row.get("smClockMhz"))
            if clock is not None:
                current["clock"] = clock if current["clock"] is None else min(current["clock"], clock)
        else:
            current = None
    gap = max(gap, window.end - reached)

    def share(nanos: int) -> dict:
        return {"seconds": _round(nanos / 1e9), "share": _round(nanos / covered, 4) if covered else None}

    metrics = {}
    for metric in gpu.METRICS:
        values = [row[metric.key] for row in inside if row.get(metric.key) is not None]
        if values:
            metrics[metric.key] = {"min": _round(min(values)), "median": _round(statistics.median(values)),
                                   "max": _round(max(values)), "samples": len(values)}
    listed = sorted(episodes, key=lambda e: (-e["ns"], e["from"]))[:top]
    violations = [v for key, want in expect.items() if (v := _violation(key, want, spans, tolerance))]
    return {"device": s.device, "name": s.name, "samples": len(inside), "periodMs": _round(s.period / 1e6),
            "coveredSeconds": _round(covered / 1e9), "coverage": _round(covered / max(1, window.end - window.start), 4),
            "longestGapSeconds": _round(gap / 1e9), "metrics": metrics,
            "pstates": {state: share(ns) for state, ns in states.most_common()},
            "throttle": {"throttledSeconds": _round(throttled / 1e9), "unknownSeconds": _round(unknown / 1e9),
                         "reasons": {reason: share(ns) for reason, ns in held.most_common()},
                         "episodeCount": len(episodes),
                         "episodes": [{"start": iso(e["from"]), "end": iso(e["to"]), "seconds": _round(e["ns"] / 1e9),
                                       "samples": e["samples"], "reasons": sorted(e["reasons"]),
                                       "minClockMhz": e["clock"]} for e in listed]},
            "expectations": violations}


def analyse(devices: dict[int, Series], windows: list[Window], expect: dict | None = None, tolerance: float = 0.0,
            top: int = 10, origin: int | None = None) -> list[dict]:
    """Every window, with every device in it."""
    origin = bounds(devices)[0] if origin is None else origin
    expect = expectations(expect or {})
    return [{**w.describe(origin), "devices": [device_window(s, w, expect, tolerance, top)
                                               for s in devices.values()]} for w in windows]


# -- words ----------------------------------------------------------------------------------------------
def headline(entry: dict) -> str:
    """One device in one window, in a line: clocks, performance state, power, temperature, memory, throttling."""
    m, parts = entry["metrics"], []
    clock = m.get("graphicsClockMhz") or m.get("smClockMhz")
    if clock:
        parts.append(f"graphics {clock['median']:,.0f} MHz median ({clock['min']:,.0f}-{clock['max']:,.0f})")
    if m.get("memoryClockMhz"):
        parts.append(f"memory {m['memoryClockMhz']['median']:,.0f} MHz")
    states = [(state, v["share"]) for state, v in entry["pstates"].items() if v["share"]]
    if states and states[0][0] != "unknown":
        parts.append(", ".join(f"{state} {share:.0%}" for state, share in states[:2]))
    if m.get("powerW"):
        limit = m.get("powerLimitW")
        parts.append(f"{m['powerW']['median']:,.0f} W median"
                     + (f" of a {limit['median']:,.0f} W limit" if limit else ""))
    if m.get("temperatureC"):
        parts.append(f"{m['temperatureC']['max']:.0f} °C max")
    if m.get("memoryUsedMiB"):
        parts.append(f"{m['memoryUsedMiB']['max'] / 1024:,.1f} GiB used")
    throttle = entry["throttle"]
    if throttle["episodeCount"]:
        reasons = sorted({r for e in throttle["episodes"] for r in e["reasons"]})
        parts.append(f"throttled {throttle['throttledSeconds']:,.1f} s in {throttle['episodeCount']} "
                     f"episode{'s' if throttle['episodeCount'] != 1 else ''} ({', '.join(reasons)})")
    elif throttle["unknownSeconds"] and throttle["unknownSeconds"] >= entry["coveredSeconds"]:
        parts.append("throttle reasons unknown")
    else:
        parts.append("not throttled")
    return ", ".join(parts)


def describe(violation: dict, tolerance: float = 0.0) -> str:
    """An expectation a window broke, in words."""
    key, want = violation["expect"], violation["want"]
    if violation.get("unknown"):
        what = {"pstate": "performance state", "throttle": "throttle reasons"}.get(key) or gpu.BY_STEM[key[4:]].what
        shown = " or ".join(want) if isinstance(want, list) else str(want).lower() if isinstance(want, bool) else want
        return f"the samples carry no {what} to check {key} = {shown}"
    seconds, share = violation["seconds"], violation["share"]
    extent = f"for {seconds:,.1f} s ({share:.0%} of the time"
    if key == "pstate":
        text = f"in {'/'.join(violation['worst'])} {extent}), expected {' or '.join(want)}"
    elif key == "throttle":
        text = f"throttled {extent}: {', '.join(violation['worst'])})"
    else:
        metric = gpu.BY_STEM[key[4:]]
        side, extreme = ("below", "lowest") if key.startswith("min_") else ("above", "highest")
        text = f"{metric.what} {side} {want:g} {metric.unit} {extent}, {extreme} {violation['worst']:g} {metric.unit})"
    return text + (f", above the {tolerance:.0%} tolerance" if tolerance else "")
