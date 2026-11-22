"""The ledger: recording runs and findings, pinning evidence, and importing Quadrature's results database."""

import shutil
import sqlite3
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO / "tests"))
from harness import Daemon  # noqa: E402

import os  # noqa: E402

QUADRATURE = Path(os.environ.get("WB_TEST_QUADRATURE_RESULTS", "-"))  # a results.sqlite to import
SCHEMA = (REPO / "modules" / "ledger" / "wb_ledger" / "migrations" / "001_tables.sql").read_text(encoding="utf-8")
TABLES = ("machine", "api_call", "offer", "rental", "run", "measurement", "finding", "finding_run", "charge")

OWN_CALLS = ("INSERT INTO ledger_api_call (id, time, provider, method, endpoint, purpose) VALUES "
             "(1, '2026-01-01T00:00:00.000Z', 'vast', 'GET', '/instances/', 'status'), "
             "(2, '2026-01-01T00:01:00.000Z', 'vast', 'GET', '/instances/', 'status')")
SOURCE_CALLS = ("INSERT INTO api_call (id, time, provider, method, endpoint, purpose) VALUES "
                "(1, '2025-06-01T00:00:00.000Z', 'vast', 'PUT', '/bundles/', 'market'), "
                "(2, '2025-06-01T00:05:00.000Z', 'vast', 'PUT', '/bundles/', 'market'), "
                "(3, '2025-06-01T00:10:00.000Z', 'vast', 'PUT', '/bundles/', 'market');")
# Which call each offer came from, by what the call is rather than its id: {t} goes before each table name.
OFFER_CALLS = "SELECT o.offer, c.time FROM {t}offer o JOIN {t}api_call c ON c.id = o.call_id"


def nonzero(counts: dict) -> dict:
    return {table: n for table, n in counts.items() if n}


def added(imported: dict) -> dict:
    """The rows an import added, per table."""
    return nonzero({table: imported[table] for table in TABLES if table in imported})


class Ledger(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = Daemon().start()
        cls.c = cls.d.client

    @classmethod
    def tearDownClass(cls):
        cls.d.stop()

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

    def test_query_is_read_only(self):
        result = self.c.submit("ledger.query", {"sql": "DELETE FROM ledger_run"}).result(timeout=60)
        self.assertEqual(result.outcome, "usage")
        self.assertIn("readonly", result.summary.replace(" ", "").lower())

    @unittest.skipUnless(QUADRATURE.exists(), "Quadrature's results database is not on this machine")
    def test_import_quadrature_results(self):
        copy = self.d.data / "results.sqlite"
        source = sqlite3.connect(f"{QUADRATURE.as_uri()}?mode=ro", uri=True)
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


class Import(unittest.TestCase):
    """Importing a results database into a ledger that holds rows of its own under the ids the source uses."""

    def setUp(self):
        self.d = Daemon().start()
        self.c = self.d.client
        self.db = self.d.workbench.db

    def tearDown(self):
        self.d.stop()

    def source(self, rows: str, name: str = "results.sqlite") -> Path:
        """A results database: the ledger's tables under their own names, holding ``rows``."""
        path = self.d.data / name
        connection = sqlite3.connect(path)
        connection.executescript(SCHEMA.replace("ledger_", "") + rows)
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


if __name__ == "__main__":
    unittest.main()
