"""The rental flow end to end against a fake Vast API and a fake machine: approval, the price check, destroy
in finally, the dead man's switch, ledger records, the start-up clean-up; and bundles made from a launch file. Every
API request goes to the fake service: nothing is searched for or rented for real."""

import hashlib
import json
import os
import shutil
import sys
import tempfile
import textwrap
import tomllib
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO / "tests"))
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(REPO / "modules" / "ledger"))
sys.path.insert(0, str(REPO / "modules" / "build"))

import fake_vast  # noqa: E402
from harness import Daemon  # noqa: E402

from wb import ApprovalRequired  # noqa: E402
from wb_core.module import Context, ParamError  # noqa: E402
from wb_vast import commands  # noqa: E402
from wb_vast.api import branch_range, numeric_driver  # noqa: E402


def make_daemon(api_url: str, root: Path | None = None, vast: str = "", extra: str = "",
                files: dict[str, str] | None = None) -> Daemon:
    """A Workbench whose vast module talks to the fake API and reaches machines through the fake transport.
    ``vast`` adds keys to [modules.vast]; ``extra`` adds tables; ``files`` adds config files."""
    os.environ["WB_VAST_FAKE_TRANSPORT"] = "fake_vast:make"
    os.environ["PYTHONPATH"] = str(HERE) + os.pathsep + os.environ.get("PYTHONPATH", "")
    root = root or Path(tempfile.mkdtemp(prefix="wb-vast-"))
    (root / "secrets").mkdir(parents=True, exist_ok=True)
    (root / "secrets" / "key").write_text("fake-key", encoding="ascii")
    (root / "secrets" / "ssh").write_text("not a real key", encoding="ascii")
    config = textwrap.dedent(f"""
        [secrets.vast_api_key]
        host = '{root / "secrets" / "key"}'
        [secrets.vast_ssh_key]
        host = '{root / "secrets" / "ssh"}'
        [modules.vast]
        api = "{api_url}"
        poll_seconds = 0.02
        ssh_retry_seconds = 0.02
        ssh_settle_seconds = 0.02
        cards = ["RTX 3060"]
        """) + textwrap.dedent(vast) + textwrap.dedent(extra)
    return Daemon(config, root=root, files=files)


