"""profile: the parsers on small synthetic inputs, a real JFR recording made here (skipped without a JDK 25+),
and the commands through an in-process Workbench."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import types
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
for extra in (REPO / "tests", REPO / "core", HERE.parent):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from wb_profile import jfc, memory  # noqa: E402
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


if __name__ == "__main__":
    unittest.main()
