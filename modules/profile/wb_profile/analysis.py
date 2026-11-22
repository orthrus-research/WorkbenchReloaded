"""profile.jfr: a recording's samples, allocation, GC pauses, file I/O, lock waits and tick stages, window by
window. Events stream through once; per window only counts by (stack, thread) are kept, and every table is
derived from them at the end, with each stack charged to a group once."""

from __future__ import annotations

import math
import os
from collections import Counter, defaultdict

from .owners import Owners
from .recording import ANYTIME, Event, Span, Stream
from .stages import OUTSIDE, StageMap, split_tick, stage_of
from .timeline import Timeline
from .windows import Index, Window, overlap, union

SAMPLES = ("jdk.ExecutionSample", "jdk.NativeMethodSample")
ALLOCATION = {"jdk.ObjectAllocationSample": "weight", "jdk.ObjectAllocationInNewTLAB": "tlabSize",
              "jdk.ObjectAllocationOutsideTLAB": "allocationSize"}
IO = {"jdk.FileRead": "bytesRead", "jdk.FileWrite": "bytesWritten", "jdk.FileForce": None}
WAITS = {"jdk.JavaMonitorEnter": "monitorClass", "jdk.JavaMonitorWait": "monitorClass",
         "jdk.ThreadPark": "parkedClass"}
EVENTS = (*SAMPLES, *ALLOCATION, "jdk.GarbageCollection", "jdk.GCPhasePause", *IO, *WAITS, "jdk.MethodTrace",
          "jdk.DataLoss")
SEMANTICS = {
    "samples": "Execution and native samples are counts of sampled stacks, not CPU time; native samples are "
               "kept apart. JFR samples a few threads each period, so shares compare within one recording.",
    "groups": "A stack's group is its first frame from the leaf that is not transparent (the Java runtime, "
              "prefixes mapped to null, wrapper frames); unmapped code is grouped by its package root. "
              "Inclusive counts overlap: a method counts once for every sample it is on the stack of.",
    "allocation": "Allocation weights estimate allocation pressure in bytes; they are not retained heap.",
    "durations": "An event with a duration belongs to every window it overlaps; its time is clipped to the "
                 "window, its longest is the whole event's. Durations overlap across threads: never add "
                 "them up as wall time.",
    "gc": "GarbageCollection pause sums and the union of top-level GC pauses are two views of the same "
          "pauses: never add them together.",
    "io": "Only file operations over the recording's threshold are recorded.",
    "ticks": "Tick spans include the tracer's overhead; only ticks wholly inside a window are split.",
}


class _Durations:
    def __init__(self):
        self.count = self.nanos = self.longest = self.bytes = 0
        self.stacks: Counter = Counter()
        self.keys: Counter = Counter()
        self.threads: Counter = Counter()

    def add(self, nanos: int, whole: int, stack, thread, key: str, size: int = 0):
        self.count += 1
        self.nanos += nanos
        self.longest = max(self.longest, whole)
        self.bytes += size
        self.stacks[stack] += nanos
        self.keys[key] += nanos
        self.threads[thread] += nanos


class _Tables:
    def __init__(self):
        self.events: Counter = Counter()
        self.samples = {kind: Counter() for kind in SAMPLES}
        self.allocation = {kind: Counter() for kind in ALLOCATION}
        self.gc: Counter = Counter()
        self.gc_longest = 0
        self.gc_causes: Counter = Counter()
        self.gc_names: Counter = Counter()
        self.pauses: list[tuple[int, int]] = []
        self.io = {kind: _Durations() for kind in IO}
        self.waits = {kind: _Durations() for kind in WAITS}
        self.traces: dict[tuple, list[int]] = defaultdict(list)
        self.boundary_traces: Counter = Counter()
        self.data_loss = 0


