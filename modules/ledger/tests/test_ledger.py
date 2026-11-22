"""The ledger: recording runs with their inputs and findings, pinning evidence, the migration to named inputs,
importing results databases, CI run records, result files and logs, and the small commands around them."""

import datetime
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO / "tests"))
from harness import Daemon  # noqa: E402

from wb_core.db import Database  # noqa: E402

LEDGER_SOURCE = Path(os.environ.get("WB_TEST_LEDGER_SOURCE", "-"))  # a results.sqlite to import, if there is one
MIGRATIONS = REPO / "modules" / "ledger" / "wb_ledger" / "migrations"
SCHEMA = (MIGRATIONS / "001_tables.sql").read_text(encoding="utf-8")
INPUTS = (MIGRATIONS / "002_run_inputs.sql").read_text(encoding="utf-8")
TABLES = ("machine", "api_call", "offer", "rental", "run", "run_input", "measurement", "finding", "finding_run",
          "charge")
RESULT = "wb/ledger-result/v1"

OWN_CALLS = ("INSERT INTO ledger_api_call (id, time, provider, method, endpoint, purpose) VALUES "
             "(1, '2026-01-01T00:00:00.000Z', 'vast', 'GET', '/instances/', 'status'), "
             "(2, '2026-01-01T00:01:00.000Z', 'vast', 'GET', '/instances/', 'status')")
SOURCE_CALLS = ("INSERT INTO api_call (id, time, provider, method, endpoint, purpose) VALUES "
                "(1, '2025-06-01T00:00:00.000Z', 'vast', 'PUT', '/bundles/', 'market'), "
                "(2, '2025-06-01T00:05:00.000Z', 'vast', 'PUT', '/bundles/', 'market'), "
                "(3, '2025-06-01T00:10:00.000Z', 'vast', 'PUT', '/bundles/', 'market');")
# Which call each offer came from, by what the call is rather than its id: {t} goes before each table name.
OFFER_CALLS = "SELECT o.offer, c.time FROM {t}offer o JOIN {t}api_call c ON c.id = o.call_id"
RUN_INPUTS = ("SELECT r.label, i.name, i.value, i.sha256 FROM ledger_run_input i "
              "JOIN ledger_run r ON r.id = i.run_id")
PARSERS = r"""
    [modules.ledger.parsers.frames]
    frame_ms = 'frame (?P<value>[\d.]+) ?(?P<unit>ms)'
    stage_ms = 'stage (?P<part>\w+) (?P<value>[\d.]+)'
    never = 'no such line (?P<value>\d+)'
    [modules.ledger.parsers.loose]
    size = 'size (?P<value>\S+)'
    [modules.ledger.parsers.broken]
    count = 'count (\d+)'
    [modules.ledger.readers.passes]
    each = "*"
    label = "passes/{@key}"
    date = "utc"
    inputs = { commit = "commit" }
    [modules.ledger.readers.passes.measurements]
    seconds = { path = "seconds", unit = "s" }
    tests = "tests.*.tests"
    failures = "tests.*.failures"
    [modules.ledger.readers.telemetry]
    require = { schema = "demo.telemetry.v1" }
    label = "telemetry/{@folder}/{@file}"
    date = "startedUtc"
    fields = { card = "machine.gpu", os = "machine.os" }
    inputs = { build = "versions.app.sha256" }
    [modules.ledger.readers.telemetry.measurements]
    passed = "passed"
    tick_p95_ms = { path = "scenarios.[scenario].tick.p95", unit = "ms" }
    ready_seconds = { path = "process.*.launchToReadySeconds", unit = "s" }
    [modules.ledger.readers.wrong]
    label = "x"
    fields = { colour = "c" }
    measurements = { a = "b" }
"""


def nonzero(counts: dict) -> dict:
    return {table: n for table, n in counts.items() if n}


def added(imported: dict) -> dict:
    """The rows an import added, per table."""
    return nonzero({table: imported[table] for table in TABLES if table in imported})


def moved_columns() -> list[str]:
    """The run columns migration 002 moves into inputs, read off the two migrations rather than named here."""
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(SCHEMA)
        before = [row[1] for row in connection.execute("PRAGMA table_info(ledger_run)")]
        connection.executescript(INPUTS)
        after = {row[1] for row in connection.execute("PRAGMA table_info(ledger_run)")}
    finally:
        connection.close()
    return [column for column in before if column not in after]


def sample_values(columns: list[str]) -> dict:
    """A value for each column, by turns a SHA-256 in upper case, a commit and a plain label."""
    kinds = ("AB01" * 16, "0123456789abcdef0123456789abcdef01234567", "frozen-label-2")
    return {column: kinds[index % len(kinds)] for index, column in enumerate(columns)}


def digest(value: str):
    """The rule migration 002 and the importers apply, restated: 64 hex digits are also the input's SHA-256."""
    return value.lower() if len(value) == 64 and all(c in "0123456789abcdefABCDEF" for c in value) else None


def write_json(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1), encoding="utf-8")
    return path


def ci_run(records: Path, run_id: str, jobs: dict, trees: dict, seconds: float = 20.0, changed: bool = False) -> Path:
    """A CI run's folder as the actions engine leaves it: a log per step and the summary.json."""
    folder = records / run_id
    for name, job in jobs.items():
        for index, step in enumerate(job.get("steps", []), 1):
            log = folder / name / f"{index:02d}-{step['name']}.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text("ok\n", encoding="utf-8")
    write_json(folder / "summary.json", {"schema": "actions.summary.v1", "runId": run_id, "targets": ["ci"],
                                         "order": list(jobs), "trees": trees, "treesChanged": changed,
                                         "seconds": seconds, "jobs": jobs})
    return folder


def step(name: str, seconds: float, code: int = 0) -> dict:
    return {"name": name, "exitCode": code, "seconds": seconds, "log": f"{name}.log"}


