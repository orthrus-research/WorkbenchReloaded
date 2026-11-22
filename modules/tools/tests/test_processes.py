"""The real backend for this platform, observed on a short-lived child Python process."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from orthrus_tools.process_lifecycle import ProcessLifecycle
from orthrus_tools.processes import ProcessGone, default_api

SUPPORTED = sys.platform == "win32" or sys.platform.startswith("linux")


@unittest.skipUnless(SUPPORTED, "no process backend on this platform")
class BackendTest(unittest.TestCase):
    def setUp(self):
        script = "import sys, time\nprint('child ready', flush=True)\ntime.sleep(30)\n"
        self.child = subprocess.Popen(
            [sys.executable, "-c", script], stdout=subprocess.PIPE, text=True
        )
        self.addCleanup(self._stop)
        self.api = default_api()

    def _stop(self):
        if self.child.poll() is None:
            self.child.kill()
        self.child.wait(10)
        if self.child.stdout:
            self.child.stdout.close()

    def test_identity_and_memory_of_a_live_child(self):
        handle = self.api.open(self.child.pid)
        try:
            identity = self.api.identity(handle)
            self.assertEqual(identity["pid"], self.child.pid)
            self.assertTrue(identity["creationId"])
            self.assertEqual(identity, {**identity, "pid": self.child.pid})
            memory = self.api.memory(handle)
            self.assertGreater(memory["workingSetBytes"], 0)
            self.assertGreaterEqual(memory["peakWorkingSetBytes"], memory["workingSetBytes"])
            self.assertGreater(memory["privateCommitBytes"], 0)
        finally:
            self.api.close(handle)

    def test_lifecycle_binds_and_reads_both_endpoints(self):
        lifecycle = ProcessLifecycle(self.child, "child ready", "child")
        lifecycle.observe_line(self.child.stdout.readline())
        self.assertTrue(lifecycle.ready_observed)
        self.assertTrue(lifecycle.capture("ready")["available"])
        self.assertTrue(lifecycle.capture("endBeforeQuit")["available"])
        lifecycle.close()
        self.assertTrue(lifecycle.report()["handleClosed"])

    def test_an_exited_process_is_gone(self):
        handle = self.api.open(self.child.pid)
        self.child.kill()
        self.child.wait(10)
        try:
            if sys.platform.startswith("linux"):
                with self.assertRaises(ProcessGone):
                    self.api.identity(handle)
        finally:
            self.api.close(handle)

    def test_the_java_inventory_runs(self):
        self.assertIsInstance(self.api.java_processes(), list)


@unittest.skipUnless(sys.platform.startswith("linux"), "procfs parsing")
class LinuxParsingTest(unittest.TestCase):
    def test_a_process_name_with_spaces_and_parentheses_parses(self):
        from orthrus_tools.processes.linux import LinuxProcessApi

        with tempfile.TemporaryDirectory() as temporary:
            proc = Path(temporary)
            (proc / "self").mkdir()
            (proc / "self" / "stat").write_text("1 (x) S")
            (proc / "77").mkdir()
            fields = ["S"] + ["0"] * 10 + ["300", "200"] + ["0"] * 6 + ["12345"] + ["0"] * 10
            (proc / "77" / "stat").write_text("77 (odd (name) x) " + " ".join(fields))
            api = LinuxProcessApi(proc)
            identity = api.identity_for(77)
            self.assertTrue(identity["creationId"].endswith(":12345"))
            tick = 1_000_000_000 // api.ticks_per_second
            self.assertEqual(identity["userCpuNanos"], 300 * tick)
            self.assertEqual(identity["kernelCpuNanos"], 200 * tick)
            with self.assertRaises(ProcessGone):
                api.identity_for(78)


if __name__ == "__main__":
    unittest.main()
