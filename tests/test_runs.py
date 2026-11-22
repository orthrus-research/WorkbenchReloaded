"""runs: a clean tree at a commit (source never written), repeats and interleaved A/B, including children that
need a quiet machine while their parent waits on them; evidence records of runs, and the precondition that refuses a
comparison of runs differing in more than their treatment."""

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import REPO, Daemon  # noqa: E402

sys.path.insert(0, str(REPO / "modules" / "runs"))
from wb_runs import evidence  # noqa: E402

BASE = {"command": "lab.play", "command.version": "0.1.0", "params.flags": "-Xmx2g", "params.seed": 7,
        "toolchain.jdk": "26.0.1", "machine.gpu": "card", "world.save": "sha256:" + "0" * 64}


def run(job: str, changes: dict | None = None, drop=(), **extra) -> dict:
    """A synthetic record: BASE with ``changes``, less the fields in ``drop``."""
    fields = {k: v for k, v in {**BASE, **(changes or {})}.items() if k not in drop}
    return {"schema": evidence.SCHEMA, "job": job, "outcome": "ok", "cache_from": None, "fields": fields, **extra}


def kinds(table: dict) -> evidence.Kinds:
    return evidence.Kinds(evidence.kind_entries(table))


def refused_fields(report: dict) -> list[str]:
    return [d["field"] for d in report["differences"] if d["refused"]]


