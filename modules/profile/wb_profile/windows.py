"""Time windows over a recording: given in a file, split at a fixed length, or the whole recording.

A windows file is a JSON list (or ``{"windows": [...]}``) of objects with a ``name`` and either absolute
``start``/``end`` (ISO-8601 with a zone, or epoch nanoseconds) or ``from``/``to`` offsets from the
recording's start (seconds, or durations like "90s"); a missing ``end``/``to`` runs to the recording's end.
An instant belongs to a window when it falls in [start, end); an event with a duration, when it overlaps it.
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass
from datetime import UTC, datetime

_ISO = re.compile(r"^(?P<base>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})(?:\.(?P<fraction>\d{1,9}))?"
                  r"(?P<zone>Z|[+-]\d{2}:?\d{2})$")
_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ns|us|ms|s|m|h)?\s*$")
_UNITS = {"ns": 1, "us": 1_000, "ms": 1_000_000, "s": 1_000_000_000, "m": 60_000_000_000,
          "h": 3_600_000_000_000, None: 1_000_000_000}


@dataclass(frozen=True)
class Window:
    name: str
    start: int  # epoch nanoseconds, inclusive
    end: int    # exclusive

    @property
    def seconds(self) -> float:
        return (self.end - self.start) / 1e9

    def describe(self, origin: int) -> dict:
        return {"name": self.name, "start": iso(self.start), "end": iso(self.end),
                "offset": round((self.start - origin) / 1e9, 3), "seconds": round(self.seconds, 3)}


def timestamp(value) -> int:
    """Epoch nanoseconds from an ISO-8601 time with a zone (nanoseconds kept) or an integer."""
    if isinstance(value, bool):
        raise ValueError(f"not a time: {value!r}")
    if isinstance(value, int):
        return value
    match = _ISO.match(str(value).strip())
    if not match:
        raise ValueError(f"not an ISO-8601 time with a zone: {value!r}")
    zone = match["zone"]
    zone = "+00:00" if zone == "Z" else zone if ":" in zone else zone[:3] + ":" + zone[3:]
    base = datetime.fromisoformat(match["base"].replace(" ", "T") + zone)
    return int(base.timestamp()) * 1_000_000_000 + int((match["fraction"] or "").ljust(9, "0"))


def offset(value) -> int:
    """Nanoseconds from seconds (a number) or a duration like "250ms", "90s", "2m"."""
    if isinstance(value, bool):
        raise ValueError(f"not a duration: {value!r}")
    if isinstance(value, (int, float)):
        return int(round(value * 1e9))
    match = _DURATION.match(str(value))
    if not match:
        raise ValueError(f"not a duration: {value!r} (use seconds, or 500ms, 90s, 2m)")
    return int(round(float(match.group(1)) * _UNITS[match.group(2)]))


def iso(nanos: int) -> str:
    """Epoch nanoseconds as ISO-8601 UTC, nanoseconds kept."""
    seconds, rest = divmod(nanos, 1_000_000_000)
    stamp = datetime.fromtimestamp(seconds, tz=UTC).strftime("%Y-%m-%dT%H:%M:%S")
    return f"{stamp}.{rest:09d}Z"


def _label(seconds: float) -> str:
    return f"{round(seconds, 3):g}s"


def resolve(start: int, end: int, spec=None, split: float | None = None, skip: float = 0.0) -> list[Window]:
    """The windows of a recording spanning [start, end): from ``spec`` (parsed JSON) when given, else split
    into ``split``-second windows after ``skip`` seconds, else the whole recording after ``skip``."""
    if spec is not None:
        if split:
            raise ValueError("give windows or split, not both")
        return _from_spec(start, end, spec)
    first = start + offset(skip or 0)
    if first >= end:
        raise ValueError(f"skip ({skip} s) leaves nothing of a {(end - start) / 1e9:.1f} s recording")
    if not split:
        return [Window("recording" if not skip else f"{_label((first - start) / 1e9)}-end", first, end)]
    length = offset(split)
    if length <= 0:
        raise ValueError("split must be positive")
    if (end - first) // length > 100_000:
        raise ValueError(f"split {split} s makes more than 100000 windows")
    out, at = [], first
    while at < end:
        stop = min(at + length, end)
        out.append(Window(f"{_label((at - start) / 1e9)}-{_label((stop - start) / 1e9)}", at, stop))
        at = stop
    return out


def _from_spec(start: int, end: int, spec) -> list[Window]:
    items = spec.get("windows") if isinstance(spec, dict) else spec
    if not isinstance(items, list) or not items:
        raise ValueError("a windows file holds a non-empty list of windows (or {\"windows\": [...]})")
    out, names = [], set()
    for index, item in enumerate(items, 1):
        if not isinstance(item, dict):
            raise ValueError(f"window {index} is not an object")
        name = str(item.get("name") or f"window {index}")
        if name in names:
            raise ValueError(f"two windows are named {name!r}")
        names.add(name)
        try:
            if "start" in item or "end" in item:
                a = timestamp(item["start"]) if "start" in item else start
                b = timestamp(item["end"]) if item.get("end") is not None else end
            else:
                a = start + offset(item.get("from", 0))
                b = start + offset(item["to"]) if item.get("to") is not None else end
        except (KeyError, ValueError) as error:
            raise ValueError(f"window {name!r}: {error}") from None
        if b <= a:
            raise ValueError(f"window {name!r} ends before it starts")
        out.append(Window(name, a, b))
    return out


class Index:
    """Which windows an event belongs to, by binary search over window starts."""

    def __init__(self, windows: list[Window]):
        self.windows = sorted(windows, key=lambda w: (w.start, w.end))
        self.starts = [w.start for w in self.windows]
        self.reach, furthest = [], None
        for w in self.windows:
            furthest = w.end if furthest is None else max(furthest, w.end)
            self.reach.append(furthest)  # the furthest end of any window starting at or before this one

    def of(self, start: int, duration: int = 0) -> list[Window]:
        """Windows holding the instant ``start`` (duration 0), or overlapping [start, start + duration)."""
        stop = start + max(duration, 1)
        i = bisect.bisect_left(self.starts, stop) - 1
        found = []
        while i >= 0 and self.reach[i] > start:
            if self.windows[i].end > start:
                found.append(self.windows[i])
            i -= 1
        return found

    def bounds(self) -> tuple[int, int]:
        return self.starts[0], max(self.reach)


def overlap(a: int, b: int, window: Window) -> int:
    """Nanoseconds of [a, b) inside the window."""
    return max(0, min(b, window.end) - max(a, window.start))


def union(intervals, window: Window) -> int:
    """Nanoseconds of the window covered by the union of intervals (overlaps counted once)."""
    clipped = sorted((max(a, window.start), min(b, window.end)) for a, b in intervals
                     if a < window.end and b > window.start)
    total, reached = 0, window.start
    for a, b in clipped:
        if b > reached:
            total += b - max(a, reached)
            reached = b
    return total
