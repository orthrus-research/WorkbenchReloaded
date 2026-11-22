"""Exclude timing runs that overlap other Java workloads. Read-only: never stops a process.

A timing run on a shared workstation is only meaningful when nothing else heavy is running. The
guard polls the machine's Java processes while a run is live and records every overlap:

- a Java process the run did not start (``foreign-java``) excludes the run;
- a resident daemon that was already idle before the run (by default a Gradle daemon) may stay,
  but if it uses more than ``daemon_cpu_fraction`` of one core over an interval
  (``active-resident-daemon``) the run is excluded;
- an observation that fails (``observation-failed``) or a platform without a backend
  (``observer-unavailable``) excludes the run: an unreadable machine is not an idle machine.

Typical use around ``subprocess.Popen``::

    guard = JavaWorkloadGuard()
    guard.start()                      # refuses if the machine is not clean right now
    with guard.launch():               # no observation can run between Popen and register
        process = subprocess.Popen(command)
        guard.register(process)
    ...                                # the run
    guard.close()
    report = guard.report()            # report["passed"] says whether the timing stands

From the command line, ``orthrus-tools workload-guard`` prints the Java processes running now
and exits 1 when any is not a known resident daemon, as a quick "is it safe to start" check.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from . import cli
from .cli import ENVIRONMENT, FINDING, OK, Output
from .processes import ProcessGone, default_api

SCHEMA = "orthrus.java-workload-guard.v1"
# A known resident daemon is recognised by one exact token of its command line.
DEFAULT_RESIDENT_TOKENS = ("org.gradle.launcher.daemon.bootstrap.GradleDaemon",)


class JavaWorkloadGuard:
    """Polls Java processes every ``interval_seconds``; owned processes are keyed by creation identity."""

    def __init__(
        self,
        api_factory: Callable[[], Any] = default_api,
        clock: Callable[[], int] = time.monotonic_ns,
        interval_seconds: float = 5,
        resident_tokens: tuple[str, ...] = DEFAULT_RESIDENT_TOKENS,
        daemon_cpu_fraction: float = 0.1,
        daemon_cpu_allowance_nanos: int = 100_000_000,
    ) -> None:
        self.clock = clock
        self.interval_seconds = interval_seconds
        self.resident_tokens = resident_tokens
        self.daemon_cpu_fraction = daemon_cpu_fraction
        self.daemon_cpu_allowance_nanos = daemon_cpu_allowance_nanos
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.api: Any = None
        self.owned: set[tuple[int, str]] = set()
        self.residents: dict[tuple[int, str], dict[str, Any]] = {}
        self.previous: dict[tuple[int, str], tuple[int, int]] = {}
        self.observations: list[dict[str, Any]] = []
        self.issues: list[dict[str, Any]] = []
        self.closed = False
        try:
            self.api = api_factory()
            self.residents = {self.key(row): row for row in self.resident_candidates()}
        except Exception as error:
            self.issue("observer-unavailable", str(error))

    @staticmethod
    def key(row: dict[str, Any]) -> tuple[int, str]:
        return int(row["pid"]), str(row["creationId"])

    def resident_candidates(self) -> list[dict[str, Any]]:
        """Resident daemons present before the run. Reads command lines once, never while timing."""
        before = {row["pid"]: row["creationId"] for row in self.api.java_processes()}
        candidates = []
        for pid, text in self.api.command_lines(list(before)).items():
            if not any(token in text.split() for token in self.resident_tokens):
                continue
            try:
                identity = self.api.identity_for(pid)
            except ProcessGone:
                continue
            if before.get(identity["pid"]) != identity["creationId"]:
                continue  # the PID was reused between the two reads
            candidates.append(
                {
                    **identity,
                    "kind": "resident-daemon",
                    "commandSha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                }
            )
        return candidates

    def issue(self, kind: str, detail: str, identity: dict[str, Any] | None = None) -> None:
        record = {"kind": kind, "detail": detail, "identity": identity}
        if record not in self.issues:
            self.issues.append(record)

    def observe(self) -> dict[str, Any]:
        with self.lock:
            if self.closed:
                raise RuntimeError("Java workload guard already closed")
            started = self.clock()
            observation: dict[str, Any] = {
                "utc": datetime.now(UTC).isoformat(),
                "monotonicNanos": started,
                "java": [],
            }
            try:
                if self.api is None:
                    raise RuntimeError("Java inventory unavailable")
                for row in self.api.java_processes():
                    key = self.key(row)
                    cpu = row["kernelCpuNanos"] + row["userCpuNanos"]
                    if key in self.owned:
                        role = "owned"
                    elif key in self.residents:
                        role = "resident-daemon"
                    else:
                        role = "foreign"
                    current = {**row, "role": role}
                    observation["java"].append(current)
                    identity = {"pid": key[0], "creationId": key[1]}
                    if role == "foreign":
                        self.issue(
                            "foreign-java", "unowned Java process overlaps the run", identity
                        )
                    elif role == "resident-daemon" and key in self.previous:
                        previous_time, previous_cpu = self.previous[key]
                        elapsed, delta = started - previous_time, cpu - previous_cpu
                        current.update(cpuDeltaNanos=delta, observationIntervalNanos=elapsed)
                        allowance = max(
                            self.daemon_cpu_allowance_nanos, int(elapsed * self.daemon_cpu_fraction)
                        )
                        if delta < 0 or elapsed <= 0:
                            self.issue(
                                "invalid-cpu-observation", "non-monotonic CPU counter", identity
                            )
                        elif delta > allowance:
                            self.issue(
                                "active-resident-daemon",
                                f"resident daemon used more than {self.daemon_cpu_fraction:.0%} "
                                "of one core over an interval",
                                identity,
                            )
                    self.previous[key] = started, cpu
            except Exception as error:
                observation["error"] = str(error)
                self.issue("observation-failed", str(error))
            observation["observerReadNanos"] = self.clock() - started
            self.observations.append(observation)
            return observation

    @contextmanager
    def launch(self) -> Iterator[None]:
        """Hold the lock from Popen to register, so no poll can see the new process as foreign."""
        with self.lock:
            self.require_clean()
            yield

    def register(self, process: Any) -> None:
        with self.lock:
            try:
                identity = self.api.identity_for(process.pid)
                if process.poll() is not None:
                    raise RuntimeError("owned process exited during guard registration")
                self.owned.add(self.key(identity))
            except Exception as error:
                # The caller must still keep this Popen for cleanup before require_clean raises.
                self.issue("owned-registration-failed", str(error))

    def start(self) -> None:
        self.observe()
        self.require_clean()
        if self.thread is not None:
            raise RuntimeError("Java workload observer already started")

        def monitor() -> None:
            while not self.stop_event.wait(self.interval_seconds):
                self.observe()

        self.thread = threading.Thread(target=monitor, name="java-workload-guard", daemon=True)
        self.thread.start()

    def require_clean(self) -> None:
        with self.lock:
            if self.issues:
                first = self.issues[0]
                raise RuntimeError(
                    f"run excluded by the Java workload guard: {first['kind']}: {first['detail']}"
                )

    def close(self) -> None:
        if self.closed:
            return
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join()
        with self.lock:
            self.observe()
            self.closed = True

    def report(self) -> dict[str, Any]:
        with self.lock:
            return {
                "schema": SCHEMA,
                "platform": getattr(self.api, "platform", None),
                "passed": not self.issues,
                "policy": {
                    "intervalSeconds": self.interval_seconds,
                    "residentTokens": list(self.resident_tokens),
                    "daemonCpuFractionOfOneCore": self.daemon_cpu_fraction,
                    "daemonCpuMinimumAllowanceNanos": self.daemon_cpu_allowance_nanos,
                },
                "ownedIdentities": [
                    {"pid": pid, "creationId": created} for pid, created in sorted(self.owned)
                ],
                "residentDaemons": list(self.residents.values()),
                "issues": list(self.issues),
                "observations": list(self.observations),
                "closed": self.closed,
                "scope": (
                    "Read-only snapshots of Java process identities and CPU counters. Foreign JVMs, "
                    "active resident daemons and observer errors exclude the run. No process is "
                    "stopped or reprioritized. Polling can miss foreign processes shorter than one "
                    "interval; non-Java workloads and GPU contention are not excluded. A passing "
                    "guard is not proof of an otherwise idle host."
                ),
            }


def build_parser() -> cli.Parser:
    return cli.parser(
        "orthrus-workload-guard",
        "List running Java processes; exit 1 when any is not a known resident daemon.",
    )


def run(args: argparse.Namespace, out: Output) -> int:
    guard = JavaWorkloadGuard()
    guard.observe()
    guard.closed = True
    report = guard.report()
    out.result = report
    for observation in report["observations"][:1]:
        for row in observation.get("java", []):
            out.line(f"{row['role']:16} pid {row['pid']:>7}  {row.get('executable', '')}")
    for issue in report["issues"]:
        out.warn(f"{issue['kind']}: {issue['detail']}")
    out.line("clean" if report["passed"] else "not clean")
    if any(issue["kind"] == "observer-unavailable" for issue in report["issues"]):
        return ENVIRONMENT
    return OK if report["passed"] else FINDING


def main(argv: list[str] | None = None) -> int:
    return cli.run("workload-guard", build_parser, run, argv)


if __name__ == "__main__":
    sys.exit(main())