class Precondition(unittest.TestCase):
    """compare on synthetic records: what differing in the treatment alone means."""

    def test_only_the_treatment_differs(self):
        a = [run("j_a1"), run("j_a2")]
        b = [run("j_b1", {"params.flags": "-Xmx4g"}), run("j_b2", {"params.flags": "-Xmx4g"})]
        report = evidence.compare(a, b, ["params.flags"])
        self.assertFalse(report["refused"], report["refusals"])
        self.assertEqual(report["treatment"], {"params.flags": {"a": "-Xmx2g", "b": "-Xmx4g"}})
        self.assertEqual(report["controls"], len(BASE) - 1)
        self.assertEqual([(d["field"], d["role"]) for d in report["differences"]], [("params.flags", "treatment")])
        self.assertEqual(report["runs"], {"A1": "j_a1", "A2": "j_a2", "B1": "j_b1", "B2": "j_b2"})

    def test_every_differing_control_refuses_and_is_reported(self):
        a = [run("j_a1"), run("j_a2")]
        b = [run("j_b1", {"params.flags": "-Xmx4g", "toolchain.jdk": "26.0.2"}),
             run("j_b2", {"params.flags": "-Xmx4g", "machine.gpu": "other"})]
        report = evidence.compare(a, b, ["params.flags"])
        self.assertTrue(report["refused"])
        self.assertEqual(refused_fields(report), ["machine.gpu", "toolchain.jdk"])
        self.assertEqual(report["refusals"], ['machine.gpu differs: "card" on A1-A2, B1; "other" on B2',
                                              'toolchain.jdk differs: "26.0.1" on A1-A2, B2; "26.0.2" on B1'])
        jdk = next(d for d in report["differences"] if d["field"] == "toolchain.jdk")
        self.assertEqual(jdk["values"], [{"value": "26.0.1", "runs": ["A1", "A2", "B2"]},
                                         {"value": "26.0.2", "runs": ["B1"]}])

    def test_the_treatment_differs_with_one_value_per_side(self):
        same = evidence.compare([run("j_a1")], [run("j_b1")], ["params.flags"])
        self.assertEqual(same["refusals"], ['treatment params.flags is the same on both sides: "-Xmx2g"'])
        varies = evidence.compare([run("j_a1"), run("j_a2", {"params.flags": "-Xmx3g"})],
                                  [run("j_b1", {"params.flags": "-Xmx4g"})], ["params.flags"])
        self.assertEqual(varies["refusals"],
                         ['treatment params.flags varies within A: "-Xmx2g" on A1; "-Xmx3g" on A2'])
        typo = evidence.compare([run("j_a1")], [run("j_b1", {"params.flags": "-Xmx4g"})], ["params.flag"])
        self.assertEqual(typo["refusals"], ["treatment params.flag names no field of these runs",
                                            'params.flags differs: "-Xmx2g" on A1; "-Xmx4g" on B1'])

    def test_a_treatment_glob_needs_one_of_its_fields_to_differ(self):
        a = [run("j_a1", {"inputs.mod_a": "sha256:1", "inputs.mod_b": "sha256:2"})]
        moved = evidence.compare(a, [run("j_b1", {"inputs.mod_a": "sha256:1", "inputs.mod_b": "sha256:3"})],
                                 ["inputs.mod_*"])
        self.assertFalse(moved["refused"], moved["refusals"])
        self.assertEqual(moved["treatment"], {"inputs.mod_b": {"a": "sha256:2", "b": "sha256:3"}})
        unmoved = evidence.compare(a, [run("j_b1", {"inputs.mod_a": "sha256:1", "inputs.mod_b": "sha256:2"})],
                                   ["inputs.mod_*"])
        self.assertEqual(unmoved["refusals"], ["treatment inputs.mod_* is the same on both sides: none of "
                                               "inputs.mod_a, inputs.mod_b differs"])

    def test_a_field_one_run_lacks_differs(self):
        report = evidence.compare([run("j_a1")], [run("j_b1", {"params.flags": "-Xmx4g"}, drop=["params.seed"])],
                                  ["params.flags"])
        self.assertEqual(report["refusals"], ["params.seed differs: 7 on A1; absent on B1"])
        nulls = evidence.compare([run("j_a1", {"params.gc": None})], [run("j_b1", {"params.flags": "-Xmx4g"})],
                                 ["params.flags"])
        self.assertEqual(nulls["refusals"], ["params.gc differs: null on A1; absent on B1"])
        added = evidence.compare([run("j_a1")], [run("j_b1", {"params.gc": "zgc"})], ["params.gc"])
        self.assertFalse(added["refused"], added["refusals"])
        self.assertEqual(added["treatment"], {"params.gc": {"b": "zgc"}})

    def test_exact_is_the_same_json_value(self):
        same = evidence.EXACT.same
        self.assertTrue(same(12, 12.0))
        self.assertTrue(same({"b": [1, 2.0], "a": None}, {"a": None, "b": [1.0, 2]}))
        self.assertFalse(same(True, 1))
        self.assertFalse(same("2", 2))
        self.assertFalse(same([1, 2], [2, 1]))

    def test_controls_and_kinds_choose_what_is_compared(self):
        a = [run("j_a1", {"toolchain.workbench": "1.0", "data.heap_mb": 1000, "data.warmup_s": 10.0})]
        b = [run("j_b1", {"params.flags": "-Xmx4g", "toolchain.workbench": "1.1", "data.heap_mb": 1040,
                          "data.warmup_s": 10.4})]
        loose = kinds({"toolchain.*": "ignored", "data": {"heap_mb": {"tolerance": 50}},
                       "data.warmup_s": {"tolerance": "5%"}})
        report = evidence.compare(a, b, ["params.flags"], kinds=loose)
        self.assertFalse(report["refused"], report["refusals"])
        self.assertEqual({d["field"]: (d["role"], d["kind"]) for d in report["differences"]},
                         {"params.flags": ("treatment", "exact"), "toolchain.workbench": ("ignored", "ignored"),
                          "data.heap_mb": ("control", "within 50"), "data.warmup_s": ("control", "within 5%")})
        tight = kinds({"toolchain.*": "ignored", "data.heap_mb": {"tolerance": 10}, "data.warmup_s": {"tolerance": "1%"}})
        self.assertEqual(refused_fields(evidence.compare(a, b, ["params.flags"], kinds=tight)),
                         ["data.heap_mb", "data.warmup_s"])
        # The last entry matching a field wins: one field taken back from an ignored glob.
        back = kinds({"toolchain.*": "ignored", "toolchain.workbench": "exact", "data.*": "ignored"})
        self.assertEqual(refused_fields(evidence.compare(a, b, ["params.flags"], kinds=back)), ["toolchain.workbench"])
        # Controls name the fields that must match; fields !glob takes out are reported, never refused.
        free = evidence.compare(a, b, ["params.flags"], control=["*", "!data.*", "!toolchain.*"])
        self.assertFalse(free["refused"], free["refusals"])
        self.assertEqual({d["field"]: d["role"] for d in free["differences"]},
                         {"data.heap_mb": "uncontrolled", "data.warmup_s": "uncontrolled",
                          "params.flags": "treatment", "toolchain.workbench": "uncontrolled"})

    def test_a_run_counts_once(self):
        twice = evidence.compare([run("j_a1")], [run("j_a1", {"params.flags": "-Xmx4g"})], ["params.flags"])
        self.assertEqual(twice["refusals"], ["B1 is the run A1 already counts (j_a1): a run counts once"])
        cached = evidence.compare([run("j_a1"), run("j_a2", cache_from="j_a1")],
                                  [run("j_b1", {"params.flags": "-Xmx4g"})], ["params.flags"])
        self.assertEqual(cached["refusals"], ["A2 is the run A1 already counts (j_a1): a run counts once"])

    def test_kinds_that_cannot_be_read(self):
        for table in ({"x": "fuzzy"}, {"x": {"tolerance": -1}}, {"x": {"tolerance": "five%"}},
                      {"x": {"tolerance": 1, "unit": "ms"}}, {"x": True}):
            with self.subTest(table=table), self.assertRaises(evidence.EvidenceError):
                evidence.kind_entries(table)
        self.assertEqual(evidence.kind_entries({"machine": {"gpu": "ignored", "cores": {"tolerance": "10%"}}}),
                         [("machine.gpu", evidence.IGNORED), ("machine.cores", evidence.Kind("tolerance", 10.0, True))])


