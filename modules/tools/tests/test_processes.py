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


# A step whose grandchild holds about 200 MiB for two seconds, then exits; the step waits for it.
HOLDS_MEMORY = (
    "import subprocess, sys\n"
    "child = 'b = bytearray(200 * 2**20)\\nfor i in range(0, len(b), 4096): b[i] = 1\\n"
    "import time; time.sleep(2)'\n"
    "sys.exit(subprocess.call([sys.executable, '-c', child]))\n"
)
MIB = 2**20


@unittest.skipUnless(SUPPORTED, "no process backend on this platform")
class TreeMemoryTest(unittest.TestCase):
    def test_the_peak_counts_what_the_step_started(self):
        from orthrus_tools.processes.tree import TreeMemory

        step = subprocess.Popen([sys.executable, "-c", HOLDS_MEMORY])
        memory = TreeMemory(step, interval=0.1)
        self.assertEqual(step.wait(60), 0)
        peak = memory.stop()
        self.assertIn(memory.source, ("job", "proc"))
        self.assertGreater(memory.samples, 3)
        self.assertGreater(peak, 190 * MIB)  # the grandchild's 200 MiB, which the step never held
        self.assertLess(peak, 1024 * MIB)

    def test_no_backend_is_no_peak_rather_than_zero(self):
        from unittest import mock

        from orthrus_tools.processes import tree

        step = subprocess.Popen([sys.executable, "-c", "pass"])
        self.addCleanup(step.wait, 60)
        with mock.patch.object(tree.sys, "platform", "darwin"):
            memory = tree.TreeMemory(step, interval=0.1)
        self.assertEqual((memory.stop(), memory.source, memory.samples), (None, None, 0))


    def test_a_jvm_the_step_works_through_counts_while_it_is_connected(self):
        import os
        from unittest import mock

        from orthrus_tools.processes import tree

        # A server the step did not start, holding 200 MiB: a daemon an earlier step left running.
        server = subprocess.Popen([sys.executable, "-c", SERVES], stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (server.kill(), server.wait(10), server.stdout.close()))
        port = server.stdout.readline().strip()
        step = [sys.executable, "-c", CONNECTS, port]
        process = subprocess.Popen(step)
        alone = tree.TreeMemory(process, interval=0.1)
        self.assertEqual(process.wait(60), 0)
        self.assertLess(alone.stop(), 150 * MIB)  # not a JVM: a client of the step's, not its work
        self.assertFalse(alone.served)
        names = {Path(sys.executable).name, Path(os.path.realpath(sys.executable)).name}
        names |= {name[:15] for name in names}  # Linux keeps 15 characters of a process's name
        with mock.patch.object(tree, "JAVA", tuple(names)):
            process = subprocess.Popen(step)
            served = tree.TreeMemory(process, interval=0.1)
            self.assertEqual(process.wait(60), 0)
            peak = served.stop()
        self.assertTrue(served.served)
        self.assertGreater(peak, 190 * MIB)

    def test_the_other_end_of_a_local_connection_is_a_peer(self):
        from orthrus_tools.processes.tree import peers

        a, b, c = ("127.0.0.1", 50001), ("127.0.0.1", 41000), ("10.0.0.2", 443)
        rows = [(7, a, b), (9, b, a), (7, ("127.0.0.1", 50002), c), (8, ("x", 1), ("y", 2))]
        self.assertEqual(peers(rows, {7}), {9})
        self.assertEqual(peers(rows, {7, 9}), set())  # the step's own daemon is no peer


# A server holding 200 MiB that prints its port, then serves one connection until it closes.
SERVES = (
    "import socket\n"
    "b = bytearray(200 * 2**20)\n"
    "for i in range(0, len(b), 4096): b[i] = 1\n"
    "s = socket.socket(); s.bind(('127.0.0.1', 0)); s.listen(4)\n"
    "print(s.getsockname()[1], flush=True)\n"
    "while True:\n"
    "    c, _ = s.accept(); c.recv(1); c.close()\n"
)
CONNECTS = (
    "import socket, sys, time\n"
    "c = socket.create_connection(('127.0.0.1', int(sys.argv[1])))\n"
    "time.sleep(1.5); c.close()\n"
)


class ProcTreeTest(unittest.TestCase):
    """The Linux tree from a procfs laid out by hand, so it runs on any platform."""

    def test_descendants_and_the_steps_session_are_the_tree(self):
        from orthrus_tools.processes.tree import _Linux

        def stat(pid, parent, session, name="x"):
            fields = ["S", str(parent), str(pid), str(session)] + ["0"] * 40
            return f"{pid} ({name}) " + " ".join(fields)

        with tempfile.TemporaryDirectory() as temporary:
            proc = Path(temporary)
            rows = {10: (1, 10), 11: (10, 10), 12: (11, 10), 20: (1, 10), 30: (1, 30), 31: (30, 30)}
            (proc / "self").mkdir()
            (proc / "self" / "stat").write_text(stat(99, 1, 1))
            for pid, (parent, session) in rows.items():
                (proc / str(pid)).mkdir()
                name = "odd (name) x" if pid == 12 else "x"
                (proc / str(pid) / "stat").write_text(stat(pid, parent, session, name))
                (proc / str(pid) / "status").write_text(
                    f"Name:\tx\nVmHWM:\t{pid * 2} kB\nVmRSS:\t{pid} kB\n"
                )
            (proc / "10" / "status").write_text("Name:\tx\nState:\tZ (zombie)\n")
            tree = _Linux(10, proc)
            # 20's parent exited (it was reparented to 1), but it is still in the step's session.
            self.assertEqual(tree.members(), ({10, 11, 12, 20}, set()))
            self.assertEqual(tree.memory(12), (12 * 1024, 24 * 1024))
            with self.assertRaises(OSError):
                tree.memory(10)  # a zombie holds no resident memory


if __name__ == "__main__":
    unittest.main()
