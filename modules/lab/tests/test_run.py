"""lab.run with a fake game: a Python server that listens on the session's port and a client that joins it and
"captures" a view each time the server teleports it. A tour is played view by view and filed by label; the session's
inputs reach the command lines and session.json and never the template; the receipt is checked before start (a JDK
of another build is refused, the same build at another path accepted); a failure names its decisive line and crash
report; a debug session writes its attach configuration; a kept session is a retention root."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path[:0] = [str(REPO / "core"), str(REPO / "sdk" / "python"), str(REPO / "modules" / "lab")]

from wb_core.module import Context, EnvironmentProblem, ParamError  # noqa: E402
from wb_lab import channel as channels  # noqa: E402
from wb_lab import desktop as desktops  # noqa: E402
from wb_lab import instance as instances  # noqa: E402
from wb_lab import run as runs  # noqa: E402
from wb_lab.store import Store  # noqa: E402

BUILD = "26.0.2.1+1"
FAKE = r'''
import json, os, socket, sys, threading, time
from pathlib import Path
args = [a for a in sys.argv[1:] if not a.startswith("-D")]  # a JVM's options, as the game would take them
side = args[0]
here = Path.cwd()
(here / "argv.json").write_text(json.dumps(sys.argv))
if side == "server":
    values = dict(l.split("=", 1) for l in Path("server.properties").read_text().splitlines() if "=" in l)
    listener = socket.socket()
    listener.bind(("127.0.0.1", int(values["server-port"])))
    listener.listen(1)
    print("[Server thread/INFO]: Done (0.1s)! For help, type help", flush=True)
    joined = []
    def accept():
        connection, _ = listener.accept()
        joined.append(connection)
        print("[Server thread/INFO]: CubicLab joined the game", flush=True)
    threading.Thread(target=accept, daemon=True).start()
    log = open("commands.log", "a")
    for line in sys.stdin:
        line = line.strip()
        log.write(line + "\n")
        log.flush()
        if line == "stop":
            if os.environ.get("FAKE_IGNORE_STOP"):
                continue
            print("[Server thread/INFO]: Stopping the server", flush=True)
            break
        if line.startswith("tp ") and joined:
            joined[0].sendall(b"tp\n")
    sys.exit(0)
if os.environ.get("FAKE_CRASH"):
    folder = here / "crash-reports"
    folder.mkdir(exist_ok=True)
    (folder / "crash-2026-11-24_10.00.00-client.txt").write_text(
        "---- Minecraft Crash Report ----\nDescription: Rendering screen\n\njava.lang.IllegalStateException: no GL\n")
    print('Exception in thread "main" java.lang.IllegalStateException: no GL', flush=True)
    sys.exit(3)
connection = socket.create_connection(("127.0.0.1", int(args[args.index("--port") + 1])))
if os.environ.get("FAKE_FIXTURE"):  # a fixture mod: answers the requests of the session's client channel
    channel = Path(next(a.split("=", 1)[1] for a in sys.argv if a.startswith("-Dfixture.channel=")))
    (channel / "fixture-ready.json").write_text("{}")
    print("fixture ready", flush=True)
    while True:
        inbox = channel / "request.json"
        if inbox.exists():
            query = json.loads(inbox.read_text())
            os.replace(inbox, channel / "responses" / (query["id"] + "-request.json"))
            answer = channel / "responses" / (query["id"] + ".json")
            failed = query["action"] == "fail"
            answer.with_suffix(".tmp").write_text(json.dumps({"ok": not failed, "result": {"echo": query},
                                                              "error": "asked to fail" if failed else None}))
            os.replace(answer.with_suffix(".tmp"), answer)
            if query["action"] == "quit":
                sys.exit(0)
        time.sleep(0.05)
if os.environ.get("FAKE_OPEN_SECONDS"):
    time.sleep(float(os.environ["FAKE_OPEN_SECONDS"]))
    sys.exit(0)
shots = here / "screenshots"
shots.mkdir(exist_ok=True)
count = 0
for line in connection.makefile():
    if line.strip() == "tp":
        count += 1
        (shots / f"2026-11-24_10.00.{count:02d}.png").write_bytes(b"png %d" % count)
        print("[Client thread/INFO]: Game view captured", flush=True)
time.sleep(3600)
'''


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Sessions(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="wb-run-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        os.environ["WB_TURN_TOKEN"] = "a-token"
        self.addCleanup(os.environ.pop, "WB_TURN_TOKEN", None)
        for name in ("FAKE_CRASH", "FAKE_IGNORE_STOP", "FAKE_OPEN_SECONDS", "FAKE_FIXTURE"):
            self.addCleanup(os.environ.pop, name, None)
        self.system = "windows" if os.name == "nt" else "linux"
        self.store = self.root / "store"
        self.cache = self.root / "cache"
        self.jdk = self.store / "jdks" / BUILD / self.system / f"jdk-{BUILD}"
        self.make_jdk(self.jdk, BUILD)
        (self.cache / "libraries").mkdir(parents=True)
        (self.cache / "libraries" / "lib.jar").write_bytes(b"a library")
        self.mod = b"a mod"
        (self.store / "files" / sha256(self.mod)).mkdir(parents=True)
        (self.store / "files" / sha256(self.mod) / "mod-1.jar").write_bytes(self.mod)
        self.launch_doc = {
            "schema": 1, "project": "game", "launch": "default", "commit": "a" * 40, "tree": "b" * 40, "set": "c" * 40,
            "store": str(self.store), "runtime": {"kind": "cleanroom", "version": "1", "java": 26, "jdk": BUILD},
            "members": [{"member": "game:mod@both", "sha256": sha256(self.mod), "file": "mod-1.jar",
                         "path": f"files/{sha256(self.mod)}/mod-1.jar", "side": "both"}],
            "jvm": {"client": ["-Dgame.mode=own"], "server": []}, "collect": [], "failures": [r"Description: "],
            "params": {}, "tour": {"setup": ["time set 6000"], "client_jvm": ["-Dgame.tour=true"],
                                   "captured": "Game view captured", "leave_seconds": 0},
            "drivers": {"probe": {"command": ["{python}", "{root}/driver.py", "{instance_json}", "{session_id}"]}}}
        self.config = {"instances": str(self.root / "instances"), "runtime_cache": str(self.cache),
                       "sessions": str(self.root / "sessions")}
        self.lab = {"trusted": ["game"], "env": {"OLD_TURN": "token"}}
        self.make_instance("game")
        self.tour = self.root / "tour.json"
        self.tour.write_text(json.dumps([{"label": "arrive", "x": 1, "y": 100, "z": 2, "yaw": 0, "pitch": 10,
                                          "commands": ["weather clear"]},
                                         {"label": "look-east", "x": 5, "y": 110, "z": 2, "yaw": 90, "pitch": 0}]),
                             encoding="utf-8")

    def make_jdk(self, home: Path, build: str):
        (home / "bin").mkdir(parents=True)
        (home / "bin" / ("java.exe" if self.system == "windows" else "java")).write_bytes(b"a java")
        (home / "release").write_text(f'JAVA_RUNTIME_VERSION="{build}"\n', encoding="utf-8")

    def make_instance(self, name: str):
        """An instance as lab.build leaves one: a template in the store, hard-linked, with its receipt."""
        staging = self.root / "instances" / ".store" / "staging" / name
        launch = json.dumps(self.launch_doc, indent=1).encode()
        for side in ("server", "client"):
            (staging / side / "mods").mkdir(parents=True)
            (staging / side / "mods" / "mod-1.jar").write_bytes(self.mod)
            (staging / side / "fake.py").write_text(FAKE, encoding="utf-8")
        (staging / "server" / "server.properties").write_text("server-port=25565\nview-distance=12\n", encoding="utf-8")
        (staging / "client" / "options.txt").write_text("renderDistance:12\nmaxFps:120\n", encoding="utf-8")
        (staging / "launch.json").write_bytes(launch)
        java = "{jdk:26}/bin/" + ("java.exe" if self.system == "windows" else "java")
        files = {"{jdk:26}/release": self.jdk / "release", java: self.jdk / "bin" / Path(java).name,
                 "{cache}/libraries/lib.jar": self.cache / "libraries" / "lib.jar"}
        receipt = {
            "schema": instances.SCHEMA, "key": "k" + name, "base": "cubature:standalone", "system": self.system,
            "launch": {"sha256": sha256(launch), "file": "launch.json", "project": "game", "launch": "default",
                       "commit": "a" * 40, "members": {"game:mod@both": sha256(self.mod)}},
            "jdk": {"26": {"build": BUILD}}, "player": {"name": "CubicLab", "uuid": "u"},
            "members": [{"member": "game:mod@both", "sha256": sha256(self.mod),
                         "placed": ["{instance}/server/mods/mod-1.jar", "{instance}/client/mods/mod-1.jar"]}],
            # the fake game is the JVM's first argument, so a session's options go after it (jvm_at)
            "server": {"cwd": "{instance}/server", "command": [sys.executable, "{instance}/server/fake.py", "server"],
                       "jvm_at": 2},
            "client": {"cwd": "{instance}/client",
                       "command": [sys.executable, "{instance}/client/fake.py", "-Dgame.mode=own", "client",
                                   "--width", "1280", "--height", "720", "--port", "{port}"], "jvm_at": 3},
            "files": {key: {"sha256": sha256(path.read_bytes()), "bytes": path.stat().st_size}
                      for key, path in files.items()},
        }
        (staging / instances.RECEIPT).write_text(json.dumps(receipt, indent=1), encoding="utf-8")
        store = Store(self.root / "instances" / ".store", instances.LAYOUT)
        store.adopt_template(staging, "k" + name, {"name": name})
        store.materialize("k" + name, self.root / "instances" / name)
        made = json.loads((self.root / "instances" / name / instances.RECEIPT).read_text(encoding="utf-8"))
        made["name"] = name
        (self.root / "instances" / name / instances.RECEIPT).write_text(json.dumps(made), encoding="utf-8")

    def context(self, label: str = "s") -> Context:
        return Context({"job": "direct", "command": "lab.run", "module": "lab", "work": str(self.root / "work"),
                        "out": str(self.root / "out" / label), "module_config": self.config, "lab": self.lab},
                       lambda event: None)

    def play(self, **params):
        ctx = self.context(params.pop("label", "s"))
        params.setdefault("instance", "game")
        result = runs.run(ctx, **params)
        record = json.loads((Path(result["folder"]) / runs.SESSION).read_text(encoding="utf-8"))
        return ctx, result, record

    def instance(self, name: str = "game") -> Path:
        return self.root / "instances" / name

    # -- tours ---------------------------------------------------------------------------------------------------
    def test_a_tour_captures_every_view_and_files_it_by_label(self):
        ctx, result, record = self.play(kind="tour", tour=str(self.tour), window="1920x1080", jvm=["client:-Dx=1"],
                                        view={"horizontal": 16})
        self.assertEqual(ctx.outcome, "ok", ctx.findings)
        folder = Path(result["folder"])
        self.assertEqual(sorted(p.name for p in (folder / "tour").glob("*.png")), ["01-arrive.png", "02-look-east.png"])
        self.assertEqual(record["tour"]["captured"], 2)
        sent = (self.instance() / "server" / "commands.log").read_text().splitlines()
        self.assertEqual(sent[:3], ["time set 6000", "setworldspawn 1 100 2", "gamemode spectator CubicLab"])
        self.assertIn("weather clear", sent)
        self.assertIn("tp CubicLab 5 110 2 90 0", sent)
        self.assertEqual(sent[-2:], ["kick CubicLab Tour finished", "stop"])
        client = record["commands"]["client"]
        self.assertEqual(client[2:5], ["-Dgame.mode=own", "-Dgame.tour=true", "-Dx=1"])  # after the launch's own
        self.assertEqual(client[client.index("--width") + 1:client.index("--width") + 4], ["1920", "--height", "1080"])
        self.assertEqual(client[-1], str(record["inputs"]["port"]))
        self.assertEqual(record["exit"]["server"], 0)
        self.assertEqual(record["stop"]["how"], "stop")
        # session.json: identity and inputs
        self.assertEqual(record["schema"], runs.SCHEMA)
        self.assertEqual(record["members"], {"game:mod@both": sha256(self.mod)})
        self.assertEqual(record["jdk"]["build"], BUILD)
        self.assertIn("name", record["gpu"])
        self.assertEqual(record["window"], "1920x1080")
        self.assertEqual(record["inputs"]["edits"], ["server/server.properties:view-distance=16"])
        self.assertTrue(record["evidence"])
        self.assertIsNone(record["failure"])
        self.assertIn("server_jvm", record["timings"])
        # the inputs were the session's: the instance's files are as the template made them
        self.assertEqual((self.instance() / "server" / "server.properties").read_text(),
                         "server-port=25565\nview-distance=12\n")
        template = self.root / "instances" / ".store" / "templates" / "kgame" / "instance"
        self.assertEqual((template / "server" / "server.properties").read_text(), "server-port=25565\nview-distance=12\n")

    def test_steps_between_views_far_apart_in_height(self):
        steps = runs.Session._descend_steps({"y": 200}, {"y": 40}, 32)
        self.assertEqual(steps, [168, 136, 104, 72])
        self.assertEqual(runs.Session._descend_steps({"y": 100}, {"y": 120}, 32), [])

    def test_a_tour_without_the_launchs_rules_is_refused(self):
        del self.launch_doc["tour"]
        self.make_instance("bare")
        with self.assertRaisesRegex(ParamError, "no tour rules"):
            self.play(instance="bare", kind="tour", tour=str(self.tour))

    # -- the receipt, checked before start -----------------------------------------------------------------------
    def test_a_jdk_of_another_build_is_refused_and_the_same_build_elsewhere_accepted(self):
        other = self.root / "elsewhere" / "jdk-other"
        self.make_jdk(other, "26.0.1+8")
        with self.assertRaisesRegex(ParamError, "is build 26.0.1\\+8.*refused before start"):
            self.play(kind="tour", tour=str(self.tour), jdk=str(other))
        self.assertFalse((self.instance() / "server" / "argv.json").exists())  # nothing started
        same = self.root / "elsewhere" / "jdk-same"
        shutil.copytree(self.jdk, same)
        ctx, _, record = self.play(kind="tour", tour=str(self.tour), jdk=str(same), label="same")
        self.assertEqual(ctx.outcome, "ok", ctx.findings)
        self.assertEqual(record["jdk"]["home"], str(same))
        self.assertEqual(record["jdk"]["build"], BUILD)

    def test_a_cache_file_that_changed_is_refused_before_start(self):
        (self.cache / "libraries" / "lib.jar").write_bytes(b"other bytes")
        with self.assertRaisesRegex(ParamError, "differs from its receipt.*lib.jar"):
            self.play(kind="tour", tour=str(self.tour))

    def test_run_needs_the_lease(self):
        os.environ.pop("WB_TURN_TOKEN")
        with self.assertRaisesRegex(EnvironmentProblem, "under the GPU lease"):
            self.play(kind="tour", tour=str(self.tour))

    # -- failures and stops --------------------------------------------------------------------------------------
    def test_a_failure_names_the_decisive_line_the_crash_report_and_the_next_command(self):
        os.environ["FAKE_CRASH"] = "1"
        ctx, result, record = self.play(kind="tour", tour=str(self.tour))
        self.assertEqual(ctx.outcome, "finding")
        failure = record["failure"]
        self.assertEqual(failure["line"], "Description: Rendering screen")
        self.assertEqual(Path(failure["crash_report"]).name, "crash-2026-11-24_10.00.00-client.txt")
        self.assertTrue(Path(failure["crash_report"]).is_file())  # kept in the session folder
        self.assertIn("wb play --from", failure["next"])
        self.assertEqual(record["state"], "failed")
        self.assertEqual(record["exit"]["client"], 3)
        self.assertIn("Description: Rendering screen", ctx.findings[0])

    def test_a_server_that_does_not_stop_is_killed_after_the_grace(self):
        os.environ["FAKE_IGNORE_STOP"] = "1"
        saved = runs.STOP_GRACE
        runs.STOP_GRACE = 1.0
        self.addCleanup(setattr, runs, "STOP_GRACE", saved)
        ctx, _, record = self.play(kind="tour", tour=str(self.tour))
        self.assertEqual(record["stop"]["how"], "killed")
        self.assertIn("did not stop", ctx.findings[0])

    def test_an_open_session_relays_commands_until_the_game_is_closed(self):
        os.environ["FAKE_OPEN_SECONDS"] = "3"
        session = "20261124-100000-abcdef"
        commands = self.root / "sessions" / session / "commands.txt"

        def write():
            deadline = time.monotonic() + 20
            while not commands.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            with commands.open("a", encoding="utf-8") as stream:
                stream.write("say hello\n")
        threading.Thread(target=write, daemon=True).start()
        ctx, _, record = self.play(kind="open", session=session)
        self.assertEqual(ctx.outcome, "ok", ctx.findings)
        self.assertIn("say hello", (self.instance() / "server" / "commands.log").read_text().splitlines())
        self.assertEqual(record["exit"]["client"], 0)

    # -- the client channel and the hidden desktop -----------------------------------------------------------------
    def fixture_instance(self, name: str = "fixture"):
        """An instance whose launch names the session's channel and folder to its client, as a fixture mod reads them."""
        self.make_instance(name)
        path = self.instance(name) / instances.RECEIPT
        receipt = json.loads(path.read_text(encoding="utf-8"))
        receipt["client"]["command"][3:3] = ["-Dfixture.channel={channel}", "-Dfixture.session={session}"]
        path.write_text(json.dumps(receipt), encoding="utf-8")
        os.environ["FAKE_FIXTURE"] = "1"

    def write_commands(self, session: str, lines: list[str]):
        folder = self.root / "sessions" / session

        def write():
            deadline = time.monotonic() + 30
            while not (folder / "channel" / "fixture-ready.json").exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            with (folder / "commands.txt").open("a", encoding="utf-8") as stream:
                for line in lines:
                    stream.write(line + "\n")
                    stream.flush()
                    time.sleep(0.2)
        threading.Thread(target=write, daemon=True).start()

    def test_client_lines_of_the_commands_file_reach_the_fixture_through_the_channel(self):
        self.fixture_instance()
        session = "20261124-100000-c0ffee"
        self.write_commands(session, ["client status", 'client echo {"text": "hi"}', "say to the server",
                                      "client {broken", "client fail", "client quit"])
        ctx, result, record = self.play(instance="fixture", kind="open", session=session)
        self.assertEqual(ctx.outcome, "ok", ctx.findings)
        folder = Path(result["folder"])
        client = record["commands"]["client"]
        self.assertIn(f"-Dfixture.channel={folder / 'channel'}", client)  # {channel} filled when the game starts
        self.assertIn(f"-Dfixture.session={folder}", client)
        requests = record["channel"]["requests"]
        self.assertEqual(record["channel"]["folder"], str(folder / "channel"))
        answered = [r for r in requests if r["state"] == "answered"]
        self.assertEqual([r["action"] for r in answered], ["status", "echo", "fail", "quit"])
        self.assertEqual([r["ok"] for r in answered], [True, True, False, True])
        self.assertEqual(answered[2]["error"], "asked to fail")
        self.assertEqual(answered[1]["request"]["text"], "hi")
        echo = json.loads((folder / answered[1]["answer"]).read_text())
        self.assertEqual(echo["result"]["echo"]["action"], "echo")
        refused = [r for r in requests if r["state"] == "refused"]
        self.assertEqual(len(refused), 1)
        self.assertIn("not a JSON object", refused[0]["error"])
        self.assertIn("say to the server", (self.instance("fixture") / "server" / "commands.log").read_text())
        self.assertEqual(record["exit"]["client"], 0)  # the fixture's quit ended the session

    def test_lab_send_sends_one_request_to_a_running_session_and_waits_for_its_answer(self):
        session = "20261124-100000-5e0d00"
        folder = self.root / "sessions" / session
        (folder / "channel" / "responses").mkdir(parents=True)
        (folder / runs.SESSION).write_text(json.dumps({"state": "running"}), encoding="utf-8")
        stop = threading.Event()

        def fixture():
            inbox = folder / "channel" / "request.json"
            while not stop.is_set():
                if inbox.exists():
                    query = json.loads(inbox.read_text())
                    inbox.unlink()
                    (folder / "channel" / "responses" / f"{query['id']}.json").write_text(
                        json.dumps({"ok": True, "result": {"owner": "title", "asked": query}}))
                time.sleep(0.02)
        threading.Thread(target=fixture, daemon=True).start()
        self.addCleanup(stop.set)
        ctx = self.context("send")
        result = runs.send(ctx, session, "status", {"detail": 2}, timeout=10)
        self.assertTrue(result["ok"])
        self.assertEqual(result["answer"]["result"]["asked"]["detail"], 2)
        self.assertTrue(Path(result["file"]).is_file())
        (folder / runs.SESSION).write_text(json.dumps({"state": "ended"}), encoding="utf-8")
        with self.assertRaisesRegex(ParamError, "is ended, not running"):
            runs.send(ctx, session, "status")

    def test_a_request_while_another_is_unread_waits_and_an_unanswered_one_times_out(self):
        folder = self.root / "channel"
        (folder / "responses").mkdir(parents=True)
        first = channels.post(folder, {"action": "status"})
        with self.assertRaisesRegex(channels.ChannelError, "still unread"):
            channels.post(folder, {"action": "status"})
        with self.assertRaisesRegex(channels.ChannelError, "stayed unread"):
            channels.send(folder, "status", timeout=0.3)
        relay = channels.Relay(folder, timeout=0.2)
        (folder / "request.json").unlink()
        relay.add("status")
        relay.pump()  # written
        time.sleep(0.3)
        ended = relay.pump()  # nobody read it: taken back, recorded, the channel free again
        self.assertEqual([r["state"] for r in ended], ["timed_out"])
        self.assertFalse((folder / "request.json").exists())
        self.assertNotEqual(ended[0]["id"], first["id"])
        self.assertEqual(channels.parse('capture {"name": "a.png"}'), {"name": "a.png", "action": "capture"})
        with self.assertRaisesRegex(channels.ChannelError, "names no action"):
            channels.parse('{"name": "a.png"}')

    @unittest.skipUnless(desktops.available(), "a hidden desktop is a Windows feature")
    def test_a_client_on_a_hidden_desktop_runs_there_and_the_desktop_never_has_the_input(self):
        self.fixture_instance("hidden")
        session = "20261124-100000-d0d0d0"
        self.write_commands(session, ["client status", "client quit"])
        ctx, result, record = self.play(instance="hidden", kind="open", session=session, desktop="hidden")
        self.assertEqual(ctx.outcome, "ok", ctx.findings)
        desktop = record["desktop"]
        self.assertTrue(desktop["name"].startswith("wb-session-"))
        self.assertTrue(desktop["never_input"])
        self.assertNotIn(desktop["name"], desktop["input_seen"])
        self.assertEqual(record["inputs"]["desktop"], "hidden")
        self.assertEqual([r["action"] for r in record["channel"]["requests"]], ["status", "quit"])
        self.assertIn("fixture ready", (Path(result["folder"]) / "client-console.log").read_text())  # its pipe
        self.assertEqual(record["exit"]["client"], 0)
        with self.assertRaisesRegex(ParamError, "none of hidden"):
            self.play(kind="open", desktop="elsewhere", label="other")

    # -- kinds that are evidence, and drivers -------------------------------------------------------------------
    def test_measure_plays_a_fresh_copy_of_the_launch_it_names(self):
        launch = self.root / "launch.json"
        launch.write_bytes((self.instance() / "launch.json").read_bytes())
        (self.instance() / "server" / "world").mkdir()  # the named instance's own world is not the copy's
        ctx, result, record = self.play(kind="measure", tour=str(self.tour), launch=str(launch))
        self.assertEqual(ctx.outcome, "ok", ctx.findings)
        self.assertNotEqual(Path(result["instance"]), self.instance())
        self.assertFalse((Path(result["instance"]) / "server" / "world").exists())
        other = self.root / "other.json"
        other.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ParamError, "not the .* measure names"):
            self.play(kind="measure", tour=str(self.tour), launch=str(other), label="other")
        with self.assertRaisesRegex(ParamError, "not evidence"):
            self.play(kind="measure", tour=str(self.tour), launch=str(launch), debug="client", label="debug")

    def test_a_driver_gets_the_token_lease_and_instance_under_the_configured_names(self):
        root = self.root / "project"
        root.mkdir()
        (root / "driver.py").write_text(
            "import json, os, sys\n"
            "json.dump({k: os.environ.get(k) for k in ('WB_TURN_TOKEN', 'OLD_TURN', 'WB_INSTANCE', 'WB_SESSION',"
            " 'CACHE_HERE', 'WB_RUNTIME_CACHE')} | {'cwd': os.getcwd()}"
            " | {'arg': sys.argv[1], 'id': sys.argv[2]},"
            " open(os.path.join(os.environ['WB_SESSION'], 'seen.json'), 'w'))\n",
            encoding="utf-8")
        self.lab["env"] = {"OLD_TURN": "token", "CACHE_HERE": "cache"}
        ctx, result, record = self.play(kind="driver", driver="probe", driver_root=str(root))
        self.assertEqual(ctx.outcome, "ok", ctx.findings)
        seen = json.loads((Path(result["folder"]) / "seen.json").read_text())
        self.assertEqual((seen["WB_TURN_TOKEN"], seen["OLD_TURN"]), ("a-token", "a-token"))
        self.assertEqual(seen["CACHE_HERE"], str(self.cache))  # the cache only by the name [lab] env gives it
        self.assertIsNone(seen["WB_RUNTIME_CACHE"])
        self.assertEqual(Path(seen["cwd"]).resolve(), root.resolve())  # {root} is the project's tree
        self.assertEqual(seen["WB_INSTANCE"], str(self.instance() / "instance.json"))
        self.assertEqual(seen["arg"], seen["WB_INSTANCE"])
        self.assertEqual(seen["id"], record["id"])
        self.assertEqual(record["exit"]["driver"], 0)
        self.lab["trusted"] = []
        with self.assertRaisesRegex(ParamError, "not in \\[lab\\] trusted"):
            self.play(kind="driver", driver="probe", driver_root=str(root), label="untrusted")

    # -- the debugger -------------------------------------------------------------------------------------------
    def test_debug_opens_jdwp_writes_the_attach_configuration_and_is_not_evidence(self):
        ctx, result, record = self.play(kind="tour", tour=str(self.tour), debug="client")
        self.assertFalse(record["evidence"])
        port = record["debug"]["ports"]["client"]
        self.assertIn(f"-agentlib:jdwp=transport=dt_socket,server=y,suspend=n,address=127.0.0.1:{port}",
                      record["commands"]["client"])
        attach = json.loads(Path(record["debug"]["attach"]).read_text())
        self.assertEqual(attach["configurations"][0]["request"], "attach")
        self.assertEqual(attach["configurations"][0]["port"], port)
        self.assertNotIn("-agentlib", " ".join(record["commands"]["server"]))

    # -- kept sessions ------------------------------------------------------------------------------------------
    def test_a_kept_session_is_a_retention_root(self):
        _, result, _ = self.play(kind="tour", tour=str(self.tour))
        kept = runs.keep(self.context("keep"), result["session"], "the reference capture")
        self.assertEqual(kept["roots"]["sessions"], [result["session"]])
        self.assertEqual(kept["roots"]["instances"], ["game"])
        self.assertEqual(kept["roots"]["templates"], ["kgame"])
        record = json.loads((Path(result["folder"]) / runs.SESSION).read_text())
        self.assertEqual(record["kept"]["reason"], "the reference capture")


