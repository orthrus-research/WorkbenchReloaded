"""lab.run: one session of an instance lab.build made, started from its receipt (instance.json) and nothing else.

Kinds:

- ``open``: a person plays. The server starts, the client joins, and lines appended to the session's
  ``commands.txt`` are sent to the server's console until the client is closed (or the limit).
- ``tour``: the views of a tour file, each captured by the game, then filed as ``tour/NN-<label>.png``. How the game
  is driven through them is the launch's ``tour`` rules (from its ``[launch.<name>.tour]``): the server commands
  that set the world up, the client's JVM properties that turn its capture on, and the client console line that says
  a view was captured. Between views the player is teleported, in steps when the height changes a lot.
- ``measure``: a tour that is evidence: quiet (nothing else runs on the machine), on a fresh copy of the instance's
  template (or, with ``copy=false``, the named instance itself), and only when the instance was built from the
  launch file the request names.
- ``driver``: the launch's declared driver (``drivers.<name>`` in the launch file), a trusted project's program
  that starts its own JVMs from the instance; it gets the turn token, the lease, the instance.json path, the session
  folder and the launch file in Workbench's own variables and the names ``[lab] env`` adds, and the runtime cache the
  receipt's ``{cache}`` paths are under by the name ``[lab] env`` gives ``cache``. Its command line may name
  ``{python}``, ``{root}`` (the project's tree it runs from: ``driver_root``, which wb play sets to the worktree the
  launch was resolved from), ``{instance}``, ``{instance_json}``, ``{session}`` (the folder), ``{session_id}`` and
  ``{launch}``.

Before anything starts the session checks the instance against its receipt: every file under ``{cache}`` and
``{jdk:N}`` and every placed member by SHA-256. The JDK is found by the build the receipt names (the Store's, or the
one ``jdk`` names): a JDK of another build is refused, the same build at another path is accepted.

What a session chooses is its own and goes into ``session.json``, never into the template: the port (``{port}``),
JVM options (``jvm``), the window, view distances (``view``, the base's view keys), file edits (``edits``) and the
tour. Edited files are put back when the session ends. ``debug`` opens JDWP on loopback for the client, the server
or both, writes an attach configuration (``attach/launch.json``, for VS Code) and marks the session as not evidence.

A stop sends the server ``stop`` and waits at least 180 s for its own exit before the tree is killed. On a failure
the session names the decisive line (the launch's ``failures`` patterns first, then Workbench's), the crash report
and the next command.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from wb_core.module import Cancelled, Context, EnvironmentProblem, ParamError, sha256_file

from . import instance as instances
from . import machine as machines
from . import process as processes
from .bases import INTERIM_BASES
from .store import MARKER, Store

SCHEMA = "wb.lab.session/1"
SESSION = "session.json"
KEPT = "kept.json"
KINDS = ("open", "tour", "measure", "driver")
SIDES = ("server", "client")
READY = re.compile(r"Done \(")
READY_TIMEOUT = 600.0
JOIN_TIMEOUT = 120.0
STOP_GRACE = 180.0  # the least a server is given to stop by itself before its tree is killed
TOUR_LIMIT = 90 * 60.0
DEBUG_PORTS = {"client": 5005, "server": 5006}
ID = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$")
# Tried after the launch's own failure patterns, most telling first.
FAILURES = (r"^Description: ", r"EXCEPTION_ACCESS_VIOLATION|# A fatal error has been detected",
            r"^Exception in thread ", r"^Caused by: ", r"\b[A-Za-z.]+(Exception|Error): ")
TOUR_DEFAULTS = {"setup": [], "spawn": "setworldspawn {x} {y} {z}", "joined": "joined the game", "spectator": True,
                 "client_jvm": [], "captured": "", "capture_timeout": 900, "step": 32, "pause": 5.0,
                 "leave": ["kick {player} Tour finished"], "leave_seconds": 12, "screenshots": "screenshots"}
ENV = {"token": "WB_TURN_TOKEN", "lane": "WB_TURN_LANE", "lease": "WB_LEASE_FILE",
       "coordination": "WB_COORDINATION_DIR", "instance": "WB_INSTANCE", "session": "WB_SESSION",
       "launch": "WB_LAUNCH"}


class Failed(Exception):
    """The session failed: a process exited, a wait ran out. ``where`` names the side to look at."""

    def __init__(self, message: str, where: str | None = None):
        super().__init__(message)
        self.where = where


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def new_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)


def sessions_root(ctx: Context) -> Path:
    configured = str(ctx.config.get("sessions") or "")
    return instances.Machine().path(configured) if configured else machines.home(ctx) / "sessions"


def write_json(path: Path, value):
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, indent=1, default=str) + "\n", encoding="utf-8")
    os.replace(temporary, path)


# --- the receipt, checked ---------------------------------------------------------------------------------------------


def _release(home: Path) -> dict[str, str]:
    info = {}
    release = home / "release"
    if release.is_file():
        for line in release.read_text(encoding="utf-8", errors="replace").splitlines():
            key, sep, value = line.partition("=")
            if sep:
                info[key.strip()] = value.strip().strip('"')
    return info


def find_jdk(receipt: dict, launch: dict, given: str | None) -> tuple[str, Path, str]:
    """The receipt's JDK root (``jdk:N``), its home and its build: ``given`` (a JDK home) or the Store's by the build
    the receipt names. A JDK of another build is refused here; its bytes are checked with the other files."""
    (major, entry), = receipt["jdk"].items()
    build = str(entry["build"])
    if given:
        home = Path(given)
        if not (home / "bin").is_dir():
            raise ParamError(f"jdk: {given} is no JDK home (no bin folder)")
    else:
        home = instances.jdk_home(Path(launch["store"]), build, receipt["system"])
    found = _release(home).get("JAVA_RUNTIME_VERSION", "")
    if found != build:
        raise ParamError(f"the JDK at {home} is build {found or 'unknown'}, and instance {receipt.get('name')} was built "
                         f"and recorded on {build}: refused before start (give jdk=<a home of {build}>, or build an "
                         "instance on the other JDK)")
    return f"jdk:{major}", home, build


def _hash(path: Path) -> str | None:
    try:
        return sha256_file(path)
    except OSError:
        return None


def verify(receipt: dict, roots: dict[str, Path]) -> list[str]:
    """Every file of the receipt under ``{cache}`` and ``{jdk:N}``, and every placed member, hashed again: the
    differences, each named."""
    wanted = {}
    for key, entry in receipt["files"].items():
        root = key[1:key.index("}")]
        if root != "instance":
            wanted[key] = entry["sha256"]
    for member in receipt.get("members") or []:
        for placed in member["placed"]:
            wanted[placed] = member["sha256"]
    paths = {key: Path(instances.expand([key], {n: str(r) for n, r in roots.items()})[0]) for key in wanted}
    with ThreadPoolExecutor(max_workers=8) as pool:
        actual = dict(zip(paths, pool.map(_hash, paths.values())))
    problems = []
    for key, digest in wanted.items():
        if actual[key] is None:
            problems.append(f"{key} is missing ({paths[key]})")
        elif actual[key] != digest:
            problems.append(f"{key} is {actual[key][:12]}, not the receipt's {digest[:12]}")
    return sorted(problems)


# --- the machine ---------------------------------------------------------------------------------------------------


def gpu() -> dict:
    """The GPU's name and driver version: nvidia-smi where it is on PATH, else the system's display adapter (WMI on
    Windows), else unknown."""
    if shutil.which("nvidia-smi"):
        try:
            done = subprocess.run(["nvidia-smi", "--query-gpu=name,driver_version", "--format=csv,noheader"],
                                  capture_output=True, text=True, timeout=15)
            if done.returncode == 0 and done.stdout.strip():
                name, driver = [p.strip() for p in done.stdout.strip().splitlines()[0].split(",")[:2]]
                return {"name": name, "driver": driver, "source": "nvidia-smi"}
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
    if os.name == "nt":
        shell = shutil.which("powershell.exe") or shutil.which("pwsh")
        if shell:
            query = ("Get-CimInstance Win32_VideoController | Sort-Object AdapterRAM -Descending | "
                     "Select-Object -First 1 Name,DriverVersion | ConvertTo-Json -Compress")
            try:
                done = subprocess.run([shell, "-NoProfile", "-Command", query], capture_output=True, text=True,
                                      timeout=30)
                value = json.loads(done.stdout) if done.returncode == 0 and done.stdout.strip() else None
                if isinstance(value, dict) and value.get("Name"):
                    return {"name": value["Name"], "driver": value.get("DriverVersion") or "unknown", "source": "wmi"}
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass
    return {"name": "unknown", "driver": "unknown", "source": None}


def free_port(preferred: int | None = None) -> int:
    if preferred:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", preferred))
                return preferred
            except OSError:
                pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


# --- session inputs ------------------------------------------------------------------------------------------------


def parse_jvm(items: list) -> dict[str, list[str]]:
    """``jvm`` items: ``-X`` for both sides, ``client:-X`` or ``server:-X`` for one."""
    out = {"server": [], "client": []}
    for item in items or []:
        side, sep, value = str(item).partition(":")
        if sep and side in SIDES and value.startswith("-"):
            out[side].append(value)
        elif str(item).startswith("-"):
            for name in SIDES:
                out[name].append(str(item))
        else:
            raise ParamError(f"jvm: {item!r} is not a JVM option (-X, client:-X or server:-X)")
    return out


EDIT = re.compile(r"^(server|client)/([^:]+):([^=]+)=(.*)$")


def parse_edit(text: str) -> tuple[str, str, str, str]:
    match = EDIT.match(str(text))
    if not match or ".." in match.group(2).replace("\\", "/").split("/"):
        raise ParamError(f"edits: {text!r} is not <server|client>/<file in the instance>:<key>=<value>")
    return match.groups()


def view_edits(base: dict, view: dict) -> list[str]:
    """``view`` keys (the base's ``views``) as edits."""
    known = base.get("views") or {}
    out = []
    for key, value in (view or {}).items():
        if key not in known:
            raise ParamError(f"view: {key!r} is no view key of the base (known: {', '.join(sorted(known)) or 'none'})")
        if not str(value).lstrip("-").isdigit():
            raise ParamError(f"view: {key}={value!r} is not a whole number")
        out += [f"{target}={value}" for target in known[key]]
    return out


def set_key(text: str, key: str, value: str, name: str) -> str:
    """Sets ``key`` in a properties (``key=value``), options (``key:value``) or Forge config (``X:key=value``) text,
    keeping its separator and indentation; a missing key is added in the file's own form, except in a Forge config."""
    pattern = re.compile(r"^(\s*" + re.escape(key) + r"\s*)([=:])(.*)$", re.MULTILINE)
    if pattern.search(text):
        return pattern.sub(lambda m: m.group(1) + m.group(2) + value, text, count=1)
    if name.endswith(".cfg"):
        raise ParamError(f"edits: {name} has no {key}")
    separator = ":" if name == "options.txt" else "="
    return text + ("" if text.endswith("\n") or not text else "\n") + f"{key}{separator}{value}\n"


# --- processes -----------------------------------------------------------------------------------------------------


def start(argv: list[str], cwd: Path, env: dict) -> subprocess.Popen:
    """A game process with its console on a pipe and its stdin for commands, and no console window of its own."""
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        return subprocess.Popen([str(a) for a in argv], cwd=str(cwd), env=env, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                errors="replace", creationflags=flags, start_new_session=os.name != "nt")
    except OSError as error:
        raise EnvironmentProblem(f"cannot start {Path(argv[0]).name}: {error}") from None


class Side:
    def __init__(self, name: str, process: subprocess.Popen, console: processes.Console, started: float):
        self.name, self.process, self.console, self.started = name, process, console, started
        self.position = 0

    def alive(self) -> bool:
        return self.process.poll() is None


class Session:
    def __init__(self, ctx: Context, p: dict):
        self.ctx, self.p = ctx, p
        self.t0 = time.monotonic()
        self.timings: dict[str, float] = {}
        self.sides: dict[str, Side] = {}
        self.restore: dict[Path, bytes | None] = {}
        self.record: dict = {}
        self.failure: Failed | None = None
        self.stopped = None

    def mark(self, name: str):
        self.timings[name] = round(time.monotonic() - self.t0, 3)

    # -- setting up ----------------------------------------------------------------------------------------------
    def open(self):
        ctx, p = self.ctx, self.p
        kind = p["kind"]
        if kind not in KINDS:
            raise ParamError(f"kind: {kind!r} is none of {', '.join(KINDS)}")
        if not os.environ.get("WB_TURN_TOKEN"):
            raise EnvironmentProblem("lab.run must run under the GPU lease: wb-host takes it for this routine, and "
                                     "wb gpu run -- <command> gives any command a turn")
        machine = instances.Machine()
        root = instances._lab_root(ctx, machine)
        folder = root / p["instance"]
        receipt_path = folder / instances.RECEIPT
        if not receipt_path.is_file():
            raise ParamError(f"instance {p['instance']}: {folder} has no {instances.RECEIPT} (lab.build makes one)")
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if receipt.get("schema") != instances.SCHEMA:
            raise ParamError(f"instance {p['instance']}: its receipt is not {instances.SCHEMA}")
        launch_path = folder / instances.LAUNCH_COPY
        if not launch_path.is_file() or sha256_file(launch_path) != receipt["launch"]["sha256"]:
            raise ParamError(f"instance {p['instance']}: its launch.json is not the one its receipt names")
        launch = json.loads(launch_path.read_text(encoding="utf-8"))
        self.not_evidence = []
        if p.get("debug"):
            self.not_evidence.append(f"debug={p['debug']}: JDWP is open")
        if p.get("no_rebuild"):
            self.not_evidence.append("no_rebuild: members compiled against other bytes were not rebuilt")
        if kind == "measure":
            if self.not_evidence:
                raise ParamError("measure refuses a session that is not evidence: " + "; ".join(self.not_evidence))
            if not p.get("launch"):
                raise ParamError("measure names the launch file it measures (launch=<launch.json>)")
            wanted = sha256_file(Path(p["launch"]))
            if wanted != receipt["launch"]["sha256"]:
                raise ParamError(f"instance {p['instance']} was built from launch {receipt['launch']['sha256'][:12]}, "
                                 f"not the {wanted[:12]} measure names; build one from it (lab.build)")
        if kind in ("tour", "measure") and not p.get("tour"):
            raise ParamError(f"a {kind} session names its tour (tour=<file>)")
        if kind == "driver":
            drivers = launch.get("drivers") or {}
            if not p.get("driver") or p["driver"] not in drivers:
                raise ParamError(f"driver: name one the launch declares ({', '.join(sorted(drivers)) or 'none'})")
            machines.require_trusted(ctx, str(launch.get("project") or ""), f"driver {p['driver']}")
        self.id = p.get("session") or new_id()
        if not ID.match(self.id):
            raise ParamError(f"session: {self.id!r} is not an id like 20261124-101500-a1b2c3")
        self.folder = sessions_root(ctx) / self.id
        self.folder.mkdir(parents=True, exist_ok=False)
        if kind == "measure" and p.get("copy", True):
            folder = self._fresh_copy(root, folder, receipt)
            receipt = json.loads((folder / instances.RECEIPT).read_text(encoding="utf-8"))
        self.instance, self.receipt, self.launch = folder, receipt, launch
        # The base's view keys and ready line as the receipt carries them; a receipt from before it did, the interim's.
        session_part = receipt.get("session") or {}
        self.base = session_part if "views" in session_part else INTERIM_BASES.get(receipt.get("base") or "", {})
        ctx.progress("session", msg=f"session {self.id}: {kind} on {folder.name}")
        ctx.log.info(f"session {self.id}  kind {kind}")
        ctx.log.info(f"launch    {launch.get('project')}:{launch.get('launch')} at {str(launch.get('commit'))[:12]} "
                     f"({launch_path})")
        ctx.log.info(f"instance  {folder}")
        ctx.log.info(f"folder    {self.folder}")
        (self.folder / "launch.json").write_bytes(launch_path.read_bytes())

        # the receipt, checked: the JDK by its build, then every cache, JDK and member file by SHA-256
        jdk_root, jdk_home, build = find_jdk(receipt, launch, p.get("jdk"))
        cache = instances._runtime_cache(ctx, machine)
        self.roots = {"instance": folder, "cache": cache, jdk_root: jdk_home}
        problems = verify(receipt, self.roots)
        self.mark("verified")
        if problems:
            raise ParamError(f"instance {folder.name} differs from its receipt in {len(problems)} file(s): "
                             + "; ".join(problems[:3]) + (" ..." if len(problems) > 3 else "")
                             + " (lab.build fresh=true makes it again)")
        self.jdk = {"major": jdk_root.split(":")[1], "build": build, "home": str(jdk_home),
                    "release": _release(jdk_home).get("JAVA_RUNTIME_VERSION")}

        # session inputs
        self.port = free_port(int(p.get("port") or 0) or None)
        self.jvm = parse_jvm(p.get("jvm") or [])
        edits = view_edits(self.base, p.get("view") or {}) + [str(e) for e in p.get("edits") or []]
        self.edits = [parse_edit(e) for e in edits]
        self.window = p.get("window") or ""
        if self.window and not re.match(r"^[0-9]+x[0-9]+$", self.window):
            raise ParamError(f"window: {self.window!r} is not WIDTHxHEIGHT")
        self.tour = self._tour() if kind in ("tour", "measure") else None
        self.debug = self._debug(p.get("debug") or "")
        self.record = self._record(kind)
        write_json(self.folder / SESSION, self.record)

    def _fresh_copy(self, root: Path, folder: Path, receipt: dict) -> Path:
        store = Store(root / ".store", instances.LAYOUT)
        if store.template(receipt["key"]) is None:
            raise ParamError(f"the template {receipt['key']} of instance {folder.name} is gone, so no fresh copy can "
                             "be made (lab.build makes it again)")
        copy = root / f"{folder.name}--{self.id}"
        self.ctx.progress("copy", msg=f"a fresh copy of template {receipt['key']}")
        store.materialize(receipt["key"], copy)
        made = json.loads((copy / instances.RECEIPT).read_text(encoding="utf-8"))
        made["name"] = copy.name
        write_json(copy / instances.RECEIPT, made)
        return copy

    def _tour(self) -> dict:
        path = Path(self.p["tour"])
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ParamError(f"tour: {path} cannot be read: {error}") from None
        views, distances = (doc, {}) if isinstance(doc, list) else (doc.get("views"), doc.get("viewDistance") or {})
        if not isinstance(views, list) or not views:
            raise ParamError(f"tour: {path.name} has no views")
        for view in views:
            if not re.match(r"^[A-Za-z0-9_-]+$", str(view.get("label", ""))):
                raise ParamError(f"tour: a view's label is letters, digits, - and _: {view!r}")
            for key in ("x", "y", "z", "yaw", "pitch"):
                float(view[key])
        rules = {**TOUR_DEFAULTS, **(self.launch.get("tour") or {})}
        if not rules["captured"]:
            raise ParamError("the launch has no tour rules (tour.captured: the client's line for a captured view); "
                             "its [launch.<name>.tour] names them")
        if distances:
            names = {"horizontal": "horizontal", "vertical": "vertical", "clientRender": "client_render"}
            extra = view_edits(self.base, {names.get(k, k): v for k, v in distances.items()})
            self.edits += [parse_edit(e) for e in extra if parse_edit(e) not in self.edits]
        return {"file": str(path), "sha256": sha256_file(path), "views": views, "rules": rules}

    def _debug(self, which: str) -> dict:
        if not which:
            return {}
        if which not in ("client", "server", "both"):
            raise ParamError(f"debug: {which!r} is none of client, server, both")
        sides = list(SIDES) if which == "both" else [which]
        ports = {side: free_port(DEBUG_PORTS[side]) for side in sides}
        attach = self.folder / "attach"
        attach.mkdir()
        configuration = {"version": "0.2.0", "configurations": [
            {"type": "java", "name": f"Attach to the {side} ({self.id})", "request": "attach", "hostName": "127.0.0.1",
             "port": port} for side, port in ports.items()]}
        write_json(attach / "launch.json", configuration)
        (attach / "jdb.txt").write_text("".join(f"jdb -connect com.sun.jdi.SocketAttach:hostname=127.0.0.1,"
                                                f"port={port}    # the {side}\n" for side, port in ports.items()),
                                        encoding="utf-8")
        return {"ports": ports, "attach": str(attach / "launch.json")}

    def _record(self, kind: str) -> dict:
        receipt, launch = self.receipt, self.launch
        return {
            "schema": SCHEMA, "id": self.id, "kind": kind, "state": "running", "started": now(), "ended": None,
            "job": self.ctx.job, "evidence": not self.not_evidence, "not_evidence": self.not_evidence,
            "instance": {"name": self.instance.name, "path": str(self.instance), "template": receipt.get("key"),
                         "receipt_sha256": sha256_file(self.instance / instances.RECEIPT)},
            "launch": {"sha256": receipt["launch"]["sha256"], "file": "launch.json",
                       **{k: launch.get(k) for k in ("project", "launch", "commit", "tree", "set")}},
            "members": dict(receipt["launch"].get("members") or {}),
            "jdk": self.jdk, "gpu": gpu(),
            "window": self.window or self._receipt_window(),
            "inputs": {"port": self.port, "jvm": self.jvm, "view": dict(self.p.get("view") or {}),
                       "edits": [f"{side}/{file}:{key}={value}" for side, file, key, value in self.edits],
                       "window": self.window or None, "debug": self.p.get("debug") or None,
                       "fresh_world": bool(self.p.get("fresh_world")), "limit": self.p.get("limit"),
                       "tour": {k: self.tour[k] for k in ("file", "sha256")} if self.tour else None,
                       "driver": self.p.get("driver") or None},
            "debug": self.debug or None,
            "commands": {}, "timings": self.timings, "exit": {}, "failure": None, "tour": None, "kept": None,
        }

    def _receipt_window(self) -> str:
        command = self.receipt["client"]["command"]
        try:
            return f"{command[command.index('--width') + 1]}x{command[command.index('--height') + 1]}"
        except (ValueError, IndexError):
            return "unknown"

    # -- the instance's own files, for this session only ----------------------------------------------------------
    def _edit(self, path: Path, change):
        if path not in self.restore:
            if path.exists() and path.stat().st_nlink > 1:
                raise ParamError(f"edits: {path.name} is shared with the template by a hard link; it is never edited")
            self.restore[path] = path.read_bytes() if path.exists() else None
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(change(text), encoding="utf-8", newline="")

    def apply_inputs(self):
        folder = self.instance
        props = folder / "server" / "server.properties"
        self._edit(props, lambda text: set_key(text, "server-port", str(self.port), props.name))
        for side, file, key, value in self.edits:
            path = folder / side / file
            self._edit(path, lambda text, k=key, v=value, n=path.name: set_key(text, k, v, n))
        if self.p.get("fresh_world"):
            world = folder / "server" / "world"
            if world.exists():
                shutil.rmtree(world)
        for side in SIDES:
            (folder / side / "java-tmp").mkdir(parents=True, exist_ok=True)

    def put_back(self):
        for path, data in self.restore.items():
            try:
                if data is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(data)
            except OSError as error:
                self.ctx.log.warning(f"could not put {path} back: {error}")

    # -- command lines ---------------------------------------------------------------------------------------------
    def argv(self, side: str) -> list[str]:
        roots = {name: str(path) for name, path in self.roots.items()}
        command = instances.expand(self.receipt[side]["command"], roots)
        command = [token.replace("{port}", str(self.port)) for token in command]
        at = self.receipt[side].get("jvm_at")
        if at is None:  # a receipt from before jvm_at: after the launch's own options when they lead
            launch_jvm = list((self.launch.get("jvm") or {}).get(side) or [])
            at = 1 + len(launch_jvm) if command[1:1 + len(launch_jvm)] == launch_jvm else 1
        extra = list(self.jvm[side])
        if side == "client" and self.tour:
            extra = list(self.tour["rules"]["client_jvm"]) + extra
        if side in (self.debug.get("ports") or {}):
            extra.append(f"-agentlib:jdwp=transport=dt_socket,server=y,suspend=n,"
                         f"address=127.0.0.1:{self.debug['ports'][side]}")
        command[at:at] = extra
        if side == "client" and self.window:
            width, height = self.window.split("x")
            for flag, value in (("--width", width), ("--height", height)):
                if flag in command:
                    command[command.index(flag) + 1] = value
        return command

    def env(self) -> dict:
        env = dict(os.environ)
        home = Path(self.jdk["home"])
        env["JAVA_HOME"] = str(home)
        env["PATH"] = str(home / "bin") + os.pathsep + env.get("PATH", "")
        return env

    def launch_side(self, side: str) -> Side:
        argv = self.argv(side)
        cwd = Path(instances.expand([self.receipt[side]["cwd"]], {n: str(r) for n, r in self.roots.items()})[0])
        self.record["commands"][side] = argv
        process = start(argv, cwd, self.env())
        started = time.monotonic()
        self.mark(f"{side}_jvm")
        console = processes.Console(process, self.folder / f"{side}-console.log")
        self.sides[side] = Side(side, process, console, started)
        self.ctx.log.info(f"{side} started (pid {process.pid}); console {self.folder / (side + '-console.log')}")
        write_json(self.folder / SESSION, self.record)
        return self.sides[side]

    def send(self, command: str):
        server = self.sides.get("server")
        if server is None or server.console.send(command) is None:
            raise Failed(f"the server is gone; could not send {command!r}", "server")
        self.ctx.log.debug(f"> {command}")

    def wait(self, side: str, pattern, timeout: float, what: str):
        """The next line of ``side`` that matches, from where the last wait on it stopped; a failure when either
        process exits first or the timeout passes."""
        regex = pattern if isinstance(pattern, re.Pattern) else re.compile(re.escape(pattern))
        target = self.sides[side]
        deadline = time.monotonic() + timeout
        while True:
            step = min(deadline, time.monotonic() + 0.5)
            for line in target.console.scan(target.position, step, self.ctx.cancelled):
                target.position = line.index + 1
                if regex.search(line.text):
                    return line
            for other in self.sides.values():
                if not other.alive():
                    raise Failed(f"the {other.name} exited {other.process.returncode} before {what}", other.name)
            if time.monotonic() >= deadline:
                raise Failed(f"no {what} within {processes.span(timeout)}", side)

    # -- kinds ----------------------------------------------------------------------------------------------------
    def play(self):
        kind = self.p["kind"]
        if kind == "driver":
            return self.drive()
        ready = re.compile(self.base.get("ready") or READY.pattern)
        self.launch_side("server")
        self.wait("server", ready, READY_TIMEOUT, "server ready line")
        self.mark("server_ready")
        if self.tour:
            first = self.tour["views"][0]
            for command in self.tour["rules"]["setup"]:
                self.send(command)
            if self.tour["rules"]["spawn"]:
                self.send(self.tour["rules"]["spawn"].format(**{k: first[k] for k in ("x", "y", "z")}))
        self.launch_side("client")
        if self.tour:
            return self.tour_views()
        return self.open_session()

    def open_session(self):
        commands = self.folder / "commands.txt"
        commands.write_text("", encoding="utf-8")
        self.ctx.log.info(f"open session: close the game to end it; console commands go in {commands}")
        limit = float(self.p.get("limit") or 4 * 3600)
        deadline = time.monotonic() + limit
        with commands.open(encoding="utf-8") as stream:
            while all(side.alive() for side in self.sides.values()):
                self.ctx.check_cancel()
                for line in stream:
                    if line.strip():
                        self.send(line.strip())
                if time.monotonic() > deadline:
                    self.ctx.finding(f"the open session reached its limit ({processes.span(limit)}); stopped")
                    break
                time.sleep(0.5)
        server = self.sides["server"]
        if not server.alive():
            raise Failed(f"the server exited {server.process.returncode} while the game was open", "server")
        self.mark("closed")

    def tour_views(self):
        rules, views = self.tour["rules"], self.tour["views"]
        player = self.receipt["player"]["name"]
        shots = self.instance / "client" / rules["screenshots"]
        shots.mkdir(parents=True, exist_ok=True)
        before = {p.name for p in shots.iterdir()}
        self.wait("server", rules["joined"], JOIN_TIMEOUT, "join of the player")
        self.mark("joined")
        if rules["spectator"]:
            self.send(f"gamemode spectator {player}")
        self.send(f"tp {player} " + _position(views[0]))
        for index, view in enumerate(views):
            for command in view.get("commands") or []:
                self.send(str(command).replace("{player}", player))
            if index:
                self._descend(player, views[index - 1], view, rules)
                self.send(f"tp {player} " + _position(view))
            self.wait("client", rules["captured"], float(rules["capture_timeout"]),
                      f"capture of view {index + 1} ({view['label']})")
            self.ctx.progress("tour", (index + 1) / len(views), f"captured view {index + 1}/{len(views)}: "
                                                                   f"{view['label']}")
            self.ctx.log.info(f"Captured view {index + 1}/{len(views)}: {view['label']}")
        self.mark("captured")
        for command in rules["leave"]:
            self.send(command.replace("{player}", player))
        time.sleep(float(rules["leave_seconds"]))
        taken = sorted((p for p in shots.iterdir() if p.name not in before), key=lambda p: p.name)
        out = self.folder / "tour"
        out.mkdir()
        filed = []
        for index, (view, shot) in enumerate(zip(views, taken)):
            target = out / f"{index + 1:02d}-{view['label']}.png"
            shutil.move(str(shot), str(target))
            filed.append({"view": view["label"], "file": target.name, "sha256": sha256_file(target)})
        write_json(out / "views.json", views)
        self.record["tour"] = {"views": len(views), "captured": len(filed), "folder": str(out), "files": filed}
        if len(filed) != len(views):
            raise Failed(f"{len(views)} views were captured but {len(filed)} new screenshot(s) were found in "
                         f"{shots}", "client")
        self.ctx.log.info(f"Tour captures: {out}")

    @staticmethod
    def _descend_steps(previous: dict, view: dict, step: float) -> list[float]:
        dy = float(view["y"]) - float(previous["y"])
        if abs(dy) <= 2 * step:
            return []
        y, out = float(previous["y"]), []
        while abs(float(view["y"]) - y) > step:
            y += step if dy > 0 else -step
            out.append(y)
        return out

    def _descend(self, player: str, previous: dict, view: dict, rules: dict):
        for y in self._descend_steps(previous, view, float(rules["step"])):
            self.send(f"tp {player} {view['x']} {y:g} {view['z']} {view['yaw']} {view['pitch']}")
            time.sleep(float(rules["pause"]))

    def drive(self):
        driver = self.launch["drivers"][self.p["driver"]]
        command = driver.get("command") if isinstance(driver, dict) else None
        if not isinstance(command, list) or not command:
            raise ParamError(f"driver {self.p['driver']}: the launch gives it no command (a list)")
        values = {"python": sys.executable, "instance": str(self.instance), "session": str(self.folder),
                  "session_id": self.id,
                  "launch": str(self.folder / "launch.json"), "instance_json": str(self.instance / instances.RECEIPT),
                  "root": str(self.p.get("driver_root") or self.instance)}
        argv = [instances._fill_known(str(item), values) for item in command]
        env = {**self.env(), **self.driver_env()}
        cwd = Path(instances._fill_known(str(driver.get("cwd") or "{root}"), values))
        self.record["commands"]["driver"] = argv
        self.ctx.progress("driver", msg=" ".join(argv[:3]))
        limit = float(self.p.get("limit") or 4 * 3600)
        self.mark("driver_start")
        done = processes.run(self.ctx, argv, cwd=cwd, timeout=limit, env=env)
        (self.folder / "driver.log").write_text(done.output, encoding="utf-8")
        self.record["exit"]["driver"] = done.code
        if done.code is None:
            raise Failed(f"the driver did not end within {processes.span(limit)}; stopped", "driver")
        if done.code != 0:
            raise Failed(f"the driver exited {done.code}", "driver")

    def driver_env(self) -> dict[str, str]:
        values = {"token": os.environ.get("WB_TURN_TOKEN", ""), "lane": os.environ.get("WB_TURN_LANE", ""),
                  "lease": os.environ.get("WB_LEASE_FILE", ""), "coordination": os.environ.get("WB_COORDINATION_DIR", ""),
                  "instance": str(self.instance / instances.RECEIPT), "session": str(self.folder),
                  "launch": str(self.folder / "launch.json"), "cache": str(self.roots["cache"])}
        out = {name: values[key] for key, name in ENV.items()}
        for name, key in (machines.lab(self.ctx)["env"] or {}).items():
            out[name] = values[key]
        return out

    # -- ending ---------------------------------------------------------------------------------------------------
    def stop(self):
        server, client = self.sides.get("server"), self.sides.get("client")
        record = {"how": None, "seconds": None}
        if server is not None:
            began = time.monotonic()
            if server.alive():
                server.console.send("stop")
                grace = max(STOP_GRACE, float(self.p.get("grace") or 0))
                try:
                    server.process.wait(timeout=grace)
                    record["how"] = "stop"
                except subprocess.TimeoutExpired:
                    record["how"] = "killed"
                    self.ctx.finding(f"the server did not stop within {processes.span(grace)} of stop; killed")
            else:
                record["how"] = "exited"
            processes.kill_tree(server.process)
            record["seconds"] = round(time.monotonic() - began, 3)
            self.record["exit"]["server"] = server.process.returncode
        if client is not None:
            if client.alive():
                processes.kill_tree(client.process)  # a tour's client, or one left after the server went
                self.record["exit"]["client_stopped"] = True
            self.record["exit"]["client"] = client.process.returncode
        for side in self.sides.values():
            side.console.close()
        self.record["stop"] = record
        self.mark("stopped")

    def explain(self, failure: Failed) -> dict:
        since = datetime.fromisoformat(self.record["started"]).timestamp() - 2
        reports = []
        for side in SIDES:
            home = self.instance / side
            for pattern in ("crash-reports/crash-*.txt", "hs_err_pid*.log"):
                reports += [p for p in home.glob(pattern) if p.stat().st_mtime >= since]
        reports.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        kept = []
        if reports:
            crashes = self.folder / "crash-reports"
            crashes.mkdir(exist_ok=True)
            for path in reports:
                shutil.copy2(path, crashes / path.name)
                kept.append(crashes / path.name)
        texts = [p.read_text(encoding="utf-8", errors="replace") for p in kept]
        order = [failure.where] + [s for s in SIDES if s != failure.where]
        for side in order:
            log = self.folder / f"{side}-console.log"
            if log.is_file():
                texts.append(log.read_text(encoding="utf-8", errors="replace"))
        if (self.folder / "driver.log").is_file():
            texts.append((self.folder / "driver.log").read_text(encoding="utf-8", errors="replace"))
        from .commands import explain as first_line
        line = first_line(list(self.launch.get("failures") or []) + list(FAILURES), texts)
        target = self.p.get("instance")
        return {"summary": str(failure), "where": failure.where, "line": line,
                "crash_report": str(kept[0]) if kept else None,
                "next": (f"open {kept[0]}" if kept else f"read {self.folder / ((failure.where or 'server') + '-console.log')}")
                        + f"; then wb play --from {self.folder / 'launch.json'} --name {target} --debug=client"}

    def close(self, outcome: str):
        self.record.update(state=outcome, ended=now(), timings=self.timings)
        if self.failure is not None:
            self.record["failure"] = self.explain(self.failure)
        write_json(self.folder / SESSION, self.record)
        shutil.copy2(self.folder / SESSION, self.ctx.out / SESSION)
        self.ctx.output("session", self.ctx.out / SESSION)


def _position(view: dict) -> str:
    return " ".join(str(view[k]) for k in ("x", "y", "z", "yaw", "pitch"))


def run(ctx: Context, instance: str, kind: str = "open", tour: str | None = None, launch: str | None = None,
        copy: bool = True, driver: str = "", driver_root: str | None = None, port: int = 0, jvm: list | None = None,
        view: dict | None = None, edits: list | None = None, window: str = "", debug: str = "",
        fresh_world: bool = False, limit: float = 4 * 3600, jdk: str | None = None, session: str = "",
        no_rebuild: bool = False):
    params = {k: v for k, v in locals().items() if k != "ctx"}
    session_ = Session(ctx, params)
    session_.open()
    outcome = "ended"
    try:
        session_.apply_inputs()
        session_.play()
    except Failed as failure:
        session_.failure, outcome = failure, "failed"
    except Cancelled:
        outcome = "cancelled"
        raise
    except BaseException:
        outcome = "failed"
        raise
    finally:
        try:
            session_.stop()
        finally:
            session_.put_back()
            session_.close(outcome)
    record = session_.record
    if session_.failure is not None:
        failure = record["failure"]
        ctx.finding(f"session {session_.id} failed: {failure['summary']}"
                    + (f": {failure['line']}" if failure["line"] else ""))
        ctx.reason("session", "failure", failure["line"] or failure["summary"], failure["next"])
    elif kind in ("tour", "measure"):
        ctx.summary(f"session {session_.id}: tour {Path(tour).name} on {session_.instance.name}, "
                    f"{record['tour']['captured']} of {record['tour']['views']} views captured")
    else:
        ctx.summary(f"session {session_.id}: {kind} on {session_.instance.name} ended")
    ctx.measure("jvm_start_s", record["timings"].get("server_jvm") or record["timings"].get("driver_start"), unit="s")
    return {"session": session_.id, "folder": str(session_.folder), "instance": str(session_.instance),
            "evidence": record["evidence"], "state": record["state"]}


# --- kept sessions ---------------------------------------------------------------------------------------------------


def keep(ctx: Context, session: str, reason: str):
    """Keeps a session: its folder, its instance, its template and its launch file are never removed by retention."""
    if not ID.match(session):
        raise ParamError(f"session: {session!r} is not a session id")
    if not reason.strip():
        raise ParamError("reason: say why the session is kept")
    root = sessions_root(ctx)
    path = root / session / SESSION
    if not path.is_file():
        raise ParamError(f"no session {session} in {root}")
    record = json.loads(path.read_text(encoding="utf-8"))
    record["kept"] = {"reason": reason.strip(), "at": now(), "job": ctx.job}
    write_json(path, record)
    # kept.json is what marks a kept session for retention: the session and what it names
    write_json(path.parent / KEPT, {**record["kept"], "session": session, "instance": record["instance"]["name"],
                                    "template": record["instance"]["template"],
                                    "launch": record["launch"]["sha256"]})
    ctx.summary(f"session {session} kept: {reason.strip()}")
    return {"session": session, "kept": record["kept"], "roots": kept_roots(root)}


def kept_roots(root: Path) -> dict[str, list[str]]:
    """What kept sessions name, which retention never removes: sessions, instances, templates and launch files."""
    out = {"sessions": [], "instances": [], "templates": [], "launches": []}
    for path in sorted(root.glob(f"*/{KEPT}")) if root.is_dir() else []:
        try:
            kept = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out["sessions"].append(kept["session"])
        out["instances"].append(kept["instance"])
        out["templates"].append(kept["template"])
        out["launches"].append(kept["launch"])
    return {key: sorted(set(values)) for key, values in out.items()}
