"""Processes a job starts: a step returns when its process exits even while a grandchild holds the pipe, a silent
tool is stopped at its deadline, a timeout is a finding that is never cached, and nothing a job started outlives it."""

import os
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import Daemon  # noqa: E402

from wb_core import process  # noqa: E402

# Starts a grandchild that inherits the pipe and sleeps, then exits at once (a launcher leaving its daemon behind).
HOLDS_PIPE = ("import subprocess, sys; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
              "print('started', flush=True)")


def alive(pid: int) -> bool:
    if os.name == "nt":
        listed = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"], capture_output=True,
                                text=True).stdout
        return f'"{pid}"' in listed
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:  # a zombie no one has reaped yet is gone for every purpose here
        with open(f"/proc/{pid}/stat", encoding="ascii") as handle:
            return handle.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


class Primitive(unittest.TestCase):
    def test_a_step_returns_when_its_process_exits_though_a_grandchild_holds_the_pipe(self):
        started = time.monotonic()
        done = process.run([sys.executable, "-c", HOLDS_PIPE], timeout=60)
        seconds = time.monotonic() - started
        self.assertEqual(done.code, 0)
        self.assertIn("started", done.output)
        self.assertLess(seconds, process.PIPE_GRACE + 2, f"took {seconds:.1f} s")

    def test_a_silent_tool_is_stopped_at_its_deadline(self):
        started = time.monotonic()
        done = process.run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=1)
        self.assertTrue(done.timed_out)
        self.assertIsNone(done.code)
        self.assertLess(time.monotonic() - started, 8)

    def test_a_cancel_stops_the_tool(self):
        cancelled = threading.Event()
        threading.Timer(0.5, cancelled.set).start()
        started = time.monotonic()
        done = process.run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=60, cancelled=cancelled)
        self.assertTrue(done.cancelled)
        self.assertLess(time.monotonic() - started, 8)


# Owns its tree as a worker or wb play does, starts a sleeper through the core, prints its pid, then waits.
OWNER = ("import sys, time; from wb_core import process; process.own_tree(); "
         "p = process.start([sys.executable, '-c', 'import time; time.sleep(60)'], owned=True, no_window=True); "
         "print(p.pid, flush=True); time.sleep(60)")


class Owner(unittest.TestCase):
    def test_what_an_owner_started_dies_when_the_owner_is_killed(self):
        env = dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path))
        owner = subprocess.Popen([sys.executable, "-c", OWNER], stdout=subprocess.PIPE, text=True, env=env)
        child = int(owner.stdout.readline())
        self.assertTrue(alive(child))
        owner.kill()  # TerminateProcess on Windows, SIGKILL elsewhere: no clean-up code runs
        owner.wait(10)
        end = time.time() + 15
        while alive(child) and time.time() < end:
            time.sleep(0.2)
        self.assertFalse(alive(child), f"process {child} outlived the owner that was killed")


class Jobs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = Daemon().start()
        cls.c = cls.d.client

    @classmethod
    def tearDownClass(cls):
        cls.d.stop()

    def test_nothing_a_job_started_outlives_it(self):
        result = self.c.submit("selftest.orphan", {"seconds": "60s"}).result(timeout=60)
        self.assertEqual(result.outcome, "ok", result.summary)
        pid = result.data.pid
        end = time.time() + 15
        while alive(pid) and time.time() < end:
            time.sleep(0.2)
        self.assertFalse(alive(pid), f"process {pid} outlived its job")

    def test_a_timeout_is_a_finding_that_is_never_cached(self):
        first = self.c.submit("selftest.tool", {"seconds": 30, "limit": 1}).result(timeout=60)
        self.assertEqual(first.outcome, "finding", first.summary)
        self.assertIn("stopped at its limit", first.summary)
        self.assertEqual([r["kind"] for r in first.reasons], ["timeout"])
        again = self.c.submit("selftest.tool", {"seconds": 30, "limit": 1}).result(timeout=60)
        self.assertFalse(again["cache"]["hit"])  # the same key, run again: a stopped run vouches for nothing


class Engine(unittest.TestCase):
    def test_an_engine_step_returns_when_its_process_exits(self):
        import tempfile
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "modules" / "tools" / "src"))
        from orthrus_tools import actions
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder) / "step.log"
            started = time.monotonic()
            # The grandchild outlives the step here (no job owns it), so it must not sit in the folder removed below.
            code, seconds, _, _ = actions.run_step([sys.executable, "-c", HOLDS_PIPE], log, Path(__file__).parent, {})
            self.assertEqual(code, 0)
            self.assertLess(time.monotonic() - started, actions.PIPE_GRACE + 2)
            self.assertIn("started", log.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
