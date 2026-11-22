"""lab.session with a fake game: a server that announces joins and leaves, and clients that connect with the name
their command line gives. Everything but the game is real: the server session, the clients' folders and command
lines from a runtime.json, the joins, the scripted kill, the stop and what is kept."""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path[:0] = [str(REPO / "core"), str(REPO / "sdk" / "python"), str(REPO / "modules" / "lab")]

from wb_core import manifest, types  # noqa: E402
from wb_core.module import Context, ParamError  # noqa: E402
from wb_lab import desktop, session  # noqa: E402

FAKE_SERVER = r'''
import socket, sys, threading
port = int([l.split("=", 1)[1] for l in open("server.properties") if l.startswith("server-port=")][0])
listener = socket.socket()
listener.bind(("127.0.0.1", port))
listener.listen()
print("Done (0.1s)! For help, type \"help\"", flush=True)

def serve(connection):
    name = connection.makefile().readline().strip()
    print(f"{name} joined the game", flush=True)
    try:
        while connection.recv(64):
            pass
    except OSError:  # a killed client's socket is reset, not closed
        pass
    print(f"{name} left the game", flush=True)

def accept():
    while True:
        connection, _ = listener.accept()
        threading.Thread(target=serve, args=(connection,), daemon=True).start()

threading.Thread(target=accept, daemon=True).start()
for line in sys.stdin:
    if line.strip() == "stop":
        print("Stopping the server", flush=True)
        break
'''

FAKE_CLIENT = r'''
import pathlib, socket, sys, time
args = sys.argv[1:]
value = lambda flag: args[args.index(flag) + 1]
name, game = value("--username"), pathlib.Path(value("--gameDir"))
(game / "logs").mkdir(parents=True, exist_ok=True)
(game / "logs" / "latest.log").write_text(f"client {name} started with {len(args)} arguments\n")
connection = socket.create_connection((value("--server"), int(value("--port"))))
connection.sendall((name + "\n").encode())
print(f"fake client {name} connected as {value('--uuid')}", flush=True)
time.sleep(600)
'''


def defaults(**given) -> dict:
    """The command's parameters as Workbench fills them: the manifest's defaults under what is given."""
    module = manifest.parse(REPO / "modules" / "lab" / "wb_lab" / "module.toml", "wb_lab")
    command = next(c for c in module.commands if c.name == "session")
    return types.validate(command.params, given)


