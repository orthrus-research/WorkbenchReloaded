"""profile: typed commands over JFR recordings, a JVM's native memory and GPU telemetry.

Maps (package prefix -> group or owner, tick stage maps) and GPU expectations come as inline objects or JSON files,
never as built-in names, so nothing about one project or one machine lives here. They are params rather than
module config because results are cached on the params and the input files' hashes.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from collections import Counter
from datetime import timedelta, timezone
from pathlib import Path

from wb_core.module import Cancelled, Context, EnvironmentProblem, ParamError

from . import analysis, attribution, gpu, jfc, telemetry
from . import memory as accounting
from . import sampler as sampling
from .owners import Owners, Spec
from .recording import Span, Stream, export
from .recording import span as span_of
from .stages import StageMap, method_name
from .timeline import Timeline
from .windows import Window, iso, resolve

JDK = 26  # the exporter's JDK; module.toml requires it


# -- shared ---------------------------------------------------------------------------------------------
def _json(value: str | None, name: str):
    if not value:
        return None
    path = Path(value)
    if not path.is_file():
        raise ParamError(f"{name} is not a file: {value}")
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except ValueError as error:
        raise ParamError(f"{name} is not JSON: {error}") from None


def _maps(build, *sources):
    try:
        return build(*sources)
    except ValueError as error:
        raise ParamError(str(error)) from None


def _recording(value: str, windows: str | None, split: float | None, skip: float) -> tuple[Path, Span, list[Window]]:
    path = Path(value)
    if not path.is_file():
        raise ParamError(f"recording is not a file: {value}")
    try:
        span = span_of(path)
        return path, span, resolve(span.start, span.end, _json(windows, "windows"), split, skip)
    except ValueError as error:
        raise ParamError(str(error)) from None


def _stream(ctx: Context, path: Path, windows: list[Window], types, margin: int = 0) -> tuple[Stream, int, int]:
    """The recording's events of ``types`` inside the windows (widened by ``margin`` ns)."""
    start, end = min(w.start for w in windows) - margin, max(w.end for w in windows) + margin
    lines = export(ctx.java(JDK), path, start, end, types, cancelled=ctx.cancelled, scratch=ctx.work)
    return Stream(lines), start, end


def _drain(ctx: Context, stream: Stream, sink, start: int, end: int, label: str):
    ctx.progress("read", 0.0, f"reading {label}")
    count = 0
    try:
        for event in stream:
            sink.feed(event)
            count += 1
            if count % 250_000 == 0:
                ctx.progress("read", min(0.99, max(0.0, (event.start - start) / max(1, end - start))),
                             f"{count:,} events")
    except InterruptedError:
        raise Cancelled() from None
    except RuntimeError as error:
        raise EnvironmentProblem(str(error)) from None
    except ValueError as error:
        raise EnvironmentProblem(f"the export of {label} could not be read: {error}") from None
    ctx.progress("analyse", None, f"{count:,} events")


def _describe(path: Path, span: Span, stream: Stream, info: dict) -> dict:
    jvm = info.get("jdk.JVMInformation") or {}
    cpu = info.get("jdk.CPUInformation") or {}
    totals = stream.totals or {}
    return {"file": path.name, "bytes": path.stat().st_size, "format": span.version, "chunks": span.chunks,
            "start": iso(span.start), "end": iso(span.end), "seconds": round(span.seconds, 3),
            "events": totals.get("events"), "exported": totals.get("exported"),
            "eventTypes": totals.get("types", {}),
            "jvm": {"version": jvm.get("jvmVersion"), "arguments": jvm.get("jvmArguments")} if jvm else None,
            "cpu": {"description": (cpu.get("description") or "").split("\n", 1)[0] or None,
                    "hwThreads": cpu.get("hwThreads"), "cores": cpu.get("cores")} if cpu else None}


def _write(ctx: Context, name: str, document: dict) -> Path:
    path = ctx.out / name
    path.write_text(json.dumps(document, indent=1) + "\n", encoding="utf-8")
    ctx.output(Path(name).stem, path, "application/json")
    return path


