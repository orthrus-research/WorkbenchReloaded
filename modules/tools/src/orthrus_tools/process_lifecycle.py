"""Observe one owned process at two endpoints: when it reports ready, and just before it quits.

Binding happens once, immediately after ``Popen``, against the process's creation identity, so a
later process that reuses the PID can never be measured by mistake. Memory is read only at the
``ready`` and ``endBeforeQuit`` endpoints and never from a log-reading thread or during timed
work. A failed read is recorded as unavailable with its reason; no zero is ever fabricated.

    lifecycle = ProcessLifecycle(process, ready_marker="Done (", label="server",
                                 launched_monotonic_ns=t0, launched_utc=utc0)
    for line in process.stdout:
        lifecycle.observe_line(line)          # cheap: a substring test, no OS calls
        if lifecycle.ready_observed: lifecycle.capture("ready")
    lifecycle.capture("endBeforeQuit")
    lifecycle.close()
    receipt = lifecycle.report()
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from .processes import default_api

SCHEMA = "orthrus.process-lifecycle.v1"
ENDPOINTS = ("ready", "endBeforeQuit")
ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")


class ProcessLifecycle:
    def __init__(
        self,
        process: Any,
        ready_marker: str,
        label: str = "process",
        launched_monotonic_ns: int | None = None,
        launched_utc: str | None = None,
        api_factory: Callable[[], Any] = default_api,
        clock: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        if not ready_marker:
            raise ValueError("ready_marker must be a non-empty log substring")
        self.process, self.label, self.marker, self.clock = process, label, ready_marker, clock
        self.launch_ns = clock() if launched_monotonic_ns is None else launched_monotonic_ns
        self.launch_utc = launched_utc or datetime.now(UTC).isoformat()
        self.pid = process.pid
        self.ready_ns: int | None = None
        self.handle: Any = None
        self.api: Any = None
        self.bound: dict[str, Any] | None = None
        self.error: str | None = None
        self.close_error: str | None = None
        self.endpoints: dict[str, dict[str, Any]] = {}
        self.closed = False
        try:
            if process.poll() is not None:
                raise OSError("owned process exited before lifecycle binding")
            self.api = api_factory()
            self.handle = self.api.open(process.pid)
            self.bound = self.api.identity(self.handle)
            created = str(self.bound.get("creationId", "")).rpartition(":")[2]
            if not created.isdigit() or int(created) <= 0:
                raise OSError("missing or invalid process creation identity")
            if self.bound["pid"] != process.pid or process.poll() is not None:
                raise OSError("owned process exited or identity changed during lifecycle binding")
        except Exception as error:
            self.error = str(error)
            self._close_handle()

    @property
    def ready_observed(self) -> bool:
        return self.ready_ns is not None

    def observe_line(self, line: str) -> None:
        if not self.closed and self.ready_ns is None and self.marker in ANSI_ESCAPE.sub("", line):
            self.ready_ns = self.clock()

    @staticmethod
    def same_identity(first: dict[str, Any], second: dict[str, Any] | None) -> bool:
        return second is not None and all(
            first[key] == second[key] for key in ("pid", "creationId")
        )

    def capture(self, endpoint: str) -> dict[str, Any]:
        """Read memory once for ``endpoint``; a second call returns the first result unchanged."""
        if endpoint not in ENDPOINTS:
            raise ValueError(f"unsupported lifecycle endpoint {endpoint!r}")
        if endpoint in self.endpoints:
            return self.endpoints[endpoint]
        started = self.clock()
        row: dict[str, Any] = {
            "available": False,
            "capturedUtc": datetime.now(UTC).isoformat(),
            "captureMonotonicOffsetNanos": started - self.launch_ns,
        }
        try:
            if self.closed:
                raise OSError("lifecycle handle already closed")
            if self.error is not None:
                raise OSError(self.error)
            if endpoint == "ready" and self.ready_ns is None:
                raise OSError("the ready marker was not observed")
            if self.process.poll() is not None:
                raise OSError("owned process exited before the endpoint; memory unavailable")
            before = self.api.identity(self.handle)
            if not self.same_identity(before, self.bound):
                raise OSError("owned process handle/creation identity mismatch")
            memory = self.api.memory(self.handle)
            after = self.api.identity(self.handle)
            if not self.same_identity(after, self.bound):
                raise OSError("owned process identity changed during the endpoint")
            if self.process.poll() is not None:
                raise OSError("owned process exited during the endpoint; memory unavailable")
            row.update(memory)
            row.update(after)
            row["available"] = True
        except Exception as error:
            row["failure"] = str(error)
            row["failureType"] = type(error).__name__
        row["observerReadNanos"] = self.clock() - started
        self.endpoints[endpoint] = row
        return row

    def _close_handle(self) -> None:
        if self.handle is not None:
            handle, self.handle = self.handle, None
            try:
                self.api.close(handle)
            except OSError as error:
                self.close_error = str(error)

    def close(self) -> None:
        if not self.closed:
            self._close_handle()
            self.closed = True
            self.process = None

    def report(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "platform": getattr(self.api, "platform", None),
            "pid": self.pid,
            "label": self.label,
            "launchMonotonicNanos": self.launch_ns,
            "launchUtc": self.launch_utc,
            "readyMarker": self.marker,
            "readyObserved": self.ready_ns is not None,
            "launchToReadyNanos": None if self.ready_ns is None else self.ready_ns - self.launch_ns,
            "boundIdentity": self.bound,
            "bindingAvailable": self.error is None,
            "bindingFailure": self.error,
            "endpoints": dict(self.endpoints),
            "observerClosed": self.closed,
            "handleClosed": self.closed and self.close_error is None,
            "closeFailure": self.close_error,
            "scope": (
                "Ready and pre-quit memory endpoints of one owned process, bound by creation "
                "identity. Peaks are the OS lifetime peak through each endpoint, not a sampled "
                "curve. Working set includes shared resident pages; private commit is not "
                "resident heap. Linux reports no peak private commit. No GC, heap or "
                "working-set intervention."
            ),
        }
