"""The peak memory of a step's process tree: the most resident memory (RSS) the step and what it
started held at once, sampled while it runs.

A step is a process and whatever it starts: a Gradle client and its daemon, a forked test JVM.
``TreeMemory`` sums their resident sets every ``interval`` seconds on a thread of its own and keeps
the largest sum. It also keeps the largest high-water mark any one of the step's own processes
reported (the kernel's own peak, which a spike between two samples still reaches), so the peak it
gives is the larger of the two.

- Linux: ``/proc``. The tree is the step's process, its descendants by parent, and every process
  of the session the step leads (a stoppable step leads one, so a daemon whose parent exited still
  counts). RSS is ``VmRSS``, the high-water mark ``VmHWM``.
- Windows: a Job Object the step is put in as soon as it starts. Everything it starts joins the
  job, a daemon too, and the job's process list is the tree. The job sets no limit and does not
  kill on close: it only names the members. RSS is the working set, the high-water mark the peak
  working set (GetProcessMemoryInfo). A step that cannot be put in a job is measured by its
  descendants by parent, from a process snapshot.

A JVM the step did not start but works through is counted too, with its descendants, while the
step is connected to it: a Gradle daemon an earlier step started and this one reuses (or a Kotlin
compile daemon) is found as the other end of a TCP connection of one of the step's processes, on
this machine (``/proc/net/tcp``, ``/proc/net/tcp6`` and the processes' sockets on Linux;
GetExtendedTcpTable on Windows). Only its resident set counts, not its high-water mark, which
another step may have made.

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
JAVA = ("java", "javaw", "java.exe", "javaw.exe")


class TreeMemory:
    """Samples ``process``'s tree from construction until ``stop()``."""

    def __init__(self, process: Any, interval: float = INTERVAL) -> None:
        self.interval = interval
        self.samples = 0
        self.peak_sum = 0
        self.peak_member = 0
        self.served = False  # a JVM the step did not start was counted at least once
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
            own, served = self._backend.members()
        except OSError:
            return
        total, seen = 0, False
        for pid in own | served:
            try:
                resident, high = self._backend.memory(pid)
            except OSError:
                continue  # exited between the listing and the read, or not ours to read
            seen = True
            total += resident
            if pid in own:
                self.peak_member = max(self.peak_member, high, resident)
            else:
                self.served = True
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


def _closure(found: set[int], children: dict[int, list[int]]) -> set[int]:
    """``found`` with every descendant of its members, by parent."""
    todo = list(found)
    found = set(found)
    while todo:
        for child in children.get(todo.pop(), ()):
            if child not in found:
                found.add(child)
                todo.append(child)
    return found


def peers(connections: list[tuple[int, Any, Any]], own: set[int]) -> set[int]:
    """The processes at the other end of an own process's TCP connection on this machine, from
    ``(owner, local endpoint, remote endpoint)`` rows of established connections. A connection
    between two local processes is listed from both ends."""
    wanted = {(remote, local) for owner, local, remote in connections if owner in own}
    return {owner for owner, local, remote in connections
            if owner not in own and (local, remote) in wanted}