def _coverage(ctx: Context, span: Span, windows: list[Window], reports: list[dict]):
    """Findings: a window reaching outside the recording, or events JFR lost inside a window."""
    for w in windows:
        if w.start < span.start or w.end > span.end:
            ctx.finding(f"window {w.name} reaches outside the recording ({iso(span.start)} to {iso(span.end)})")
    for w in reports:
        if w["dataLossBytes"]:
            ctx.finding(f"JFR lost {w['dataLossBytes']:,} bytes of events in window {w['name']}: "
                        "its counts are incomplete")


# -- profile.jfr ----------------------------------------------------------------------------------------
def jfr(ctx: Context, recording: str, windows: str | None = None, split: float | None = None, skip: float = 0.0,
        groups: dict | None = None, groups_file: str | None = None, stages: dict | None = None,
        stages_file: str | None = None, top: int = 20):
    path, span, chosen = _recording(recording, windows, split, skip)
    owners = Owners(_maps(Spec.merge, _json(groups_file, "groups_file"), groups))
    stage_map = _maps(StageMap.load, _json(stages_file, "stages_file"), stages)
    stream, start, end = _stream(ctx, path, chosen, analysis.EVENTS)
    timeline = Timeline(min(w.start for w in chosen))
    profile = analysis.Profile(stream, chosen, owners, stage_map, top, timeline)
    _drain(ctx, stream, profile, start, end, path.name)
    result = profile.report(span)
    notes = analysis.notes(profile, result["windows"])
    report = {"schema": "wb.profile.jfr/1", "recording": _describe(path, span, stream, profile.info),
              "groups": owners.spec.describe(), "stages": stage_map.describe() if stage_map else None,
              "semantics": analysis.SEMANTICS, "notes": notes, **result}
    _write(ctx, "report.json", report)
    trace = ctx.out / "trace.json"
    timeline.write(trace, path.name, {"recording": path.name, "semantics": analysis.SEMANTICS})
    ctx.output("trace", trace, "application/json")

    samples, seen, allocated, paused = 0, Counter(), 0, 0.0
    for w in result["windows"]:
        samples += w["execution"]["samples"]
        seen.update({r["name"]: r["count"] for r in w["execution"]["groups"]})
        allocated += w["allocation"]["bytes"]
        paused += w["gc"]["pauseUnionMs"]
        labels = {"window": w["name"]}
        ctx.measure("execution_samples", w["execution"]["samples"], **labels)
        ctx.measure("allocation_bytes", w["allocation"]["bytes"], "B", **labels)
        ctx.measure("gc_pause_ms", w["gc"]["pauseUnionMs"], "ms", **labels)
        ctx.measure("lock_wait_ms", round(sum(v["ms"] for v in w["waits"].values()), 3), "ms", **labels)
        ctx.measure("file_io_ms", round(sum(v["ms"] for v in w["io"].values()), 3), "ms", **labels)
        ticks = (w.get("stages") or {}).get("ticks")
        if ticks and ticks["count"]:
            ctx.measure("tick_p50_ms", ticks["p50Ms"], "ms", **labels)
            ctx.measure("tick_p95_ms", ticks["p95Ms"], "ms", **labels)
    leaders = [{"name": n, "count": c, "share": round(c / samples, 4)} for n, c in seen.most_common(5)] \
        if samples else []
    _coverage(ctx, span, chosen, result["windows"])
    seconds = sum(w.seconds for w in chosen)
    if ctx.outcome == "ok":
        shares = ", ".join(f"{r['name']} {100 * r['share']:.0f}%" for r in leaders[:3] if r["share"] >= 0.01)
        ctx.summary(f"{len(chosen)} window{'s' if len(chosen) != 1 else ''}, {seconds:.1f} s: {samples:,} execution "
                    f"samples{f' ({shares})' if shares else ''}; {allocated / 2**20:,.1f} MiB allocated; "
                    f"GC paused {paused:,.1f} ms")
    return {"windows": len(chosen), "seconds": round(seconds, 3), "samples": samples, "groups": leaders,
            "allocationBytes": allocated, "gcPauseMs": round(paused, 3), "notes": notes}


