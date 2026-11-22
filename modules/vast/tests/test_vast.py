"""The rental flow end to end against a fake Vast API and a fake machine: approval, the price check, destroy
in finally, the dead man's switch, ledger records, the start-up clean-up; and bundles made from templates in config,
with their input archives built from pinned commits. Every API request goes to the fake service: nothing is
searched for or rented for real."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace

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
from wb_core.module import Context, EnvironmentProblem, ParamError  # noqa: E402
from wb_vast import commands, lab_inputs  # noqa: E402
from wb_vast import template as templates  # noqa: E402
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
        cards = ["RTX 3060"]
        """) + textwrap.dedent(vast) + textwrap.dedent(extra)
    return Daemon(config, root=root, files=files)


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
        self.assertEqual(loaded.stop, "pkill -f '/root/run.sh'")  # no launcher's name built in
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


# ----------------------------------------------------------------------------------------------
# templates, input archives and bundles

def git(cwd, *args) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args], cwd=cwd,
                          check=True, capture_output=True, text=True).stdout.strip()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


MAKE = b"""import pathlib
pathlib.Path("build").mkdir(exist_ok=True)
pathlib.Path("build", "out.txt").write_bytes(b"made from " + pathlib.Path("VERSION").read_bytes().strip())
"""


def make_repo(folder: Path) -> str:
    """A checkout with tracked tools (one executable), and a step that makes a file the tree does not track."""
    (folder / "tools").mkdir(parents=True)
    git(folder, "init", "-q", "-b", "main")
    (folder / "tools" / "launcher.py").write_bytes(b"print('launch')\n")
    (folder / "tools" / "start.sh").write_bytes(b"#!/bin/sh\necho start\n")
    (folder / "VERSION").write_bytes(b"1.0\n")
    (folder / "make.py").write_bytes(MAKE)
    (folder / ".gitignore").write_bytes(b"build/\n")
    git(folder, "add", "-A")
    git(folder, "update-index", "--chmod=+x", "tools/start.sh")
    git(folder, "commit", "-q", "-m", "one")
    return git(folder, "rev-parse", "HEAD")


EXAMPLE = Path(__file__).resolve().parents[1] / "docs" / "example"
GAME_STEPS = [
    {"id": "prepare", "command": "lab.prepare", "role": "prepare",
     "params": {"name": "inst-01", "seed": 7, "args": ["--client-mod", "@JAR@"]}},
    {"id": "settle", "command": "lab.play", "role": "play",
     "params": {"name": "inst-01", "tour": "@TOUR@", "label": "s1", "options": "-Dx=$HOME"}},
    {"id": "measure", "command": "lab.play", "role": "play", "params": {"name": "inst-01", "tour": "@TOUR@"}}]


def game_steps(jar: Path, tour: Path) -> list[dict]:
    return json.loads(json.dumps(GAME_STEPS).replace("@JAR@", jar.as_posix()).replace("@TOUR@", tour.as_posix()))


