"""lab.server and lab.server-scenario: headless dedicated-server sessions. They need no GPU and no host, so they
run in the container.

A session copies a stopped server folder (its JARs, mods, config and world) into the job's work folder, so the
source is never written, lays any added files and mods over the copy, gives the copy a free port unless the
request names one, and launches it with ``launch`` (``java -jar <jar> nogui`` unless the request gives another
command line). It reads the console line by line and

1. waits until each ``ready`` pattern has matched a line (by default the vanilla ``Done (...)! For help`` line);
2. sends ``commands`` in order, each optionally waiting for a reply that matches its ``expect``;
3. lets the server run until the first of: ``ticks`` game ticks have passed (the server's own game time, read
   with ``tick_query``), ``seconds`` have passed, or a line matches ``until`` (a probe's last line, or the
   server's own ``Stopping server`` when the probe stops it);
4. stops it as an operator would: ``save-all`` first when ``save`` is set, then ``stop``. A server still running
   ``grace`` later is killed, and that is a finding.

What the run leaves is kept under the out folder: ``console.log`` (written as it is read), the crash reports the
run wrote (``crash-reports/``, and JVM ``hs_err`` logs), what ``keep`` names (under ``files/``), every input file
by SHA-256 (``inventory.json``) and ``receipt.json``: the inputs by SHA-256, the JVM and its arguments, the times,
the exit code and every line matched. The copy itself is then removed.

``lab.server-scenario`` is the same session with a probe: the run must write JSON where ``probe`` (a glob) says,
it must parse, and it must hold the values ``require`` names; anything else is a finding.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

from wb_core import types
from wb_core.module import Cancelled, Context, EnvironmentProblem, ParamError, sha256_file

from . import process as processes
from .store import remove_tree

SCHEMA = "wb.lab.server/1"
SAVED = re.compile(r"Saved the (game|world)")  # save-all's reply, old and new
PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")
IN_LAUNCH = {"java", "jar", "jvm_args", "server", "port", "ticks"}
IN_VALUES = {"server", "port", "ticks"}  # in jvm_args and properties
TICKS_PER_SECOND = 20  # the game's own rate; it only paces the game-time checks
CANCEL_STOP = 30.0     # a cancelled session waits at most this long for stop before killing
JSON_IN_DATA = 64 * 1024
NEVER = threading.Event()


# -- the request ------------------------------------------------------------------------------------
@dataclass
class Step:
    send: str
    expect: re.Pattern | None = None
    timeout: float | None = None


@dataclass
class Options:
    """A session's parameters, checked: patterns compiled, globs relative, placeholders known."""
    server: Path
    jdk: str
    jar: str
    jvm_args: list[str]
    launch: list[str]
    exclude: list[str]
    files: Path | None
    mods: list[Path]
    properties: dict[str, str]
    ready: list[re.Pattern]
    ready_timeout: float
    commands: list[Step]
    reply_timeout: float
    ticks: int
    seconds: float
    until: list[re.Pattern]
    run_timeout: float
    tick_query: str
    tick_reply: re.Pattern
    save: bool
    stop: list[str]
    grace: float
    keep: list[str]

    @property
    def runs(self) -> bool:
        """Whether the server runs on after the commands (else it is stopped right after them)."""
        return bool(self.ticks or self.seconds or self.until)

    @classmethod
    def parse(cls, params: dict) -> Options:
        server = Path(params["server"])
        if not server.is_dir():
            raise ParamError(f"server: {params['server']} is not a folder")
        files = Path(params["files"]) if params.get("files") else None
        if files is not None and not files.is_dir():
            raise ParamError(f"files: {params['files']} is not a folder")
        mods = [Path(m) for m in params["mods"]]
        for mod in mods:
            if not mod.is_file():
                raise ParamError(f"mods: {mod} is not a file")
        launch = [str(item) for item in params["launch"]]
        if not launch:
            raise ParamError("launch: the server's command line is empty")
        _placeholders("launch", launch, IN_LAUNCH)
        _placeholders("jvm_args", params["jvm_args"], IN_VALUES)
        if any("{jvm_args}" in item and item != "{jvm_args}" for item in launch):
            raise ParamError("launch: {jvm_args} must be an item of its own")
        if not params["jdk"] and any("{java}" in item for item in launch):
            raise ParamError("launch uses {java}, so it needs a jdk")
        for name in ("jvm_args", "jar"):
            if params[name] and not any("{" + name + "}" in item for item in launch):
                raise ParamError(f"{name} is given, but launch has no {{{name}}} to put it in")
        properties = {}
        for key, value in params["properties"].items():
            if isinstance(value, (dict, list)) or not str(key).strip():
                raise ParamError(f"properties: {key!r} needs a plain value")
            properties[str(key)] = "" if value is None else str(value).lower() if isinstance(value, bool) else str(value)
        _placeholders("properties", properties.values(), IN_VALUES)
        tick_reply = _regex("tick_reply", params["tick_reply"])
        if params["ticks"] and (tick_reply.groups < 1 or not params["tick_query"].strip()):
            raise ParamError("ticks are counted by the game time: tick_query must ask for it and tick_reply's group 1 "
                             "must capture it")
        if not [c for c in params["stop"] if c.strip()]:
            raise ParamError("stop: name the console command that stops the server")
        for name in ("exclude", "keep"):
            for pattern in params[name]:
                _relative_glob(name, pattern)
        if params["jar"]:
            _relative_glob("jar", params["jar"])
        return cls(server=server, jdk=params["jdk"], jar=params["jar"], jvm_args=[str(a) for a in params["jvm_args"]],
                   launch=launch, exclude=list(params["exclude"]), files=files, mods=mods, properties=properties,
                   ready=[_regex("ready", p) for p in params["ready"]], ready_timeout=params["ready_timeout"],
                   commands=[_step(i, c) for i, c in enumerate(params["commands"])],
                   reply_timeout=params["reply_timeout"], ticks=params["ticks"], seconds=params["seconds"],
                   until=[_regex("until", p) for p in params["until"]], run_timeout=params["run_timeout"],
                   tick_query=params["tick_query"], tick_reply=tick_reply, save=params["save"],
                   stop=[c for c in params["stop"] if c.strip()], grace=params["grace"], keep=list(params["keep"]))


