"""A game log's errors and warnings mapped back to script file:line.

GroovyScript names a script by its path with the extension dropped and / made . (postInit/main.groovy is
postInit.main). Its own log (groovy.log) names the running script on each line (``[CLIENT/ERROR]
[postInit.main]: ...``, ``postInit.main:12`` in debug) and says what ran, in order (`` - running script
postInit.main``); a game log (latest.log) has stack frames (``postInit.main.run(main.groovy:12)``) and the Groovy
compiler's errors (``postInit/main.groovy: 12: unexpected token``). An entry is a head line and the lines after
it that have no head of their own.
"""

from __future__ import annotations

import gzip
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

from .runconfig import SUFFIXES

_HEAD = re.compile(r"^\[[^\]]{1,40}\]\s*\[(?P<thread>[^\]]*?)/(?P<level>[A-Z]+)\]\s*(?:\[(?P<source>[^\]]*)\]\s*)?:"
                   r"?\s?(?P<message>.*)$")
_CONSOLE = re.compile(r"^\[(?P<time>[^\]]*?)\s(?P<level>TRACE|DEBUG|INFO|WARN|WARNING|ERROR|FATAL)\]:?\s?"
                      r"(?P<message>.*)$")
_EXTENSION = "|".join(re.escape(s[1:]) for s in SUFFIXES)
_MENTION = re.compile(r"(?P<file>[\w$./\\:%~-]+?\.(?:" + _EXTENSION + r"))\b(?!\.\w)"
                      r"(?:(?::\s?|\s+in\s+line\s+|\s*@\s*line\s+)(?P<line>\d+))?")
_FRAME = re.compile(r"(?P<class>[\w$.]+)\.[\w$<>]+\((?P<file>[^():\s]+):(?P<line>\d+)\)")
_CLASS = re.compile(r"^(?P<class>[\w$]+(?:\.[\w$]+)*)(?::(?P<line>\d+))?$")
_QUOTED = re.compile(r"'(?P<class>[\w$]+(?:\.[\w$]+)+)'")
_AT_LINE = re.compile(r"@ line (\d+)")
_RAN = re.compile(r"^\s*-\s+(?:running script|loading class)\s+(?P<name>\S+)\s*$")
_STAGE = re.compile(r"Running scripts in loader '(?P<stage>\w+)'")
LEVELS = {"WARNING": "WARN"}


@dataclass
class Entry:
    number: int  # the head's line in the log
    level: str
    source: str
    message: str
    lines: list[str] = field(default_factory=list)  # the lines that follow the head
    places: list[tuple[str, int | None]] = field(default_factory=list)  # script file and line, the innermost first
    unplaced: list[str] = field(default_factory=list)  # script mentions that match no file of the folder

    @property
    def groovy(self) -> bool:
        """Whether GroovyScript wrote it or it concerns a script."""
        return bool(self.places or self.unplaced) or "groovy" in self.source.lower()

    def place(self) -> str:
        if not self.places:
            return ""
        file, line = self.places[0]
        return f"{file}:{line}" if line else file


def lines(path: Path) -> Iterator[str]:
    opener = gzip.open if path.suffix.lower() == ".gz" else open
    with opener(path, "rt", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            yield line.rstrip("\r\n")


def class_name(path: str) -> str:
    """The class GroovyScript compiles a script to: its path, the extension dropped and / made '.'."""
    return path.rsplit(".", 1)[0].replace("/", ".")


class Resolver:
    """Maps what a log names to a file of the groovy folder: a script class, a path under the folder, or a file
    name only one file has (or one the entry has already placed)."""

    def __init__(self, files: list[str], folder: str):
        self.files = {f.casefold(): f for f in files}
        self.classes = {class_name(f): f for f in files}
        self.by_name: dict[str, list[str]] = {}
        for f in files:
            self.by_name.setdefault(f.rsplit("/", 1)[-1].casefold(), []).append(f)
        self.folder = folder.casefold()

    def script(self, name: str) -> str | None:
        return self.classes.get(name.split("$", 1)[0])

    def __call__(self, mention: str, near: list[str]) -> str | None:
        folded = unquote(mention).replace("\\", "/").casefold()
        for marker in ("/" + self.folder + "/", "/groovy/"):
            if marker in folded and folded.rsplit(marker, 1)[1] in self.files:
                return self.files[folded.rsplit(marker, 1)[1]]
        best = None
        for key, file in self.files.items():
            if (folded == key or folded.endswith("/" + key)) and (best is None or len(file) > len(best)):
                best = file
        if best:
            return best
        named = self.by_name.get(folded.rsplit("/", 1)[-1], [])
        preferred = [f for f in named if f in near]
        if len(preferred) == 1 or len(named) == 1:
            return (preferred or named)[0]
        return None


def read(path: Path, resolve: Resolver) -> tuple[list[Entry], list[tuple[str, str]], int]:
    """The log's warnings and errors, what ran in order ((loader, file) from groovy.log), and its line count."""
    entries: list[Entry] = []
    ran: list[tuple[str, str]] = []
    stage, count, current = "", 0, None
    for number, text in enumerate(lines(path), 1):
        count = number
        head = _HEAD.match(text) or _CONSOLE.match(text)
        if head is None:
            if current is not None:
                current.lines.append(text)
            continue
        groups = head.groupdict()
        level = LEVELS.get(groups["level"], groups["level"])
        current = Entry(number, level, (groups.get("source") or groups.get("thread") or "").strip(),
                        groups["message"])
        if level in ("WARN", "ERROR", "FATAL"):
            entries.append(current)
        switched = _STAGE.search(current.message)
        if switched:
            stage = switched.group("stage")
        done = _RAN.match(current.message)
        if done:
            ran.append((stage, resolve.script(done.group("name")) or done.group("name")))
    for entry in entries:
        _place(entry, resolve)
    return entries, ran, count


def _place(entry: Entry, resolve: Resolver) -> None:
    found: list[tuple[str, int | None]] = []

    def add(file: str, line: int | None) -> None:
        if (file, line) not in found:
            found.append((file, line))

    named = _CLASS.match(entry.source)
    if named and resolve.script(named.group("class")):
        add(resolve.script(named.group("class")), int(named.group("line")) if named.group("line") else None)
    for text in (entry.message, *entry.lines):
        framed = set()
        for frame in _FRAME.finditer(text):
            file = resolve.script(frame.group("class"))
            if file:
                add(file, int(frame.group("line")))
                framed.add(frame.group("file"))
        for quoted in _QUOTED.finditer(text):
            if resolve.script(quoted.group("class")):
                add(resolve.script(quoted.group("class")), None)
        for match in _MENTION.finditer(text):
            if match.group("file") in framed:
                continue
            file = resolve(match.group("file"), [f for f, _ in found])
            line = int(match.group("line")) if match.group("line") else None
            if line is None:
                at = _AT_LINE.search(text, match.end())
                line = int(at.group(1)) if at else None
            if file is None:
                if match.group("file") not in entry.unplaced:
                    entry.unplaced.append(match.group("file"))
            else:
                add(file, line)
    lined = {f for f, line in found if line}
    entry.places = [p for p in found if p[1]] + [p for p in found if not p[1] and p[0] not in lined]
