"""The output contract, checked on every command tool the registry lists."""

import json
import unittest

from orthrus_tools import cli
from orthrus_tools.registry import COMMANDS
from tests.support import capture, capture_json


class ContractTest(unittest.TestCase):
    def test_every_tool_reports_a_usage_error_as_one_json_object(self):
        for tool in COMMANDS:
            with self.subTest(tool=tool.name):
                code, document = capture_json(tool.main, "--no-such-option")
                self.assertEqual(code, cli.USAGE)
                self.assertEqual(document["schema"], cli.RESULT_SCHEMA)
                self.assertEqual(document["tool"], tool.name)
                self.assertEqual(document["status"], "usage")
                self.assertEqual(document["exitCode"], cli.USAGE)
                self.assertEqual(document["messages"][0]["level"], "error")

    def test_every_tool_has_help_and_a_usage_error_without_json(self):
        for tool in COMMANDS:
            with self.subTest(tool=tool.name):
                code, out, _ = capture(tool.main, "--help")
                self.assertEqual(code, 0)
                self.assertIn("--json", out)
                code, out, err = capture(tool.main, "--no-such-option")
                self.assertEqual((code, out), (cli.USAGE, ""))
                self.assertTrue(err.startswith("orthrus-"), err)

    def test_json_is_accepted_after_a_subcommand(self):
        from orthrus_tools import jdk

        code, out, _ = capture(jdk.main, "list", "--json")
        document = json.loads(out)
        self.assertEqual(document["command"], "list")
        self.assertIn(code, (cli.OK, cli.ENVIRONMENT))
        self.assertIsInstance(document["result"]["jdks"], list)

    def test_output_routes_messages(self):
        import io

        stdout, stderr = io.StringIO(), io.StringIO()
        out = cli.Output("t", False, stdout, stderr)
        out.line("data")
        out.note("info")
        out.warn("careful")
        self.assertEqual(out.finish(cli.OK), cli.OK)
        self.assertEqual(stdout.getvalue(), "data\ninfo\n")
        self.assertEqual(stderr.getvalue(), "careful\n")

        stdout = io.StringIO()
        out = cli.Output("t", True, stdout, io.StringIO())
        out.line("dropped")
        out.result = {"x": 1}
        out.finish(cli.FINDING)
        document = json.loads(stdout.getvalue())
        self.assertEqual((document["status"], document["result"]), ("finding", {"x": 1}))

    def test_a_tool_error_keeps_partial_results(self):
        def build():
            return cli.parser("t", None)

        def command(args, out):
            out.result = {"done": 1}
            raise cli.ToolError(cli.ENVIRONMENT, "no network")

        code, document = capture_json(lambda argv: cli.run("t", build, command, argv))
        self.assertEqual(code, cli.ENVIRONMENT)
        self.assertEqual(document["result"], {"done": 1})
        self.assertEqual(document["messages"], [{"level": "error", "text": "no network"}])


if __name__ == "__main__":
    unittest.main()
