"""Named instance profiles: each harness its own profile, picked per job; the default profile stays as it was."""

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path[:0] = [str(REPO / "core"), str(REPO / "sdk" / "python"), str(REPO / "modules" / "lab")]

from wb_core.module import EnvironmentProblem  # noqa: E402
from wb_lab import commands, profile  # noqa: E402

BASE = {"launcher": "run.py", "instances": "instances", "prepare": ["{python}", "run.py", "prepare", "{name}"],
        "play": ["{python}", "run.py", "play", "{name}"]}


class Checkouts(unittest.TestCase):
    """Two launcher checkouts and a config naming a profile for each; no tests of its own."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        root = Path(self.folder.name)
        self.main, self.other = root / "main", root / "other"
        for checkout in (self.main, self.other):
            checkout.mkdir()
            (checkout / "run.py").write_text("# a launcher\n", encoding="utf-8")
        self.config = {"launcher": str(self.main), "profile": dict(BASE),
                       "profiles": {"other": dict(BASE, checkout=str(self.other), instances="runs")}}

    def tearDown(self):
        self.folder.cleanup()

    def ctx(self, name: str = ""):
        return SimpleNamespace(config=self.config, params={"profile": name} if name else {})


class NamedProfiles(Checkouts):
    def test_a_job_picks_its_harness_and_the_default_stays(self):
        self.assertEqual(profile.merged(self.config)["instances"], "instances")
        self.assertEqual(profile.merged(self.config, "other")["instances"], "runs")
        self.assertEqual(commands._launcher(self.ctx()), self.main)
        self.assertEqual(commands._launcher(self.ctx("other")), self.other)  # the profile's own checkout
        with self.assertRaises(profile.ProfileError):
            profile.merged(self.config, "absent")
        with self.assertRaises(EnvironmentProblem):
            commands._profile(self.ctx("absent"))

    def test_a_profile_may_start_from_another(self):
        self.config["profiles"]["branch"] = {"from": "", "checkout": str(self.other)}  # the default, elsewhere
        self.config["profiles"]["runs"] = {"from": "branch", "instances": "runs"}
        branch = profile.merged(self.config, "branch")
        self.assertEqual((branch["checkout"], branch["play"]), (str(self.other), BASE["play"]))
        self.assertEqual(profile.merged(self.config, "runs")["instances"], "runs")
        self.assertEqual(profile.merged(self.config, "runs")["checkout"], str(self.other))  # through branch
        self.assertEqual(commands._launcher(self.ctx("branch")), self.other)
        self.assertIsNone(profile.merged(self.config)["checkout"])  # the default profile is as it was
        cases = [({"loop": {"from": "loop"}}, "cycle"),
                 ({"a": {"from": "b"}, "b": {"from": "a"}}, "cycle (a -> b -> a)"),
                 ({"bad": {"from": 3}}, "from names the profile"),
                 ({"typo": {"from": "", "chekout": "x"}}, "unknown instance profile key(s) chekout"),
                 ({"lost": {"from": "absent"}}, "no instance profile absent")]
        for profiles, expected in cases:
            with self.subTest(expected=expected):
                config = dict(self.config, profiles=profiles)
                with self.assertRaises(profile.ProfileError) as raised:
                    profile.merged(config, next(iter(profiles)))
                self.assertIn(expected, str(raised.exception))

    def test_another_harness_never_shares_a_recipe(self):
        default = commands.recipe_inputs(self.ctx(), 1, None, [])
        other = commands.recipe_inputs(self.ctx("other"), 1, None, [])
        self.assertNotIn("profile", default)  # the default profile's recipe keys are what they were
        self.assertEqual(other["profile"], "other")
        self.assertNotEqual(default, other)


class InputSteps(Checkouts):
    """lab.inputs' steps are checked with the profile, before anything runs."""

    def test_steps_are_checked(self):
        good = [{"name": "pins", "run": ["{python}", "setup.py"]},
                {"name": "build", "run": ["{cwd}/gradlew", "build"], "checkout": "engine"}]
        self.config["profile"] = dict(BASE, inputs=good, checkouts={"engine": "../engine"})
        self.assertEqual([s["name"] for s in profile.merged(self.config)["inputs"]], ["pins", "build"])
        cases = [({"name": "x"}, "run is a non-empty command template"),
                 ({"name": "x", "run": []}, "run is a non-empty command template"),
                 ({"name": "a b", "run": ["x"]}, "not a plain, distinct step name"),
                 ({"name": "x", "run": ["x"], "cwd": "y"}, "is not a step"),
                 ({"name": "x", "run": ["x"], "checkout": "absent"}, "checkouts name no absent")]
        for step, expected in cases:
            with self.subTest(expected=expected):
                self.config["profile"] = dict(BASE, inputs=[step])
                with self.assertRaises(profile.ProfileError) as raised:
                    profile.merged(self.config)
                self.assertIn(expected, str(raised.exception))
        self.config["profile"] = dict(BASE, inputs=[good[0], good[0]])
        with self.assertRaises(profile.ProfileError):
            profile.merged(self.config)  # two steps of one name


