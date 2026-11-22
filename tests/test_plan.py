"""Plans: checked before any work, inputs pinned, references between steps, the protocol's stop rule, and the
rental compiler (a plan's game steps as calibration 01's plan.env)."""

import hashlib
import sys
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import REPO, Daemon  # noqa: E402

sys.path.insert(0, str(REPO / "modules" / "plan"))
from wb_plan import plan as plans  # noqa: E402


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

    def test_rental_compiles_to_plan_env(self):
        data = {"plan": {"name": "cal-02", "machine": "rental"},
                "rental": {"offer": "1", "max_dph": 0.1, "lab": "x", "window": "1280x720"},
                "step": [{"id": "prepare", "command": "lab.prepare",
                          "params": {"name": "q-cal-02", "seed": 2485331, "scale": 4,
                                     "args": ["--client-mod", "D:\\jars\\q.jar"]}},  # scrub: allow
                         {"id": "settle", "command": "lab.play",
                          "params": {"name": "q-cal-02", "tour": "D:\\t\\forests.json", "label": "1-settle",  # scrub: allow
                                     "options": "-Da=1"}},
                         {"id": "measure", "command": "lab.play",
                          "params": {"name": "q-cal-02", "tour": "D:\\t\\forests.json", "label": "2-measure"}}]}  # scrub: allow
        registry = {"lab.prepare": {"turn": True, "role": "prepare"}, "lab.play": {"turn": True, "role": "play"}}
        env, labels = plans.plan_env(plans.parse(data), {"D:\\jars\\q.jar": "/root/bundle/jars/q.jar"},  # scrub: allow
                                     {"D:\\t\\forests.json": "forests.json"}, registry)  # scrub: allow
        self.assertIn("NAME=q-cal-02", env)
        self.assertIn('PREPARE_ARGS="--client-mod /root/bundle/jars/q.jar"', env)
        self.assertIn('TOURS="forests.json forests.json"', env)
        self.assertIn('OPTIONS_1="-Da=1"', env)
        self.assertIn("WINDOW=1280x720", env)
        self.assertEqual(labels, ["1-settle", "2-measure"])
        grouped = plans.groups(plans.parse(data), registry)
        self.assertEqual([[s.id for s in g] for g in grouped], [["prepare", "settle", "measure"]])
        self.assertEqual(len(plans.groups(plans.parse(data), {})), 3)  # nothing declares turn: one job per step

    def test_lab_manifest_declares_its_turn_routines_and_roles(self):
        from wb_core.manifest import parse
        manifest = parse(REPO / "modules" / "lab" / "wb_lab" / "module.toml", "wb_lab")
        commands = {c.name: c for c in manifest.commands}
        self.assertEqual({n for n, c in commands.items() if c.turn},
                         {"prepare", "play", "script", "gpu", "ensure", "snapshot", "reset"})
        self.assertEqual({n: c.role for n, c in commands.items() if c.role},
                         {"prepare": "prepare", "ensure": "prepare", "play": "play"})


if __name__ == "__main__":
    unittest.main()
