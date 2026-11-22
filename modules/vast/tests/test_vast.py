"""The rental flow end to end against a fake Vast API and a fake machine: approval, the price check, destroy
in finally, the dead man's switch, ledger records, the start-up clean-up."""

import os
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO / "tests"))
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(REPO / "modules" / "ledger"))

import fake_vast  # noqa: E402
from harness import Daemon  # noqa: E402

from wb import ApprovalRequired  # noqa: E402
from wb_vast.api import branch_range, numeric_driver  # noqa: E402


def make_daemon(api_url: str, root: Path | None = None) -> Daemon:
    os.environ["WB_VAST_FAKE_TRANSPORT"] = "fake_vast:make"
    os.environ["PYTHONPATH"] = str(HERE) + os.pathsep + os.environ.get("PYTHONPATH", "")
    import tempfile
    root = root or Path(tempfile.mkdtemp(prefix="wb-vast-"))
    (root / "secrets").mkdir(parents=True, exist_ok=True)
    (root / "secrets" / "key").write_text("fake-key", encoding="ascii")
    (root / "secrets" / "ssh").write_text("not a real key", encoding="ascii")
    config = f"""
        [secrets.vast_api_key]
        host = '{root / "secrets" / "key"}'
        [secrets.vast_ssh_key]
        host = '{root / "secrets" / "ssh"}'
        [modules.vast]
        api = "{api_url}"
        poll_seconds = 0.02
        ssh_retry_seconds = 0.02
        cards = ["RTX 3060"]
        """
    return Daemon(config, root=root)


def write_bundle(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "payload.tar").write_bytes(b"x" * 1024)
    (folder / "run.sh").write_text("#!/bin/bash\necho RUN done\n", encoding="utf-8")
    (folder / "bundle.toml").write_text(
        'name = "test-bundle"\n[needs]\njdk = ["26"]\ngpu = true\n[[upload]]\npath = "payload.tar"\n'
        '[run]\nscript = "run.sh"\ndone = "RUN done"\nfailed = \'^RUN tour .* exit [^0]\'\n', encoding="utf-8")
    return folder


class Drivers(unittest.TestCase):
    def test_numeric_driver_matches_the_cli(self):
        self.assertEqual(numeric_driver("580.95.05"), 580095005)
        self.assertEqual(branch_range("R580"), (580000000, 581000000))


