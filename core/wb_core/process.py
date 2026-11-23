"""Owning the processes a job starts.

A step ends when its process exits, not when its output pipe closes: a grandchild that keeps the pipe open (a Gradle
daemon, a launcher's server) cannot hold the job. A deadline fires whether or not the process prints anything.
Everything a job started is killed when the job ends, including what detached itself from the step that started it:

- on Windows the worker puts itself in a Job Object that kills every process in it when the worker exits;
- on Linux the worker is a child subreaper, so a detached descendant becomes its child, and ``kill_descendants``
  kills them all before the worker exits.

Outside Windows each tool also leads a session of its own, so ``kill_tree`` reaches what it started.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

PR_SET_PDEATHSIG = 1
PR_SET_CHILD_SUBREAPER = 36

_PRCTL = None
if sys.platform.startswith("linux"):
    try:
        import ctypes
        _PRCTL = ctypes.CDLL(None, use_errno=True).prctl
    except (OSError, AttributeError):
        _PRCTL = None

_JOB_HANDLE = None  # Windows: kept open for the worker's life; closing it (the worker exiting) kills the job's tree


class StartError(OSError):
    pass


def own_tree() -> str:
    """Makes this process the owner of everything it will start: called once, when a worker starts. Returns what
    was set up, for the log."""
    global _JOB_HANDLE
    if os.name == "nt":
        try:
            _JOB_HANDLE = _windows_job()
            return "a Job Object that kills the job's processes when it ends"
        except OSError as error:
            return f"no Job Object ({error}); tools are killed by taskkill /T"
    if _PRCTL is not None:
        if _PRCTL(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) == 0:
            return "a child subreaper, so detached descendants are still killed"
        return "not a subreaper (prctl refused); detached descendants may outlive the job"
    return "no process ownership on this system"


def _windows_job():
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class BASIC(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class IO(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in ("ReadOperationCount", "WriteOperationCount",
                                                         "OtherOperationCount", "ReadTransferCount",
                                                         "WriteTransferCount", "OtherTransferCount")]

    class EXTENDED(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    info = EXTENDED()
    info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(wintypes.HANDLE(job), 9, ctypes.byref(info), ctypes.sizeof(info)):
        raise ctypes.WinError(ctypes.get_last_error())  # 9: JobObjectExtendedLimitInformation
    if not kernel32.AssignProcessToJobObject(wintypes.HANDLE(job), wintypes.HANDLE(kernel32.GetCurrentProcess())):
        raise ctypes.WinError(ctypes.get_last_error())
    return job


def _children() -> dict[int, list[int]]:
    """Linux: parent pid -> child pids, from /proc."""
    tree: dict[int, list[int]] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat", encoding="ascii", errors="replace") as handle:
                fields = handle.read().rsplit(")", 1)[1].split()
        except OSError:
            continue
        tree.setdefault(int(fields[1]), []).append(int(entry))
    return tree


def kill_descendants() -> int:
    """Kills every descendant of this process (Linux; on Windows the Job Object does it when the worker exits).
    Returns how many were killed."""
    if os.name == "nt" or not os.path.isdir("/proc"):
        return 0
    killed = 0
    for _ in range(3):  # a process may fork while the first sweep runs
        tree, found, frontier = _children(), [], [os.getpid()]
        while frontier:
            frontier = [c for p in frontier for c in tree.get(p, [])]
            found.extend(frontier)
        if not found:
            break
        for pid in found:
            try:
                os.kill(pid, signal.SIGKILL)
                killed += 1
            except OSError:
                pass
        for pid in found:
            try:
                os.waitpid(pid, os.WNOHANG)
            except OSError:
                pass
        time.sleep(0.05)
    return killed


def start(args: list, *, cwd=None, env: dict | None = None, stdin: bool = False, owned: bool = False
          ) -> subprocess.Popen:
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
        raise StartError(f"cannot start {Path(str(args[0])).name}: {error}") from None


def _die_with_parent():  # runs in the child between fork and exec
    _PRCTL(PR_SET_PDEATHSIG, signal.SIGKILL)


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
    """The exit status; None when the run was stopped at its timeout or by a cancel."""
    output: str
    seconds: float
    timed_out: bool = False
    cancelled: bool = False


PIPE_GRACE = 5.0  # seconds to finish reading a pipe a grandchild still holds once the process has exited


def run(args: list, *, cwd=None, env: dict | None = None, timeout: float | None = None,
        cancelled: threading.Event | None = None, on_line: Callable[[str], None] | None = None,
        capture: bool = True) -> Ran:
    """Runs a tool until it exits, its deadline passes or ``cancelled`` is set, whichever comes first. The deadline
    holds when the tool prints nothing. Output lines go to ``on_line`` as they come. A tool that exits while a
    grandchild still holds its output pipe returns at once, with what was read within PIPE_GRACE seconds."""
    started = time.monotonic()
    process = start(args, cwd=cwd, env=env)
    lines: list[str] = []

    def pump():
        try:
            for raw in process.stdout:
                line = raw.rstrip("\n")
                if capture:
                    lines.append(line)
                if on_line is not None:
                    on_line(line)
        except (OSError, ValueError):
            pass

    reader = threading.Thread(target=pump, name="tool-output", daemon=True)
    reader.start()
    code: int | None = None
    timed_out = stopped = False
    try:
        while True:
            try:
                code = process.wait(timeout=0.25)
                break
            except subprocess.TimeoutExpired:
                if cancelled is not None and cancelled.is_set():
                    stopped = True
                    break
                if timeout is not None and time.monotonic() - started > timeout:
                    timed_out = True
                    break
    finally:
        if process.poll() is None:  # the timeout, a cancel, or an interrupt
            kill_tree(process)
    reader.join(PIPE_GRACE)
    if not reader.is_alive():  # a grandchild still holding the pipe keeps the reader; closing under it would race
        process.stdout.close()
    return Ran(None if (timed_out or stopped) else code, "\n".join(lines), round(time.monotonic() - started, 3),
               timed_out, stopped)
