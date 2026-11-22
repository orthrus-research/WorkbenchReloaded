"""Loop and out-parameter shapes that some drivers have been seen to run differently from the text.

A lookup once used its ``out int layer`` parameter as the counter of a loop it returned from, and the driver handed
back one more than the value at the return. These shapes are found in the source text (comments ignored):

    borrowed-counter   error    a for loop whose counter is not declared in the loop itself
    out-in-loop        review   a function returns from inside a loop after writing an out or inout parameter
                                inside that loop
    out-unwritten      review   an out parameter that some return can be reached without assigning

A review-level shape can be sound; one that was reviewed is named ``file:function`` and is then reported as reviewed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

QUALIFIER = r"(?:(?:lowp|mediump|highp|precise)\s+)?"
TYPE = r"(?:void|bool|int|uint|float|double|[biud]?vec[234]|d?mat[234](?:x[234])?|[A-Z]\w*)"
FUNCTION = re.compile(r"\b" + QUALIFIER + TYPE + r"\s+([A-Za-z_]\w*)\s*\(([^()]*)\)\s*\{")
FOR = re.compile(r"\bfor\s*\(([^;]*);")
DECLARATION = re.compile(r"^(?:const\s+)?" + QUALIFIER + r"(?:bool|int|uint|float|double|[biud]?vec[234])\s+[A-Za-z_]\w*")
OUTS = re.compile(r"\b(?:out|inout)\s+" + QUALIFIER + r"\w+\s+([A-Za-z_]\w*)")
PURE_OUTS = re.compile(r"\bout\s+" + QUALIFIER + r"\w+\s+([A-Za-z_]\w*)")
LEVELS = {"borrowed-counter": "error", "out-in-loop": "review", "out-unwritten": "review"}


@dataclass
class Hazard:
    kind: str
    file: str
    line: int
    function: str
    detail: str
    reviewed: bool = False

    @property
    def level(self) -> str:
        return LEVELS[self.kind]

    def __str__(self) -> str:
        where = f"{self.file}:{self.line}" + (f" {self.function}" if self.function else "")
        return f"{self.kind} at {where}: {self.detail}"

    def as_dict(self) -> dict:
        return {"kind": self.kind, "level": self.level, "file": self.file, "line": self.line,
                "function": self.function, "detail": self.detail, "reviewed": self.reviewed}


def blank_comments(text: str) -> str:
    """The text with every comment replaced by spaces, newlines kept, so offsets and line numbers still hold."""
    def blank(match):
        return re.sub(r"[^\n]", " ", match.group())
    return re.sub(r"/\*.*?\*/|//[^\n]*", blank, text, flags=re.S)


def _close(text: str, start: int, opening: str, closing: str) -> int:
    depth = 0
    for index in range(start, len(text)):
        if text[index] == opening:
            depth += 1
        elif text[index] == closing:
            depth -= 1
            if depth == 0:
                return index
    return len(text) - 1


def _loops(body: str) -> list[tuple[int, int]]:
    """Spans of every for and while loop body (a block, or one statement) in a function body."""
    spans = []
    for match in re.finditer(r"\b(?:for|while)\s*\(", body):
        end = _close(body, match.end() - 1, "(", ")")
        start = end + 1
        while start < len(body) and body[start].isspace():
            start += 1
        if start < len(body) and body[start] == "{":
            spans.append((start, _close(body, start, "{", "}")))
        else:
            stop = body.find(";", start)
            spans.append((start, stop if stop >= 0 else len(body)))
    return spans


def _line(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def scan(text: str, file: str) -> list[Hazard]:
    text = blank_comments(text)
    found = []
    for match in FOR.finditer(text):
        init = match.group(1).strip()
        if init and not DECLARATION.match(init):
            found.append(Hazard("borrowed-counter", file, _line(text, match.start()), "", f"for ({init};"))
    for match in FUNCTION.finditer(text):
        name, parameters = match.group(1), match.group(2)
        outs = OUTS.findall(parameters)
        if not outs:
            continue
        start = match.end() - 1
        body = text[start:_close(text, start, "{", "}") + 1]
        line = _line(text, match.start())
        for begin, end in _loops(body):
            inside = body[begin:end + 1]
            if re.search(r"\breturn\b", inside):
                written = [o for o in outs if re.search(r"\b" + re.escape(o) + r"\b(?:\s*\[[^\]]*\])?(?:\.\w+)?\s*"
                                                         r"[-+*/]?=(?!=)", inside)]
                if written:
                    found.append(Hazard("out-in-loop", file, line, name,
                                        "writes " + ", ".join(written) + " in a loop it returns from"))
        for out in PURE_OUTS.findall(parameters):
            assigned = re.search(r"\b" + re.escape(out) + r"\b(?:\s*\[[^\]]*\])?(?:\.\w+)?\s*=(?!=)", body)
            first_return = re.search(r"\breturn\b", body)
            passed_on = re.search(r"\w+\s*\([^;]*\b" + re.escape(out) + r"\b[^;]*\)\s*[;)]", body)
            if first_return and (assigned is None or first_return.start() < assigned.start()) and not (
                    passed_on and passed_on.start() < first_return.start()):
                found.append(Hazard("out-unwritten", file, line, name, f"a return precedes the first assignment of {out}"))
    return found


def lint(files: dict[str, str], reviewed=()) -> list[Hazard]:
    """Every hazard in ``files`` (path -> text); review-level ones named in ``reviewed`` (file:function, the file
    by path or by name) are marked reviewed."""
    accepted = set(reviewed)
    out = []
    for path, text in sorted(files.items()):
        for hazard in scan(text, path):
            name = path.rpartition("/")[2]
            if hazard.level == "review" and ({f"{path}:{hazard.function}", f"{name}:{hazard.function}"} & accepted):
                hazard.reviewed = True
            out.append(hazard)
    return out