class Rental(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server, cls.url = fake_vast.serve()
        cls.d = make_daemon(cls.url).start()
        cls.c = cls.d.client
        cls.bundle = write_bundle(cls.d.data / "bundle-01")

    @classmethod
    def tearDownClass(cls):
        cls.d.stop()
        cls.server.shutdown()

    def setUp(self):
        fake_vast.STATE.billed_override = None
        fake_vast.STATE.offers["777"]["dph_total"] = 0.082

    def test_commands_are_active(self):
        names = {c["name"]: c for c in self.c.commands()}
        self.assertTrue(names["vast.rent"]["active"], names["vast.rent"]["reasons"])
        self.assertTrue(names["vast.rent"]["spends"])

    def test_offers(self):
        result = self.c.submit("vast.offers", {"gpu": "RTX 3060", "max_dph": 0.2}).result(timeout=60)
        self.assertEqual(result.outcome, "ok", result.summary)
        self.assertEqual(result.data.offers[0]["id"], 777)
        calls = self.c.submit("ledger.calls", {}).result(timeout=60)
        self.assertTrue(any(c["endpoint"] == "/bundles/" for c in calls.data.calls))

    def test_bundle_declares_its_own_stop(self):
        import sys as _sys
        from wb_vast import bundle as bundles
        loaded = bundles.load(self.bundle, lambda v: v)
        self.assertEqual(loaded.stop, "pkill -f '/root/run.sh'")  # nothing of Riven's by default
        self.assertIsNone(loaded.partial)

    def test_bundle_check_shows_a_pinned_bootstrap(self):
        result = self.c.submit("vast.bundle-check", {"bundle": str(self.bundle)}).result(timeout=60)
        self.assertEqual(result.outcome, "ok", result.summary)
        self.assertIn("sha256sum -c", result.data.bootstrap)
        self.assertIn("/opt/jdk/26", result.data.bootstrap)
        self.assertNotIn("/opt/jdk/8", result.data.bootstrap)

    def test_rent_waits_for_approval_then_runs_and_destroys(self):
        asks = len(fake_vast.STATE.calls)
        job = self.c.submit("vast.rent", {"offer": "777", "bundle": str(self.bundle), "max_dph": 0.1, "cap": "30m"})
        with self.assertRaises(ApprovalRequired) as raised:
            job.result(timeout=60)
        quote = raised.exception.quote
        priced = 0.082 + fake_vast.storage_dph(fake_vast.STATE.offers["777"], 32)  # the price with the bundle's disk
        self.assertEqual(quote["dph"], priced)
        self.assertEqual(quote["cap_minutes"], 30)
        self.assertAlmostEqual(quote["max_cost"], priced / 2, places=3)
        self.assertIn(f"${priced:.3f}/h", raised.exception.prompt["question"])
        searches = [c[2] for c in fake_vast.STATE.calls[asks:] if c[1] == "/bundles/"]
        self.assertTrue(searches and all(b.get("allocated_storage") == 32 for b in searches))  # priced as billed
        before = len(fake_vast.STATE.destroyed)
        self.assertFalse(any(c[1].startswith("/asks/") for c in fake_vast.STATE.calls[asks:]),
                         "nothing rented before a yes")
        job.approve(quote["id"], via="relayed")
        result = job.result(timeout=120)
        self.assertEqual(result.outcome, "ok", result.summary)
        self.assertEqual(result.data.outcome, "completed")
        self.assertEqual(len(fake_vast.STATE.destroyed), before + 1)
        self.assertEqual(result["approval"]["approved"]["via"], "relayed")
        self.assertIsNotNone(result["cost"])
        names = {o["name"] for o in result["outputs"]}
        self.assertIn("results.tar", names)
        remote_log = (self.d.state / "jobs" / job.id / "work" / "fake-remote.log").read_text(encoding="utf-8")
        self.assertIn("kill -TERM 1", remote_log)  # the dead man's switch was armed
        costs = self.c.submit("ledger.costs", {}).result(timeout=60)
        self.assertTrue(any(r["job"] == job.id for r in costs.data.rentals))
        label = [c for c in fake_vast.STATE.calls if c[0] == "PUT" and c[1] == "/asks/777/"][-1][2]["label"]
        self.assertEqual(label, f"wb-{self.d.workbench.install_id}-{job.id}")

    def test_an_unrounded_price_is_not_a_rise(self):
        price = 0.042222222222222223  # more decimals than a quote prints (cal-02 met this on 2026-11-22)
        fake_vast.STATE.offers["777"]["dph_total"] = price
        job = self.c.submit("vast.rent", {"offer": "777", "bundle": str(self.bundle), "max_dph": 0.1, "cap": "30m"})
        with self.assertRaises(ApprovalRequired) as raised:
            job.result(timeout=60)
        self.assertGreaterEqual(raised.exception.quote["max_cost"], price / 2)  # the most it can cost rounds up
        job.approve(raised.exception.quote["id"], via="relayed")
        result = job.result(timeout=120)  # rents at once: an unchanged price is not re-quoted
        self.assertEqual(result.outcome, "ok", result.summary)

    def test_price_rise_means_a_new_quote(self):
        job = self.c.submit("vast.rent", {"offer": "777", "bundle": str(self.bundle), "max_dph": 0.2, "cap": "30m"})
        with self.assertRaises(ApprovalRequired) as raised:
            job.result(timeout=60)
        fake_vast.STATE.offers["777"]["dph_total"] = 0.095
        asks_before = sum(1 for c in fake_vast.STATE.calls if c[1] == "/asks/777/")
        job.approve(raised.exception.quote["id"])
        with self.assertRaises(ApprovalRequired) as again:
            job.result(timeout=60)
        self.assertEqual(again.exception.quote["dph"], 0.095 + fake_vast.storage_dph(fake_vast.STATE.offers["777"], 32))
        self.assertEqual(asks_before, sum(1 for c in fake_vast.STATE.calls if c[1] == "/asks/777/"))
        job.cancel()
        self.assertEqual(job.wait(timeout=30).state, "cancelled")

    def test_billed_above_quote_destroys(self):
        fake_vast.STATE.billed_override = 0.5
        job = self.c.submit("vast.rent", {"offer": "777", "bundle": str(self.bundle), "max_dph": 0.1, "cap": "30m"})
        with self.assertRaises(ApprovalRequired) as raised:
            job.result(timeout=60)
        before = len(fake_vast.STATE.destroyed)
        job.approve(raised.exception.quote["id"])
        result = job.result(timeout=120)
        self.assertEqual(result.outcome, "environment")
        self.assertIn("billed", result.summary)
        self.assertEqual(len(fake_vast.STATE.destroyed), before + 1)

    def _rows(self, purpose: str, count: int):
        import time as _time
        from wb_ledger import store
        connection = store.connect(self.d.state / "db" / "wb.sqlite")
        connection.execute("INSERT INTO ledger_api_call (time, provider, method, endpoint, result_count, purpose) "
                           "VALUES (?, 'vast', 'POST', '/bundles/', ?, ?)",
                           (_time.strftime("%Y-%m-%dT%H:%M:%S.000Z", _time.gmtime()), count, purpose))
        connection.commit()
        return connection

    def test_market_allowance_is_paced_and_keeps_70_percent_for_work(self):
        from wb_vast import commands
        os.environ["WB_STATE_DIR"] = str(self.d.state)
        noon = 86400 * 20500 + 43200  # 12:00 UTC on some day
        base = commands.market_allowance({}, now=noon)
        self.assertEqual(base["share"], 0.3)
        used = base["market_used"]
        self.assertEqual(base["available"], int(20000 * 0.3 * 0.5) - used)  # half the day: half the 30%
        late = commands.market_allowance({}, now=noon + 43199)
        self.assertLessEqual(late["available"], 6000 - used)  # never past 30% of the day
        connection = self._rows("find", 5000)  # work uses its own 70%
        self.assertEqual(commands.market_allowance({}, now=noon)["available"], base["available"])
        connection.execute("DELETE FROM ledger_api_call WHERE purpose = 'find' AND result_count = 5000")
        connection.commit()

    def test_market_skips_past_its_share_and_rotates_cards(self):
        connection = self._rows("market", 6000)  # the whole day's market share already used
        skipped = self.c.submit("vast.market", {}).result(timeout=60)
        self.assertEqual(skipped.outcome, "ok", skipped.summary)
        self.assertTrue(skipped.data.skipped)
        connection.execute("DELETE FROM ledger_api_call WHERE purpose = 'market' AND result_count = 6000")
        connection.commit()
        ran = self.c.submit("vast.market", {"gpus": ["RTX 3060", "RTX 4060", "RTX 5060"]}).result(timeout=60)
        if ran.data.skipped:  # just after 00:00 UTC the paced allowance can be under one card's page
            self.skipTest(ran.summary)
        self.assertEqual(ran.data.cards[0], "RTX 3060")
        again = self.c.submit("vast.market", {"gpus": ["RTX 3060", "RTX 4060", "RTX 5060"]}).result(timeout=60)
        if not again.data.skipped and len(ran.data.cards) < 3:
            self.assertNotEqual(again.data.cards[0], ran.data.cards[0])  # it continues where the last run stopped

    def test_charges_recorded(self):
        result = self.c.submit("vast.charges", {"days": 7}).result(timeout=60)
        self.assertEqual(result.outcome, "ok", result.summary)
        rows = self.c.submit("ledger.query", {"sql": "SELECT count(*) AS n FROM ledger_charge"}).result(timeout=60)
        self.assertGreaterEqual(rows.data.rows[0][0], 1)


class StartupCleanup(unittest.TestCase):
    def test_only_this_installations_orphans_are_destroyed_at_start(self):
        server, url = fake_vast.serve()
        d = make_daemon(url)
        mine = f"wb-{d.workbench.install_id}-j_OLD"
        fake_vast.STATE.instances["99"] = {"id": 99, "label": mine, "actual_status": "running"}
        fake_vast.STATE.instances["98"] = {"id": 98, "label": "someone-else", "actual_status": "running"}
        fake_vast.STATE.instances["97"] = {"id": 97, "label": "wb-0ther1d-j_THEIRS", "actual_status": "running"}
        d.start()
        try:
            self.assertIn("99", fake_vast.STATE.destroyed)
            self.assertNotIn("98", fake_vast.STATE.destroyed)
            self.assertNotIn("97", fake_vast.STATE.destroyed)  # another installation on the same account
        finally:
            d.stop()
            server.shutdown()
            for key in ("98", "97"):
                fake_vast.STATE.instances.pop(key, None)


if __name__ == "__main__":
    unittest.main()