def rows(counter: Counter, total: float, top: int | None = None, unit: str = "count", scale: float = 1.0,
         digits: int = 0) -> list[dict]:
    out = []
    for name, value in counter.most_common(top):
        scaled = round(value * scale, digits) if digits else int(round(value * scale))
        out.append({"name": str(name), unit: scaled, "share": round(value / total, 4) if total else None})
    return out


def quantile(ordered: list[int], p: float) -> int:
    """Nearest rank."""
    return ordered[max(0, math.ceil(len(ordered) * p) - 1)]


def _ms(nanos: float) -> float:
    return round(nanos / 1e6, 3)


class Profile:
    def __init__(self, stream: Stream, windows: list[Window], owners: Owners, stages: StageMap | None = None,
                 top: int = 20, timeline: Timeline | None = None):
        self.stream, self.windows, self.owners, self.stages = stream, windows, owners, stages
        self.top, self.timeline = top, timeline
        self.index = Index(windows)
        self.tables = {w.name: _Tables() for w in windows}
        self.info: dict[str, dict] = {}
        self.seen: Counter = Counter()
        self.stage_spans: list[tuple[str, int, int, int]] = []  # (method, thread, start, end)
        self._charges: dict = {}
        self._inclusive: dict = {}

    # -- streaming -----------------------------------------------------------------------------------
    def feed(self, event: Event):
        kind = event.type
        if kind in ANYTIME:
            self.info[kind] = event.values
            return
        hits = self.index.of(event.start, event.duration)
        if not hits:
            return
        self.seen[kind] += 1
        tables = [self.tables[w.name] for w in hits]
        for t in tables:
            t.events[kind] += 1
        values = event.values
        if kind in SAMPLES:
            key = (event.stack, event.thread)
            for t in tables:
                t.samples[kind][key] += 1
        elif kind in ALLOCATION:
            weight = max(0, int(values.get(ALLOCATION[kind]) or 0))
            key = (event.stack, event.thread, values.get("objectClass") or "(unknown)")
            for t in tables:
                t.allocation[kind][key] += weight
        elif kind == "jdk.GarbageCollection":
            for t in tables:
                t.gc["collections"] += 1
                t.gc["pauseNanos"] += int(values.get("sumOfPauses") or 0)
                t.gc_longest = max(t.gc_longest, int(values.get("longestPause") or 0))
                t.gc_causes[values.get("cause") or "(unknown)"] += 1
                t.gc_names[values.get("name") or "(unknown)"] += 1
        elif kind == "jdk.GCPhasePause":
            for t in tables:
                t.pauses.append((event.start, event.end))
            self._span(values.get("name") or "GC pause", kind, event)
        elif kind in IO:
            field = IO[kind]
            size = max(0, int(values.get(field) or 0)) if field else 0
            path = values.get("path") or "(unknown)"
            for w, t in zip(hits, tables):
                t.io[kind].add(overlap(event.start, event.end, w), event.duration, event.stack, event.thread,
                               path, size)
            self._span(f"{kind.rsplit('.', 1)[-1]} {os.path.basename(path) or path}", kind, event, {"path": path})
        elif kind in WAITS:
            cls = values.get(WAITS[kind]) or "(unknown)"
            for w, t in zip(hits, tables):
                t.waits[kind].add(overlap(event.start, event.end, w), event.duration, event.stack, event.thread, cls)
            self._span(f"{kind.rsplit('.', 1)[-1]} {cls}", kind, event)
        elif kind == "jdk.MethodTrace":
            method = self.stream.methods.get(values.get("method"), "(unknown)")
            for w, t in zip(hits, tables):
                if w.start <= event.start and event.end <= w.end:
                    t.traces[(event.thread, method)].append(event.duration)
                else:
                    t.boundary_traces[(event.thread, method)] += 1
            stage = None
            if self.stages and (method == self.stages.tick or method in self.stages.stages):
                self.stage_spans.append((method, event.thread, event.start, event.end))
                stage = "tick" if method == self.stages.tick else self.stages.stages[method]
            self._span(stage or method, kind, event, {"method": method} if stage else None)
        elif kind == "jdk.DataLoss":
            for t in tables:
                t.data_loss += int(values.get("amount") or 0)
            if self.timeline:
                self.timeline.instant("data loss", kind, 0, event.start, {"bytes": values.get("amount")})

    def _span(self, name: str, cat: str, event: Event, args: dict | None = None):
        if self.timeline is None or event.duration <= 0:
            return
        tid = (event.thread or 0) + 1
        self.timeline.thread(tid, self.stream.thread_name(event.thread))
        self.timeline.span(name, cat, tid, event.start, event.duration, args)

    # -- tables --------------------------------------------------------------------------------------
    def charge(self, stack):
        found = self._charges.get(stack)
        if found is None:
            found = self._charges[stack] = self.owners.charge(self.stream.frames(stack))
        return found

    def inclusive(self, stack) -> tuple[str, ...]:
        found = self._inclusive.get(stack)
        if found is None:
            found = self._inclusive[stack] = tuple(
                dict.fromkeys(m for m in self.stream.frames(stack) if self.owners.owner(m) is not None))
        return found

    def _samples(self, counter: Counter) -> dict:
        total = sum(counter.values())
        groups, leaf, first, inclusive, threads = Counter(), Counter(), Counter(), Counter(), Counter()
        missing = truncated = 0
        for (stack, thread), n in counter.items():
            frames = self.stream.frames(stack)
            missing += n if not frames else 0
            truncated += n if stack in self.stream.truncated else 0
            charge = self.charge(stack)
            groups[charge.owner] += n
            leaf[frames[0] if frames else "(no stack)"] += n
            first[charge.method] += n
            for method in self.inclusive(stack):
                inclusive[method] += n
            threads[self.stream.thread_name(thread)] += n
        return {"samples": total, "missingStacks": missing, "truncatedStacks": truncated,
                "groups": rows(groups, total, self.top), "groupMethods": rows(first, total, self.top),
                "leafMethods": rows(leaf, total, self.top), "inclusiveMethods": rows(inclusive, total, self.top),
                "threads": rows(threads, total, self.top)}

    def allocation_source(self) -> tuple[str, ...]:
        if self.seen["jdk.ObjectAllocationSample"]:
            return ("jdk.ObjectAllocationSample",)
        return ("jdk.ObjectAllocationInNewTLAB", "jdk.ObjectAllocationOutsideTLAB")

    def _allocation(self, t: _Tables) -> dict:
        kinds = self.allocation_source()
        groups, types, sites, threads = Counter(), Counter(), Counter(), Counter()
        events = sum(t.events[k] for k in kinds)
        for kind in kinds:
            for (stack, thread, cls), weight in t.allocation[kind].items():
                charge = self.charge(stack)
                groups[charge.owner] += weight
                types[cls] += weight
                sites[charge.method] += weight
                threads[self.stream.thread_name(thread)] += weight
        total = sum(groups.values())
        return {"source": " + ".join(kinds), "events": events, "bytes": total,
                "groups": rows(groups, total, unit="bytes"), "types": rows(types, total, self.top, "bytes"),
                "sites": rows(sites, total, self.top, "bytes"), "threads": rows(threads, total, self.top, "bytes")}

    def _durations(self, d: _Durations, key_name: str, with_bytes: bool = False) -> dict:
        groups, sites, threads = Counter(), Counter(), Counter()
        for stack, nanos in d.stacks.items():
            charge = self.charge(stack)
            groups[charge.owner] += nanos
            sites[charge.method] += nanos
        for thread, nanos in d.threads.items():
            threads[self.stream.thread_name(thread)] += nanos
        out = {"count": d.count, "ms": _ms(d.nanos), "longestMs": _ms(d.longest)}
        if with_bytes:
            out["bytes"] = d.bytes
        ms = dict(unit="ms", scale=1e-6, digits=3)
        out.update({"groups": rows(groups, d.nanos, **ms), key_name: rows(d.keys, d.nanos, self.top, **ms),
                    "sites": rows(sites, d.nanos, self.top, **ms), "threads": rows(threads, d.nanos, self.top, **ms)})
        return out

    def tick_threads(self) -> set:
        if not self.stages:
            return set()
        if self.stages.thread:
            return {tid for tid, thread in self.stream.threads.items() if thread.name == self.stages.thread}
        if not self.stages.tick:
            return set(self.stream.threads)
        found = {thread for method, thread, _, _ in self.stage_spans if method == self.stages.tick}
        for t in self.tables.values():
            for stack, thread in t.samples["jdk.ExecutionSample"]:
                if thread not in found and self.stages.tick in self.stream.frames(stack):
                    found.add(thread)
        return found

    def _stages(self, window: Window, t: _Tables, threads: set) -> dict:
        stages = Counter()
        for (stack, thread), n in t.samples["jdk.ExecutionSample"].items():
            if thread in threads:
                stages[stage_of(self.stream.frames(stack), self.stages.stages, self.stages.tick)] += n
        total = sum(stages.values())
        out = {"samples": total, "stages": rows(stages, total),
               "insideTickShare": round(1 - stages[OUTSIDE] / total, 4) if total and self.stages.tick else None}
        ticks = self._ticks(window, threads)
        if ticks:
            out["ticks"] = ticks
        return out

    def _ticks(self, window: Window, threads: set) -> dict | None:
        tick = self.stages.tick
        if not tick:
            return None
        children = defaultdict(list)
        spans, boundary = [], 0
        for method, thread, start, end in self.stage_spans:
            if thread not in threads:
                continue
            if method == tick:
                if window.start <= start and end <= window.end:
                    spans.append((thread, start, end))
                elif start < window.end and end > window.start:
                    boundary += 1
            else:
                children[thread].append((start, end, self.stages.stages[method]))
        if not spans and not boundary:
            return None
        parts, lengths = Counter(), sorted(end - start for _, start, end in spans)
        for thread, start, end in spans:
            parts.update(split_tick(start, end, children[thread]))
        count = len(spans)
        return {"count": count, "boundary": boundary,
                "meanMs": _ms(sum(lengths) / count) if count else None,
                "p50Ms": _ms(quantile(lengths, 0.5)) if count else None,
                "p95Ms": _ms(quantile(lengths, 0.95)) if count else None,
                "maxMs": _ms(lengths[-1]) if count else None,
                "stagesMs": {stage: _ms(n) for stage, n in parts.most_common()},
                "stagesMsPerTick": {stage: _ms(n / count) for stage, n in parts.most_common()} if count else {}}

    def _traces(self, t: _Tables) -> list[dict]:
        out = []
        for (thread, method), lengths in sorted(t.traces.items(), key=lambda kv: -sum(kv[1])):
            lengths.sort()
            out.append({"thread": self.stream.thread_name(thread), "method": method, "count": len(lengths),
                        "totalMs": _ms(sum(lengths)), "p50Ms": _ms(quantile(lengths, 0.5)),
                        "p95Ms": _ms(quantile(lengths, 0.95)), "maxMs": _ms(lengths[-1])})
        return out[: self.top * 2]

    def window_report(self, window: Window, origin: int, threads: set) -> dict:
        t = self.tables[window.name]
        gc = {"collections": t.gc["collections"], "pauseMs": _ms(t.gc["pauseNanos"]),
              "longestPauseMs": _ms(t.gc_longest), "pausePhases": len(t.pauses),
              "pauseUnionMs": _ms(union(t.pauses, window)),
              "causes": rows(t.gc_causes, t.gc["collections"]), "collectors": rows(t.gc_names, t.gc["collections"])}
        out = dict(window.describe(origin), events=dict(sorted(t.events.items())),
                   execution=self._samples(t.samples["jdk.ExecutionSample"]),
                   native=self._samples(t.samples["jdk.NativeMethodSample"]),
                   allocation=self._allocation(t), gc=gc,
                   io={k: self._durations(d, "paths", True) for k, d in t.io.items() if d.count},
                   waits={k: self._durations(d, "classes") for k, d in t.waits.items() if d.count},
                   dataLossBytes=t.data_loss)
        if self.stages:
            out["stages"] = self._stages(window, t, threads)
        if t.traces or t.boundary_traces:
            out["methodTraces"] = self._traces(t)
            out["boundaryMethodTraces"] = sum(t.boundary_traces.values())
        return out

    def report(self, span: Span) -> dict:
        threads = self.tick_threads()
        windows = [self.window_report(w, span.start, threads) for w in self.windows]
        if self.timeline:
            self._timeline(windows)
        out = {"windows": windows}
        if self.stages:
            out["tickThreads"] = sorted({self.stream.thread_name(t) for t in threads})
        return out

    def _timeline(self, windows: list[dict]):
        names = Counter()
        for w in windows:
            for row in w["execution"]["groups"]:
                names[row["name"]] += row["count"]
        shown = [name for name, _ in names.most_common(6)]
        last_end = None
        for window, w in zip(self.windows, windows):
            waits = sum(v["ms"] for v in w["waits"].values())
            io = sum(v["ms"] for v in w["io"].values())
            self.timeline.window(window.name, window.start, window.end, {
                "executionSamples": w["execution"]["samples"],
                "allocationMiB": round(w["allocation"]["bytes"] / 2**20, 3),
                "gcPauseMs": w["gc"]["pauseUnionMs"], "lockWaitMs": round(waits, 3), "fileIoMs": round(io, 3)})
            counts = {row["name"]: row["count"] for row in w["execution"]["groups"]}
            values = {name: counts.get(name, 0) for name in shown}
            values["other groups"] = w["execution"]["samples"] - sum(values.values())
            self.timeline.counter("execution samples", window.start, values)
            self.timeline.counter("allocation MiB", window.start, {"allocated": w["allocation"]["bytes"] / 2**20})
            last_end = window.end if last_end is None else max(last_end, window.end)
        if last_end is not None:
            self.timeline.counter("execution samples", last_end, {name: 0 for name in [*shown, "other groups"]})
            self.timeline.counter("allocation MiB", last_end, {"allocated": 0})


