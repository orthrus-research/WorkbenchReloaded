"""Splitting a game loop's tick into stages by the methods that run them.

A stage map names the tick method, optionally the thread that runs it, and the methods of its stages::

    {"tick": "com.example.Server.tick", "thread": "Server thread",
     "stages": {"com.example.World.tick": "world", "com.example.Net.flush": "network"}}

Methods are ``package.Class.method`` (``package.Class::method``, the JFR filter form, is accepted too).
Without ``thread``, the tick threads are those whose samples or traces show the tick method: JFR can keep a
thread's first name after the game renames it. Samples on a tick thread go to the stage whose method is
nearest the leaf; with the tick method on the stack but no stage method, to "other"; without it, to
"outside tick". With method traces (``jdk.MethodTrace``, JDK 25+, see profile.settings), each complete
tick's wall time is split once: every instant goes to the innermost stage span covering it, the rest to
"other".
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

OTHER = "other"
OUTSIDE = "outside tick"


def method_name(text: str) -> str:
    text = str(text).strip()
    if "::" in text:
        cls, _, name = text.partition("::")
        return f"{cls}.{name}"
    return text


def filter_name(method: str) -> str:
    """A method in JFR's method-trace filter form: package.Class::method."""
    cls, _, name = method.rpartition(".")
    return f"{cls}::{name}"


@dataclass
class StageMap:
    stages: dict[str, str] = field(default_factory=dict)
    tick: str | None = None
    thread: str | None = None

    @classmethod
    def load(cls, *sources) -> StageMap | None:
        """One map from several (later ones win); None when none names any stage or tick."""
        merged = cls()
        for source in sources:
            if source in (None, {}):
                continue
            if not isinstance(source, dict):
                raise ValueError("a stage map is a JSON object")
            unknown = set(source) - {"tick", "thread", "stages"}
            if unknown:
                raise ValueError(f"unknown stage map keys {sorted(unknown)} (known: tick, thread, stages)")
            if source.get("tick"):
                merged.tick = method_name(source["tick"])
            if source.get("thread"):
                merged.thread = str(source["thread"])
            stages = source.get("stages") or {}
            if not isinstance(stages, dict):
                raise ValueError("stages is an object of method -> stage")
            for method, stage in stages.items():
                merged.stages[method_name(method)] = str(stage)
        return merged if merged.stages or merged.tick else None

    def methods(self) -> list[str]:
        return ([self.tick] if self.tick else []) + [m for m in self.stages if m != self.tick]

    def describe(self) -> dict:
        return {"tick": self.tick, "thread": self.thread, "stages": self.stages}


def stage_of(frames, stages: dict[str, str], tick: str | None) -> str:
    """The stage of one sample's stack (leaf first)."""
    for frame in frames:
        if frame in stages:
            return stages[frame]
    if tick is None or tick in frames:
        return OTHER
    return OUTSIDE


def split_tick(start: int, end: int, spans) -> Counter:
    """Nanoseconds of [start, end) by stage: each instant goes to the shortest (innermost) span covering
    it, uncovered time to "other". ``spans`` are (start, end, stage); the parts always sum to end - start."""
    clipped = [(max(start, a), min(end, b), b - a, stage) for a, b, stage in spans if a < end and b > start]
    points = sorted({start, end, *(a for a, _, _, _ in clipped), *(b for _, b, _, _ in clipped)})
    parts: Counter = Counter()
    for a, b in zip(points, points[1:]):
        covering = [(length, stage) for x, y, length, stage in clipped if x <= a and y >= b]
        parts[min(covering)[1] if covering else OTHER] += b - a
    return parts
