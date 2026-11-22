"""Lifecycle contracts with a fake process API; no game, server or timed sampler is started."""

import ctypes
import itertools
import unittest

from orthrus_tools.process_lifecycle import ProcessLifecycle
from orthrus_tools.processes.windows import FileTime, ProcessMemoryCountersEx


class FakeProcess:
    pid = 4242

    def __init__(self, statuses=None):
        self.statuses = list(statuses or [])
        self.returncode = None

    def poll(self):
        return self.statuses.pop(0) if self.statuses else self.returncode


class FakeApi:
    platform = "fake"

    def __init__(self):
        self.identity_value = {
            "pid": 4242,
            "creationId": "134000000000000001",
            "creationClock": "fake",
            "kernelCpuNanos": 20,
            "userCpuNanos": 30,
        }
        self.opened, self.closed, self.memory_calls = [], [], 0
        self.failure, self.after_memory, self.close_failure = None, None, None

    def open(self, pid):
        self.opened.append(pid)
        return 77

    def identity(self, handle):
        if handle != 77:
            raise AssertionError("unknown handle")
        return dict(self.identity_value)

    def memory(self, handle):
        if handle != 77:
            raise AssertionError("unknown handle")
        self.memory_calls += 1
        if self.failure:
            raise OSError(self.failure)
        if self.after_memory:
            self.after_memory()
        return {
            "workingSetBytes": 5 * 1024**3,
            "peakWorkingSetBytes": 7 * 1024**3,
            "privateCommitBytes": 9 * 1024**3,
            "peakPrivateCommitBytes": 10 * 1024**3,
        }

    def close(self, handle):
        self.closed.append(handle)
        if self.close_failure:
            raise OSError(self.close_failure)


