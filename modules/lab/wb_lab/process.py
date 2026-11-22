"""Owning a child process: killed together with what it started (outside Windows it leads a session of its own),
bounded by a timeout that holds even when it prints nothing, and (``Console``) read line by line while commands
are written to it.

Cancelling a job sets ``ctx.cancelled``; every wait here polls it, so a cancelled job stops its process too.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from wb_core.module import Cancelled, Context, EnvironmentProblem

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

_PRCTL = None
if sys.platform.startswith("linux"):
    try:
        import ctypes
        _PRCTL = ctypes.CDLL(None, use_errno=True).prctl
    except (OSError, AttributeError):
        _PRCTL = None


def _die_with_parent():  # runs in the child between fork and exec
    _PRCTL(1, signal.SIGKILL)  # PR_SET_PDEATHSIG: a worker killed outright takes its server with it


def span(seconds: float) -> str:
    """A duration for a message: 2.5 s, 40 s, 15 min, 4 h."""
    if seconds < 10:
        return f"{seconds:.1f}".removesuffix(".0") + " s"
    if seconds < 120:
        return f"{seconds:.0f} s"
    if seconds < 7200:
        return f"{seconds / 60:.0f} min"
    return f"{seconds / 3600:.1f}".removesuffix(".0") + " h"


def start(args: list, *, cwd, env: dict, stdin: bool, owned: bool = False) -> subprocess.Popen:
    """Starts ``args`` with stdout and stderr merged into one text pipe. Outside Windows it leads a session of its
    own, so ``kill_tree`` reaches what it starts; ``owned`` (Linux) also kills it when this process dies."""
    kwargs: dict = {}
    if os.name != "nt":
        kwargs["start_new_session"] = True
        if owned and _PRCTL is not None:
            kwargs["preexec_fn"] = _die_with_parent
    try:
        return subprocess.Popen([str(a) for a in args], cwd=cwd, env=env,
                                stdin=subprocess.PIPE if stdin else subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", **kwargs)
    except OSError as error:
        raise EnvironmentProblem(f"cannot start {Path(str(args[0])).name}: {error}") from None


def kill_tree(process: subprocess.Popen):
    """Kills the process and everything it started, then reaps it. Outside Windows that is its whole session,
    which may outlive it (a launch script's JVM), so the group is killed even after the process has exited."""
    if os.name == "nt":
        if process.poll() is None:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass
    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


@dataclass
class Ran:
    code: int | None
    """The exit status; None when the run was stopped at its timeout."""
    output: str
    seconds: float


def run(ctx: Context, args: list, *, cwd, timeout: float, env: dict | None = None) -> Ran:
    """Runs a tool to the end or to ``timeout``, whichever comes first, its output going to the job log as it
    comes. Cancelling the job kills it."""
    args = [str(a) for a in args]
    ctx.log.debug("$ " + " ".join(args))
    started = time.monotonic()
    process = start(args, cwd=cwd, env=dict(os.environ, **(env or {})), stdin=False)
    lines: list[str] = []

    def pump():
        for line in process.stdout:
            lines.append(line.rstrip("\n"))
            ctx.log.debug(lines[-1])

    reader = threading.Thread(target=pump, name="tool-output", daemon=True)
    reader.start()
    code: int | None = None
    try:
        while True:
            try:
                code = process.wait(timeout=0.25)
                break
            except subprocess.TimeoutExpired:
                if ctx.cancelled.is_set():
                    raise Cancelled() from None
                if time.monotonic() - started > timeout:
                    break
    finally:
        if process.poll() is None:  # the timeout, a cancel, or an interrupt
            kill_tree(process)
    reader.join(5)
    if not reader.is_alive():  # a grandchild still holding the pipe keeps the reader; closing under it would race
        process.stdout.close()
    return Ran(code, "\n".join(lines), round(time.monotonic() - started, 3))


@dataclass
class Line:
    index: int
    at: float
    """Seconds since the console was opened."""
    text: str
    """The line without its line end and ANSI colours."""


class Console:
    """An interactive process's console: its output lines in order, each with a global index (also written to
    ``log`` as it is read), and its stdin for commands. Waits name a deadline on the monotonic clock.

    Only the last ``keep`` lines or so stay in memory, so a long, chatty run cannot exhaust it; waits only ever look
    at recent lines, and the log has them all."""

    def __init__(self, process: subprocess.Popen, log: Path, keep: int = 100_000):
        self.process = process
        self.lines: list[Line] = []
        self.count = 0  # lines read so far; self.lines holds the last of them, from index self.count - len
        self.closed = False
        self.opened = time.monotonic()
        self._keep = keep
        self._cond = threading.Condition()
        self._log = open(log, "w", encoding="utf-8", newline="\n")
        self._reader = threading.Thread(target=self._read, name="console", daemon=True)
        self._reader.start()

    def now(self) -> float:
        return round(time.monotonic() - self.opened, 3)

    def _read(self):
        try:
            for raw in self.process.stdout:
                text = ANSI.sub("", raw.rstrip("\r\n"))
                self._log.write(text + "\n")
                self._log.flush()
                with self._cond:
                    self.lines.append(Line(self.count, self.now(), text))
                    self.count += 1
                    if len(self.lines) > 2 * self._keep:
                        del self.lines[:-self._keep]
                    self._cond.notify_all()
        except (OSError, ValueError):
            pass
        finally:
            with self._cond:
                self.closed = True
                self._cond.notify_all()

    def send(self, command: str) -> int | None:
        """Writes one command line; returns the index its reply starts at, or None when the process is gone."""
        with self._cond:
            index = self.count
        try:
            self.process.stdin.write(command + "\n")
            self.process.stdin.flush()
        except (OSError, ValueError):
            return None
        return index

    def scan(self, start: int, deadline: float, cancelled: threading.Event) -> Iterator[Line]:
        """The lines from ``start`` on, as they arrive, until the deadline passes or the output ends."""
        position = start
        while True:
            with self._cond:
                while position >= self.count and not self.closed and not cancelled.is_set():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self._cond.wait(min(remaining, 0.25))
                first = self.count - len(self.lines)
                fresh = self.lines[max(position - first, 0):]
                closed = self.closed
            yield from fresh
            if fresh:
                position = fresh[-1].index + 1
            if cancelled.is_set():
                raise Cancelled()
            if closed or time.monotonic() >= deadline:
                return

    def first(self, patterns: list[re.Pattern], start: int, deadline: float, cancelled: threading.Event):
        """The first line from ``start`` on that one of ``patterns`` matches: (pattern index, line, match), or
        None at the deadline or the end of the output."""
        for line in self.scan(start, deadline, cancelled):
            for number, pattern in enumerate(patterns):
                match = pattern.search(line.text)
                if match:
                    return number, line, match
        return None

    def tail(self, count: int = 3, width: int = 300) -> str:
        with self._cond:
            last = [line.text.strip() for line in self.lines[-count * 4:] if line.text.strip()][-count:]
        text = " | ".join(last)
        return text if len(text) <= width else "..." + text[-width:]

    def close(self, timeout: float = 10):
        """Waits for the output to end (the process has exited) and closes the log."""
        if self.process.stdin:
            try:
                self.process.stdin.close()
            except OSError:
                pass
        self._reader.join(timeout)
        if not self._reader.is_alive():
            self._log.close()
            try:  # the pipe it read from is done too
                self.process.stdout.close()
            except (OSError, AttributeError):
                pass
        else:
            self._log.flush()