def _regex(name: str, text: str) -> re.Pattern:
    try:
        return re.compile(text)
    except re.error as error:
        raise ParamError(f"{name}: {text!r} is not a regular expression: {error}") from None


def _step(index: int, item) -> Step:
    where = f"commands[{index}]"
    if isinstance(item, str):
        item = {"send": item}
    unknown = set(item) - {"send", "expect", "timeout"} if isinstance(item, dict) else set()
    if unknown:
        raise ParamError(f"{where}: unknown key(s) {', '.join(sorted(unknown))} (known: send, expect, timeout)")
    if not isinstance(item, dict) or not isinstance(item.get("send"), str) or not item["send"].strip():
        raise ParamError(f"{where}: a command is a string, or {{send, expect, timeout}}")
    expect = _regex(f"{where}.expect", item["expect"]) if item.get("expect") else None
    timeout = None
    if item.get("timeout") is not None:
        try:
            timeout = types.parse_duration(item["timeout"])
        except ValueError as error:
            raise ParamError(f"{where}.timeout: {error}") from None
    return Step(item["send"], expect, timeout)


def _placeholders(name: str, items, allowed: set[str]):
    for item in items:
        for key in PLACEHOLDER.findall(str(item)):
            if key not in allowed:
                known = ", ".join("{" + k + "}" for k in sorted(allowed))
                raise ParamError(f"{name}: unknown placeholder {{{key}}} in {item!r} (known here: {known})")


def _relative_glob(name: str, pattern: str):
    posix = str(pattern).replace("\\", "/").strip()
    if not posix or posix in (".", "./") or posix.startswith("/") or re.match(r"^[A-Za-z]:", posix) \
            or ".." in PurePosixPath(posix).parts:
        raise ParamError(f"{name}: {pattern!r} must be a glob relative to the server folder")


def _fill(text: str, values: dict[str, str]) -> str:
    return PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), text)


def _matches(relative: str, pattern: str) -> bool:
    return PurePosixPath(relative).full_match(pattern.replace("\\", "/").rstrip("/") or pattern)


# -- files ------------------------------------------------------------------------------------------
def _copy_file(source: Path, target: Path) -> tuple[str, int]:
    """Copies one file (writable in the copy, its times and modes kept) and returns its SHA-256 and size."""
    digest, size = hashlib.sha256(), 0
    with open(source, "rb") as reader, open(target, "wb") as writer:
        while block := reader.read(1 << 20):
            digest.update(block)
            writer.write(block)
            size += len(block)
    info = os.stat(source)
    os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns))
    os.chmod(target, stat.S_IMODE(info.st_mode) | stat.S_IRUSR | stat.S_IWUSR)
    return digest.hexdigest(), size


def copy_tree(ctx: Context, source: Path, target: Path, exclude: list[str]) -> tuple[dict[str, dict], list[str]]:
    """Copies ``source`` into ``target``, leaving out what an ``exclude`` glob matches (a folder with all it holds).
    Returns every copied file's SHA-256 and size by relative path, and what was left out."""
    copied: dict[str, dict] = {}
    left_out: list[str] = []
    seen: set[str] = set()
    for folder, dirs, names in os.walk(source, followlinks=True):
        ctx.check_cancel()
        real = os.path.realpath(folder)
        if real in seen:  # a link back up the tree
            dirs[:] = []
            continue
        seen.add(real)
        here = Path(folder).relative_to(source)
        base = "" if here == Path(".") else here.as_posix() + "/"
        (target / here).mkdir(parents=True, exist_ok=True)
        kept = []
        for name in sorted(dirs):
            if any(_matches(base + name, p) for p in exclude):
                left_out.append(base + name + "/")
            else:
                kept.append(name)
        dirs[:] = kept
        for name in sorted(names):
            relative = base + name
            if any(_matches(relative, p) for p in exclude):
                left_out.append(relative)
                continue
            digest, size = _copy_file(Path(folder) / name, target / relative)
            copied[relative] = {"sha256": digest, "bytes": size}
    return copied, left_out


