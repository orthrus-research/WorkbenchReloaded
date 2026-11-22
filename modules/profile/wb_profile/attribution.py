"""profile.attribute: a recording's CPU, samples, allocation, compilation and waits, charged to the owner
whose code incurred them (see owners.py for how a frame's owner is found).

Each thread's CPU comes from ``jdk.ThreadCPULoad`` (a share of the whole machine since the thread's last
point) and is split by that thread group's execution samples, so shares become CPU seconds without
trusting sample counts across threads. Time an engine owner spends below another owner's frame is also
credited to that owner as engine work done for it. Compilations and deoptimizations are charged to the
owner of their method.
"""

from __future__ import annotations

import os
import re
from collections import Counter, defaultdict

from .analysis import ALLOCATION
from .owners import RUNTIME_OWNER, UNNAMED_HANDLER, Owners, thread_group
from .recording import ANYTIME, Event, Span, Stream
from .windows import Index, Window, overlap

EVENTS = ("jdk.ExecutionSample", "jdk.NativeMethodSample", *ALLOCATION, "jdk.JavaMonitorEnter", "jdk.ThreadPark",
          "jdk.GarbageCollection", "jdk.ThreadCPULoad", "jdk.CPULoad", "jdk.Compilation", "jdk.Deoptimization",
          "jdk.CompilationFailure", "jdk.DataLoss")
MARGIN = 30_000_000_000  # CPU load points this far outside the windows still cover time inside them
UNSAMPLED = "(threads without samples)"  # CPU of threads never sampled in Java code: JIT, GC, the recorder
SEMANTICS = {
    "cpu": "A thread group's CPU seconds come from jdk.ThreadCPULoad (each point covers the time since the "
           "thread's previous point, at most 1.5 recorder periods) times the processor count, and are split "
           "among owners by the group's execution samples.",
    "owners": "A sample's owner is its first owned frame from the leaf; the Java runtime and transparent "
              "prefixes pass their time to their caller; unmapped code is owned by its package root.",
    "engine": "Engine work for an owner is engine time below that owner's frames, at the group's CPU a "
              "sample; it is also part of the engine's own CPU: never add the two.",
    "unsampled": "CPU of thread groups with no execution samples (JIT compilers, GC, the recorder) is one "
                 "row of its own, so the ranking's CPU adds up to the window's.",
    "allocation": "Allocation weights estimate allocation pressure, not retained heap.",
    "compilation": "Only compilations over the recording's threshold are recorded (100 ms in the profile "
                   "settings, 1 s by default).",
}


class _Tables:
    def __init__(self):
        self.events: Counter = Counter()
        self.samples: Counter = Counter()
        self.native: Counter = Counter()
        self.allocation = {kind: Counter() for kind in ALLOCATION}
        self.monitor: Counter = Counter()
        self.park: Counter = Counter()
        self.cpu: Counter = Counter()
        self.compile_ns: Counter = Counter()
        self.compiles: Counter = Counter()
        self.deopts: Counter = Counter()
        self.failures: Counter = Counter()
        self.gc_pauses: list[int] = []
        self.gc_longest = 0
        self.gc_nanos = 0
        self.machine: list[tuple[float, float]] = []
        self.data_loss = 0


def _percent(fractions: list[float]) -> float | None:
    return round(100 * sum(fractions) / len(fractions), 2) if fractions else None


def cores_of(info: dict, given: int | None) -> tuple[int, str]:
    """The processor count ThreadCPULoad shares are of, and where it came from."""
    if given:
        return given, "the cores parameter"
    arguments = (info.get("jdk.JVMInformation") or {}).get("jvmArguments") or ""
    match = re.search(r"-XX:ActiveProcessorCount=(\d+)", arguments)
    if match and int(match.group(1)) > 0:
        return int(match.group(1)), "the JVM's -XX:ActiveProcessorCount"
    hardware = (info.get("jdk.CPUInformation") or {}).get("hwThreads")
    if hardware:
        return int(hardware), "the recording's CPU information"
    return os.cpu_count() or 1, "this machine (the recording names no processor count)"


