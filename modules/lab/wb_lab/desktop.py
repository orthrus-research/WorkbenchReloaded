"""A hidden desktop (Windows): processes started on it run and draw, but it never takes the input, so a session never
takes the user's focus, keyboard or mouse.

The desktop is made without the right to switch to it, and nothing here calls a desktop-switch, foreground or input
function; when it cannot be made, starting refuses rather than falling back to the user's desktop. Each process is
placed in a job object, so killing it kills everything it started, and its output goes to a log file.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

DESKTOP_ACCESS = 0xC7  # read/create objects, enumerate, write/read objects; not DESKTOP_SWITCHDESKTOP (0x100)
CREATE_UNICODE_ENVIRONMENT = 0x400
CREATE_SUSPENDED = 0x4
CREATE_NO_WINDOW = 0x08000000
STARTF_USESTDHANDLES, STARTF_FORCEOFFFEEDBACK, STARTF_USESHOWWINDOW = 0x100, 0x80, 0x1
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
WAIT_TIMEOUT = 0x102
STILL_ACTIVE = 259


def available() -> bool:
    return sys.platform == "win32"


if available():  # the Windows API, bound once
    import ctypes as C
    import msvcrt
    from ctypes import wintypes as W

    class STARTUPINFO(C.Structure):
        _fields_ = [("cb", W.DWORD), ("lpReserved", W.LPWSTR), ("lpDesktop", W.LPWSTR), ("lpTitle", W.LPWSTR),
                    ("dwX", W.DWORD), ("dwY", W.DWORD), ("dwXSize", W.DWORD), ("dwYSize", W.DWORD),
                    ("dwXCountChars", W.DWORD), ("dwYCountChars", W.DWORD), ("dwFillAttribute", W.DWORD),
                    ("dwFlags", W.DWORD), ("wShowWindow", W.WORD), ("cbReserved2", W.WORD),
                    ("lpReserved2", C.POINTER(W.BYTE)), ("hStdInput", W.HANDLE), ("hStdOutput", W.HANDLE),
                    ("hStdError", W.HANDLE)]

    class PROCESS_INFORMATION(C.Structure):
        _fields_ = [("hProcess", W.HANDLE), ("hThread", W.HANDLE), ("dwProcessId", W.DWORD),
                    ("dwThreadId", W.DWORD)]

    class IO_COUNTERS(C.Structure):
        _fields_ = [(name, C.c_ulonglong) for name in ("ReadOperationCount", "WriteOperationCount",
                                                         "OtherOperationCount", "ReadTransferCount",
                                                         "WriteTransferCount", "OtherTransferCount")]

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(C.Structure):
        _fields_ = [("PerProcessUserTimeLimit", C.c_longlong), ("PerJobUserTimeLimit", C.c_longlong),
                    ("LimitFlags", W.DWORD), ("MinimumWorkingSetSize", C.c_size_t),
                    ("MaximumWorkingSetSize", C.c_size_t), ("ActiveProcessLimit", W.DWORD),
                    ("Affinity", C.c_size_t), ("PriorityClass", W.DWORD), ("SchedulingClass", W.DWORD)]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(C.Structure):
        _fields_ = [("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION), ("IoInfo", IO_COUNTERS),
                    ("ProcessMemoryLimit", C.c_size_t), ("JobMemoryLimit", C.c_size_t),
                    ("PeakProcessMemoryUsed", C.c_size_t), ("PeakJobMemoryUsed", C.c_size_t)]

    _user = C.WinDLL("user32", use_last_error=True)
    _kernel = C.WinDLL("kernel32", use_last_error=True)
    for _lib, _name, _args, _result in [
        (_user, "CreateDesktopW", [W.LPCWSTR, W.LPCWSTR, C.c_void_p, W.DWORD, W.DWORD, C.c_void_p], W.HANDLE),
        (_user, "CloseDesktop", [W.HANDLE], W.BOOL),
        (_user, "OpenInputDesktop", [W.DWORD, W.BOOL, W.DWORD], W.HANDLE),
        (_user, "GetUserObjectInformationW", [W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.POINTER(W.DWORD)], W.BOOL),
        (_kernel, "CreateProcessW", [W.LPCWSTR, W.LPWSTR, C.c_void_p, C.c_void_p, W.BOOL, W.DWORD, C.c_void_p,
                                     W.LPCWSTR, C.POINTER(STARTUPINFO), C.POINTER(PROCESS_INFORMATION)], W.BOOL),
        (_kernel, "CreateJobObjectW", [C.c_void_p, W.LPCWSTR], W.HANDLE),
        (_kernel, "SetInformationJobObject", [W.HANDLE, C.c_int, C.c_void_p, W.DWORD], W.BOOL),
        (_kernel, "AssignProcessToJobObject", [W.HANDLE, W.HANDLE], W.BOOL),
        (_kernel, "TerminateJobObject", [W.HANDLE, W.UINT], W.BOOL),
        (_kernel, "ResumeThread", [W.HANDLE], W.DWORD),
        (_kernel, "WaitForSingleObject", [W.HANDLE, W.DWORD], W.DWORD),
        (_kernel, "GetExitCodeProcess", [W.HANDLE, C.POINTER(W.DWORD)], W.BOOL),
        (_kernel, "CloseHandle", [W.HANDLE], W.BOOL),
    ]:
        _fn = getattr(_lib, _name)
        _fn.argtypes, _fn.restype = _args, _result

    def _checked(value):
        if not value:
            raise C.WinError(C.get_last_error())
        return value

    def _desktop_name(handle) -> str:
        buffer, needed = C.create_unicode_buffer(512), W.DWORD()
        _checked(_user.GetUserObjectInformationW(handle, 2, buffer, C.sizeof(buffer), C.byref(needed)))
        return buffer.value

    def input_desktop() -> str | None:
        """The desktop that has the input now (None where a lock screen hides it)."""
        handle = _user.OpenInputDesktop(0, False, 1)
        if not handle:
            return None
        try:
            return _desktop_name(handle)
        finally:
            _user.CloseDesktop(handle)


class DesktopProcess:
    """A process on a hidden desktop, with the part of subprocess.Popen's surface a session uses."""

    def __init__(self, info, job, log: Path):
        self.pid = int(info.dwProcessId)
        self._process, self._job, self.log = info.hProcess, job, log
        self.returncode: int | None = None

    def poll(self) -> int | None:
        if self.returncode is None and _kernel.WaitForSingleObject(self._process, 0) != WAIT_TIMEOUT:
            code = W.DWORD()
            _checked(_kernel.GetExitCodeProcess(self._process, C.byref(code)))
            self.returncode = code.value
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        milliseconds = 0xFFFFFFFF if timeout is None else int(timeout * 1000)
        if _kernel.WaitForSingleObject(self._process, milliseconds) == WAIT_TIMEOUT:
            raise subprocess.TimeoutExpired(str(self.pid), timeout)
        return self.poll()

    def kill_tree(self):
        """Ends the process and everything it started (its job), then reaps it."""
        _kernel.TerminateJobObject(self._job, 1)
        try:
            self.wait(10)
        except subprocess.TimeoutExpired:
            pass

    def close(self):
        for handle in (self._process, self._job):
            if handle:
                _kernel.CloseHandle(handle)
        self._process = self._job = None


