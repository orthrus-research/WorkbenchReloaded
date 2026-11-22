"""profile: the parsers on small synthetic inputs, a real JFR recording made here (skipped without a JDK 25+),
GPU telemetry from synthetic nvidia-smi output, a fake nvidia-smi on PATH and the real one (skipped without it),
and the commands through an in-process Workbench and host runner."""

from __future__ import annotations

import _thread
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import types
import unittest
import xml.etree.ElementTree as ET
from datetime import timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
for extra in (REPO / "tests", REPO / "core", HERE.parent):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

REAL_SMI = shutil.which("nvidia-smi")  # before any test puts the fake first on PATH

from wb_core.module import Context, EnvironmentProblem, ParamError  # noqa: E402
from wb_profile import commands as profile_commands  # noqa: E402
from wb_profile import gpu, jfc, memory, nvidia, telemetry  # noqa: E402
from wb_profile.analysis import Profile, notes  # noqa: E402
from wb_profile.attribution import Attribution, cores_of  # noqa: E402
from wb_profile.owners import (  # noqa: E402
    RUNTIME_OWNER,
    UNNAMED_HANDLER,
    Owners,
    Spec,
    package_root,
    thread_group,
)
from wb_profile.recording import HEADER, Span, Stream, export, java_of, span  # noqa: E402
from wb_profile.stages import OTHER, OUTSIDE, StageMap, split_tick, stage_of  # noqa: E402
from wb_profile.timeline import Timeline  # noqa: E402
from wb_profile.windows import Index, Window, iso, offset, resolve, timestamp, union  # noqa: E402

BASE = 1_795_000_000_000_000_000  # an epoch-nanosecond origin for synthetic events
MS = 1_000_000
S = 1_000_000_000


def lines(*rows) -> list[str]:
    return [json.dumps(row) for row in rows]


METHODS = {1: "com.example.alpha.Render.draw", 2: "com.example.alpha.Render.frame", 3: "net.game.Loop.run",
           4: "java.util.HashMap.get", 5: "org.lib.Util.compute", 6: "com.example.beta.Net.flush",
           7: "net.game.Server.tick", 8: "net.game.World.update", 9: "net.game.World.handler$zza000$alpha$onUpdate",
           10: "org.ui.Display.sync"}
TABLES = [*({"m": i, "n": n} for i, n in METHODS.items()),
          {"s": 1, "f": [4, 1, 2, 3]}, {"s": 2, "f": [5, 6, 3]}, {"s": 3, "f": [8, 7]}, {"s": 4, "f": [4], "x": 1},
          {"s": 5, "f": [8, 1, 7]}, {"s": 6, "f": [9, 7]}, {"s": 7, "f": [10, 2, 3]},
          {"h": 1, "n": "Server thread", "j": 20, "o": 900}, {"h": 2, "n": "Render thread", "j": 21, "o": 901},
          {"h": 3, "n": "Worker-12", "j": 22, "o": 902}, {"h": 4, "n": "Worker-13", "j": 23, "o": 903},
          {"h": 5, "n": "C2 CompilerThread0", "j": 9, "o": 800}]
END = {"end": {"events": 99, "exported": 30, "first": BASE, "last": BASE + 2 * S, "types": {}}}
SPAN = Span(start=BASE, end=BASE + 2 * S, chunks=1, version="2.1")


def sample(at_ms: int, stack: int, thread: int, kind: str = "jdk.ExecutionSample") -> dict:
    return {"e": kind, "t": BASE + at_ms * MS, "h": thread, "s": stack}


def timed(kind: str, at_ms: float, length_ms: float, thread: int | None = None, stack: int | None = None, **values):
    row = {"e": kind, "t": BASE + int(at_ms * MS), "d": int(length_ms * MS)}
    if thread is not None:
        row["h"] = thread
    if stack is not None:
        row["s"] = stack
    if values:
        row["v"] = values
    return row


class Windows(unittest.TestCase):
    def test_times_and_offsets(self):
        self.assertEqual(timestamp("2026-11-22T00:56:53.123456789Z") % S, 123456789)
        self.assertEqual(timestamp("2026-11-22T01:56:53+01:00"), timestamp("2026-11-22T00:56:53Z"))
        self.assertEqual(timestamp(42), 42)
        with self.assertRaises(ValueError):
            timestamp("2026-11-22T00:56:53")  # no zone: ambiguous
        self.assertEqual((offset(1.5), offset("250ms"), offset("2m")), (1_500_000_000, 250 * MS, 120 * S))
        self.assertEqual(iso(timestamp("2026-11-22T00:56:53.000000007Z")), "2026-11-22T00:56:53.000000007Z")

    def test_resolve(self):
        whole = resolve(BASE, BASE + 10 * S)
        self.assertEqual([(w.name, w.seconds) for w in whole], [("recording", 10.0)])
        self.assertEqual(resolve(BASE, BASE + 10 * S, skip=3)[0].start, BASE + 3 * S)
        split = resolve(BASE, BASE + 10 * S, split=4, skip=1)
        self.assertEqual([w.name for w in split], ["1s-5s", "5s-9s", "9s-10s"])
        spec = {"windows": [{"name": "warm", "from": "2s", "to": 5},
                            {"name": "late", "start": iso(BASE + 6 * S)},
                            {"start": BASE + S, "end": BASE + 2 * S}]}
        named = resolve(BASE, BASE + 10 * S, spec)
        self.assertEqual([(w.name, w.start - BASE, w.end - BASE) for w in named],
                         [("warm", 2 * S, 5 * S), ("late", 6 * S, 10 * S), ("window 3", S, 2 * S)])
        for bad in ({"windows": [{"name": "x", "from": 5, "to": 4}]}, [{"name": "a"}, {"name": "a"}], []):
            with self.assertRaises(ValueError):
                resolve(BASE, BASE + 10 * S, bad)
        with self.assertRaises(ValueError):
            resolve(BASE, BASE + 10 * S, [{"name": "a"}], split=2)
        with self.assertRaises(ValueError):
            resolve(BASE, BASE + 10 * S, skip=11)

    def test_index_and_union(self):
        a, b, wide = Window("a", 0, 10), Window("b", 10, 20), Window("wide", 5, 30)
        index = Index([b, wide, a])
        self.assertEqual({w.name for w in index.of(0)}, {"a"})
        self.assertEqual({w.name for w in index.of(10)}, {"b", "wide"})   # instants: [start, end)
        self.assertEqual({w.name for w in index.of(9, 2)}, {"a", "b", "wide"})  # spans: overlap
        self.assertEqual(index.of(30), [])
        self.assertEqual(index.bounds(), (0, 30))
        self.assertEqual(union([(2, 6), (4, 8), (12, 14)], Window("w", 0, 13)), 7)


class OwnersTest(unittest.TestCase):
    spec = {"owners": {"com.example.alpha.": "Alpha", "com.example.": "Example", "org.lib.": None,
                       "net.game.": "Game", "java.util.zip.": "zip"},
            "engine": ["Game"], "tokens": {"alpha": "Alpha"}, "wrappers": [r"\.Loop\.run$"],
            "waits": {r"\.Display\.sync$": "idle"}}

    def test_frame_owners(self):
        owners = Owners(Spec.merge(self.spec))
        self.assertEqual(owners.owner("com.example.alpha.Render.draw"), "Alpha")  # the longest prefix wins
        self.assertEqual(owners.owner("com.example.beta.Net.flush"), "Example")
        self.assertIsNone(owners.owner("org.lib.Util.compute"))                 # mapped to null: transparent
        self.assertIsNone(owners.owner("java.util.HashMap.get"))                # the runtime: transparent
        self.assertEqual(owners.owner("java.util.zip.Inflater.inflate"), "zip")  # unless the map names it
        self.assertEqual(owners.owner("it.unimi.dsi.fastutil.Map.get"), "it.unimi")  # unmapped: package root
        self.assertIsNone(owners.owner("net.game.Loop.run"))                    # a wrapper
        self.assertEqual(owners.owner("net.game.World.handler$zza000$alpha$onUpdate"), "Alpha")
        self.assertEqual(owners.owner("net.game.World.redirect$abc123$other$hook"), UNNAMED_HANDLER)
        self.assertEqual(package_root("Main.run"), "(default package)")
        self.assertEqual(thread_group("Worker-12 #3"), "Worker-# ##")

    def test_charges(self):
        owners = Owners(Spec.merge(self.spec))
        engine = owners.charge(["net.game.World.update", "com.example.alpha.Render.draw", "net.game.Server.tick"])
        self.assertEqual((engine.owner, engine.behalf, engine.behalf_method),
                         ("Game", "Alpha", "com.example.alpha.Render.draw"))
        self.assertEqual(engine.present, frozenset({"Alpha"}))
        through = owners.charge(["java.util.HashMap.get", "org.lib.Util.compute", "com.example.beta.Net.flush"])
        self.assertEqual((through.owner, through.method), ("Example", "com.example.beta.Net.flush"))
        self.assertEqual(owners.charge(["org.ui.Display.sync", "com.example.alpha.Render.frame"]).owner, "idle")
        self.assertEqual(owners.charge(["java.lang.Thread.run"]).owner, RUNTIME_OWNER)
        self.assertEqual(owners.charge([]).owner, RUNTIME_OWNER)

    def test_merge(self):
        spec = Spec.merge({"a.": "A", "b.": "B"}, {"owners": {"b.": ""}, "engine": ["A"]}, None)
        self.assertEqual(spec.prefixes, {"a.": "A", "b.": None})
        self.assertEqual(spec.engine, ["A"])
        for bad in ({"owners": {"a.": 3}}, {"wrappers": ["("]}, {"owners": {}, "colour": 1}, ["a."]):
            with self.assertRaises(ValueError):
                Spec.merge(bad)


