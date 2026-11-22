"""profile: typed commands over JFR recordings and a JVM's native memory.

Maps (package prefix -> group or owner, tick stage maps) come as inline objects or JSON files, never as
built-in names, so nothing about one project lives here. The maps are params rather than module config
because results are cached on the params and the input files' hashes.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from pathlib import Path

from wb_core.module import Cancelled, Context, EnvironmentProblem, ParamError

from . import analysis, attribution, jfc
from . import memory as accounting
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