JAVA = shutil.which("java")
JDB = shutil.which("jdb") or (str(Path(JAVA).with_name("jdb.exe" if os.name == "nt" else "jdb")) if JAVA else None)


@unittest.skipUnless(JAVA and JDB and Path(JDB).is_file(), "needs a JDK with java and jdb")
class Debugger(unittest.TestCase):
    """A breakpoint is hit through the port the attach configuration names."""

    def test_a_breakpoint_is_hit_from_the_attach_configuration(self):
        root = Path(tempfile.mkdtemp(prefix="wb-jdwp-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        (root / "Tick.java").write_text(
            "public class Tick {\n"
            "  static int tick(int n) { return n + 1; }\n"
            "  public static void main(String[] a) throws Exception {\n"
            "    for (int i = 0; i < 600; i++) { tick(i); Thread.sleep(100); }\n"
            "  }\n}\n", encoding="utf-8")
        subprocess.run([str(Path(JAVA).with_name("javac.exe" if os.name == "nt" else "javac")), "-d", str(root),
                        str(root / "Tick.java")], check=True, capture_output=True)
        port = runs.free_port(runs.DEBUG_PORTS["client"])
        agent = f"-agentlib:jdwp=transport=dt_socket,server=y,suspend=n,address=127.0.0.1:{port}"
        game = subprocess.Popen([JAVA, agent, "-cp", str(root), "Tick"], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
        self.addCleanup(game.kill)
        game.stdout.readline()  # "Listening for transport dt_socket at address: <port>"
        attach = Path(root / "jdb.txt")
        attach.write_text(f"jdb -connect com.sun.jdi.SocketAttach:hostname=127.0.0.1,port={port}\n")
        argv = [JDB] + attach.read_text().split()[1:]  # the command lab.run writes beside attach/launch.json
        jdb = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True)
        self.addCleanup(jdb.kill)
        seen = []
        reader = threading.Thread(target=lambda: seen.extend(jdb.stdout), daemon=True)
        reader.start()
        for command, pause in (("stop in Tick.tick", 2), ("cont", 2), ("where", 1), ("exit", 0)):
            jdb.stdin.write(command + "\n")
            jdb.stdin.flush()
            time.sleep(pause)
        jdb.wait(timeout=30)
        reader.join(5)
        jdb = type("Done", (), {"stdout": "".join(seen)})
        self.assertIn("Breakpoint hit", jdb.stdout)
        self.assertIn("Tick.tick", jdb.stdout)


if __name__ == "__main__":
    unittest.main()