def notes(profile: Profile, windows: list[dict]) -> list[str]:
    """What the reader should know before trusting the numbers."""
    out = []
    arguments = (profile.info.get("jdk.JVMInformation") or {}).get("jvmArguments")
    samples = sum(w["execution"]["samples"] for w in windows)
    if arguments is not None and samples and "DebugNonSafepoints" not in arguments:
        out.append("the JVM ran without -XX:+DebugNonSafepoints: samples resolve to the nearest safepoint "
                   "poll, so tight loops attract them; record with the options profile.settings gives")
    if not samples:
        out.append("no execution samples fall in the windows: the recording's settings may not sample "
                   "(profile.settings writes settings that do)")
    if profile.allocation_source()[0] != "jdk.ObjectAllocationSample" and any(
            w["allocation"]["events"] for w in windows):
        out.append("allocation comes from TLAB events (tlabSize, allocationSize), as runtimes before JDK 16 "
                   "record it: a coarser estimate than allocation samples")
    if profile.stages and profile.stages.tick:
        seen = any(w.get("stages", {}).get("insideTickShare") for w in windows) or \
            any(m == profile.stages.tick for m, _, _, _ in profile.stage_spans)
        if not seen:
            out.append(f"the tick method {profile.stages.tick} is on no sampled stack and in no trace: check the "
                       "name (package.Class.method) and the tick thread")
    return out
