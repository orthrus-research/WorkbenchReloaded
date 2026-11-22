"""lab.install-server with a fake installer: the pin is checked before anything runs, what it installs is kept by
SHA-256 with a receipt, a failing installer is a finding, and the EULA is accepted only when asked."""

import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path[:0] = [str(REPO / "core"), str(REPO / "sdk" / "python"), str(REPO / "modules" / "lab")]

from wb_core.module import Context, ParamError  # noqa: E402
from wb_lab import install  # noqa: E402

INSTALLER = r'''
import pathlib, sys
target = pathlib.Path(sys.argv[sys.argv.index("--installServer") + 1])
if "--fail" in sys.argv:
    print("downloading libraries"); print("could not reach the library server"); raise SystemExit(2)
(target / "libraries" / "net" / "loader").mkdir(parents=True)
(target / "libraries" / "net" / "loader" / "loader.jar").write_bytes(b"loader bytes")
(target / "run.sh").write_text("java @user_jvm_args.txt @libraries/args.txt nogui\n")
(target / "user_jvm_args.txt").write_text("# your JVM options\n")
print("installed")
'''


class InstallServer(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="wb-install-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.installer = self.root / "loader-installer.py"
        self.installer.write_text(INSTALLER, encoding="utf-8")
        self.pin = hashlib.sha256(self.installer.read_bytes()).hexdigest()

    def run_install(self, name: str, *, sha256=None, launch=None, eula=False):
        ctx = Context({"job": "direct", "command": "lab.install-server", "module": "lab",
                       "work": str(self.root / name / "work"), "out": str(self.root / name / "out")}, lambda e: None)
        result = install.install_server(ctx, str(self.installer), sha256 or self.pin, "",
                                        launch or [sys.executable, "{installer}", "--installServer", "{target}"],
                                        eula, 60)
        return ctx, result, self.root / name / "out"

    def test_an_installed_server_is_kept_by_sha256(self):
        ctx, result, out = self.run_install("ok")
        self.assertEqual(ctx.outcome, "ok", ctx.findings)
        files = json.loads((out / "inventory.json").read_text(encoding="utf-8"))["files"]
        self.assertEqual(sorted(files), ["libraries/net/loader/loader.jar", "run.sh", "user_jvm_args.txt"])
        self.assertEqual(files["libraries/net/loader/loader.jar"]["sha256"], hashlib.sha256(b"loader bytes").hexdigest())
        receipt = json.loads((out / "receipt.json").read_text(encoding="utf-8"))
        self.assertEqual((receipt["installer"]["sha256"], receipt["exit"], receipt["eula"]), (self.pin, 0, False))
        self.assertFalse((out / "server" / "eula.txt").exists())  # nobody accepted it
        self.assertIn("installed", (out / "install.log").read_text(encoding="utf-8"))
        self.assertEqual(result["files"], 3)

    def test_the_eula_is_written_only_when_asked(self):
        _, _, out = self.run_install("eula", eula=True)
        self.assertIn("eula=true", (out / "server" / "eula.txt").read_text(encoding="utf-8"))

    def test_another_installer_is_refused_before_it_runs(self):
        ctx, result, out = self.run_install("pin", sha256="0" * 64)
        self.assertEqual(ctx.outcome, "finding")
        self.assertIn("not the pinned 000000000000; nothing was run", ctx.findings[0])
        self.assertFalse((out / "server").exists())

    def test_a_failing_installer_is_a_finding_naming_its_last_line(self):
        ctx, result, _ = self.run_install(
            "fail", launch=[sys.executable, "{installer}", "--installServer", "{target}", "--fail"])
        self.assertEqual(ctx.outcome, "finding")
        self.assertIn("exited 2: could not reach the library server", ctx.findings[0])

    def test_an_unknown_placeholder_is_refused(self):
        with self.assertRaises(ParamError) as raised:
            self.run_install("bad", launch=["{java}", "-jar", "{installr}"])
        self.assertIn("{installr}", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