ENVELOPE = {
    "job": "j_1", "state": "settled", "outcome": "ok", "command": "lab.play", "command_version": "0.1.0",
    "module_version": "0.1.0", "params": {"times": "3", "jar": "/data/a.jar", "out": "/out/x", "opts": {"gc": "zgc"}},
    "inputs": [{"name": "jar", "host_path": "/data/a.jar", "sha256": "ab" * 32, "files": 1}],
    "toolchain": {"python": "3.14.6", "jdk": "26.0.1"}, "machine": {"id": "host", "gpu": "card"},
    "data": {"exit": 0, "jvm": {"flags": ["-Xmx2g"], "gc": "zgc"}},
    "measurements": [{"name": "fps", "value": 60.0, "unit": None, "labels": {}}],
    "outputs": [{"name": "log", "host_path": "/out/x/log.txt", "sha256": "cd" * 32, "bytes": 10}],
    "timings": {"started": "2026-11-21T10:00:00.000Z", "settled": "2026-11-21T10:05:00.000Z"},
    "cache": {"hit": False, "from": None},
}
PARAMS = {"properties": {
    "times": {"type": "integer", "default": 1}, "seconds": {"type": "number", "default": 2},
    "window": {"oneOf": [{"type": "number"}, {"type": "string"}], "format": "duration", "default": "5m"},
    "jar": {"type": "string", "format": "path.in"}, "out": {"type": "string", "format": "path.out"},
    "opts": {"type": "object", "default": {}}, "label": {"type": "string"}}}


