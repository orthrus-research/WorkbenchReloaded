"""Every SDK against one live daemon (D24): Python, Node, Java, PowerShell and the Gradle plugin all run the same
conformance checks, and the generated envelope types are up to date with the schema."""

import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import REPO, Daemon  # noqa: E402

from wb import ApprovalRequired, WbError  # noqa: E402
from wb.client import Client  # noqa: E402

JDK25 = next(iter(sorted((Path(os.environ.get("ProgramFiles", "/")) / "Eclipse Adoptium").glob("jdk-25*"))),
             Path(os.environ.get("JAVA_HOME_25", "-")))


class SDKs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = Daemon().start()
        cls.env = dict(os.environ, WB_URL=f"http://127.0.0.1:{cls.d.port}", WB_TOKEN=cls.d.token)
        cls.env.pop("WB_JOB", None)

    @classmethod
    def tearDownClass(cls):
        cls.d.stop()

    def run_tool(self, args, cwd=REPO, env=None, timeout=600):
        done = subprocess.run(args, cwd=cwd, env=env or self.env, capture_output=True, text=True, timeout=timeout)
        return done.returncode, done.stdout + done.stderr

    def test_generated_types_are_current(self):
        code, out = self.run_tool([sys.executable, "schema/generate.py", "--check"])
        self.assertEqual(code, 0, out)

    def test_python(self):
        client = Client(self.env["WB_URL"], self.env["WB_TOKEN"])
        echo = client.submit("selftest.echo", {"text": "py", "times": 2}).result(timeout=60)
        self.assertEqual((echo.outcome, echo.exit_code, echo.data.text), ("ok", 0, "pypy"))
        self.assertEqual(client.submit("selftest.fail", {"outcome": "finding"}).result(timeout=60).code, 1)
        job = client.submit("selftest.spend", {"price": 0})
        with self.assertRaises(ApprovalRequired) as pending:
            job.result(timeout=60)
        job.approve(pending.exception.quote["id"], via="relayed")
        self.assertEqual(job.result(timeout=60).outcome, "ok")
        with self.assertRaises(WbError) as missing:
            client.submit("selftest.nothing", {})
        self.assertEqual(missing.exception.status, 404)

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_node(self):
        code, out = self.run_tool(["node", "--test", "sdk/node/test/conformance.test.ts"])
        self.assertEqual(code, 0, out)

    @unittest.skipUnless(shutil.which("javac"), "no JDK")
    def test_java(self):
        build = REPO / "sdk" / "java" / "build"
        shutil.rmtree(build, ignore_errors=True)
        sources = [str(p) for p in (REPO / "sdk" / "java" / "src" / "main" / "java").rglob("*.java")]
        code, out = self.run_tool(["javac", "--release", "17", "-d", str(build / "classes"), *sources])
        self.assertEqual(code, 0, out)
        code, out = self.run_tool(["javac", "--release", "17", "-cp", str(build / "classes"), "-d", str(build / "test"),
                                   str(REPO / "sdk/java/src/test/java/cc/orthrus/wb/Conformance.java")])
        self.assertEqual(code, 0, out)
        code, out = self.run_tool(["java", "-cp", os.pathsep.join([str(build / "classes"), str(build / "test")]),
                                   "cc.orthrus.wb.Conformance"])
        self.assertEqual(code, 0, out)
        self.assertIn("java conformance: ok", out)

    @unittest.skipUnless(shutil.which("pwsh"), "PowerShell 7 is not installed")
    def test_powershell(self):
        code, out = self.run_tool(["pwsh", "-NoProfile", "-File", "sdk/powershell/Conformance.ps1"])
        self.assertEqual(code, 0, out)
        self.assertIn("powershell conformance: ok", out)

    @unittest.skipUnless(shutil.which("gradle") and JDK25.exists(), "Gradle or JDK 25 is not installed")
    def test_gradle_plugin(self):
        env = dict(self.env, JAVA_HOME=str(JDK25))
        gradle = shutil.which("gradle")
        code, out = self.run_tool([gradle, "--offline", "--no-daemon", "-q", "-p", "sdk/gradle/sample", "wbEcho",
                                   "wbFindingAllowed"], env=env, timeout=900)
        self.assertEqual(code, 0, out)
        envelope = REPO / "sdk" / "gradle" / "sample" / "build" / "wb" / "wbEcho.json"
        self.assertIn('"outcome":"ok"', envelope.read_text(encoding="utf-8"))
        code, out = self.run_tool([gradle, "--offline", "--no-daemon", "-q", "-p", "sdk/gradle/sample", "wbFindingFails"],
                                  env=env, timeout=900)
        self.assertNotEqual(code, 0)
        self.assertIn("ended finding", out)

    @unittest.skipUnless(shutil.which("gradle") and JDK25.exists(), "Gradle or JDK 25 is not installed")
    def test_gradle_run_keeps_a_receipt(self):
        env = dict(self.env, JAVA_HOME=str(JDK25))
        gradle = [shutil.which("gradle"), "--offline", "--no-daemon", "-q", "-p", "sdk/gradle/sample"]
        receipts = REPO / "sdk" / "gradle" / "sample" / "build" / "wb"
        shutil.rmtree(receipts, ignore_errors=True)
        code, out = self.run_tool([*gradle, "wbRunHello"], env=env, timeout=900)
        self.assertEqual(code, 0, out)
        self.assertIn("hello gradle", out)
        receipt = json.loads((receipts / "wbRunHello.run.json").read_text(encoding="utf-8"))
        self.assertEqual((receipt["schema"], receipt["main_class"], receipt["args"], receipt["exit"]),
                         ("wb.gradle-run.v1", "sample.Hello", ["gradle"], 0))
        self.assertEqual(len(receipt["single_provider"]["sample.Hello"]), 1)  # one class path entry provides it
        folders = [e for e in receipt["classpath"] if e["kind"] == "folder"]
        self.assertTrue(folders and all(len(e["sha256"]) == 64 for e in folders), receipt["classpath"])
        code, out = self.run_tool([*gradle, "wbRunPinned"], env=env, timeout=900)
        self.assertNotEqual(code, 0)
        self.assertIn("wbRunPinned does not start", out)
        self.assertIn("not its pin 000000000000", out)
        self.assertFalse((receipts / "wbRunPinned.run.json").exists())  # it never ran
        code, out = self.run_tool([*gradle, "wbRunFails"], env=env, timeout=900)
        self.assertNotEqual(code, 0)
        self.assertIn("sample.Hello exited 3", out)
        failed = json.loads((receipts / "wbRunFails.run.json").read_text(encoding="utf-8"))
        self.assertEqual(failed["exit"], 3)  # the receipt is written before the task fails


if __name__ == "__main__":
    unittest.main()
