"""A Chrome Trace Event timeline (``trace.json``; open it in Perfetto or chrome://tracing).

The windows lie on their own track with their totals as arguments, sample and allocation counters change
at each window, and recorded spans (GC pauses, lock waits, file I/O, traced methods and ticks) lie on their
threads. Times are microseconds from the first window's start, so viewers keep sub-millisecond detail; the
epoch origin is in ``otherData``. Past ``limit`` spans, the longest are kept.
"""

from __future__ import annotations

import heapq
import itertools
import json
from pathlib import Path

WINDOWS_TRACK = 0
PROCESS = 1


class Timeline:
    def __init__(self, origin: int, limit: int = 200_000):
        self.origin = origin
        self.limit = limit
        self.fixed: list[dict] = []
        self.spans: list[tuple[int, int, dict]] = []  # a min-heap on duration: the shortest leaves first
        self.dropped = 0
        self.threads: dict[int, str] = {}
        self._order = itertools.count()

    def _us(self, nanos: int) -> float:
        return round((nanos - self.origin) / 1000, 3)

    def thread(self, tid: int, name: str):
        self.threads.setdefault(tid, name)

    def window(self, name: str, start: int, end: int, args: dict):
        self.fixed.append({"name": name, "cat": "window", "ph": "X", "pid": PROCESS, "tid": WINDOWS_TRACK,
                           "ts": self._us(start), "dur": round((end - start) / 1000, 3), "args": args})

    def counter(self, name: str, at: int, values: dict):
        self.fixed.append({"name": name, "ph": "C", "pid": PROCESS, "ts": self._us(at),
                           "args": {k: round(v, 3) for k, v in values.items()}})

    def instant(self, name: str, cat: str, tid: int, at: int, args: dict | None = None):
        self.fixed.append({"name": name, "cat": cat, "ph": "i", "s": "t", "pid": PROCESS, "tid": tid,
                           "ts": self._us(at), "args": args or {}})

    def span(self, name: str, cat: str, tid: int, start: int, duration: int, args: dict | None = None):
        event = {"name": name, "cat": cat, "ph": "X", "pid": PROCESS, "tid": tid, "ts": self._us(start),
                 "dur": round(duration / 1000, 3)}
        if args:
            event["args"] = args
        item = (duration, next(self._order), event)
        if len(self.spans) < self.limit:
            heapq.heappush(self.spans, item)
        else:
            heapq.heappushpop(self.spans, item)
            self.dropped += 1

    def write(self, path: Path, process: str, other: dict) -> int:
        meta = [{"name": "process_name", "ph": "M", "pid": PROCESS, "args": {"name": process}},
                {"name": "thread_name", "ph": "M", "pid": PROCESS, "tid": WINDOWS_TRACK, "args": {"name": "windows"}},
                {"name": "thread_sort_index", "ph": "M", "pid": PROCESS, "tid": WINDOWS_TRACK,
                 "args": {"sort_index": -1}}]
        meta += [{"name": "thread_name", "ph": "M", "pid": PROCESS, "tid": tid, "args": {"name": name}}
                 for tid, name in sorted(self.threads.items())]
        spans = sorted((event for _, _, event in self.spans), key=lambda e: e["ts"])
        events = meta + self.fixed + spans
        document = {"traceEvents": events, "displayTimeUnit": "ms",
                    "otherData": dict(other, originEpochNanos=self.origin, droppedSpans=self.dropped)}
        path.write_text(json.dumps(document, separators=(",", ":")), encoding="utf-8")
        return len(events)