class Ledger(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = Daemon().start()
        cls.c = cls.d.client

    @classmethod
    def tearDownClass(cls):
        cls.d.stop()

    def ok(self, command: str, params: dict):
        result = self.c.submit(command, params).result(timeout=60)
        self.assertEqual(result.outcome, "ok", result.summary)
        return result

    def test_run_finding_and_pinning(self):
        echo = self.c.submit("selftest.echo", {"text": "evidence"}, fresh=True).result(timeout=60)
        run = self.c.submit("ledger.add-run", {"label": "t/run-1", "fields": {"card": "RTX 3060"},
                                                "measurements": [{"metric": "psnr", "value": 41.5, "unit": "dB"}]})
        self.assertEqual(run.result(timeout=60).outcome, "ok")
        finding = self.c.submit("ledger.add-finding", {"id": "t-finding", "title": "T", "statement": "S",
                                                        "status": "established", "runs": ["t/run-1"],
                                                        "jobs": [echo.job]}).result(timeout=60)
        self.assertEqual(finding.outcome, "ok", finding.summary)
        shown = self.c.submit("ledger.show", {"ref": "t/run-1"}).result(timeout=60)
        self.assertEqual(shown.data.measurements[0]["value"], 41.5)
        pinned = self.d.workbench.db.one("SELECT pinned FROM artifact WHERE job = ?", (echo.job,))
        self.assertEqual(pinned["pinned"], 1)
        bad = self.c.submit("ledger.add-finding", {"id": "t-bad", "title": "T", "statement": "S",
                                                    "status": "established", "runs": ["no-such-run"]}).result(timeout=60)
        self.assertEqual(bad.outcome, "usage")
        self.assertIn("no run no-such-run", bad.summary)

    def test_query_is_read_only(self):
        result = self.c.submit("ledger.query", {"sql": "DELETE FROM ledger_run"}).result(timeout=60)
        self.assertEqual(result.outcome, "usage")
        self.assertIn("readonly", result.summary.replace(" ", "").lower())

    def test_a_run_names_its_inputs_and_shows_them(self):
        jar = "AB" * 32
        self.ok("ledger.add-run", {"label": "t/inputs", "fields": {"card": "RTX 3060", "radius": 12},
                                   "inputs": [{"name": "mod.jar", "value": "1.4.2", "sha256": "sha256:" + jar},
                                              {"name": "checkout", "value": "0a1b2c3d"},
                                              {"name": "world", "sha256": "cd" * 32}]})
        shown = self.ok("ledger.show", {"ref": "t/inputs"})
        self.assertEqual(shown.data.inputs, [{"name": "checkout", "value": "0a1b2c3d", "sha256": None},
                                             {"name": "mod.jar", "value": "1.4.2", "sha256": jar.lower()},
                                             {"name": "world", "value": None, "sha256": "cd" * 32}])
        self.assertIn("3 input(s)", shown.summary)
        self.assertEqual(shown.data.run["radius"], 12)
        self.ok("ledger.add-run", {"label": "t/inputs", "inputs": [{"name": "mod.jar", "value": "1.4.3"}]})
        listed = self.ok("ledger.runs", {"like": "t/inputs"})
        self.assertEqual(listed.data.runs[0]["inputs"][1], {"name": "mod.jar", "value": "1.4.3", "sha256": None})
        self.assertEqual(listed.data.runs[0]["card"], "RTX 3060")  # an update keeps the fields it does not give
        for params, said in (({"inputs": [{"name": "lonely"}]}, "needs a value, a sha256, or both"),
                             ({"inputs": [{"name": "x", "value": "1", "sha256": "abc"}]}, "64 hex digits"),
                             ({"inputs": [{"name": "x", "value": "1", "kind": "jar"}]}, "unknown key(s) kind"),
                             ({"inputs": [{"name": "x", "value": "1"}, {"name": "x", "value": "2"}]}, "named twice"),
                             ({"fields": {"checkout": "0a1b"}}, "goes in its inputs"),
                             ({"fields": {"date": "20 November"}}, "YYYY-MM-DD"),
                             ({"measurements": [{"metric": "fps", "value": "fast"}]}, "not a number")):
            refused = self.c.submit("ledger.add-run", {"label": "t/refused", **params}).result(timeout=60)
            self.assertEqual(refused.outcome, "usage", params)
            self.assertIn(said, refused.summary)
        self.assertIsNone(self.d.workbench.db.one("SELECT id FROM ledger_run WHERE label = 't/refused'"))

    def test_link_run_to_jobs_and_a_machine(self):
        echo = self.c.submit("selftest.echo", {"text": "linked"}, fresh=True).result(timeout=60)
        run = self.ok("ledger.add-run", {"label": "t/linked"}).data.run
        machine = self.ok("ledger.add-machine", {"key": "desk", "gpu": "RTX 3060", "cpu_cores_effective": 16})
        self.assertTrue(machine.data.added)
        again = self.ok("ledger.add-machine", {"key": "desk", "cpu_ghz": 4.2})
        self.assertEqual((again.data.machine, again.data.added), (machine.data.machine, False))
        self.assertIn("updated", again.summary)
        specs = self.d.workbench.db.one("SELECT gpu, cpu_cores_effective, cpu_ghz FROM ledger_machine WHERE id = ?",
                                        (machine.data.machine,))
        self.assertEqual(tuple(specs), ("RTX 3060", 16.0, 4.2))
        linked = self.ok("ledger.link-run", {"run": "t/linked", "jobs": [echo.job], "machine": "local:desk"})
        self.assertEqual((linked.data.linked, linked.data.jobs), ([echo.job], [echo.job]))
        shown = self.ok("ledger.show", {"ref": str(run)})
        self.assertEqual((shown.data.jobs, shown.data.run["machine_id"]), ([echo.job], machine.data.machine))
        self.assertEqual(self.ok("ledger.link-run", {"run": "t/linked", "jobs": [echo.job]}).data.linked, [])
        for params, said in (({"jobs": ["no-such-job"]}, "no job no-such-job"),
                             ({"machine": "local:nowhere"}, "no machine local:nowhere"),
                             ({"machine": "desk"}, "PROVIDER:KEY"), ({}, "name the jobs")):
            refused = self.c.submit("ledger.link-run", {"run": "t/linked", **params}).result(timeout=60)
            self.assertEqual(refused.outcome, "usage", params)
            self.assertIn(said, refused.summary)
        self.assertEqual(self.ok("ledger.show", {"ref": "t/linked"}).data.jobs, [echo.job])

    def test_supersede(self):
        for name in ("s-old", "s-new", "s-third"):
            self.ok("ledger.add-finding", {"id": name, "title": name, "statement": "S", "status": "hypothesis"})
        first = self.ok("ledger.supersede", {"finding": "s-old", "by": "s-new"})
        self.assertTrue(first.data.changed)
        shown = self.ok("ledger.show", {"ref": "s-old"})
        self.assertEqual((shown.data.finding["status"], shown.data.finding["superseded_by"]), ("superseded", "s-new"))
        self.assertFalse(self.ok("ledger.supersede", {"finding": "s-old", "by": "s-new"}).data.changed)
        for finding, by, said in (("s-old", "s-third", "superseded by s-new already"),
                                  ("s-new", "s-old", "cannot supersede s-new")):
            refused = self.c.submit("ledger.supersede", {"finding": finding, "by": by}).result(timeout=60)
            self.assertEqual(refused.outcome, "finding", refused.summary)
            self.assertIn(said, refused.summary)
        for finding, by, said in (("s-old", "s-old", "cannot supersede itself"), ("s-old", "s-none", "no finding")):
            refused = self.c.submit("ledger.supersede", {"finding": finding, "by": by}).result(timeout=60)
            self.assertEqual(refused.outcome, "usage")
            self.assertIn(said, refused.summary)
        held = self.d.workbench.db.one("SELECT superseded_by FROM ledger_finding WHERE id = 's-old'")
        self.assertEqual(held["superseded_by"], "s-new")

    def test_prune_drops_old_response_bodies_and_keeps_the_calls(self):
        from wb_ledger import store
        connection = store.connect(self.d.state / "db" / "wb.sqlite")
        try:
            listing = json.dumps({"offers": [{"id": 7, "machine_id": 70, "dph_total": 0.1}]}).encode()
            old = store.record_call(connection, "test", "POST", "/bundles/", {}, 200, 10.0, listing, "market",
                                    sent="2025-01-01T00:00:00.000Z")
            new = store.record_call(connection, "test", "GET", "/instances/", None, 200, 5.0, b'{"instances": []}',
                                    "status")
        finally:
            connection.close()
        dry = self.ok("ledger.prune", {"days": 30})
        self.assertEqual((dry.data.calls, dry.data.dropped), (1, False))
        self.assertIn("nothing dropped", dry.summary)
        self.assertIsNotNone(self.d.workbench.db.one("SELECT response FROM ledger_api_call WHERE id = ?",
                                                     (old,))["response"])
        done = self.ok("ledger.prune", {"days": 30, "execute": True})
        self.assertEqual((done.data.calls, done.data.dropped), (1, True))
        bodies = {row["id"]: row["response"] is not None for row in self.d.workbench.db.query(
            "SELECT id, response FROM ledger_api_call WHERE id IN (?, ?)", (old, new))}
        self.assertEqual(bodies, {old: False, new: True})
        self.assertEqual(self.d.workbench.db.one("SELECT count(*) AS n FROM ledger_offer WHERE call_id = ?",
                                                 (old,))["n"], 1)
        self.assertEqual(self.ok("ledger.prune", {"days": 30}).data.calls, 0)

    @unittest.skipUnless(LEDGER_SOURCE.exists(), "no results database to import (WB_TEST_LEDGER_SOURCE)")
    def test_import_a_results_database(self):
        copy = self.d.data / "results.sqlite"
        source = sqlite3.connect(f"{LEDGER_SOURCE.as_uri()}?mode=ro", uri=True)
        target = sqlite3.connect(copy)
        source.backup(target)
        source.close()
        target.close()
        expected = sqlite3.connect(copy).execute("SELECT count(*) FROM offer").fetchone()[0]
        first = self.c.submit("ledger.import", {"source": str(copy)}).result(timeout=300)
        self.assertEqual(first.outcome, "ok", first.summary)
        self.assertEqual(first.data.imported.get("offer"), expected)
        again = self.c.submit("ledger.import", {"source": str(copy)}).result(timeout=300)
        self.assertEqual(added(again.data.imported), {})  # idempotent
        view = self.c.submit("ledger.query", {"sql": "SELECT count(*) FROM ledger_offer_price"}).result(timeout=60)
        self.assertEqual(view.data.rows[0][0], expected)


class Migration(unittest.TestCase):
    """A ledger the first schema made, with rows, as the next start finds it."""

    def test_old_run_columns_become_inputs_and_every_other_row_stays(self):
        moved = moved_columns()
        self.assertTrue(moved)
        values = sample_values(moved)
        root = Path(tempfile.mkdtemp(prefix="wb-test-"))
        database = Database(root / "state" / "db" / "wb.sqlite")
        database.migrate("ledger", [SCHEMA])  # the ledger as its first migration left it
        listed = ", ".join(f'"{column}"' for column in moved)
        database.execute("INSERT INTO ledger_machine (id, provider, key, gpu) VALUES (1, 'local', 'desk', 'RTX 3060')")
        database.execute("INSERT INTO ledger_rental (id, provider, instance, machine_id, held_s, dph_billed) "
                         "VALUES (1, 'test', 'i-1', 1, 3600, 0.1)")
        database.execute(f"INSERT INTO ledger_run (id, label, date, card, tour, radius, rental_id, {listed}) "
                         f"VALUES (1, 'old/full', '2025-06-01', 'RTX 3060', 'forest.json', 12, 1, "
                         f"{', '.join('?' * len(moved))})", tuple(values.values()))
        database.execute("INSERT INTO ledger_run (id, label, date) VALUES (2, 'old/bare', '2025-06-02')")
        database.execute(f'INSERT INTO ledger_run (id, label, date, "{moved[0]}") VALUES (3, \'old/empty\', '
                         "'2025-06-03', '')")
        database.execute("INSERT INTO ledger_measurement (run_id, source, place, metric, value) VALUES "
                         "(1, 'bench', '1, 2, 3', 'ms', 1.5), (1, 'bench', '4, 5, 6', 'ms', 1.7), "
                         "(1, 'stage', NULL, 'cpu_ms', 3.0), (2, 'bench', '1, 2, 3', 'ms', 2.5)")
        database.execute("INSERT INTO ledger_finding (id, date, title, statement, status) "
                         "VALUES ('f-old', '2025-06-04', 'T', 'S', 'established')")
        database.execute("INSERT INTO ledger_finding_run VALUES ('f-old', 1), ('f-old', 2)")
        database.connect().close()
        daemon = Daemon(root=root).start()
        try:
            db = daemon.workbench.db
            self.assertIsNotNone(db.one("SELECT 1 FROM schema_migration WHERE module = 'ledger' AND version = 2"))
            columns = {row["name"] for row in db.query("PRAGMA table_info(ledger_run)")}
            self.assertFalse(columns & set(moved))
            expected = {("old/full", column, value, digest(value)) for column, value in values.items()}
            expected.add(("old/empty", moved[0], "", None))
            self.assertEqual({tuple(row) for row in db.query(RUN_INPUTS)}, expected)
            self.assertTrue(any(sha for *_, sha in expected))  # the SHA-256 rule was exercised
            from wb_ledger import store
            self.assertEqual({value: digest(value) for value in values.values()},
                             {value: store.digest_of(value) for value in values.values()})
            self.assertEqual(db.one("SELECT count(*) AS n FROM ledger_measurement")["n"], 4)
            self.assertEqual(db.one("SELECT count(*) AS n FROM ledger_finding_run")["n"], 2)
            kept = db.one("SELECT card, tour, radius, rental_id FROM ledger_run WHERE id = 1")
            self.assertEqual(tuple(kept), ("RTX 3060", "forest.json", 12, 1))
            cost = db.one("SELECT label, round(cost, 4) AS cost, places_benchmarked FROM ledger_run_cost")
            self.assertEqual(tuple(cost), ("old/full", 0.1, 2))
            shown = daemon.client.submit("ledger.show", {"ref": "old/full"}).result(timeout=60)
            self.assertEqual(shown.outcome, "ok", shown.summary)
            self.assertEqual({item["name"]: item["value"] for item in shown.data.inputs}, values)
            listed_runs = daemon.client.submit("ledger.runs", {}).result(timeout=60)
            self.assertEqual({run["label"]: len(run["inputs"]) for run in listed_runs.data.runs},
                             {"old/full": len(moved), "old/bare": 0, "old/empty": 1})
        finally:
            daemon.stop()


class Import(unittest.TestCase):
    """Importing a results database into a ledger that holds rows of its own under the ids the source uses."""

    def setUp(self):
        self.d = Daemon().start()
        self.c = self.d.client
        self.db = self.d.workbench.db

    def tearDown(self):
        self.d.stop()

    def source(self, rows: str, name: str = "results.sqlite", schema: str = SCHEMA) -> Path:
        """A results database: the ledger's tables under their own names, holding ``rows``."""
        path = self.d.data / name
        connection = sqlite3.connect(path)
        connection.executescript(schema.replace("ledger_", "") + rows)
        connection.close()
        return path

    def imported(self, source: Path):
        result = self.c.submit("ledger.import", {"source": str(source)}).result(timeout=60)
        self.assertEqual(result.outcome, "ok", result.summary)
        return result

    def import_keeping_ids(self, source: Path):
        """The import as it was before it mapped ids: each row under its source id, dropped when that is taken."""
        connection = sqlite3.connect(self.d.state / "db" / "wb.sqlite")
        connection.execute("ATTACH DATABASE ? AS src", (str(source),))
        for table in ("api_call", "offer"):
            columns = ", ".join(r[1] for r in connection.execute(f"PRAGMA src.table_info({table})"))
            connection.execute(f"INSERT OR IGNORE INTO ledger_{table} ({columns}) SELECT {columns} FROM src.{table}")
        connection.commit()
        connection.close()

    def pairs(self, sql: str, source: Path | None = None) -> set:
        """What ``sql`` returns from the ledger, or from ``source``."""
        if source is None:
            return {tuple(row) for row in self.db.query(sql.format(t="ledger_"))}
        connection = sqlite3.connect(source)
        try:
            return set(connection.execute(sql.format(t="")).fetchall())
        finally:
            connection.close()

    def calls_of_offers(self) -> dict:
        return {row["offer"]: row["call_id"] for row in self.db.query("SELECT offer, call_id FROM ledger_offer")}

    def test_rows_whose_ids_are_taken_get_new_ones_and_references_follow(self):
        self.db.execute(OWN_CALLS)
        self.db.execute("INSERT INTO ledger_offer (id, observed, provider, offer, call_id) VALUES "
                        "(1, '2026-01-01T00:00:00.000Z', 'vast', 'own-1', 1), "
                        "(2, '2026-01-01T00:01:00.000Z', 'vast', 'own-2', 2)")
        ours = self.pairs(OFFER_CALLS)
        source = self.source(SOURCE_CALLS + "INSERT INTO offer (id, observed, provider, offer, call_id) VALUES "
                             "(1, '2025-06-01T00:00:00.000Z', 'vast', 'o-1', 1), "
                             "(2, '2025-06-01T00:05:00.000Z', 'vast', 'o-2', 2), "
                             "(3, '2025-06-01T00:10:00.000Z', 'vast', 'o-3', 3);")
        first = self.imported(source)
        self.assertEqual(added(first.data.imported), {"api_call": 3, "offer": 3})  # nothing dropped
        self.assertEqual(nonzero(first.data.imported.remapped), {"api_call": 2, "offer": 2})
        self.assertIn("inserted api_call 1, offer 1; remapped api_call 2, offer 2", first.summary)
        # Call 3 keeps its id; the source's calls 1 and 2 follow it as 4 and 5, and their offers name them there.
        self.assertEqual(self.calls_of_offers(), {"own-1": 1, "own-2": 2, "o-1": 4, "o-2": 5, "o-3": 3})
        self.assertEqual(self.pairs(OFFER_CALLS), ours | self.pairs(OFFER_CALLS, source))
        again = self.imported(source)
        self.assertEqual(added(again.data.imported), {})
        self.assertEqual(nonzero(again.data.imported.matched), {"api_call": 3, "offer": 3})
        self.assertEqual(self.calls_of_offers(), {"own-1": 1, "own-2": 2, "o-1": 4, "o-2": 5, "o-3": 3})

    def test_reimport_repairs_offers_an_earlier_import_pointed_at_our_calls(self):
        self.db.execute(OWN_CALLS)
        source = self.source(SOURCE_CALLS + "INSERT INTO offer (id, observed, provider, offer, call_id) VALUES "
                             "(10, '2025-06-01T00:00:00.000Z', 'vast', 'o-1', 1), "
                             "(11, '2025-06-01T00:05:00.000Z', 'vast', 'o-2', 2), "
                             "(12, '2025-06-01T00:10:00.000Z', 'vast', 'o-3', 3);")
        self.import_keeping_ids(source)
        self.assertEqual(self.pairs(OFFER_CALLS), {("o-1", "2026-01-01T00:00:00.000Z"),  # our calls, not theirs
                                                   ("o-2", "2026-01-01T00:01:00.000Z"),
                                                   ("o-3", "2025-06-01T00:10:00.000Z")})
        repaired = self.imported(source)
        self.assertEqual(added(repaired.data.imported), {"api_call": 2})
        self.assertEqual(nonzero(repaired.data.imported.repaired), {"offer": 2})
        self.assertIn("remapped api_call 2; repaired offer 2", repaired.summary)
        self.assertEqual(self.pairs(OFFER_CALLS), self.pairs(OFFER_CALLS, source))
        self.assertEqual([row["id"] for row in self.db.query("SELECT id FROM ledger_offer ORDER BY id")], [10, 11, 12])
        again = self.imported(source)
        self.assertEqual(added(again.data.imported), {})
        self.assertEqual(nonzero(again.data.imported.repaired), {})
        self.assertEqual(self.pairs(OFFER_CALLS), self.pairs(OFFER_CALLS, source))

    def test_rows_with_the_natural_key_of_one_already_there_map_to_it(self):
        self.db.execute("INSERT INTO ledger_machine (id, provider, key) VALUES (1, 'vast', 'm-a')")
        self.db.execute("INSERT INTO ledger_rental (id, provider, instance, machine_id) VALUES (1, 'vast', 'i-a', 1)")
        self.db.execute("INSERT INTO ledger_run (id, label, date, machine_id, rental_id) "
                        "VALUES (1, 'shared/run', '2026-01-01', 1, 1)")
        self.db.execute("INSERT INTO ledger_offer (id, observed, provider, offer, machine_id) "
                        "VALUES (1, '2025-06-01T00:00:00.000Z', 'vast', 'o-a', 1)")
        source = self.source("""
            INSERT INTO machine (id, provider, key) VALUES (1, 'vast', 'm-b'), (2, 'vast', 'm-a');
            INSERT INTO rental (id, provider, instance, machine_id) VALUES (1, 'vast', 'i-b', 1), (2, 'vast', 'i-a', 2);
            INSERT INTO run (id, label, date, machine_id, rental_id)
                VALUES (2, 'shared/run', '2025-06-01', 2, 2), (3, 'source/run', '2025-06-02', 1, 1);
            INSERT INTO offer (id, observed, provider, offer, machine_id)
                VALUES (2, '2025-06-01T00:00:00.000Z', 'vast', 'o-a', 2),
                       (3, '2025-06-02T00:00:00.000Z', 'vast', 'o-b', 1);
            INSERT INTO measurement (run_id, source, metric, value) VALUES (2, 'wb', 'fps', 60), (3, 'wb', 'fps', 90);
            INSERT INTO finding (id, date, title, statement, status)
                VALUES ('f-1', '2025-06-03', 'T', 'S', 'established');
            INSERT INTO finding_run VALUES ('f-1', 2), ('f-1', 3);
        """)
        first = self.imported(source)
        imported = first.data.imported
        self.assertEqual(nonzero(imported.matched),
                         {"machine": 1, "offer": 1, "rental": 1, "run": 1, "measurement": 1})
        self.assertEqual(nonzero(imported.remapped), {"machine": 1, "rental": 1})
        self.assertEqual(nonzero(imported.inserted),
                         {"offer": 1, "run": 1, "measurement": 1, "finding": 1, "finding_run": 2})
        for sql in ("SELECT x.instance, m.key FROM {t}rental x JOIN {t}machine m ON m.id = x.machine_id",
                    "SELECT o.offer, m.key FROM {t}offer o JOIN {t}machine m ON m.id = o.machine_id",
                    "SELECT r.label, m.key, x.instance FROM {t}run r JOIN {t}machine m ON m.id = r.machine_id "
                    "JOIN {t}rental x ON x.id = r.rental_id",
                    "SELECT f.finding_id, r.label FROM {t}finding_run f JOIN {t}run r ON r.id = f.run_id"):
            self.assertEqual(self.pairs(sql), self.pairs(sql, source), sql)
        # A run already there keeps its own measurements; the run this import added brings its own.
        self.assertEqual(self.pairs("SELECT r.label, x.value FROM {t}measurement x JOIN {t}run r ON r.id = x.run_id"),
                         {("source/run", 90.0)})
        again = self.imported(source)
        self.assertEqual(added(again.data.imported), {})

    def test_a_row_the_ledger_refuses_stops_the_import_and_changes_nothing(self):
        source = self.source(SOURCE_CALLS + """
            DROP TABLE finding;
            CREATE TABLE finding (id TEXT PRIMARY KEY, date TEXT, title TEXT, statement TEXT, status TEXT);
            INSERT INTO finding VALUES ('f-odd', '2025-06-01', 'T', 'S', 'pending');
        """)
        refused = self.c.submit("ledger.import", {"source": str(source)}).result(timeout=60)
        self.assertEqual(refused.outcome, "usage")
        self.assertIn("finding 'f-odd' can be neither matched nor inserted", refused.summary)
        self.assertEqual(self.db.one("SELECT count(*) AS n FROM ledger_api_call")["n"], 0)  # its calls are undone
        dangling = self.source(SOURCE_CALLS + "INSERT INTO offer (id, observed, provider, offer, call_id) "
                                              "VALUES (1, '2025-06-01T00:00:00.000Z', 'vast', 'o-1', 9);",
                               name="dangling.sqlite")
        refused = self.c.submit("ledger.import", {"source": str(dangling)}).result(timeout=60)
        self.assertEqual(refused.outcome, "usage")
        self.assertIn("offer 1: call_id names api_call 9, which the source does not have", refused.summary)

    def test_an_older_schemas_run_columns_become_inputs_of_the_runs_they_map_to(self):
        moved = moved_columns()
        values = sample_values(moved)
        self.db.execute("INSERT INTO ledger_run (id, label, date) VALUES (1, 'own/run', '2026-01-01')")
        listed = ", ".join(f'"{column}"' for column in moved)
        given = ", ".join(f"'{value}'" for value in values.values())
        source = self.source(f"INSERT INTO run (id, label, date, {listed}) VALUES (1, 'old/run', '2025-06-01', "
                             f"{given}); INSERT INTO run (id, label, date) VALUES (2, 'old/bare', '2025-06-02');")
        first = self.imported(source)
        self.assertEqual(added(first.data.imported), {"run": 2, "run_input": len(moved)})
        self.assertEqual(nonzero(first.data.imported.remapped), {"run": 1})  # id 1 is ours: the inputs follow it
        expected = {("old/run", column, value, digest(value)) for column, value in values.items()}
        self.assertEqual(self.pairs(RUN_INPUTS), expected)
        again = self.imported(source)
        self.assertEqual(added(again.data.imported), {})
        self.assertEqual(nonzero(again.data.imported.matched), {"run": 2, "run_input": len(moved)})
        self.assertEqual(self.pairs(RUN_INPUTS), expected)

    def test_a_sources_run_inputs_follow_their_runs(self):
        self.db.execute("INSERT INTO ledger_run (id, label, date) VALUES (1, 'own/run', '2026-01-01')")
        source = self.source("INSERT INTO run (id, label, date) VALUES (1, 'new/run', '2025-06-01');"
                             f"INSERT INTO run_input VALUES (1, 'mod', '1.2', NULL), (1, 'world', NULL, '{'e' * 64}');",
                             schema=SCHEMA + INPUTS)
        first = self.imported(source)
        self.assertEqual(added(first.data.imported), {"run": 1, "run_input": 2})
        self.assertEqual(self.pairs(RUN_INPUTS), {("new/run", "mod", "1.2", None), ("new/run", "world", None, "e" * 64)})
        self.assertEqual(added(self.imported(source).data.imported), {})


class Importers(unittest.TestCase):
    """CI run records, result files and logs, imported into a ledger that may hold their runs already."""

    @classmethod
    def setUpClass(cls):
        cls.d = Daemon(PARSERS).start()
        cls.c = cls.d.client
        cls.db = cls.d.workbench.db

    @classmethod
    def tearDownClass(cls):
        cls.d.stop()

    def submit(self, command: str, params: dict, outcome: str = "ok"):
        result = self.c.submit(command, params).result(timeout=60)
        self.assertEqual(result.outcome, outcome, result.summary)
        return result

    def measured(self, label: str) -> set:
        shown = self.submit("ledger.show", {"ref": label})
        return {(m["source"], m["metric"], m["part"], m["sample"], m["value"], m["unit"])
                for m in shown.data.measurements}

    def test_ci_runs_become_runs_with_trees_and_job_measurements(self):
        records = self.d.state / "ci" / "records" / "demo"  # where ci.run keeps project demo's records
        failing = ci_run(records, "20261120-101500", trees={"demo": "a" * 40, "lib": "b" * 40}, jobs={
            "build": {"status": "passed", "steps": [step("compile", 3.0), step("jar", 4.5)]},
            "test": {"status": "failed", "steps": [step("unit", 10.0, code=1)]},
            "lint": {"status": "skipped", "reason": "after failure"}})
        ci_run(records, "20261121-090000", trees={"demo": "c" * 40, "lib": "b" * 40}, changed=True, seconds=1.5,
               jobs={"build": {"status": "passed", "cached": "20261120-101500", "steps": []}})
        (records / "20261121-100000").mkdir()  # running: no summary.json yet
        write_json(records / "cache" / "build-key.json", {"job": "build"})  # the engine's pass records
        self.db.execute("INSERT INTO job (id, command, module, params, state, submitted) "
                        "VALUES ('ci-job-1', 'ci.run', 'ci', '{}', 'settled', 0)")
        self.db.execute("INSERT INTO artifact (job, name, sha256, bytes, path) VALUES ('ci-job-1', 'run', ?, 0, ?)",
                        ("0" * 64, str(failing)))
        first = self.submit("ledger.import-ci", {"project": "demo"})
        self.assertEqual(added(first.data.imported), {"run": 2, "run_input": 4, "measurement": 8})
        self.assertEqual(first.data.imported.inserted.get("run_job"), 1)
        self.assertEqual(first.data.unfinished, ["20261121-100000"])
        shown = self.submit("ledger.show", {"ref": "ci/demo/20261120-101500"})
        self.assertEqual(shown.data.inputs, [{"name": "demo", "value": "a" * 40, "sha256": None},
                                             {"name": "lib", "value": "b" * 40, "sha256": None}])
        self.assertEqual(shown.data.jobs, ["ci-job-1"])
        self.assertEqual((shown.data.run["date"], shown.data.run["exit"]), ("2026-11-20", 1))
        self.assertEqual(self.measured("ci/demo/20261120-101500"), {
            ("ci", "job_seconds", "build", None, 7.5, "s"), ("ci", "job_passed", "build", None, 1.0, None),
            ("ci", "job_seconds", "test", None, 10.0, "s"), ("ci", "job_passed", "test", None, 0.0, None),
            ("ci", "job_skipped", "lint", None, 1.0, None), ("ci", "run_seconds", "", None, 20.0, "s")})
        cached = self.submit("ledger.show", {"ref": "ci/demo/20261121-090000"})
        self.assertEqual(cached.data.run["exit"], 0)
        self.assertIn("vouches for no tree", cached.data.run["note"])
        again = self.submit("ledger.import-ci", {"project": "demo"})
        self.assertEqual(added(again.data.imported), {})
        self.assertIn("nothing new", again.summary)
        copy = self.d.data / "records-copy" / "demo"
        shutil.copytree(records, copy)
        elsewhere = self.submit("ledger.import-ci", {"project": "demo", "runs": str(copy)})
        self.assertEqual(added(elsewhere.data.imported), {})  # the same runs, wherever their records lie
        odd = self.d.state / "ci" / "records" / "odd"
        ci_run(odd, "20261120-120000", trees={"odd": "d" * 40}, jobs={"x": {"status": "weird", "steps": []}})
        ci_run(odd, "20261120-130000", trees={"odd": "d" * 40}, jobs={"y": {"status": "passed", "steps": []}})
        refused = self.submit("ledger.import-ci", {"project": "odd"}, outcome="finding")
        self.assertIn("1 refused, first 20261120-120000: job x has status 'weird'", refused.summary)
        self.assertEqual(added(refused.data.imported), {"run": 1, "run_input": 1, "measurement": 2})
        missing = self.submit("ledger.import-ci", {"project": "none"}, outcome="usage")
        self.assertIn("no CI run records", missing.summary)

    def test_result_files_become_runs_and_findings(self):
        folder = self.d.data / "results"
        write_json(folder / "a" / "result.json", {
            "schema": RESULT, "label": "lab/a", "fields": {"date": "2026-11-20", "card": "RTX 3060"},
            "inputs": [{"name": "mod.jar", "value": "1.4.2", "sha256": "ab" * 32}],
            "measurements": [{"metric": "frame_ms", "value": 6.9, "unit": "ms", "part": "1920x1080", "sample": 1},
                             {"metric": "frame_ms", "value": 7.1, "unit": "ms", "part": "1920x1080", "sample": 2}],
            "findings": [{"id": "a-matches-b", "title": "A matches B", "statement": "Their frame times agree.",
                          "status": "hypothesis", "runs": ["lab/b"]}]})  # lab/b is a file read after this one
        write_json(folder / "b" / "result.json", {"schema": RESULT, "label": "lab/b", "jobs": ["job-elsewhere"],
                                                  "measurements": [{"metric": "frame_ms", "value": 7.0}]})
        write_json(folder / "c" / "result.json", {"schema": "some/other/v2", "label": "lab/c"})
        write_json(folder / "d" / "result.json", {"schema": RESULT, "label": "lab/a",
                                                  "measurements": [{"metric": "frame_ms", "value": 9.9}]})
        write_json(folder / "e" / "result.json", {"schema": RESULT, "label": "lab/e",
                                                  "measurements": [{"metric": "fps", "value": "fast"}]})
        write_json(folder / "e" / "notes.json", {"not": "a result"})  # not named result.json: not read
        first = self.submit("ledger.import-results", {"folder": str(folder)}, outcome="finding")
        self.assertEqual(first.data.files, 5)
        self.assertEqual(added(first.data.imported), {"run": 2, "run_input": 1, "measurement": 3, "finding": 1,
                                                      "finding_run": 2})
        self.assertEqual(first.data.imported.inserted.get("run_job"), 1)
        why = {Path(item["file"]).parent.name: item["why"] for item in first.data.refused}
        self.assertEqual(sorted(why), ["c", "d", "e"])
        self.assertIn("is not a wb/ledger-result/v1 file", why["c"])
        self.assertIn("lab/a holds other measurements from result", why["d"])
        self.assertIn("is not a number", why["e"])
        self.assertIsNone(self.db.one("SELECT id FROM ledger_run WHERE label = 'lab/e'"))
        shown = self.submit("ledger.show", {"ref": "lab/a"})
        self.assertEqual((shown.data.run["date"], shown.data.run["card"]), ("2026-11-20", "RTX 3060"))
        self.assertEqual(shown.data.inputs, [{"name": "mod.jar", "value": "1.4.2", "sha256": "ab" * 32}])
        self.assertEqual(self.measured("lab/a"), {("result", "frame_ms", "1920x1080", 1, 6.9, "ms"),
                                                  ("result", "frame_ms", "1920x1080", 2, 7.1, "ms")})
        finding = self.submit("ledger.show", {"ref": "a-matches-b"})
        self.assertEqual(sorted(run["label"] for run in finding.data.runs), ["lab/a", "lab/b"])
        self.assertEqual(self.submit("ledger.show", {"ref": "lab/b"}).data.jobs, ["job-elsewhere"])
        for name in ("c", "d", "e"):
            shutil.rmtree(folder / name)
        again = self.submit("ledger.import-results", {"folder": str(folder)})
        self.assertEqual(added(again.data.imported), {})
        self.assertIn("2 result file(s): nothing new", again.summary)
        one = self.submit("ledger.import-results", {"folder": str(folder / "b" / "result.json")})
        self.assertEqual((one.data.files, added(one.data.imported)), (1, {}))
        dangling = write_json(self.d.data / "dangling" / "result.json", {
            "schema": RESULT, "label": "lab/f", "findings": [{"id": "f-rests-on-nothing", "title": "T",
                                                              "statement": "S", "status": "hypothesis",
                                                              "runs": ["lab/nowhere"]}]})
        refused = self.submit("ledger.import-results", {"folder": str(dangling)}, outcome="finding")
        self.assertIn("rests on run lab/nowhere", refused.summary)
        self.assertIsNone(self.db.one("SELECT id FROM ledger_finding WHERE id = 'f-rests-on-nothing'"))
        self.assertIsNotNone(self.db.one("SELECT id FROM ledger_run WHERE label = 'lab/f'"))  # its run is recorded

    def test_a_parser_turns_a_log_into_a_runs_measurements_once(self):
        log = self.d.data / "logs" / "session.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        lines = "frame 6.9 ms\nstage terrain 3.1\nframe 7.1ms\nstage sky 0.4\r\nstage terrain 3.3\n"
        log.write_text(lines, encoding="utf-8", newline="")
        params = {"parser": "frames", "log": str(log), "run": "lab/parsed"}
        first = self.submit("ledger.parse-log", params)
        self.assertEqual((first.data.added, first.data.recorded, first.data.measurements), (True, "inserted", 5))
        self.assertEqual(first.data.missing, ["never"])
        self.assertIn("no match for never", first.summary)
        self.assertEqual(self.measured("lab/parsed"), {
            ("frames", "frame_ms", "", 1, 6.9, "ms"), ("frames", "frame_ms", "", 2, 7.1, "ms"),
            ("frames", "stage_ms", "terrain", 1, 3.1, None), ("frames", "stage_ms", "terrain", 2, 3.3, None),
            ("frames", "stage_ms", "sky", 1, 0.4, None)})
        day = datetime.datetime.fromtimestamp(log.stat().st_mtime, datetime.timezone.utc).date().isoformat()
        self.assertEqual(self.submit("ledger.show", {"ref": "lab/parsed"}).data.run["date"], day)
        again = self.submit("ledger.parse-log", params)
        self.assertEqual((again.data.added, again.data.recorded), (False, "matched"))
        log.write_text(lines + "frame 7.5 ms\n", encoding="utf-8", newline="")
        refused = self.submit("ledger.parse-log", params, outcome="finding")
        self.assertIn("holds other measurements from frames", refused.summary)
        self.assertEqual(len(self.measured("lab/parsed")), 5)
        replaced = self.submit("ledger.parse-log", dict(params, replace=True))
        self.assertEqual(replaced.data.recorded, "replaced")
        self.assertEqual(len(self.measured("lab/parsed")), 6)
        run = self.submit("ledger.add-run", {"label": "lab/by-id", "measurements": [{"metric": "fps", "value": 60}]})
        into = self.submit("ledger.parse-log", dict(params, run=str(run.data.run)))
        self.assertEqual((into.data.label, into.data.added), ("lab/by-id", False))
        self.assertEqual(len(self.measured("lab/by-id")), 7)  # its own, and the parser's
        loose = self.d.data / "logs" / "loose.log"
        loose.write_text("size big\n", encoding="utf-8")
        bad = self.submit("ledger.parse-log", {"parser": "loose", "log": str(loose), "run": "lab/loose"}, "finding")
        self.assertIn("'big' is not a number", bad.summary)
        nothing = self.submit("ledger.parse-log", {"parser": "loose", "log": str(log), "run": "lab/loose"},
                              "finding")
        self.assertIn("matched none of size", nothing.summary)
        self.assertIsNone(self.db.one("SELECT id FROM ledger_run WHERE label = 'lab/loose'"))
        broken = self.submit("ledger.parse-log", dict(params, parser="broken"), "environment")
        self.assertIn("modules.ledger.parsers.broken.count names no (?P<value>...) group", broken.summary)
        unknown = self.submit("ledger.parse-log", dict(params, parser="nope"), "usage")
        self.assertIn("no parser nope", unknown.summary)

    def test_a_reader_turns_each_entry_of_a_file_into_a_run(self):
        tree_a, tree_b = "a1" * 20, "b2" * 20
        passes = write_json(self.d.data / "json" / "passes.json", {
            tree_a: {"commit": "c" * 40, "seconds": 57.1, "utc": "2026-11-20T04:21:23Z",
                     "tests": {"unit": {"tests": 595, "failures": 0}, "lab": {"tests": 106, "failures": 1}}},
            tree_b: {"commit": "d" * 40, "seconds": 12, "utc": "2026-11-21T00:00:00Z", "tests": {}}})
        first = self.submit("ledger.import-json", {"reader": "passes", "folder": str(passes)})
        self.assertEqual((first.data.files, first.data.runs), (1, 2))
        self.assertEqual(added(first.data.imported), {"run": 2, "run_input": 2, "measurement": 6})
        shown = self.submit("ledger.show", {"ref": f"passes/{tree_a}"})
        self.assertEqual(shown.data.run["date"], "2026-11-20")
        self.assertEqual(shown.data.inputs, [{"name": "commit", "value": "c" * 40, "sha256": None}])
        self.assertEqual(self.measured(f"passes/{tree_a}"), {
            ("passes", "seconds", "", None, 57.1, "s"), ("passes", "tests", "unit", None, 595.0, None),
            ("passes", "tests", "lab", None, 106.0, None), ("passes", "failures", "unit", None, 0.0, None),
            ("passes", "failures", "lab", None, 1.0, None)})
        self.assertEqual(self.measured(f"passes/{tree_b}"), {("passes", "seconds", "", None, 12.0, "s")})
        again = self.submit("ledger.import-json", {"reader": "passes", "folder": str(passes)})
        self.assertEqual(added(again.data.imported), {})
        self.assertIn("nothing new", again.summary)

    def test_a_reader_names_parts_by_field_and_refuses_what_it_does_not_read(self):
        folder = self.d.data / "telemetry" / "11-21"
        record = {"schema": "demo.telemetry.v1", "startedUtc": "2026-11-21T02:56:03Z", "passed": True,
                  "machine": {"gpu": "Test GPU", "os": "Test OS"}, "versions": {"app": {"sha256": "e" * 64}},
                  "process": {"server": {"launchToReadySeconds": 33.1}, "client": {"launchToReadySeconds": 22.8}},
                  "scenarios": [{"scenario": "full-view", "tick": {"p95": 41.3}},
                                {"scenario": "flight", "tick": {"p95": 12.5}},
                                {"scenario": "flight", "tick": {"p95": 13.5}}, {"tick": {"p95": "n/a"}}]}
        write_json(folder / "025603Z-capture.json", record)
        write_json(folder / "030000Z-other.json", dict(record, schema="other.v2"))
        done = self.submit("ledger.import-json", {"reader": "telemetry", "folder": str(folder.parent)}, "finding")
        self.assertEqual((done.data.files, done.data.runs), (2, 1))
        self.assertIn("030000Z-other.json: is not what reader telemetry reads: its schema is 'other.v2', not "
                      "'demo.telemetry.v1'", done.summary)
        shown = self.submit("ledger.show", {"ref": "telemetry/11-21/025603Z-capture"})
        self.assertEqual((shown.data.run["date"], shown.data.run["card"], shown.data.run["os"]),
                         ("2026-11-21", "Test GPU", "Test OS"))
        self.assertEqual(shown.data.inputs, [{"name": "build", "value": "e" * 64, "sha256": "e" * 64}])
        self.assertEqual(self.measured("telemetry/11-21/025603Z-capture"), {
            ("telemetry", "passed", "", None, 1.0, None),
            ("telemetry", "tick_p95_ms", "full-view", None, 41.3, "ms"),
            ("telemetry", "tick_p95_ms", "flight", 1, 12.5, "ms"),
            ("telemetry", "tick_p95_ms", "flight", 2, 13.5, "ms"),
            ("telemetry", "ready_seconds", "server", None, 33.1, "s"),
            ("telemetry", "ready_seconds", "client", None, 22.8, "s")})
        wrong = self.submit("ledger.import-json", {"reader": "wrong", "folder": str(folder)}, "environment")
        self.assertIn("modules.ledger.readers.wrong.fields: colour not a run field", wrong.summary)
        unknown = self.submit("ledger.import-json", {"reader": "nope", "folder": str(folder)}, "usage")
        self.assertIn("no reader nope", unknown.summary)


class Readers(unittest.TestCase):
    """A JSON reader's config is checked before any file is read, each mistake named."""

    def test_mistakes_in_a_reader_are_named(self):
        sys.path.insert(0, str(REPO / "modules" / "ledger"))
        from wb_ledger import importers
        base = {"label": "x/{@key}", "each": "runs.*", "measurements": {"m": "v"}}
        for change, said in (({"each": "runs"}, "each must take entries"),
                             ({"label": "x/{runs.*}"}, "takes no * or [field]"),
                             ({"label": ""}, "label must name the run"),
                             ({"date": "a.*"}, "date: 'a.*' must reach one value"),
                             ({"measurements": {}}, "must name at least one metric"),
                             ({"measurements": {"m": {"path": "v", "scale": 2}}}, "unknown key(s) scale"),
                             ({"measurements": {"m": "a..b"}}, "has an empty step"),
                             ({"measurements": {"m": "a.[x"}}, "must be [field]"),
                             ({"inputs": {"i": "a.@key"}}, "@key is a path of its own"),
                             ({"fields": {"date": "d"}}, "date has its own key"),
                             ({"colour": 1}, "unknown key(s) colour")):
            with self.subTest(change=change):
                with self.assertRaises(ValueError) as caught:
                    importers.reader({"r": dict(base, **change)}, "r")
                self.assertIn(said, str(caught.exception))
        self.assertEqual(importers.reader({"r": base}, "r").each, ["runs", "*"])


if __name__ == "__main__":
    unittest.main()
