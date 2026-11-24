"""Plans: checked before any work, inputs pinned, references between steps, the protocol's stop rule, and the
retired rental route refused with what replaces it."""

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

    def test_a_rental_plan_is_refused_with_what_replaces_it(self):
        for data in ({"plan": {"name": "r", "machine": "rental"}, "step": [{"command": "selftest.echo"}]},
                     {"plan": {"name": "r"}, "rental": {"offer": "1"}, "step": [{"command": "selftest.echo"}]}):
            with self.assertRaisesRegex(plans.PlanError, "rental plans are retired: .*vast.bundle-launch"):
                plans.parse(data)
        path = self.write("rental.toml", """
            [plan]
            name = "rental"
            machine = "rental"
            [[step]]
            command = "selftest.echo"
            """)
        checked = self.c.submit("plan.check", {"plan": str(path)}).result(timeout=120)
        self.assertEqual(checked.outcome, "usage", checked.summary)
        self.assertIn("vast.bundle-launch", checked.summary)
        refused = self.c.submit("plan.check", {"plan": str(self.plan("host.toml")), "machine": "rental"})
        self.assertEqual(refused.result(timeout=120).outcome, "usage")  # no machine parameter any more

    def test_lab_manifest_declares_its_turn_routines(self):
        from wb_core.manifest import parse
        manifest = parse(REPO / "modules" / "lab" / "wb_lab" / "module.toml", "wb_lab")
        commands = {c.name: c for c in manifest.commands}
        self.assertEqual({n for n, c in commands.items() if c.turn}, {"gpu"})


if __name__ == "__main__":
    unittest.main()
