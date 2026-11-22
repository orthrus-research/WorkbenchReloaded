"""The host runner: a container-side daemon sends a host routine to wb-host, which takes the GPU lease (the
same lock gpu_queue.py uses), runs the handler natively with the turn token, and streams events back."""

import json
import os
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


if __name__ == "__main__":
    unittest.main()
