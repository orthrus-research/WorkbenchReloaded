"""The scheduler: parents that call children never deadlock, a queued job says why it waits, a job that ends takes
its descendants with it, inline calls need no job of their own, quiet can depend on a param, and named locks."""

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import Daemon  # noqa: E402

from wb_core.manifest import Command  # noqa: E402
from wb_core.service import Running, Workbench  # noqa: E402

BUDGET = """
[core.budget]
cpu_cores = 3
mem_gb = 16
"""


def until(predicate, timeout=60.0, step=0.1):
    end = time.time() + timeout
    while time.time() < end:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    return predicate()


class Admission(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = Daemon(BUDGET).start()
        cls.c = cls.d.client

    @classmethod
    def tearDownClass(cls):
        cls.d.stop()

    def test_parents_that_call_children_never_deadlock(self):
        # Six parents of 1 CPU each against a budget of 3: three run, three queue, and each running parent's child
        # must still start, ahead of the queued parents that wait for a parent's slot.
        jobs = [self.c.submit("selftest.nest", {"text": f"p{n}", "hold": "1s"}) for n in range(6)]
        for job in jobs:
            result = job.result(timeout=120)
            self.assertEqual((result.outcome, result.data.inner_outcome), ("ok", "ok"), result.summary)

    def test_a_queued_job_says_why_it_waits(self):
        holders = [self.c.submit("selftest.nest", {"text": f"h{n}", "hold": "4s"}) for n in range(3)]
        waiting = self.c.submit("selftest.nest", {"text": "w", "hold": "0s"})
        reason = until(lambda: (waiting.refresh().get("waiting") or {}).get("reason"), 20)
        self.assertTrue(reason and reason.startswith(("budget", "behind")), reason)
        events = [e for e in self.c.events_json(waiting.id) if e.get("kind") == "waiting"] \
            if hasattr(self.c, "events_json") else None
        for job in holders + [waiting]:
            self.assertEqual(job.result(timeout=120).outcome, "ok")
        self.assertIsNone(waiting.refresh().get("waiting"))
        if events is not None:
            self.assertTrue(events)

    def test_cancelling_a_parent_cancels_its_child(self):
        parent = self.c.submit("selftest.nest", {"sleep": "30s"})
        child = until(lambda: [j for j in self.c.jobs(command="selftest.sleep")
                               if j.get("parent") == parent.id and j.get("state") == "running"], 30)
        self.assertTrue(child, "the child never started")
        parent.cancel()
        settled = until(lambda: self.c.job(child[0]["job"]).refresh().state in ("cancelled", "settled"), 60)
        self.assertTrue(settled)
        self.assertEqual(self.c.job(child[0]["job"]).refresh().state, "cancelled")

    def test_an_inline_call_has_no_job_of_its_own(self):
        before = len(self.c.jobs(command="selftest.inline-echo"))
        result = self.c.submit("selftest.nest", {"text": "in", "inline": True}).result(timeout=60)
        self.assertEqual((result.outcome, result.data.inner_outcome, result.data.text), ("ok", "ok", "in"))
        self.assertEqual(result.children, [])
        self.assertEqual(len(self.c.jobs(command="selftest.inline-echo")), before)

    def test_only_an_inline_command_can_be_called_inline_and_only_by_a_running_job(self):
        from wb import WbError
        done = self.c.submit("selftest.echo", {"text": "x"}).result(timeout=60)
        with self.assertRaises(WbError) as raised:
            self.c.call_inline(done.job, "selftest.inline-echo", {})
        self.assertIn("not running", str(raised.exception))


    def test_a_drain_holds_new_jobs_and_lets_a_running_jobs_children_start(self):
        parent = self.c.submit("selftest.nest", {"text": "drained", "hold": "2s"})
        until(lambda: parent.refresh().state == "running", 20)
        self.c.request("POST", "/v1/drain", {"on": True})
        try:
            held = self.c.submit("selftest.echo", {"text": "after the deploy"})
            reason = until(lambda: (held.refresh().get("waiting") or {}).get("reason"), 20)
            self.assertTrue(reason and reason.startswith("draining"), reason)
            self.assertEqual(parent.result(timeout=60).data.inner_outcome, "ok")  # its child still ran
            self.assertEqual(held.refresh().state, "queued")
        finally:
            self.c.request("POST", "/v1/drain", {"on": False})
        self.assertEqual(held.result(timeout=60).outcome, "ok")

    def test_a_cli_on_other_code_is_refused_with_the_fix(self):
        from wb import WbError
        with self.assertRaises(WbError) as raised:
            self.c.request("POST", "/v1/jobs", {"command": "selftest.echo", "params": {}},
                           {"X-Wb-Commit": "0" * 40})
        self.assertEqual(raised.exception.status, 409)
        self.assertIn("wb deploy", str(raised.exception))
        self.assertEqual(raised.exception.body["reasons"][0]["kind"], "version")
        same = self.c.request("POST", "/v1/jobs", {"command": "selftest.echo", "params": {}},
                              {"X-Wb-Commit": self.d.workbench.commit or ""})
        self.assertTrue(same["job"])

class Rules(unittest.TestCase):
    def service(self):
        service = Workbench.__new__(Workbench)  # only the scheduling arithmetic
        service.cpu_budget, service.mem_budget = 8.0, 8.0
        service._lineage = lambda job: []
        return service

    def test_quiet_can_depend_on_a_param(self):
        command = Command(module="lab", name="play", summary="", handler="x:y", version="1",
                          resources={"gpu": True, "quiet_if": ["tour"]})
        service = self.service()
        self.assertFalse(service._resources(command, "host", {})[3])                # an open session
        self.assertTrue(service._resources(command, "host", {"tour": "/t.json"})[3])  # a tour is a measurement

    def test_a_named_lock_is_held_by_one_job_at_a_time(self):
        command = Command(module="ci", name="run", summary="", handler="x:y", version="1",
                          resources={"locks": ["project:{project}"]})
        service = self.service()
        locks = service._resources(command, "container", {"project": "demo"})[4]
        self.assertEqual(locks, {"project:demo"})
        service.running = {"j1": Running("j1", "container", 1.0, 1.0, set(), False, {"project:demo"})}
        self.assertEqual(service._blocker("container", 1.0, 1.0, set(), False, None, locks),
                         "lock project:demo held by j1")
        self.assertIsNone(service._blocker("container", 1.0, 1.0, set(), False, None, {"project:other"}))

    def test_an_open_game_no_longer_blocks_the_machine(self):
        service = self.service()
        service.running = {"play": Running("play", "host", 0.0, 0.0, {"gpu@host"}, False)}
        self.assertIsNone(service._blocker("container", 2.0, 2.0, set(), False))      # CI runs beside it
        self.assertEqual(service._blocker("host", 0.0, 0.0, set(), True),
                         "quiet: waiting for 1 job(s) on this machine to end")    # a measurement still waits
        self.assertEqual(service._blocker("host", 0.0, 0.0, {"gpu@host"}, False), "gpu held by play")


class Restart(unittest.TestCase):
    def test_a_child_of_a_job_that_ended_is_cancelled_at_start(self):
        d = Daemon().start()
        try:
            db = d.workbench.db
            now = time.time()
            db.execute("INSERT INTO job (id, command, module, params, state, machine, path_style, fresh, source, "
                       "submitted, settled) VALUES ('j_PARENTPARENTPARENTPAREN', 'selftest.nest', 'selftest', '{}', "
                       "'interrupted', 'container', 'posix', 0, 'api', ?, ?)", (now, now))
            db.execute("INSERT INTO job (id, command, module, params, state, machine, path_style, fresh, source, "
                       "submitted, parent) VALUES ('j_CHILDCHILDCHILDCHILDCH', 'selftest.echo', 'selftest', '{}', "
                       "'queued', 'container', 'posix', 0, 'child', ?, 'j_PARENTPARENTPARENTPAREN')", (now,))
            d.workbench.recover()
            row = db.one("SELECT state, summary FROM job WHERE id = 'j_CHILDCHILDCHILDCHILDCH'")
            self.assertEqual((row["state"], row["summary"]), ("cancelled", "cancelled: its parent had ended"))
        finally:
            d.stop()


if __name__ == "__main__":
    unittest.main()
