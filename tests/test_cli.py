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
from harness import Daemon  # noqa: E402

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


if __name__ == "__main__":
    unittest.main()