class Stages(unittest.TestCase):
    def test_split_tick_gives_each_instant_to_the_innermost_stage(self):
        parts = split_tick(0, 100, [(10, 50, "world"), (20, 30, "network"), (40, 120, "save"), (200, 300, "x")])
        self.assertEqual(dict(parts), {"other": 10, "world": 30, "network": 10, "save": 50})
        self.assertEqual(sum(parts.values()), 100)

    def test_map_and_sample_stages(self):
        stages = StageMap.load({"tick": "net.game.Server::tick", "stages": {"net.game.World::update": "world"}},
                               {"thread": "Server thread"})
        self.assertEqual((stages.tick, stages.thread), ("net.game.Server.tick", "Server thread"))
        self.assertEqual(stages.methods(), ["net.game.Server.tick", "net.game.World.update"])
        self.assertEqual(stage_of(("a.B.c", "net.game.World.update", "net.game.Server.tick"), stages.stages,
                                  stages.tick), "world")
        self.assertEqual(stage_of(("a.B.c", "net.game.Server.tick"), stages.stages, stages.tick), OTHER)
        self.assertEqual(stage_of(("a.B.c",), stages.stages, stages.tick), OUTSIDE)
        self.assertIsNone(StageMap.load({}, None))
        with self.assertRaises(ValueError):
            StageMap.load({"tick": "a.B.c", "stage": {}})


