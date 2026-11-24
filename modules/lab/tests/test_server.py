"""lab.server against a fake dedicated server written here: a small Python program that
prints the vanilla ready line, answers console commands on stdin, writes files and stops on "stop". Its
behaviour comes from fake.json in its folder. The default launch (java -jar <jar> nogui) is run against a tiny
server JAR compiled here when this machine has a JDK."""

import json
import os
import re
import subprocess
import sys
import time
import unittest
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
for extra in (REPO / "tests", REPO / "modules" / "jar" / "tests", REPO / "modules" / "lab"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import jvmkit  # noqa: E402
from harness import Daemon  # noqa: E402

from wb_core.module import sha256_tree  # noqa: E402
from wb_lab import server as sessions  # noqa: E402

JDKS = jvmkit.find_jdks()
jvmkit.export(JDKS)

FAKE = r'''
import json, os, sys, time
from pathlib import Path

here = Path.cwd()
spec = json.loads((here / "fake.json").read_text()) if (here / "fake.json").exists() else {}
props = {}
if (here / "server.properties").exists():
    for line in (here / "server.properties").read_text(encoding="latin-1").splitlines():
        if "=" in line and not line.startswith("#"):
            key, _, value = line.partition("=")
            props[key.strip()] = value.strip()
began = time.monotonic()

def ticks():
    return int((time.monotonic() - began) * spec.get("tps", 20))

def say(text):
    print(f"[12:00:00] [Server thread/INFO]: {text}", flush=True)

say("Starting minecraft server version 1.12.2")
say(f"pid {os.getpid()}")
say("args " + " ".join(sys.argv[1:]))
say(f"Starting Minecraft server on *:{props.get('server-port', '25565')}")
if spec.get("exit_before_ready"):
    say("Encountered an unexpected exception")
    sys.exit(spec["exit_before_ready"])
time.sleep(spec.get("ready_delay", 0.1))
if not spec.get("never_ready"):
    say('Done (0.42s)! For help, type "help" or "?"')

def note(text):
    (here / "world").mkdir(exist_ok=True)
    with open(here / "world" / "level.dat", "a") as handle:
        handle.write(text + "\n")

for raw in sys.stdin:
    command = raw.strip()
    if command == "time query gametime":
        say(f"Time is {ticks()}")
    elif command.startswith("say "):
        say(f"[Server] {command[4:]}")
    elif command == "list":
        say("There are 0/20 players online:")
    elif command == "save-all":
        say("Saving...")
        note("saved")
        say("Saved the world")
    elif command == "crash":
        (here / "crash-reports").mkdir(exist_ok=True)
        (here / "crash-reports" / "crash-fake-server.txt").write_text("---- Minecraft Crash Report ----\n")
        say("This crash report has been saved to: crash-reports/crash-fake-server.txt")
        sys.exit(1)
    elif command == "stop":
        say("Stopping server")
        if spec.get("ignore_stop"):
            time.sleep(600)
        note("stopped")
        (here / "written.txt").write_text(f"ticks={ticks()}\n")
        probe = spec.get("probe")
        if probe:
            target = here / probe["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            config = here / "config" / "probe.cfg"
            mods = sorted(os.listdir(here / "mods")) if (here / "mods").is_dir() else []
            target.write_text(probe["text"] if "text" in probe else json.dumps(
                {"ticks": ticks(), "error": probe.get("error"), "mods": mods,
                 "config": config.read_text().strip() if config.is_file() else None}))
        sys.exit(spec.get("stop_exit", 0))
    else:
        say("Unknown command. Try /help for a list of commands")
'''

JAVA_SERVER = {
    "FakeServer.java": """
        import java.io.BufferedReader;
        import java.io.InputStreamReader;

        public class FakeServer {
            public static void main(String[] args) throws Exception {
                long start = System.nanoTime();
                System.out.println("[Server thread/INFO]: Starting minecraft server version 1.12.2");
                System.out.println("[Server thread/INFO]: port=" + System.getProperty("probe.port")
                                   + " args=" + String.join(" ", args));
                System.out.println("[Server thread/INFO]: Done (0.10s)! For help, type \\"help\\" or \\"?\\"");
                BufferedReader in = new BufferedReader(new InputStreamReader(System.in, "UTF-8"));
                for (String line; (line = in.readLine()) != null; ) {
                    line = line.trim();
                    if (line.equals("time query gametime")) {
                        System.out.println("[Server thread/INFO]: Time is " + (System.nanoTime() - start) / 50000000L);
                    } else if (line.equals("stop")) {
                        System.out.println("[Server thread/INFO]: Stopping server");
                        System.exit(0);
                    }
                }
            }
        }
    """,
}

LAUNCH = [sys.executable, "-u", "fake_server.py", "{jvm_args}"]


def alive(pid: int) -> bool:
    if os.name == "nt":
        listed = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"], capture_output=True,
                                text=True).stdout
        return f'"{pid}"' in listed
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:  # exited but not yet reaped counts as gone
        return Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()[0] != "Z"
    except OSError:
        return True


