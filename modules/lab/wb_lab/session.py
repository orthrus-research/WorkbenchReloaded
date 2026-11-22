"""lab.session: one dedicated server and several clients on the host, each client started from an assembled runtime
(``lab.assemble``'s runtime.json) with no launcher.

The server is a ``lab.server`` session, with all of its options: a copy of a stopped server folder, ready patterns,
commands, a run of ticks, seconds or until a line, and a graceful stop. Once it is ready, each client gets a game
folder of its own (the server's mods, ``client_mods`` added, ``client_files`` laid over), an offline name and the UUID
the game gives that name, and is started with ``--server`` so it joins on its own. The session waits until the
server's console shows every client joined (``joined``, with ``{name}``), then runs as a server session does; a
scripted ``kill_client`` is killed ``kill_after`` the last join, and the server is watched for its leaving. Clients
are stopped before the server. ``desktop = "hidden"`` starts them on a hidden desktop (Windows), so they never take
the user's focus or input.

Each client's console, log and crash reports are kept under ``clients/<name>/`` in the out folder, and the receipt
gains the runtime (by SHA-256), each client's command line, join time, exit and what the kill did.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from wb_core.module import Context, EnvironmentProblem, ParamError, sha256_file

from . import desktop as desktops
from . import process as processes
from .server import Options, Session, _span

DEFAULT_GAME_ARGUMENTS = ("--username ${auth_player_name} --version ${version_name} --gameDir ${game_directory} "
                          "--assetsDir ${assets_root} --assetIndex ${assets_index_name} --uuid ${auth_uuid} "
                          "--accessToken ${auth_access_token} --userType ${user_type}")
PLACEHOLDER = re.compile(r"\$\{([a-z_]+)\}")


def offline_uuid(name: str) -> str:
    """The UUID the game gives an offline player: an MD5 name UUID of ``OfflinePlayer:<name>``."""
    digest = bytearray(hashlib.md5(("OfflinePlayer:" + name).encode("utf-8")).digest())
    digest[6] = (digest[6] & 0x0F) | 0x30
    digest[8] = (digest[8] & 0x3F) | 0x80
    return uuid.UUID(bytes=bytes(digest)).hex


def client_argv(runtime: dict, java: str, game: Path, name: str, server: tuple[str, int] | None,
                jvm_args: list[str], window: tuple[int, int]) -> list[str]:
    """A client's command line from an assembled runtime."""
    if not runtime.get("main_class"):
        raise ParamError("the runtime names no main class")
    assets = runtime.get("assets") or {}
    values = {"auth_player_name": name, "version_name": "workbench", "game_directory": str(game),
              "assets_root": str(assets.get("root", "")), "assets_index_name": str(assets.get("index", "")),
              "auth_uuid": offline_uuid(name), "auth_access_token": "0", "user_type": "legacy",
              "version_type": "release", "user_properties": "{}", "auth_session": "0", "game_assets": str(assets.get("root", ""))}
    game_args = [PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), item)
                 for item in shlex.split(runtime.get("game_arguments") or DEFAULT_GAME_ARGUMENTS)]
    for tweaker in runtime.get("tweakers") or []:
        if tweaker not in game_args:
            game_args += ["--tweakClass", tweaker]
    argv = [java, *runtime.get("jvm_args", []), *jvm_args, f"-Djava.library.path={runtime['natives']}",
            "-cp", os.pathsep.join(runtime["classpath"]), runtime["main_class"], *game_args,
            "--width", str(window[0]), "--height", str(window[1])]
    if server:
        argv += ["--server", server[0], "--port", str(server[1])]
    return argv


@dataclass
class Client:
    name: str
    game: Path
    log: Path
    argv: list[str]
    process: object = None  # subprocess.Popen or desktop.DesktopProcess
    console: processes.Console | None = None
    joined_at: float | None = None
    exit: int | None = None
    killed: str | None = None
    left_at: float | None = None
    record: dict = field(default_factory=dict)

    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def stop(self):
        if self.process is None:
            return
        if isinstance(self.process, desktops.DesktopProcess):
            self.process.kill_tree()
        else:
            processes.kill_tree(self.process)
        self.exit = self.process.poll()