class Templates(unittest.TestCase):
    """Templates are config: parsed and checked by key, and a plan's game steps written as they declare."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="wb-template-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        (self.root / "mod.jar").write_bytes(b"jar bytes")
        (self.root / "forests.json").write_bytes(b"{}")
        self.steps = game_steps(self.root / "mod.jar", self.root / "forests.json")

    def example(self) -> templates.Template:
        return templates.parse("tours", tomllib.loads((EXAMPLE / "tours.toml").read_text(encoding="utf-8")))

    def test_the_documented_example_writes_its_steps_file(self):
        example = self.example()
        self.assertTrue((EXAMPLE / example.folder.rsplit("/", 1)[-1] / example.script).is_file())
        self.assertEqual(sorted(example.inputs), ["app.tar", "cache.tar", "engine.tar", "pins.tar"])
        self.assertIsNone(example.inputs["cache.tar"].recipe)  # made by hand
        compiled = templates.compile_steps(example, self.steps, {"window": "1280x720"}, "turn-01", lambda v: v)
        self.assertEqual(compiled.problems, [])
        self.assertEqual(compiled.text, textwrap.dedent("""\
            # written by Workbench for bundle turn-01 from template tours
            NAME=inst-01
            SEED=7
            SCALE=1
            PREPARE_ARGS='--client-mod /root/bundle/jars/mod.jar'
            TOURS='forests.json forests.json'
            LABELS='s1 measure'
            OPTIONS_1='-Dx=$HOME'
            OPTIONS_2=''
            WINDOW=1280x720
            GPU=1
            """))  # quoted for the shell: $HOME stays text
        self.assertEqual(sorted(compiled.copies), ["forests.json", "jars/mod.jar"])
        self.assertIn({"step": "settle", "name": "s1-captures", "path": "results/s1-captures"}, compiled.outputs)
        self.assertIn({"step": "measure", "name": "measure-play-log", "path": "results/measure-play.log"},
                      compiled.outputs)
        defaults = templates.compile_steps(example, self.steps, {}, "turn-01", lambda v: v)
        self.assertIn("WINDOW=1920x1080", defaults.text)

    def test_steps_written_as_json(self):
        raw = {"folder": "x", "steps": {"file": "steps.json", "roles": {"play": "1-2"},
                                        "files": [{"role": "play", "param": "tour", "into": "tours"}]}}
        steps = [s for s in self.steps if s["role"] == "play"]
        compiled = templates.compile_steps(templates.parse("j", raw), steps, {"window": "800x600"}, "b", lambda v: v)
        self.assertEqual(compiled.problems, [])
        document = json.loads(compiled.text)
        self.assertEqual(document["schema"], "wb.bundle-steps.v1")
        self.assertEqual([(s["id"], s["n"], s["params"]["tour"]) for s in document["steps"]],
                         [("settle", 1, "tours/forests.json"), ("measure", 2, "tours/forests.json")])
        self.assertEqual(document["values"], {"window": "800x600"})

    def test_steps_a_template_cannot_take_are_refusals_naming_them(self):
        example = self.example()
        steps = self.steps + [dict(self.steps[0], id="again")]
        steps[2] = {**steps[2], "params": {**steps[2]["params"], "name": "other", "tour": str(self.root / "gone.json")}}
        problems = " | ".join(templates.compile_steps(example, steps, {}, "b", lambda v: v).problems)
        self.assertIn("takes exactly 1 prepare step(s), and 2 were given", problems)
        self.assertIn("the same name", problems)
        self.assertIn("gone.json, which is not a file", problems)
        unknown = templates.compile_steps(example, [{"id": "x", "role": "score", "params": {}}], {}, "b", str)
        self.assertIn("takes no score step", " ".join(unknown.problems))
        no_name = [{**s, "params": {k: v for k, v in s["params"].items() if k != "name"}} for s in self.steps]
        no_name[0]["params"]["name"] = "inst-01"
        self.assertIn("gives no name", " ".join(templates.compile_steps(example, no_name, {}, "b", str).problems))
        strict = dict(tomllib.loads((EXAMPLE / "tours.toml").read_text(encoding="utf-8")))
        strict["steps"] = {**strict["steps"], "values": {"SEED": "{prepare.seed}"}}
        unseeded = [{**s, "params": {k: v for k, v in s["params"].items() if k != "seed"}} for s in self.steps]
        missing = templates.compile_steps(templates.parse("t", strict), unseeded, {}, "b", lambda v: v)
        self.assertEqual(missing.problems, ["plan.env: SEED: step prepare gives no prepare.seed"])

    def test_two_files_of_one_name_are_refused(self):
        (self.root / "other").mkdir()
        (self.root / "other" / "forests.json").write_bytes(b"[]")
        steps = game_steps(self.root / "mod.jar", self.root / "forests.json")
        steps[2]["params"]["tour"] = str(self.root / "other" / "forests.json")
        problems = templates.compile_steps(self.example(), steps, {}, "b", lambda v: v).problems
        self.assertTrue(any("already put another file of that name" in p for p in problems), problems)
        same = game_steps(self.root / "mod.jar", self.root / "forests.json")  # one file twice is no conflict
        self.assertEqual(templates.compile_steps(self.example(), same, {}, "b", lambda v: v).problems, [])

    def test_a_check_leaves_unresolved_references_for_the_make(self):
        steps = game_steps(self.root / "mod.jar", self.root / "forests.json")
        steps[1]["params"]["tour"] = "{steps.build.outputs.tour}"
        checked = templates.compile_steps(self.example(), steps, {}, "b", lambda v: v, check_files=False)
        self.assertEqual(checked.problems, [])
        made = templates.compile_steps(self.example(), steps, {}, "b", lambda v: v)
        self.assertTrue(any("which is not a file" in p for p in made.problems), made.problems)

    def test_config_errors_name_the_key(self):
        base = tomllib.loads((EXAMPLE / "tours.toml").read_text(encoding="utf-8"))
        cases = [
            ({"folder": "x", "colour": 1}, "unknown key(s) colour"),
            ({}, "folder is not set"),
            ({"folder": "x", "script": "../run.sh"}, "stays inside it"),
            ({"folder": "x", "run": {"unpack": ["other.tar"]}}, "not the name of a fetched result"),
            ({"folder": "x", "run": {"failed": "("}}, "not a regular expression"),
            ({"folder": "x", "inputs": {"a.tar": {"repo": "r", "artifacts": ["x"]}}}, "missing commit"),
            ({"folder": "x", "inputs": {"a.zip": {"repo": "r", "commit": "abcdef0", "artifacts": ["x"]}}},
             "ends in .tar"),
            ({"folder": "x", "inputs": {"a.tar": {"sha256": "nope"}}}, "not a SHA-256"),
            ({"folder": "x", "inputs": {"a/b.tar": {}}}, "named by its file name"),
            ({"folder": "x", "steps": {"file": "s.env", "roles": {"score": 1}, "values": {"A": "1"}}},
             "score is not a role"),
            ({"folder": "x", "steps": {"file": "s.env", "roles": {"play": "many"}, "values": {"A": "1"}}},
             "a count of steps"),
            ({**base, "steps": {**base["steps"], "values": {"A": "{prepar.seed}"}}}, "names neither a role"),
            ({**base, "steps": {**base["steps"], "values": {"A_{n}": "{values.x}"}}}, "reads exactly one role"),
            ({**base, "steps": {**base["steps"], "values": {"A": "{prepare.name|play.name}"}}}, "reads one role"),
            ({**base, "steps": {**base["steps"], "values": {"A": "{prepare.name"}}}, "unbalanced brace"),
            ({**base, "steps": {**base["steps"], "values": {"A-B": "1"}}}, "not a variable name"),
            ({**base, "outputs": {"{prepare.id}-{play.id}": "x"}}, "reads play and prepare"),
            ({"folder": "x", "steps": {"file": "s.json", "roles": {"play": 1}, "values": {"A": "1"}}},
             "values are for env"),
        ]
        for raw, expected in cases:
            with self.subTest(expected=expected):
                with self.assertRaises(templates.TemplateError) as raised:
                    templates.parse("t", raw)
                self.assertIn(expected, str(raised.exception))
                self.assertIn("modules.vast.templates.t", str(raised.exception))

    def test_a_template_is_named_or_the_only_one(self):
        one = SimpleNamespace(config={"templates": {"only": {"folder": "x"}}})
        self.assertEqual(commands._template(one, "").name, "only")
        two = SimpleNamespace(config={"templates": {"a": {"folder": "x"}, "b": {"folder": "y"}}})
        with self.assertRaises(ParamError) as raised:
            commands._template(two, "")
        self.assertIn("a, b", str(raised.exception))
        with self.assertRaises(ParamError):
            commands._template(two, "c")
        broken = SimpleNamespace(config={"templates": {"a": {"folder": "x", "needs": {"gpu": "yes"}}}})
        with self.assertRaises(EnvironmentProblem) as raised:
            commands._template(broken, "a")
        self.assertIn("modules.vast.templates.a.needs.gpu", str(raised.exception))


class Archives(unittest.TestCase):
    """vast.lab-inputs' builder on its own: a clean tree at the pinned commit, a byte-stable tar, and refusals."""

    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(prefix="wb-archives-"))
        cls.repo = cls.root / "app"
        cls.commit = make_repo(cls.repo)
        cls.saved = {k: os.environ.pop(k, None) for k in ("WB_PIN_CACHE", "WB_STATE_DIR", "WB_URL")}

    @classmethod
    def tearDownClass(cls):
        for key, value in cls.saved.items():
            if value is not None:
                os.environ[key] = value
        shutil.rmtree(cls.root, ignore_errors=True)

    def context(self) -> Context:
        folder = Path(tempfile.mkdtemp(prefix="run-", dir=self.root))
        return Context({"job": "direct", "command": "vast.lab-inputs", "module": "vast", "work": str(folder / "work"),
                        "out": str(folder / "out")}, lambda event: None)

    def input(self, **recipe) -> templates.Input:
        raw = {"repo": str(self.repo), "commit": self.commit[:12], "steps": ["{python} make.py"],
               "artifacts": ["tools", "build/out.txt"], **recipe}
        return templates.parse("t", {"folder": "x", "inputs": {"app.tar": raw}}).inputs["app.tar"]

    def build(self, item: templates.Input, cache: Path) -> tuple[Context, dict]:
        ctx = self.context()
        return ctx, lab_inputs.build(ctx, "t", item, self.repo, cache, cached=False, keep=False)

    def test_the_archive_depends_on_the_commit_and_the_files_alone(self):
        cache = self.root / "pins-stable"
        ctx, first = self.build(self.input(), cache)
        self.assertEqual(ctx.outcome, "ok", ctx.findings)
        _, second = self.build(self.input(), cache)
        self.assertEqual(first["sha256"], second["sha256"])
        self.assertEqual((first["commit"], first["files"]), (self.commit, 3))
        with tarfile.open(ctx.out / "app.tar") as archive:
            members = archive.getmembers()
            self.assertEqual([m.name for m in members], ["build/out.txt", "tools/launcher.py", "tools/start.sh"])
            self.assertEqual({m.name: oct(m.mode) for m in members}["tools/start.sh"], "0o755")  # as git records it
            self.assertEqual({m.name: oct(m.mode) for m in members}["tools/launcher.py"], "0o644")
            moment = int(git(self.repo, "show", "-s", "--format=%ct", self.commit))
            self.assertEqual({(m.mtime, m.uid, m.gid, m.uname) for m in members}, {(moment, 0, 0, "")})
            self.assertEqual(archive.extractfile("build/out.txt").read(), b"made from 1.0")
        self.assertEqual((cache / first["sha256"]).read_bytes(), (ctx.out / "app.tar").read_bytes())
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")  # the checkout was only read
        self.assertFalse((self.repo / "build").exists())

    def test_an_archive_that_is_not_its_pin_is_not_kept(self):
        cache = self.root / "pins-wrong"
        ctx, result = self.build(self.input(sha256="0" * 64), cache)
        self.assertEqual(ctx.outcome, "finding")
        self.assertIn("not the pinned 000000000000", ctx.findings[0])
        self.assertIs(result["matched"], False)
        self.assertFalse((ctx.out / "app.tar").exists())
        self.assertFalse(cache.exists() and any(cache.iterdir()))

    def test_recipes_that_make_nothing_are_findings(self):
        cases = [({"steps": ['{python} -c "raise SystemExit(3)"']}, "exited 3"),
                 ({"artifacts": ["nothing/*.bin"]}, "no file matches"),
                 ({"base": "absent"}, "has no folder absent")]
        for recipe, expected in cases:
            with self.subTest(expected=expected):
                ctx, result = self.build(self.input(**recipe), self.root / "pins-none")
                self.assertEqual(ctx.outcome, "finding")
                self.assertIn(expected, ctx.findings[0])
                self.assertIsNone(result["archive"])
                self.assertFalse((ctx.out / "app.tar").exists())
        with self.assertRaises(ParamError):  # a commit the checkout lacks
            self.build(self.input(commit="f" * 40), self.root / "pins-none")

    def test_the_pin_cache_is_the_one_toolchain_uses(self):
        ctx = self.context()
        os.environ["WB_STATE_DIR"] = str(self.root / "state")
        try:
            self.assertEqual(lab_inputs.pin_cache(ctx), self.root / "state" / "cache" / "pins")
            os.environ["WB_PIN_CACHE"] = str(self.root / "from-env")
            self.assertEqual(lab_inputs.pin_cache(ctx), self.root / "from-env")  # a job's own cache comes first
        finally:
            os.environ.pop("WB_PIN_CACHE", None)
            os.environ.pop("WB_STATE_DIR", None)


