"""A tool's looping query run in the background, its lines turned into samples as they come.

The tool runs without a console window (on Windows a console program started from a windowless host would open
one, over the game it watches), and stopping it ends its whole process tree, so a wrapper on PATH goes with it.
"""

from __future__ import annotations

import collections
import os
import subprocess
import threading

HIDDEN = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def run(args: list[str], timeout: float = 30) -> tuple[int, str]:
    """A short query: (exit code, output with errors)."""
    done = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, **HIDDEN)
    return done.returncode, done.stdout + done.stderr


def end(process: subprocess.Popen):
    """Ends a process and what it started."""
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, timeout=10,
                           **HIDDEN)
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


class Sampler:
    """Runs ``command`` and feeds each line through ``parse`` (a line -> (epoch nanoseconds, sample), ValueError
    when it is not one). Samples go to ``sink`` as they come, and to ``samples``; after ``stop`` none is added."""

    def __init__(self, command: list[str], parse, sink=None):
        self.command, self.parse, self.sink = command, parse, sink
        self.samples: list[tuple[int, dict]] = []
        self.unread = 0                      # lines that were not samples
        self.examples: list[str] = []        # the first few of them
        self.errors = collections.deque(maxlen=6)  # the tool's last error lines
        self.process: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._stopped = False
        self._threads: list[threading.Thread] = []

    def start(self) -> "Sampler":
        self.process = subprocess.Popen(self.command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                                        **HIDDEN)
        self._threads = [threading.Thread(target=self._read, name="gpu-sampler", daemon=True),
                         threading.Thread(target=self._drain, name="gpu-sampler-errors", daemon=True)]
        for thread in self._threads:
            thread.start()
        return self

    def _read(self):
        with self.process.stdout as stream:
            for line in stream:
                line = line.strip()
                if not line:
                    continue
                try:
                    sample = self.parse(line)
                except ValueError as error:
                    with self._lock:
                        if self._stopped:
                            return
                        self.unread += 1
                        if len(self.examples) < 3:
                            self.examples.append(f"{line[:120]} ({error})")
                    continue
                with self._lock:
                    if self._stopped:
                        return
                    self.samples.append(sample)
                    if self.sink:
                        self.sink(sample[1])

    def _drain(self):
        with self.process.stderr as stream:
            for line in stream:
                if line.strip():
                    self.errors.append(line.strip())

    @property
    def count(self) -> int:
        with self._lock:
            return len(self.samples)

    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def stop(self) -> int | None:
        """Stops the tool; returns its exit code when it had already exited by itself (else None)."""
        code = self.process.poll()
        if code is None:
            with self._lock:
                self._stopped = True  # nothing after this instant counts
            end(self.process)
        for thread in self._threads:
            thread.join(timeout=3)  # a tool that exited by itself still has its last lines read
        with self._lock:
            self._stopped = True
        return code
