"""Plans: checked before any work, inputs pinned, references between steps, the protocol's stop rule, and the
rental compile (a plan's game steps written into a bundle as its template in config declares, run on the fake
rental service, their results back as step outputs)."""

import hashlib
import json
import os
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import REPO, Daemon  # noqa: E402

sys.path.insert(0, str(REPO / "modules" / "plan"))
from wb_plan import plan as plans  # noqa: E402

VAST_TESTS = REPO / "modules" / "vast" / "tests"
sys.path.insert(0, str(VAST_TESTS))
import fake_vast  # noqa: E402


class Plans(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = Daemon().start()
        cls.c = cls.d.client
        cls.source = cls.d.data / "input.txt"
        cls.source.write_text("pinned payload", encoding="utf-8")
        cls.sha = hashlib.sha256(cls.source.read_bytes()).hexdigest()

    @classmethod
    def tearDownClass(cls):
        cls.d.stop()

    def write(self, name: str, text: str) -> Path:
        path = self.d.data / name
        path.write_text(textwrap.dedent(text), encoding="utf-8")
        return path

    def plan(self, name: str, extra_steps: str = "", sha: str | None = None) -> Path:
        return self.write(name, f"""
            [plan]
            name = "{name}"
            protocol = "copy the pinned input, echo its name; stop at the first step that is not ok"
            [inputs.payload]
            path = '{self.source}'
            sha256 = "{sha or self.sha}"
            [[step]]
            id = "copy"
            command = "selftest.copy"
            params = {{ source = "{{inputs.payload}}" }}
            [[step]]
            id = "echo"
            command = "selftest.echo"
            params = {{ text = "{{steps.copy.data.name}}" }}
            {extra_steps}
            """)

    def test_check_and_run(self):
        path = self.plan("good.toml")
        checked = self.c.submit("plan.check", {"plan": str(path)}).result(timeout=120)
        self.assertEqual(checked.outcome, "ok", checked.data)
        self.assertEqual(checked.data.problems, [])
        ran = self.c.submit("plan.run", {"plan": str(path)}).result(timeout=240)
        self.assertEqual(ran.outcome, "ok", ran.summary)
        self.assertEqual([r["steps"] for r in ran.data.ran], [["copy"], ["echo"]])
        echo = self.c.job(ran.data.ran[1]["job"]).refresh()
        self.assertEqual(echo.data.text, "input.txt")
        self.assertEqual(echo["parent"], ran["job"])

    def test_protocol_stops_at_the_first_unaccepted_step(self):
        path = self.plan("stops.toml", """
            [[step]]
            id = "fails"
            command = "selftest.fail"
            params = { outcome = "finding" }
            [[step]]
            id = "never"
            command = "selftest.echo"
            """)
        ran = self.c.submit("plan.run", {"plan": str(path)}).result(timeout=240)
        self.assertEqual(ran.outcome, "finding")
        self.assertIn("stopped at fails", ran.summary)
        self.assertEqual(len(ran.data.ran), 3)

    def test_drifted_input_and_bad_references_are_refused_before_work(self):
        path = self.plan("drift.toml", """
            [[step]]
            id = "bad"
            command = "selftest.echo"
            params = { text = "{steps.later.data.x}", nope = 1 }
            """, sha="0" * 64)
        checked = self.c.submit("plan.check", {"plan": str(path)}).result(timeout=120)
        self.assertEqual(checked.outcome, "finding")
        text = " ".join(checked.data.problems)
        self.assertIn("drifted", text)
        self.assertIn("does not run before it", text)
        self.assertIn("unknown parameters", text)
        ran = self.c.submit("plan.run", {"plan": str(path)}).result(timeout=120)
        self.assertEqual(ran.outcome, "finding")
        self.assertEqual(ran["children"], [])  # nothing ran

    def test_rental_compiles_the_game_steps(self):
        data = {"plan": {"name": "turn-02", "machine": "rental"},
                "inputs": {"mod": {"path": "/srv/jars/mod.jar"}},
                "rental": {"offer": "1", "max_dph": 0.1, "cap": "30m", "template": "tours", "lab": "x",
                           "window": "1280x720"},
                "step": [{"id": "prepare", "command": "lab.prepare",
                          "params": {"name": "inst-02", "seed": 2485331, "scale": 4,
                                     "args": ["--client-mod", "{inputs.mod}"]}},
                         {"id": "settle", "command": "lab.play",
                          "params": {"name": "inst-02", "tour": "/srv/tours/forests.json", "label": "1-settle",
                                     "options": "-Da=1"}},
                         {"id": "measure", "command": "lab.play",
                          "params": {"name": "inst-02", "tour": "/srv/tours/forests.json", "label": "2-measure"}},
                         {"id": "echo", "command": "selftest.echo", "params": {"text": "x"}}]}
        registry = {"lab.prepare": {"turn": True, "role": "prepare"}, "lab.play": {"turn": True, "role": "play"}}
        plan = plans.parse(data)
        self.assertEqual([s.id for s in plans.game_steps(plan, registry)], ["prepare", "settle", "measure"])
        steps = plans.rental_steps(plan, registry, lambda ref: plan.inputs[ref.split(".", 1)[1]]["path"])
        self.assertEqual(steps[0], {"id": "prepare", "command": "lab.prepare", "role": "prepare",
                                    "params": {"name": "inst-02", "seed": 2485331, "scale": 4,
                                               "args": ["--client-mod", "/srv/jars/mod.jar"]}})
        self.assertEqual([s["role"] for s in steps], ["prepare", "play", "play"])
        self.assertEqual(plans.rental_values(plan), {"window": "1280x720"})  # what plan.run reads itself stays out
        grouped = plans.groups(plan, registry)
        self.assertEqual([[s.id for s in g] for g in grouped], [["prepare", "settle", "measure"], ["echo"]])
        self.assertEqual(len(plans.groups(plan, {})), 4)  # nothing declares turn: one job per step

    def test_a_rental_check_names_what_the_rental_lacks(self):
        checked = self.c.submit("plan.check", {"plan": str(self.plan("host.toml")), "machine": "rental"})
        checked = checked.result(timeout=120)
        self.assertEqual(checked.outcome, "finding")  # this Workbench has no Vast key and no templates
        text = " ".join(checked.data.problems)
        self.assertIn("a rental plan needs rental.offer", text)
        self.assertIn("no game steps", text)
        self.assertIn("vast.bundle, which is not active", text)

    def test_lab_manifest_declares_its_turn_routines_and_roles(self):
        from wb_core.manifest import parse
        manifest = parse(REPO / "modules" / "lab" / "wb_lab" / "module.toml", "wb_lab")
        commands = {c.name: c for c in manifest.commands}
        self.assertEqual({n for n, c in commands.items() if c.turn}, {"gpu"})
        self.assertEqual({n: c.role for n, c in commands.items() if c.role}, {"prepare": "prepare", "play": "play"})


TEMPLATE = """
folder = '@FOLDER@'
script = "run.sh"
needs = { gpu = true }

[run]
unpack = ["results.tar"]

[inputs."base.tar"]
sha256 = "@BASE@"

[steps]
file = "steps.env"
roles = { prepare = 1, play = "1+" }
same = ["name"]
files = [{ role = "prepare", param = "args", match = "*.jar", into = "jars", write = "remote" },
         { role = "play", param = "tour" }]

[steps.values]
NAME = "{prepare.name}"
ARGS = "{prepare.args|}"
TOURS = "{play.tour}"
"OPTIONS_{n}" = "{play.options|}"
WINDOW = "{values.window|1920x1080}"

[outputs]
"{play.label|play.id}-log" = "results/{play.label|play.id}.log"
"""


class RentalPlans(unittest.TestCase):
    """A plan whose game steps run on a rental: checked against its bundle template, written into a bundle, run on
    the fake rental service after a yes, and their fetched results read by a later host step. Every API request
    goes to the fake service: nothing is searched for or rented for real."""

    @classmethod
    def setUpClass(cls):
        cls.server, url = fake_vast.serve()
        os.environ["WB_VAST_FAKE_TRANSPORT"] = "fake_vast:make"
        os.environ["PYTHONPATH"] = str(VAST_TESTS) + os.pathsep + os.environ.get("PYTHONPATH", "")
        root = Path(tempfile.mkdtemp(prefix="wb-plan-rental-"))
        data = root / "data"
        for folder in ("files", "lab", "launcher", "secrets"):
            (data / folder).mkdir(parents=True)
        (data / "files" / "run.sh").write_bytes(b"#!/bin/bash\necho RUN done\n")
        (data / "lab" / "base.tar").write_bytes(b"made by hand")
        (data / "mod.jar").write_bytes(b"jar bytes")
        (data / "forests.json").write_bytes(b"{}")
        (data / "secrets" / "key").write_text("fake-key", encoding="ascii")
        (data / "secrets" / "ssh").write_text("not a real key", encoding="ascii")
        template = TEMPLATE.replace("@FOLDER@", (data / "files").as_posix()).replace(
            "@BASE@", hashlib.sha256(b"made by hand").hexdigest())
        cls.d = Daemon(f"""
            [secrets.vast_api_key]
            host = '{data / "secrets" / "key"}'
            [secrets.vast_ssh_key]
            host = '{data / "secrets" / "ssh"}'
            [modules.vast]
            api = "{url}"
            poll_seconds = 0.02
            ssh_retry_seconds = 0.02
            cards = ["RTX 3060"]
            """, root=root, files={"modules/vast/templates/turn.toml": template}).start()
        cls.c, cls.data = cls.d.client, data
        cls.jar = hashlib.sha256(b"jar bytes").hexdigest()

    @classmethod
    def tearDownClass(cls):
        cls.d.stop()
        cls.server.shutdown()

    def write(self, name: str, extra_steps: str = "", prepare_args: str = '"--client-mod", "{inputs.mod}"') -> Path:
        path = self.data / name
        path.write_text(textwrap.dedent(f"""
            [plan]
            name = "turn-01"
            protocol = "prepare one instance, play two tours on a rented GPU, then read the first tour's log"
            machine = "rental"
            [inputs.mod]
            path = '{self.data / "mod.jar"}'
            sha256 = "{self.jar}"
            [[step]]
            id = "prepare"
            command = "lab.prepare"
            params = {{ name = "inst-01", seed = 7, args = [{prepare_args}] }}
            [[step]]
            id = "settle"
            command = "lab.play"
            params = {{ name = "inst-01", tour = '{self.data / "forests.json"}', label = "s1" }}
            [[step]]
            id = "measure"
            command = "lab.play"
            params = {{ name = "inst-01", tour = '{self.data / "forests.json"}', label = "s2", options = "-Dx=1" }}
            {extra_steps}
            [[step]]
            id = "read"
            command = "selftest.echo"
            params = {{ text = "{{steps.settle.outputs.s1-log}}" }}
            [rental]
            offer = "777"
            max_dph = 0.2
            cap = "30m"
            template = "turn"
            lab = '{self.data / "lab"}'
            window = "1280x720"
            """), encoding="utf-8")
        return path

    def check(self, path: Path):
        """plan.check with an out folder under a rw root, as a rental plan needs."""
        return self.c.submit("plan.check", {"plan": str(path)}, out=str(self.d.out / f"check-{path.stem}"))

    def test_a_rental_plan_is_checked_against_its_template(self):
        good = self.check(self.write("good.toml")).result(timeout=120)
        self.assertEqual(good.outcome, "ok", good.data.problems)
        self.assertEqual([self.c.job(j).refresh()["command"] for j in good["children"]], ["vast.bundle"])
        twice = self.write("twice.toml", extra_steps="""
            [[step]]
            id = "again"
            command = "lab.prepare"
            params = { name = "inst-01" }
            """)
        bad = self.check(twice).result(timeout=120)
        self.assertEqual(bad.outcome, "finding")
        self.assertIn("rental: template turn takes exactly 1 prepare step(s), and 2 were given",
                      bad.data.problems)
        early = self.check(self.write("early.toml", extra_steps="""
            [[step]]
            id = "late"
            command = "lab.play"
            params = { name = "inst-01", tour = "{steps.prepare.job}" }
            """)).result(timeout=120)
        self.assertIn("step late: a game step is written into the rental's bundle before any step runs, so "
                      "{steps.prepare.job} has no value yet", early.data.problems)
        missing = self.check(self.write("missing.toml", prepare_args=f"'{self.data / 'gone.jar'}'"))
        missing = missing.result(timeout=120)
        self.assertTrue(any("gone.jar, which is not a file" in p for p in missing.data.problems),
                        missing.data.problems)

    def test_a_rental_plan_runs_its_game_steps_in_one_bundle(self):
        results = {"results/s1.log": "first tour", "results/s2.log": "second tour"}
        os.environ[fake_vast.RESULTS_ENV] = json.dumps(results)  # what the fake machine's results.tar holds
        fake_vast.STATE.offers["777"]["dph_total"] = 0.082
        fake_vast.STATE.billed_override = None
        try:
            job = self.c.submit("plan.run", {"plan": str(self.write("run.toml"))}, out=str(self.d.out / "turn-01"))
            deadline = time.time() + 120
            waiting, plan = [], job.refresh()
            while not waiting and time.time() < deadline and plan.get("state") not in ("settled", "cancelled"):
                waiting = [e for e in self.c.jobs(state="awaiting_approval", command="vast.rent")
                           if e.get("parent") == job.id]
                plan = job.refresh(wait=0.2)
            self.assertTrue(waiting, f"the rental never asked for a yes: {plan.get('summary')}")
            self.c.job(waiting[0]["job"]).approve(waiting[0]["approval"]["quote"]["id"], via="relayed")
            ran = job.result(timeout=240)
        finally:
            os.environ.pop(fake_vast.RESULTS_ENV, None)
        self.assertEqual(ran.outcome, "ok", ran.summary)
        self.assertEqual([r["steps"] for r in ran.data.ran], [["prepare", "settle", "measure"], ["read"]])
        steps = (self.d.out / "turn-01" / "bundle" / "files" / "steps.env").read_text(encoding="utf-8")
        self.assertIn("ARGS='--client-mod /root/bundle/jars/mod.jar'", steps)
        self.assertIn("TOURS='forests.json forests.json'", steps)
        self.assertIn("OPTIONS_2=-Dx=1", steps)
        self.assertIn("WINDOW=1280x720", steps)
        fetched = self.d.out / "turn-01" / "rental" / "results" / "s1.log"
        self.assertEqual(fetched.read_bytes(), b"first tour")
        read = self.c.job(ran.data.ran[1]["job"]).refresh()
        self.assertEqual(Path(read.data.text), fetched)  # a host step read a rented step's output
        self.assertIn("s2-log", {o["name"] for o in ran["outputs"]})


if __name__ == "__main__":
    unittest.main()