class ProcessLifecycleTest(unittest.TestCase):
    def make(self, marker="Done (", process=None, api=None):
        process, api = process or FakeProcess(), api or FakeApi()
        clock = itertools.count(1000, 10).__next__
        probe = ProcessLifecycle(
            process,
            marker,
            "server",
            500,
            "fixture-launch-UTC",
            api_factory=lambda: api,
            clock=clock,
        )
        return probe, process, api

    def test_win32_structures_keep_filetime_and_pointer_sized_counters(self):
        self.assertEqual(ctypes.sizeof(FileTime), 8)
        expected = 8 + 9 * ctypes.sizeof(ctypes.c_size_t)
        self.assertEqual(ctypes.sizeof(ProcessMemoryCountersEx), expected)
        self.assertEqual(FileTime(1, 2).value(), (2 << 32) | 1)

    def test_monotonic_startup_and_only_two_idempotent_memory_endpoints(self):
        probe, _, api = self.make()
        for _ in range(10):
            probe.observe_line("ordinary log line")
        self.assertEqual(api.memory_calls, 0)
        probe.observe_line("\x1b[32mDone (1.2s)!\x1b[0m")
        elapsed = probe.report()["launchToReadyNanos"]
        probe.observe_line("Done (another event)")
        self.assertEqual(probe.report()["launchToReadyNanos"], elapsed)
        first = probe.capture("ready")
        self.assertTrue(first["available"])
        self.assertEqual(first["privateCommitBytes"], 9 * 1024**3)
        for _ in range(10):
            self.assertIs(first, probe.capture("ready"))
        end = probe.capture("endBeforeQuit")
        self.assertIs(end, probe.capture("endBeforeQuit"))
        self.assertEqual(api.memory_calls, 2)
        self.assertEqual(api.opened, [4242])
        self.assertGreater(elapsed, 0)
        probe.close()
        probe.close()
        self.assertEqual(api.closed, [77])

    def test_a_custom_marker_and_a_missed_ready_stay_explicit(self):
        probe, _, api = self.make(marker="VIEW screen=none")
        probe.observe_line("VIEW screen=MainMenu")
        self.assertFalse(probe.capture("ready")["available"])
        self.assertEqual(api.memory_calls, 0)
        self.assertIn("not observed", probe.report()["endpoints"]["ready"]["failure"])
        probe.observe_line("VIEW screen=none")
        self.assertTrue(probe.report()["readyObserved"])
        self.assertFalse(probe.capture("ready")["available"])
        probe.close()

    def test_an_empty_marker_is_refused(self):
        with self.assertRaises(ValueError):
            ProcessLifecycle(FakeProcess(), "", api_factory=FakeApi)

    def test_unknown_platform_or_api_failure_has_no_fabricated_memory_zero(self):
        def unavailable():
            raise OSError("unsupported fixture platform")

        probe = ProcessLifecycle(FakeProcess(), "Done (", api_factory=unavailable)
        probe.observe_line("Done (fixture)")
        row = probe.capture("ready")
        self.assertFalse(row["available"])
        self.assertIn("unsupported", row["failure"])
        self.assertNotIn("workingSetBytes", row)
        self.assertFalse(probe.report()["bindingAvailable"])
        probe.close()

    def test_process_exit_before_and_during_binding_does_not_attach_replacement(self):
        for statuses, opened in [([0], []), ([None, 0], [4242])]:
            api, process = FakeApi(), FakeProcess(statuses)
            probe, _, _ = self.make(process=process, api=api)
            self.assertFalse(probe.report()["bindingAvailable"])
            self.assertEqual(api.opened, opened)
            self.assertEqual(api.memory_calls, 0)
            if opened:
                self.assertEqual(api.closed, [77])
            probe.close()

    def test_pid_or_creation_mismatch_before_snapshot_refuses_memory(self):
        for field, value in [("pid", 4243), ("creationId", "134000000000000002")]:
            probe, _, api = self.make()
            probe.observe_line("Done (fixture)")
            api.identity_value[field] = value
            row = probe.capture("ready")
            self.assertFalse(row["available"])
            self.assertIn("identity mismatch", row["failure"])
            self.assertEqual(api.memory_calls, 0)
            probe.close()

    def test_creation_mismatch_during_snapshot_never_publishes_memory(self):
        probe, _, api = self.make()
        probe.observe_line("Done (fixture)")
        api.after_memory = lambda: api.identity_value.update(creationId="different")
        row = probe.capture("ready")
        self.assertFalse(row["available"])
        self.assertNotIn("workingSetBytes", row)
        self.assertEqual(api.memory_calls, 1)
        probe.close()

    def test_exit_before_or_during_snapshot_is_explicitly_unavailable(self):
        probe, process, api = self.make()
        probe.observe_line("Done (fixture)")
        process.returncode = 0
        self.assertFalse(probe.capture("ready")["available"])
        self.assertEqual(api.memory_calls, 0)
        probe.close()
        probe, process, api = self.make()
        probe.observe_line("Done (fixture)")
        api.after_memory = lambda: setattr(process, "returncode", 0)
        row = probe.capture("ready")
        self.assertFalse(row["available"])
        self.assertNotIn("peakWorkingSetBytes", row)
        probe.close()

    def test_memory_failure_does_not_prevent_later_endpoint_or_cleanup(self):
        probe, _, api = self.make()
        probe.observe_line("Done (fixture)")
        api.failure = "fixture access denied"
        self.assertFalse(probe.capture("ready")["available"])
        api.failure = None
        self.assertTrue(probe.capture("endBeforeQuit")["available"])
        probe.close()
        self.assertEqual(api.closed, [77])

    def test_invalid_creation_identity_and_close_failure_remain_visible(self):
        for invalid in ("0", "boot:0", ""):
            api = FakeApi()
            api.identity_value["creationId"] = invalid
            probe, _, _ = self.make(api=api)
            self.assertFalse(probe.report()["bindingAvailable"], invalid)
            self.assertEqual(api.closed, [77])
        probe, _, api = self.make()
        api.close_failure = "fixture close denied"
        probe.close()
        self.assertIn("close denied", probe.report()["closeFailure"])
        self.assertFalse(probe.report()["handleClosed"])
        self.assertTrue(probe.report()["observerClosed"])
        probe.observe_line("Done (too late)")
        self.assertFalse(probe.report()["readyObserved"])


if __name__ == "__main__":
    unittest.main()