class Sessions(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="wb-session-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        server = self.root / "server"
        server.mkdir()
        (server / "fake_server.py").write_text(FAKE_SERVER, encoding="utf-8")
        (server / "server.properties").write_text("motd=test\n", encoding="utf-8")
        (server / "mods").mkdir()
        (server / "mods" / "shared.jar").write_bytes(b"a mod both sides load")
        (server / "mods" / "1.12.2").mkdir()
        (server / "mods" / "1.12.2" / "library.jar").write_bytes(b"a library the loader keeps under mods")
        (self.root / "fake_client.py").write_text(FAKE_CLIENT, encoding="utf-8")
        (self.root / "client.jar").write_bytes(b"client")
        self.runtime = self.root / "runtime.json"
        self.runtime.write_text(json.dumps({
            "schema": "wb.lab.runtime.v1", "main_class": "fake.Main", "tweakers": ["fake.Tweaker"],
            "game_arguments": "--username ${auth_player_name} --gameDir ${game_directory} --uuid ${auth_uuid}",
            "jvm_args": [], "classpath": [str(self.root / "client.jar")], "natives": str(self.root / "natives"),
            "assets": {"root": str(self.root / "assets"), "index": "1.12"}}), encoding="utf-8")
        real = session.client_argv
        script = str(self.root / "fake_client.py")
        # The game is faked: the client's java is Python running the fake client with the rest of its command line.
        self.patch = lambda *a, **k: [sys.executable, "-u", script, *real(*a, **k)[1:]]
        session.client_argv, self._real = self.patch, real
        self.addCleanup(setattr, session, "client_argv", real)

    def run_session(self, name: str, **given):
        params = defaults(**{"server": str(self.root / "server"), "runtime": str(self.runtime), "jdk": "",
                             "launch": [sys.executable, "-u", "fake_server.py"], "ready": ["Done \\("],
                             "seconds": 2, "grace": "20s", **given})
        ctx = Context({"job": "direct", "command": "lab.session", "module": "lab", "work": str(self.root / name / "work"),
                       "out": str(self.root / name / "out"), "jdks": {"26": str(self.root / "jdk")}},
                      lambda event: None)
        result = session.session(ctx, **params)
        receipt = json.loads((self.root / name / "out" / "receipt.json").read_text(encoding="utf-8"))
        return ctx, result, receipt, self.root / name / "out"

    def test_two_clients_join_and_a_scripted_kill_is_seen_to_leave(self):
        ctx, result, receipt, out = self.run_session("two", clients=2, kill_client=2, kill_after="0.5s")
        self.assertEqual(ctx.outcome, "ok", ctx.findings)
        clients = {client["name"]: client for client in receipt["clients"]}
        self.assertEqual(sorted(clients), ["wb1", "wb2"])
        self.assertTrue(all(client["joined_at"] is not None for client in clients.values()))
        self.assertIsNotNone(clients["wb2"]["killed"])
        self.assertIsNotNone(clients["wb2"]["left_at"])  # the server saw the killed client go
        self.assertIsNone(clients["wb1"]["killed"])
        self.assertEqual(clients["wb1"]["uuid"], session.offline_uuid("wb1"))
        console = (out / "clients" / "wb1" / "console.log").read_text(encoding="utf-8")
        self.assertIn(f"fake client wb1 connected as {session.offline_uuid('wb1')}", console)
        self.assertTrue((out / "clients" / "wb1" / "latest.log").is_file())
        argv = clients["wb1"]["argv"]
        self.assertIn("--server", argv)
        self.assertEqual(argv[argv.index("--tweakClass") + 1], "fake.Tweaker")
        self.assertTrue((self.root / "two" / "work" / "clients" / "wb1" / "mods" / "shared.jar").is_file())
        self.assertTrue((self.root / "two" / "work" / "clients" / "wb1" / "mods" / "1.12.2" / "library.jar").is_file())
        self.assertEqual(receipt["runtime"]["main_class"], "fake.Main")

    @unittest.skipUnless(desktop.available(), "hidden desktops are a Windows feature")
    def test_clients_on_a_hidden_desktop_never_take_the_input(self):
        ctx, result, receipt, out = self.run_session("hidden", clients=1, desktop="hidden")
        self.assertEqual(ctx.outcome, "ok", ctx.findings)
        self.assertTrue(receipt["desktop"]["never_input"])
        self.assertIn("fake client wb1 connected", (out / "clients" / "wb1" / "console.log").read_text(encoding="utf-8"))

    def test_a_client_that_never_joins_is_a_finding(self):
        session.client_argv = lambda *a, **k: [sys.executable, "-c", "print('crashed at start'); raise SystemExit(3)"]
        ctx, result, receipt, out = self.run_session("crash", clients=1)
        self.assertEqual(ctx.outcome, "finding")
        self.assertTrue(any("exited 3 before it joined: crashed at start" in f for f in ctx.findings), ctx.findings)

    def test_refusals_name_what_is_wrong(self):
        for given, expected in (({"window": "big"}, "WIDTHxHEIGHT"), ({"joined": "joined"}, "{name}"),
                                ({"seconds": 0}, "how long the clients play")):
            with self.subTest(expected=expected):
                with self.assertRaises(ParamError) as raised:
                    self.run_session("refused", **given)
                self.assertIn(expected, str(raised.exception))

    def test_the_client_command_line_comes_from_the_runtime(self):
        runtime = json.loads(self.runtime.read_text(encoding="utf-8"))
        argv = self._real(runtime, "java", Path("/games/wb1"), "wb1", ("127.0.0.1", 25565), ["-Xmx2G"], (960, 540))
        self.assertEqual(argv[:2], ["java", "-Xmx2G"])
        self.assertEqual(argv[argv.index("-cp") + 2], "fake.Main")
        self.assertIn("--uuid", argv)
        self.assertEqual(argv[-4:], ["--server", "127.0.0.1", "--port", "25565"])
        self.assertEqual(session.offline_uuid("Notch"), "b50ad385829d3141a2167e7d7539ba7f")  # the game's own


if __name__ == "__main__":
    unittest.main()