class Recording(unittest.TestCase):
    @staticmethod
    def chunk(size: int, start: int, duration: int) -> bytes:
        """A chunk: its header (magic, version 2.1, size, offsets, start, duration, ticks) and padding."""
        return HEADER.pack(b"FLR\0", 2, 1, size, 0, 0, start, duration, 0, 10_000_000, 0) + bytes(size - HEADER.size)

    def test_span_reads_every_chunk_header(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "two.jfr"
            first = self.chunk(100, BASE, 3 * S)
            path.write_bytes(first + self.chunk(80, BASE + 3 * S, 2 * S))
            found = span(path)
            self.assertEqual((found.start, found.end, found.chunks, found.version), (BASE, BASE + 5 * S, 2, "2.1"))
            path.write_bytes(b"PK\3\4" + bytes(100))
            with self.assertRaises(ValueError):
                span(path)
            path.write_bytes(first[:40])
            with self.assertRaises(ValueError):
                span(path)

    def test_stream_decodes_tables_and_events(self):
        stream = Stream(lines(*TABLES, sample(5, 1, 2), timed("jdk.ThreadPark", 1, 2, 3, 4, parkedClass="L"), END))
        events = list(stream)
        self.assertEqual([e.type for e in events], ["jdk.ExecutionSample", "jdk.ThreadPark"])
        self.assertEqual(stream.frames(1)[0], "java.util.HashMap.get")
        self.assertEqual(stream.thread_name(2), "Render thread")
        self.assertEqual((events[1].end - events[1].start, events[1].values), (2 * MS, {"parkedClass": "L"}))
        self.assertIn(4, stream.truncated)
        self.assertEqual(stream.totals["events"], 99)
        with self.assertRaises(ValueError):
            list(Stream(lines(*TABLES)))  # no totals: the export stopped early
        with self.assertRaises(ValueError):
            list(Stream(['{"e": "jdk.ExecutionSample"}', json.dumps(END)]))


class ProfileTest(unittest.TestCase):
    """profile.jfr's tables from synthetic exporter lines: two one-second windows."""

    @classmethod
    def setUpClass(cls):
        rows = [*TABLES,
                {"e": "jdk.JVMInformation", "t": BASE, "v": {"jvmArguments": "-Xmx1g", "jvmVersion": "test"}},
                sample(100, 1, 2), sample(150, 1, 2), sample(180, 1, 2),
                sample(200, 3, 1), sample(300, 3, 1), sample(1200, 3, 1), sample(1300, 2, 1),
                sample(400, 4, 2, "jdk.NativeMethodSample"),
                timed("jdk.ObjectAllocationSample", 110, 0, 1, 3, weight=1000, objectClass="[B"),
                timed("jdk.ObjectAllocationSample", 120, 0, 2, 1, weight=3000, objectClass="java.lang.String"),
                timed("jdk.GarbageCollection", 900, 300, 3, None, sumOfPauses=2 * MS, longestPause=2 * MS,
                      cause="Allocation Failure", name="G1New"),
                timed("jdk.GCPhasePause", 900, 300, 3, None, name="GC Pause"),
                timed("jdk.GCPhasePause", 950, 100, 3, None, name="GC Pause"),
                timed("jdk.FileRead", 1500, 5, 1, 2, bytesRead=4096, path="/data/region.bin"),
                timed("jdk.JavaMonitorEnter", 950, 100, 1, 3, monitorClass="com.example.Lock"),
                timed("jdk.ThreadPark", 500, 10, 2, 4, parkedClass="java.util.concurrent.locks.ReentrantLock"),
                timed("jdk.DataLoss", 1600, 0, None, None, amount=512),
                timed("jdk.MethodTrace", 200, 10, 1, None, method=7),
                timed("jdk.MethodTrace", 201, 4, 1, None, method=8),
                timed("jdk.MethodTrace", 202, 1, 1, None, method=6),
                timed("jdk.MethodTrace", 995, 10, 1, None, method=7),
                END]
        cls.stream = Stream(lines(*rows))
        cls.windows = resolve(BASE, BASE + 2 * S, split=1)
        groups = Spec.merge({"com.example.alpha.": "alpha", "com.example.beta.": "beta", "org.lib.": None})
        stages = StageMap.load({"tick": "net.game.Server.tick",
                                "stages": {"net.game.World.update": "world", "com.example.beta.Net.flush": "network"}})
        cls.timeline = Timeline(BASE)
        cls.profile = Profile(cls.stream, cls.windows, Owners(groups), stages, top=10, timeline=cls.timeline)
        for event in cls.stream:
            cls.profile.feed(event)
        cls.report = cls.profile.report(SPAN)
        cls.a, cls.b = cls.report["windows"]

    def test_samples_by_group(self):
        a, b = self.a["execution"], self.b["execution"]
        self.assertEqual((a["samples"], b["samples"]), (5, 2))
        self.assertEqual({r["name"]: r["count"] for r in a["groups"]}, {"alpha": 3, "net.game": 2})
        self.assertEqual({r["name"]: r["count"] for r in b["groups"]}, {"net.game": 1, "beta": 1})  # org.lib passes
        self.assertEqual(a["groupMethods"][0], {"name": "com.example.alpha.Render.draw", "count": 3, "share": 0.6})
        self.assertEqual(a["leafMethods"][0]["name"], "java.util.HashMap.get")
        self.assertIn({"name": "net.game.Loop.run", "count": 3, "share": 0.6}, a["inclusiveMethods"])
        self.assertEqual(a["threads"][0], {"name": "Render thread", "count": 3, "share": 0.6})
        self.assertEqual(self.a["native"]["groups"][0]["name"], RUNTIME_OWNER)
        self.assertEqual(self.a["native"]["truncatedStacks"], 1)

    def test_allocation_gc_io_and_waits(self):
        allocation = self.a["allocation"]
        self.assertEqual((allocation["bytes"], allocation["events"]), (4000, 2))
        self.assertEqual(allocation["types"][0], {"name": "java.lang.String", "bytes": 3000, "share": 0.75})
        self.assertEqual({r["name"]: r["bytes"] for r in allocation["groups"]}, {"alpha": 3000, "net.game": 1000})
        self.assertEqual(self.b["allocation"]["bytes"], 0)
        gc_a, gc_b = self.a["gc"], self.b["gc"]
        self.assertEqual((gc_a["collections"], gc_a["pauseMs"], gc_a["longestPauseMs"]), (1, 2.0, 2.0))
        self.assertEqual((gc_a["pauseUnionMs"], gc_b["pauseUnionMs"]), (100.0, 200.0))  # clipped, overlaps once
        self.assertEqual(gc_b["collections"], 1)  # the collection overlaps both windows
        read = self.b["io"]["jdk.FileRead"]
        self.assertEqual((read["count"], read["bytes"], read["ms"]), (1, 4096, 5.0))
        self.assertEqual(read["paths"][0]["name"], "/data/region.bin")
        self.assertEqual(read["groups"][0]["name"], "beta")
        enter_a, enter_b = self.a["waits"]["jdk.JavaMonitorEnter"], self.b["waits"]["jdk.JavaMonitorEnter"]
        self.assertEqual((enter_a["ms"], enter_b["ms"], enter_a["longestMs"]), (50.0, 50.0, 100.0))
        self.assertEqual(enter_a["classes"][0]["name"], "com.example.Lock")
        self.assertEqual(self.a["waits"]["jdk.ThreadPark"]["threads"][0]["name"], "Render thread")
        self.assertEqual((self.a["dataLossBytes"], self.b["dataLossBytes"]), (0, 512))

    def test_stages_and_ticks(self):
        stages = self.a["stages"]
        self.assertEqual(self.report["tickThreads"], ["Server thread"])
        self.assertEqual({r["name"]: r["count"] for r in stages["stages"]}, {"world": 2})
        ticks = stages["ticks"]
        self.assertEqual((ticks["count"], ticks["boundary"], ticks["p50Ms"]), (1, 1, 10.0))
        self.assertEqual(ticks["stagesMs"], {"other": 6.0, "world": 3.0, "network": 1.0})
        self.assertEqual(self.b["stages"]["ticks"]["boundary"], 1)
        self.assertEqual({(r["name"], r["count"]) for r in self.b["stages"]["stages"]}, {("world", 1), ("network", 1)})
        traces = {(t["method"], t["count"]) for t in self.a["methodTraces"]}
        self.assertEqual(traces, {("net.game.Server.tick", 1), ("net.game.World.update", 1),
                                  ("com.example.beta.Net.flush", 1)})

    def test_notes_and_timeline(self):
        said = notes(self.profile, self.report["windows"])
        self.assertTrue(any("DebugNonSafepoints" in n for n in said), said)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "trace.json"
            self.timeline.write(path, "synthetic.jfr", {})
            trace = json.loads(path.read_text(encoding="utf-8"))
        events = trace["traceEvents"]
        windows = [e for e in events if e.get("cat") == "window"]
        self.assertEqual([(w["name"], w["ts"], w["dur"]) for w in windows], [("0s-1s", 0.0, 1e6), ("1s-2s", 1e6, 1e6)])
        self.assertEqual(windows[0]["args"]["executionSamples"], 5)
        ticks = [e for e in events if e.get("cat") == "jdk.MethodTrace" and e["name"] == "tick"]
        self.assertEqual(len(ticks), 2)
        names = {e["args"]["name"] for e in events if e.get("name") == "thread_name"}
        self.assertIn("Server thread", names)
        counters = [e for e in events if e["ph"] == "C" and e["name"] == "execution samples"]
        self.assertEqual(counters[0]["args"]["alpha"], 3)
        self.assertEqual(trace["otherData"]["originEpochNanos"], BASE)


class AttributionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rows = [*TABLES,
                {"e": "jdk.CPUInformation", "t": BASE, "v": {"hwThreads": 4, "cores": 2}},
                sample(100, 5, 1), sample(200, 5, 1), sample(300, 5, 1), sample(400, 2, 1),
                sample(500, 6, 3), sample(600, 7, 2), sample(1500, 3, 1),
                {"e": "jdk.ThreadCPULoad", "t": BASE, "h": 1, "v": {"user": 0.2, "system": 0.05}},
                {"e": "jdk.ThreadCPULoad", "t": BASE + S, "h": 1, "v": {"user": 0.2, "system": 0.05}},
                {"e": "jdk.ThreadCPULoad", "t": BASE + 2 * S, "h": 1, "v": {"user": 0.2, "system": 0.05}},
                {"e": "jdk.ThreadCPULoad", "t": BASE + S, "h": 5, "v": {"user": 0.125, "system": 0.0}},
                timed("jdk.ObjectAllocationSample", 150, 0, 1, 5, weight=2_000_000, objectClass="[I"),
                timed("jdk.Compilation", 700, 150, 3, None, method=1),
                timed("jdk.Deoptimization", 800, 0, 3, None, method=8, reason="unstable_if"),
                timed("jdk.CompilationFailure", 810, 0, 3, None, failureMessage="out of nodes"),
                timed("jdk.JavaMonitorEnter", 250, 20, 1, 5, monitorClass="L"),
                timed("jdk.CPULoad", 500, 0, machineTotal=0.5, jvmUser=0.2, jvmSystem=0.05),
                END]
        cls.stream = Stream(lines(*rows))
        windows = resolve(BASE, BASE + 2 * S, split=1)
        spec = Spec.merge(OwnersTest.spec)
        cls.attribution = Attribution(cls.stream, windows, Owners(spec), focus=["Server thread"], top=10)
        for event in cls.stream:
            cls.attribution.feed(event)
        cls.report = cls.attribution.report(SPAN)
        cls.a, cls.b = cls.report["windows"]

    def test_cpu_split_by_samples(self):
        self.assertEqual((self.report["cores"], self.report["coresFrom"]), (4, "the recording's CPU information"))
        self.assertAlmostEqual(self.report["cpuLoadPeriodSeconds"], 1.0)
        server = self.a["threads"]["Server thread"]
        self.assertAlmostEqual(server["cpuSeconds"], 1.0)  # 0.25 of 4 processors for one second
        self.assertEqual(server["owners"]["Game"], {"share": 0.75, "cpuSeconds": 0.75})
        self.assertEqual(server["engineFor"], {"Alpha": 0.75})
        self.assertEqual(server["onStack"], {"Alpha": 0.75, "Example": 0.25})
        self.assertIn("Alpha: com.example.alpha.Render.draw -> Game: net.game.World.update", server["engineCalls"])
        self.assertAlmostEqual(self.b["threads"]["Server thread"]["cpuSeconds"], 1.0)
        self.assertEqual(self.a["threads"]["Worker-#"]["owners"]["Alpha"]["share"], 1.0)  # the handler's token
        self.assertEqual(list(self.a["threads"]["Render thread"]["owners"]), ["idle"])
        self.assertEqual(self.a["threads"]["C# CompilerThread#"]["samples"], 0)
        unsampled = next(r for r in self.a["ranking"] if r["kind"] == "unsampled")
        self.assertAlmostEqual(unsampled["cpuSeconds"], 0.5)  # its own row: the ranking adds up
        self.assertAlmostEqual(sum(r["cpuSeconds"] for r in self.a["ranking"]), self.a["cpuSeconds"])

    def test_ranking_compilation_and_allocation(self):
        ranking = {r["owner"]: r for r in self.a["ranking"]}
        self.assertEqual([r["owner"] for r in self.a["ranking"][:2]], ["Game", "Alpha"])
        self.assertEqual((ranking["Alpha"]["cpuSeconds"], ranking["Alpha"]["engineCpuSeconds"]), (0.0, 0.75))
        self.assertEqual(ranking["Alpha"]["compileMs"], 150.0)
        self.assertEqual((ranking["Game"]["kind"], ranking["Game"]["deoptimizations"]), ("engine", 1))
        self.assertEqual(ranking["Game"]["cpuByThread"], {"Server thread": 0.75})
        self.assertEqual(ranking["Game"]["allocationBytes"], 2_000_000)
        self.assertEqual(ranking["idle"]["kind"], "wait")
        compilation = self.a["compilation"]
        self.assertEqual((compilation["msByOwner"], compilation["failures"]), ({"Alpha": 150.0}, {"out of nodes": 1}))
        self.assertEqual(compilation["deoptimizationReasons"], {"unstable_if": 1})
        self.assertEqual(self.a["threads"]["Server thread"]["monitorMs"], {"Game": 20.0})
        self.assertEqual(self.a["cpu"], {"machinePercent": 50.0, "jvmPercent": 25.0})

    def test_cores(self):
        info = {"jdk.JVMInformation": {"jvmArguments": "-XX:ActiveProcessorCount=6"},
                "jdk.CPUInformation": {"hwThreads": 8}}
        self.assertEqual(cores_of(info, None)[0], 6)
        self.assertEqual(cores_of({"jdk.CPUInformation": {"hwThreads": 8}}, None)[0], 8)
        self.assertEqual(cores_of(info, 3), (3, "the cores parameter"))


NMT = textwrap.dedent("""\
    12345:

    Native Memory Tracking:

    (Omitting categories weighting less than 1KB)

    Total: reserved=3540648KB, committed=1136176KB
           malloc: 18208KB #4000, peak=19000KB #4100
           mmap:   reserved=3522440KB, committed=1117968KB

    -                 Java Heap (reserved=2097152KB, committed=1048576KB)
                                (mmap: reserved=2097152KB, committed=1048576KB, at peak)

    -                     Class (reserved=1048600KB, committed=10240KB)
                                (classes #3000)
                                (malloc=1024KB tag=Class #900) (peak=1100KB #910)
                                (mmap: reserved=1047576KB, committed=9216KB, at peak)
                                (  Metadata:   )
                                (    reserved=65536KB, committed=9216KB)

    -                    Thread (reserved=40000KB, committed=3072KB)
                                (threads #20)
                                (stack: reserved=39936KB, committed=2048KB, peak=2048KB)
                                (malloc=1000KB tag=Thread #200) (peak=1100KB #210)
                                (arena=24KB #40) (peak=30KB #41)

    -                      Code (reserved=250000KB, committed=8192KB)
                                (malloc=64KB tag=Code #1500) (peak=64KB #1500)
                                (mmap: reserved=249936KB, committed=8128KB, at peak)

    -                        GC (reserved=100000KB, committed=60000KB)
                                (malloc=10000KB tag=GC #1000) (peak=10000KB #1000)
                                (mmap: reserved=90000KB, committed=50000KB, at peak)

    -                     Other (reserved=4096KB, committed=4096KB)
                                (malloc=4096KB tag=Other #10) (at peak)

    -                    Symbol (reserved=2000KB, committed=2000KB)
                                (malloc=2000KB tag=Symbol #40) (at peak)
    """)
NMT_OLD = textwrap.dedent("""\
    Native Memory Tracking:

    Total: reserved=1460000KB, committed=170000KB
    -                 Java Heap (reserved=262144KB, committed=131072KB)
                                (mmap: reserved=262144KB, committed=131072KB)

    -                    Thread (reserved=17483KB, committed=17483KB)
                                (thread #18)
                                (stack: reserved=17408KB, committed=17408KB)
                                (malloc=56KB #94)
                                (arena=19KB #34)

    -                     Other (reserved=1000KB, committed=1000KB)
                                (malloc=1000KB #3)
    """)
KB, MIB = 1024, 1024 * 1024
LINUX_MAP = textwrap.dedent("""\
    12345:
    Memory mappings:
    rss:             resident set size

    from               to                        vsize prot          rss      hugetlb pgsz notes            info                                  file
    ========================================================================================================================================================================
    0x0000000600000000-0x0000000640000000   1073741824 rw-p    805306368            0 4K   com              JAVAHEAP                              -
    0x0000000640000000-0x0000000680000000   1073741824 ---p            0            0 4K   -                JAVAHEAP                              -
    0x00007f0000000000-0x00007f0000800000      8388608 rwxp      4194304            0 4K   com              CODE                                  -
    0x00007f0001000000-0x00007f0001100000      1048576 rw-p       524288            0 4K   com              META                                  -
    0x00007f0002000000-0x00007f0002100000      1048576 rw-p      1048576            0 4K   com,nothp        STACK-12-Server-thread                -
    0x00007f0003000000-0x00007f0006000000     50331648 rw-p     41943040            0 4K   com              -                                     -
    0x0000560000000000-0x0000560000400000      4194304 rw-p      2097152            0 4K   com              -                                     [heap]
    0x00007f0007000000-0x00007f0007400000      4194304 r-xp      3145728            0 4K   com              -                                     /usr/lib/libGL.so.1
    0x00007f0008000000-0x00007f0008100000      1048576 r--p       262144            0 4K   com              CDS                                   /opt/jdk/lib/server/classes.jsa
    0x00007fff00000000-0x00007fff00021000       135168 rw-p        36864            0 4K   com              -                                     [stack]

    Number of mappings: 10
                 vsize: 2216820736 (2114M)
                   rss: 859607040 (819M)
    """)
IMAGE = "C:\\Program Files\\Java\\bin\\server\\jvm.dll"  # scrub: allow (a system path a Windows map lists)
FONT = "C:\\Windows\\Fonts\\arial.ttf"  # scrub: allow (a system path a Windows map lists)
WINDOWS_MAP = textwrap.dedent(f"""\
    6789:
    Memory mappings:
    from               to                        vsize prot  state      offset vminfo/file
    ===========================================================================================
    0x0000000404800000-0x000000043a000000    897581056 rw-   c-pvt           0 JAVAHEAP
    0x000000043a000000-0x0000000800000000  16200000000 ---   r-pvt           0 JAVAHEAP
    0x0000021910a20000-0x0000021910a25000        20480 rw-   c-pvt           0 CODE
    0x0000021910a10000-0x0000021910a20000        65536 rw-   c-map           0 INTERN
    0x00000061a3d00000-0x00000061a3d04000        16384 rw-g  c-pvt           0 STACK-75688-main
    0x0000021000000000-0x0000021002000000     33554432 rw-   c-pvt           0
    0x0000021003000000-0x0000021003100000      1048576 rw-W  c-pvt           0
    0x00007ffb0a040000-0x00007ffb0a041000         4096 r--   c-img           0 {IMAGE}
    0x00007ffb0a041000-0x00007ffb0a051000        65536 rw-   c-img      0x1000 {IMAGE}
    0x00007ffb0b000000-0x00007ffb0b100000      1048576 r--   c-map           0 {FONT}

    current process WorkingSet (physical memory assigned to process): 900M, peak: 910M
    current process commit charge ("private bytes"): 1000M, peak: 1000M
    """)


def classes(accounting: dict) -> dict:
    return {c["owner"]: c["bytes"] for c in accounting["classes"]}


class Memory(unittest.TestCase):
    def test_nmt(self):
        found = memory.nmt(NMT)
        self.assertEqual((found["committed"], found["malloc"]), (1136176 * KB, 18208 * KB))
        self.assertEqual(found["categories"]["Thread"]["stackCommitted"], 2048 * KB)
        self.assertEqual(found["categories"]["Thread"]["threads"], 20)
        self.assertEqual(found["categories"]["Code"]["malloc"], 64 * KB)
        old = memory.nmt(NMT_OLD)
        self.assertEqual(old["malloc"], (56 + 19 + 1000) * KB)  # no total line: the categories' own
        self.assertEqual(old["categories"]["Thread"]["threads"], 18)
        with self.assertRaises(ValueError):
            memory.nmt("12345:\nNative memory tracking is not enabled\n")
        with self.assertRaises(ValueError):
            memory.nmt("hello")

    def test_nmt_alone(self):
        found = classes(memory.account(memory.nmt(NMT)))
        self.assertEqual((found["heap"], found["code cache"], found["thread stacks"], found["direct buffers"]),
                         (1048576 * KB, 8192 * KB, 2048 * KB, 4096 * KB))
        self.assertEqual(found["JVM internals"], (1136176 - 1048576 - 8192 - 2048 - 4096) * KB)
        self.assertIsNone(found["native libraries and drivers"])

    def test_linux_map(self):
        mapped = memory.system_map(LINUX_MAP)
        self.assertEqual((mapped["style"], len(mapped["mappings"])), ("linux", 10))
        self.assertEqual(mapped["footer"], {"rss": 859607040})
        self.assertEqual(mapped["mappings"][7]["file"], "/usr/lib/libGL.so.1")
        accounting = memory.account(memory.nmt(NMT), mapped)
        found = classes(accounting)
        malloc, other, code_malloc = 18208 * KB, 4096 * KB, 64 * KB
        self.assertEqual(found["heap"], 805306368)
        self.assertEqual(found["code cache"], 4194304 + code_malloc)
        self.assertEqual(found["thread stacks"], 1048576 + 36864)
        self.assertEqual(found["JVM internals"], 524288 + 262144 + malloc - other - code_malloc)
        self.assertEqual(found["native libraries and drivers"], 41943040 + 2097152 - malloc)
        self.assertEqual(found["images and mapped files"], 3145728)
        self.assertEqual(found["unaccounted"], 859607040 - sum(v for k, v in found.items() if k != "unaccounted"))
        self.assertEqual(accounting["basis"], "resident (Linux map)")
        self.assertEqual(accounting["map"]["largestFiles"], {"libGL.so.1": 3145728})

    def test_windows_map(self):
        mapped = memory.system_map(WINDOWS_MAP)
        self.assertEqual(mapped["style"], "windows")
        self.assertEqual(mapped["footer"], {"commitCharge": 1000 * MIB, "workingSet": 900 * MIB})
        accounting = memory.account(memory.nmt(NMT), mapped)
        found = classes(accounting)
        malloc, other, code_malloc = 18208 * KB, 4096 * KB, 64 * KB
        self.assertEqual(found["heap"], 897581056)  # the reserved rest does not count
        self.assertEqual(found["code cache"], 20480 + code_malloc)
        self.assertEqual(found["thread stacks"], 16384)
        self.assertEqual(found["JVM internals"], 65536 + malloc - other - code_malloc)
        self.assertEqual(found["native libraries and drivers"], 33554432 + 1048576 - malloc)
        self.assertEqual(found["images and mapped files"], 65536)  # the writable image pages
        self.assertEqual(found["unaccounted"], 1000 * MIB - sum(v for k, v in found.items() if k != "unaccounted"))
        self.assertEqual(accounting["map"]["writeCombined"], 1048576)
        self.assertIn("heap", memory.summary_line(accounting))
        with self.assertRaises(ValueError):
            memory.system_map("no mappings here")


class Settings(unittest.TestCase):
    def test_jfc(self):
        events = jfc.settings(["demo.app.Loop.tick", "demo.app.World.update"], sample_period=0.01, threshold=0.001,
                              method_threshold=0, allocation_rate="150/s", cpu_period=1, compilation_threshold=0.1,
                              tlab=True)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "p.jfc"
            jfc.write(path, events, "test")
            root = ET.parse(path).getroot()
        found = {e.get("name"): {s.get("name"): s.text for s in e} for e in root}
        self.assertEqual(found["jdk.ExecutionSample"], {"enabled": "true", "period": "10 ms"})
        self.assertEqual(found["jdk.MethodTrace"]["filter"], "demo.app.Loop::tick;demo.app.World::update")
        self.assertEqual(found["jdk.MethodTrace"]["threshold"], "0 ns")
        self.assertEqual(found["jdk.ThreadCPULoad"]["period"], "1 s")
        self.assertEqual(found["jdk.JavaMonitorEnter"], {"enabled": "true", "threshold": "1 ms", "stackTrace": "true"})
        self.assertIn("jdk.ObjectAllocationInNewTLAB", found)
        self.assertNotIn("jdk.MethodTrace", jfc.settings([], sample_period=0.02, threshold=0.01, method_threshold=0,
                                                          allocation_rate="1/s", cpu_period=10,
                                                          compilation_threshold=1, tlab=False))


# -- a real recording -------------------------------------------------------------------------------------
DEMO = textwrap.dedent("""\
    package demo.app;

    import java.util.ArrayList;
    import java.util.List;

    public class Busy {
        static volatile long sink;
        static final Object LOCK = new Object();

        public static void main(String[] args) throws Exception {
            long end = System.nanoTime() + (long) (Double.parseDouble(args[0]) * 1e9);
            Thread worker = new Thread(() -> {
                while (System.nanoTime() < end) { synchronized (LOCK) { spin(20_000); } }
            }, "worker-1");
            worker.start();
            while (System.nanoTime() < end) tick();
            worker.join();
        }

        static void tick() { prepare(); synchronized (LOCK) { spin(30_000); } allocate(); }
        static void prepare() { spin(60_000); }
        static void allocate() {
            List<int[]> list = new ArrayList<>();
            for (int i = 0; i < 64; i++) list.add(new int[256]);
            sink += list.size();
        }
        static void spin(int n) { long s = 0; for (int i = 0; i < n; i++) s += Integer.hashCode(i) * 31L; sink += s; }
    }
    """)
DEMO_STAGES = {"tick": "demo.app.Busy.tick", "stages": {"demo.app.Busy.prepare": "prepare",
                                                       "demo.app.Busy.allocate": "allocate"}}
DEMO_GROUPS = {"demo.app.": "demo"}


def _major(home: Path) -> int:
    release = home / "release"
    if release.is_file():
        for line in release.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("JAVA_VERSION="):
                return int(line.split("=", 1)[1].strip().strip('"').split(".")[0].split("-")[0])
    return 0


def find_jdk(minimum: int = 25) -> Path | None:
    """A JDK of ``minimum`` or later: JAVA_HOME, else one Workbench would find (WB_JDKS, ~/.jdks)."""
    candidates = [Path(os.environ["JAVA_HOME"])] if os.environ.get("JAVA_HOME") else []
    try:
        from wb_core.requirements import find_jdks
        found = find_jdks(types.SimpleNamespace(core={"jdks": {"root": "/opt/jdk"}}))
        candidates += [Path(found[key]) for key in sorted(found, key=lambda key: (key != "26", key))]
    except Exception:  # noqa: BLE001 - no JDK registry: JAVA_HOME alone
        pass
    for home in candidates:
        if java_of(home).exists() and (home / "bin" / ("javac.exe" if os.name == "nt" else "javac")).exists() \
                and _major(home) >= minimum:
            return home
    return None


JDK_HOME = find_jdk()
_RECORDING: dict = {}


def demo_recording() -> Path:
    """Compiles the demo, writes profile.settings' JFC for its stage map, and records 1.5 s of it (once)."""
    if "path" in _RECORDING or "skip" in _RECORDING:
        if "skip" in _RECORDING:
            raise unittest.SkipTest(_RECORDING["skip"])
        return _RECORDING["path"]
    if JDK_HOME is None:
        _RECORDING["skip"] = "no JDK 25+ (JAVA_HOME or one Workbench finds)"
        raise unittest.SkipTest(_RECORDING["skip"])
    folder = Path(tempfile.mkdtemp(prefix="wb-profile-"))
    _RECORDING["folder"] = folder
    source = folder / "src" / "demo" / "app" / "Busy.java"
    source.parent.mkdir(parents=True)
    source.write_text(DEMO, encoding="utf-8")
    javac = JDK_HOME / "bin" / ("javac.exe" if os.name == "nt" else "javac")
    subprocess.run([str(javac), "-d", str(folder / "classes"), str(source)], check=True, capture_output=True)
    settings = folder / "demo.jfc"
    jfc.write(settings, jfc.settings(StageMap.load(DEMO_STAGES).methods(), sample_period=0.01, threshold=0.001,
                                     method_threshold=0, allocation_rate="300/s", cpu_period=0.5,
                                     compilation_threshold=0.1, tlab=False), "test")
    recording = folder / "demo.jfr"
    start = f"-XX:StartFlightRecording=settings={settings},filename={recording}"
    done = subprocess.run([str(java_of(JDK_HOME)), f"-Djava.io.tmpdir={folder}", "-XX:+UnlockDiagnosticVMOptions",
                           "-XX:+DebugNonSafepoints", start, "-cp", str(folder / "classes"), "demo.app.Busy", "1.5"],
                          capture_output=True, text=True, timeout=120)
    if done.returncode != 0 or not recording.is_file() or recording.stat().st_size == 0:
        message = (done.stdout + done.stderr).strip().splitlines()
        if any("Flight Recorder" in line or "repository" in line for line in message):
            cause = next((line for line in message if "[error]" in line), message[-1])
            _RECORDING["skip"] = f"JFR cannot start in this environment ({cause.strip()}); point TEMP elsewhere"
            raise unittest.SkipTest(_RECORDING["skip"])
        raise AssertionError("the demo did not record: " + " | ".join(message[-5:]))
    _RECORDING["path"] = recording
    return recording


def tearDownModule():
    if _RECORDING.get("folder"):
        shutil.rmtree(_RECORDING["folder"], ignore_errors=True)


class RealRecording(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.path = demo_recording()
        cls.span = span(cls.path)

    def _stream(self, types_, windows):
        lo, hi = min(w.start for w in windows), max(w.end for w in windows)
        return Stream(export(java_of(JDK_HOME), self.path, lo, hi, types_))

    def test_profile_of_a_real_recording(self):
        from wb_profile.analysis import EVENTS
        windows = resolve(self.span.start, self.span.end)
        stream = self._stream(EVENTS, windows)
        profile = Profile(stream, windows, Owners(Spec.merge(DEMO_GROUPS)), StageMap.load(DEMO_STAGES), top=10)
        latest = 0
        for event in stream:
            profile.feed(event)
            latest = max(latest, event.start)
        report = profile.report(self.span)
        window = report["windows"][0]
        self.assertLessEqual(self.span.start, stream.totals["first"])  # the chunk headers hold every start
        self.assertLess(latest, self.span.end)
        execution = window["execution"]
        self.assertGreater(execution["samples"], 20)
        self.assertEqual(execution["groups"][0]["name"], "demo")
        self.assertIn("main", {r["name"] for r in execution["threads"]})
        self.assertIn("demo.app.Busy.spin", {r["name"] for r in execution["leafMethods"]})
        self.assertGreater(window["allocation"]["bytes"], 0)
        self.assertIn("main", report["tickThreads"])
        stages = {r["name"] for r in window["stages"]["stages"]}
        self.assertTrue(stages & {"prepare", "other"}, stages)
        ticks = window["stages"]["ticks"]
        self.assertGreater(ticks["count"], 10)  # method tracing (JDK 25+) recorded each tick
        self.assertGreater(ticks["stagesMs"].get("prepare", 0), 0)
        self.assertEqual(notes(profile, report["windows"]), [])  # DebugNonSafepoints was on

    def test_attribution_of_a_real_recording(self):
        from wb_profile.attribution import EVENTS, MARGIN
        windows = resolve(self.span.start, self.span.end)
        lo, hi = windows[0].start - MARGIN, windows[0].end + MARGIN
        stream = Stream(export(java_of(JDK_HOME), self.path, lo, hi, EVENTS))
        charged = Attribution(stream, windows, Owners(Spec.merge({"demo.app.": "Demo"})), top=10)
        for event in stream:
            charged.feed(event)
        report = charged.report(self.span)
        window = report["windows"][0]
        ranking = {r["owner"]: r for r in window["ranking"]}
        self.assertGreater(ranking["Demo"]["samples"], 20)
        self.assertGreater(ranking["Demo"]["cpuSeconds"], 0.2)  # two busy threads for 1.5 s
        self.assertAlmostEqual(sum(r["cpuSeconds"] for r in window["ranking"]), window["cpuSeconds"], places=2)
        self.assertGreater(report["cores"], 0)

    def test_export_selects_by_time(self):
        middle = self.span.start + (self.span.end - self.span.start) // 2
        stream = self._stream(["jdk.ExecutionSample"], [Window("late", middle, self.span.end)])
        starts = [e.start for e in stream if e.type == "jdk.ExecutionSample"]
        self.assertTrue(starts)
        self.assertGreaterEqual(min(starts), middle)
        self.assertGreater(stream.totals["events"], stream.totals["exported"])


# -- GPU telemetry ----------------------------------------------------------------------------------------
SMI_HELP = textwrap.dedent("""\
    List of valid properties to query for the switch "--query-gpu":

    "timestamp"
    The timestamp of when the query was made in format "YYYY/MM/DD HH:MM:SS.msec".

    "kmd_version"
    "driver_version"
    "index"
    "name" or "gpu_name"
    "pstate"
    "clocks_event_reasons.active" or "clocks_throttle_reasons.active"
    "memory.total"
    "memory.used"
    "utilization.gpu"
    "utilization.memory"
    "temperature.gpu"
    "power.draw"
    "enforced.power.limit" or "gpu.power.current_limit"
    "clocks.current.graphics" or "clocks.gr"
    "clocks.current.sm" or "clocks.sm"
    "clocks.current.memory" or "clocks.mem"
    """)
OLD_SMI_HELP = '"timestamp"\n"index"\n"name"\n"driver_version"\n"clocks.gr"\n"clocks.mem"\n"power.draw"\n' \
               '"power.limit"\n"clocks_throttle_reasons.active"\n'
# A saved sampler's CSV with a header and units, a lost-device line and a second header where two files were joined.
SAVED_CSV = textwrap.dedent("""\
    timestamp, pstate, clocks.current.sm [MHz], clocks.current.memory [MHz], power.draw [W], temperature.gpu, \
utilization.gpu [%], memory.used [MiB], power.draw.instant [W]
    2026/11/21 21:16:39.803, P0, 1845 MHz, 7500 MHz, 165.20 W, 70, 98 %, 5120 MiB, 160.00 W
    2026/11/21 21:16:40.803, P0, 1830 MHz, 7500 MHz, 166.00 W, 71, 97 %, 5122 MiB, 161.00 W

    Unable to determine the device handle for GPU0: Unknown Error
    timestamp, pstate, clocks.current.sm [MHz], clocks.current.memory [MHz], power.draw [W], temperature.gpu, \
utilization.gpu [%], memory.used [MiB], power.draw.instant [W]
    2026/11/21 21:16:41.803, P2, [N/A], 7500 MHz, 120.00 W, 71, 60 %, 5122 MiB, N/A
    """)
UTC = timezone.utc


def query_answers(help_text: str | None, refuse=()):
    """A run function answering as nvidia-smi would: its help, refusals, one device."""
    asked = []

    def run(args):
        asked.append(args)
        if "--help-query-gpu" in args:
            return (0, help_text) if help_text is not None else (2, "unknown option")
        fields = args[1].split("=", 1)[1].split(",")
        refused = next((f for f in fields if f in refuse), None)
        if refused:
            return 2, f'Field "{refused}" is not a valid field to query.\n\n'
        values = {"timestamp": "2026/11/21 21:15:05.467", "index": "0", "name": "Example GPU",
                  "driver_version": "999.01", "kmd_version": "999.01"}
        return 0, ", ".join(values.get(f, "1") for f in fields) + "\n"
    return run, asked


def gpu_rows(seconds: float = 20.0, period: float = 0.5, device: int = 0, name: str = "Example GPU",
             idle: float = 2.0, dip=(10.0, 11.0), gap=None, reasons: bool = True) -> list[dict]:
    """Samples of a device that idles at P8, then runs at P0 at 1845 MHz but for a power-capped dip to 1395 MHz;
    it warms by a degree a second."""
    rows, at = [], 0.0
    while at < seconds - 1e-9:
        if not (gap and gap[0] <= at < gap[1]):
            busy, dipping = at >= idle, dip is not None and dip[0] <= at < dip[1]
            row = {"time": gpu.stamp(BASE + round(at * S)), "device": device, "name": name,
                   "pstate": "P0" if busy else "P8", "graphicsClockMhz": (1395 if dipping else 1845) if busy else 210,
                   "memoryClockMhz": 7500 if busy else 405, "powerW": 165.5 if busy else 17.1, "powerLimitW": 170,
                   "temperatureC": 60 + int(at), "memoryUsedMiB": 5120}
            if reasons:
                row["throttleReasons"] = (["power_cap"] if dipping else []) if busy else ["idle"]
            rows.append(row)
        at += period
    return rows


def pairs_of(rows: list[dict]) -> list[tuple[int, dict]]:
    return [gpu.check(row) for row in rows]


def whole(rows: list[dict], expect=None, tolerance: float = 0.0) -> dict:
    """The one device of rows over all of them."""
    devices = telemetry.series(pairs_of(rows))
    start, end = telemetry.bounds(devices)
    return telemetry.analyse(devices, resolve(start, end), expect, tolerance)[0]["devices"][0]


class NvidiaSmi(unittest.TestCase):
    def test_the_plan_takes_the_names_this_binary_lists(self):
        run, asked = query_answers(SMI_HELP)
        found = nvidia.plan("nvidia-smi", run)
        chosen = dict(zip(found.keys, found.names))
        self.assertEqual((chosen["graphicsClockMhz"], chosen["powerLimitW"], chosen["throttleReasons"]),
                         ("clocks.current.graphics", "enforced.power.limit", "clocks_event_reasons.active"))
        self.assertEqual(found.missing, [])
        self.assertEqual(found.devices, [{"device": 0, "name": "Example GPU", "driver": "999.01"}])
        self.assertEqual(asked[-1][1], "--query-gpu=index,kmd_version")
        old = nvidia.plan("nvidia-smi", query_answers(OLD_SMI_HELP)[0])
        self.assertEqual(dict(zip(old.keys, old.names)),
                         {"time": "timestamp", "device": "index", "name": "name", "graphicsClockMhz": "clocks.gr",
                          "memoryClockMhz": "clocks.mem", "powerW": "power.draw", "powerLimitW": "power.limit",
                          "throttleReasons": "clocks_throttle_reasons.active"})
        self.assertIn("pstate", old.missing)
        self.assertEqual(nvidia.command(old, 250, 1)[-3:], ["-lms", "250", "--id=1"])

    def test_without_a_help_refusals_pick_the_names(self):
        refuse = {"clocks.current.graphics", "clocks_event_reasons.active", "power.draw", "power.draw.average",
                  "clocks.current.sm", "clocks.sm"}
        found = nvidia.plan("nvidia-smi", query_answers(None, refuse)[0])
        chosen = dict(zip(found.keys, found.names))
        self.assertEqual((chosen["graphicsClockMhz"], chosen["throttleReasons"], chosen["powerW"]),
                         ("clocks.gr", "clocks_throttle_reasons.active", "power.draw.instant"))
        self.assertEqual(found.missing, ["smClockMhz"])
        with self.assertRaises(LookupError):
            nvidia.plan("nvidia-smi", query_answers(SMI_HELP)[0], device=3)
        with self.assertRaises(RuntimeError):
            nvidia.plan("nvidia-smi", query_answers(SMI_HELP, {"index"})[0])

    def test_a_line_and_its_values(self):
        found = nvidia.Plan("nvidia-smi", keys=["time", "device", "name", "pstate", "graphicsClockMhz", "powerW",
                                                "powerLimitW", "temperatureC", "memoryUsedMiB", "throttleReasons"])
        nanos, sample = nvidia.parse(found, "2026/11/21 21:15:05.467, 0, Example GPU, P2, 1845, 165.27, 170.00, 71, "
                                            "5120, 0x0000000000000005", UTC)
        self.assertEqual(sample, {"time": "2026-11-21T21:15:05.467Z", "device": 0, "name": "Example GPU",
                                  "pstate": "P2", "graphicsClockMhz": 1845, "powerW": 165.27, "powerLimitW": 170,
                                  "temperatureC": 71, "memoryUsedMiB": 5120, "throttleReasons": ["idle", "power_cap"]})
        self.assertEqual(nanos, timestamp("2026-11-21T21:15:05.467Z"))
        _, unknown = nvidia.parse(found, "2026/11/21 21:15:05.500, 1, Other GPU, [N/A], N/A, [Not Supported], "
                                         "170.00, [Unknown Error], 5120, [N/A]", UTC)
        self.assertEqual(set(unknown), {"time", "device", "name", "powerLimitW", "memoryUsedMiB"})  # unknown: absent
        local = nvidia.moment("2026/11/21 21:15:05.467")
        self.assertEqual(local, nvidia.moment("2026/11/21 21:15:05.467", datetime_zone(local)))
        for bad in ("2026/11/21 21:15:05.467, 0", "yesterday, 0, Example GPU, P2, 1845, 1, 1, 1, 1, 0x0"):
            with self.assertRaises(ValueError):
                nvidia.parse(found, bad, UTC)
        self.assertEqual([nvidia.value(t, unit) for t, unit in (("1845 MHz", "MHz"), ("1.8 GHz", "MHz"),
                                                                ("17.10 W", "W"), ("8 %", "%"), ("12 GiB", "MiB"))],
                         [1845, 1800, 17.1, 8, 12288])
        self.assertEqual([nvidia.value(t, unit) for t, unit in (("12 W", "MHz"), ("fast", "W"), ("P20", "pstate"),
                                                                ("", "W"))], [None] * 4)
        self.assertEqual((nvidia.reasons(0), nvidia.reasons(0x44), nvidia.reasons(0x201)),
                         ([], ["power_cap", "hw_thermal"], ["idle", "0x200"]))

    def test_a_saved_csv(self):
        pairs, notes = nvidia.read_csv(SAVED_CSV, zone=timezone(timedelta(hours=1)))
        self.assertEqual(len(pairs), 3)
        self.assertEqual(pairs[0][1], {"time": "2026-11-21T20:16:39.803Z", "device": 0, "pstate": "P0",
                                       "smClockMhz": 1845, "memoryClockMhz": 7500, "powerW": 165.2,
                                       "utilisationPercent": 98, "temperatureC": 70, "memoryUsedMiB": 5120})
        self.assertNotIn("smClockMhz", pairs[2][1])
        self.assertTrue(any(n.startswith("1 line is not a sample") for n in notes), notes)
        self.assertTrue(any("taken as device 0" in n for n in notes), notes)
        self.assertTrue(any("power.draw.instant" in n for n in notes), notes)  # power.draw is preferred
        flags = "2026/11/21 21:16:39.803, 0, Active, Not Active\n2026/11/21 21:16:39.803, 1, Not Active, [N/A]\n"
        columns = ["timestamp", "index", "clocks_throttle_reasons.sw_power_cap", "clocks_throttle_reasons.hw_slowdown"]
        pairs, notes = nvidia.read_csv(flags, columns, UTC)
        self.assertEqual(pairs[0][1]["throttleReasons"], ["power_cap"])
        self.assertNotIn("throttleReasons", pairs[1][1])  # an unknown flag is not an inactive one
        self.assertTrue(any("power_cap, hw_slowdown only" in n for n in notes), notes)
        with self.assertRaises(ValueError):
            nvidia.read_csv(flags)  # no header and no columns
        self.assertTrue(nvidia.recognises(SAVED_CSV))
        self.assertFalse(nvidia.recognises("a, b\n1, 2\n"))

    def test_the_sample_format(self):
        rows = gpu_rows(seconds=2)
        text = "".join(gpu.dumps(r) + "\n" for r in rows) + '{"time": "2026-11-21T21:16:4'  # cut short
        pairs, notes = gpu.read(text)
        self.assertEqual([p[1] for p in pairs], rows)
        self.assertTrue(notes[0].startswith("1 line is not a sample"), notes)
        for bad in ({"time": rows[0]["time"], "device": True}, {"time": rows[0]["time"], "device": 0, "pstate": "P16"},
                    {"time": rows[0]["time"], "device": 0, "powerW": "high"}, {"time": "2026-11-21", "device": 0}):
            with self.assertRaises(ValueError):
                gpu.check(bad)
        with self.assertRaises(ValueError):
            gpu.read("\n\n")
        self.assertEqual(gpu.load(text)[2], gpu.SCHEMA)
        self.assertEqual(gpu.load(SAVED_CSV, (), UTC)[2], "nvidia-smi")
        with self.assertRaises(ValueError):
            gpu.load("hello\n")


def datetime_zone(nanos: int):
    """This machine's zone at an instant, as a fixed offset."""
    from datetime import datetime
    return datetime.fromtimestamp(nanos / 1e9).astimezone().tzinfo


class Telemetry(unittest.TestCase):
    def test_metrics_states_and_episodes(self):
        entry = whole(gpu_rows())
        self.assertEqual((entry["samples"], entry["coveredSeconds"], entry["coverage"], entry["longestGapSeconds"]),
                         (40, 20, 1, 0))
        self.assertEqual(entry["metrics"]["graphicsClockMhz"], {"min": 210, "median": 1845, "max": 1845, "samples": 40})
        self.assertEqual(entry["pstates"], {"P0": {"seconds": 18, "share": 0.9}, "P8": {"seconds": 2, "share": 0.1}})
        throttle = entry["throttle"]
        self.assertEqual(throttle["reasons"], {"idle": {"seconds": 2, "share": 0.1},
                                               "power_cap": {"seconds": 1, "share": 0.05}})
        self.assertEqual((throttle["throttledSeconds"], throttle["episodeCount"]), (1, 1))
        self.assertEqual(throttle["episodes"][0], {"start": iso(BASE + 10 * S), "end": iso(BASE + 11 * S),
                                                   "seconds": 1, "samples": 2, "reasons": ["power_cap"],
                                                   "minClockMhz": 1395})
        line = telemetry.headline(entry)
        for part in ("graphics 1,845 MHz median (210-1,845)", "P0 90%, P8 10%", "of a 170 W limit", "79 °C max",
                     "throttled 1.0 s in 1 episode (power_cap)"):
            self.assertIn(part, line)

    def test_expectations(self):
        expect = {"pstate": "P0", "min_graphics_clock_mhz": 1500, "max_temperature_c": 75, "throttle": False}
        found = {v["expect"]: v for v in whole(gpu_rows(), expect)["expectations"]}
        self.assertEqual((found["pstate"]["seconds"], found["pstate"]["share"], found["pstate"]["worst"]),
                         (2, 0.1, ["P8"]))
        self.assertEqual((found["min_graphics_clock_mhz"]["seconds"], found["min_graphics_clock_mhz"]["samples"],
                          found["min_graphics_clock_mhz"]["worst"]), (3, 6, 210))
        self.assertEqual((found["max_temperature_c"]["seconds"], found["max_temperature_c"]["worst"]), (4, 79))
        self.assertEqual((found["throttle"]["seconds"], found["throttle"]["worst"]), (1, ["power_cap"]))
        self.assertTrue(all(v["finding"] for v in found.values()))
        tolerant = {v["expect"]: v["finding"] for v in whole(gpu_rows(), expect, 0.12)["expectations"]}
        self.assertEqual(tolerant, {"pstate": False, "min_graphics_clock_mhz": True, "max_temperature_c": True,
                                    "throttle": False})
        self.assertEqual(telemetry.describe(found["min_graphics_clock_mhz"]),
                         "graphics clock below 1500 MHz for 3.0 s (15% of the time, lowest 210 MHz)")
        self.assertEqual(telemetry.describe(found["pstate"], 0.05),
                         "in P8 for 2.0 s (10% of the time), expected P0, above the 5% tolerance")
        unknown = whole(gpu_rows(reasons=False), {"throttle": False, "min_sm_clock_mhz": 1000})["expectations"]
        self.assertEqual([telemetry.describe(v) for v in unknown],
                         ["the samples carry no throttle reasons to check throttle = false",
                          "the samples carry no SM clock to check min_sm_clock_mhz = 1000"])
        self.assertEqual(telemetry.expectations({"pstate": ["p2", "P0"], "throttle": True}), {"pstate": ["P0", "P2"]})
        for bad in ({"pstat": "P0"}, {"pstate": "P99"}, {"min_power_w": "high"}, {"throttle": "no"},
                    {"min_power_w": 200, "max_power_w": 100}):
            with self.assertRaises(ValueError):
                telemetry.expectations(bad)

    def test_windows_and_gaps(self):
        devices = telemetry.series(pairs_of(gpu_rows()))
        start, end = telemetry.bounds(devices)
        split = telemetry.analyse(devices, resolve(start, end, split=10), {"pstate": "P0"})
        self.assertEqual([w["name"] for w in split], ["0s-10s", "10s-20s"])
        self.assertEqual(split[1]["devices"][0]["throttle"]["throttledSeconds"], 1)
        self.assertEqual([len(w["devices"][0]["expectations"]) for w in split], [1, 0])
        named = telemetry.analyse(devices, resolve(start, end, [{"name": "warm", "to": "2s"}, {"name": "run",
                                                                                              "from": "2s"}]))
        self.assertEqual([list(w["devices"][0]["pstates"]) for w in named], [["P8"], ["P0"]])
        gapped = whole(gpu_rows(gap=(5.0, 12.0)))
        self.assertEqual((gapped["coveredSeconds"], gapped["longestGapSeconds"]), (13.5, 6.5))  # 4.5 s spans to 5.5
        self.assertEqual(gapped["throttle"]["episodeCount"], 0)  # the dip fell in the gap
        self.assertEqual(whole(gpu_rows(reasons=False))["throttle"]["unknownSeconds"], 20)
        self.assertIn("throttle reasons unknown", telemetry.headline(whole(gpu_rows(reasons=False))))
        two = telemetry.series(pairs_of(gpu_rows() + gpu_rows(device=1, name="Other GPU", idle=0)))
        self.assertEqual([s.name for s in two.values()], ["Example GPU", "Other GPU"])


class FakeSmi:
    """fake_nvidia_smi.py first on PATH as nvidia-smi (a .cmd shim on Windows), with its knobs set."""

    def __init__(self, **knobs):
        self.knobs = {f"FAKE_GPU_{name.upper()}": str(value) for name, value in knobs.items()}

    def __enter__(self) -> "FakeSmi":
        self.folder = Path(tempfile.mkdtemp(prefix="wb-fake-smi-"))
        script = HERE / "fake_nvidia_smi.py"
        if os.name == "nt":
            (self.folder / "nvidia-smi.cmd").write_text(f'@"{sys.executable}" -I "{script}" %*\r\n', encoding="utf-8")
        else:
            shim = self.folder / "nvidia-smi"
            shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" -I "{script}" "$@"\n', encoding="utf-8")
            shim.chmod(0o755)
        self.saved = {name: os.environ.get(name) for name in ("PATH", "FAKE_GPU_REFUSE", "FAKE_GPU_ROUNDS")}
        for name in ("FAKE_GPU_REFUSE", "FAKE_GPU_ROUNDS"):
            os.environ.pop(name, None)
        os.environ.update(self.knobs)
        os.environ["PATH"] = str(self.folder) + os.pathsep + os.environ.get("PATH", "")
        return self

    def __exit__(self, *exc):
        for name, value in self.saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        shutil.rmtree(self.folder, ignore_errors=True)


def gpu_context(test: unittest.TestCase) -> Context:
    """A real handler context outside the daemon; its events are kept in ``ctx.events``."""
    folder = Path(tempfile.mkdtemp(prefix="wb-gpu-"))
    test.addCleanup(shutil.rmtree, folder, ignore_errors=True)
    events: list[dict] = []
    ctx = Context({"job": "direct", "command": "profile.gpu-sample", "module": "profile",
                   "work": str(folder / "work"), "out": str(folder / "out")}, events.append)
    ctx.events = events
    return ctx


def lines_of(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class GpuSample(unittest.TestCase):
    """profile.gpu-sample's handler with a fake nvidia-smi on PATH."""

    def test_samples_for_the_time_asked(self):
        ctx = gpu_context(self)
        with FakeSmi():
            result = profile_commands.gpu_sample(ctx, seconds=1.5, period_ms=100)
        self.assertEqual((ctx.outcome, result["stopped"]), ("ok", "time"), ctx.findings)
        self.assertGreaterEqual(result["samples"], 6)
        self.assertEqual([(d["device"], d["name"], d["driver"]) for d in result["devices"]],
                         [(0, "Example GPU A", "999.01"), (1, "Example GPU B", "999.01")])
        rows = lines_of(ctx.out / "gpu-samples.jsonl")
        self.assertEqual(len(rows), result["samples"])
        self.assertEqual({r["device"] for r in rows}, {0, 1})
        self.assertTrue(all("powerW" not in r for r in rows if r["device"] == 1))  # [N/A]: unknown
        self.assertEqual(rows[0]["pstate"], "P8")
        summary = json.loads((ctx.out / "gpu-summary.json").read_text(encoding="utf-8"))
        self.assertEqual((summary["schema"], summary["stopped"], summary["samples"]["count"]),
                         ("wb.profile.gpu-sample/1", "time", len(rows)))
        self.assertEqual([d["device"] for d in summary["windows"][0]["devices"]], [0, 1])
        self.assertIn("P0", summary["windows"][0]["devices"][0]["pstates"])
        kinds = {e["kind"] for e in ctx.events}
        self.assertTrue({"artifact", "measurement", "machine"} <= kinds, kinds)
        machine = next(e for e in ctx.events if e["kind"] == "machine")
        self.assertEqual((machine["gpu"], machine["driver"]), ("Example GPU A, Example GPU B", "999.01"))
        self.assertIn("samples: Example GPU A (0): graphics", ctx.summary_text)

    def test_one_device_and_a_missing_one(self):
        ctx = gpu_context(self)
        with FakeSmi():
            result = profile_commands.gpu_sample(ctx, seconds=0.8, period_ms=100, device=1)
            self.assertEqual({r["device"] for r in lines_of(ctx.out / "gpu-samples.jsonl")}, {1})
            self.assertEqual([d["device"] for d in result["devices"]], [1])
            with self.assertRaises(ParamError):
                profile_commands.gpu_sample(gpu_context(self), seconds=1, device=5)

    def test_a_cancel_ends_the_sampling_and_keeps_the_samples(self):
        """As the worker cancels: it sets ctx.cancelled, then interrupts the main thread."""
        ctx = gpu_context(self)

        def cancel():
            samples, deadline = ctx.out / "run-samples.jsonl", time.monotonic() + 60
            while time.monotonic() < deadline and not (samples.is_file()
                                                        and samples.read_text(encoding="utf-8").count("\n") >= 4):
                time.sleep(0.05)
            ctx.cancelled.set()
            _thread.interrupt_main()
        threading.Thread(target=cancel, daemon=True).start()
        try:
            with FakeSmi():
                result = profile_commands.gpu_sample(ctx, period_ms=100, label="run")
        except KeyboardInterrupt:
            self.fail("the cancel interrupted the handler instead of ending the sampling")
        self.assertEqual((ctx.outcome, result["stopped"]), ("ok", "cancelled"), ctx.findings)
        self.assertGreater(result["samples"], 0)
        self.assertEqual(len(lines_of(ctx.out / "run-samples.jsonl")), result["samples"])
        self.assertTrue((ctx.out / "run-summary.json").is_file())
        self.assertIn("stopped by a cancel", ctx.summary_text)

    def test_a_tool_that_stops_early_is_a_finding(self):
        ctx = gpu_context(self)
        with FakeSmi(rounds=4):
            result = profile_commands.gpu_sample(ctx, seconds=30, period_ms=100)
        self.assertEqual((ctx.outcome, result["stopped"]), ("finding", "tool exited"))
        self.assertIn("exit 15", ctx.findings[0])
        self.assertIn("GPU is lost", ctx.findings[0])
        self.assertEqual(result["samples"], 8)  # four rounds of two devices, all read

    def test_fields_the_tool_refuses_are_left_out(self):
        ctx = gpu_context(self)
        with FakeSmi(refuse="clocks.current.sm,clocks.sm"):
            result = profile_commands.gpu_sample(ctx, seconds=0.6, period_ms=100)
        self.assertTrue(any("cannot give smClockMhz" in n for n in result["notes"]), result["notes"])
        self.assertTrue(all("smClockMhz" not in r for r in lines_of(ctx.out / "gpu-samples.jsonl")))

    def test_without_the_tool(self):
        empty = Path(tempfile.mkdtemp(prefix="wb-no-tools-"))
        self.addCleanup(shutil.rmtree, empty, ignore_errors=True)
        saved = os.environ["PATH"]
        os.environ["PATH"] = str(empty)
        try:
            with self.assertRaises(EnvironmentProblem) as raised:
                profile_commands.gpu_sample(gpu_context(self), seconds=1)
        finally:
            os.environ["PATH"] = saved
        self.assertIn("nvidia-smi is not on PATH", str(raised.exception))

    def test_the_real_tool(self):
        if REAL_SMI is None:
            self.skipTest("no nvidia-smi on this machine")
        ctx = gpu_context(self)
        try:
            result = profile_commands.gpu_sample(ctx, seconds=1.2, period_ms=200)
        except EnvironmentProblem as error:
            self.skipTest(f"nvidia-smi does not answer here: {error}")
        self.assertGreater(result["samples"], 0)
        self.assertTrue(all(d["name"] for d in result["devices"]), result["devices"])
        rows = lines_of(ctx.out / "gpu-samples.jsonl")
        self.assertTrue(all(gpu.check(r)[1] == r for r in rows))  # the real tool's lines are in the format
        self.assertTrue(any("graphicsClockMhz" in r for r in rows), rows[:1])


# -- through Workbench ----------------------------------------------------------------------------------
class ThroughWorkbench(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from harness import Daemon
        cls._jdks = os.environ.get("WB_JDKS")
        if JDK_HOME is not None and _major(JDK_HOME) == 26:
            os.environ["WB_JDKS"] = json.dumps({**json.loads(cls._jdks or "{}"), "26": str(JDK_HOME)})
        cls.d = Daemon().start()
        cls.c = cls.d.client

    @classmethod
    def tearDownClass(cls):
        cls.d.stop()
        if cls._jdks is None:
            os.environ.pop("WB_JDKS", None)
        else:
            os.environ["WB_JDKS"] = cls._jdks

    def test_memory_from_saved_files(self):
        (self.d.data / "nmt.txt").write_text(NMT, encoding="utf-8")
        (self.d.data / "map.txt").write_text(LINUX_MAP, encoding="utf-8")
        result = self.c.submit("profile.memory", {"nmt": str(self.d.data / "nmt.txt"),
                                                  "system_map": str(self.d.data / "map.txt")},
                               out=str(self.d.out / "memory")).result(timeout=120)
        self.assertEqual(result.outcome, "ok", result.summary)
        self.assertEqual(result.data.classes["heap"], 805306368)
        self.assertEqual(result.data.basis, "resident (Linux map)")
        self.assertTrue((self.d.out / "memory" / "memory.json").is_file())
        self.assertIn("heap 768 MiB", result.summary)
        usage = self.c.submit("profile.memory", {}).result(timeout=60)
        self.assertEqual((usage.outcome, usage.code), ("usage", 2))

    def test_settings(self):
        result = self.c.submit("profile.settings", {"stages": DEMO_STAGES, "methods": ["demo.app.Other::run"]},
                               out=str(self.d.out / "settings")).result(timeout=60)
        self.assertEqual(result.outcome, "ok", result.summary)
        self.assertEqual(result.data.traced[0], "demo.app.Busy.tick")  # stage order is not kept through JSON
        self.assertEqual(set(result.data.traced), {"demo.app.Busy.tick", "demo.app.Busy.prepare",
                                                   "demo.app.Busy.allocate", "demo.app.Other.run"})
        self.assertTrue((self.d.out / "settings" / "profile.jfc").is_file())
        self.assertIn("-XX:+DebugNonSafepoints", result.data.options)

    def test_jfr_and_attribute(self):
        if "26" not in self.d.workbench.checker.jdks:
            self.skipTest("profile.jfr needs JDK 26 and none was found")
        recording = self.d.data / "demo.jfr"
        shutil.copyfile(demo_recording(), recording)
        (self.d.data / "groups.json").write_text(json.dumps(DEMO_GROUPS), encoding="utf-8")
        params = {"recording": str(recording), "groups_file": str(self.d.data / "groups.json"), "split": "0.5s",
                  "stages": DEMO_STAGES}
        result = self.c.submit("profile.jfr", params, out=str(self.d.out / "jfr")).result(timeout=300)
        self.assertEqual(result.outcome, "ok", result.summary)
        self.assertGreater(result.data.samples, 20)
        self.assertEqual(result.data.groups[0]["name"], "demo")
        report = json.loads((self.d.out / "jfr" / "report.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(report["windows"]), 3)
        trace = json.loads((self.d.out / "jfr" / "trace.json").read_text(encoding="utf-8"))
        self.assertTrue(any(e.get("cat") == "window" for e in trace["traceEvents"]))
        self.assertIn("execution_samples", {m["name"] for m in result["measurements"]})
        again = self.c.submit("profile.jfr", params).result(timeout=300)
        self.assertTrue(again["cache"]["hit"])
        charged = self.c.submit("profile.attribute", {"recording": str(recording), "owners": {"demo.app.": "Demo"},
                                                      "threads": ["main"]},
                                out=str(self.d.out / "attribute")).result(timeout=300)
        self.assertEqual(charged.outcome, "ok", charged.summary)
        self.assertEqual(charged.data.ranking["recording"][0]["owner"], "Demo")
        self.assertTrue((self.d.out / "attribute" / "attribution.md").is_file())
        bad = self.c.submit("profile.jfr", {"recording": str(self.d.data / "groups.json")}).result(timeout=60)
        self.assertEqual(bad.outcome, "usage", bad.summary)

    def test_gpu_report(self):
        samples = self.d.data / "gpu-samples.jsonl"
        samples.write_text("".join(gpu.dumps(r) + "\n" for r in gpu_rows()), encoding="utf-8")
        (self.d.data / "gpu-windows.json").write_text(json.dumps([{"name": "warm", "to": "2s"},
                                                                  {"name": "run", "from": "2s"}]), encoding="utf-8")
        params = {"samples": str(samples), "windows": str(self.d.data / "gpu-windows.json"),
                  "expect": {"pstate": "P0", "min_graphics_clock_mhz": 1500}, "tolerance": 0.04}
        broken = self.c.submit("profile.gpu-report", params, out=str(self.d.out / "gpu")).result(timeout=120)
        self.assertEqual(broken.outcome, "finding", broken.summary)
        self.assertEqual(broken.data.violations, 3)  # warm: P8 and slow; run: the dip is 6% of it
        self.assertIn("window run: graphics clock below 1500 MHz for 1.0 s", " | ".join(broken["findings"]))
        report = json.loads((self.d.out / "gpu" / "gpu-report.json").read_text(encoding="utf-8"))
        self.assertEqual((report["schema"], [w["name"] for w in report["windows"]]),
                         ("wb.profile.gpu-report/1", ["warm", "run"]))
        self.assertIn("gpu_graphics_clock_mhz", {m["name"] for m in broken["measurements"]})
        steady = {"samples": str(samples), "skip": "2s", "expect": {"pstate": "P0"}}
        kept = self.c.submit("profile.gpu-report", steady).result(timeout=120)
        self.assertEqual(kept.outcome, "ok", kept.summary)
        self.assertIn("P0 100%", kept.summary)
        self.assertTrue(self.c.submit("profile.gpu-report", steady).result(timeout=120)["cache"]["hit"])
        unknown = self.c.submit("profile.gpu-report", {"samples": str(samples), "expect": {"pstat": "P0"}})
        self.assertEqual(unknown.result(timeout=60).outcome, "usage")
        saved = self.d.data / "saved.csv"
        saved.write_text(SAVED_CSV, encoding="utf-8")
        (self.d.data / "timed.json").write_text(json.dumps([{"name": "a", "start": "2026-11-21T20:16:40Z"}]),
                                                encoding="utf-8")
        placed = {"samples": str(saved), "windows": str(self.d.data / "timed.json")}
        self.assertEqual(self.c.submit("profile.gpu-report", placed).result(timeout=60).outcome, "usage")  # no zone
        zoned = self.c.submit("profile.gpu-report", {**placed, "utc_offset": "+01:00"}).result(timeout=120)
        self.assertEqual(zoned.outcome, "ok", zoned.summary)
        self.assertEqual(zoned.data.samples, 3)


class GpuSampleOnTheHost(unittest.TestCase):
    """profile.gpu-sample through wb-host, with the fake nvidia-smi on PATH: it takes no GPU lease, so it samples
    while another lane holds the lease, and a cancel through Workbench ends the sampling with the samples kept."""

    @classmethod
    def setUpClass(cls):
        from harness import Daemon
        from wb_core.hostclient import HostClient
        from wb_host.runner import HostServer, Runner
        cls.fake = FakeSmi().__enter__()  # the host runner's workers inherit PATH
        cls.lab = Path(tempfile.mkdtemp(prefix="wb-gpu-lab-"))
        cls.lock = cls.lab / "lease" / "gpu.lock"
        config = f"""
            [runner]
            lab_home = '{cls.lab}'
            lease_file = '{cls.lock}'
            coordination_dir = '{cls.lab / "coordination"}'
            """
        probe = Daemon(config)  # only to resolve settings and modules the same way
        cls.runner = Runner(probe.settings, probe.modules)
        probe.hub.close()
        shutil.rmtree(probe.root, ignore_errors=True)
        cls.server = HostServer(cls.runner, "host-token-for-tests", 0)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.d = Daemon(config, files={"core/host.toml": "enabled = true\n"},
                       host_client=HostClient(url, "host-token-for-tests")).start()
        cls.c = cls.d.client

    @classmethod
    def tearDownClass(cls):
        cls.d.stop()
        cls.server.shutdown()
        cls.server.server_close()
        cls.fake.__exit__(None, None, None)
        shutil.rmtree(cls.lab, ignore_errors=True)

    def test_it_samples_while_another_lane_holds_the_lease(self):
        from wb_host import lease
        other = lease.Lease(self.lock, {"pid": os.getpid(), "name": "someone", "lane": "another-lane"})
        try:
            out = self.d.out / "beside"
            result = self.c.submit("profile.gpu-sample", {"seconds": "1s", "period_ms": 100},
                                   out=str(out)).result(timeout=120)
            self.assertEqual(result.outcome, "ok", result.summary)
            self.assertEqual(result.data.stopped, "time")
            self.assertGreater(result.data.samples, 0)
            self.assertEqual(result["machine"]["gpu"], "Example GPU A, Example GPU B")
            self.assertEqual(lease.owner(self.lock)["lane"], "another-lane")  # never taken from it
            self.assertFalse((self.lab / "coordination" / "workbench.json").exists())  # never queued for it
            self.assertEqual(len(lines_of(out / "gpu-samples.jsonl")), result.data.samples)
        finally:
            other.close()

    def test_a_cancel_ends_the_sampling_and_keeps_the_samples(self):
        out = self.d.out / "until-cancelled"
        job = self.c.submit("profile.gpu-sample", {"period_ms": 100, "label": "run"}, out=str(out))
        samples, deadline = out / "run-samples.jsonl", time.monotonic() + 90
        while time.monotonic() < deadline and not (samples.is_file()
                                                    and samples.read_text(encoding="utf-8").count("\n") >= 4):
            time.sleep(0.2)
        job.cancel()
        result = job.result(timeout=90)
        self.assertEqual((result.state, result.outcome), ("settled", "ok"), result.summary)
        self.assertEqual(result.data.stopped, "cancelled")
        self.assertIn("stopped by a cancel", result.summary)
        self.assertEqual(len(lines_of(samples)), result.data.samples)
        self.assertTrue((out / "run-summary.json").is_file())


if __name__ == "__main__":
    unittest.main()
