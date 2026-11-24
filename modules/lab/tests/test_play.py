"""wb play's own parts: the target it plays, the line that says which set revision it resolves at, and its
key=value inputs. (The build and the session it runs are lab.build's and lab.run's, tested beside them.)"""

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(REPO / "core"), str(REPO / "sdk" / "python"), str(REPO / "host"), str(REPO / "modules" / "lab")]

from wb import play  # noqa: E402


class Play(unittest.TestCase):
    def test_a_target_names_a_project_a_revision_and_a_launch(self):
        self.assertEqual(play.parse_target("riven"), {"project": "riven", "rev": None, "launch": "default"})
        self.assertEqual(play.parse_target("riven@set:canonical"),
                         {"project": "riven", "rev": "set", "launch": "canonical"})
        self.assertEqual(play.parse_target(None), {"project": None, "rev": None, "launch": "default"})
        self.assertEqual(play.parse_target(":canonical")["launch"], "canonical")
        with self.assertRaises(ValueError):
            play.parse_target("Riven!")

    def test_the_resolution_line_says_which_set_and_why(self):
        self.assertEqual(play.set_line("a" * 40, "b" * 40, 3, True), "set aaaaaaa (this folder, 3 behind)")
        self.assertEqual(play.set_line("b" * 40, "b" * 40, 0, True), "set bbbbbbb (head)")
        self.assertEqual(play.set_line("b" * 40, "b" * 40, None, False), "set bbbbbbb (head)")

    def test_key_value_inputs_keep_their_types(self):
        self.assertEqual(play.pairs(["seed=42", "mode=fast", "on=true"], "--param"),
                         {"seed": 42, "mode": "fast", "on": True})
        with self.assertRaises(ValueError):
            play.pairs(["seed"], "--param")


if __name__ == "__main__":
    unittest.main()