# -- profile.attribute ----------------------------------------------------------------------------------
def attribute(ctx: Context, recording: str, windows: str | None = None, split: float | None = None,
              skip: float = 0.0, owners: dict | None = None, owners_file: str | None = None,
              cores: int | None = None, threads: list | None = None, top: int = 12):
    path, span, chosen = _recording(recording, windows, split, skip)
    spec = _maps(Spec.merge, _json(owners_file, "owners_file"), owners)
    stream, start, end = _stream(ctx, path, chosen, attribution.EVENTS, attribution.MARGIN)
    charged = attribution.Attribution(stream, chosen, Owners(spec), cores, threads, top)
    _drain(ctx, stream, charged, start, end, path.name)
    result = charged.report(span)
    notes = []
    if result["coresFrom"].startswith("this machine"):
        notes.append(f"the recording names no processor count: CPU seconds assume this machine's {result['cores']}")
    if not charged.points:
        notes.append("no jdk.ThreadCPULoad events: CPU seconds are zero and only sample shares stand "
                     "(profile.settings records them every second)")
    elif min(w.seconds for w in chosen) < 2 * result["cpuLoadPeriodSeconds"]:
        notes.append(f"CPU load points are {result['cpuLoadPeriodSeconds']} s apart, coarse for the shortest "
                     "window: its CPU seconds are spread from the points around it")
    report = {"schema": "wb.profile.attribute/1", "recording": _describe(path, span, stream, charged.info),
              "map": spec.describe(), "semantics": attribution.SEMANTICS, "notes": notes, **result}
    _write(ctx, "attribution.json", report)
    text = ctx.out / "attribution.md"
    text.write_text(attribution.markdown(report, path.name, top), encoding="utf-8")
    ctx.output("attribution-report", text, "text/markdown")
    for w in result["windows"]:
        for row in w["ranking"][:top]:
            ctx.measure("owner_cpu_seconds", row["cpuSeconds"], "s", window=w["name"], owner=row["owner"])
    _coverage(ctx, span, chosen, result["windows"])
    longest = max(result["windows"], key=lambda w: w["seconds"])
    if ctx.outcome == "ok":
        leaders = [r for r in longest["ranking"] if r["cpuSeconds"] or r["samples"]][:4]
        parts = [f"{r['owner']} {r['cpuSeconds']:.2f} s"
                 + (f" (+{r['engineCpuSeconds']:.2f} s engine work)" if r["engineCpuSeconds"] else "") for r in leaders]
        ctx.summary(f"{longest['name']} ({longest['seconds']} s, {longest['cpuSeconds']:.1f} CPU s): "
                    + (", ".join(parts) or "no samples"))
    keys = ("owner", "cpuSeconds", "engineCpuSeconds", "samples")
    return {"windows": len(chosen), "cores": result["cores"], "notes": notes,
            "ranking": {w["name"]: [{k: r[k] for k in keys} for r in w["ranking"][:top]] for w in result["windows"]}}


# -- profile.memory -------------------------------------------------------------------------------------
def _jcmd(ctx: Context, jcmd: Path, pid: int, *command: str) -> str:
    done = ctx.run([jcmd, str(pid), *command], timeout=120, log_output=False)
    lines = done.stdout.splitlines()
    if done.returncode != 0 or any("Exception" in line or "Unknown diagnostic command" in line for line in lines[:5]):
        tail = " | ".join(line.strip() for line in lines[-4:] if line.strip())
        raise EnvironmentProblem(f"jcmd {pid} {command[0]} failed: {tail or f'exit {done.returncode}'}")
    return done.stdout