class Attribution:
    def __init__(self, stream: Stream, windows: list[Window], owners: Owners, cores: int | None = None,
                 focus: list[str] | None = None, top: int = 12):
        self.stream, self.windows, self.owners = stream, windows, owners
        self.given_cores, self.focus, self.top = cores, list(focus or []), top
        self.index = Index(windows)
        self.tables = {w.name: _Tables() for w in windows}
        self.info: dict[str, dict] = {}
        self.points: dict = defaultdict(list)  # thread -> [(time, load)]
        self.seen: Counter = Counter()
        self._charges: dict = {}

    def feed(self, event: Event):
        kind = event.type
        if kind in ANYTIME:
            self.info[kind] = event.values
            return
        values = event.values
        if kind == "jdk.ThreadCPULoad":
            load = float(values.get("user") or 0) + float(values.get("system") or 0)
            self.points[event.thread].append((event.start, load))
            return
        hits = self.index.of(event.start, event.duration)
        if not hits:
            return
        self.seen[kind] += 1
        tables = [self.tables[w.name] for w in hits]
        for t in tables:
            t.events[kind] += 1
        if kind == "jdk.ExecutionSample" or kind == "jdk.NativeMethodSample":
            key = (event.stack, event.thread)
            for t in tables:
                (t.samples if kind == "jdk.ExecutionSample" else t.native)[key] += 1
        elif kind in ALLOCATION:
            weight = max(0, int(values.get(ALLOCATION[kind]) or 0))
            key = (event.stack, event.thread, values.get("objectClass") or "(unknown)")
            for t in tables:
                t.allocation[kind][key] += weight
        elif kind in ("jdk.JavaMonitorEnter", "jdk.ThreadPark"):
            for w, t in zip(hits, tables):
                (t.monitor if kind == "jdk.JavaMonitorEnter" else t.park)[(event.stack, event.thread)] += \
                    overlap(event.start, event.end, w)
        elif kind == "jdk.GarbageCollection":
            for t in tables:
                pause = int(values.get("sumOfPauses") or 0)
                t.gc_pauses.append(pause)
                t.gc_longest = max(t.gc_longest, int(values.get("longestPause") or 0))
                t.gc_nanos += event.duration
        elif kind == "jdk.CPULoad":
            jvm = float(values.get("jvmUser") or 0) + float(values.get("jvmSystem") or 0)
            for t in tables:
                t.machine.append((float(values.get("machineTotal") or 0), jvm))
        elif kind == "jdk.Compilation":
            method = self.stream.methods.get(values.get("method"), "(unknown)")
            for t in tables:
                t.compile_ns[method] += event.duration
                t.compiles[method] += 1
        elif kind == "jdk.Deoptimization":
            method = self.stream.methods.get(values.get("method"), "(unknown)")
            for t in tables:
                t.deopts[(method, values.get("reason") or "(unknown)")] += 1
        elif kind == "jdk.CompilationFailure":
            for t in tables:
                t.failures[values.get("failureMessage") or "(unknown)"] += 1
        elif kind == "jdk.DataLoss":
            for t in tables:
                t.data_loss += int(values.get("amount") or 0)

    def charge(self, stack):
        found = self._charges.get(stack)
        if found is None:
            found = self._charges[stack] = self.owners.charge(self.stream.frames(stack))
        return found

    def _cpu(self, cores: int, origin: int) -> float:
        """Spreads every CPU load point over the windows its interval overlaps; returns the recorder's
        period in seconds (the median gap between a thread's points). A thread's first point covers one
        period before it, never before the recording."""
        ordered = {thread: sorted(points) for thread, points in self.points.items()}
        gaps = sorted(b[0] - a[0] for points in ordered.values() for a, b in zip(points, points[1:]) if b[0] > a[0])
        period = gaps[len(gaps) // 2] if gaps else 10_000_000_000
        for thread, points in ordered.items():
            previous = None
            for at, load in points:
                began = max(previous if previous is not None else at - period, at - int(1.5 * period), origin)
                previous = at
                if at <= began or load <= 0:
                    continue
                seconds = load * cores * (at - began) / 1e9
                for w in self.index.of(began, at - began):
                    self.tables[w.name].cpu[thread] += seconds * overlap(began, at, w) / (at - began)
        return period / 1e9

    def report(self, span: Span) -> dict:
        cores, source = cores_of(self.info, self.given_cores)
        period = self._cpu(cores, span.start)
        windows = [self.window_report(w, span.start) for w in self.windows]
        return {"cores": cores, "coresFrom": source, "cpuLoadPeriodSeconds": round(period, 3),
                "windows": windows}

    def _kind(self, owner: str) -> str:
        if owner == UNSAMPLED:
            return "unsampled"
        if owner in self.owners.engine:
            return "engine"
        if owner == RUNTIME_OWNER:
            return "runtime"
        if owner == UNNAMED_HANDLER:
            return "merged handler"
        if owner in self.owners.spec.waits.values():
            return "wait"
        return "owner"

    def window_report(self, window: Window, origin: int) -> dict:
        t, top = self.tables[window.name], self.top
        name = self.stream.thread_name
        group_cpu = Counter()
        for thread, seconds in t.cpu.items():
            group_cpu[thread_group(name(thread))] += seconds
        count, owners, hot, behalf, calls, present = Counter(), *(defaultdict(Counter) for _ in range(5))
        for (stack, thread), n in t.samples.items():
            group, charge = thread_group(name(thread)), self.charge(stack)
            count[group] += n
            owners[group][charge.owner] += n
            hot[group][f"{charge.owner}: {charge.method}"] += n
            if charge.behalf:
                behalf[group][charge.behalf] += n
                calls[group][f"{charge.behalf}: {charge.behalf_method} -> {charge.owner}: {charge.method}"] += n
            for owner in charge.present:
                present[group][owner] += n
        native = defaultdict(Counter)
        for (stack, thread), n in t.native.items():
            native[thread_group(name(thread))][self.charge(stack).owner] += n
        kinds = ("jdk.ObjectAllocationSample",) if self.seen["jdk.ObjectAllocationSample"] else \
            ("jdk.ObjectAllocationInNewTLAB", "jdk.ObjectAllocationOutsideTLAB")
        allocated, sites = defaultdict(Counter), defaultdict(Counter)
        for kind in kinds:
            for (stack, thread, cls), weight in t.allocation[kind].items():
                group, charge = thread_group(name(thread)), self.charge(stack)
                allocated[group][charge.owner] += weight
                sites[group][f"{charge.owner}: {charge.method} ({cls})"] += weight
        monitor, park = defaultdict(Counter), defaultdict(Counter)
        for table, into in ((t.monitor, monitor), (t.park, park)):
            for (stack, thread), nanos in table.items():
                into[thread_group(name(thread))][self.charge(stack).owner] += nanos

        threads, ranking = {}, defaultdict(Counter)
        for group in sorted(set(count) | set(group_cpu) | set(native) | set(allocated)):
            cpu, k = group_cpu.get(group, 0.0), count.get(group, 0)
            per_sample = cpu / k if k else 0.0
            for owner, n in owners[group].items():
                ranking[owner]["cpuSeconds"] += n * per_sample
                ranking[owner]["samples"] += n
                if group in self.focus:
                    ranking[owner][f"cpu:{group}"] += n * per_sample
            for owner, n in behalf[group].items():
                ranking[owner]["engineCpuSeconds"] += n * per_sample
            for owner, weight in allocated[group].items():
                ranking[owner]["allocationBytes"] += weight
            if not (k or cpu > 0.005 or native[group] or allocated[group]):
                continue
            threads[group] = {
                "cpuSeconds": round(cpu, 3), "samples": k, "nativeSamples": sum(native[group].values()),
                "owners": {o: {"share": round(n / k, 4), "cpuSeconds": round(n * per_sample, 3)}
                           for o, n in owners[group].most_common(top)},
                "engineFor": {o: round(n * per_sample, 3) for o, n in behalf[group].most_common(top)},
                "hotMethods": dict(hot[group].most_common(2 * top)),
                "engineCalls": dict(calls[group].most_common(top)),
                "onStack": {o: round(n / k, 4) for o, n in present[group].most_common(top)} if k else {},
                "native": dict(native[group].most_common(top)),
                "allocationMB": {o: round(b / 1e6, 3) for o, b in allocated[group].most_common(top)},
                "allocationSitesMB": {s: round(b / 1e6, 3) for s, b in sites[group].most_common(top)},
                "monitorMs": {o: round(v / 1e6, 3) for o, v in monitor[group].most_common(top)},
                "parkMs": {o: round(v / 1e6, 3) for o, v in park[group].most_common(top)},
            }
        unsampled = sum(cpu for group, cpu in group_cpu.items() if not count.get(group))
        if unsampled > 0:
            ranking[UNSAMPLED]["cpuSeconds"] += unsampled
        compile_ms, compiles, deopts, deopt_methods, reasons = Counter(), Counter(), Counter(), Counter(), Counter()
        for method, nanos in t.compile_ns.items():
            owner = self.owners.single(method)
            compile_ms[owner] += nanos / 1e6
            compiles[owner] += t.compiles[method]
            ranking[owner]["compileMs"] += nanos / 1e6
            ranking[owner]["compiles"] += t.compiles[method]
        for (method, reason), n in t.deopts.items():
            owner = self.owners.single(method)
            deopts[owner] += n
            deopt_methods[f"{owner}: {method}"] += n
            reasons[reason] += n
            ranking[owner]["deoptimizations"] += n
        slowest = Counter({m: ns / 1e6 for m, ns in t.compile_ns.items()})
        rows = []
        for owner, values in ranking.items():
            row = {"owner": owner, "kind": self._kind(owner)}
            for key in ("cpuSeconds", "engineCpuSeconds", "samples", "allocationBytes", "compileMs", "compiles",
                        "deoptimizations"):
                value = values.get(key, 0)
                row[key] = round(value, 3) if isinstance(value, float) else int(value)
            if self.focus:
                row["cpuByThread"] = {g: round(values.get(f"cpu:{g}", 0.0), 3) for g in self.focus}
            rows.append(row)
        rows.sort(key=lambda r: (-(r["cpuSeconds"] + r["engineCpuSeconds"]), -r["samples"], r["owner"]))
        machine = t.machine
        return dict(window.describe(origin), events=dict(sorted(t.events.items())),
                    cpuSeconds=round(sum(group_cpu.values()), 3), ranking=rows, threads=threads,
                    compilation={"msByOwner": {o: round(v, 1) for o, v in compile_ms.most_common(top)},
                                 "countByOwner": dict(compiles.most_common(top)),
                                 "slowestMethodsMs": {m: round(v, 1) for m, v in slowest.most_common(top)},
                                 "deoptimizationsByOwner": dict(deopts.most_common(top)),
                                 "deoptimizedMethods": dict(deopt_methods.most_common(top)),
                                 "deoptimizationReasons": dict(reasons.most_common(8)),
                                 "failures": dict(t.failures.most_common(5))},
                    gc={"collections": len(t.gc_pauses), "pauseMs": round(sum(t.gc_pauses) / 1e6, 3),
                        "longestPauseMs": round(t.gc_longest / 1e6, 3), "gcMs": round(t.gc_nanos / 1e6, 3)},
                    cpu={"machinePercent": _percent([m for m, _ in machine]),
                         "jvmPercent": _percent([j for _, j in machine])},
                    dataLossBytes=t.data_loss)


def markdown(report: dict, title: str, top: int = 12) -> str:
    lines = [f"# Attribution: {title}", "",
             f"{report['cores']} logical processors ({report['coresFrom']}); CPU load points every "
             f"{report['cpuLoadPeriodSeconds']} s. CPU is split among owners by each thread group's samples.", ""]
    for w in report["windows"]:
        lines += [f"## {w['name']} ({w['seconds']} s, {w['cpuSeconds']} CPU s)", "",
                  "| Owner | Kind | CPU s | Engine work for it, CPU s | Samples | Allocation MB | Compile ms "
                  "| Deopts |", "|---|---|---|---|---|---|---|---|"]
        for r in w["ranking"][: 2 * top]:
            lines.append(f"| {r['owner']} | {r['kind']} | {r['cpuSeconds']:.2f} | {r['engineCpuSeconds']:.2f} | "
                         f"{r['samples']} | {r['allocationBytes'] / 1e6:.1f} | {r['compileMs']:.0f} | "
                         f"{r['deoptimizations']} |")
        lines += ["", "| Thread group | CPU s | Samples | Owners |", "|---|---|---|---|"]
        heaviest = sorted(w["threads"].items(), key=lambda kv: (-kv[1]["cpuSeconds"], -kv[1]["samples"]))
        for group, g in heaviest[:top]:
            owners = ", ".join(f"{o} {100 * v['share']:.0f}%" for o, v in list(g["owners"].items())[:5])
            lines.append(f"| {group} | {g['cpuSeconds']:.2f} | {g['samples']} | {owners or '-'} |")
        gc = w["gc"]
        lines += ["", f"GC: {gc['pauseMs']} ms of pauses in {gc['collections']} collections, longest "
                      f"{gc['longestPauseMs']} ms.", ""]
    return "\n".join(lines) + "\n"