class Sessions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = Daemon().start()
        cls.c = cls.d.client
        cls.made = 0

    @classmethod
    def tearDownClass(cls):
        cls.d.stop()

    def server(self, spec: dict | None = None, files: dict | None = None) -> Path:
        """A stopped server folder: the fake, its behaviour, a world, an old crash report, a fixed port."""
        Sessions.made += 1
        folder = self.d.data / f"server-{Sessions.made}"
        contents = {"fake_server.py": FAKE, "fake.json": json.dumps(spec or {}), "eula.txt": "eula=true\n",
                    "server.properties": "#Minecraft server properties\nmotd=A Minecraft Server\nserver-port=25565\n",
                    "world/level.dat": "generated\n", "crash-reports/crash-old-server.txt": "an old crash\n",
                    **(files or {})}
        for name, text in contents.items():
            (folder / name).parent.mkdir(parents=True, exist_ok=True)
            (folder / name).write_text(text, encoding="utf-8")
        return folder

    def run_server(self, folder: Path, command: str = "lab.server", **params):
        request = {"server": str(folder), "jdk": "", "launch": LAUNCH, **params}
        return self.c.submit(command, request).result(timeout=180)

    @staticmethod
    def receipt(result) -> dict:
        return json.loads(Path(result.data.receipt).read_text(encoding="utf-8"))

    def test_a_session_runs_ticks_saves_stops_and_keeps_what_it_wrote(self):
        folder = self.server({"tps": 20})
        result = self.run_server(folder, commands=["say hello", {"send": "list", "expect": "players online"}],
                                 ticks=20, save=True, keep=["world", "written.txt"],
                                 jvm_args=["-Dprobe.port={port}", "-Dprobe.ticks={ticks}"])
        self.assertEqual(result.outcome, "ok", result.summary)
        self.assertTrue(result.summary.startswith("server ready in"), result.summary)
        data = result.data
        self.assertEqual((data.ready, data.ended, data.stopped, data.exit), (True, "ticks", "stop", 0))
        self.assertGreaterEqual(data.ticks, 20)
        self.assertEqual(data.commands[1]["line"].split(": ", 1)[1], "There are 0/20 players online:")
        self.assertEqual(data.crash_reports, [])  # the old report came with the folder
        receipt = self.receipt(result)
        out = Path(data.receipt).parent
        self.assertEqual(receipt["schema"], "wb.lab.server/1")
        server_input = next(i for i in result["inputs"] if i["name"] == "server")
        self.assertEqual(receipt["server"]["sha256"], server_input["sha256"])  # the copy is what the job hashed
        self.assertEqual(receipt["server"]["sha256"], sha256_tree(folder)[0])
        self.assertIn("Done (0.42s)!", receipt["ready"]["matched"][0]["line"])
        self.assertEqual(receipt["stop"]["commands"], ["save-all", "stop"])
        self.assertIn("Saved the world", receipt["stop"]["saved"]["line"])
        self.assertGreaterEqual(receipt["run"]["game_time"]["checks"], 2)
        port = receipt["properties"]["set"]["server-port"]
        self.assertNotEqual(port, "25565")  # a free port, so sessions never collide
        self.assertEqual(receipt["launch"][:3], [sys.executable, "-u", "fake_server.py"])
        self.assertEqual(receipt["launch"][3:], [f"-Dprobe.port={port}", "-Dprobe.ticks=20"])
        for key in ("launched", "ready", "stop", "exited"):
            self.assertTrue(receipt["times"][key], key)
        console = (out / "console.log").read_text(encoding="utf-8")
        self.assertIn(f"Starting Minecraft server on *:{port}", console)
        self.assertIn("Stopping server", console)
        self.assertEqual((out / "files" / "world" / "level.dat").read_text(encoding="utf-8").split(),
                         ["generated", "saved", "stopped"])
        self.assertTrue((out / "files" / "written.txt").is_file())
        inventory = json.loads((out / "inventory.json").read_text(encoding="utf-8"))
        self.assertEqual(inventory["server"]["fake_server.py"]["sha256"], sha256_tree(folder / "fake_server.py")[0])
        self.assertEqual({o["name"] for o in result["outputs"]}, {"console", "inventory", "receipt", "files"})
        self.assertIn("ready_seconds", {m["name"] for m in result["measurements"]})
        # the source is never written, and the copy is gone once what was kept is out
        self.assertIn("server-port=25565", (folder / "server.properties").read_text(encoding="utf-8"))
        self.assertEqual((folder / "world" / "level.dat").read_text(encoding="utf-8"), "generated\n")
        self.assertFalse((folder / "written.txt").exists())
        self.assertFalse((self.d.state / "jobs" / result["job"] / "work" / "server").exists())

    def test_a_run_ends_at_a_line_or_after_seconds(self):
        until = self.run_server(self.server(), commands=["say probe finished"], until=["probe finished"],
                                exclude=["crash-reports"])
        self.assertEqual(until.outcome, "ok", until.summary)
        self.assertEqual(until.data.ended, "until")
        self.assertIn("crash-reports/", self.receipt(until)["server"]["left_out"])
        timed = self.run_server(self.server(), seconds="1s")
        self.assertEqual(timed.outcome, "ok", timed.summary)
        self.assertEqual(timed.data.ended, "seconds")
        self.assertGreaterEqual(self.receipt(timed)["run"]["seconds_run"], 1.0)
        plain = self.run_server(self.server(), commands=["stop"])  # it may also be stopped by a command
        self.assertEqual(plain.outcome, "ok", plain.summary)
        self.assertEqual((plain.data.ended, plain.data.exit), ("commands", 0))

    def test_a_server_that_exits_early_or_never_gets_ready(self):
        early = self.run_server(self.server({"exit_before_ready": 3}))
        self.assertEqual(early.outcome, "finding")
        self.assertEqual(len(early["findings"]), 1, early["findings"])
        self.assertIn("the server exited 3 before it was ready", early.summary)
        self.assertIn("Encountered an unexpected exception", early.summary)
        self.assertEqual((early.data.ready, early.data.stopped, early.data.exit), (False, "exited", 3))
        never = self.run_server(self.server({"never_ready": True}), ready_timeout="2s")
        self.assertEqual(never.outcome, "finding")
        self.assertIn("not ready within 2 s", never.summary)
        self.assertEqual(never.data.stopped, "stop")  # it is still stopped gracefully

    def test_a_server_that_ignores_stop_is_killed_after_the_grace(self):
        started = time.monotonic()
        result = self.run_server(self.server({"ignore_stop": True}), grace="2s")
        self.assertLess(time.monotonic() - started, 60)
        self.assertEqual(result.outcome, "finding")
        self.assertIn("did not stop within 2 s of stop; killed", result.summary)
        self.assertEqual(result.data.stopped, "killed")
        self.assertEqual(self.receipt(result)["stop"]["how"], "killed")

    def started(self, seconds: str = "120s"):
        """A long session, once its server is ready: (job, out folder, the server's pid)."""
        job = self.c.submit("lab.server", {"server": str(self.server()), "jdk": "", "launch": LAUNCH,
                                           "seconds": seconds})
        out = self.d.state / "jobs" / job.id / "out"
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            log = out / "console.log"
            text = log.read_text(encoding="utf-8") if log.is_file() else ""
            if "Done (" in text:
                return job, out, int(re.search(r"pid (\d+)", text).group(1))
            time.sleep(0.2)
        self.fail("the session never got ready")

    def test_a_cancelled_session_still_stops_its_server_and_keeps_the_receipt(self):
        job, out, pid = self.started()
        job.cancel()
        result = job.wait(timeout=90)
        self.assertEqual(result.state, "cancelled")
        receipt = json.loads((out / "receipt.json").read_text(encoding="utf-8"))
        self.assertTrue(receipt["cancelled"])
        self.assertEqual(receipt["stop"]["how"], "stop")  # stopped as an operator would, even when cancelled
        self.assertIn("Stopping server", (out / "console.log").read_text(encoding="utf-8"))
        self.assertFalse(alive(pid))

    @unittest.skipUnless(sys.platform.startswith("linux"), "the parent-death signal is Linux's")
    def test_a_worker_killed_outright_takes_its_server_with_it(self):
        import signal
        job, _out, pid = self.started()
        worker = self.d.workbench.db.one("SELECT worker_pid FROM job WHERE id = ?", (job.id,))["worker_pid"]
        os.kill(worker, signal.SIGKILL)
        deadline = time.monotonic() + 20
        while alive(pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertFalse(alive(pid))
        self.assertNotEqual(job.wait(timeout=60).outcome, "ok")

    def test_missing_replies_crashes_and_unreached_ends_are_findings(self):
        silent = self.run_server(self.server(), commands=[{"send": "say x", "expect": "never printed", "timeout": "1s"}])
        self.assertEqual(silent.outcome, "finding")
        self.assertIn("no reply to 'say x' matched /never printed/ within 1 s", silent.summary)
        crashed = self.run_server(self.server(), commands=["crash"])
        self.assertEqual(crashed.outcome, "finding")
        self.assertIn("the server wrote a crash report: crash-reports/crash-fake-server.txt", crashed["findings"])
        self.assertEqual(crashed.data.crash_reports, ["crash-reports/crash-fake-server.txt"])
        self.assertEqual(crashed.data.exit, 1)
        out = Path(crashed.data.receipt).parent
        self.assertTrue((out / "crash-reports" / "crash-fake-server.txt").is_file())
        self.assertFalse((out / "crash-reports" / "crash-old-server.txt").exists())
        uncounted = self.run_server(self.server(), ticks=10, tick_query="time query nothing", reply_timeout="1s")
        self.assertEqual((uncounted.outcome, uncounted.data.ended), ("finding", "no game time"))
        self.assertIn("so the ticks cannot be counted", uncounted.summary)
        endless = self.run_server(self.server(), until=["never printed"], run_timeout="1s")
        self.assertEqual((endless.outcome, endless.data.ended), ("finding", "timeout"))
        self.assertIn("did not reach a line matching /never printed/ within 1 s", endless.summary)

    def test_bad_requests_are_refused(self):
        folder = self.server()
        for params, word in (({"launch": ["{java}", "-jar", "{jar}"]}, "needs a jdk"),
                             ({"launch": [sys.executable, "{jav}"]}, "unknown placeholder {jav}"),
                             ({"launch": [sys.executable, "fake_server.py"], "jvm_args": ["-Xmx1G"]},
                              "launch has no {jvm_args}"),
                             ({"keep": ["../elsewhere"]}, "relative to the server folder"),
                             ({"commands": [{"sent": "say x"}]}, "unknown key(s) sent"),
                             ({"ready": ["("]}, "not a regular expression"),
                             ({"launch": [sys.executable, "-u", "fake_server.py", "{jar}"]}, "0 JARs at its top level")):
            result = self.run_server(folder, **params)
            self.assertEqual(result.outcome, "usage", params)
            self.assertIn(word, result.summary)
        missing = self.c.submit("lab.server", {"server": str(folder), "jdk": "5"}).result(timeout=120)
        self.assertEqual(missing.outcome, "environment")
        self.assertIn("JDK 5 is not available", missing.summary)

    # -- added files and mods ----------------------------------------------------------------------
    def test_files_and_mods_are_laid_over_the_copy_only(self):
        folder = self.server({"probe": {"path": "probe/result.json"}}, files={"config/probe.cfg": "mode=slow\n"})
        overlay = self.d.data / "overlay"
        (overlay / "config").mkdir(parents=True)
        (overlay / "config" / "probe.cfg").write_text("mode=fast\n", encoding="utf-8")
        mod = self.d.data / "tick-probe.jar"
        with zipfile.ZipFile(mod, "w") as archive:
            archive.writestr("mcmod.info", "[]")
        result = self.run_server(folder, files=str(overlay), mods=[str(mod)], ticks=10, keep=["probe/*.json"])
        self.assertEqual(result.outcome, "ok", result.summary)
        out = Path(result.data.receipt).parent
        written = json.loads((out / "files" / "probe" / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(written["config"], "mode=fast")
        self.assertEqual(written["mods"], ["tick-probe.jar"])
        receipt = self.receipt(result)
        self.assertEqual(receipt["replaced"], ["config/probe.cfg"])
        self.assertEqual(receipt["added"]["mods/tick-probe.jar"]["sha256"], sha256_tree(mod)[0])
        self.assertEqual(receipt["jars"]["mods/tick-probe.jar"], sha256_tree(mod)[0])
        self.assertEqual((folder / "config" / "probe.cfg").read_text(encoding="utf-8"), "mode=slow\n")

    # -- the default launch -------------------------------------------------------------------------
    @unittest.skipUnless(JDKS, "no JDK with javac on this machine")
    def test_the_default_launch_runs_the_server_jar_on_a_jdk(self):
        newest = max(JDKS, key=int)
        classes = jvmkit.compile_java(JDKS[newest], JAVA_SERVER, self.d.root / "java-server")
        folder = self.d.data / "java-server"
        jvmkit.make_jar(folder / "server.jar", folders=[classes], manifest={"Main-Class": "FakeServer"})
        (folder / "server.properties").write_text("server-port=25565\n", encoding="utf-8")
        result = self.c.submit("lab.server", {
            "server": str(folder), "jdk": newest, "jvm_args": ["-Dprobe.port={port}"], "ticks": 10,
            "commands": [{"send": "time query gametime", "expect": "Time is \\d+"}]}).result(timeout=180)
        self.assertEqual(result.outcome, "ok", result.summary)
        receipt = self.receipt(result)
        port = receipt["properties"]["set"]["server-port"]
        self.assertEqual(receipt["launch"], [receipt["jvm"]["java"], f"-Dprobe.port={port}", "-jar", "server.jar",
                                             "nogui"])
        self.assertEqual(receipt["jvm"]["jdk"], newest)
        self.assertTrue(receipt["jvm"]["runtime"])
        self.assertIn("jdk", result["toolchain"])
        console = (Path(result.data.receipt).parent / "console.log").read_text(encoding="utf-8")
        self.assertIn(f"port={port} args=nogui", console)
        self.assertGreaterEqual(result.data.ticks, 10)


class Parts(unittest.TestCase):
    """The session's file handling, without a server."""

    def test_properties_are_set_as_java_reads_them_and_other_lines_stay(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "server.properties"
            path.write_text("#Minecraft server properties\nmotd=A Minecraft Server\nserver-port=25565\n",
                            encoding="latin-1")
            sessions.set_properties(path, {"server-port": "40000", "level-seed": "a=b: c", "motd": " Grüße"})
            self.assertEqual(path.read_text(encoding="latin-1").splitlines(),
                             ["#Minecraft server properties", "motd=\\ Gr\\u00fc\\u00dfe", "server-port=40000",
                              "level-seed=a\\=b\\: c"])

    def test_a_console_keeps_recent_lines_in_memory_and_every_line_in_its_log(self):
        import tempfile
        import threading
        from wb_lab import process
        with tempfile.TemporaryDirectory() as folder:
            child = process.start([sys.executable, "-c", "for i in range(50): print(i)"], cwd=folder,
                                  env=dict(os.environ), stdin=True)
            console = process.Console(child, Path(folder) / "console.log", keep=10)
            while not console.closed:
                time.sleep(0.05)
            lines = list(console.scan(0, time.monotonic() + 5, threading.Event()))
            console.close()
            child.wait()
            self.assertEqual(console.count, 50)
            self.assertLessEqual(len(lines), 20)
            self.assertEqual([line.index for line in lines], list(range(50 - len(lines), 50)))
            self.assertEqual([line.text for line in lines][-1], "49")
            self.assertEqual((Path(folder) / "console.log").read_text(encoding="utf-8").split(),
                             [str(i) for i in range(50)])

    def test_a_copy_hashes_as_the_core_does_and_leaves_out_what_is_excluded(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            source, target = Path(folder) / "source", Path(folder) / "target"
            for name in ("a.jar", "a-b/c.txt", "a/b.txt", "logs/latest.log", "world/region/r.0.0.mca", "Z.txt"):
                (source / name).parent.mkdir(parents=True, exist_ok=True)
                (source / name).write_text(name, encoding="utf-8")

            class Ctx:
                @staticmethod
                def check_cancel():
                    pass
            copied, left_out = sessions.copy_tree(Ctx(), source, target, [])
            self.assertEqual(sessions.tree_digest(target, copied), sha256_tree(source)[0])
            copied, left_out = sessions.copy_tree(Ctx(), source, Path(folder) / "fresh", ["logs", "world/**/*.mca"])
            self.assertEqual(left_out, ["logs/", "world/region/r.0.0.mca"])
            self.assertFalse((Path(folder) / "fresh" / "logs").exists())
            self.assertTrue((Path(folder) / "fresh" / "world" / "region").is_dir())


if __name__ == "__main__":
    unittest.main()