def memory(ctx: Context, pid: int | None = None, nmt: str | None = None, system_map: str | None = None,
           jdk: str = "26", top: int = 8):
    if (pid is None) == (nmt is None):
        raise ParamError("give pid (a running JVM to ask) or nmt (a saved VM.native_memory summary), one of them")
    if system_map and pid is not None:
        raise ParamError("system_map goes with nmt: with pid the map is read from the JVM")
    notes = []
    if pid is not None:
        jcmd = ctx.jdk(jdk) / "bin" / ("jcmd.exe" if os.name == "nt" else "jcmd")
        nmt_text = _jcmd(ctx, jcmd, pid, "VM.native_memory", "summary")
        (ctx.out / "native-memory.txt").write_text(nmt_text, encoding="utf-8")
        ctx.output("native-memory", ctx.out / "native-memory.txt", "text/plain")
        try:
            map_text = _jcmd(ctx, jcmd, pid, "System.map")
            (ctx.out / "system-map.txt").write_text(map_text, encoding="utf-8")
            ctx.output("system-map", ctx.out / "system-map.txt", "text/plain")
        except EnvironmentProblem as error:
            map_text = None
            notes.append(f"no memory map ({error}); System.map needs a JDK 21+ target")
    else:
        nmt_text = Path(nmt).read_text(encoding="utf-8", errors="replace")
        map_text = Path(system_map).read_text(encoding="utf-8", errors="replace") if system_map else None
    problem = EnvironmentProblem if pid is not None else ParamError
    try:
        summary = accounting.nmt(nmt_text)
        mapped = accounting.system_map(map_text) if map_text else None
    except ValueError as error:
        raise problem(str(error)) from None
    if mapped is None:
        notes.append("without a memory map, native libraries and drivers, images and what is unaccounted are unknown")
    elif mapped["style"] == "linux" and not any(m["rss"] is not None for m in mapped["mappings"]):
        notes.append("the map has no resident sizes (a JDK before 23): accessible mappings count whole")
    result = accounting.account(summary, mapped, top)
    result["notes"] = notes
    _write(ctx, "memory.json", result)
    for row in result["classes"]:
        if row["bytes"] is not None:
            ctx.measure("memory_mib", row["mib"], "MiB", owner=row["owner"])
    ctx.summary(accounting.summary_line(result))
    return {"basis": result["basis"], "classes": {c["owner"]: c["bytes"] for c in result["classes"]},
            "processTotal": result.get("processTotal"), "notes": notes}


# -- profile.settings -----------------------------------------------------------------------------------
def settings(ctx: Context, stages: dict | None = None, stages_file: str | None = None, methods: list | None = None,
             sample_period: float = 0.01, threshold: float = 0.001, method_threshold: float = 0.0,
             allocation_rate: str = "150/s", cpu_period: float = 1.0, compilation_threshold: float = 0.1,
             tlab: bool = False, name: str = "profile"):
    stage_map = _maps(StageMap.load, _json(stages_file, "stages_file"), stages)
    traced = [*(stage_map.methods() if stage_map else []), *(method_name(m) for m in methods or [])]
    traced = list(dict.fromkeys(traced))
    bad = [m for m in traced if not re.match(r"^[\w$]+(\.[\w$]+)+$", m)]
    if bad:
        raise ParamError(f"not a package.Class.method name: {', '.join(bad)}")
    events = jfc.settings(traced, sample_period=sample_period, threshold=threshold, method_threshold=method_threshold,
                          allocation_rate=allocation_rate, cpu_period=cpu_period,
                          compilation_threshold=compilation_threshold, tlab=tlab)
    path = ctx.out / f"{name}.jfc"
    jfc.write(path, events, f"Workbench {name}")
    ctx.output("settings", path, "application/xml")
    where = ctx.host_path(path)
    options = [f"-XX:StartFlightRecording=settings={where},filename={name}.jfr,dumponexit=true", *jfc.JVM_OPTIONS]
    traced_text = f", tracing {len(traced)} method{'s' if len(traced) != 1 else ''}" if traced else ""
    ctx.summary(f"JFR settings for {len(events)} events{traced_text}: start the JVM with {' '.join(options)}")
    return {"settings": where, "options": options, "traced": traced, "events": sorted(events)}


