"""The CLI: parameters parsed by their schema, a queued job's wait reason, the job id at once on stderr with stdout
one envelope, and JSON from the read verbs."""

import contextlib
import io
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import REPO, Daemon  # noqa: E402

from wb import cli  # noqa: E402

SCHEMA = {"properties": {"names": {"type": "array", "items": {"type": "string"}}, "spec": {"type": "object"},
                         "text": {"type": "string"}}}


class Params(unittest.TestCase):
    def test_a_list_or_table_that_is_not_json_is_refused(self):
        for pair in ("names=[a,b]", "spec={broken"):
            with self.subTest(pair=pair):
                with self.assertRaises(SystemExit) as raised:
                    cli.parse_params([pair], SCHEMA)
                self.assertIn("is not JSON", str(raised.exception))

    def test_a_string_may_start_with_a_bracket_and_a_list_may_repeat_its_key(self):
        self.assertEqual(cli.parse_params(["text=[draft]"], SCHEMA), {"text": "[draft]"})
        self.assertEqual(cli.parse_params(["names=a", "names=b"], SCHEMA), {"names": ["a", "b"]})
        self.assertEqual(cli.parse_params(['names=["a","b"]'], SCHEMA), {"names": ["a", "b"]})

    def test_a_queued_job_prints_what_it_waits_for(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.print_envelope({"state": "queued", "command": "ci.run", "job": "j_X", "summary": None,
                                "waiting": {"reason": "lock ci:demo held by j_Y", "since": None}}, False)
        self.assertIn("waiting: lock ci:demo held by j_Y", out.getvalue())

    def test_a_ci_run_shows_each_checks_and_steps_peak_memory(self):
        steps = [{"name": "compile", "exitCode": 0, "seconds": 12.0, "peakRssBytes": 3 * 2**30},
                 {"name": "test", "exitCode": 0, "seconds": 30.5, "peakRssBytes": 512 * 2**20}]
        engine = {"jobs": {"build": {"status": "passed", "steps": steps, "peakRssBytes": 3 * 2**30},
                           "docs": {"status": "skipped", "reason": "needs build"},
                           "old": {"status": "passed", "steps": [{"name": "x", "exitCode": 0, "seconds": 1.0}]}}}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.print_envelope({"state": "settled", "outcome": "ok", "command": "ci.run", "job": "j_X",
                                "summary": "1 passed", "data": {"engine": engine}}, False)
        lines = out.getvalue().splitlines()
        self.assertIn("  check build: passed, 42.5 s, peak 3.00 GiB", lines)
        self.assertIn("    compile: exit 0, 12.0 s, peak 3.00 GiB", lines)
        self.assertIn("    test: exit 0, 30.5 s, peak 512 MiB", lines)
        self.assertIn("    x: exit 0, 1.0 s", lines)  # a summary from before peaks were kept shows none
        self.assertFalse([line for line in lines if "check docs" in line])  # it did not run
        # ci.host-run's engine is a list: one summary per lease
        self.assertEqual(cli.check_lines({"engine": [engine, {"jobs": {}}]})[0],
                         "  check build: passed, 42.5 s, peak 3.00 GiB")


class Verbs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = Daemon().start()
        cls.env = {"WB_URL": cls.d.client.url, "WB_TOKEN": cls.d.token}

    @classmethod
    def tearDownClass(cls):
        cls.d.stop()

    def wb(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        saved = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                args = cli.build_parser().parse_args(list(argv))
                code = args.function(args)
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        return code, out.getvalue(), err.getvalue()

    def test_run_prints_the_job_id_at_once_on_stderr_and_one_envelope_on_stdout(self):
        code, out, err = self.wb("run", "selftest.echo", "text=cli", "--json")
        self.assertEqual(code, 0)
        envelope = json.loads(out)
        self.assertIn(envelope["job"], err.splitlines()[0])
        quiet = self.wb("run", "selftest.echo", "text=cli", "--json", "--quiet")
        self.assertEqual(quiet[2], "")

    def test_jobs_and_logs_give_json(self):
        self.wb("run", "selftest.echo", "text=listed", "--quiet")
        code, out, _ = self.wb("jobs", "--json", "--limit", "3")
        self.assertEqual(code, 0)
        self.assertTrue(isinstance(json.loads(out), list))
        code, out, _ = self.wb("logs", "--json", "-n", "5")
        self.assertEqual(code, 0)
        self.assertTrue(isinstance(json.loads(out), list))


    def test_a_caller_label_is_kept_and_filters_jobs(self):
        os.environ["WB_CALLER"] = "session-a"
        try:
            code, out, _ = self.wb("run", "selftest.echo", "text=mine", "--json", "--quiet")
            self.assertEqual(json.loads(out)["caller"], "session-a")
            code, out, _ = self.wb("jobs", "--mine", "--json")
            self.assertTrue(json.loads(out) and all(j["caller"] == "session-a" for j in json.loads(out)))
            code, out, _ = self.wb("job", "--json")
            self.assertEqual(json.loads(out)["caller"], "session-a")
        finally:
            os.environ.pop("WB_CALLER", None)

    def test_wb_dev_runs_the_working_tree_on_purpose_past_the_version_handshake(self):
        from unittest import mock
        from wb import WbError, client
        saved = os.getcwd()
        os.chdir(REPO)
        try:
            with mock.patch.object(client, "_COMMIT", ["0" * 40]), mock.patch.dict(os.environ, {}):
                os.environ.pop("WB_DEV", None)
                with self.assertRaises(WbError) as raised:  # a CLI on other code than the daemon's
                    self.wb("run", "selftest.echo", "text=refused", "--quiet")
                self.assertIn("wb dev", str(raised.exception))
                code, out, _ = self.wb("dev", "run", "selftest.echo", "text=dev", "--json", "--quiet")
                self.assertEqual((code, json.loads(out)["outcome"]), (0, "ok"))
                self.assertEqual(os.environ.get("WB_DEV"), "1")
        finally:
            os.chdir(saved)


class Dev(unittest.TestCase):
    """wb dev: the working tree's own CLI, found from the folder you stand in."""

    def setUp(self):
        import subprocess
        import tempfile
        self.folder = tempfile.TemporaryDirectory()
        self.tree = Path(self.folder.name) / "wb-topic"
        for name in ("sdk/python/wb/cli.py", "core/wb_core/__init__.py"):
            (self.tree / name).parent.mkdir(parents=True, exist_ok=True)
            (self.tree / name).write_text("", encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(self.tree)], check=True)
        self.saved = os.getcwd()

    def tearDown(self):
        os.chdir(self.saved)
        self.folder.cleanup()

    def test_the_tree_is_a_workbench_checkout_or_worktree_and_nothing_else(self):
        self.assertEqual(cli.dev_tree(self.tree / "sdk").resolve(), self.tree.resolve())
        self.assertEqual(cli.dev_tree(REPO / "tests").resolve(), REPO.resolve())
        other = Path(self.folder.name) / "other"
        other.mkdir()
        import subprocess
        subprocess.run(["git", "init", "-q", str(other)], check=True)
        self.assertIsNone(cli.dev_tree(other))

    def test_another_working_tree_runs_its_own_cli_with_wb_dev_set(self):
        import subprocess
        from unittest import mock
        real, started = subprocess.run, []

        def run(command, **kwargs):  # git answers for real; the working tree's CLI is only recorded
            if command[0] == "git":
                return real(command, **kwargs)
            started.append((command, kwargs["env"]))
            return subprocess.CompletedProcess(command, 0)

        os.chdir(self.tree)
        with mock.patch("subprocess.run", side_effect=run), mock.patch.object(cli, "main") as in_process, \
                mock.patch.dict(os.environ, {}):
            args = cli.build_parser().parse_args(["dev", "status", "--json"])
            self.assertEqual(args.function(args), 0)
        in_process.assert_not_called()  # not this CLI: the tree's own
        [(command, env)] = started
        self.assertEqual(command[1:], ["-m", "wb", "status", "--json"])
        self.assertEqual((env["WB_DEV"], Path(env["WB_REPO"]).resolve()), ("1", self.tree.resolve()))
        self.assertEqual(Path(env["PYTHONPATH"].split(os.pathsep)[0]).resolve(),
                         (self.tree / "sdk" / "python").resolve())

    def test_no_verb_is_a_usage_error(self):
        args = cli.build_parser().parse_args(["dev"])
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(args.function(args), 2)


if __name__ == "__main__":
    unittest.main()