TEMPLATE = """
summary = "a test template"
folder = "bundles/tours"
script = "run.sh"
exclude = ["*.md"]
needs = { jdk = ["26"], gpu = true, disk_gb = 32 }

[run]
failed = '^RUN tour .* exit [^0]'
stop = "pkill -f launcher.py"
unpack = ["results.tar"]

[inputs."app.tar"]
repo = "app"
commit = "@COMMIT@"
steps = ["{python} make.py"]
artifacts = ["tools", "build/out.txt"]

[inputs."extra.bin"]
sha256 = "@EXTRA@"

[steps]
file = "plan.env"
roles = { prepare = 1, play = "1+" }
same = ["name"]
files = [{ role = "prepare", param = "args", match = "*.jar", into = "jars", write = "remote" },
         { role = "play", param = "tour" }]

[steps.values]
NAME = "{prepare.name}"
TOURS = "{play.tour}"
LABELS = "{play.label|play.id}"
"OPTIONS_{n}" = "{play.options|}"
WINDOW = "{values.window|1920x1080}"

[outputs]
"{play.label|play.id}-log" = "results/{play.label|play.id}.log"
"""
EXTRA = b"made by hand"


class Bundles(unittest.TestCase):
    """vast.lab-inputs, vast.bundle and vast.rent of the result, from a template in a config
    file, against the fake service."""

    @classmethod
    def setUpClass(cls):
        cls.saved = {k: os.environ.pop(k, None) for k in ("WB_PIN_CACHE",)}
        cls.server, url = fake_vast.serve()
        root = Path(tempfile.mkdtemp(prefix="wb-bundles-"))
        data = root / "data"
        cls.commit = make_repo(data / "app")
        folder = data / "bundles" / "tours"
        folder.mkdir(parents=True)
        (folder / "run.sh").write_bytes(b"#!/bin/bash\necho RUN done\n")
        (folder / "README.md").write_bytes(b"not bundled\n")
        (data / "mod.jar").write_bytes(b"jar bytes")
        (data / "forests.json").write_bytes(b"{}")
        cls.pins = root / "pins"
        text = TEMPLATE.replace("@COMMIT@", cls.commit).replace("@EXTRA@", sha(EXTRA))
        cls.d = make_daemon(url, root, vast=f"root = '{data.as_posix()}'\n",
                            extra=f"[modules.toolchain]\npin_cache = '{cls.pins.as_posix()}'\n",
                            files={"modules/vast/templates/tours.toml": text}).start()
        cls.c, cls.data, cls.made = cls.d.client, data, None

    @classmethod
    def tearDownClass(cls):
        cls.d.stop()
        cls.server.shutdown()
        for key, value in cls.saved.items():
            if value is not None:
                os.environ[key] = value

    def lab(self) -> Path:
        """The template's inputs: app.tar built by vast.lab-inputs, extra.bin by hand."""
        if Bundles.made is None:
            out = self.d.out / "lab"
            result = self.c.submit("vast.lab-inputs", {}, out=str(out)).result(timeout=300)
            self.assertEqual(result.outcome, "ok", result.summary)
            (out / "extra.bin").write_bytes(EXTRA)
            Bundles.made = out
        return Bundles.made

    def bundle(self, out: str, **params):
        base = {"steps": game_steps(self.data / "mod.jar", self.data / "forests.json"), "lab": str(self.lab()),
                "values": {"window": "1280x720"}, "name": "turn-01"}
        params = {key: value for key, value in {**base, **params}.items() if value is not None}
        return self.c.submit("vast.bundle", params, out=str(self.d.out / out)).result(timeout=120)

    def test_commands_are_active_with_a_template(self):
        names = {c["name"]: c for c in self.c.commands()}
        for name in ("vast.bundle", "vast.lab-inputs"):
            self.assertTrue(names[name]["active"], names[name]["reasons"])
        self.assertEqual(names["vast.bundle"]["aliases"], [])  # no project's name, even as an alias

    def test_lab_inputs_are_built_with_receipts_and_taken_from_the_pin_cache(self):
        out = self.lab()
        receipt = json.loads((out / "app.tar.receipt.json").read_text(encoding="utf-8"))
        digest = sha((out / "app.tar").read_bytes())
        self.assertEqual(receipt["schema"], "wb.lab-input.v1")
        self.assertEqual((receipt["commit"], receipt["tree"]),
                         (self.commit, git(self.data / "app", "rev-parse", f"{self.commit}^{{tree}}")))
        self.assertEqual(receipt["steps"], [{"step": "{python} make.py", "exit": 0}])
        self.assertEqual(receipt["archive"]["sha256"], digest)
        self.assertEqual({f["path"]: f["sha256"] for f in receipt["files"]}["build/out.txt"], sha(b"made from 1.0"))
        self.assertEqual((out / "app.tar.sha256").read_text(encoding="utf-8"), f"{digest}  app.tar\n")
        self.assertTrue((self.pins / digest).is_file())  # the pin cache [modules.toolchain] names
        again = self.c.submit("vast.lab-inputs", {"inputs": ["app.tar"]}, out=str(self.d.out / "lab-again"))
        again = again.result(timeout=300)
        self.assertEqual(again.outcome, "ok", again.summary)
        self.assertEqual((again.data.archives[0]["cached"], again.data.archives[0]["sha256"]), (True, digest))
        self.assertIn("1 from the pin cache", again.summary)
        rebuilt = self.c.submit("vast.lab-inputs", {"cached": False}, out=str(self.d.out / "lab-rebuilt"))
        rebuilt = rebuilt.result(timeout=300)
        self.assertEqual((rebuilt.data.archives[0]["cached"], rebuilt.data.archives[0]["sha256"]), (False, digest))
        refused = self.c.submit("vast.lab-inputs", {"inputs": ["extra.bin"]}).result(timeout=60)
        self.assertEqual(refused.outcome, "usage")  # made by hand: no recipe
        self.assertIn("no recipe", refused.summary)

    def test_a_bundle_from_the_template_runs_on_the_rental(self):
        made = self.bundle("bundle-good")
        self.assertEqual(made.outcome, "ok", made.summary)
        folder = self.d.out / "bundle-good"
        self.assertEqual((folder / "files" / "plan.env").read_text(encoding="utf-8"), textwrap.dedent("""\
            # written by Workbench for bundle turn-01 from template tours
            NAME=inst-01
            TOURS='forests.json forests.json'
            LABELS='s1 measure'
            OPTIONS_1='-Dx=$HOME'
            OPTIONS_2=''
            WINDOW=1280x720
            """))
        self.assertEqual((folder / "files" / "jars" / "mod.jar").read_bytes(), b"jar bytes")
        self.assertTrue((folder / "files" / "run.sh").is_file())
        self.assertFalse((folder / "files" / "README.md").exists())  # the template's exclude
        written = tomllib.loads((folder / "bundle.toml").read_text(encoding="utf-8"))
        self.assertEqual((written["template"], written["run"]["script"], written["needs"]["gpu"]),
                         ("tours", "files/run.sh", True))
        pinned = {Path(u["path"]).name: u.get("sha256") for u in written["upload"]}
        self.assertEqual(pinned, {"app.tar": sha((self.lab() / "app.tar").read_bytes()), "extra.bin": sha(EXTRA),
                                  "files": None})
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema"], "wb.bundle.v1")
        for entry in manifest["files"]:
            self.assertEqual(sha((folder / entry["path"]).read_bytes()), entry["sha256"], entry["path"])
        self.assertIn("bundle.toml", {e["path"] for e in manifest["files"]})
        inputs = {i["name"]: i for i in manifest["inputs"]}
        self.assertEqual(inputs["app.tar"]["receipt"]["commit"], self.commit)
        self.assertIsNone(inputs["extra.bin"]["receipt"])
        self.assertEqual(made.data.outputs, [{"step": "settle", "name": "s1-log", "path": "results/s1.log"},
                                             {"step": "measure", "name": "measure-log", "path": "results/measure.log"}])
        self.assertEqual(made.data.unpack, ["results.tar"])
        checked = self.c.submit("vast.bundle-check", {"bundle": str(folder)}).result(timeout=60)
        self.assertEqual(checked.outcome, "ok", checked.summary)
        job = self.c.submit("vast.rent", {"offer": "777", "bundle": str(folder), "max_dph": 0.2, "cap": "30m"})
        with self.assertRaises(ApprovalRequired) as raised:
            job.result(timeout=60)
        job.approve(raised.exception.quote["id"], via="relayed")
        rented = job.result(timeout=120)
        self.assertEqual((rented.outcome, rented.data.outcome), ("ok", "completed"), rented.summary)
        remote = (self.d.state / "jobs" / job.id / "work" / "fake-remote.log").read_text(encoding="utf-8")
        for line in ("put app.tar -> /root/bundle/", "put extra.bin -> /root/bundle/", "put plan.env -> /root/bundle/",
                     "put jars -> /root/bundle/", "put run.sh -> /root/", "bash /root/run.sh /root/bundle"):
            self.assertIn(line, remote)

    def test_a_refused_bundle_writes_nothing_and_names_every_reason(self):
        steps = game_steps(self.data / "mod.jar", self.data / "forests.json")
        steps.append(dict(steps[0], id="prepare-2"))
        steps[1]["params"]["tour"] = str(self.data / "gone.json")
        lab = self.d.out / "lab-partial"
        lab.mkdir(exist_ok=True)
        shutil.copy2(self.lab() / "app.tar", lab / "app.tar")
        refused = self.bundle("bundle-refused", steps=steps, lab=str(lab))
        self.assertEqual(refused.outcome, "finding")
        text = " | ".join(refused.data.problems)
        self.assertIn("exactly 1 prepare step(s), and 2 were given", text)
        self.assertIn("gone.json, which is not a file", text)
        self.assertIn("has no extra.bin", text)
        self.assertFalse((self.d.out / "bundle-refused" / "bundle.toml").exists())
        unlabelled = self.bundle("bundle-unlabelled", lab=None)
        self.assertIn("give lab=", " ".join(unlabelled.data.problems))

    def test_a_drifted_input_or_a_changed_bundle_is_refused(self):
        lab = self.d.out / "lab-drifted"
        shutil.copytree(self.lab(), lab, dirs_exist_ok=True)
        (lab / "extra.bin").write_bytes(b"other bytes")
        drifted = self.bundle("bundle-drifted", lab=str(lab))
        self.assertEqual(drifted.outcome, "finding")
        self.assertIn(f"not the {sha(EXTRA)[:12]} template tours pins", drifted.summary)
        (lab / "extra.bin").write_bytes(EXTRA)
        (lab / "app.tar").write_bytes(b"not the archive its receipt names")
        mismatched = self.bundle("bundle-mismatched", lab=str(lab))
        self.assertIn("no longer matches its receipt", mismatched.summary)
        made = self.bundle("bundle-changed")
        self.assertEqual(made.outcome, "ok", made.summary)
        env = self.d.out / "bundle-changed" / "files" / "plan.env"
        env.write_text(env.read_text(encoding="utf-8") + "EXTRA=1\n", encoding="utf-8")
        changed = self.c.submit("vast.bundle-check", {"bundle": str(env.parents[1])}).result(timeout=60)
        self.assertEqual(changed.outcome, "usage")
        self.assertIn("files/plan.env changed since the bundle was made", changed.summary)
        again = self.bundle("bundle-changed")  # making it again replaces what it made
        self.assertEqual(again.outcome, "ok", again.summary)
        (env.parent / "added.txt").write_text("x", encoding="utf-8")
        added = self.c.submit("vast.bundle-check", {"bundle": str(env.parents[1])}).result(timeout=60)
        self.assertIn("files/added.txt is not in its manifest.json", added.summary)

    def test_a_dry_run_checks_and_writes_nothing(self):
        steps = game_steps(self.data / "mod.jar", self.data / "forests.json")
        steps[1]["params"]["tour"] = "{steps.build.outputs.tour}"  # known once the plan has run
        checked = self.bundle("bundle-dry", steps=steps, dry_run=True)
        self.assertEqual(checked.outcome, "ok", checked.summary)
        self.assertIn("checks out", checked.summary)
        self.assertIsNone(checked.data.bundle)
        self.assertFalse((self.d.out / "bundle-dry" / "bundle.toml").exists())

    def test_a_bundle_takes_a_folder_already_written(self):
        written = self.d.out / "written"
        written.mkdir(exist_ok=True)
        (written / "plan.env").write_text("NAME=inst-01\n", encoding="utf-8")
        (written / "forests.json").write_bytes(b"{}")
        made = self.c.submit("vast.bundle", {"plan": str(written), "lab": str(self.lab()), "name": "by-hand"},
                             out=str(written / "made")).result(timeout=120)
        self.assertEqual(made.outcome, "ok", made.summary)
        self.assertEqual(made["command"], "vast.bundle")
        folder = written / "made"
        self.assertEqual((folder / "files" / "plan.env").read_text(encoding="utf-8"), "NAME=inst-01\n")
        self.assertFalse((folder / "files" / "made").exists())  # the out folder inside it is not taken
        self.assertEqual(tomllib.loads((folder / "bundle.toml").read_text(encoding="utf-8"))["name"], "by-hand")
        (written / "plan.env").unlink()
        missing = self.c.submit("vast.bundle", {"plan": str(written), "lab": str(self.lab())},
                                out=str(written / "made")).result(timeout=120)
        self.assertEqual(missing.outcome, "finding")
        self.assertIn("has no plan.env", missing.summary)


if __name__ == "__main__":
    unittest.main()