# -- GPU telemetry: shared ------------------------------------------------------------------------------
GPU_MEASURES = (("graphicsClockMhz", "median", "gpu_graphics_clock_mhz", "MHz"),
                ("memoryClockMhz", "median", "gpu_memory_clock_mhz", "MHz"),
                ("powerW", "median", "gpu_power_w", "W"),
                ("utilisationPercent", "median", "gpu_utilisation_percent", "%"),
                ("temperatureC", "max", "gpu_temperature_max_c", "°C"),
                ("memoryUsedMiB", "max", "gpu_memory_used_max_mib", "MiB"))
FINDINGS_LISTED = 20  # past this many, one finding counts the rest (the report has them all)


def _gpu_line(window: dict) -> str:
    """Each device of a window in words: its name (with its index when there are several) and its headline."""
    devices = window["devices"]
    names = [d["name"] or f"device {d['device']}" for d in devices]
    if len(devices) > 1:
        names = [f"{name} ({d['device']})" if d["name"] else name for name, d in zip(names, devices)]
    return "; ".join(f"{name}: {telemetry.headline(d)}" for name, d in zip(names, devices))


def _gpu_measures(ctx: Context, windows: list[dict]):
    for w in windows:
        for d in w["devices"]:
            labels = {"window": w["name"], "device": d["device"]}
            for key, stat, name, unit in GPU_MEASURES:
                value = (d["metrics"].get(key) or {}).get(stat)
                if value is not None:
                    ctx.measure(name, value, unit, **labels)
            ctx.measure("gpu_throttled_seconds", d["throttle"]["throttledSeconds"], "s", **labels)


def _gpu_totals(windows: list[dict]) -> tuple[dict, float]:
    """Seconds in each performance state and throttled, added over windows and devices."""
    states, throttled = Counter(), 0.0
    for w in windows:
        for d in w["devices"]:
            for state, entry in d["pstates"].items():
                states[state] += entry["seconds"]
            throttled += d["throttle"]["throttledSeconds"]
    return {state: round(seconds, 3) for state, seconds in states.most_common()}, round(throttled, 3)


def _gpu_findings(ctx: Context, chosen: list[Window], windows: list[dict], start: int, end: int,
                  tolerance: float, report: str):
    """Findings: a window outside the samples or with gaps in them, and every expectation a window broke."""
    said, more = 0, 0

    def finding(text: str):
        nonlocal said, more
        if said < FINDINGS_LISTED:
            ctx.finding(text)
            said += 1
        else:
            more += 1
    for window, entry in zip(chosen, windows):
        outside = window.start < start or window.end > end
        if outside:
            finding(f"window {window.name} reaches outside the samples ({iso(start)} to {iso(end)})")
        for d in entry["devices"]:
            where = f"window {window.name}" + (f", device {d['device']}" if len(entry["devices"]) > 1 else "")
            if not d["coveredSeconds"]:
                if not outside:
                    finding(f"{where}: no samples")
                continue
            if d["coverage"] < 0.9 and not outside:
                finding(f"{where}: the samples cover {d['coverage']:.0%} of it "
                        f"(the longest gap is {d['longestGapSeconds']:,.1f} s)")
            for violation in d["expectations"]:
                if violation["finding"]:
                    finding(f"{where}: {telemetry.describe(violation, tolerance)}")
    if more:
        ctx.finding(f"{more} more finding{'s' if more != 1 else ''}: see {report}")


# -- profile.gpu-sample ---------------------------------------------------------------------------------
def _gpu_tool(tool: str):
    adapters = gpu.adapters()
    names = list(adapters) if tool == "auto" else [tool]
    for name in names:
        executable = shutil.which(adapters[name].EXECUTABLE)
        if executable:
            return adapters[name], executable
    raise EnvironmentProblem(f"{' or '.join(names)} is not on PATH on this machine")