class Agreement(Checkouts):
    """Shared dependencies: each project's pin read from its own file; prefixes agree, anything else does not."""

    def write(self, checkout: Path, text: str):
        (checkout / "pins.json").write_text(text, encoding="utf-8")

    def configure(self):
        pins = [{"file": "pins.json", "pattern": r'"lib"\s*:\s*"([0-9a-f]+)"'},
                {"file": "pins.json", "pattern": r'"lib-commit"\s*=\s*"([0-9a-f]+)"', "checkout": "other"}]
        self.config["profile"] = dict(BASE, agree={"lib": pins}, checkouts={"other": str(self.other)})

    def test_prefixes_agree_and_other_commits_do_not(self):
        from wb_lab import inputs
        self.configure()
        self.write(self.main, '{"lib": "0c2995e"}')
        self.write(self.other, '"lib-commit" = "0c2995e6c1d838be8c2b4975eec68b923eb0f1c4"')
        found = inputs.agreement(self.ctx())
        self.assertTrue(found["lib"]["agrees"])
        self.assertEqual(inputs.check_agreement(self.ctx(), False), [])
        self.write(self.main, '{"lib": "84a58e84e4"}')
        self.assertFalse(inputs.agreement(self.ctx())["lib"]["agrees"])
        with self.assertRaises(EnvironmentProblem) as raised:
            inputs.check_agreement(self.ctx(), False)
        self.assertIn("lib: launcher pins.json 84a58e84e4, other pins.json 0c2995e6c1d8", str(raised.exception))
        self.assertEqual(len(inputs.check_agreement(self.ctx(), True)), 1)  # allowed: reported, not refused
        self.write(self.main, '{"nothing": 1}')
        with self.assertRaises(EnvironmentProblem):
            inputs.agreement(self.ctx())  # a pin its pattern cannot find is no agreement

    def test_pins_are_checked(self):
        cases = [({"lib": [{"file": "a", "pattern": "(x)"}]}, "two or more pins"),
                 ({"lib": [{"file": "a", "pattern": "x"}, {"file": "b", "pattern": "(x)"}]}, "no group"),
                 ({"lib": [{"file": "a", "pattern": "(x"}, {"file": "b", "pattern": "(x)"}]}, "not a regular"),
                 ({"lib": [{"file": "a"}, {"file": "b", "pattern": "(x)"}]}, "is not a pin"),
                 ({"lib": [{"file": "a", "pattern": "(x)", "checkout": "absent"},
                           {"file": "b", "pattern": "(x)"}]}, "checkouts name no absent")]
        for agree, expected in cases:
            with self.subTest(expected=expected):
                self.config["profile"] = dict(BASE, agree=agree)
                with self.assertRaises(profile.ProfileError) as raised:
                    profile.merged(self.config)
                self.assertIn(expected, str(raised.exception))


class PlayCollect(Checkouts):
    """A play may keep more files than its profile names; what it names must stay inside the instance."""

    def test_a_bad_collect_entry_is_refused_before_anything_runs(self):
        from wb_core.module import ParamError
        for collect, expected in (([["only-one"]], "pair"), ([["../outside.log", "x"]], "inside the instance"),
                                  ([["logs/a.log", "sub/name"]], "plain name")):
            with self.subTest(expected=expected):
                with self.assertRaises(ParamError) as raised:
                    commands.play(self.ctx(), "inst", "tour.json", "854x480", "", "p", [], collect=collect)
                self.assertIn(expected, str(raised.exception))


class FailurePatterns(unittest.TestCase):
    """A failed play says why, by the profile's failure patterns over the client's log and play's own output."""

    CLIENT = ("loading mods\nA custom exception was thrown by a mod, the game will display an error screen\n"
              "RendererRequirement: Engine cannot start without Renderer: install it\n")
    OUTPUT = "Traceback (most recent call last):\nRuntimeError: Native process exited before joined the game\n"

    def test_the_most_telling_pattern_wins_over_the_earliest_line(self):
        patterns = ["cannot start without", "Native process exited before .*"]
        self.assertEqual(commands.explain(patterns, [self.CLIENT, self.OUTPUT]),
                         "RendererRequirement: Engine cannot start without Renderer: install it")
        self.assertEqual(commands.explain(patterns[1:], [self.CLIENT, self.OUTPUT]),
                         "RuntimeError: Native process exited before joined the game")
        self.assertIsNone(commands.explain(patterns, ["nothing to see"]))
        self.assertIsNone(commands.explain([], [self.CLIENT]))  # no patterns: the finding stays as it was
        self.assertEqual(len(commands.explain(patterns, ["x" * 300 + " cannot start without"])), 240)

    def test_a_pattern_that_is_no_regular_expression_is_refused(self):
        with self.assertRaises(profile.ProfileError) as raised:
            profile.merged({"profile": dict(BASE, failures=["("])})
        self.assertIn("failures", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