class MultiSession(Session):
    def __init__(self, ctx: Context, options: Options, clients: dict):
        super().__init__(ctx, options)
        self.c = clients
        self.clients: list[Client] = []
        self.runtime: dict = {}
        self.desktop: desktops.HiddenDesktop | None = None
        self.kill_timer: threading.Timer | None = None

    # the server is ready: start the clients and wait for them to join, then run as a server session does
    def _run(self):
        if self._start_clients() and self._await_joins():
            self._arm_kill()
            super()._run()
            self._await_leave()

    def _start_clients(self) -> bool:
        ctx, c = self.ctx, self.c
        java = str(ctx.jdk(c["jdk"]) / "bin" / ("java.exe" if os.name == "nt" else "java"))
        port = self._listening_port()
        if c["desktop"] == "hidden":
            self.desktop = desktops.HiddenDesktop().__enter__()
        env = dict(os.environ, JAVA_HOME=str(Path(java).parents[1]))
        for index in range(1, c["count"] + 1):
            name = f"{c['prefix']}{index}"
            game = ctx.work / "clients" / name
            (game / "mods").mkdir(parents=True, exist_ok=True)
            if (self.copy / "mods").is_dir():  # the server's mods, with the libraries a loader keeps under them
                shutil.copytree(self.copy / "mods", game / "mods", dirs_exist_ok=True)
            for mod in c["mods"]:
                shutil.copy2(mod, game / "mods" / mod.name)
            if c["files"] is not None:
                shutil.copytree(c["files"], game, dirs_exist_ok=True)
            options = game / "options.txt"
            if not options.exists():  # a client in the background must not pause when it loses focus
                options.write_text("pauseOnLostFocus:false\n", encoding="utf-8")
            log = ctx.out / "clients" / name / "console.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            argv = client_argv(self.runtime, java, game, name, ("127.0.0.1", port), c["jvm_args"], c["window"])
            client = Client(name, game, log, argv)
            ctx.progress("clients", msg=f"starting {name} ({index} of {c['count']})")
            if self.desktop is not None:
                client.process = self.desktop.start(argv, cwd=game, env=env, log=log)
            else:
                client.process = processes.start(argv, cwd=game, env=env, stdin=False, owned=True)
                client.console = processes.Console(client.process, log)
            self.clients.append(client)
        return True

    def _listening_port(self) -> int:
        properties = self.copy / "server.properties"
        for line in properties.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip().startswith("server-port="):
                return int(line.split("=", 1)[1].strip() or 25565)
        return 25565

    def _await_joins(self) -> bool:
        ctx, c, console = self.ctx, self.c, self.console
        patterns = {client.name: re.compile(c["joined"].replace("{name}", re.escape(client.name)))
                    for client in self.clients}
        deadline = time.monotonic() + c["join_timeout"]
        position = self.ready_index
        waiting = {client.name: client for client in self.clients}
        ctx.progress("clients", msg=f"waiting for {len(waiting)} client(s) to join")
        while waiting and time.monotonic() < deadline:
            for line in console.scan(position, min(deadline, time.monotonic() + 1.0), ctx.cancelled):
                position = line.index + 1
                for name in [n for n, p in patterns.items() if n in waiting and p.search(line.text)]:
                    waiting.pop(name).joined_at = line.at
            for client in list(waiting.values()):
                if not client.alive():
                    client.exit = client.process.poll()
                    waiting.pop(client.name)
                    self._finding(f"client {client.name} exited {client.exit} before it joined: "
                                  f"{_last_line(client.log)}")
                    return False
            if self._gone():
                self._finding("the server exited while the clients were joining", exit=True)
                return False
        self.ready_index = position
        if waiting:
            self._finding(f"{', '.join(waiting)} did not join within {_span(c['join_timeout'])}")
            return False
        joined = [client.joined_at for client in self.clients]
        ctx.measure("clients_joined", len(joined))
        ctx.measure("last_join_seconds", round(max(joined) - min(joined), 3), unit="s")
        return True

    def _arm_kill(self):
        c = self.c
        if not c["kill_client"]:
            return
        if c["kill_client"] > len(self.clients):
            raise ParamError(f"kill_client {c['kill_client']}: the session has {len(self.clients)} client(s)")
        target = self.clients[c["kill_client"] - 1]

        def kill():
            if target.alive():
                target.stop()
                target.killed = f"{_span(c['kill_after'])} after the last join"
                self.ctx.log.info(f"killed {target.name} as scripted")

        self.kill_timer = threading.Timer(c["kill_after"], kill)
        self.kill_timer.daemon = True
        self.kill_timer.start()

    def _await_leave(self):
        """After a scripted kill, the server should notice the client go."""
        target = next((client for client in self.clients if client.killed), None)
        if target is None:
            return
        pattern = re.compile(self.c["left"].replace("{name}", re.escape(target.name)))
        line = self.console.first([pattern], 0, time.monotonic() + self.c["leave_timeout"], self.ctx.cancelled)
        if line is None:
            self._finding(f"the server did not show {target.name} leaving within {_span(self.c['leave_timeout'])} "
                          "of the scripted kill")
        else:
            target.left_at = line[1].at

    # clients go before the server
    def _stop(self, cancelled: bool):
        if self.kill_timer is not None:
            self.kill_timer.cancel()
        for client in self.clients:
            if client.alive():
                client.stop()
            elif client.process is not None:
                client.exit = client.process.poll()
        try:
            super()._stop(cancelled)
        finally:
            if self.desktop is not None:
                self.receipt["desktop"] = {"name": self.desktop.name, "input_seen": self.desktop.input_seen,
                                           "never_input": self.desktop.never_input}
                self.desktop.__exit__(None, None, None)

    def _collect(self):
        try:
            super()._collect()
        finally:
            for client in self.clients:
                if client.console is not None:
                    client.console.close()
                kept = self.ctx.out / "clients" / client.name
                latest = client.game / "logs" / "latest.log"
                if latest.is_file():
                    shutil.copy2(latest, kept / "latest.log")
                crashes = client.game / "crash-reports"
                if crashes.is_dir():
                    shutil.copytree(crashes, kept / "crash-reports", dirs_exist_ok=True)
                for hs_err in client.game.glob("hs_err_pid*.log"):
                    shutil.copy2(hs_err, kept / hs_err.name)
            self.ctx.output("clients", self.ctx.out / "clients")

    def _receipt(self) -> dict:
        receipt = super()._receipt()
        receipt["runtime"] = {"path": self.ctx.host_path(self.c["runtime"]), "sha256": sha256_file(self.c["runtime"]),
                              "main_class": self.runtime.get("main_class")}
        receipt["clients"] = [{"name": client.name, "uuid": offline_uuid(client.name), "argv": client.argv,
                               "joined_at": client.joined_at, "exit": client.exit, "killed": client.killed,
                               "left_at": client.left_at} for client in self.clients]
        return receipt