def _gpu_query(ctx: Context):
    def run(args: list[str]) -> tuple[int, str]:
        ctx.log.debug("$ " + " ".join(args))
        try:
            return sampling.run(args)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"{Path(args[0]).name} did not answer in 30 s") from None
    return run


@contextlib.contextmanager
def _cancel_stops(ctx: Context):
    """While sampling, a cancel ends the sampling instead of interrupting the handler. The worker sets
    ctx.cancelled and then interrupts the main thread; that interrupt is taken here, so the samples so far are
    still summarised and kept. A Ctrl+C in a console cancels the same way."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    taken = threading.Event()

    def take(*_):
        taken.set()
        ctx.cancelled.set()
    previous = signal.signal(signal.SIGINT, take)
    try:
        yield
    finally:
        deadline = time.monotonic() + 1.0
        while ctx.cancelled.is_set() and not taken.is_set() and time.monotonic() < deadline:
            time.sleep(0.02)  # the interrupt that follows a cancel lands here, not on the results
        signal.signal(signal.SIGINT, previous if previous is not None else signal.default_int_handler)


def _sample_until(ctx: Context, sampler: sampling.Sampler, seconds: float | None, period_ms: int) -> str:
    """Waits for the end of the sampling: the time asked for (once there is a sample), a cancel, or the tool
    stopping."""
    began = time.monotonic()
    deadline = began + seconds if seconds else None
    first_by = began + max(10.0, 5 * period_ms / 1000)
    report_at = began + 10
    while True:
        now = time.monotonic()
        if deadline is not None and now >= deadline and sampler.count:
            return "time"
        if not sampler.alive():
            return "tool exited"
        if not sampler.count and now > first_by:
            return "no samples"
        if ctx.cancelled.wait(0.2 if deadline is None or now >= deadline else min(0.2, deadline - now)):
            return "cancelled"
        if now >= report_at:
            ctx.progress("sample", min(1.0, (now - began) / seconds) if seconds else None,
                         f"{sampler.count:,} samples")
            report_at = now + 10


def gpu_sample(ctx: Context, seconds: float | None = None, period_ms: int = 500, device: int | None = None,
               tool: str = "auto", label: str = "gpu"):
    """Samples the host's GPUs beside whatever runs on them. It takes no GPU lease and asks for no quiet machine:
    the lease is one lock, so a sampler that held it would wait out, or hold up, the run it is there to watch;
    reading the driver's counters needs neither. Inside a turn it runs under the turn's lease like any step."""
    adapter, executable = _gpu_tool(tool)
    ctx.progress("probe", msg=f"{adapter.NAME} at {executable}")
    try:
        plan = adapter.plan(executable, _gpu_query(ctx), device)
    except LookupError as error:
        raise ParamError(str(error)) from None
    except (RuntimeError, OSError) as error:
        raise EnvironmentProblem(f"{adapter.NAME}: {error}") from None
    devices = [d for d in plan.devices if device is None or d["device"] == device]
    drivers = sorted({d["driver"] for d in devices if d["driver"]})
    ctx.machine_info(gpu=", ".join(d["name"] or f"device {d['device']}" for d in devices),
                     **({"driver": ", ".join(drivers)} if drivers else {}))
    path, summary = ctx.out / f"{label}-samples.jsonl", f"{label}-summary.json"
    with _cancel_stops(ctx):  # from here a cancel ends the sampling, and what was sampled is still summarised
        tap, stopped, code = _record(ctx, adapter, plan, path, seconds, period_ms, device)
        tail = f": {' | '.join(tap.errors)}" if tap.errors else ""
        if not tap.samples:
            if stopped == "cancelled":
                raise Cancelled()
            why = f"exited {code}" if code is not None else "gave no samples"
            raise EnvironmentProblem(f"{adapter.NAME} {why}" + (tail or (f": {tap.examples[0]}" if tap.examples
                                                                          else "")))
        series = telemetry.series(tap.samples, period_ms * 1_000_000)
        start, end = telemetry.bounds(series)
        chosen = resolve(start, end)
        windows = telemetry.analyse(series, chosen, origin=start)
        sampled = (end - start) / 1e9
        notes = []
        if plan.missing:
            notes.append(f"{adapter.NAME} here cannot give {', '.join(plan.missing)}")
        if tap.unread:
            notes.append(f"{tap.unread} line{'s' if tap.unread != 1 else ''} of {adapter.NAME} were not samples "
                         f"(first: {tap.examples[0]})")
        if stopped == "tool exited":
            ctx.finding(f"{adapter.NAME} stopped {sampled:,.1f} s in (exit {code}){tail}: the samples end there")
        _gpu_findings(ctx, chosen, windows, start, end, 0.0, summary)
        ctx.output(f"{label}-samples", path, "application/x-ndjson")
        _write(ctx, summary, {
            "schema": "wb.profile.gpu-sample/1", "samples": {"file": path.name, "format": gpu.SCHEMA,
                                                             "count": len(tap.samples)},
            "query": plan.describe(), "periodMs": period_ms, "device": device, "start": iso(start), "end": iso(end),
            "seconds": round(sampled, 3), "stopped": stopped, "toolExit": code, "unreadLines": tap.unread,
            "devices": devices, "semantics": telemetry.SEMANTICS, "notes": notes, "windows": windows})
        _gpu_measures(ctx, windows)
        if ctx.outcome == "ok":
            ctx.summary(f"{sampled:,.1f} s every {period_ms} ms, {len(tap.samples):,} samples"
                        + (", stopped by a cancel" if stopped == "cancelled" else "") + f": {_gpu_line(windows[0])}")
        pstates, throttled = _gpu_totals(windows)
        return {"samples": len(tap.samples), "seconds": round(sampled, 3), "periodMs": period_ms,
                "stopped": stopped, "devices": devices, "pstates": pstates, "throttledSeconds": throttled,
                "notes": notes}


def _record(ctx: Context, adapter, plan, path: Path, seconds: float | None, period_ms: int, device: int | None):
    """Runs the tool's looping query into the samples file until the sampling ends: (sampler, why, tool's exit)."""
    with open(path, "w", encoding="utf-8", newline="\n") as sink:
        def keep(sample: dict):
            sink.write(gpu.dumps(sample) + "\n")
            sink.flush()
        tap = sampling.Sampler(adapter.command(plan, period_ms, device), lambda line: adapter.parse(plan, line), keep)
        ctx.progress("sample", 0.0 if seconds else None,
                     f"every {period_ms} ms" + (f" for {seconds:g} s" if seconds else " until cancelled"))
        try:
            tap.start()
        except OSError as error:
            raise EnvironmentProblem(f"{adapter.NAME} could not start: {error}") from None
        try:
            stopped = _sample_until(ctx, tap, seconds, period_ms)
        finally:
            code = tap.stop()
    return tap, stopped, code


# -- profile.gpu-report ---------------------------------------------------------------------------------
def _zone(text: str | None):
    """The tzinfo of a UTC offset such as +01:00, -0500 or Z; None when not given."""
    if not text:
        return None
    if text.upper() == "Z":
        return timezone.utc
    digits = text[1:].replace(":", "")
    try:
        return timezone((1 if text[0] == "+" else -1) * timedelta(hours=int(digits[:2]), minutes=int(digits[2:])))
    except ValueError as error:
        raise ParamError(f"utc_offset {text}: {error}") from None


def _absolute(spec) -> bool:
    """Whether a windows file places any window by time rather than by offset."""
    items = spec.get("windows") if isinstance(spec, dict) else spec
    return any(isinstance(item, dict) and ("start" in item or "end" in item) for item in items or [])


def gpu_report(ctx: Context, samples: str, windows: str | None = None, split: float | None = None,
               skip: float = 0.0, device: int | None = None, expect: dict | None = None,
               expect_file: str | None = None, tolerance: float = 0.0, columns: list | None = None,
               utc_offset: str | None = None, top: int = 10):
    path = Path(samples)
    if not path.is_file():
        raise ParamError(f"samples is not a file: {samples}")
    zone = _zone(utc_offset)
    try:
        pairs, notes, source = gpu.load(path.read_text(encoding="utf-8-sig", errors="replace"), columns or [],
                                        zone or timezone.utc)
    except ValueError as error:
        raise ParamError(f"samples: {error}") from None
    saved_csv = source != gpu.SCHEMA
    if saved_csv and zone is None:
        notes.append("the CSV's times have no zone and are read as UTC: give utc_offset to place them")
    if device is not None:
        pairs = [pair for pair in pairs if pair[1]["device"] == device]
        if not pairs:
            raise ParamError(f"no samples of device {device} in {path.name}")
    extra = _json(expect_file, "expect_file")
    if extra is not None and not isinstance(extra, dict):
        raise ParamError("expect_file holds an object of expectations")
    try:
        expected = telemetry.expectations({**(extra or {}), **(expect or {})})
    except ValueError as error:
        raise ParamError(f"expect: {error}") from None
    series = telemetry.series(pairs)
    start, end = telemetry.bounds(series)
    spec = _json(windows, "windows")
    if saved_csv and zone is None and _absolute(spec):
        raise ParamError("the windows are placed by time and the CSV's times have no zone: give utc_offset "
                         "(e.g. +01:00), or windows by offset (from/to)")
    try:
        chosen = resolve(start, end, spec, split, skip)
    except ValueError as error:
        raise ParamError(str(error)) from None
    result = telemetry.analyse(series, chosen, expected, tolerance, top, origin=start)
    if not any(r.get("throttleReasons") is not None for _, r in pairs):
        notes.append("no sample names its throttle reasons: throttling is unknown, not absent")
    unknown = sorted({r for _, row in pairs for r in row.get("throttleReasons") or [] if gpu.kind(r) == "unknown"})
    if unknown:
        notes.append(f"clock event reasons no table names: {', '.join(unknown)}")
    _gpu_findings(ctx, chosen, result, start, end, tolerance, "gpu-report.json")
    _write(ctx, "gpu-report.json", {
        "schema": "wb.profile.gpu-report/1",
        "source": {"file": path.name, "bytes": path.stat().st_size, "format": source, "samples": len(pairs),
                   "start": iso(start), "end": iso(end), "seconds": round((end - start) / 1e9, 3),
                   "devices": [{"device": s.device, "name": s.name, "samples": len(s.times),
                                "periodMs": round(s.period / 1e6, 3)} for s in series.values()]},
        "expect": expected, "tolerance": tolerance, "semantics": telemetry.SEMANTICS, "notes": notes,
        "windows": result})
    _gpu_measures(ctx, result)
    seconds = sum(w.seconds for w in chosen)
    if ctx.outcome == "ok":
        longest = max(range(len(chosen)), key=lambda i: chosen[i].seconds)
        named = f"{chosen[longest].name} ({chosen[longest].seconds:,.1f} s) " if len(chosen) > 1 else ""
        ctx.summary(f"{len(chosen)} window{'s' if len(chosen) != 1 else ''}, {seconds:,.1f} s of "
                    f"{len(series)} device{'s' if len(series) != 1 else ''}: {named}{_gpu_line(result[longest])}")
    pstates, throttled = _gpu_totals(result)
    violations = sum(1 for w in result for d in w["devices"] for v in d["expectations"] if v["finding"])
    return {"windows": len(chosen), "seconds": round(seconds, 3), "samples": len(pairs),
            "devices": [{"device": s.device, "name": s.name} for s in series.values()], "pstates": pstates,
            "throttledSeconds": throttled, "violations": violations, "notes": notes}
