"""Read-only process identity, CPU and memory observation on Windows and Linux.

Every backend offers the same small interface, used by the workload guard and the process
lifecycle observer:

- ``open(pid)`` returns a handle bound to that process, ``close(handle)`` releases it.
- ``identity(handle)`` returns ``pid``, ``creationId`` (a string that differs between two
  processes that reused one PID), ``creationClock`` (what ``creationId`` counts) and the
  ``kernelCpuNanos`` / ``userCpuNanos`` counters.
- ``memory(handle)`` returns ``workingSetBytes``, ``peakWorkingSetBytes``,
  ``privateCommitBytes`` and ``peakPrivateCommitBytes`` (``None`` where the OS keeps no peak).
- ``java_processes()`` lists the identity of every running ``java``/``javaw`` process, and
  ``command_lines(pids)`` returns their command lines where the OS lets us read them.

No backend writes to, signals, suspends or reprioritizes any process. A process that exits
between two calls raises ``ProcessGone``; any other failure raises ``OSError`` so callers can
fail closed rather than mistake an unreadable machine for an idle one.
"""

from __future__ import annotations

import sys


class ProcessGone(OSError):
    """The process exited (or its PID was never valid) before it could be read."""


def default_api():
    """The backend for this platform, or ``OSError`` where none exists (macOS, for now)."""
    if sys.platform == "win32":
        from .windows import WindowsProcessApi

        return WindowsProcessApi()
    if sys.platform.startswith("linux"):
        from .linux import LinuxProcessApi

        return LinuxProcessApi()
    raise OSError(f"no process observation backend for platform {sys.platform!r}")


__all__ = ["ProcessGone", "default_api"]