def _last_line(log: Path) -> str:
    try:
        lines = [line for line in log.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()]
    except OSError:
        return "no output"
    return lines[-1][:200] if lines else "no output"


def session(ctx: Context, runtime: str, clients: int, prefix: str, client_jdk: str, client_mods: list,
            client_jvm_args: list, window: str, joined: str, left: str, join_timeout: float, kill_client: int,
            kill_after: float, leave_timeout: float, desktop: str, client_files: str | None = None, **params):
    """lab.session: one server and several clients (module.toml lists the params; this module's docstring the run)."""
    options = Options.parse(params)
    runtime_path = Path(runtime)
    try:
        loaded = json.loads(runtime_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ParamError(f"runtime: {runtime} is not a runtime.json ({error})") from None
    if loaded.get("schema") != "wb.lab.runtime.v1":
        raise ParamError(f"runtime: {runtime} is not lab.assemble's runtime.json (schema {loaded.get('schema')!r})")
    if desktop == "hidden" and not desktops.available():
        raise ParamError("desktop = hidden needs Windows")
    match = re.fullmatch(r"(\d+)x(\d+)", window)
    if not match:
        raise ParamError(f"window: {window!r} is not WIDTHxHEIGHT")
    for name, pattern in (("joined", joined), ("left", left)):
        if "{name}" not in pattern:
            raise ParamError(f"{name}: the pattern must name the client as {{name}}")
    mods = [Path(m) for m in client_mods]
    missing = [str(m) for m in mods if not m.is_file()]
    if missing:
        raise ParamError(f"client_mods: {missing[0]} is not a file")
    if client_files and not Path(client_files).is_dir():
        raise ParamError(f"client_files: {client_files} is not a folder")
    if not options.runs:
        raise ParamError("a session needs ticks, seconds or until: how long the clients play")
    unknown = [entry for entry in loaded.get("classpath", []) if not Path(entry).exists()]
    if unknown:
        raise EnvironmentProblem(f"the runtime's class path names {len(unknown)} file(s) this machine lacks, such as "
                                 f"{unknown[0]} (assemble it where the cache is under a root the host shares)")
    multi = MultiSession(ctx, options, {
        "runtime": runtime_path, "count": clients, "prefix": prefix, "jdk": client_jdk, "mods": mods,
        "files": Path(client_files) if client_files else None, "jvm_args": [str(a) for a in client_jvm_args],
        "window": (int(match.group(1)), int(match.group(2))), "joined": joined, "left": left,
        "join_timeout": join_timeout, "kill_client": kill_client, "kill_after": kill_after,
        "leave_timeout": leave_timeout, "desktop": desktop})
    multi.runtime = loaded
    return multi.execute()
