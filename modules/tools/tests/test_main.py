import unittest

from orthrus_tools import __version__, cli, main
from orthrus_tools.registry import COMMANDS
from tests.support import capture, capture_json


class UmbrellaTest(unittest.TestCase):
    def test_help_lists_every_command(self):
        code, out, _ = capture(main.main, "--help")
        self.assertEqual(code, 0)
        for name in [*main.BUILTINS, *(tool.name for tool in COMMANDS)]:
            self.assertIn(f"  {name} ", out)
        self.assertEqual(capture(main.main)[0], cli.USAGE)

    def test_version(self):
        self.assertEqual(capture(main.main, "--version")[1].strip(), f"orthrus-tools {__version__}")

    def test_dispatch_reaches_the_tool(self):
        code, document = capture_json(main.main, "stats", "missing.json")
        self.assertEqual((code, document["tool"]), (cli.USAGE, "stats"))

    def test_unknown_commands_and_libraries(self):
        code, document = capture_json(main.main, "nope")
        self.assertEqual((code, document["status"]), (cli.USAGE, "usage"))
        code, _, err = capture(main.main, "classfile")
        self.assertEqual(code, cli.USAGE)
        self.assertIn("library", err)


if __name__ == "__main__":
    unittest.main()
