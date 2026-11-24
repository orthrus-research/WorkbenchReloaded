"""The host runner: a container-side daemon sends a host routine to wb-host, which takes the GPU lease (the
same lock gpu_queue.py uses), runs the handler natively with the turn token, and streams events back."""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import Daemon  # noqa: E402
import fake_riven  # noqa: E402

from wb_core.hostclient import HostClient  # noqa: E402
from wb_host import lease  # noqa: E402
from wb_host.runner import HostServer, Runner  # noqa: E402


class ScriptEnvironments(unittest.TestCase):
    """lab.script runs a folder's Python scripts in the environment its requirements make."""

    def test_a_folder_with_requirements_gets_its_own_environment_once(self):
        import subprocess
        from harness import REPO
        sys.path.insert(0, str(REPO / "modules" / "lab"))
        from wb_lab import commands
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "tools").mkdir()
            (root / "other").mkdir()
            (root / "tools" / "requirements.txt").write_text("# nothing to install\n", encoding="utf-8")
            built = []

            class Ctx:
                config = {"environments": {str(root / "tools"): ["requirements.txt"]},
                          "python_cache": str(root / "cache")}

                @staticmethod
                def progress(*args, **kwargs):
                    built.append(kwargs.get("msg"))

                @staticmethod
                def run(args, timeout=None, **kwargs):
                    return subprocess.run(args, timeout=timeout, text=True, stdout=subprocess.PIPE,
                                          stderr=subprocess.STDOUT)
            python = commands._script_python(Ctx(), root / "tools" / "probe.py")
            self.assertTrue(Path(python).is_file())
            self.assertTrue(Path(python).is_relative_to(root / "cache"))
            self.assertEqual(commands._script_python(Ctx(), root / "tools" / "sub" / "x.py"), python)
            self.assertEqual(len(built), 1)  # made once, then reused
            self.assertEqual(commands._script_python(Ctx(), root / "other" / "x.py"), commands._python(Ctx()))

    def test_the_manifest_allows_the_longest_script_timeout(self):
        from harness import REPO
        from wb_core import types
        from wb_core.manifest import parse
        lab = parse(REPO / "modules" / "lab" / "wb_lab" / "module.toml", "wb_lab")
        script = next(c for c in lab.commands if c.name == "script")
        spec = script.params["timeout"]
        self.assertEqual(types.coerce("timeout", spec, spec["default"]), 4 * 3600)
        self.assertGreaterEqual(script.timeout, types.parse_duration(spec["max"]))

    def test_the_manifest_allows_the_longest_open_session(self):
        from harness import REPO
        from wb_core import types
        from wb_core.manifest import parse
        from wb_lab import commands
        lab = parse(REPO / "modules" / "lab" / "wb_lab" / "module.toml", "wb_lab")
        play = next(c for c in lab.commands if c.name == "play")
        spec = play.params["limit"]
        self.assertGreaterEqual(play.timeout, types.parse_duration(spec["max"]))
        self.assertGreaterEqual(play.timeout, commands.TOUR_LIMIT)
        self.assertTrue(play.params["tour"].get("optional"))