def tree_digest(root: Path, files: dict[str, dict]) -> str:
    """The digest the core gives a folder input (wb_core.module.sha256_tree), from digests already taken."""
    digest = hashlib.sha256()
    for relative in sorted(files, key=lambda r: root / r):
        digest.update(f"{relative}\0{files[relative]['sha256']}\n".encode())
    return digest.hexdigest()


def _entries(root: Path) -> tuple[list[str], list[str]]:
    """Every file and every folder under ``root``, as relative paths."""
    files, folders = [], []
    for folder, dirs, names in os.walk(root):
        here = Path(folder).relative_to(root)
        base = "" if here == Path(".") else here.as_posix() + "/"
        folders += [base + d for d in dirs]
        files += [base + n for n in names]
    return sorted(files), sorted(folders)


def _place(source: Path, target: Path, move: bool):
    if target.is_dir() and not target.is_symlink():
        remove_tree(target)
    elif target.exists():
        target.unlink()
    target.parent.mkdir(parents=True, exist_ok=True)
    if move:
        shutil.move(str(source), str(target))
    elif source.is_dir():
        shutil.copytree(source, target)
    else:
        shutil.copy2(source, target)


def _escape(text: str, key: bool) -> str:
    """A value as java.util.Properties writes it (server.properties is read with Properties.load)."""
    out = []
    for index, char in enumerate(text):
        code = ord(char)
        if char == "\\":
            out.append("\\\\")
        elif char in "\t\n\r\f":
            out.append({"\t": "\\t", "\n": "\\n", "\r": "\\r", "\f": "\\f"}[char])
        elif char in "=:#!":
            out.append("\\" + char)
        elif char == " " and (key or index == 0):
            out.append("\\ ")
        elif code < 0x20 or code > 0x7E:
            units = char.encode("utf-16-be")
            out += [f"\\u{int.from_bytes(units[i:i + 2], 'big'):04x}" for i in range(0, len(units), 2)]
        else:
            out.append(char)
    return "".join(out)


def _property_key(line: str) -> str | None:
    stripped = line.lstrip()
    if not stripped or stripped[0] in "#!":
        return None
    match = re.match(r"(?:\\.|[^\\=:\s])+", stripped)
    return re.sub(r"\\(.)", r"\1", match.group(0)) if match else None


def set_properties(path: Path, values: dict[str, str]):
    """Sets ``values`` in a properties file (made if missing); other lines stay as they are."""
    lines = path.read_text(encoding="latin-1").splitlines() if path.is_file() else []
    pending = dict(values)
    out = []
    for line in lines:
        key = _property_key(line)
        out.append(f"{_escape(key, True)}={_escape(pending.pop(key), False)}" if key in pending else line)
    out += [f"{_escape(key, True)}={_escape(value, False)}" for key, value in pending.items()]
    path.write_text("\n".join(out) + "\n", encoding="latin-1")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("", 0))
        return probe.getsockname()[1]


def _release(home: Path) -> dict[str, str]:
    info = {}
    release = home / "release"
    if release.is_file():
        for line in release.read_text(encoding="utf-8", errors="replace").splitlines():
            key, sep, value = line.partition("=")
            if sep:
                info[key.strip()] = value.strip().strip('"')
    return info


_MISSING = object()


def _lookup(data, dotted: str):
    for part in dotted.split("."):
        if isinstance(data, dict) and part in data:
            data = data[part]
        elif isinstance(data, list) and part.isdigit() and int(part) < len(data):
            data = data[int(part)]
        else:
            return _MISSING
    return data


def _same(a, b) -> bool:
    """JSON equality: true is not 1."""
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    return type(a) is type(b) and a == b


# -- wording ----------------------------------------------------------------------------------------
_span = processes.span


def _exited(code) -> str:
    return f"the server exited {code}" if code is not None else "the server closed its console"


def _patterns(patterns: list[re.Pattern]) -> str:
    return ", ".join(f"/{p.pattern}/" for p in patterns)


@contextmanager
def _no_interrupt():
    """Cancelling a job sets ctx.cancelled and interrupts the worker's main thread. A session polls ctx.cancelled at
    every wait instead, so the interrupt can never land between sending stop and writing the receipt."""
    try:
        previous = signal.signal(signal.SIGINT, lambda *_: None)
    except ValueError:  # not the main thread, which is the only one an interrupt reaches
        previous = None
    try:
        yield
    finally:
        if previous is not None:
            signal.signal(signal.SIGINT, previous)


