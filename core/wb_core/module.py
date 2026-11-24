"""The API a module's handlers see. A handler is ``def name(ctx: Context, **params) -> dict``.

Returning means ``ok``; ``ctx.finding(...)`` makes the outcome ``finding``; raising ``ParamError`` means
``usage``; any other exception means ``environment`` (traceback in the job log, not the envelope).
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import subprocess
import threading
import time
from pathlib import Path


class ParamError(Exception):
    """The request's parameters are wrong in a way the schema could not catch: outcome ``usage``."""


class EnvironmentProblem(Exception):
    """Something outside the request is missing or broken: outcome ``environment``."""


class QuoteChanged(Exception):
    """The approved quote no longer holds (the price rose, the offer went): the job returns to
    ``awaiting_approval`` with ``quote``, and the earlier yes no longer counts."""

    def __init__(self, quote: dict, reason: str):
        super().__init__(reason)
        self.quote, self.reason = quote, reason


class Cancelled(Exception):
    pass


class TimedOut(Exception):
    """A tool was stopped at its limit: outcome ``finding`` with reason kind timeout, and never cached."""


def sha256_file(path: Path, tick=None) -> str:
    """A file's SHA-256. ``tick(bytes)`` is called for each block read; it may raise to stop the read."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
            if tick is not None:
                tick(len(block))
    return digest.hexdigest()


LISTED_EVERY = 256  # files listed between two calls of a tree digest's watch


def sha256_tree(path: Path, known: dict | None = None, watch=None) -> tuple[str, int]:
    """A directory's digest: SHA-256 over its sorted relative paths and file digests; with file count.

    ``known`` (a path's ``(size, mtime_ns, digest)``, kept by the caller) lets one that hashes the same files again
    skip those whose size and modification time are unchanged.

    ``watch(stage, files, size, read)`` lets the caller follow a digest and stop it by raising: it is called while
    the files are listed (stage ``listing``: files and bytes found so far, nothing read yet), once when the list is
    whole (``listed``: the totals, before any file is read) and while they are read (``reading``: bytes read so
    far). A folder is always listed whole before its first byte is read, so a caller can refuse one by its size."""
    path = Path(path)
    if path.is_file():
        size = path.stat().st_size if watch is not None else 0
        if watch is not None:
            watch("listed", 1, size, 0)
        return _file_digest(path, known, _reader(watch, 1, size)), 1
    files, size = [], 0
    for file in path.rglob("*"):
        if not file.is_file():
            continue
        files.append(file)
        if watch is not None:
            size += file.stat().st_size
            if len(files) % LISTED_EVERY == 0:
                watch("listing", len(files), size, 0)
    if watch is not None:
        watch("listed", len(files), size, 0)
    digest = hashlib.sha256()
    tick = _reader(watch, len(files), size)
    for file in sorted(files):
        relative = file.relative_to(path).as_posix()
        digest.update(f"{relative}\0{_file_digest(file, known, tick)}\n".encode())
    return digest.hexdigest(), len(files)


def _reader(watch, files: int, size: int):
    """The per-block callback of a watched digest: counts the bytes read and reports them."""
    if watch is None:
        return None
    read = [0]

    def tick(count: int):
        read[0] += count
        watch("reading", files, size, read[0])
    return tick


RACY_SECONDS = 2.0  # a file changed this recently may change again within the same clock tick, unseen by its stat


def _file_digest(file: Path, known: dict | None, tick=None) -> str:
    # sha256_file gets a tick only when there is one, so a caller that replaces it sees the call it always saw.
    read = (lambda: sha256_file(file, tick)) if tick is not None else (lambda: sha256_file(file))
    if known is None:
        return read()
    stat = file.stat()
    key = str(file)
    hit = known.get(key)
    if hit and hit[0] == stat.st_size and hit[1] == stat.st_mtime_ns:
        return hit[2]
    digest = read()
    if time.time() - stat.st_mtime > RACY_SECONDS:
        known[key] = (stat.st_size, stat.st_mtime_ns, digest)
    return digest


class _Log:
    def __init__(self, ctx: "Context"):
        self.ctx = ctx

    def _emit(self, level, msg, **fields):
        self.ctx.emit("log", level=level, msg=str(msg), fields=fields or None)

    def debug(self, msg, **f):
        self._emit("DEBUG", msg, **f)

    def info(self, msg, **f):
        self._emit("INFO", msg, **f)

    def warning(self, msg, **f):
        self._emit("WARNING", msg, **f)

    def error(self, msg, **f):
        self._emit("ERROR", msg, **f)


class Context:
    def __init__(self, spec: dict, emit):
        self.spec = spec
        self._emit = emit
        self.job: str = spec["job"]
        self.command: str = spec["command"]
        self.module: str = spec["module"]
        self.params: dict = spec.get("params", {})
        self.config: dict = spec.get("module_config", {})
        self.machine: str = spec.get("machine", "container")
        self.work = Path(spec["work"])
        self.out = Path(spec["out"])
        self.quote: dict | None = spec.get("quote")
        self.log = _Log(self)
        self.cancelled = threading.Event()
        self.outcome = "ok"
        self.reasons: list[dict] = []
        self.summary_text: str | None = None
        self.findings: list[str] = []
        self._secrets: dict = spec.get("secrets", {})
        self._jdks: dict = spec.get("jdks", {})
        # What the scheduler holds for this job on its machine ({"cpu", "mem_gb"}); empty on the host.
        self.reservation: dict = spec.get("reservation") or {}
        self.work.mkdir(parents=True, exist_ok=True)
        self.out.mkdir(parents=True, exist_ok=True)

    # -- events ----------------------------------------------------------------------------------
    def emit(self, kind: str, **data):
        self._emit({"kind": kind, "ts": time.time(), **data})

    def progress(self, phase: str, fraction: float | None = None, msg: str | None = None):
        self.emit("progress", phase=phase, fraction=fraction, msg=msg)

    def measure(self, name: str, value, unit: str | None = None, **labels):
        self.emit("measurement", name=name, value=value, unit=unit, labels=labels)

    def output(self, name: str, path, media_type: str | None = None) -> str:
        path = Path(path)
        digest, count = sha256_tree(path)
        size = path.stat().st_size if path.is_file() else sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
        kind = media_type or (mimetypes.guess_type(path.name)[0] if path.is_file() else "inode/directory") \
            or "application/octet-stream"
        self.emit("artifact", name=name, path=str(path), sha256=digest, bytes=size, media_type=kind,
                  files=count)
        return digest

    def finding(self, summary: str):
        self.outcome = "finding"
        self.findings.append(summary)
        self.summary_text = summary if len(self.findings) == 1 else f"{len(self.findings)} findings: {self.findings[0]}"
        self.emit("finding", summary=summary)

    def summary(self, text: str):
        self.summary_text = text

    def reason(self, kind: str, name: str, detail: str, fix: str | None = None):
        """Adds a reason to the envelope: what went wrong (detail) and the next thing to do (fix)."""
        self.reasons.append({"kind": kind, "name": name, "detail": detail, "fix": fix})

    def cost(self, amount: float, currency: str = "USD", **details):
        """Records money this job spent (a rental's charge); it appears as the envelope's ``cost``."""
        self.emit("cost", amount=round(float(amount), 6), currency=currency, **details)

    def machine_info(self, **info):
        """Describes the machine the work ran on (card, driver, host); merged into the envelope."""
        self.emit("machine", **info)

    # -- resources -------------------------------------------------------------------------------
    def jdk(self, version: str | int) -> Path:
        key = str(version)
        if key not in self._jdks:
            raise EnvironmentProblem(f"JDK {key} is not available (have: {', '.join(sorted(self._jdks)) or 'none'})")
        home = Path(self._jdks[key])
        release = home / "release"
        build = key
        if release.is_file():
            for line in release.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("JAVA_RUNTIME_VERSION="):
                    build = line.split("=", 1)[1].strip().strip('"')
        self.emit("toolchain", jdk=build)
        return home

    def java(self, version: str | int) -> Path:
        home = self.jdk(version)
        exe = home / "bin" / ("java.exe" if os.name == "nt" else "java")
        return exe

    def secret(self, name: str) -> str:
        path = self._secrets.get(name)
        if not path or not Path(path).is_file():
            raise EnvironmentProblem(f"secret {name} is not mounted")
        return Path(path).read_text(encoding="utf-8").strip()

    def secret_path(self, name: str) -> Path:
        path = self._secrets.get(name)
        if not path or not Path(path).exists():
            raise EnvironmentProblem(f"secret {name} is not mounted")
        return Path(path)

    def host_path(self, container_path) -> str:
        """The caller-facing host path of a container path (for messages and data)."""
        from .paths import PathMap
        from .settings import Root
        roots = [Root(**r) for r in self.spec.get("roots", [])]
        return PathMap(roots).to_host(str(container_path), self.spec.get("path_style", "windows"))

    def requote(self, quote: dict, reason: str):
        raise QuoteChanged(quote, reason)

    def check_cancel(self):
        if self.cancelled.is_set():
            raise Cancelled()

    def sleep(self, seconds: float):
        if self.cancelled.wait(seconds):
            raise Cancelled()

    # -- running tools ---------------------------------------------------------------------------
    def run(self, args: list, *, cwd=None, env: dict | None = None, timeout: float | None = None,
            check: bool = False, log_output: bool = True, phase: str | None = None,
            capture: bool = True, inherit: bool = True) -> subprocess.CompletedProcess:
        """Runs a tool until it exits; its output lines go to the job log; cancelling the job kills it and everything
        it started. A ``timeout`` holds even when the tool prints nothing; reaching it raises TimedOut. A grandchild
        that keeps the output pipe open (a Gradle daemon) does not keep the call waiting. With ``inherit`` off the
        tool gets ``env`` alone, none of this worker's own environment."""
        from . import process as processes
        args = [str(a) for a in args]
        self.log.debug("$ " + " ".join(args))
        try:
            done = processes.run(args, cwd=cwd, env=dict(os.environ if inherit else {}, **(env or {})), timeout=timeout,
                                 cancelled=self.cancelled, on_line=self.log.debug if log_output else None,
                                 capture=capture)
        except processes.StartError as error:
            raise EnvironmentProblem(str(error)) from None
        if done.cancelled or self.cancelled.is_set():
            raise Cancelled()
        if done.timed_out:
            raise TimedOut(f"{Path(args[0]).name} was stopped at its limit of {timeout:.0f} s")
        result = subprocess.CompletedProcess(args, done.code, done.output, "")
        if check and done.code != 0:
            raise EnvironmentProblem(f"{Path(args[0]).name} exited {done.code}: "
                                     + "\n".join(done.output.splitlines()[-5:]))
        return result

    def _kill_on_cancel(self, process):
        while process.poll() is None:
            if self.cancelled.wait(0.5):
                process.kill()
                return

    # -- calling other commands (plans) ----------------------------------------------------------
    def call(self, command: str, out: str | None = None, fresh: bool = False, **params) -> dict:
        """Submits another command to the daemon and waits for its envelope (used by plans)."""
        from wb.client import Client
        client = Client(os.environ.get("WB_URL"), os.environ.get("WB_TOKEN"))
        client.path_style = "posix"
        job = client.submit(command, params, out=out, fresh=fresh)
        self.emit("child", job=job.id, command=command)
        while True:
            envelope = job.wait(timeout=30)
            if envelope.get("state") in ("settled", "cancelled", "interrupted", "awaiting_approval"):
                return dict(envelope)
            if self.cancelled.is_set():
                job.cancel()
                raise Cancelled()

    def call_inline(self, command: str, **params) -> dict:
        """Runs an inline command (one its manifest marks inline) inside this job's reservation and returns
        {command, outcome, summary, data}. It has no job of its own, so it never queues."""
        from wb.client import Client
        client = Client(os.environ.get("WB_URL"), os.environ.get("WB_TOKEN"))
        return client.call_inline(self.job, command, params)


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
