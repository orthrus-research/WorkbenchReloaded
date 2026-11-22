"""Reading a JFR recording: its time span from the chunk headers, and its events through the JDK's own reader.

The JDK's ``jfr print`` cannot select by time and expands every stack frame's class, loader and module, so
a long recording prints gigabytes. ``java/JfrExport.java`` (run with the JDK the command requires) filters
by event type and time range first and writes compact JSON lines, with methods, stacks and threads as
tables written once. ``Stream`` decodes those lines; it takes any iterable of lines, so tests feed it
synthetic ones.
"""

from __future__ import annotations

import collections
import json
import os
import struct
import subprocess
import threading
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

EXPORTER = Path(__file__).resolve().parent / "java" / "JfrExport.java"
MAGIC = b"FLR\0"
HEADER = struct.Struct(">4sHHqqqqqqqi")  # magic, major, minor, size, pool, metadata, start ns, duration ns, ticks...
ANYTIME = ("jdk.JVMInformation", "jdk.CPUInformation")  # written at chunk start, kept whatever the window


@dataclass(frozen=True)
class Span:
    """A recording's extent from its chunk headers, in epoch nanoseconds."""
    start: int
    end: int
    chunks: int
    version: str

    @property
    def seconds(self) -> float:
        return (self.end - self.start) / 1e9


def span(path: Path) -> Span:
    """Reads every chunk header (68 bytes each); ValueError when the file is not a finished recording."""
    size = path.stat().st_size
    start = end = None
    chunks, offset, version = 0, 0, ""
    with open(path, "rb") as handle:
        while offset < size:
            handle.seek(offset)
            raw = handle.read(HEADER.size)
            if len(raw) < HEADER.size:
                raise ValueError(f"{path.name}: a chunk header is cut short at byte {offset}")
            magic, major, minor, chunk, _pool, _meta, began, lasted, *_ = HEADER.unpack(raw)
            if magic != MAGIC:
                raise ValueError(f"{path.name} is not a JFR recording" if offset == 0
                                 else f"{path.name}: no chunk header at byte {offset}")
            if chunk <= 0:
                raise ValueError(f"{path.name}: chunk {chunks + 1} is still being written")
            version = version or f"{major}.{minor}"
            start = began if start is None else min(start, began)
            end = began + lasted if end is None else max(end, began + lasted)
            chunks += 1
            offset += chunk
    if not chunks:
        raise ValueError(f"{path.name} is empty")
    return Span(start=start, end=end, chunks=chunks, version=version)


@dataclass(frozen=True)
class Thread:
    name: str
    java_id: int
    os_id: int


class Event:
    """One exported event: times in epoch nanoseconds, its thread and stack as table IDs."""
    __slots__ = ("type", "start", "duration", "thread", "stack", "values")

    def __init__(self, type: str, start: int, duration: int, thread: int | None, stack: int | None,
                 values: dict):
        self.type, self.start, self.duration = type, start, duration
        self.thread, self.stack, self.values = thread, stack, values

    @property
    def end(self) -> int:
        return self.start + self.duration

    def __repr__(self) -> str:
        return f"Event({self.type}, t={self.start}, d={self.duration})"


@dataclass
class Stream:
    """Decodes the exporter's lines. Iterating yields events; table rows fill ``methods`` (ID -> name),
    ``stacks`` (ID -> method IDs, leaf first) and ``threads`` as they arrive; the last row sets ``totals``."""
    lines: Iterable[str]
    methods: dict[int, str] = field(default_factory=dict)
    stacks: dict[int, tuple[int, ...]] = field(default_factory=dict)
    truncated: set[int] = field(default_factory=set)
    threads: dict[int, Thread] = field(default_factory=dict)
    totals: dict | None = None

    def __iter__(self) -> Iterator[Event]:
        for number, line in enumerate(self.lines, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                if "e" in row:
                    yield Event(row["e"], row["t"], row.get("d", 0), row.get("h"), row.get("s"), row.get("v") or {})
                elif "f" in row:
                    self.stacks[row["s"]] = tuple(row["f"])
                    if row.get("x"):
                        self.truncated.add(row["s"])
                elif "m" in row:
                    self.methods[row["m"]] = row["n"]
                elif "end" in row:
                    self.totals = row["end"]
                elif "h" in row:
                    self.threads[row["h"]] = Thread(row["n"], row.get("j", -1), row.get("o", -1))
                else:
                    raise ValueError("unknown row")
            except (ValueError, KeyError, TypeError) as error:
                raise ValueError(f"export line {number} is malformed ({error}): {line[:120]}") from None
        if self.totals is None:
            raise ValueError("the export ended before its totals: the recording could not be read to the end")

    def frames(self, stack: int | None) -> tuple[str, ...]:
        """A stack's frames as method names, leaf first."""
        if stack is None:
            return ()
        return tuple(self.methods.get(m, "(unknown)") for m in self.stacks.get(stack, ()))

    def thread_name(self, thread: int | None) -> str:
        found = self.threads.get(thread) if thread is not None else None
        return found.name if found else "(unknown thread)"


def java_of(home: Path) -> Path:
    return Path(home) / "bin" / ("java.exe" if os.name == "nt" else "java")


def export(java: Path, recording: Path, start: int, end: int, types: Iterable[str],
           anytime: Iterable[str] = ANYTIME, cancelled: threading.Event | None = None,
           scratch: Path | None = None) -> Iterator[str]:
    """Runs the exporter and yields its lines. Cancelling kills it (InterruptedError at the end); a failed
    export raises RuntimeError with the tail of its error output."""
    args = [str(java), "-XX:-UsePerfData"]
    if scratch is not None:
        args.append(f"-Djava.io.tmpdir={scratch}")
    args += [str(EXPORTER), str(recording), str(start), str(end), ",".join(types), ",".join(anytime)]
    process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                               text=True, encoding="utf-8", errors="replace")
    errors: collections.deque[str] = collections.deque(maxlen=12)

    def drain():
        for line in process.stderr:
            errors.append(line.rstrip())

    def watch():
        while process.poll() is None:
            if cancelled.wait(0.5):
                process.kill()
                return

    drainer = threading.Thread(target=drain, daemon=True)
    drainer.start()
    if cancelled is not None:
        threading.Thread(target=watch, daemon=True).start()
    try:
        yield from process.stdout
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        drainer.join(timeout=5)
        process.stdout.close()
        process.stderr.close()
    if cancelled is not None and cancelled.is_set():
        raise InterruptedError("cancelled")
    if process.returncode:
        raise RuntimeError(f"the JFR exporter exited {process.returncode}: " + " | ".join(errors))
