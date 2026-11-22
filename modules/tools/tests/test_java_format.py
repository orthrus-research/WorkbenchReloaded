import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orthrus_tools import cli, java_format, jdk
from tests.support import capture_json

JAR = b"a stand-in formatter"


def fake_formatter(command, cwd, capture_output, text):
    """Stands in for google-java-format: a file is unformatted while it holds 'BAD'."""
    files = [a for a in command if a.endswith(".java")]
    stdout, stderr, code = [], [], 0
    for name in files:
        path = Path(cwd) / name
        text_ = path.read_text(encoding="utf-8")
        if "SYNTAX" in text_:
            stderr.append(f"{name}:1:1: error: class, interface or enum expected")
            code = 1
        elif "BAD" in text_:
            if "--replace" in command:
                path.write_text(text_.replace("BAD", "GOOD"), encoding="utf-8")
            else:
                stdout.append(name)
                code = 1
    return subprocess.CompletedProcess(command, code, "\n".join(stdout), "\n".join(stderr))


class JavaFormatTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        digest = hashlib.sha256(JAR).hexdigest()
        (self.root / "orthrus.toml").write_text(
            f'[java-format]\nversion = "0.0.1"\nsha256 = "{digest}"\nexclude = ["src/gen/**"]\n',
            encoding="utf-8",
        )
        tools = self.root / ".local/tools"
        tools.mkdir(parents=True)
        (tools / "google-java-format-0.0.1-all-deps.jar").write_bytes(JAR)
        self.home = self.root / "jdk"
        (self.home / "bin").mkdir(parents=True)
        (self.home / "bin" / jdk.EXECUTABLE).write_bytes(b"")
        (self.home / "release").write_text('JAVA_VERSION="21"\n', encoding="utf-8")
        for name, text in {
            "src/main/java/a/Good.java": "class Good {}",
            "src/main/java/a/Bad.java": "class BAD {}",
            "src/gen/Generated.java": "class BAD {}",
        }.items():
            (self.root / name).parent.mkdir(parents=True, exist_ok=True)
            (self.root / name).write_text(text, encoding="utf-8")
        patcher = mock.patch.object(java_format.subprocess, "run", side_effect=fake_formatter)
        self.runner = patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.temporary.cleanup()

    def tool(self, *argv):
        return capture_json(
            java_format.main, *argv, "--root", str(self.root), "--java", str(self.home)
        )

    def test_check_lists_what_would_change_and_skips_excluded_files(self):
        code, document = self.tool("check")
        self.assertEqual(code, cli.FINDING)
        self.assertEqual(document["result"]["files"], 2)
        self.assertEqual(document["result"]["changed"], ["src/main/java/a/Bad.java"])
        command = self.runner.call_args.args[0]
        self.assertEqual(command[0], str(self.home.resolve() / "bin" / jdk.EXECUTABLE))
        self.assertIn("--dry-run", command)

    def test_apply_formats_and_then_check_passes(self):
        code, document = self.tool("apply")
        self.assertEqual(code, cli.OK)
        self.assertEqual(document["result"]["changed"], ["src/main/java/a/Bad.java"])
        code, document = self.tool("check")
        self.assertEqual(code, cli.OK)

    def test_parse_errors_are_findings(self):
        (self.root / "src/main/java/a/Broken.java").write_text("SYNTAX", encoding="utf-8")
        code, document = self.tool("check", str(self.root / "src/main/java/a/Broken.java"))
        self.assertEqual(code, cli.FINDING)
        self.assertIn("error", document["result"]["errors"][0])

    def test_a_formatter_that_differs_from_its_pin_is_refused(self):
        (self.root / ".local/tools/google-java-format-0.0.1-all-deps.jar").write_bytes(b"x")
        code, document = self.tool("fetch")
        self.assertEqual(code, cli.FINDING)
        self.assertIn("pinned SHA-256", document["messages"][0]["text"])

    def test_a_new_version_needs_its_digest(self):
        (self.root / "orthrus.toml").write_text('[java-format]\nversion = "2.0"\n', "utf-8")
        code, document = self.tool("fetch")
        self.assertEqual(code, cli.USAGE)

    def test_a_missing_formatter_is_downloaded_and_verified(self):
        (self.root / ".local/tools/google-java-format-0.0.1-all-deps.jar").unlink()

        def download(url, target, expected):
            self.assertIn("/0.0.1/google-java-format-0.0.1-all-deps.jar", url)
            self.assertEqual(expected, hashlib.sha256(JAR).hexdigest())
            target.write_bytes(JAR)
            return expected

        with mock.patch.object(java_format.pins, "download", side_effect=download):
            code, document = self.tool("fetch")
        self.assertEqual(code, cli.OK)
        self.assertEqual(document["result"]["formatter"]["version"], "0.0.1")

    def test_long_file_lists_are_split(self):
        groups = java_format.chunks(["x" * 100] * 200, limit=1000)
        self.assertTrue(all(sum(len(i) + 1 for i in g) <= 1000 for g in groups))
        self.assertEqual(sum(len(g) for g in groups), 200)

    def test_no_jdk_is_an_environment_error(self):
        with mock.patch.object(jdk, "discover", return_value=[]):
            code, document = capture_json(java_format.main, "check", "--root", str(self.root))
        self.assertEqual(code, cli.ENVIRONMENT)


if __name__ == "__main__":
    unittest.main()
