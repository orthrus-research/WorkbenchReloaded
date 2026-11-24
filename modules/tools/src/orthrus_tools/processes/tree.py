"""The peak memory of a step's process tree: the most resident memory (RSS) the step and what it
started held at once, sampled while it runs.

A step is a process and whatever it starts: a Gradle client and its daemon, a forked test JVM.
``TreeMemory`` sums their resident sets every ``interval`` seconds on a thread of its own and keeps
the largest sum. It also keeps the largest high-water mark any one member reported (the kernel's
own peak, which a spike between two samples still reaches), so the peak it gives is the larger of
the two: never more than the tree held, and never less than its largest process held.

- Linux: ``/proc``. The tree is the step's process, its descendants by parent, and every process
  of the session the step leads (a stoppable step leads one, so a daemon whose parent exited still
  counts). RSS is ``VmRSS``, the high-water mark ``VmHWM``.
- Windows: a Job Object the step is put in as soon as it starts. Everything it starts joins the
  job, a daemon too, and the job's process list is the tree. The job sets no limit and does not
  kill on close: it only names the members. RSS is the working set, the high-water mark the peak
  working set (GetProcessMemoryInfo). A step that cannot be put in a job is measured by its
  descendants by parent, from a process snapshot.

The container image carries no psutil (image/apt-image.txt, tests/requirements.txt), so this
reads the OS itself. Reads only; a process that exits between two reads is skipped. Where no
backend exists, or no sample saw a process, there is no peak (None), never a made-up zero.
"""

from __future__ import annotations

import ctypes
import os
import sys
import threading
from pathlib import Path
from typing import Any

INTERVAL = 0.5  # seconds between samples