class _Linux:
    source = "proc"

    def __init__(self, root: int, proc: Path = Path("/proc")) -> None:
        if not (proc / "self" / "stat").is_file():
            raise OSError(f"{proc} is not a readable procfs")
        self.root = root
        self.proc = proc

    def members(self) -> tuple[set[int], set[int]]:
        children: dict[int, list[int]] = {}
        session: set[int] = {self.root}
        names: dict[int, str] = {}
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
            names[pid] = text[text.index("(") + 1 : text.rindex(")")]
            children.setdefault(parent, []).append(pid)
            if sid == self.root:
                session.add(pid)
        own = _closure(session, children)
        served = {pid for pid in peers(self._connections(own, set(names)), own)
                  if names.get(pid) in JAVA}
        return own, _closure(served, children) - own

    def _sockets(self, pid: int) -> set[str]:
        found = set()
        try:
            descriptors = os.scandir(self.proc / str(pid) / "fd")
        except OSError:
            return found
        with descriptors:
            for descriptor in descriptors:
                try:
                    target = os.readlink(descriptor.path)
                except OSError:
                    continue
                if target.startswith("socket:["):
                    found.add(target[8:-1])
        return found

    def _connections(self, own: set[int], every: set[int]) -> list[tuple[int, Any, Any]]:
        """Established TCP connections of this machine's processes, each with its owner, for the
        connections an own process holds and their other ends."""
        rows = {}
        for table in ("tcp", "tcp6"):
            try:
                lines = (self.proc / "net" / table).read_text(encoding="ascii").splitlines()[1:]
            except OSError:
                continue
            for line in lines:
                fields = line.split()
                if len(fields) > 9 and fields[3] == "01":  # ESTABLISHED
                    rows[fields[9]] = (fields[1], fields[2])
        if not rows:
            return []
        mine = {inode: pid for pid in own for inode in self._sockets(pid) if inode in rows}
        wanted = {(rows[inode][1], rows[inode][0]) for inode in mine}
        theirs = {inode for inode, ends in rows.items() if ends in wanted} - set(mine)
        if not theirs:  # the step talks only to itself (a Gradle client to the daemon it started)
            return [(pid, *rows[inode]) for inode, pid in mine.items()]
        owners = {}
        for pid in every - own:  # only when another process holds the other end
            for inode in self._sockets(pid) & theirs:
                owners[inode] = pid
        return [(pid, *rows[inode]) for inode, pid in {**mine, **owners}.items()]

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
AF_INET = 2
TCP_TABLE_OWNER_PID_CONNECTIONS = 4
MIB_TCP_STATE_ESTAB = 5
ERROR_INSUFFICIENT_BUFFER = 122


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
        kernel = ctypes.WinDLL(  # type: ignore[attr-defined,unused-ignore]
            "kernel32", use_last_error=True
        )
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
        try:
            self.iphlpapi = ctypes.WinDLL("iphlpapi")  # type: ignore[attr-defined,unused-ignore]
            self.iphlpapi.GetExtendedTcpTable.argtypes = [
                handle, ctypes.POINTER(dword), boolean, dword, ctypes.c_int, dword
            ]
            self.iphlpapi.GetExtendedTcpTable.restype = dword
        except (OSError, AttributeError):
            self.iphlpapi = None
        self.job = kernel.CreateJobObjectW(None, None)
        own = getattr(process, "_handle", None)  # Popen's handle of the step's process
        if self.job and (own is None or not kernel.AssignProcessToJobObject(self.job, int(own))):
            kernel.CloseHandle(self.job)
            self.job = None
        self.source = "job" if self.job else "snapshot"

    def members(self) -> tuple[set[int], set[int]]:
        snapshot: tuple[dict[int, list[int]], dict[int, str]] | None = None
        if self.job:
            own = self._job_members()
        else:
            snapshot = self._snapshot()
            own = _closure({self.root}, snapshot[0])
        connected = peers(self._connections(), own)
        if not connected:
            return own, set()
        children, names = snapshot or self._snapshot()
        served = {pid for pid in connected if names.get(pid, "").lower() in JAVA}
        return own, _closure(served, children) - own

    def _job_members(self) -> set[int]:
        ids = _ProcessIds()
        done = self.kernel.QueryInformationJobObject(
            self.job, JOB_OBJECT_BASIC_PROCESS_ID_LIST, ctypes.byref(ids), ctypes.sizeof(ids), None
        )
        if not done and not ids.listed:  # more members than LISTED still fills the list
            raise OSError("QueryInformationJobObject failed")
        return {int(pid) for pid in ids.ids[: min(ids.listed, LISTED)]}

    def _snapshot(self) -> tuple[dict[int, list[int]], dict[int, str]]:
        """Children by parent and executable names by process, from a process snapshot."""
        from .windows import TH32CS_SNAPPROCESS, ProcessEntry

        kernel = self.api.kernel
        snapshot = kernel.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snapshot == ctypes.c_void_p(-1).value:
            raise OSError("CreateToolhelp32Snapshot failed")
        children: dict[int, list[int]] = {}
        names: dict[int, str] = {}
        try:
            entry = ProcessEntry()
            entry.dwSize = ctypes.sizeof(entry)
            more = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
            while more:
                parent, pid = int(entry.th32ParentProcessID), int(entry.th32ProcessID)
                children.setdefault(parent, []).append(pid)
                names[pid] = entry.szExeFile
                more = kernel.Process32NextW(snapshot, ctypes.byref(entry))
        finally:
            kernel.CloseHandle(snapshot)
        return children, names

    def _connections(self) -> list[tuple[int, Any, Any]]:
        """Established IPv4 TCP connections with their owners (GetExtendedTcpTable)."""
        if self.iphlpapi is None:
            return []
        size = ctypes.c_uint32(0)
        call = self.iphlpapi.GetExtendedTcpTable
        for _ in range(3):  # the table may grow between the size query and the read
            buffer = ctypes.create_string_buffer(size.value or 1)
            table = TCP_TABLE_OWNER_PID_CONNECTIONS
            code = call(buffer, ctypes.byref(size), False, AF_INET, table, 0)
            if code == 0:
                break
            if code != ERROR_INSUFFICIENT_BUFFER:
                return []
        else:
            return []
        count = ctypes.c_uint32.from_buffer(buffer).value
        words = (ctypes.c_uint32 * (1 + 6 * count)).from_buffer(buffer)
        rows = []
        for index in range(count):
            row = words[1 + 6 * index : 7 + 6 * index]
            state, local, local_port, remote, remote_port, owner = row
            if state == MIB_TCP_STATE_ESTAB:
                rows.append((int(owner), (local, local_port), (remote, remote_port)))
        return rows

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