def write_bundle(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "payload.tar").write_bytes(b"x" * 1024)
    (folder / "run.sh").write_bytes(b"#!/bin/bash\necho RUN done\n")  # LF on every OS, as a rental needs
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

    def test_the_template_route_answers_with_bundle_launch(self):
        names = {c["name"] for c in self.c.commands()}
        for retired in ("vast.bundle", "vast.lab-inputs"):
            self.assertNotIn(retired, names)
            with self.assertRaises(Exception) as raised:
                self.c.submit(retired, {})
            self.assertIn(f"{retired} is retired; use vast.bundle-launch", str(raised.exception))

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
        self.assertEqual(loaded.stop, "pkill -f '/root/run.sh'")  # no launcher's name built in
        self.assertIsNone(loaded.partial)

    def test_a_script_with_windows_line_endings_is_refused_before_renting(self):
        folder = write_bundle(self.d.data / "bundle-crlf")
        (folder / "run.sh").write_bytes(b"#!/bin/bash\r\necho RUN done\r\n")  # as autocrlf checks it out
        checked = self.c.submit("vast.bundle-check", {"bundle": str(folder)}).result(timeout=60)
        self.assertEqual(checked.outcome, "usage")
        self.assertIn("run.sh has Windows line endings (CRLF)", checked.summary)

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

    def test_ssh_must_answer_three_times_in_a_row_before_the_rental_is_armed(self):
        # A container's sshd often restarts once just after it first answers; the next command then loses its
        # connection. The rental waits until SSH answers three times in a row before it arms anything.
        os.environ[fake_vast.SSH_ENV] = "ok,fail,ok,ok,ok"
        self.addCleanup(os.environ.pop, fake_vast.SSH_ENV, None)
        job = self.c.submit("vast.rent", {"offer": "777", "bundle": str(self.bundle), "max_dph": 0.1, "cap": "30m"})
        with self.assertRaises(ApprovalRequired) as raised:
            job.result(timeout=60)
        job.approve(raised.exception.quote["id"], via="relayed")
        result = job.result(timeout=120)
        self.assertEqual(result.outcome, "ok", result.summary)
        lines = (self.d.state / "jobs" / job.id / "work" / "fake-remote.log").read_text(encoding="utf-8").splitlines()
        armed = next(i for i, line in enumerate(lines) if "kill -TERM 1" in line)
        self.assertEqual([line for line in lines[:armed] if line == "run true"], ["run true"] * 5)

    def test_a_host_that_refuses_the_key_gets_it_again(self):
        os.environ[fake_vast.SSH_ENV] = ",".join(["denied"] * 5 + ["ok"] * 3)
        self.addCleanup(os.environ.pop, fake_vast.SSH_ENV, None)
        job = self.c.submit("vast.rent", {"offer": "777", "bundle": str(self.bundle), "max_dph": 0.1, "cap": "30m"})
        with self.assertRaises(ApprovalRequired) as raised:
            job.result(timeout=60)
        before = len(fake_vast.STATE.calls)
        job.approve(raised.exception.quote["id"], via="relayed")
        result = job.result(timeout=120)
        self.assertEqual(result.outcome, "ok", result.summary)
        attaches = [c for c in fake_vast.STATE.calls[before:] if c[0] == "POST" and c[1].endswith("/ssh/")
                    or c[0] == "POST" and c[1].endswith("/ssh")]
        self.assertEqual(len(attaches), 2, attaches)  # the first attach, and one more after four refusals

    def test_an_unrounded_price_is_not_a_rise(self):
        price = 0.042222222222222223  # more decimals than a quote prints (a real offer was priced so)
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

    def test_a_clean_up_that_cannot_list_instances_shows_in_health(self):
        server, url = fake_vast.serve()
        d = make_daemon(url)
        fake_vast.STATE.listing_gone = True  # the provider retired the listing, as it did with /api/v0/instances/
        d.start()
        try:
            failed = d.client.health()["hooks_failed"]
            self.assertIn("vast", failed)
            self.assertEqual(failed["vast"]["outcome"], "environment")
            self.assertIn("could not list instances", failed["vast"]["summary"])
        finally:
            fake_vast.STATE.listing_gone = False
            d.stop()
            server.shutdown()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class LaunchBundles(unittest.TestCase):
    """vast.bundle-launch: a bundle from a launch file alone. Every member and runtime file by SHA-256 from the Store,
    the JDK as the platform's Linux archive, Workbench's own lab code and a run script; every load checks it against
    its launch file, so a member or JDK archive that is missing or other bytes is refused before any quote."""

    BUILD = "26.0.2.1+1"

    def setUp(self):
        from wb_vast import launch_bundle
        self.launch_bundle = launch_bundle
        self.root = Path(tempfile.mkdtemp(prefix="wb-launch-bundle-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.store = self.root / "out" / "store"
        members = []
        for spec, file, data in (("game:mod@both", "game-1.jar", b"the game"), ("cube:mod@both", "cube-1.jar", b"cube"),
                                 ("scalar@both", "Scalar Legacy-1.0.1.jar", b"a file name with a space")):
            self.keep(file, data)
            members.append({"member": spec, "name": spec.split(":")[0].split("@")[0], "side": "both",
                            "kind": "line", "commit": "c" * 40, "sha256": sha(data), "file": file,
                            "path": f"files/{sha(data)}/{file}", "mod_ids": []})
        runtime = {}
        for part, file, data in (("installer", "cleanroom-installer.jar", b"installer"),
                                 ("universal", "cleanroom-universal.jar", b"universal"),
                                 ("prism", "cleanroom.zip", b"prism")):
            if part != "universal":  # the installer makes it
                self.keep(file, data)
            runtime[part] = {"url": f"https://example.org/{file}", "file": file, "sha256": sha(data)}
        self.jdk = b"a jdk archive"
        self.keep("OpenJDK26U-jdk_x64_linux_hotspot_26.0.2.1_1.tar.gz", self.jdk)
        revision = "5" * 40
        (self.root / "out" / "sets" / revision).mkdir(parents=True)
        (self.root / "out" / "sets" / revision / "set.json").write_text(json.dumps({
            "schema": 1, "projects": {}, "platform": {"java": 26, "jdk": {"26": {"build": self.BUILD, "linux": {
                "url": "https://example.org/jdk-26.0.2.1%2B1/OpenJDK26U-jdk_x64_linux_hotspot_26.0.2.1_1.tar.gz",
                "sha256": sha(self.jdk)}}}}}), encoding="utf-8")
        self.doc = {"schema": 1, "project": "game", "launch": "default", "commit": "a" * 40, "tree": "b" * 40,
                    "set": revision, "store": str(self.store),
                    "runtime": {"kind": "cleanroom", "version": "0.7.14-alpha", "java": 26, "jdk": self.BUILD,
                                "files": runtime},
                    "members": members, "jvm": {"client": [], "server": []}, "collect": [], "failures": [],
                    "params": {"seed": 1}, "param_types": {"seed": {"type": "int"}},
                    "tour": {"captured": "view captured"}}
        self.tour = self.root / "views.json"
        self.tour.write_text('[{"label": "a", "x": 0, "y": 80, "z": 0, "yaw": 0, "pitch": 0}]', encoding="utf-8")
        self.lab = {"trusted": [], "eula": True}

    def keep(self, name: str, data: bytes):
        path = self.store / "files" / sha(data) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def make(self, doc=None, **params):
        launch = self.root / "launch.json"
        launch.write_text(json.dumps(doc or self.doc, indent=1) + "\n", encoding="utf-8")
        out = self.root / "bundle"
        ctx = Context({"job": "j", "command": "vast.bundle-launch", "module": "vast", "work": str(self.root / "work"),
                       "out": str(out), "lab": self.lab, "machine": "host"}, lambda event: None)
        values = {"name": None, "window": "1920x1080", "gpu": True, "eula": False, "params": {}, "limit": 600,
                  "disk_gb": 32, "purpose": "calibration", **params}
        result = self.launch_bundle.make(ctx, str(launch), str(self.tour), readable=Path, **values)
        return ctx, result, out

    def check(self, out: Path) -> dict:
        ctx = Context({"job": "c", "command": "vast.bundle-check", "module": "vast", "work": str(self.root / "cw"),
                       "out": str(self.root / "co")}, lambda event: None)
        return commands.bundle_check(ctx, str(out))

    def test_the_bundle_carries_every_file_by_sha256_and_the_lab_code(self):
        ctx, result, out = self.make()
        self.assertEqual(ctx.outcome, "ok", ctx.findings)
        files = out / "files"
        self.assertEqual((files / "launch.json").read_bytes(), (self.root / "launch.json").read_bytes())
        for member in self.doc["members"]:
            self.assertEqual(sha(((files / "store") / member["path"]).read_bytes()), member["sha256"])
        for part in ("installer", "prism"):
            entry = self.doc["runtime"]["files"][part]
            self.assertTrue((files / "store" / "files" / entry["sha256"] / entry["file"]).is_file())
        self.assertEqual((files / "jdk" / "OpenJDK26U-jdk_x64_linux_hotspot_26.0.2.1_1.tar.gz").read_bytes(), self.jdk)
        for code in ("runner/wb_lab/instance.py", "runner/wb_lab/run.py", "runner/wb_core/module.py",
                     "runner/wb/config.py", "runner/launch_run.py"):
            self.assertTrue((files / code).is_file(), code)
        self.assertFalse(list(files.rglob("__pycache__")))
        self.assertNotIn(b"\r\n", (files / "run.sh").read_bytes())
        checksums = (files / "checksums.sha256").read_text(encoding="utf-8").splitlines()
        self.assertIn(f"{self.doc['members'][2]['sha256']}  store/{self.doc['members'][2]['path']}", checksums)
        self.assertEqual(len(checksums), 3 + 2 + 1 + 2)  # members, runtime files, the JDK, the launch and tour
        info = json.loads((files / "bundle-launch.json").read_text(encoding="utf-8"))
        self.assertEqual((info["jdk"]["build"], info["instance"], info["eula"], info["launch"]["commit"]),
                         (self.BUILD, "rental", True, "a" * 40))
        toml = tomllib.loads((out / "bundle.toml").read_text(encoding="utf-8"))
        self.assertEqual((toml["needs"]["own_jdk"], toml["needs"]["gpu"], toml["run"]["script"]),
                         (True, True, "files/run.sh"))
        checked = self.check(out)
        self.assertEqual((checked["launch"]["members"], checked["launch"]["jdk"]), (3, self.BUILD))
        self.assertNotIn("/opt/jdk", checked["bootstrap"])  # the bundle's own JDK, not the image's
        self.assertIn("virtualgl", checked["bootstrap"].lower())

    def test_a_member_or_jdk_archive_missing_or_changed_is_refused_before_any_quote(self):
        _, _, out = self.make()
        member = out / "files" / "store" / self.doc["members"][1]["path"]
        member.write_bytes(b"other bytes")
        with self.assertRaisesRegex(ParamError, r"member cube:mod@both: store/files/.*/cube-1.jar is [0-9a-f]{12}, "
                                                r"not [0-9a-f]{12}"):
            self.check(out)
        member.unlink()
        with self.assertRaisesRegex(ParamError, r"member cube:mod@both: the bundle has no store/files/"):
            self.check(out)
        _, _, out = self.make()
        archive = out / "files" / "jdk" / "OpenJDK26U-jdk_x64_linux_hotspot_26.0.2.1_1.tar.gz"
        archive.write_bytes(b"another jdk")
        with self.assertRaisesRegex(ParamError, r"JDK 26.0.2.1\+1: jdk/OpenJDK26U.* is [0-9a-f]{12}, not "):
            self.check(out)
        archive.unlink()
        with self.assertRaisesRegex(ParamError, r"JDK 26.0.2.1\+1: the bundle has no jdk/OpenJDK26U"):
            self.check(out)
        # the quote loads it the same way: nothing is asked of Vast
        with self.assertRaisesRegex(ParamError, r"the bundle has no jdk/"):
            commands._bundle(Context({"job": "q", "command": "vast.rent", "module": "vast",
                                      "work": str(self.root / "qw"), "out": str(self.root / "qo")},
                                     lambda event: None), str(out))

    def test_a_bundle_file_changed_or_added_since_it_was_made_is_refused(self):
        _, _, out = self.make()
        script = out / "files" / "run.sh"
        script.write_bytes(script.read_bytes() + b"echo changed\n")
        with self.assertRaisesRegex(ParamError, r"files/run.sh changed since the bundle was made"):
            self.check(out)
        _, _, out = self.make()  # making it again replaces what it made
        self.assertIn("launch", self.check(out))
        (out / "files" / "added.txt").write_text("x", encoding="utf-8")
        with self.assertRaisesRegex(ParamError, r"files/added.txt is not in its manifest.json"):
            self.check(out)

    def test_what_the_store_lacks_is_a_finding_and_nothing_is_written(self):
        doc = json.loads(json.dumps(self.doc))
        doc["members"][0]["sha256"] = "e" * 64
        doc["members"][0]["path"] = f"files/{'e' * 64}/game-1.jar"
        shutil.rmtree(self.store / "files" / sha(self.jdk))
        ctx, result, out = self.make(doc)
        self.assertEqual(ctx.outcome, "finding")
        self.assertRegex(result["problems"][0], r"member game:mod@both: the Store has no files/e{64}/game-1.jar "
                                                r"\(wb check in game at cccccccccccc\)")
        self.assertRegex(result["problems"][1], r"JDK 26.0.2.1\+1: the Store has no Linux archive OpenJDK26U.*"
                                                r"store.platform revision=5{40}")
        self.assertFalse((out / "bundle.toml").exists())

    def test_a_configure_script_of_a_project_not_trusted_is_not_carried(self):
        doc = {**self.doc, "configure": {"project": "game", "commit": "a" * 40, "path": "launch/configure.py",
                                         "blob": "f" * 40}}
        ctx, result, _ = self.make(doc)
        self.assertRegex(result["problems"][0], r"configure script launch/configure.py is game's code, and game is not "
                                                r"in \[lab\] trusted")

    def test_a_bundle_without_the_eula_is_refused_before_the_quote(self):
        self.lab["eula"] = False
        ctx, result, out = self.make()
        self.assertFalse(result["eula"])
        loaded = commands._bundle(Context({"job": "q", "command": "vast.rent", "module": "vast",
                                           "work": str(self.root / "qw"), "out": str(self.root / "qo")},
                                          lambda event: None), str(out))
        with self.assertRaisesRegex(ParamError, r"records no acceptance of the Minecraft EULA.*Nothing was quoted"):
            commands._launch_ready(loaded)
        self.assertIn("no EULA acceptance", self.check_summary(out))

    def check_summary(self, out: Path) -> str:
        ctx = Context({"job": "c", "command": "vast.bundle-check", "module": "vast", "work": str(self.root / "cw"),
                       "out": str(self.root / "co")}, lambda event: None)
        commands.bundle_check(ctx, str(out))
        return ctx.summary_text

    def test_a_tour_for_a_launch_without_tour_rules_is_refused_naming_the_fix(self):
        """lab.run plays a tour only by the launch's tour rules, which launch.json carries from the launch and every
        launch it inherits: a bundle of a tour for a launch with none is refused here, not on the rental."""
        doc = {k: v for k, v in self.doc.items() if k != "tour"}
        with self.assertRaisesRegex(ParamError, r"tour: launch game:default has no tour rules \(tour.captured: .*"
                                                r"would refuse to play views.json; name them in "
                                                r"\[launch.default.tour\] of game's project.toml .*lab.resolve"):
            self.make(doc)
        self.assertFalse((self.root / "bundle" / "bundle.toml").exists())
        inherited = {**doc, "tour": {"setup": ["time set 6000"]},
                     "inherits": [{"name": "base:default", "commit": "d" * 40, "chosen": "line"}]}
        with self.assertRaisesRegex(ParamError, r"none of the launches it inherits gives them "
                                                r"\(base:default at dddddddddddd\)"):
            self.make(inherited)
        ctx, _, _ = self.make({**inherited, "tour": {"captured": "view captured"}})  # inherited rules are enough
        self.assertEqual(ctx.outcome, "ok", ctx.findings)

    def test_a_bundle_without_a_gpu_installs_a_display_and_software_opengl(self):
        """A launch bundle draws: its bootstrap installs Xvfb and the client's libraries, and without a GPU Mesa's
        software OpenGL (llvmpipe) instead of VirtualGL."""
        _, _, out = self.make(gpu=False)
        toml = tomllib.loads((out / "bundle.toml").read_text(encoding="utf-8"))
        self.assertEqual((toml["needs"]["gpu"], toml["needs"]["display"]), (False, True))
        software = self.check(out)["bootstrap"]
        for package in ("xvfb", "libgl1", "libgl1-mesa-dri", "libglx-mesa0", "libegl-mesa0"):
            self.assertRegex(software, rf"apt-get install .* {package}( |$)", package)
        self.assertNotIn("vgl.deb", software)  # no VirtualGL
        _, _, out = self.make(gpu=True)
        gpu = self.check(out)["bootstrap"]
        self.assertIn(" xvfb ", gpu)
        self.assertIn("vgl.deb", gpu)
        self.assertNotIn("libgl1-mesa-dri", gpu)  # the driver's own GL, through VirtualGL

    def test_run_sh_stops_with_a_run_line_when_the_display_does_not_start(self):
        """run.sh starts Xvfb and stops with a RUN line naming its last word when it does not start, instead of
        playing on a display that is not there."""
        import subprocess
        bash = shutil.which("bash")
        if not bash or os.name == "nt":
            self.skipTest("needs bash and a POSIX shell's tools")
        _, _, out = self.make(gpu=False)
        files = out / "files"
        jdk = files / "store" / "jdks" / self.BUILD / "linux"
        jdk.mkdir(parents=True)
        (jdk / "release").write_text("JAVA_VERSION=26", encoding="utf-8")  # already unpacked
        stubs = self.root / "stubs"
        stubs.mkdir()
        for name, body in (("pgrep", "exit 1"),  # no display running yet, whatever this machine has
                           ("Xvfb", "echo '(EE) Fatal server error: stub cannot open the screen' >&2; exit 1"),
                           ("python3", "echo 'RUN built rental from template k (new)'; exit 0")):
            (stubs / name).write_text(f"#!/bin/bash\n{body}\n", encoding="utf-8", newline="\n")
            (stubs / name).chmod(0o755)
        done = subprocess.run([bash, str(files / "run.sh"), str(files)], capture_output=True, text=True,
                              env={**os.environ, "PATH": f"{stubs}{os.pathsep}{os.environ.get('PATH', '')}",
                                   "WB_BUNDLE_LAB": str(self.root / "lab")}, timeout=120)
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("RUN session failed: the display did not start (Xvfb :1): (EE) Fatal server error: stub "
                      "cannot open the screen", done.stdout)
        self.assertNotIn("RUN session tour", done.stdout)
        self.assertTrue((files / "results.tar").is_file())

    def test_the_runner_takes_the_bundles_store_and_sources(self):
        # the runner calls lab.build with the bundle's own Store copy and trees, and nothing of this machine
        _, _, out = self.make()
        sys.path.insert(0, str(out / "files" / "runner"))
        self.addCleanup(sys.path.remove, str(out / "files" / "runner"))
        import importlib.util
        spec = importlib.util.spec_from_file_location("bundle_runner", out / "files" / "runner" / "launch_run.py")
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        seen = {}

        def build(ctx, launch, name, **params):
            seen.update(launch=launch, name=name, lab=ctx.spec["lab"], config=ctx.config, **params)
            folder = Path(ctx.config["instances"]) / name
            folder.mkdir(parents=True)
            (folder / "instance.json").write_text("{}", encoding="utf-8")
            return {"instance": str(folder), "template": "k", "built": True}

        from wb_lab import instance as instances
        from unittest import mock
        with mock.patch.object(instances, "build", build):
            code = runner.main(["build", str(out / "files"), str(self.root / "lab")])
        self.assertEqual(code, 0)
        self.assertEqual((Path(seen["launch"]).name, seen["name"], seen["eula"]), ("launch.json", "rental", True))
        self.assertTrue(Path(seen["store"]).samefile(out / "files" / "store"))
        self.assertEqual(Path(seen["sources"]), (out / "files" / "sources").resolve())
        self.assertEqual(Path(seen["config"]["instances"]), (self.root / "lab" / "instances").resolve())
        self.assertEqual(seen["config"]["asset_base"], "https://resources.download.minecraft.net/")  # lab's defaults
        self.assertTrue((out / "files" / "results" / "instance.json").is_file())

    def test_the_runners_session_takes_the_bundles_jdk(self):
        # the launch file names this machine's Store; the session must run on the JDK the bundle carries
        _, _, out = self.make()
        files = out / "files"
        from unittest import mock
        modules = mock.patch.dict(sys.modules)  # another test's bundle code may be loaded: use this bundle's own
        modules.start()
        self.addCleanup(modules.stop)
        for name in [n for n in sys.modules if n.split(".")[0] in ("wb_lab", "wb_core", "wb")]:
            del sys.modules[name]
        sys.path.insert(0, str(files / "runner"))
        self.addCleanup(sys.path.remove, str(files / "runner"))
        import importlib.util
        spec = importlib.util.spec_from_file_location("bundle_runner_tour", files / "runner" / "launch_run.py")
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        info = json.loads((files / runner.BUNDLE_INFO).read_text(encoding="utf-8"))
        (files / "store" / "jdks" / "26.0.2+7" / "linux" / "jdk-26.0.2+7" / "bin").mkdir(parents=True)  # run.sh unpacks it
        (files / "results").mkdir(exist_ok=True)
        (files / "results" / "instance.json").write_text(json.dumps(
            {"jdk": {"26": {"build": "26.0.2+7"}}, "system": "linux"}), encoding="utf-8")
        seen = {}

        def run(ctx, instance, **params):
            seen.update(params)
            folder = self.root / "session"
            folder.mkdir(exist_ok=True)
            (folder / "session.json").write_text(json.dumps({"tour": {"views": 1, "captured": 1}}),
                                                 encoding="utf-8")
            return {"folder": str(folder), "session": "s", "state": "ended"}

        from wb_lab import run as sessions
        with mock.patch.object(sessions, "run", run):
            code = runner.main(["tour", str(files), str(self.root / "lab")])
        self.assertEqual(code, 0)
        self.assertEqual(Path(seen["jdk"]), files / info["store"] / "jdks" / "26.0.2+7" / "linux" / "jdk-26.0.2+7")



class Memory(unittest.TestCase):
    def test_a_launch_bundle_needs_two_heaps_and_headroom(self):
        from wb_vast import launch_bundle
        doc = {"base": {"definition": {"jvm_profile": {"arguments": ["-Xms512M", "-Xmx8G"]}}}}
        self.assertEqual(launch_bundle.ram_needed(doc), 20)
        self.assertEqual(launch_bundle.ram_needed({"jvm": {"client": ["-Xmx3072M"]}}), 10)
        self.assertEqual(launch_bundle.ram_needed({}), 12)  # no heap named: a 4 GB default each
        self.assertIn("killed by signal", launch_bundle.RUN_SH)

    def test_an_offer_with_too_little_memory_is_refused(self):
        from wb_vast import commands
        from wb_core.module import ParamError
        offer = {"id": 1, "cpu_cores_effective": 10, "cpu_ram": 16 * 1024}
        with self.assertRaises(ParamError) as raised:
            commands._check_offer(None, offer, "1", 0.1, 8, 20)
        self.assertIn("16 GB of memory", str(raised.exception))
        commands._check_offer(None, dict(offer, cpu_ram=32 * 1024), "1", 0.1, 8, 20)
        commands._check_offer(None, dict(offer, cpu_ram=None), "1", 0.1, 8, 20)  # unknown: not refused here

if __name__ == "__main__":
    unittest.main()