# -- the session ------------------------------------------------------------------------------------
class Session:
    def __init__(self, ctx: Context, options: Options, probe: str | None = None, require: dict | None = None):
        self.ctx, self.o = ctx, options
        self.probe, self.require = probe, dict(require or {})
        self.copy = ctx.work / "server"
        self.copied: dict[str, dict] = {}      # the server's files, as copied
        self.before: dict[str, dict] = {}      # the copy as launched: server files, added files and mods
        self.added: dict[str, dict] = {}
        self.receipt: dict = {"schema": SCHEMA, "command": ctx.command, "job": ctx.job}
        self.process: subprocess.Popen | None = None
        self.console: processes.Console | None = None
        self.launched: datetime | None = None
        self.jvm: dict | None = None
        self.java_home: Path | None = None
        self.argv: list[str] = []
        self.ready_at: float | None = None
        self.ready_index = 0
        self.ended: str | None = None
        self.ticks_run: int | None = None
        self.stopped = "never started"
        self.exit: int | None = None
        self.reported_exit = False
        self.kept: list[dict] = []
        self.crash_reports: list[str] = []
        self.probes: list[dict] = []
        self.problems: list[str] = []
        self.files_cache: tuple[list[str], list[str]] | None = None

    # the parts of a run --------------------------------------------------------------------------
    def execute(self) -> dict:
        cancelled = False
        with _no_interrupt():
            try:
                self._prepare()
                self._launch()
                if self._await_ready() and self._send_commands():
                    self._run()
            except Cancelled:
                cancelled = True
            finally:
                try:
                    self._stop(cancelled)
                finally:
                    self._collect()
        if cancelled or self.ctx.cancelled.is_set():
            raise Cancelled()
        return self._result()

    def _finding(self, text: str, exit: bool = False):
        self.ctx.finding(text)
        self.reported_exit = self.reported_exit or exit

    def _prepare(self):
        ctx, o = self.ctx, self.o
        if ctx.out.resolve().is_relative_to(o.server.resolve()):
            raise ParamError("the out folder is inside the server folder, which a session never writes")
        java = None
        if o.jdk:
            home = ctx.jdk(o.jdk)
            java = home / "bin" / ("java.exe" if os.name == "nt" else "java")
            if not java.is_file():
                raise EnvironmentProblem(f"JDK {o.jdk} at {home} has no {java.name}")
            release = _release(home)
            self.java_home = home
            self.jvm = {"jdk": o.jdk, "home": ctx.host_path(home), "java": ctx.host_path(java),
                        "runtime": release.get("JAVA_RUNTIME_VERSION") or release.get("JAVA_VERSION"),
                        "vendor": release.get("IMPLEMENTOR")}
        ctx.progress("copy", msg=f"{o.server.name} into the job's work folder")
        if self.copy.exists():
            remove_tree(self.copy)
        self.copied, left_out = copy_tree(ctx, o.server, self.copy, o.exclude)
        self.receipt["server"] = {"source": ctx.host_path(o.server), "sha256": tree_digest(self.copy, self.copied),
                                  "files": len(self.copied), "bytes": sum(f["bytes"] for f in self.copied.values()),
                                  "exclude": o.exclude, "left_out": left_out}
        self.before = dict(self.copied)
        self._lay_over()
        port = self._port()
        values = {"server": str(self.copy), "port": str(port), "ticks": str(o.ticks)}
        properties = {key: _fill(value, values) for key, value in o.properties.items()}
        properties.setdefault("server-port", str(port))
        set_properties(self.copy / "server.properties", properties)
        self.receipt["properties"] = {"file": "server.properties", "set": properties}
        if java is not None:
            values["java"] = str(java)
        if any("{jar}" in item for item in o.launch):
            values["jar"] = self._jar()
        self.argv = []
        for item in o.launch:
            self.argv += [_fill(a, values) for a in o.jvm_args] if item == "{jvm_args}" else [_fill(item, values)]
        self.receipt["jars"] = {rel: f["sha256"] for rel, f in sorted(self.before.items())
                                if rel.lower().endswith(".jar")}
        self.receipt["jvm"] = self.jvm
        self.receipt["launch"] = self.argv

    def _lay_over(self):
        """The added files: a folder laid over the copy, then mods into its mods folder."""
        ctx, o = self.ctx, self.o
        if o.files is not None:
            files, _ = copy_tree(ctx, o.files, self.copy, [])
            for relative, entry in files.items():
                self.added[relative] = dict(entry, source=ctx.host_path(o.files / relative))
        for mod in o.mods:
            relative = f"mods/{mod.name}"
            (self.copy / "mods").mkdir(exist_ok=True)
            digest, size = _copy_file(mod, self.copy / relative)
            self.added[relative] = {"sha256": digest, "bytes": size, "source": ctx.host_path(mod)}
        replaced = sorted(set(self.added) & set(self.copied))
        self.before.update({rel: {"sha256": e["sha256"], "bytes": e["bytes"]} for rel, e in self.added.items()})
        if self.added:
            self.receipt["added"] = self.added
            self.receipt["replaced"] = replaced

    def _port(self) -> int:
        given = self.o.properties.get("server-port")
        if given is None or "{port}" in given:
            return _free_port()
        if not given.strip().isdigit():
            raise ParamError(f"properties: server-port {given!r} is not a port")
        return int(given)

    def _jar(self) -> str:
        files = sorted(self.before)  # every file of the copy as it will launch
        if self.o.jar:
            found = [f for f in files if _matches(f, self.o.jar)]
            if len(found) != 1:
                raise ParamError(f"jar: {self.o.jar!r} matches {len(found)} files in the server folder"
                                 + (f" ({', '.join(found[:5])})" if found else ""))
        else:
            found = [f for f in files if "/" not in f and f.lower().endswith(".jar")]
            if len(found) != 1:
                raise ParamError(f"the server folder has {len(found)} JARs at its top level"
                                 + (f" ({', '.join(found[:5])})" if found else "")
                                 + "; name the server JAR with jar, or give a launch")
        return found[0]

    def _launch(self):
        env = dict(os.environ)
        if self.java_home is not None:
            env["JAVA_HOME"] = str(self.java_home)
            env["PATH"] = str(self.java_home / "bin") + os.pathsep + env.get("PATH", "")
        self.ctx.progress("ready", msg="launching " + " ".join([Path(self.argv[0]).name, *self.argv[1:4]]))
        self.launched = datetime.now(UTC)
        process = processes.start(self.argv, cwd=self.copy, env=env, stdin=True, owned=True)
        try:
            self.console = processes.Console(process, self.ctx.out / "console.log")
        except BaseException:
            processes.kill_tree(process)
            raise
        self.process, self.stopped = process, None
        self.ctx.log.info(f"server console: {self.ctx.host_path(self.ctx.out / 'console.log')}")

    def _gone(self) -> bool:
        return self.process.poll() is not None or self.console.closed

    def _exit_code(self) -> int | None:
        try:
            return self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            return None

    def _await_ready(self) -> bool:
        o, console = self.o, self.console
        pending, matched = list(o.ready), []
        self.receipt["ready"] = {"patterns": [p.pattern for p in o.ready], "timeout": o.ready_timeout,
                                 "matched": matched}
        if pending:
            for line in console.scan(0, console.opened + o.ready_timeout, self.ctx.cancelled):
                for pattern in [p for p in pending if p.search(line.text)]:
                    matched.append({"pattern": pattern.pattern, "line": line.text, "at": line.at})
                    pending.remove(pattern)
                self.ready_index = line.index + 1
                if not pending:
                    break
        if pending:
            if self._gone():
                self._finding(f"{_exited(self._exit_code())} before it was ready: {console.tail()}", exit=True)
            else:
                self._finding(f"the server was not ready within {_span(o.ready_timeout)}: no line matched "
                              + _patterns(pending))
            return False
        self.ready_at = matched[-1]["at"] if matched else console.now()
        self.ctx.measure("ready_seconds", self.ready_at, unit="s")
        self.ctx.log.info(f"server ready in {_span(self.ready_at)}")
        return True

    def _send_commands(self) -> bool:
        records: list[dict] = []
        self.receipt["commands"] = records
        if self.o.commands:
            self.ctx.progress("commands", msg=f"{len(self.o.commands)} console command(s)")
        for step in self.o.commands:
            self.ctx.check_cancel()
            record = {"send": step.send, "expect": step.expect.pattern if step.expect else None,
                      "at": self.console.now(), "line": None}
            records.append(record)
            index = self.console.send(step.send)
            if index is None:
                self._finding(f"{_exited(self._exit_code())} before {step.send!r} could be sent", exit=True)
                return False
            if step.expect is None:
                continue
            timeout = step.timeout if step.timeout is not None else self.o.reply_timeout
            hit = self.console.first([step.expect], index, time.monotonic() + timeout, self.ctx.cancelled)
            if hit is None:
                if self._gone():
                    self._finding(f"{_exited(self._exit_code())} before replying to {step.send!r}", exit=True)
                else:
                    self._finding(f"no reply to {step.send!r} matched /{step.expect.pattern}/ within {_span(timeout)}")
                return False
            record.update(line=hit[1].text, reply_at=hit[1].at)
        return True

    def _game_time(self) -> int | None:
        index = self.console.send(self.o.tick_query)
        if index is None:
            return None
        hit = self.console.first([self.o.tick_reply], index, time.monotonic() + self.o.reply_timeout,
                                 self.ctx.cancelled)
        try:
            return int(hit[2].group(1)) if hit else None
        except (TypeError, ValueError):
            return None

    def _no_game_time(self):
        if self._gone():
            self._finding(f"{_exited(self._exit_code())} before its game time could be read", exit=True)
        else:
            self._finding(f"no reply to {self.o.tick_query!r} matched /{self.o.tick_reply.pattern}/ with a number "
                          f"within {_span(self.o.reply_timeout)}, so the ticks cannot be counted")

    def _run(self):
        ctx, o, console = self.ctx, self.o, self.console
        record: dict = {"ticks": o.ticks or None, "seconds": o.seconds or None,
                        "until": [p.pattern for p in o.until], "ended": None}
        self.receipt["run"] = record
        if not o.runs:
            record["ended"] = self.ended = "commands"
            return
        wanted = [f"{o.ticks} ticks" if o.ticks else "", _span(o.seconds) if o.seconds else "",
                  "a line matching " + _patterns(o.until) if o.until else ""]
        ctx.progress("run", msg=" or ".join(w for w in wanted if w))
        began = time.monotonic()
        deadline = began + (o.seconds or o.run_timeout)
        target = next_check = None
        if o.ticks:
            first = self._game_time()
            if first is None:
                self._no_game_time()
                record["ended"] = self.ended = "no game time"
                return
            record["game_time"] = {"start": first, "end": first, "checks": 1}
            target = first + o.ticks
            next_check = time.monotonic() + min(o.ticks / TICKS_PER_SECOND, 10)
        position, ended = self.ready_index, None
        while ended is None:
            for line in console.scan(position, min(deadline, next_check or deadline), ctx.cancelled):
                position = line.index + 1
                if any(p.search(line.text) for p in o.until):
                    record["line"] = {"line": line.text, "at": line.at}
                    ended = "until"
                    break
            if ended:
                break
            if self._gone():
                ended = "exited"
            elif target is not None and time.monotonic() >= next_check:
                now = self._game_time()
                if now is None:
                    if not self._gone():
                        self._no_game_time()
                        ended = "no game time"
                    else:
                        ended = "exited"
                    break
                record["game_time"].update(end=now, checks=record["game_time"]["checks"] + 1)
                if now >= target:
                    ended = "ticks"
                else:
                    next_check = time.monotonic() + max(0.05, min((target - now) / TICKS_PER_SECOND, 10))
            if ended is None and time.monotonic() >= deadline:
                ended = "seconds" if o.seconds else "timeout"
        elapsed = time.monotonic() - began
        record.update(ended=ended, seconds_run=round(elapsed, 3))
        self.ended = ended
        if "game_time" in record:
            self.ticks_run = record["game_time"]["end"] - record["game_time"]["start"]
            record["ticks_run"] = self.ticks_run
            ctx.measure("ticks", self.ticks_run)
            if elapsed > 0 and record["game_time"]["checks"] > 1:
                ctx.measure("tps", round(self.ticks_run / elapsed, 2), unit="ticks/s")
        ctx.measure("run_seconds", round(elapsed, 3), unit="s")
        counted = f", {self.ticks_run} of {o.ticks} ticks" if o.ticks and self.ticks_run is not None else ""
        if ended == "exited":
            self._finding(f"{_exited(self._exit_code())} during the run ({_span(elapsed)} in{counted})", exit=True)
        elif ended == "timeout":
            self._finding(f"the run did not reach {' or '.join(w for w in wanted if w)} within "
                          f"{_span(o.run_timeout)}" + (f" ({counted[2:]})" if counted else ""))

    def _stop(self, cancelled: bool):
        if self.process is None:
            return
        ctx, o, console = self.ctx, self.o, self.console
        record: dict = {"commands": [], "saved": None, "grace": o.grace, "how": None, "sent_at": None}
        self.receipt["stop"] = record
        began = time.monotonic()
        if not self._gone():
            quick = cancelled or ctx.cancelled.is_set()
            grace = min(o.grace, CANCEL_STOP) if quick else o.grace
            ctx.progress("stop", msg=" then ".join((["save-all"] if o.save and not quick else []) + o.stop))
            record["sent_at"] = console.now()
            if o.save and not quick:
                index = console.send("save-all")
                record["commands"].append("save-all")
                hit = console.first([SAVED], index, time.monotonic() + o.reply_timeout, NEVER) \
                    if index is not None else None
                if hit:
                    record["saved"] = {"line": hit[1].text, "at": hit[1].at}
                else:
                    ctx.log.warning(f"save-all was not confirmed (no line matched /{SAVED.pattern}/); stopping anyway")
            for command in o.stop:
                console.send(command)
                record["commands"].append(command)
            deadline = time.monotonic() + grace
            while self.process.poll() is None and time.monotonic() < deadline:
                if ctx.cancelled.is_set() and not quick:
                    quick, deadline = True, min(deadline, time.monotonic() + CANCEL_STOP)
                try:
                    self.process.wait(timeout=0.25)
                except subprocess.TimeoutExpired:
                    pass
            if self.process.poll() is None:
                processes.kill_tree(self.process)
                record["how"] = "killed"
                self._finding(f"the server did not stop within {_span(grace)} of {' then '.join(record['commands'])}; "
                              "killed")
            else:
                record["how"] = "stop"
        else:
            record["how"] = "exited"
        processes.kill_tree(self.process)  # what it started goes with it; this also reaps it
        self.exit = self.process.returncode
        self.stopped = record["how"]
        record.update(seconds=round(time.monotonic() - began, 3), exited_at=console.now())
        if record["sent_at"] is not None:
            ctx.measure("stop_seconds", record["seconds"], unit="s")
        if self.exit not in (0, None) and self.stopped != "killed" and not self.reported_exit:
            self._finding(f"the server exited {self.exit} " + ("after stop" if self.stopped == "stop" else "by itself"),
                          exit=True)

    # what the run leaves -------------------------------------------------------------------------
    def _files(self) -> tuple[list[str], list[str]]:
        if self.files_cache is None:
            self.files_cache = _entries(self.copy) if self.copy.is_dir() else ([], [])
        return self.files_cache

    def _remove_copy(self):
        try:
            remove_tree(self.copy)
        except OSError as error:
            self.ctx.log.warning(f"the copy at {self.copy} could not be removed: {error}")

    def _collect(self):
        ctx = self.ctx
        if self.process is None:  # it never ran: there is nothing to keep, only the copy to remove
            self._remove_copy()
            return
        ctx.progress("keep", msg="console, crash reports, receipt" + (", kept files" if self.o.keep else ""))
        steps = [self._close_console, self._keep_crash_reports]
        if self.probe is not None:
            steps.append(self._keep_probe)
        steps.append(self._keep_files)
        for step in steps:
            try:
                step()
            except Exception as error:  # noqa: BLE001 - one step that fails must not cost the receipt
                problem = f"{step.__name__.strip('_').replace('_', ' ')}: {type(error).__name__}: {error}"
                self.problems.append(problem)
                self._finding(f"keeping what the run left failed ({problem})")
        inventory = ctx.out / "inventory.json"
        inventory.write_text(json.dumps({"schema": "wb.lab.server.inventory/1", "server": self.copied,
                                         "added": self.added}, indent=1) + "\n", encoding="utf-8")
        ctx.output("inventory", inventory, "application/json")
        receipt = ctx.out / "receipt.json"
        receipt.write_text(json.dumps(self._receipt(), indent=1, default=str) + "\n", encoding="utf-8")
        ctx.output("receipt", receipt, "application/json")
        self._remove_copy()

    def _close_console(self):
        if self.console is not None:
            self.console.close()
            ctx = self.ctx
            ctx.output("console", ctx.out / "console.log", "text/plain")

    def _keep_crash_reports(self):
        files, _ = self._files()
        for relative in files:
            inside = relative.startswith("crash-reports/")
            if not (inside or ("/" not in relative and _matches(relative, "hs_err_pid*.log"))):
                continue
            if self.before.get(relative, {}).get("sha256") == sha256_file(self.copy / relative):
                continue  # it came with the server folder
            _place(self.copy / relative, self.ctx.out / (relative if inside else f"crash-reports/{relative}"), False)
            self.crash_reports.append(relative)
            self._finding(f"the server wrote a crash report: {relative}" if inside
                          else f"the JVM wrote a fatal error log: {relative}")
        if self.crash_reports:
            self.ctx.output("crash-reports", self.ctx.out / "crash-reports")

    def _keep_probe(self):
        files, _ = self._files()
        found = [f for f in files if _matches(f, self.probe)]
        if not found:
            self._finding(f"the run wrote no file matching {self.probe}")
            return
        for relative in found:
            path = self.copy / relative
            digest = sha256_file(path)
            entry = {"file": relative, "sha256": digest, "bytes": path.stat().st_size, "valid": False, "failed": []}
            self.probes.append(entry)
            _place(path, self.ctx.out / "probe" / relative, False)
            entry["kept"] = self.ctx.host_path(self.ctx.out / "probe" / relative)
            if self.before.get(relative, {}).get("sha256") == digest:
                self._finding(f"{relative} came with the server folder unchanged; the run did not write it")
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, UnicodeDecodeError) as error:
                self._finding(f"{relative} is not valid JSON: {error}")
                continue
            entry["valid"] = True
            for dotted, expected in self.require.items():
                actual = _lookup(data, dotted)
                if actual is _MISSING:
                    entry["failed"].append(f"{dotted} is missing")
                elif not _same(actual, expected):
                    entry["failed"].append(f"{dotted} is {json.dumps(actual)}, not {json.dumps(expected)}")
            for failure in entry["failed"]:
                self._finding(f"{relative}: {failure}")
            if len(found) == 1 and entry["bytes"] <= JSON_IN_DATA:
                entry["json"] = data
        self.ctx.output("probe", self.ctx.out / "probe")

    def _keep_files(self):
        if not self.o.keep:
            return
        files, folders = self._files()
        everything = files + folders
        chosen: list[str] = []
        unmatched = []
        for pattern in self.o.keep:
            matched = [e for e in everything if _matches(e, pattern)]
            if not matched:
                unmatched.append(pattern)
            chosen += matched
        selected: list[str] = []
        for relative in sorted(set(chosen), key=lambda r: (r.count("/"), r)):
            if not any(relative.startswith(parent + "/") for parent in selected):
                selected.append(relative)
        folder = self.ctx.out / "files"
        for relative in selected:
            _place(self.copy / relative, folder / relative, True)
            self.kept.append({"path": relative, "folder": relative in folders})
        if unmatched:
            self.ctx.log.warning(f"keep: nothing matched {', '.join(unmatched)}")
        self.receipt["keep"] = {"patterns": self.o.keep, "kept": [k["path"] for k in self.kept], "unmatched": unmatched}
        if self.kept:
            self.ctx.output("files", folder)

    def _receipt(self) -> dict:
        receipt = dict(self.receipt)
        console = self.console
        if self.launched is not None and console is not None:
            def at(offset):
                return (self.launched + timedelta(seconds=offset)).isoformat(timespec="milliseconds") \
                    if offset is not None else None
            stop = receipt.get("stop") or {}
            receipt["times"] = {
                "launched": self.launched.isoformat(timespec="milliseconds"), "ready": at(self.ready_at),
                "stop": at(stop.get("sent_at")), "exited": at(stop.get("exited_at")),
                "seconds": {"to_ready": self.ready_at, "run": (receipt.get("run") or {}).get("seconds_run"),
                            "stop": stop.get("seconds"), "total": stop.get("exited_at")}}
            receipt["console"] = {"file": "console.log", "lines": console.count}
        receipt.update(exit=self.exit, stopped=self.stopped, crash_reports=self.crash_reports,
                       findings=list(self.ctx.findings), cancelled=self.ctx.cancelled.is_set())
        if self.probe is not None:
            receipt["probe"] = {"pattern": self.probe, "require": self.require,
                                "files": [{k: v for k, v in p.items() if k != "json"} for p in self.probes]}
        if self.problems:
            receipt["problems"] = self.problems
        return receipt

    def _result(self) -> dict:
        ctx, o = self.ctx, self.o
        commands = self.receipt.get("commands") or []
        data = {"ready": self.ready_at is not None, "ready_seconds": self.ready_at, "ended": self.ended,
                "ticks": self.ticks_run, "stopped": self.stopped, "exit": self.exit,
                "commands": [{"send": c["send"], "line": c["line"]} for c in commands],
                "crash_reports": self.crash_reports, "kept": [k["path"] for k in self.kept],
                "receipt": ctx.host_path(ctx.out / "receipt.json"), "console": ctx.host_path(ctx.out / "console.log")}
        if self.probe is not None:
            data["probe"] = [{k: v for k, v in p.items() if k != "json"} for p in self.probes]
            data["json"] = self.probes[0].get("json") if len(self.probes) == 1 else None
        if ctx.outcome == "ok":
            run = self.receipt.get("run") or {}
            parts = [f"ready in {_span(self.ready_at)}"]
            answered = sum(1 for c in commands if c["line"] is not None)
            if commands:
                parts.append(f"{len(commands)} command(s)" + (f", {answered} answered" if answered else ""))
            if self.ended == "ticks":
                rate = self.ticks_run / run["seconds_run"] if run.get("seconds_run") else 0
                parts.append(f"ran {self.ticks_run} ticks in {_span(run['seconds_run'])} ({rate:.1f} ticks/s)")
            elif self.ended == "seconds":
                parts.append(f"ran {_span(run['seconds_run'])}")
            elif self.ended == "until":
                parts.append(f"ran {_span(run['seconds_run'])} until {run['line']['line'].strip()[:80]!r}")
            sent = (self.receipt.get("stop") or {}).get("commands")
            parts.append((f"stopped with {' then '.join(map(repr, sent))}" if sent else "stopped by itself")
                         + f" (exit {self.exit})")
            if self.probes:
                parts.append("probe JSON " + ", ".join(p["file"] for p in self.probes))
            ctx.summary("server " + "; ".join(parts))
        return data


# -- the handlers -----------------------------------------------------------------------------------
def server(ctx: Context, **params):
    """lab.server: one dedicated-server session (module.toml lists the params; this module's docstring the run)."""
    return Session(ctx, Options.parse(params)).execute()


def scenario(ctx: Context, probe: str, require: dict, **params):
    """lab.server-scenario: a session whose probe must leave valid JSON at ``probe``."""
    options = Options.parse(params)
    _relative_glob("probe", probe)
    if not options.runs:
        raise ParamError("a scenario needs ticks, seconds or until: how long the probe runs")
    return Session(ctx, options, probe=probe, require=require).execute()