class HostRunner(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lab = Path(tempfile.mkdtemp(prefix="wb-lab-"))
        cls.lock = cls.lab / "lease" / "gpu.lock"
        cls.riven = cls.lab.parent / (cls.lab.name + "-riven")  # a fake Riven checkout beside the lab home
        (cls.riven / "tools").mkdir(parents=True)
        (cls.riven / "tools" / "runtime.py").write_text("# fake\n", encoding="utf-8")
        (cls.riven / ".local" / "toolchains").mkdir(parents=True)
        (cls.riven / ".local" / "settings.json").write_text('{"java26": "x"}', encoding="utf-8")
        for item in ("frozen", "builds"):
            (cls.riven / ".local" / item).mkdir(parents=True)
            (cls.riven / ".local" / item / "marker.txt").write_text(item, encoding="utf-8")
        old = time.time() - 40 * 86400
        for name, age in (("smoke-old", old), ("keep-me", old), ("fresh", time.time())):
            folder = cls.riven / ".local" / "cubic-standalone" / name
            folder.mkdir(parents=True)
            (folder / "log.txt").write_text(name, encoding="utf-8")
            os.utime(folder / "log.txt", (age, age))
            os.utime(folder, (age, age))
        config = f"""
            [runner]
            lab_home = '{cls.lab}'
            lease_file = '{cls.lock}'
            coordination_dir = '{cls.lab / "coordination"}'
            token_env = ["LEGACY_TURN_TOKEN"]
            [modules.lab]
            scripts = ['{cls.lab / "scripts"}']
            launcher = '{cls.riven}'
            keep_instances = ["keep-*"]
            """
        files = {"modules/lab/profile.toml": fake_riven.PROFILE_TOML}
        probe = Daemon(config, files=files)  # only to resolve settings and modules the same way
        cls.runner = Runner(probe.settings, probe.modules)
        probe.hub.close()
        cls.token = "host-token-for-tests"
        cls.server = HostServer(cls.runner, cls.token, 0)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.d = Daemon(config, files={"core/host.toml": "enabled = true\n", **files},
                       host_client=HostClient(url, cls.token)).start()
        cls.c = cls.d.client

    @classmethod
    def tearDownClass(cls):
        cls.d.stop()
        cls.server.shutdown()

    def test_host_routine_runs_under_the_lease(self):
        result = self.c.submit("selftest.host-echo", {"text": "hi"}).result(timeout=120)
        self.assertEqual(result.outcome, "ok", result.summary)
        self.assertEqual(result.data.machine, "host")
        self.assertTrue(result.data.lease_token)
        self.assertNotEqual(result.data.pid, os.getpid())
        lane = json.loads((self.lab / "coordination" / "workbench.json").read_text(encoding="utf-8"))
        self.assertEqual(lane["state"], "idle")
        self.assertFalse(lease.occupied(self.lock))
        self.assertIn("releasedUtc", lease.owner(self.lock))

    def test_waits_while_another_lane_holds_the_lease(self):
        other = lease.Lease(self.lock, {"pid": os.getpid(), "name": "someone", "lane": "quadrature"})
        job = self.c.submit("selftest.host-echo", {"text": "wait"})
        time.sleep(3)
        self.assertNotEqual(job.refresh().state, "settled")
        self.assertTrue(lease.occupied(self.lock))
        other.close()
        result = job.result(timeout=120)  # after the 15 s cooldown it takes its turn
        self.assertEqual(result.outcome, "ok", result.summary)

    def test_auth_and_host_header(self):
        import urllib.error
        import urllib.request
        url = f"http://127.0.0.1:{self.server.server_address[1]}/v1/health"
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(url, timeout=5)
        self.assertEqual(raised.exception.code, 401)
        request = urllib.request.Request(url, headers={"Authorization": "Bearer " + self.token,
                                                       "Host": "host.docker.internal:" + url.split(":")[2].split("/")[0]})
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertTrue(json.loads(response.read())["ok"])

    def test_turn_runs_allow_listed_scripts_under_one_lease(self):
        scripts = self.lab / "scripts"
        scripts.mkdir(exist_ok=True)
        (scripts / "ok.py").write_text(  # the token in WB_TURN_TOKEN and in the configured alias
            "import os, sys; t = os.environ.get('WB_TURN_TOKEN'); "
            "sys.exit(0 if t and os.environ.get('LEGACY_TURN_TOKEN') == t else 5)\n", encoding="utf-8")
        (scripts / "fail.py").write_text("raise SystemExit(4)\n", encoding="utf-8")
        steps = [{"id": "one", "command": "lab.script", "params": {"script": str(scripts / "ok.py")}},
                 {"id": "two", "command": "lab.script", "params": {"script": str(scripts / "fail.py")}},
                 {"id": "three", "command": "lab.script", "params": {"script": str(scripts / "ok.py")}}]
        result = self.c.submit("lab.turn", {"steps": steps}).result(timeout=180)
        self.assertEqual(result.outcome, "finding", result.summary)
        self.assertEqual([s["id"] for s in result.data.steps], ["one", "two"])
        outside = self.c.submit("lab.script", {"script": str(Path(__file__))}).result(timeout=120)
        self.assertEqual(outside.outcome, "usage")

    def test_a_silent_script_is_stopped_at_its_timeout(self):
        scripts = self.lab / "scripts"
        scripts.mkdir(exist_ok=True)
        (scripts / "silent.py").write_text("import time\ntime.sleep(120)\n", encoding="utf-8")
        started = time.monotonic()
        result = self.c.submit("lab.script", {"script": str(scripts / "silent.py"), "timeout": "2s"}).result(timeout=120)
        self.assertLess(time.monotonic() - started, 60)  # it printed nothing, and was stopped all the same
        self.assertEqual(result.outcome, "finding", result.summary)
        self.assertEqual(result.summary, "silent.py did not finish within 2 s; stopped")
        self.assertIsNone(result.data.exit)

    def test_migrate_links_back_and_undoes(self):
        from wb import platform
        plan = self.c.submit("lab.migrate", {}).result(timeout=120)
        self.assertEqual(plan.outcome, "ok", plan.summary)
        self.assertEqual({m["item"] for m in plan.data.moves}, {"frozen", "builds", "cubic-standalone", "settings.json"})
        self.assertTrue((self.riven / ".local" / "toolchains").is_dir())  # toolchains never move
        done = self.c.submit("lab.migrate", {"execute": True, "items": ["frozen", "builds", "settings.json"]})
        done = done.result(timeout=120)
        self.assertEqual(done.outcome, "ok", done.summary)
        self.assertTrue((self.lab / "frozen" / "marker.txt").exists())
        self.assertTrue(platform.is_link(self.riven / ".local" / "frozen"))
        self.assertEqual((self.riven / ".local" / "frozen" / "marker.txt").read_text(encoding="utf-8"), "frozen")
        self.assertTrue(os.path.samefile(self.riven / ".local" / "settings.json", self.lab / "settings.json"))
        undone = self.c.submit("lab.migrate", {"undo": True, "execute": True}).result(timeout=120)
        self.assertEqual(undone.outcome, "ok", undone.summary)
        self.assertFalse(platform.is_link(self.riven / ".local" / "frozen"))
        self.assertTrue((self.riven / ".local" / "frozen" / "marker.txt").exists())
        self.assertFalse((self.lab / "frozen").exists())
        self.assertFalse((self.lab / "settings.json").exists())
        self.assertTrue((self.riven / ".local" / "settings.json").is_file())

    def test_prune_instances_dry_run_then_execute(self):
        dry = self.c.submit("lab.prune-instances", {"days": 30}).result(timeout=120)
        self.assertEqual(dry.outcome, "ok", dry.summary)
        self.assertEqual([c["instance"] for c in dry.data.candidates], ["smoke-old"])
        self.assertTrue((self.riven / ".local" / "cubic-standalone" / "smoke-old").exists())
        drifted = self.c.submit("lab.prune-instances", {"days": 30, "execute": True, "expect": 5}).result(timeout=120)
        self.assertEqual(drifted.outcome, "usage")  # the approved count no longer matches: nothing removed
        self.assertTrue((self.riven / ".local" / "cubic-standalone" / "smoke-old").exists())
        gone = self.c.submit("lab.prune-instances", {"days": 30, "execute": True, "names": ["smoke-old", "fresh"]})
        self.assertEqual(gone.result(timeout=120).outcome, "usage")  # "fresh" is no longer a candidate
        done = self.c.submit("lab.prune-instances", {"days": 30, "execute": True, "expect": 1}).result(timeout=120)
        self.assertTrue(done.data.removed)
        self.assertFalse((self.riven / ".local" / "cubic-standalone" / "smoke-old").exists())
        self.assertTrue((self.riven / ".local" / "cubic-standalone" / "keep-me").exists())
        self.assertTrue((self.riven / ".local" / "cubic-standalone" / "fresh").exists())

    def test_lab_queue_reports_lanes(self):
        result = self.c.submit("lab.queue", {}).result(timeout=120)
        self.assertEqual(result.outcome, "ok", result.summary)
        self.assertIn("lease", result.data)


REPO = Path(__file__).resolve().parents[1]
SHELL = r"""
import ctypes, os, subprocess, sys
from ctypes import wintypes
kernel = ctypes.WinDLL("kernel32", use_last_error=True)
kernel.CreateJobObjectW.restype = wintypes.HANDLE
kernel.GetCurrentProcess.restype = wintypes.HANDLE
class BASIC(ctypes.Structure):
    _fields_ = [("a", ctypes.c_int64), ("b", ctypes.c_int64), ("LimitFlags", wintypes.DWORD),
                ("c", ctypes.c_size_t), ("d", ctypes.c_size_t), ("e", wintypes.DWORD), ("f", ctypes.c_size_t),
                ("g", wintypes.DWORD), ("h", wintypes.DWORD)]
class EXTENDED(ctypes.Structure):
    _fields_ = [("basic", BASIC), ("io", ctypes.c_uint64 * 6), ("i", ctypes.c_size_t), ("j", ctypes.c_size_t),
                ("k", ctypes.c_size_t), ("l", ctypes.c_size_t)]
job = kernel.CreateJobObjectW(None, None)
info = EXTENDED()
info.basic.LimitFlags = 0x2000 | 0x800  # kill on close, breakaway allowed: a terminal's job
kernel.SetInformationJobObject(wintypes.HANDLE(job), 9, ctypes.byref(info), ctypes.sizeof(info))
kernel.AssignProcessToJobObject(wintypes.HANDLE(job), wintypes.HANDLE(kernel.GetCurrentProcess()))
# a plain child of the shell, which the job's end must kill, and the detached turn, which must outlive it
plain = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30); open(sys.argv[1], 'w')", sys.argv[2]])
done = subprocess.run([sys.executable, "-m", "wb", "gpu", "run", "--detach", "--lane", "detached", "--", sys.executable,
                       "-c", "import pathlib, sys, time; time.sleep(4); pathlib.Path(sys.argv[1]).write_text('ran')",
                       sys.argv[1]], capture_output=True, text=True)
print(done.stdout, done.stderr, flush=True)
"""


class Lanes(unittest.TestCase):
    """wb gpu's parity list: waiters served in waiting_since order, a restarted waiter keeping its place, a detached
    turn outliving its shell, a turn limit, stuck lanes and leave."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="wb-lanes-"))
        self.lock, self.lanes = self.root / "gpu.lock", self.root / "coordination"
        self.order = self.root / "order.txt"
        saved = lease.COOLDOWN
        lease.COOLDOWN = 0.3
        self.addCleanup(setattr, lease, "COOLDOWN", saved)

    def command(self, label: str) -> list[str]:
        return [sys.executable, "-c", "import sys; open(sys.argv[1], 'a').write(sys.argv[2] + '\\n')",
                str(self.order), label]

    def take_turn(self, lane: str) -> threading.Thread:
        thread = threading.Thread(target=lease.turn, args=(self.lock, self.lanes, lane, self.command(lane)),
                                  kwargs={"poll": 0.1, "log": lambda line: None}, daemon=True)
        thread.start()
        return thread

    def play(self, lane: str) -> threading.Thread:
        """A session's taker, as wb-host and wb play take the lease."""
        def take():
            held, mine, _ = lease.acquire(self.lock, self.lanes, note="play", job="j", cancelled=threading.Event(),
                                          lane_name=lane, poll=0.1)
            with open(self.order, "a") as stream:
                stream.write(lane + "\n")
            lease.release(held, mine, self.lanes)
        thread = threading.Thread(target=take, daemon=True)
        thread.start()
        return thread

    def held(self):
        holder = lease.Lease(self.lock, {"pid": os.getpid(), "lane": "holder", "startedUtc": lease.stamp()})
        return holder

    def release(self, holder):
        holder.close()
        lease.write_entry(self.lanes / "last-release.json", {"utc": lease.stamp(), "lane": "holder"})

    def wait_for(self, predicate, timeout=30):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(predicate())

    def served(self) -> list[str]:
        return self.order.read_text().split() if self.order.exists() else []

    def test_two_run_waiters_and_a_play_are_served_in_waiting_since_order(self):
        holder = self.held()
        threads = []
        for taker in ("run-1", "play-1", "run-2"):
            threads.append(self.play(taker) if taker.startswith("play") else self.take_turn(taker))
            self.wait_for(lambda: (self.lanes / f"{taker}.json").is_file())
            time.sleep(0.05)
        report = lease.status(self.lock, self.lanes)
        self.assertEqual(report["waiters"], ["run-1", "play-1", "run-2"])
        self.assertEqual(report["lease"]["holder"]["lane"], "holder")
        self.release(holder)
        for thread in threads:
            thread.join(60)
        self.assertEqual(self.served(), ["run-1", "play-1", "run-2"])
        self.assertEqual(lease.status(self.lock, self.lanes)["waiters"], [])

    def test_a_restarted_waiter_keeps_its_place(self):
        gone = subprocess.Popen([sys.executable, "-c", "pass"])
        gone.wait()
        early = lease.stamp(time.time() - 120)
        lease.write_entry(self.lanes / "first.json", {"state": "waiting", "waiting_since": early, "pid": gone.pid,
                                                      "updatedUtc": lease.stamp()})
        self.assertEqual(lease.status(self.lock, self.lanes)["waiters"], [])  # its process is gone: not live
        holder = self.held()
        later = self.take_turn("later")
        self.wait_for(lambda: (self.lanes / "later.json").is_file())
        first = self.take_turn("first")  # the first request, started again
        self.wait_for(lambda: lease.read_entry(self.lanes / "first.json").get("pid") == os.getpid())
        self.assertEqual(lease.read_entry(self.lanes / "first.json")["waiting_since"], early)
        self.assertEqual(lease.status(self.lock, self.lanes)["waiters"], ["first", "later"])
        self.release(holder)
        first.join(60)
        later.join(60)
        self.assertEqual(self.served(), ["first", "later"])

    def test_a_turn_past_its_limit_is_stopped_and_the_lease_released(self):
        started = time.monotonic()
        code = lease.turn(self.lock, self.lanes, "slow", [sys.executable, "-c", "import time; time.sleep(60)"],
                          limit=1, poll=0.1, log=lambda line: None)
        self.assertNotEqual(code, 0)
        self.assertLess(time.monotonic() - started, 30)
        self.assertFalse(lease.occupied(self.lock))
        self.assertEqual(lease.read_entry(self.lanes / "slow.json")["state"], "idle")

    def test_stuck_names_a_running_lane_whose_holder_is_gone_and_leave_clears_it(self):
        gone = subprocess.Popen([sys.executable, "-c", "pass"])
        gone.wait()
        lease.write_entry(self.lanes / "ghost.json", {"state": "running", "pid": gone.pid, "updatedUtc": lease.stamp(),
                                                      "note": "a crashed turn"})
        issues = lease.stuck(self.lock, self.lanes)
        self.assertEqual([(i["lane"], i["fix"]) for i in issues], [("ghost", "wb gpu leave ghost")])
        lease.leave(self.lanes, "ghost")
        self.assertEqual(lease.stuck(self.lock, self.lanes), [])
        self.assertEqual(lease.read_entry(self.lanes / "ghost.json")["state"], "idle")

    @unittest.skipUnless(os.name == "nt", "a terminal's Job Object is Windows'")
    def test_a_detached_turn_survives_its_shell(self):
        config = self.root / "config"
        config.mkdir()
        (config / "workbench.toml").write_text(
            f"[runner]\nlab_home = '{self.root.as_posix()}'\nlease_file = '{self.lock.as_posix()}'\n"
            f"coordination_dir = '{self.lanes.as_posix()}'\n", encoding="utf-8")
        marker, plain = self.root / "detached.txt", self.root / "plain.txt"
        env = dict(os.environ, WB_CONFIG=str(config),
                   PYTHONPATH=os.pathsep.join(str(REPO / p) for p in ("sdk/python", "core", "host")))
        shell = subprocess.run([sys.executable, "-c", SHELL, str(marker), str(plain)], env=env, capture_output=True,
                               text=True, timeout=60)
        self.assertIn('"left_job": true', shell.stdout, shell.stdout + shell.stderr)
        # the shell has exited and its job has killed what it started; the detached turn goes on
        self.wait_for(lambda: marker.is_file(), timeout=60)
        self.assertEqual(marker.read_text(), "ran")
        time.sleep(1)
        self.assertFalse(plain.exists())
        self.wait_for(lambda: lease.read_entry(self.lanes / "detached.json").get("state") == "idle")
        self.assertTrue(any((self.lanes / "logs").glob("detached-*.log")))


if __name__ == "__main__":
    unittest.main()
