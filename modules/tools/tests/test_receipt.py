import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from orthrus_tools import cli, receipt
from tests.support import capture, git_repo

BUILD = """
import pathlib, sys
out = pathlib.Path("build")
(out / "libs").mkdir(parents=True, exist_ok=True)
(out / "libs" / "app.jar").write_bytes(b"jar " + sys.argv[1].encode())
(out / "TEST-a.xml").write_text(
    '<testsuite tests="3" failures="%s" errors="0" skipped="0"/>' % sys.argv[2]
)
print("built")
"""


class ReceiptTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = git_repo(Path(self.temporary.name).resolve())
        (self.root / ".gitignore").write_text("build/\n.local/\n", encoding="utf-8")
        (self.root / "build.py").write_text(BUILD, encoding="utf-8")
        (self.root / "jdk-release").write_text('JAVA_VERSION="26"\n', encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "--all"], check=True)

    def tearDown(self):
        self.temporary.cleanup()

    def run_build(self, run_id, *build, options=()):
        code, out, _ = capture(
            receipt.main,
            "run",
            "--run-id",
            run_id,
            "--root",
            str(self.root),
            "--artifact",
            "build/libs/*.jar",
            "--junit",
            "build/TEST-*.xml",
            "--identity",
            "jdk-release",
            *options,
            "--json",
            "--",
            sys.executable,
            "build.py",
            *build,
        )
        document = json.loads(out)
        path = self.root / ".local/receipts" / run_id / "receipt.json"
        return code, document, json.loads(path.read_text(encoding="utf-8"))

    def test_a_passing_build_records_sources_artifacts_tests_and_identity(self):
        code, document, written = self.run_build("build-001", "one", "0")
        self.assertEqual(code, cli.OK, document["messages"])
        self.assertEqual(written["schema"], receipt.SCHEMA)
        self.assertEqual(written["status"], "passed")
        self.assertEqual(set(written["source"]["files"]), {".gitignore", "build.py", "jdk-release"})
        self.assertEqual([a["path"] for a in written["artifacts"]], ["build/libs/app.jar"])
        self.assertTrue(written["artifacts"][0]["fresh"])
        self.assertEqual(written["tests"]["totals"]["tests"], 3)
        self.assertEqual(written["identity"][0]["path"], "jdk-release")
        self.assertTrue(written["sourceUnchanged"])
        log = (self.root / ".local/receipts/build-001/build.log").read_text(encoding="utf-8")
        self.assertIn("built", log)

    def test_failed_tests_a_failed_command_and_stale_outputs_fail_the_receipt(self):
        code, document, written = self.run_build("b1", "one", "2")
        self.assertEqual(code, cli.FINDING)
        self.assertIn("2 failures and 0 errors in tests", written["errors"])
        code, document, written = self.run_build("b2", "one", "0", options=())
        self.assertEqual(code, cli.OK)
        # A command that writes nothing leaves the earlier build's outputs stale.
        code, out, _ = capture(
            receipt.main,
            "run",
            "--run-id",
            "b3",
            "--root",
            str(self.root),
            "--artifact",
            "build/libs/*.jar",
            "--json",
            "--",
            sys.executable,
            "-c",
            "raise SystemExit(4)",
        )
        self.assertEqual(code, cli.FINDING)
        errors = json.loads(out)["result"]["errors"]
        self.assertIn("the build exited with code 4", errors)
        self.assertIn("build/libs/app.jar was not written by this build", errors)

    def test_a_run_id_is_never_reused(self):
        self.run_build("same", "one", "0")
        code, out, _ = capture(
            receipt.main,
            "run",
            "--run-id",
            "same",
            "--root",
            str(self.root),
            "--json",
            "--",
            sys.executable,
            "build.py",
            "one",
            "0",
        )
        self.assertEqual(code, cli.USAGE)
        self.assertIn("never overwritten", json.loads(out)["messages"][0]["text"])

    def test_a_missing_command_or_bad_run_id_is_a_usage_error(self):
        code, _, _ = capture(receipt.main, "run", "--run-id", "x", "--root", str(self.root))
        self.assertEqual(code, cli.USAGE)
        code, _, _ = capture(
            receipt.main, "run", "--run-id", "../x", "--root", str(self.root), "--", "true"
        )
        self.assertEqual(code, cli.USAGE)

    def test_verify_notices_changed_artifacts_and_sources(self):
        self.run_build("v1", "one", "0")
        path = str(self.root / ".local/receipts/v1/receipt.json")
        code, out, _ = capture(receipt.main, "verify", path, "--root", str(self.root), "--json")
        self.assertEqual(code, cli.OK, out)
        (self.root / "build/libs/app.jar").write_bytes(b"other")
        (self.root / "build.py").write_text(BUILD + "\n# edited\n", encoding="utf-8")
        code, out, _ = capture(receipt.main, "verify", path, "--root", str(self.root), "--json")
        self.assertEqual(code, cli.FINDING)
        result = json.loads(out)["result"]
        self.assertEqual(result["artifacts"], [{"path": "build/libs/app.jar", "state": "changed"}])
        self.assertFalse(result["sourceSame"])
        self.assertIn("build.py", result["problems"][-1])
        (self.root / "build/libs/app.jar").write_bytes(b"jar one")
        code, out, _ = capture(
            receipt.main, "verify", path, "--root", str(self.root), "--artifacts-only"
        )
        self.assertEqual(code, cli.OK, out)

    def test_settings_come_from_orthrus_toml(self):
        (self.root / "orthrus.toml").write_text(
            '[receipt]\nout = "evidence"\nartifacts = ["build/libs/*.jar"]\n', encoding="utf-8"
        )
        code, out, _ = capture(
            receipt.main,
            "run",
            "--run-id",
            "s1",
            "--root",
            str(self.root),
            "--json",
            "--",
            sys.executable,
            "build.py",
            "one",
            "0",
        )
        self.assertEqual(code, cli.OK, out)
        written = json.loads((self.root / "evidence/s1/receipt.json").read_text(encoding="utf-8"))
        self.assertEqual(len(written["artifacts"]), 1)
        self.assertNotIn("evidence/s1/build.log", written["source"]["files"])


if __name__ == "__main__":
    unittest.main()