class TreeMemory:
    """Samples ``process``'s tree from construction until ``stop()``."""

    def __init__(self, process: Any, interval: float = INTERVAL) -> None:
        self.interval = interval
        self.samples = 0
        self.peak_sum = 0
        self.peak_member = 0
        self._stop = threading.Event()
        self._backend: Any = None
        try:
            if sys.platform == "win32":
                self._backend = _Windows(process)
            elif sys.platform.startswith("linux"):
                self._backend = _Linux(process.pid)
        except OSError:
            self._backend = None
        self.source = getattr(self._backend, "source", None)
        self._thread: threading.Thread | None = None
        if self._backend is not None:
            self._thread = threading.Thread(target=self._run, name="step-memory", daemon=True)
            self._thread.start()

    def _sample(self) -> None:
        try:
            members = self._backend.members()
        except OSError:
            return
        total, seen = 0, False
        for pid in members:
            try:
                resident, high = self._backend.memory(pid)
            except OSError:
                continue  # exited between the listing and the read, or not ours to read
            seen = True
            total += resident
            self.peak_member = max(self.peak_member, high, resident)
        if seen:
            self.samples += 1
            self.peak_sum = max(self.peak_sum, total)

    def _run(self) -> None:
        while True:
            self._sample()
            if self._stop.wait(self.interval):
                return

    def peak(self) -> int | None:
        return max(self.peak_sum, self.peak_member) if self.samples else None

    def stop(self) -> int | None:
        """Ends the sampling (when the step's process has exited); the peak in bytes, or None."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(5)
        if self._backend is not None and (self._thread is None or not self._thread.is_alive()):
            self._backend.close()
        return self.peak()


def _closure(root: int, children: dict[int, list[int]], found: set[int]) -> set[int]:
    """``found`` with every descendant of its members, by parent."""
    todo = list(found | {root})
    found = set(todo)
    while todo:
        for child in children.get(todo.pop(), ()):
            if child not in found:
                found.add(child)
                todo.append(child)
    return found


class _Linux:
    source = "proc"

    def __init__(self, root: int, proc: Path = Path("/proc")) -> None:
        if not (proc / "self" / "stat").is_file():
            raise OSError(f"{proc} is not a readable procfs")
        self.root = root
        self.proc = proc

    def members(self) -> set[int]:
        children: dict[int, list[int]] = {}
        session: set[int] = set()
        for entry in os.scandir(self.proc):
            if not entry.name.isdigit():
                continue
            try:
                with open(os.path.join(entry.path, "stat"), encoding="utf-8",
                          errors="replace") as handle:
                    text = handle.read()
                fields = text[text.rindex(")") + 2 :].split()  # the name may hold spaces and ')'
                pid, parent, sid = int(entry.name), int(fields[1]), int(fields[3])
            except (OSError, ValueError, IndexError):
                continue
            children.setdefault(parent, []).append(pid)
            if sid == self.root:
                session.add(pid)
        return _closure(self.root, children, session)

    def memory(self, pid: int) -> tuple[int, int]:
        values = {}
        with open(self.proc / str(pid) / "status", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                key, _, rest = line.partition(":")
                if key in ("VmRSS", "VmHWM"):
                    values[key] = int(rest.split()[0]) * 1024
        if "VmRSS" not in values:
            raise OSError(f"process {pid} has no resident memory (exited, or a kernel thread)")
        return values["VmRSS"], values.get("VmHWM", values["VmRSS"])

    def close(self) -> None:
        pass


JOB_OBJECT_BASIC_PROCESS_ID_LIST = 3
LISTED = 1024  # members read per sample; a step with more is measured by its first 1024


class _ProcessIds(ctypes.Structure):
    _fields_ = [
        ("assigned", ctypes.c_uint32),
        ("listed", ctypes.c_uint32),
        ("ids", ctypes.c_size_t * LISTED),
    ]


class _Windows:
    def __init__(self, process: Any) -> None:
        from .windows import WindowsProcessApi

        self.api = WindowsProcessApi()
        self.root = process.pid
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined,unused-ignore]
        handle, dword, boolean = ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int32
        kernel.CreateJobObjectW.argtypes = [handle, ctypes.c_wchar_p]
        kernel.CreateJobObjectW.restype = handle
        kernel.AssignProcessToJobObject.argtypes = [handle, handle]
        kernel.AssignProcessToJobObject.restype = boolean
        kernel.QueryInformationJobObject.argtypes = [
            handle, ctypes.c_int, handle, dword, ctypes.POINTER(dword)
        ]
        kernel.QueryInformationJobObject.restype = boolean
        kernel.CloseHandle.argtypes = [handle]
        kernel.CloseHandle.restype = boolean
        self.kernel = kernel
        self.job = kernel.CreateJobObjectW(None, None)
        own = getattr(process, "_handle", None)  # Popen's handle of the step's process
        if self.job and (own is None or not kernel.AssignProcessToJobObject(self.job, int(own))):
            kernel.CloseHandle(self.job)
            self.job = None
        self.source = "job" if self.job else "snapshot"

    def members(self) -> set[int]:
        if not self.job:
            return self._descendants()
        ids = _ProcessIds()
        done = self.kernel.QueryInformationJobObject(
            self.job, JOB_OBJECT_BASIC_PROCESS_ID_LIST, ctypes.byref(ids), ctypes.sizeof(ids), None
        )
        if not done and not ids.listed:  # more members than LISTED still fills the list
            raise OSError("QueryInformationJobObject failed")
        return {int(pid) for pid in ids.ids[: min(ids.listed, LISTED)]}

    def _descendants(self) -> set[int]:
        from .windows import TH32CS_SNAPPROCESS, ProcessEntry

        kernel = self.api.kernel
        snapshot = kernel.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snapshot == ctypes.c_void_p(-1).value:
            raise OSError("CreateToolhelp32Snapshot failed")
        children: dict[int, list[int]] = {}
        try:
            entry = ProcessEntry()
            entry.dwSize = ctypes.sizeof(entry)
            more = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
            while more:
                parent, pid = int(entry.th32ParentProcessID), int(entry.th32ProcessID)
                children.setdefault(parent, []).append(pid)
                more = kernel.Process32NextW(snapshot, ctypes.byref(entry))
        finally:
            kernel.CloseHandle(snapshot)
        return _closure(self.root, children, set())

    def memory(self, pid: int) -> tuple[int, int]:
        handle = self.api.open(pid)
        try:
            counters = self.api.memory(handle)
        finally:
            self.api.close(handle)
        return counters["workingSetBytes"], counters["peakWorkingSetBytes"]

    def close(self) -> None:
        if self.job:
            job, self.job = self.job, None
            self.kernel.CloseHandle(job)
