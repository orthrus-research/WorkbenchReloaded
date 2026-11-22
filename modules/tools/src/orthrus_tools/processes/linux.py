"""Linux backend: read-only reads of ``/proc``.

``creationId`` is ``<boot id>:<start time in clock ticks since boot>``, which no later process
can share even across a reboot. Where the kernel offers ``pidfd_open`` the handle holds a
pidfd, so the process cannot be confused with a later one that reuses its PID while the
handle is open; every read also re-checks the start time.

Memory fields map onto the Windows names as closely as Linux allows: ``workingSetBytes`` is
``VmRSS``, ``peakWorkingSetBytes`` is ``VmHWM``, ``privateCommitBytes`` is private resident plus
swapped memory from ``smaps_rollup`` (``RssAnon`` plus ``VmSwap`` where that file is not
readable), and ``peakPrivateCommitBytes`` is ``None``: Linux keeps no such peak.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import ProcessGone

JAVA_EXECUTABLES = ("java", "javaw")


@dataclass
class LinuxHandle:
    pid: int
    start_ticks: int
    pidfd: int | None


class LinuxProcessApi:
    platform = "linux"

    def __init__(self, proc: Path = Path("/proc")) -> None:
        self.proc = proc
        if not (proc / "self" / "stat").is_file():
            raise OSError(f"{proc} is not a readable procfs")
        self.ticks_per_second = os.sysconf("SC_CLK_TCK")  # type: ignore[attr-defined,unused-ignore]
        boot_id = proc / "sys" / "kernel" / "random" / "boot_id"
        self.boot_id = boot_id.read_text().strip() if boot_id.is_file() else "unknown-boot"

    def _read(self, pid: int, name: str) -> str:
        try:
            return (self.proc / str(pid) / name).read_text(encoding="utf-8", errors="replace")
        except (FileNotFoundError, ProcessLookupError) as error:
            raise ProcessGone(3, f"process {pid} is gone") from error

    def _stat(self, pid: int) -> list[str]:
        """Fields of /proc/PID/stat from field 3 (state) on; the name may hold spaces and ')'."""
        text = self._read(pid, "stat")
        return text[text.rindex(")") + 2 :].split()

    def _identity_of(self, pid: int) -> dict[str, Any]:
        fields = self._stat(pid)
        nanos_per_tick = 1_000_000_000 // self.ticks_per_second
        return {
            "pid": pid,
            "creationId": f"{self.boot_id}:{int(fields[19])}",
            "creationClock": "linux-boot-id-and-start-ticks",
            "kernelCpuNanos": int(fields[12]) * nanos_per_tick,
            "userCpuNanos": int(fields[11]) * nanos_per_tick,
        }

    def open(self, pid: int) -> LinuxHandle:
        pidfd = None
        if hasattr(os, "pidfd_open"):
            try:
                pidfd = os.pidfd_open(pid)
            except ProcessLookupError as error:
                raise ProcessGone(3, f"process {pid} is gone") from error
            except OSError:
                pidfd = None  # kernel older than 5.3, or a sandbox that forbids it
        start = int(self._stat(pid)[19])
        return LinuxHandle(pid, start, pidfd)

    def close(self, handle: LinuxHandle) -> None:
        if handle.pidfd is not None:
            fd, handle.pidfd = handle.pidfd, None
            os.close(fd)

    def identity(self, handle: LinuxHandle) -> dict[str, Any]:
        identity = self._identity_of(handle.pid)
        if identity["creationId"] != f"{self.boot_id}:{handle.start_ticks}":
            raise ProcessGone(3, f"process {handle.pid} exited and its PID was reused")
        return identity

    def memory(self, handle: LinuxHandle) -> dict[str, Any]:
        status = self._kilobytes(self._read(handle.pid, "status"))
        if "VmRSS" not in status:
            raise OSError(f"process {handle.pid} reports no resident memory (a kernel thread?)")
        try:
            rollup = self._kilobytes(self._read(handle.pid, "smaps_rollup"))
            private = (
                rollup.get("Private_Clean", 0)
                + rollup.get("Private_Dirty", 0)
                + rollup.get("Swap", 0)
            )
        except PermissionError:
            private = status.get("RssAnon", 0) + status.get("VmSwap", 0)
        self.identity(handle)  # the PID must still name the same process after the reads
        return {
            "workingSetBytes": status["VmRSS"] * 1024,
            "peakWorkingSetBytes": status.get("VmHWM", status["VmRSS"]) * 1024,
            "privateCommitBytes": private * 1024,
            "peakPrivateCommitBytes": None,
        }

    @staticmethod
    def _kilobytes(text: str) -> dict[str, int]:
        values = {}
        for line in text.splitlines():
            key, _, rest = line.partition(":")
            parts = rest.split()
            if len(parts) == 2 and parts[1] == "kB" and parts[0].isdigit():
                values[key.strip()] = int(parts[0])
        return values

    def identity_for(self, pid: int) -> dict[str, Any]:
        return self._identity_of(pid)

    def _executable(self, pid: int) -> str | None:
        try:
            return os.path.basename(os.readlink(self.proc / str(pid) / "exe"))
        except OSError:
            pass
        try:
            return self._read(pid, "comm").strip()
        except OSError:
            return None

    def java_processes(self) -> list[dict[str, Any]]:
        rows = []
        for entry in self.proc.iterdir():
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            executable = self._executable(pid)
            if executable not in JAVA_EXECUTABLES:
                continue
            try:
                rows.append({**self._identity_of(pid), "executable": executable})
            except ProcessGone:
                continue
        return sorted(rows, key=lambda row: row["pid"])

    def command_lines(self, pids: list[int]) -> dict[int, str]:
        lines = {}
        for pid in pids:
            try:
                raw = self._read(pid, "cmdline")
            except OSError:
                continue
            lines[pid] = " ".join(part for part in raw.split("\0") if part)
        return lines
