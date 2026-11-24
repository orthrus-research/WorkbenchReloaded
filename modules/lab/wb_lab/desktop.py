"""A hidden desktop (Windows) for a session's client: the game runs and draws on it, but the desktop never takes the
input, so a session never takes the person's focus, keyboard or mouse (lab.run ``desktop=hidden``).

The desktop is made without the right to switch to it, and nothing here calls a desktop-switch, foreground or input
function; when it cannot be made, the session refuses rather than fall back to the person's desktop. A process
started on it is placed in a job object before it runs, so killing it kills everything it started, and its console
comes back on a pipe, as ``subprocess.Popen``'s would: a session reads it the same way. The desktop notes which
desktop had the input each time it is asked (``observe``), and which windows were opened on it, by which process
(``windows``), so a session can record that the game was on it and that it never had the input.

A driver session (lab.run ``kind=driver desktop=hidden``) makes the desktop and gives the driver its name in
``WB_DESKTOP`` and this file's path in ``WB_DESKTOP_LAUNCHER``; the driver starts the client it runs there, either
with this file as a program, which needs nothing but the standard library::

    python -I $WB_DESKTOP_LAUNCHER --desktop $WB_DESKTOP [--cwd DIR] -- <client command line>

(the client's console comes back on its standard output and its exit status is the launcher's; a killed launcher
kills the client), or, from Python with this file's folder's parent on ``sys.path``,
``wb_lab.desktop.start_on(name, args, cwd=..., env=...)``. Only a trusted project's driver runs, and the desktop
lives as long as the session.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import uuid

DESKTOP_ACCESS = 0xC7  # read/create objects, enumerate, write/read objects; not DESKTOP_SWITCHDESKTOP (0x100)
CREATE_UNICODE_ENVIRONMENT = 0x400
CREATE_SUSPENDED = 0x4
CREATE_NO_WINDOW = 0x08000000
STARTF_USESTDHANDLES, STARTF_FORCEOFFFEEDBACK, STARTF_USESHOWWINDOW = 0x100, 0x80, 0x1
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
WAIT_TIMEOUT = 0x102
UOI_NAME = 2


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

    ENUM_WINDOWS = C.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)

    _user = C.WinDLL("user32", use_last_error=True)
    _kernel = C.WinDLL("kernel32", use_last_error=True)
    for _lib, _name, _args, _result in [
        (_user, "CreateDesktopW", [W.LPCWSTR, W.LPCWSTR, C.c_void_p, W.DWORD, W.DWORD, C.c_void_p], W.HANDLE),
        (_user, "CloseDesktop", [W.HANDLE], W.BOOL),
        (_user, "OpenInputDesktop", [W.DWORD, W.BOOL, W.DWORD], W.HANDLE),
        (_user, "GetUserObjectInformationW", [W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.POINTER(W.DWORD)], W.BOOL),
        (_user, "EnumDesktopWindows", [W.HANDLE, ENUM_WINDOWS, W.LPARAM], W.BOOL),
        (_user, "GetWindowThreadProcessId", [W.HWND, C.POINTER(W.DWORD)], W.DWORD),
        (_user, "GetClassNameW", [W.HWND, W.LPWSTR, C.c_int], C.c_int),
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
        _checked(_user.GetUserObjectInformationW(handle, UOI_NAME, buffer, C.sizeof(buffer), C.byref(needed)))
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


def environment_block(env: dict) -> str:
    """A CreateProcess environment block: ``name=value`` entries sorted by name without case, each ended by a NUL,
    and one more NUL."""
    return "".join(f"{k}={v}\0" for k, v in sorted(env.items(), key=lambda item: item[0].upper())) + "\0"


class DesktopProcess:
    """A process on a hidden desktop, with the part of ``subprocess.Popen``'s surface a session uses: ``pid``,
    ``returncode``, ``stdout`` (its console, text), ``stdin`` (none), ``poll``, ``wait`` and ``kill``."""

    def __init__(self, info, job, stdout):
        self.pid = int(info.dwProcessId)
        self._process, self._job = info.hProcess, job
        self.stdout = stdout
        self.stdin = None
        self.returncode: int | None = None

    def poll(self) -> int | None:
        if self.returncode is None and self._process and \
                _kernel.WaitForSingleObject(self._process, 0) != WAIT_TIMEOUT:
            code = W.DWORD()
            _checked(_kernel.GetExitCodeProcess(self._process, C.byref(code)))
            self.returncode = code.value
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is not None or not self._process:
            return self.returncode
        milliseconds = 0xFFFFFFFF if timeout is None else max(0, int(timeout * 1000))
        if _kernel.WaitForSingleObject(self._process, milliseconds) == WAIT_TIMEOUT:
            raise subprocess.TimeoutExpired(str(self.pid), timeout)
        return self.poll()

    def kill(self):
        """Ends the process and everything it started: its job."""
        if self._job:
            _kernel.TerminateJobObject(self._job, 1)

    def close(self):
        for handle in (self._process, self._job):
            if handle:
                _kernel.CloseHandle(handle)
        self._process = self._job = None


class HiddenDesktop:
    """``with HiddenDesktop() as desktop: desktop.start(args, cwd=..., env=...)``."""

    def __init__(self, prefix: str = "wb-session"):
        if not available():
            raise OSError("a hidden desktop is a Windows feature")
        self.name = f"{prefix}-{uuid.uuid4().hex[:12]}"
        self._handle = None
        self.processes: list[DesktopProcess] = []
        self.input_seen: list[str] = []
        self.input_unknown = False
        self._windows: dict[int, list[str]] = {}

    def __enter__(self) -> HiddenDesktop:
        self._handle = _checked(_user.CreateDesktopW(self.name, None, None, 0, DESKTOP_ACCESS, None))
        if _desktop_name(self._handle) != self.name:
            raise OSError(f"the desktop made is not {self.name}")
        self.observe()
        return self

    def observe(self):
        """Notes the desktop that has the input now and the windows opened on this desktop, by process: its own
        processes' and those a driver started on it by its name."""
        current = input_desktop()
        if current is None:
            self.input_unknown = True
        elif current not in self.input_seen:
            self.input_seen.append(current)
        if self._handle:

            @ENUM_WINDOWS
            def one(hwnd, _):
                owner = W.DWORD()
                _user.GetWindowThreadProcessId(hwnd, C.byref(owner))
                if owner.value:
                    name = C.create_unicode_buffer(256)
                    if _user.GetClassNameW(hwnd, name, len(name)):
                        seen = self._windows.setdefault(owner.value, [])
                        if name.value not in seen:
                            seen.append(name.value)
                return True
            _user.EnumDesktopWindows(self._handle, one, 0)  # enumerating this desktop is what shows a window is on it

    @property
    def never_input(self) -> bool:
        return self.name not in self.input_seen

    def windows(self, pid: int) -> list[str]:
        """The window classes ``pid`` opened on this desktop, as far as ``observe`` saw them."""
        return list(self._windows.get(pid, []))

    def record(self) -> dict:
        return {"name": self.name, "never_input": self.never_input, "input_seen": list(self.input_seen),
                **({"input_unknown": True} if self.input_unknown else {}),
                "windows": {str(pid): classes for pid, classes in self._windows.items()}}

    def start(self, args: list, *, cwd, env: dict) -> DesktopProcess:
        """Starts ``args`` on this desktop, hidden, in a job of its own, with its console on a pipe."""
        process = start_on(self.name, args, cwd=cwd, env=env)
        self.processes.append(process)
        self.observe()
        return process

    def __exit__(self, *exc):
        self.observe()
        for process in self.processes:
            if process.poll() is None:
                process.kill()
                try:
                    process.wait(10)
                except subprocess.TimeoutExpired:
                    pass
            process.close()
        if self._handle:
            _user.CloseDesktop(self._handle)
            self._handle = None