class Records(unittest.TestCase):
    """build and the records on disk."""

    def test_a_record_holds_the_facts_that_define_a_run(self):
        record = evidence.build(ENVELOPE, PARAMS, declared={"world": {"seed": 7}, "distance.render": 12},
                                files={"client.options": {"sha256": "ef" * 32}}, data=["jvm.*"])
        fields = record["fields"]
        self.assertEqual(fields["params.times"], 3)  # the CLI's text, as the command reads it
        self.assertEqual((fields["params.seconds"], fields["params.window"]), (2.0, 300.0))  # defaults it ran with
        self.assertEqual(fields["params.opts.gc"], "zgc")
        self.assertNotIn("params.label", fields)  # optional, and not given
        self.assertEqual(fields["inputs.jar"], "sha256:" + "ab" * 32)
        self.assertFalse({"params.jar", "params.out"} & set(fields))  # a path is where an input was, not what it held
        self.assertEqual(record["paths"], {"jar": "/data/a.jar", "out": "/out/x"})
        self.assertEqual((fields["toolchain.jdk"], fields["machine.gpu"]), ("26.0.1", "card"))
        self.assertEqual((fields["data.jvm.flags"], fields["data.jvm.gc"]), (["-Xmx2g"], "zgc"))
        self.assertNotIn("data.exit", fields)
        self.assertEqual((fields["world.seed"], fields["distance.render"]), (7, 12))
        self.assertEqual(fields["client.options"], "sha256:" + "ef" * 32)
        self.assertEqual(record["declared"], ["client.options", "distance.render", "world.seed"])
        self.assertEqual(list(fields), sorted(fields))
        self.assertEqual((record["measurements"][0]["name"], record["outputs"][0]["name"]), ("fps", "log"))
        self.assertTrue(record["defaults"])
        bare = evidence.build(ENVELOPE)  # without the command's schema: params as submitted
        self.assertEqual(bare["fields"]["params.times"], "3")
        self.assertNotIn("params.seconds", bare["fields"])
        self.assertNotIn("params.jar", bare["fields"])  # still known as an input
        self.assertFalse(bare["defaults"])

    def test_what_a_record_refuses(self):
        for envelope, why in ((dict(ENVELOPE, state="running"), "has not settled"),
                              (dict(ENVELOPE, outcome="environment"), "measured nothing")):
            with self.subTest(why=why), self.assertRaisesRegex(evidence.EvidenceError, why):
                evidence.build(envelope, PARAMS)
        for declared, files, data, why in (({"machine.gpu": "other"}, {}, (), "records already"),
                                           ({"jvm flags": "x"}, {}, (), "use letters"),
                                           ({"options": 1}, {"options": {"sha256": "ef" * 32}}, (), "more than once"),
                                           ({"jvm.flags": 1, "jvm": {"flags": 2}}, {}, (), "more than once"),
                                           ({}, {}, ["fps"], "names no field")):
            with self.subTest(why=why), self.assertRaisesRegex(evidence.EvidenceError, why):
                evidence.build(ENVELOPE, PARAMS, declared=declared, files=files, data=data)
        self.assertEqual(evidence.build(dict(ENVELOPE, cache={"hit": True, "from": "j_0"}))["cache_from"], "j_0")
        with self.assertRaisesRegex(evidence.EvidenceError, "measured nothing"):
            evidence.check_record(dict(run("j_x"), outcome="usage"), "x.json")
        with self.assertRaisesRegex(evidence.EvidenceError, "not an evidence record"):
            evidence.check_record({"job": "j_x"}, "x.json")

    def test_declared_files_are_digested_and_kept_up_to_keep_mb(self):
        from wb_core.module import Context, ParamError, sha256_tree
        from wb_runs import commands
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            world = root / "world"
            (world / "region").mkdir(parents=True)
            (world / "level.dat").write_bytes(b"level")
            (world / "region" / "r.0.0.mca").write_bytes(b"chunks")
            options = root / "options.txt"
            options.write_bytes(b"fov:70\n")

            def recorder(keep_mb: float, files: dict):
                ctx = Context({"job": "direct", "command": "runs.ab", "module": "runs", "work": str(root / "work"),
                               "out": str(root / "out"), "module_config": {"keep_mb": keep_mb}}, lambda event: None)
                return commands._Recorder(ctx, {}, files, [])

            declared = {"world.save": str(world), "client.options": str(options)}
            kept = recorder(64, declared).snapshot(root / "run" / "1")
            self.assertEqual(kept["world.save"]["sha256"], sha256_tree(world)[0])
            self.assertEqual((kept["world.save"]["files"], kept["world.save"]["bytes"]), (2, 11))
            copy = root / "run" / "1" / kept["world.save"]["kept"]
            self.assertEqual((copy / "region" / "r.0.0.mca").read_bytes(), b"chunks")
            self.assertEqual(kept["client.options"]["kept"], "files/client.options/options.txt")
            digested = recorder(0.000001, declared).snapshot(root / "run" / "2")  # one byte: too small for either
            self.assertEqual([digested[name]["kept"] for name in declared], [None, None])
            self.assertEqual(digested["client.options"]["sha256"], kept["client.options"]["sha256"])
            with self.assertRaisesRegex(ParamError, "holds the record itself"):
                recorder(64, declared).snapshot(world / "records" / "1")
            with self.assertRaisesRegex(ParamError, "does not exist"):
                recorder(64, {"gone": str(root / "none")})

    def test_records_are_found_in_natural_order(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for number in (10, 2, 1):
                evidence.write(run(f"j_{number}"), root / str(number))
            self.assertEqual([p.parent.name for p in evidence.find(root)], ["1", "2", "10"])
            self.assertEqual(evidence.find(root / "2"), [root / "2" / evidence.RECORD])
            self.assertEqual(evidence.find(root / "2" / evidence.RECORD), [root / "2" / evidence.RECORD])
            self.assertEqual(json.loads((root / "10" / evidence.RECORD).read_text(encoding="utf-8"))["job"], "j_10")


class Interleaved(unittest.TestCase):
    """Treatments interleaved by a hash of the sample's index and paired window by window."""

    @staticmethod
    def session(frames: int, cost: dict, assign=None) -> list[dict]:
        """A session whose workload alternates cheap and dear stretches every 100 frames, under conditions that
        add a fixed cost; the stretches would mislead a comparison by fixed turns."""
        from wb_runs import interleaved
        conditions = list(cost)
        rows = []
        for frame in range(frames):
            label = assign(frame) if assign else interleaved.assign(frame, conditions, seed=7)
            workload = 2.0 if (frame // 100) % 2 else 1.0
            rows.append({"frame": frame, "condition": label, "ms": workload + cost[label]})
        return rows

    def test_the_hashed_assignment_is_stable_and_balanced(self):
        from wb_runs import interleaved
        labels = [interleaved.assign(i, ["a", "b"], seed=7) for i in range(10000)]
        self.assertEqual(labels[:5], [interleaved.assign(i, ["a", "b"], seed=7) for i in range(5)])
        self.assertAlmostEqual(labels.count("a") / len(labels), 0.5, delta=0.02)
        self.assertEqual(interleaved.splitmix64(0), 0xE220A8397B1DCDAF)  # the standard finaliser's first value

    def test_pairing_finds_the_treatment_cost_where_fixed_turns_would_not(self):
        from wb_runs import interleaved
        rows = self.session(6000, {"whole": 0.0, "culled": 0.25})
        # A window holds one stretch of the workload, as a window of a tour's dwell does.
        report = interleaved.analyse(rows, index="frame", condition="condition", metrics=["ms"], baseline="whole",
                                     treatments=["culled"], size=100, min_share=0.3)
        result = report["metrics"]["ms"]["culled"]
        self.assertAlmostEqual(result["medianDifference"], 0.25, places=6)
        self.assertEqual(result["treatmentHigher"], result["windows"])
        turns = self.session(6000, {"whole": 0.0, "culled": 0.25},
                             assign=lambda f: "culled" if (f // 100) % 2 else "whole")  # turns locked to the workload
        locked = interleaved.analyse(turns, index="frame", condition="condition", metrics=["ms"], baseline="whole",
                                     treatments=["culled"], size=100, min_share=0.3)
        self.assertEqual(locked["metrics"]["ms"], {})  # every window is one-sided: nothing can be paired
        self.assertEqual(len(locked["unbalanced"]), 60)

    def test_the_command_reports_through_the_service(self):
        d = Daemon().start()
        try:
            rows = self.session(3000, {"off": 0.0, "on": -0.1})
            path = d.data / "samples.jsonl"
            path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
            result = d.client.submit("runs.interleaved", {"samples": str(path), "baseline": "off", "treatments": ["on"],
                                                          "metrics": ["ms"], "window": 300}).result(timeout=120)
            self.assertEqual(result.outcome, "ok", result.summary)
            self.assertAlmostEqual(result.data.metrics["ms"]["on"]["medianDifference"], -0.1, places=6)
            self.assertIn("lower in 10 of 10 windows", result.summary)
        finally:
            d.stop()


class Runs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = Daemon().start()
        cls.c = cls.d.client
        repo = cls.d.data / "repo"
        repo.mkdir()
        for args in (["init", "-q", "-b", "main"], ["config", "user.email", "t@example.invalid"],
                     ["config", "user.name", "t"]):
            subprocess.run(["git", "-C", str(repo), *args], check=True)
        (repo / "a.txt").write_text("one\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "one"], check=True)
        cls.first = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        (repo / "a.txt").write_text("two\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-am", "two"], check=True)
        cls.repo = repo

    @classmethod
    def tearDownClass(cls):
        cls.d.stop()

    def echo(self, text: str):
        result = self.c.submit("selftest.echo", {"text": text}, fresh=True).result(timeout=60)
        self.assertEqual(result.outcome, "ok", result.summary)
        return result

    def test_at_commit_runs_in_a_clean_tree(self):
        step = f'"{sys.executable}" -c "print(open(\'a.txt\').read().strip())"'
        result = self.c.submit("runs.at-commit", {"repo": str(self.repo), "commit": self.first,
                                                  "steps": [step]}).result(timeout=120)
        self.assertEqual(result.outcome, "ok", result.summary)
        self.assertEqual(result.data.commit, self.first)
        log = next(o for o in result["outputs"] if o["name"] == "step-1")
        self.assertEqual(Path(log["host_path"]).read_text(encoding="utf-8").strip().splitlines()[-1], "one")
        self.assertEqual((self.repo / "a.txt").read_text(encoding="utf-8"), "two\n")  # source untouched
        failing = self.c.submit("runs.at-commit", {"repo": str(self.repo), "steps": [
            f'"{sys.executable}" -c "raise SystemExit(3)"', "never-runs"]}).result(timeout=120)
        self.assertEqual(failing.outcome, "finding")
        self.assertEqual(len(failing.data.steps), 1)

    def test_repeat_with_quiet_children(self):
        result = self.c.submit("runs.repeat", {"command": "selftest.quiet-echo", "params": {"text": "ab"},
                                               "times": 3, "warmup": 1, "metric": "length"}).result(timeout=180)
        self.assertEqual(result.outcome, "ok", result.summary)
        self.assertEqual(result.data["values"], [2.0, 2.0, 2.0])
        self.assertEqual(len(result["children"]), 4)
        child = self.c.job(result["children"][0]).refresh()
        self.assertEqual(child["parent"], result["job"])
        records = Path(result.data.evidence["records"])  # one for each measured run, none for the warm-up
        self.assertEqual([p.parent.name for p in evidence.find(records)], ["1", "2", "3"])
        self.assertEqual([json.loads(p.read_text(encoding="utf-8"))["job"] for p in evidence.find(records)],
                         result.data.jobs)

    def test_ab_paired_change(self):
        result = self.c.submit("runs.ab", {"command": "selftest.echo", "a": {"text": "x", "times": 2},
                                           "b": {"text": "x", "times": 3}, "pairs": 3, "metric": "length",
                                           "fail_above": 20}).result(timeout=240)
        self.assertEqual(result.outcome, "finding", result.summary)  # +50% is above the 20% gate
        self.assertAlmostEqual(result.data.median_percent_change, 50.0)
        self.assertFalse(result.data.evidence["precondition"])  # no treatment declared

    def test_evidence_records_a_finished_run(self):
        echo = self.echo("ab")
        options = self.d.data / "options.txt"
        options.write_bytes(b"renderDistance:12\n")
        out = self.d.out / "evidence-echo"
        result = self.c.submit("runs.evidence", {
            "job": echo["job"], "fields": {"jvm": {"flags": "-Xmx2g"}, "distance.render": 12},
            "files": {"client.options": str(options)}, "data": ["text"]}, out=str(out)).result(timeout=60)
        self.assertEqual(result.outcome, "ok", result.summary)
        record = json.loads((out / "evidence.json").read_text(encoding="utf-8"))
        self.assertEqual(record["job"], echo["job"])
        fields = record["fields"]
        self.assertEqual((fields["command"], fields["params.text"], fields["params.times"]), ("selftest.echo", "ab", 1))
        self.assertEqual((fields["jvm.flags"], fields["distance.render"], fields["data.text"]), ("-Xmx2g", 12, "ab"))
        self.assertEqual(fields["machine.id"], "container")
        self.assertIn("toolchain.python", fields)
        self.assertEqual(fields["client.options"], "sha256:" + hashlib.sha256(b"renderDistance:12\n").hexdigest())
        self.assertEqual((out / record["files"]["client.options"]["kept"]).read_bytes(), b"renderDistance:12\n")
        self.assertEqual(record["measurements"][0]["name"], "length")
        self.assertEqual(sorted(o["name"] for o in result["outputs"]), ["evidence", "files"])
        taken = self.c.submit("runs.evidence", {"job": self.echo("cd")["job"]}, out=str(out)).result(timeout=60)
        self.assertEqual(taken.outcome, "usage")  # never over another run's record
        self.assertIn("already holds the record of " + echo["job"], taken.summary)
        unknown = self.c.submit("runs.evidence", {"job": "j_0000000000000000000000"}).result(timeout=60)
        self.assertEqual(unknown.outcome, "usage", unknown.summary)

    def test_compare_evidence_of_jobs(self):
        x, y = self.echo("x"), self.echo("y")
        matched = self.c.submit("runs.compare-evidence", {"a_jobs": [x["job"]], "b_jobs": [y["job"]],
                                                          "treatment": ["params.text"]}).result(timeout=60)
        self.assertEqual(matched.outcome, "ok", matched.summary)
        self.assertEqual(matched.data.treatment, {"params.text": {"a": "x", "b": "y"}})
        self.assertIn("differ only in params.text", matched.summary)
        refused = self.c.submit("runs.compare-evidence", {"a_jobs": [x["job"]], "b_jobs": [y["job"]],
                                                          "treatment": ["params.times"]}).result(timeout=60)
        self.assertEqual(refused.outcome, "finding", refused.summary)
        self.assertEqual(refused.data.refusals, ["treatment params.times is the same on both sides: 1",
                                                 'params.text differs: "x" on A1; "y" on B1'])
        self.assertEqual(refused["findings"], refused.data.refusals)

    def test_compare_evidence_of_records_with_declared_files(self):
        records = []
        for text, options in (("x", b"fov:70\n"), ("y", b"fov:90\n")):
            job = self.echo(text)["job"]
            path = self.d.data / f"options-{text}.txt"
            path.write_bytes(options)
            out = self.d.out / f"record-{text}"
            made = self.c.submit("runs.evidence", {"job": job, "files": {"client.options": str(path)}},
                                 out=str(out)).result(timeout=60)
            self.assertEqual(made.outcome, "ok", made.summary)
            records.append(str(out))
        files_differ = self.c.submit("runs.compare-evidence", {"a": [records[0]], "b": [records[1]],
                                                               "treatment": ["params.text"]}).result(timeout=60)
        self.assertEqual(files_differ.outcome, "finding", files_differ.summary)
        self.assertEqual([d["field"] for d in files_differ.data.differences if d["refused"]], ["client.options"])
        both = self.c.submit("runs.compare-evidence", {"a": [records[0]], "b": [records[1]],
                                                       "treatment": ["params.text", "client.options"]}).result(timeout=60)
        self.assertEqual(both.outcome, "ok", both.summary)
        missing = self.c.submit("runs.compare-evidence", {"a": [records[0]], "b": [str(self.d.out / "none")]}
                                ).result(timeout=60)
        self.assertEqual(missing.outcome, "usage", missing.summary)

    def test_ab_refuses_a_verdict_when_a_control_differs(self):
        result = self.c.submit("runs.ab", {"command": "selftest.echo", "a": {"text": "x", "times": 2},
                                           "b": {"text": "y", "times": 3}, "pairs": 3, "metric": "length",
                                           "treatment": ["params.times"]}).result(timeout=240)
        self.assertEqual(result.outcome, "finding", result.summary)
        self.assertEqual(result.summary, 'no verdict on length, stopped after pair 1 of 3: params.text differs: '
                                         '"x" on A1; "y" on B1')
        self.assertIsNone(result.data.median_percent_change)
        self.assertIsNone(result.data.interval)
        proof = result.data.evidence
        self.assertTrue(proof["refused"])
        self.assertEqual(proof["refusals"], ['params.text differs: "x" on A1; "y" on B1'])
        self.assertEqual(len(result["children"]), 2)  # stopped after the first pair: more runs cannot undo it
        records = Path(proof["records"])
        for side, times in (("A", 2), ("B", 3)):
            record = json.loads((records / side / "1" / evidence.RECORD).read_text(encoding="utf-8"))
            self.assertEqual(record["fields"]["params.times"], times)
        self.assertTrue((records / "comparison.json").is_file())

    def test_ab_reports_a_verdict_when_only_the_treatment_differs(self):
        options = self.d.data / "ab-options.txt"
        options.write_bytes(b"fov:70\n")
        result = self.c.submit("runs.ab", {"command": "selftest.echo", "a": {"text": "x", "times": 2},
                                           "b": {"text": "x", "times": 3}, "pairs": 2, "metric": "length",
                                           "treatment": ["params.times"], "fields": {"world.seed": 7},
                                           "files": {"client.options": str(options)}}).result(timeout=240)
        self.assertEqual(result.outcome, "ok", result.summary)
        self.assertAlmostEqual(result.data.median_percent_change, 50.0)
        self.assertTrue(result.summary.endswith("; the runs differ only in params.times"), result.summary)
        proof = result.data.evidence
        self.assertFalse(proof["refused"], proof["refusals"])
        self.assertEqual(proof["treatment"], {"params.times": {"a": 2, "b": 3}})
        records = Path(proof["records"])
        self.assertEqual(len(list(records.glob(f"*/*/{evidence.RECORD}"))), 4)
        first = json.loads((records / "B" / "2" / evidence.RECORD).read_text(encoding="utf-8"))
        self.assertEqual(first["fields"]["world.seed"], 7)
        self.assertEqual((records / "B" / "2" / first["files"]["client.options"]["kept"]).read_bytes(), b"fov:70\n")

    def test_ab_kinds_and_an_advisory_precondition(self):
        kinds_file = self.d.data / "kinds.toml"
        kinds_file.write_text('[kinds]\n"params.text" = "ignored"\n', encoding="utf-8")
        base = {"command": "selftest.echo", "a": {"text": "x", "times": 2}, "b": {"text": "y", "times": 3},
                "pairs": 2, "metric": "length", "treatment": ["params.times"]}
        ignored = self.c.submit("runs.ab", dict(base, kinds=str(kinds_file))).result(timeout=240)
        self.assertEqual(ignored.outcome, "ok", ignored.summary)
        self.assertEqual({d["field"]: d["role"] for d in ignored.data.evidence["differences"]},
                         {"params.text": "ignored", "params.times": "treatment"})
        advisory = self.c.submit("runs.ab", dict(base, precondition=False)).result(timeout=240)
        self.assertEqual(advisory.outcome, "ok", advisory.summary)  # the verdict, with the refusal beside it
        self.assertAlmostEqual(advisory.data.median_percent_change, 50.0)
        self.assertTrue(advisory.data.evidence["refused"])
        self.assertFalse(advisory.data.evidence["precondition"])
        self.assertIn("the evidence would refuse it", advisory.summary)
        broken = self.d.data / "broken-kinds.toml"
        broken.write_text('"params.text" = "ignored"\n', encoding="utf-8")
        refused = self.c.submit("runs.ab", dict(base, kinds=str(broken))).result(timeout=60)
        self.assertEqual(refused.outcome, "usage", refused.summary)  # before any run
        self.assertEqual(refused["children"], [])


if __name__ == "__main__":
    unittest.main()
