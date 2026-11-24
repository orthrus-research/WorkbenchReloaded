import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from orthrus_tools import actions, cli
from tests.support import capture, capture_json, git_repo

PASS = '["{python}", "-c", "pass"]'
FAIL = '["{python}", "-c", "import sys; sys.exit(3)"]'


def git(repo, *arguments):
    return subprocess.run(
        ["git", *arguments], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def commit_all(repo, message="work"):
    git(repo, "add", "--all")
    git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return git(repo, "rev-parse", "HEAD")


class Project(unittest.TestCase):
    """A temporary Git project with an actions.toml."""

    config = f"""
        [settings]
        name = "demo"

        [workflows]
        ci = ["lint", "test"]

        [jobs.lint]
        description = "always passes"
        steps = [{{ name = "lint", run = {PASS} }}]

        [jobs.test]
        needs = ["lint"]
        inputs = ["src/*.txt"]
        steps = [{{ name = "unit tests", run = {PASS} }}]

        [[quick.demo]]
        paths = ["docs/**"]
        jobs = []

        [[quick.demo]]
        paths = ["src/**"]
        jobs = ["test"]
    """

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = git_repo(Path(self.temporary.name).resolve())
        (self.root / ".gitignore").write_text(".local/\n", encoding="utf-8")
        (self.root / "src").mkdir()
        (self.root / "src" / "a.txt").write_text("a\n", encoding="utf-8")
        self.write_config(self.config)
        commit_all(self.root, "base")

    def tearDown(self):
        self.temporary.cleanup()

    def write_config(self, text):
        (self.root / "actions.toml").write_text(textwrap.dedent(text), encoding="utf-8")

    def actions(self, *argv):
        return capture_json(actions.main, *argv, "--root", str(self.root))

    def load(self):
        return actions.load(self.root)


class InputKeyTest(unittest.TestCase):
    def test_an_input_in_a_sibling_checkout_is_keyed_wherever_the_checkouts_are(self):
        with tempfile.TemporaryDirectory() as folder:
            first = Path(folder) / "first"
            (first / "demo" / "src").mkdir(parents=True)
            (first / "lib").mkdir()
            (first / "demo" / "src" / "a.txt").write_text("a\n", encoding="utf-8")
            (first / "lib" / "b.txt").write_text("b\n", encoding="utf-8")
            (first / "demo" / "actions.toml").write_text(textwrap.dedent(f"""
                [settings]
                name = "demo"

                [repositories.lib]
                path = "../lib"

                [workflows]
                ci = ["test"]

                [jobs.test]
                inputs = ["src/*.txt", "{{lib}}/b.txt"]
                steps = [{{ name = "t", run = {PASS} }}]
            """), encoding="utf-8")
            second = Path(folder) / "second"  # a clean clone of both lands in a new folder every run
            shutil.copytree(first, second)

            def key(top):
                config = actions.load(top / "demo")
                return actions.input_key(config, "test", actions.placeholders(config, "r"))

            self.assertEqual(key(first), key(second))
            (second / "lib" / "b.txt").write_text("changed\n", encoding="utf-8")
            self.assertNotEqual(key(first), key(second))


class MatchTest(unittest.TestCase):
    def test_double_star_crosses_folders_and_single_star_does_not(self):
        self.assertTrue(actions.match("docs/**", "docs/a/b.md"))
        self.assertTrue(actions.match("src/**/*.java", "src/A.java"))
        self.assertTrue(actions.match("src/**/*.java", "src/a/b/A.java"))
        self.assertFalse(actions.match("src/*.java", "src/a/A.java"))
        self.assertFalse(actions.match("docs/**", "docs.md"))
        self.assertTrue(actions.match("README.md", "README.md"))
        self.assertFalse(actions.match("readme.md", "README.md"))

    def test_a_path_takes_the_first_rule_it_matches_and_unmatched_paths_take_all(self):
        rules = [{"paths": ["docs/**"], "jobs": []}, {"paths": ["**/*.py"], "jobs": ["lint"]}]
        every = ["lint", "test"]
        self.assertEqual(actions.affected(rules, ["docs/x.py"], every), set())
        self.assertEqual(actions.affected(rules, ["tools/x.py"], every), {"lint"})
        self.assertEqual(actions.affected(rules, ["build.gradle"], every), set(every))


class ConfigTest(Project):
    def test_workflows_expand_and_needs_come_first(self):
        config = self.load()
        self.assertEqual(actions.resolve(config, ["test"]), ["lint", "test"])
        self.assertEqual(actions.resolve(config, ["ci"]), ["lint", "test"])

    def test_unknown_targets_cycles_and_bad_steps_are_usage_errors(self):
        with self.assertRaises(cli.ToolError):
            actions.resolve(self.load(), ["nope"])
        self.write_config(
            f"""
            [jobs.a]
            needs = ["b"]
            steps = [{{ name = "a", run = {PASS} }}]
            [jobs.b]
            needs = ["a"]
            steps = [{{ name = "b", run = {PASS} }}]
            """
        )
        with self.assertRaises(cli.ToolError) as raised:
            actions.resolve(self.load(), ["a"])
        self.assertIn("cycle", raised.exception.message)
        self.write_config('[jobs.a]\nsteps = [{ name = "a", run = "not a list" }]\n')
        code, document = self.actions("list")
        self.assertEqual(code, cli.USAGE)
        for extra, expected in (('artifacts = "build/*.jar"', "glob patterns"),
                                ('artifacts = ["build/*.jar"]\ninputs = ["src/*"]', "cannot be a cached check")):
            with self.subTest(expected=expected):
                self.write_config(f'[jobs.a]\n{extra}\nsteps = [{{ name = "a", run = {PASS} }}]\n')
                with self.assertRaises(cli.ToolError) as raised:
                    self.load()
                self.assertIn(expected, raised.exception.message)

    def test_a_missing_configuration_is_a_usage_error(self):
        (self.root / "actions.toml").unlink()
        code, document = self.actions("list")
        self.assertEqual(code, cli.USAGE)
        self.assertIn("no actions configuration", document["messages"][0]["text"])

    def test_orthrus_toml_can_name_another_file(self):
        (self.root / "actions.toml").rename(self.root / "ci.toml")
        (self.root / "orthrus.toml").write_text('[actions]\nconfig = "ci.toml"\n', "utf-8")
        code, document = self.actions("list")
        self.assertEqual(code, cli.OK)
        self.assertEqual(document["result"]["config"], "ci.toml")
        self.assertEqual(list(document["result"]["jobs"]), ["lint", "test"])

    def test_placeholders(self):
        self.write_config(
            """
            [settings]
            name = "demo"
            [interpreters]
            tool_python = ["missing/python.exe", "src/a.txt"]
            [placeholders]
            out = "{run_dir}/out"
            [jobs.a]
            steps = [{ name = "a", run = ["{tool_python}", "{out}", "{env.ORTHRUS_TEST}", "{{x}}"] }]
            """
        )
        with mock.patch.dict("os.environ", {"ORTHRUS_TEST": "value"}):
            values = actions.placeholders(self.load(), "r1")
            command = actions.expand(self.load().jobs["a"]["steps"][0]["run"], values)
        self.assertEqual(command[0], str(self.root / "src/a.txt"))
        self.assertEqual(Path(command[1]), self.root / ".local/actions/r1/out")
        self.assertEqual(command[2:], ["value", "{x}"])
        with self.assertRaises(cli.ToolError):
            actions.expand(["{nothing}"], values)


class WorkingTreeTest(Project):
    def test_a_clean_checkout_is_its_head_tree(self):
        self.assertEqual(
            actions.working_tree(self.root), git(self.root, "rev-parse", "HEAD^{tree}")
        )

    def test_uncommitted_work_hashes_as_its_commit_and_leaves_the_index_alone(self):
        (self.root / "src" / "a.txt").write_text("changed\n", encoding="utf-8")
        (self.root / "src" / "b.txt").write_text("added\n", encoding="utf-8")
        status = git(self.root, "status", "--porcelain")
        tested = actions.working_tree(self.root)
        self.assertEqual(git(self.root, "status", "--porcelain"), status)
        commit_all(self.root)
        self.assertEqual(tested, git(self.root, "rev-parse", "HEAD^{tree}"))

    def test_the_runs_folder_never_enters_the_tree(self):
        (self.root / ".gitignore").unlink()
        commit_all(self.root)
        clean = actions.working_tree(self.root)
        (self.root / ".local/actions/r1").mkdir(parents=True)
        (self.root / ".local/actions/r1/summary.json").write_text("{}", encoding="utf-8")
        self.assertEqual(actions.working_tree(self.root, self.root / ".local/actions"), clean)

    def test_no_checkout_has_no_tree(self):
        with tempfile.TemporaryDirectory() as empty:
            self.assertIsNone(actions.working_tree(Path(empty)))


class PassedJobsTest(unittest.TestCase):
    def test_passes_join_by_trees_and_unrecorded_or_moving_trees_do_not_count(self):
        with tempfile.TemporaryDirectory() as temporary:
            runs = Path(temporary)

            def summary(run_id, content):
                (runs / run_id).mkdir()
                text = content if isinstance(content, str) else json.dumps(content)
                (runs / run_id / "summary.json").write_text(text, encoding="utf-8")

            def jobs(**statuses):
                return {name: {"status": status} for name, status in statuses.items()}

            both = {"a": "a1", "b": "b1"}
            summary(
                "1", {"trees": both, "treesChanged": False, "jobs": jobs(x="passed", y="failed")}
            )
            summary("2", {"trees": both, "treesChanged": False, "jobs": jobs(z="passed")})
            summary(
                "3",
                {"trees": {"a": "a2", "b": "b1"}, "treesChanged": False, "jobs": jobs(x="passed")},
            )
            summary("4", {"trees": both, "treesChanged": True, "jobs": jobs(y="passed")})
            summary(
                "5",
                {"trees": {"a": "a1", "b": None}, "treesChanged": False, "jobs": jobs(y="passed")},
            )
            summary("6", {"jobs": jobs(y="passed")})
            summary("7", "{not json")
            self.assertEqual(
                actions.passed_jobs(runs, ("a", "b")),
                {("a1", "b1"): {"x", "z"}, ("a2", "b1"): {"x"}},
            )


class BudgetTest(unittest.TestCase):
    def test_claims_must_fit_and_locks_and_exclusive_jobs_run_alone(self):
        jobs = {
            "small": {},
            "half": {"share": 0.5},
            "big": {"memory": 10},
            "gradle1": {"locks": ["gradle"]},
            "gradle2": {"locks": ["gradle"]},
            "alone": {"exclusive": True},
        }
        budget = actions.Budget(jobs, 0, memory=12)
        self.assertTrue(budget.fits("alone"))  # nothing else runs
        budget.running = {"half"}
        self.assertTrue(budget.fits("small"))
        self.assertFalse(budget.fits("big"))  # 4 + 10 GiB is more than 12
        budget.running = {"half", "small", "gradle1"}
        self.assertFalse(budget.fits("small"))  # 1.25 of the cores
        budget.running = {"gradle1"}
        self.assertFalse(budget.fits("gradle2"))
        self.assertFalse(budget.fits("alone"))
        one = actions.Budget(jobs, 1, memory=12)
        one.running = {"small"}
        self.assertFalse(one.fits("small"))


class RunTest(Project):
    def test_a_workflow_runs_records_its_tree_and_reuses_cached_checks(self):
        code, document = self.actions("run", "ci")
        self.assertEqual(code, cli.OK, document["messages"])
        summary = document["result"]
        self.assertEqual(summary["schema"], actions.SUMMARY_SCHEMA)
        self.assertEqual([summary["jobs"][n]["status"] for n in ("lint", "test")], ["passed"] * 2)
        self.assertEqual(summary["trees"], {"demo": git(self.root, "rev-parse", "HEAD^{tree}")})
        self.assertFalse(summary["treesChanged"])
        log = self.root / summary["jobs"]["lint"]["steps"][0]["log"]
        self.assertTrue(log.read_text(encoding="utf-8").startswith("$ "))
        self.assertTrue(
            (self.root / ".local/actions" / summary["runId"] / "summary.json").is_file()
        )

        code, again = self.actions("run", "ci")
        self.assertEqual(again["result"]["jobs"]["test"]["cached"], summary["runId"])
        self.assertNotIn("cached", again["result"]["jobs"]["lint"])

        (self.root / "src" / "a.txt").write_text("changed\n", encoding="utf-8")
        code, changed = self.actions("run", "test")
        self.assertNotIn("cached", changed["result"]["jobs"]["test"])

    def test_if_needed_skips_what_passed_for_these_trees(self):
        self.actions("run", "lint")
        code, document = self.actions("run", "ci", "--if-needed")
        self.assertEqual(code, cli.OK)
        self.assertEqual(document["result"]["order"], ["lint", "test"])  # test needs lint
        code, document = self.actions("run", "ci", "--if-needed")
        self.assertEqual(document["result"]["order"], [])
        self.assertEqual(document["result"]["alreadyPassed"], ["lint", "test"])

    def test_each_step_and_job_keeps_the_peak_memory_of_its_process_tree(self):
        # The step's own process stays small; the 200 MiB is held by a process it starts.
        (self.root / "tools").mkdir()
        (self.root / "tools" / "hold.py").write_text(
            "import subprocess, sys\n"
            "child = 'b = bytearray(200 * 2**20)\\nfor i in range(0, len(b), 4096): b[i] = 1\\n"
            "import time; time.sleep(2)'\n"
            "sys.exit(subprocess.call([sys.executable, '-c', child]))\n",
            encoding="utf-8",
        )
        self.write_config(
            self.config.replace(
                f'name = "lint", run = {PASS}',
                f'name = "small", run = {PASS} }}, {{ name = "holds", run = '
                '["{python}", "tools/hold.py"]',
            )
        )
        commit_all(self.root)
        code, document = self.actions("run", "lint")
        self.assertEqual(code, cli.OK, document["messages"])
        job = document["result"]["jobs"]["lint"]
        small, holds = job["steps"]
        self.assertGreater(holds["peakRssBytes"], 190 * 2**20)
        self.assertLess(small.get("peakRssBytes", 0), holds["peakRssBytes"])
        self.assertEqual(job["peakRssBytes"], holds["peakRssBytes"])  # its largest step's
        recorded = json.loads(
            (self.root / ".local/actions" / document["result"]["runId"] / "summary.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(recorded["jobs"]["lint"]["peakRssBytes"], job["peakRssBytes"])
        code, report = self.actions("report")
        self.assertIn("| Step | Exit | Seconds | Peak MiB |", report["result"]["markdown"])
        self.assertIn(f"| {actions.mebibytes(job['peakRssBytes'])} |", report["result"]["markdown"])

    def test_a_failure_skips_its_dependants_and_exits_with_a_finding(self):
        self.write_config(
            self.config.replace(f'name = "lint", run = {PASS}', f'name = "lint", run = {FAIL}')
        )
        commit_all(self.root)
        code, document = self.actions("run", "ci")
        self.assertEqual(code, cli.FINDING)
        jobs = document["result"]["jobs"]
        self.assertEqual(jobs["lint"]["status"], "failed")
        self.assertEqual(jobs["lint"]["steps"][0]["exitCode"], 3)
        self.assertEqual(jobs["test"], {"status": "skipped", "reason": "needs lint"})

    def test_a_passed_job_keeps_its_artifacts_with_the_run(self):
        build = (
            '["{python}", "-c", "import pathlib; p = pathlib.Path(\'.local/out/libs\'); '
            "p.mkdir(parents=True, exist_ok=True); (p / 'tool.jar').write_bytes(b'jar bytes')\"]"
        )
        self.write_config(
            f"""
            [jobs.build]
            artifacts = [".local/out/**/*.jar"]
            steps = [{{ name = "build", run = {build} }}]
            [jobs.missing]
            artifacts = ["nowhere/*.jar"]
            steps = [{{ name = "build", run = {PASS} }}]
            """
        )
        commit_all(self.root)
        code, document = self.actions("run", "build")
        self.assertEqual(code, cli.OK, document["messages"])
        summary = document["result"]
        kept = summary["jobs"]["build"]["artifacts"]["tool.jar"]
        self.assertEqual(kept["from"], ".local/out/libs/tool.jar")
        self.assertEqual(kept["bytes"], len(b"jar bytes"))
        copy = self.root / ".local/actions" / summary["runId"] / "build" / "artifacts" / "tool.jar"
        self.assertEqual(copy.read_bytes(), b"jar bytes")
        self.assertEqual(kept["sha256"], actions.file_sha256(copy))
        recorded = json.loads(
            (self.root / ".local/actions" / summary["runId"] / "summary.json").read_text(encoding="utf-8")
        )
        self.assertEqual(recorded["jobs"]["build"]["artifacts"], summary["jobs"]["build"]["artifacts"])
        code, document = self.actions("run", "missing")
        self.assertEqual(code, cli.FINDING)
        job = document["result"]["jobs"]["missing"]
        self.assertEqual(job["status"], "failed")  # its steps passed, but it built nothing it declared
        self.assertIn("no file matches its artifact nowhere/*.jar", job["error"])

    def test_a_command_that_cannot_start_fails_its_step(self):
        self.write_config('[jobs.a]\nsteps = [{ name = "a", run = ["no-such-program-orthrus"] }]\n')
        commit_all(self.root)
        code, document = self.actions("run", "a")
        self.assertEqual(code, cli.FINDING)
        self.assertEqual(document["result"]["jobs"]["a"]["steps"][0]["exitCode"], 127)

    def test_dry_run_prints_commands_and_runs_nothing(self):
        code, out, _ = capture(actions.main, "run", "ci", "--dry-run", "--root", str(self.root))
        self.assertEqual(code, cli.OK)
        self.assertIn("lint:", out)
        self.assertFalse((self.root / ".local/actions").exists())

    def test_quick_runs_only_the_jobs_the_changes_reach(self):
        (self.root / "docs").mkdir()
        (self.root / "docs" / "a.md").write_text("doc\n", encoding="utf-8")
        code, document = self.actions("run", "quick")
        self.assertEqual(document["result"]["order"], [])
        (self.root / "src" / "b.txt").write_text("b\n", encoding="utf-8")
        code, document = self.actions("run", "quick")
        self.assertEqual(document["result"]["order"], ["lint", "test"])

    def test_a_callers_run_id_names_the_run_and_must_be_new(self):
        code, document = self.actions("run", "lint", "--run-id", "job-1", "--memory", "2")
        self.assertEqual(code, cli.OK)
        self.assertEqual(document["result"]["runId"], "job-1")
        self.assertTrue((self.root / ".local/actions/job-1/summary.json").is_file())
        code, document = self.actions("run", "lint", "--run-id", "job-1")
        self.assertEqual(code, cli.USAGE)
        self.assertIn("already exists", document["messages"][0]["text"])
        code, document = self.actions("run", "lint", "--run-id", "../out")
        self.assertEqual(code, cli.USAGE)

    def test_runs_in_one_second_each_get_their_own_folder(self):
        runs = self.root / ".local/actions"
        ids = {actions.new_run(runs)[0] for _ in range(3)}
        self.assertEqual(len(ids), 3)
        self.assertFalse((runs / ".lock").exists())

    def test_the_budget_takes_the_callers_memory(self):
        self.assertEqual(actions.budget(self.load(), 0, 3.0).memory, 3.0)

    def test_status_and_report(self):
        code, document = self.actions("status")
        self.assertEqual(code, cli.FINDING)
        self.assertEqual(document["result"]["missing"], ["lint", "test"])
        self.write_config(
            self.config.replace(
                f'name = "lint", run = {PASS}',
                'name = "lint", run = ["{python}", "-c", '
                '"print(\'timing {{\\"check\\": \\"x\\", \\"run\\": 1.5}}\')"]',
            )
        )
        commit_all(self.root)
        self.actions("run", "ci")
        code, document = self.actions("status")
        self.assertEqual(code, cli.OK)
        code, document = self.actions("report")
        self.assertEqual(code, cli.OK)
        markdown = document["result"]["markdown"]
        self.assertIn("| lint | passed |", markdown)
        self.assertIn("| lint / lint | x | run 1.5 |", markdown)
        self.assertTrue(Path(document["result"]["report"]).is_file())


MANIFEST = f"""
schema = 1
gate = ["build"]
[project]
name = "demo"
[artifacts.mod]
check = "build"
file = "out/*.jar"
[checks.build]
steps = [["{{python}}", "-c", "import pathlib; pathlib.Path('out').mkdir(exist_ok=True); pathlib.Path('out/demo.jar').write_bytes(b'jar')"]]
[checks.docs]
inputs = ["docs/*.md"]
steps = [{PASS}]
"""


class ProjectManifestTest(Project):
    """project.toml is read before actions.toml: each check a job, the gate the default workflow, each artifact's
    glob on the check that makes it; unknown keys are refused, naming the key."""

    def write_manifest(self, text):
        (self.root / "project.toml").write_text(textwrap.dedent(text), encoding="utf-8")

    def test_the_gate_is_the_default_and_artifacts_are_kept(self):
        self.write_manifest(MANIFEST)
        commit_all(self.root)
        config = self.load()
        self.assertEqual(config.path.name, "project.toml")
        self.assertEqual((config.workflow, config.required()), ("gate", ["build"]))
        self.assertEqual(config.jobs["build"]["artifacts"], ["out/*.jar"])
        code, document = self.actions("run", "gate", "--runs", str(self.root / "records"))
        self.assertEqual(code, cli.OK, document)
        build = document["result"]["jobs"]["build"]
        self.assertEqual(sorted(build["artifacts"]), ["demo.jar"])
        self.assertTrue((self.root / "records" / document["result"]["runId"] / "summary.json").is_file())

    def test_unknown_keys_and_gpu_gates_are_refused(self):
        self.write_manifest(MANIFEST.replace('[checks.docs]', '[checks.docs]\nsurprise = 1'))
        with self.assertRaises(actions.ToolError) as raised:
            self.load()
        self.assertIn("checks.docs.surprise", str(raised.exception))
        self.write_manifest(MANIFEST.replace('gate = ["build"]', 'gate = ["build", "docs"]', 1)
                            .replace('[checks.docs]', '[checks.docs]\ngpu = true'))
        with self.assertRaises(actions.ToolError) as raised:
            self.load()
        self.assertIn("GPU", str(raised.exception))
        self.write_manifest(MANIFEST.replace('gate = ["build"]\n', '', 1) + 'gate = ["build"]\n')
        with self.assertRaises(actions.ToolError) as raised:
            self.load()
        self.assertIn("top-level key", str(raised.exception))

    def test_a_checks_tools_are_kept_and_must_name_a_project(self):
        self.write_manifest(MANIFEST.replace('[checks.docs]', '[checks.docs]\ntools = ["cube:tools/**", "cube:lab/x.properties"]'))
        self.assertEqual(self.load().jobs["docs"]["tools"], ["cube:tools/**", "cube:lab/x.properties"])
        self.assertNotIn("tools", self.load().jobs["build"])
        self.write_manifest(MANIFEST.replace('[checks.docs]', '[checks.docs]\ntools = ["tools/**"]'))
        with self.assertRaises(actions.ToolError) as raised:
            self.load()
        self.assertIn("checks.docs.tools", str(raised.exception))

    def test_a_commits_own_file_is_found_whichever_kind_it_is(self):
        self.write_manifest(MANIFEST)
        commit = commit_all(self.root)
        self.assertEqual(actions.file_at(self.root, commit)[0], "project.toml")
        self.assertEqual(actions.file_at(self.root, git(self.root, "rev-parse", "HEAD~1"))[0], "actions.toml")

    def test_a_set_identity_keys_passes_and_a_jdk_identity_keys_pure_checks(self):
        """A run given the set's identity records it as a tree, so a platform or line move needs a new pass; a pure
        check's key takes the platform's JDK digest. The gate, given no identity, still judges by the tree alone."""
        from unittest import mock
        self.write_manifest(MANIFEST)
        commit_all(self.root)
        plain = self.load()
        self.assertEqual(plain.names, ("demo",))
        with mock.patch.dict(os.environ, {actions.SET_IDENTITY: "a" * 64, actions.JDK_IDENTITY: "b" * 64}):
            config = self.load()
            self.assertEqual(config.names, ("demo", "@set"))
            self.assertEqual(actions.trees(config)["@set"], "a" * 64)
            first = actions.input_key(config, "docs", actions.placeholders(config, "r"))
            with mock.patch.dict(os.environ, {actions.JDK_IDENTITY: "c" * 64}):
                moved = actions.input_key(config, "docs", actions.placeholders(config, "r"))
            with mock.patch.dict(os.environ, {actions.SET_IDENTITY: "d" * 64}):
                same = actions.input_key(config, "docs", actions.placeholders(config, "r"))
        self.assertNotEqual(first, moved)  # a JDK move reruns a pure check
        self.assertEqual(first, same)      # a line move alone does not

    def test_a_pure_checks_key_takes_the_used_third_entries_and_holds(self):
        """The run's WB_USES_IDENTITY (the SHA-256 of the third entries and holds the project uses) is in a pure
        check's key: a candidate set with other bytes for a used third entry reruns it; without one the key is the
        one it had."""
        from unittest import mock
        self.write_manifest(MANIFEST)
        commit_all(self.root)
        config = self.load()
        bare = actions.input_key(config, "docs", actions.placeholders(config, "r"))
        with mock.patch.dict(os.environ, {actions.USES_IDENTITY: "a" * 64}):
            config = self.load()
            used = actions.input_key(config, "docs", actions.placeholders(config, "r"))
        with mock.patch.dict(os.environ, {actions.USES_IDENTITY: "b" * 64}):
            config = self.load()
            moved = actions.input_key(config, "docs", actions.placeholders(config, "r"))
        with mock.patch.dict(os.environ, {actions.USES_IDENTITY: ""}):
            config = self.load()
            empty = actions.input_key(config, "docs", actions.placeholders(config, "r"))
        given = actions.Config(config.root, config.path, config.data, config.repositories, config.runs,
                               identity={"uses": "a" * 64})
        self.assertEqual(actions.input_key(given, "docs", actions.placeholders(given, "r")), used)
        self.assertNotEqual(bare, used)
        self.assertNotEqual(used, moved)
        self.assertEqual(bare, empty)

    def test_a_pure_checks_key_covers_the_tools_files_it_reads(self):
        from unittest import mock
        self.write_manifest(MANIFEST.replace('[checks.docs]', '[checks.docs]\ntools = ["lib:assets/**"]'))
        commit_all(self.root)
        scratch = Path(tempfile.mkdtemp(prefix="wb-tools-key-"))
        self.addCleanup(shutil.rmtree, scratch, True)
        laid = scratch / "Lib"
        (laid / "assets").mkdir(parents=True)
        (laid / "assets" / "a.json").write_text("1", encoding="utf-8")
        (laid / "README").write_text("r", encoding="utf-8")
        roots = {actions.TOOLS_ROOTS: json.dumps({"lib": str(laid)})}
        with mock.patch.dict(os.environ, roots):
            config = self.load()
            first = actions.input_key(config, "docs", actions.placeholders(config, "r"))
            (laid / "README").write_text("changed", encoding="utf-8")  # outside the glob: same key
            same = actions.input_key(config, "docs", actions.placeholders(config, "r"))
            (laid / "assets" / "a.json").write_text("2", encoding="utf-8")
            moved = actions.input_key(config, "docs", actions.placeholders(config, "r"))
        self.assertEqual(first, same)
        self.assertNotEqual(first, moved)


class LibraryTest(unittest.TestCase):
    """``actions.execute``: the engine as a library. The caller gives the steps' whole environment, the interpreter
    ``{python}`` names and the set's identities; nothing comes from this process's environment."""

    def setUp(self):
        self.root = git_repo(Path(tempfile.mkdtemp(prefix="wb-engine-lib-")).resolve())
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / ".gitignore").write_text(".local/\n", encoding="utf-8")
        show = '["{python}", "-c", "import os; print(os.environ.get(\'GIVEN\'), os.environ.get(\'OURS\'))"]'
        sleep = '["{python}", "-c", "import time; time.sleep(60)"]'
        (self.root / "actions.toml").write_text(textwrap.dedent(f"""
            [settings]
            name = "demo"
            identity_env = ["GIVEN"]

            [workflows]
            ci = ["show"]

            [jobs.show]
            inputs = ["actions.toml"]
            steps = [{{ name = "show", run = {show} }}]

            [jobs.slow]
            steps = [{{ name = "sleep", run = {sleep} }}]
        """), encoding="utf-8")
        commit_all(self.root, "base")
        self.runs = Path(tempfile.mkdtemp(prefix="wb-engine-runs-"))
        self.addCleanup(shutil.rmtree, self.runs, True)

    def environ(self, **extra):
        keep = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "HOME", "USERPROFILE")
        return {**{k: v for k, v in os.environ.items() if k.upper() in keep}, **extra}

    def test_the_steps_get_the_given_environment_and_none_of_this_process(self):
        with mock.patch.dict(os.environ, {"OURS": "leaked"}):
            document = actions.execute(self.root, ["ci"], runs=self.runs, run_id="r1",
                                       environ=self.environ(GIVEN="given"), python=sys.executable)
        self.assertEqual(document["status"], "ok", document)
        self.assertEqual(document["schema"], cli.RESULT_SCHEMA)
        self.assertEqual(document["result"]["jobs"]["show"]["status"], "passed")
        log = next((self.runs / "r1" / "show").glob("*.log")).read_text(encoding="utf-8")
        self.assertIn("given None", log)
        # The identity_env value is read from the given environment, so it keys the pass.
        moved = actions.execute(self.root, ["ci"], runs=self.runs, run_id="r2",
                                environ=self.environ(GIVEN="other"), python=sys.executable)
        self.assertFalse(moved["result"]["jobs"]["show"].get("cached"))
        same = actions.execute(self.root, ["ci"], runs=self.runs, run_id="r3",
                               environ=self.environ(GIVEN="other"), python=sys.executable)
        self.assertEqual(same["result"]["jobs"]["show"].get("cached"), "r2")

    def test_python_and_identity_are_given_not_read(self):
        project = str(self.root / "project-python")
        with mock.patch.dict(os.environ, {actions.SET_IDENTITY: "e" * 64}):
            plan = actions.execute(self.root, ["ci"], runs=self.runs, dry_run=True, environ=self.environ(),
                                   python=project, identity={"set": "a" * 64, "jdk": "b" * 64})
            plain = actions.execute(self.root, ["ci"], runs=self.runs, run_id="r3", environ=self.environ(),
                                    python=sys.executable, identity={})
        self.assertEqual(plan["result"]["commands"]["show"][0][0], project)
        self.assertNotIn("@set", plain["result"]["trees"])  # an explicit identity: the variable is not read
        named = actions.execute(self.root, ["ci"], runs=self.runs, run_id="r4", environ=self.environ(),
                                python=sys.executable, identity={"set": "a" * 64})
        self.assertEqual(named["result"]["trees"]["@set"], "a" * 64)

    def test_a_refusal_is_a_document_not_an_exception(self):
        document = actions.execute(self.root, ["nope"], runs=self.runs, environ=self.environ())
        self.assertEqual(document["status"], "usage")
        self.assertTrue(any("nope" in m["text"] for m in document["messages"]))

    def test_a_stop_kills_the_running_step_and_raises(self):
        import threading
        import time
        stop = threading.Event()
        threading.Timer(1.0, stop.set).start()
        started = time.monotonic()
        with self.assertRaises(actions.Stopped):
            actions.execute(self.root, ["slow"], runs=self.runs, run_id="r5", environ=self.environ(),
                            python=sys.executable, stop=stop)
        self.assertLess(time.monotonic() - started, 30)
        summary = json.loads((self.runs / "r5" / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["jobs"]["slow"]["status"], "failed")
        self.assertIn("stopped", next((self.runs / "r5" / "slow").glob("*.log")).read_text(encoding="utf-8"))


class GateTest(Project):
    def setUp(self):
        super().setUp()
        (self.root / "other").mkdir()
        self.other = git_repo(self.root / "other")
        (self.other / "b.txt").write_text("b\n", encoding="utf-8")
        self.pin = commit_all(self.other, "other")
        self.other_tree = git(self.other, "rev-parse", "HEAD^{tree}")
        self.write_config(
            self.config.replace(
                "[workflows]",
                '[repositories.other]\npath = "other"\npin = { file = "pin.json" }\n\n[workflows]',
            )
        )
        (self.root / ".gitignore").write_text(".local/\nother/\n", encoding="utf-8")
        self.commit = self.pinned(self.pin)

    def pinned(self, pin):
        (self.root / "pin.json").write_text(json.dumps({"commit": pin}), encoding="utf-8")
        return commit_all(self.root, f"pin {pin[:8]}")

    def refusal(self, repository, commit, passed):
        return actions.refusal(self.load(), repository, commit, passed, ["lint", "test"])

    def test_a_project_commit_needs_every_job_with_its_pinned_tree(self):
        tree = git(self.root, "rev-parse", "HEAD^{tree}")
        self.assertIsNone(
            self.refusal("demo", self.commit, {(tree, self.other_tree): {"lint", "test"}})
        )
        reason = self.refusal("demo", self.commit, {(tree, self.other_tree): {"lint"}})
        self.assertIn("has not passed test", reason)
        reason = self.refusal("demo", self.commit, {(tree, "elsewhere"): {"lint", "test"}})
        self.assertIn("has not passed lint, test", reason)

    def test_a_pin_key_may_be_a_dotted_path(self):
        self.assertEqual(actions.pin_value({"lib": {"commit": "c1"}}, "lib.commit"), "c1")
        self.assertEqual(actions.pin_value({"lib.commit": "top"}, "lib.commit"), "top")  # a literal key wins
        with self.assertRaises(KeyError):
            actions.pin_value({"lib": "c1"}, "lib.commit")
        text = (self.root / "actions.toml").read_text(encoding="utf-8")
        (self.root / "actions.toml").write_text(
            text.replace('pin = { file = "pin.json" }', 'pin = { file = "pin.json", key = "other.commit" }'),
            encoding="utf-8",
        )
        (self.root / "pin.json").write_text(json.dumps({"other": {"commit": self.pin}}), encoding="utf-8")
        commit = commit_all(self.root, "a nested pin")
        tree = git(self.root, "rev-parse", "HEAD^{tree}")
        self.assertIsNone(self.refusal("demo", commit, {(tree, self.other_tree): {"lint", "test"}}))

    def test_a_pin_to_an_absent_commit_is_refused(self):
        commit = self.pinned("f" * 40)
        reason = self.refusal("demo", commit, {})
        self.assertEqual(reason, "its other pin ffffffff is not in the other checkout")

    def test_a_pin_named_passed_must_have_passed_its_own_projects_checks(self):
        records = self.root / ".local" / "other-records"
        (self.other / "actions.toml").write_text(textwrap.dedent(f"""
            [settings]
            name = "other"
            runs = "{records.as_posix()}"

            [workflows]
            ci = ["check"]

            [jobs.check]
            steps = [{{ name = "check", run = {PASS} }}]
        """), encoding="utf-8")
        pin = commit_all(self.other, "other's own checks")
        text = (self.root / "actions.toml").read_text(encoding="utf-8")
        (self.root / "actions.toml").write_text(
            text.replace('pin = { file = "pin.json" }', 'pin = { file = "pin.json" }\npassed = "other-records"'),
            encoding="utf-8")
        commit = self.pinned(pin)
        tree = git(self.root, "rev-parse", "HEAD^{tree}")
        other_tree = git(self.other, "rev-parse", "HEAD^{tree}")
        passed = {(tree, other_tree): {"lint", "test"}}
        reason = self.refusal("demo", commit, passed)
        self.assertIn("its other pin", reason)
        self.assertIn("has not passed its own checks", reason)
        code, _ = capture_json(actions.main, "run", "ci", "--root", str(self.other))
        self.assertEqual(code, 0)
        self.assertIsNone(self.refusal("demo", commit, passed))  # the upstream's own pass is enough

    def test_passed_needs_a_pin(self):
        self.write_config(self.config.replace(
            "[workflows]", '[repositories.lib]\npath = "other"\npassed = "lib"\n\n[workflows]'))
        with self.assertRaises(actions.ToolError) as raised:
            self.load()
        self.assertIn("needs a pin", str(raised.exception))

    def test_another_repository_needs_every_job_in_one_combination(self):
        split = {("p1", self.other_tree): {"lint"}, ("p2", self.other_tree): {"test"}}
        self.assertIn("has not passed", self.refusal("other", self.pin, split))
        joined = {**split, ("p3", self.other_tree): {"lint", "test"}}
        self.assertIsNone(self.refusal("other", self.pin, joined))

    def test_an_untested_repository_never_counts_toward_a_pass(self):
        (self.root / "source").mkdir()
        source = git_repo(self.root / "source")
        (source / "c.txt").write_text("c\n", encoding="utf-8")
        commit_all(source, "source")
        text = (self.root / "actions.toml").read_text(encoding="utf-8")
        entry = '[repositories.source]\npath = "source"\ntested = false\n'
        (self.root / "actions.toml").write_text(
            text.replace("[workflows]", entry + "\n[workflows]"), encoding="utf-8"
        )
        (self.root / ".gitignore").write_text(".local/\nother/\nsource/\n", encoding="utf-8")
        commit = commit_all(self.root, "with a source")
        config = self.load()
        self.assertEqual(config.names, ("demo", "other"))
        self.assertIn("source", actions.placeholders(config, "r"))  # jobs still reach it by name
        code, document = self.actions("run", "ci")
        self.assertEqual(code, cli.OK, document["messages"])
        self.assertEqual(set(document["result"]["trees"]), {"demo", "other"})
        (source / "c.txt").write_text("a new commit\n", encoding="utf-8")
        commit_all(source, "moved on")
        code, document = self.actions("run", "ci", "--if-needed")
        self.assertEqual(document["result"]["order"], [])  # the passes still stand
        passed = actions.passed_jobs(config.runs, config.names)
        self.assertIsNone(actions.refusal(config, "demo", commit, passed, ["lint", "test"]))
        code, document = self.actions("gate", "install")
        self.assertEqual(code, cli.OK, document["messages"])
        self.assertFalse((source / ".git/hooks/pre-push").exists())
        (self.root / "actions.toml").write_text(
            text.replace("[workflows]", entry + 'pin = { file = "pin.json" }\n\n[workflows]'),
            encoding="utf-8",
        )
        with self.assertRaises(cli.ToolError):  # a pinned repository is tested
            self.load()

    def test_the_hook_check_reads_refs_from_stdin(self):
        zero = "0" * 40
        lines = f"refs/heads/main {self.commit} refs/heads/main {zero}\n(delete) {zero} refs/heads/old {self.commit}\n"
        with mock.patch("sys.stdin", io.StringIO(lines)):
            code, document = self.actions("gate", "check", "demo", "origin", "url")
        self.assertEqual(code, cli.FINDING)
        self.assertEqual(len(document["result"]["refused"]), 1)
        self.actions("run", "ci")
        with mock.patch("sys.stdin", io.StringIO(lines)):
            code, document = self.actions("gate", "check", "demo")
        # The run tested the working tree, which is the commit's tree with the other's pinned tree.
        self.assertEqual(code, cli.OK, document["messages"])

    def test_install_writes_hooks_and_keeps_another_hook(self):
        code, document = self.actions("gate", "install")
        self.assertEqual(code, cli.OK, document["messages"])
        hook = self.root / ".git/hooks/pre-push"
        self.assertIn(actions.HOOK_MARKER, hook.read_text(encoding="utf-8"))
        self.assertIn("gate check", hook.read_text(encoding="utf-8"))
        self.assertTrue((self.other / ".git/hooks/pre-push").is_file())
        code, document = self.actions("gate", "install")
        self.assertEqual(code, cli.OK)
        hook.write_text("#!/bin/sh\necho mine\n", encoding="utf-8")
        code, document = self.actions("gate", "install", "--repository", "demo")
        self.assertEqual(code, cli.FINDING)
        self.assertIn("another hook", document["messages"][0]["text"])
        code, document = self.actions("gate", "install", "--repository", "demo", "--force")
        self.assertEqual(code, cli.OK)


if __name__ == "__main__":
    unittest.main()