def start_on(desktop: str, args: list, *, cwd, env: dict) -> DesktopProcess:
    """Starts ``args`` on the desktop named ``desktop`` (one a session made, which this process may use), hidden, in a
    job of its own that kills everything it starts when it is closed, with its console on a pipe."""
    if not available():
        raise OSError("a hidden desktop is a Windows feature")
    info = PROCESS_INFORMATION()
    job = _checked(_kernel.CreateJobObjectW(None, None))
    limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    _checked(_kernel.SetInformationJobObject(job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS, C.byref(limits),
                                             C.sizeof(limits)))
    block = C.create_unicode_buffer(environment_block({str(k): str(v) for k, v in env.items()}))
    read_end, write_end = os.pipe()
    nothing = open(os.devnull, "rb")
    try:
        out_handle, in_handle = msvcrt.get_osfhandle(write_end), msvcrt.get_osfhandle(nothing.fileno())
        os.set_handle_inheritable(out_handle, True)
        os.set_handle_inheritable(in_handle, True)
        try:
            startup = STARTUPINFO()
            startup.cb = C.sizeof(startup)
            startup.lpDesktop = "WinSta0\\" + desktop
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
    except BaseException:
        os.close(read_end)
        _kernel.CloseHandle(job)
        raise
    finally:
        os.close(write_end)  # the child holds the only write end, so its exit ends the console
        nothing.close()
    try:  # in its job before it runs a single instruction, so nothing it starts escapes the job
        _checked(_kernel.AssignProcessToJobObject(job, info.hProcess))
        _kernel.ResumeThread(info.hThread)
    except OSError:
        _kernel.TerminateJobObject(job, 1)
        os.close(read_end)
        raise
    finally:
        _kernel.CloseHandle(info.hThread)
    stdout = open(read_end, "r", encoding="utf-8", errors="replace")
    return DesktopProcess(info, job, stdout)


def main(argv: list[str] | None = None) -> int:
    """The launcher a driver runs: ``desktop.py --desktop NAME [--cwd DIR] -- COMMAND...`` starts the command on the
    hidden desktop NAME, writes its console to standard output as it comes, and exits with its status."""
    parser = argparse.ArgumentParser(prog="desktop.py", description=main.__doc__)
    parser.add_argument("--desktop", default=os.environ.get("WB_DESKTOP", ""),
                        help="the hidden desktop's name (default: WB_DESKTOP)")
    parser.add_argument("--cwd", default=os.getcwd(), help="the command's working folder")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not args.desktop or not command:
        parser.error("give --desktop NAME (or WB_DESKTOP) and a command after --")
    try:
        process = start_on(args.desktop, command, cwd=args.cwd, env=dict(os.environ))
    except OSError as error:
        print(f"desktop.py: {command[0]} could not be started on desktop {args.desktop}: {error}", file=sys.stderr)
        return 127
    try:
        for line in process.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
        return process.wait()
    except KeyboardInterrupt:
        return 130
    finally:
        process.kill()  # a launcher that ends, however it ends, takes the client with it
        process.close()


if __name__ == "__main__":
    sys.exit(main())
