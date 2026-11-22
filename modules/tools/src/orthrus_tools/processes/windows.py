"""Windows backend: documented, read-only Win32 calls through ctypes.

Handles are opened with ``PROCESS_QUERY_LIMITED_INFORMATION`` only: no write, VM-read or
terminate rights. Memory counters are pointer-sized, so 32- and 64-bit Python both read them
correctly. ``creationId`` is the process creation FILETIME in 100 ns units.
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
from typing import Any

from . import ProcessGone

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
ERROR_INVALID_PARAMETER = 87  # OpenProcess on a PID that no longer exists
ERROR_NO_MORE_FILES = 18
TH32CS_SNAPPROCESS = 2
JAVA_EXECUTABLES = ("java.exe", "javaw.exe")


class FileTime(ctypes.Structure):
    _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]

    def value(self) -> int:
        return self.low | self.high << 32


class ProcessMemoryCountersEx(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_uint32), ("pageFaultCount", ctypes.c_uint32)] + [
        (name, ctypes.c_size_t)
        for name in (
            "peakWorkingSetSize",
            "workingSetSize",
            "quotaPeakPagedPoolUsage",
            "quotaPagedPoolUsage",
            "quotaPeakNonPagedPoolUsage",
            "quotaNonPagedPoolUsage",
            "pagefileUsage",
            "peakPagefileUsage",
            "privateUsage",
        )
    ]


class ProcessEntry(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_uint32),
        ("cntUsage", ctypes.c_uint32),
        ("th32ProcessID", ctypes.c_uint32),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", ctypes.c_uint32),
        ("cntThreads", ctypes.c_uint32),
        ("th32ParentProcessID", ctypes.c_uint32),
        ("pcPriClassBase", ctypes.c_int32),
        ("dwFlags", ctypes.c_uint32),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


class WindowsProcessApi:
    platform = "windows"

    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("the Windows process API is unavailable on this platform")
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined,unused-ignore]
        self.psapi = ctypes.WinDLL("psapi", use_last_error=True)  # type: ignore[attr-defined,unused-ignore]
        handle, dword, boolean = ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int32
        k = self.kernel
        k.OpenProcess.argtypes = [dword, boolean, dword]
        k.OpenProcess.restype = handle
        k.CloseHandle.argtypes = [handle]
        k.CloseHandle.restype = boolean
        k.GetProcessId.argtypes = [handle]
        k.GetProcessId.restype = dword
        k.GetProcessTimes.argtypes = [handle] + [ctypes.POINTER(FileTime)] * 4
        k.GetProcessTimes.restype = boolean
        k.CreateToolhelp32Snapshot.argtypes = [dword, dword]
        k.CreateToolhelp32Snapshot.restype = handle
        for name in ("Process32FirstW", "Process32NextW"):
            method = getattr(k, name)
            method.argtypes = [handle, ctypes.POINTER(ProcessEntry)]
            method.restype = boolean
        self.psapi.GetProcessMemoryInfo.argtypes = [
            handle,
            ctypes.POINTER(ProcessMemoryCountersEx),
            dword,
        ]
        self.psapi.GetProcessMemoryInfo.restype = boolean

    @staticmethod
    def failure(operation: str) -> OSError:
        code = ctypes.get_last_error()  # type: ignore[attr-defined,unused-ignore]
        message = operation + ": " + ctypes.FormatError(code).strip()  # type: ignore[attr-defined,unused-ignore]
        if code == ERROR_INVALID_PARAMETER and operation == "OpenProcess":
            return ProcessGone(code, message)
        return OSError(code, message)

    def open(self, pid: int):
        handle = self.kernel.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            raise self.failure("OpenProcess")
        return handle

    def close(self, handle) -> None:
        if not self.kernel.CloseHandle(handle):
            raise self.failure("CloseHandle")

    def identity(self, handle) -> dict[str, Any]:
        pid = self.kernel.GetProcessId(handle)
        if not pid:
            raise self.failure("GetProcessId")
        created, exited, kernel, user = FileTime(), FileTime(), FileTime(), FileTime()
        if not self.kernel.GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            raise self.failure("GetProcessTimes")
        # A running process's exit FILETIME is undefined and deliberately never reported.
        return {
            "pid": int(pid),
            "creationId": str(created.value()),
            "creationClock": "windows-filetime-100ns",
            "kernelCpuNanos": kernel.value() * 100,
            "userCpuNanos": user.value() * 100,
        }

    def memory(self, handle) -> dict[str, Any]:
        counters = ProcessMemoryCountersEx()
        counters.cb = ctypes.sizeof(counters)
        if not self.psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
            raise self.failure("GetProcessMemoryInfo")
        return {
            "workingSetBytes": int(counters.workingSetSize),
            "peakWorkingSetBytes": int(counters.peakWorkingSetSize),
            "privateCommitBytes": int(counters.privateUsage),
            "peakPrivateCommitBytes": int(counters.peakPagefileUsage),
        }

    def identity_for(self, pid: int) -> dict[str, Any]:
        handle = self.open(pid)
        try:
            return self.identity(handle)
        finally:
            self.close(handle)

    def java_processes(self) -> list[dict[str, Any]]:
        snapshot = self.kernel.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snapshot == ctypes.c_void_p(-1).value:
            raise self.failure("CreateToolhelp32Snapshot")
        rows = []
        try:
            entry = ProcessEntry()
            entry.dwSize = ctypes.sizeof(entry)
            more = self.kernel.Process32FirstW(snapshot, ctypes.byref(entry))
            while more:
                if entry.szExeFile.lower() in JAVA_EXECUTABLES:
                    try:
                        identity = self.identity_for(entry.th32ProcessID)
                        rows.append({**identity, "executable": entry.szExeFile})
                    except ProcessGone:
                        pass  # exited between enumeration and OpenProcess; denial is not absence
                more = self.kernel.Process32NextW(snapshot, ctypes.byref(entry))
            if ctypes.get_last_error() != ERROR_NO_MORE_FILES:  # type: ignore[attr-defined,unused-ignore]
                raise self.failure("Process32NextW")
            return rows
        finally:
            self.close(snapshot)

    def command_lines(self, pids: list[int]) -> dict[int, str]:
        """Command lines through CIM. Slow (a PowerShell start): call before a run, never during."""
        if not pids:
            return {}
        names = " OR ".join(f"name='{name}'" for name in JAVA_EXECUTABLES)
        command = (
            "$ErrorActionPreference='Stop'; @(Get-CimInstance Win32_Process -Filter "
            f'"{names}" | Select-Object ProcessId,CommandLine) | ConvertTo-Json -Compress'
        )
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        rows = json.loads(result.stdout) if result.stdout.strip() else []
        if isinstance(rows, dict):
            rows = [rows]
        wanted = set(pids)
        return {
            int(row["ProcessId"]): row.get("CommandLine") or ""
            for row in rows
            if int(row["ProcessId"]) in wanted
        }