class HiddenDesktop:
    """``with HiddenDesktop() as desktop: desktop.start(args, cwd=..., env=..., log=...)``."""

    def __init__(self, prefix: str = "wb-hidden"):
        if not available():
            raise OSError("hidden desktops are a Windows feature")
        self.name = f"{prefix}-{uuid.uuid4().hex[:12]}"
        self._handle = None
        self.processes: list[DesktopProcess] = []
        self.input_seen: list[str] = []

    def __enter__(self) -> HiddenDesktop:
        self._handle = _checked(_user.CreateDesktopW(self.name, None, None, 0, DESKTOP_ACCESS, None))
        if _desktop_name(self._handle) != self.name:
            raise OSError(f"the desktop made is not {self.name}")
        self.observe()
        return self

    def observe(self):
        """Notes the desktop that has the input, so a run can show it never was this one."""
        current = input_desktop()
        if current and current not in self.input_seen:
            self.input_seen.append(current)

    @property
    def never_input(self) -> bool:
        return self.name not in self.input_seen

    def start(self, args: list, *, cwd, env: dict, log: Path) -> DesktopProcess:
        log.parent.mkdir(parents=True, exist_ok=True)
        info = PROCESS_INFORMATION()
        job = _checked(_kernel.CreateJobObjectW(None, None))
        limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        _checked(_kernel.SetInformationJobObject(job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS, C.byref(limits),
                                                 C.sizeof(limits)))
        block = C.create_unicode_buffer("".join(f"{k}={v}\0" for k, v in env.items()) + "\0")
        with log.open("wb", buffering=0) as output, open(os.devnull, "rb") as nothing:
            out_handle, in_handle = msvcrt.get_osfhandle(output.fileno()), msvcrt.get_osfhandle(nothing.fileno())
            os.set_handle_inheritable(out_handle, True)
            os.set_handle_inheritable(in_handle, True)
            try:
                startup = STARTUPINFO()
                startup.cb = C.sizeof(startup)
                startup.lpDesktop = "WinSta0\\" + self.name
                startup.dwFlags = STARTF_USESTDHANDLES | STARTF_FORCEOFFFEEDBACK | STARTF_USESHOWWINDOW
                startup.wShowWindow = 0
                startup.hStdInput, startup.hStdOutput, startup.hStdError = in_handle, out_handle, out_handle
                line = C.create_unicode_buffer(subprocess.list2cmdline([str(a) for a in args]))
                _checked(_kernel.CreateProcessW(None, line, None, None, True,
                                                CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT | CREATE_NO_WINDOW,
                                                block, str(cwd), C.byref(startup), C.byref(info)))
            finally:
                os.set_handle_inheritable(out_handle, False)
                os.set_handle_inheritable(in_handle, False)
        try:  # in its job before it runs a single instruction, so nothing it starts escapes the job
            _checked(_kernel.AssignProcessToJobObject(job, info.hProcess))
            _kernel.ResumeThread(info.hThread)
        except OSError:
            _kernel.TerminateJobObject(job, 1)
            raise
        finally:
            _kernel.CloseHandle(info.hThread)
        process = DesktopProcess(info, job, log)
        self.processes.append(process)
        self.observe()
        return process

    def __exit__(self, *exc):
        for process in self.processes:
            if process.poll() is None:
                process.kill_tree()
            process.close()
        if self._handle:
            _user.CloseDesktop(self._handle)
            self._handle = None
