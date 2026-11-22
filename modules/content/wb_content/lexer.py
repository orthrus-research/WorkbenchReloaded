"""A Groovy tokenizer for static analysis: strings and comments are passed over as Groovy reads them.

It knows every string form (single, double and triple quotes, slashy and dollar-slashy, interpolation with
code nested in it), so a call or a name inside a string or a comment is never taken for code, and a string's
value is kept when it has no interpolation. It is not a parser: syntax.py reads calls, imports and
declarations off the tokens.
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass

NAME, NUMBER, STRING, OP = "name", "number", "string", "op"

KEYWORDS = frozenset("""
    abstract as assert boolean break byte case catch char class const continue def default do double else enum
    extends false final finally float for goto if implements import in instanceof int interface long native new
    null package private protected public return short static strictfp super switch synchronized this threadsafe
    throw throws trait transient true try var void volatile while yield""".split())

_SLASHY_AFTER = frozenset({"return", "case", "in", "assert", "yield", "throw"})  # names a slashy string may follow
_OPERATORS = sorted(""">>>= >>> >>= <<= <=> === !== ==~ **= ... ..< .. ?. *. .@ .& :: -> ?: ?= =~ ** ++ -- && ||
    == != <= >= << >> += -= *= /= %= &= |= ^= ! ~ + - * / % & | ^ < > = ? : ; , . ( ) [ ] { } @""".split(),
                    key=len, reverse=True)
_OPS = re.compile("|".join(re.escape(op) for op in _OPERATORS))
_SPACE = re.compile(r"(?:[ \t\f\r\n\v ﻿]|\\\r?\n)+")
_NAME = re.compile(r"(?:[^\W\d]|\$)(?:\w|\$)*")
_DOTTED = re.compile(r"[^\W\d]\w*(?:\.[^\W\d]\w*)*")
_NUMBER = re.compile(r"0[xX][0-9a-fA-F_]+[lLgGiI]?|0[bB][01_]+[lLgGiI]?"
                     r"|\d[\d_]*(?:\.\d[\d_]*)?(?:[eE][+-]?\d[\d_]*)?[lLgGiIdDfF]?")
_HEX4 = re.compile(r"[0-9a-fA-F]{4}")
_OCTAL = re.compile(r"[0-3][0-7]{0,2}|[4-7][0-7]?")
_ESCAPES = {"b": "\b", "t": "\t", "n": "\n", "f": "\f", "r": "\r", "s": " "}


@dataclass(frozen=True, slots=True)
class Token:
    kind: str
    text: str
    start: int
    line: int
    col: int
    value: str | None = None  # a string's value, when it has no interpolation


@dataclass(frozen=True, slots=True)
class Problem:
    line: int
    col: int
    message: str


def tokenize(source: str) -> tuple[list[Token], list[Problem]]:
    """The tokens of a Groovy source, comments and whitespace left out, and what could not be read."""
    scanner = _Scanner(source)
    scanner.run()
    return scanner.tokens, scanner.problems


class _Scanner:
    def __init__(self, source: str):
        self.s, self.n = source, len(source)
        self.starts = [0] + [m.end() for m in re.finditer("\n", source)]
        self.tokens: list[Token] = []
        self.problems: list[Problem] = []

    def where(self, index: int) -> tuple[int, int]:
        line = bisect.bisect_right(self.starts, index)
        return line, index - self.starts[line - 1] + 1

    def add(self, kind: str, start: int, end: int, value: str | None = None) -> None:
        line, col = self.where(start)
        self.tokens.append(Token(kind, self.s[start:end], start, line, col, value))

    def problem(self, index: int, message: str) -> None:
        line, col = self.where(index)
        self.problems.append(Problem(line, col, message))

    def run(self) -> None:
        s, n = self.s, self.n
        i = 0
        if s.startswith("#!"):
            i = s.find("\n") if "\n" in s else n
        while i < n:
            space = _SPACE.match(s, i)
            if space:
                i = space.end()
                continue
            c = s[i]
            if s.startswith("//", i):
                end = s.find("\n", i)
                i = n if end < 0 else end
                continue
            if s.startswith("/*", i):
                end = s.find("*/", i + 2)
                if end < 0:
                    self.problem(i, "a block comment is not closed")
                i = n if end < 0 else end + 2
                continue
            if c in "'\"":
                end, value = self.quoted(i)
                self.add(STRING, i, end, value)
                i = end
                continue
            if s.startswith("$/", i):
                end, value = self.dollar_slashy(i)
                self.add(STRING, i, end, value)
                i = end
                continue
            if c == "/" and self.slashy_allowed():
                found = self.slashy(i)
                if found:
                    self.add(STRING, i, found[0], found[1])
                    i = found[0]
                    continue
            if "0" <= c <= "9":
                end = _NUMBER.match(s, i).end()
                self.add(NUMBER, i, end)
                i = end
                continue
            name = _NAME.match(s, i)
            if name:
                self.add(NAME, i, name.end())
                i = name.end()
                continue
            op = _OPS.match(s, i)
            end = op.end() if op else i + 1
            self.add(OP, i, end)
            i = end

    def slashy_allowed(self) -> bool:
        if not self.tokens:
            return True
        last = self.tokens[-1]
        if last.kind == OP:
            return last.text not in (")", "]", "}", "++", "--")
        return last.kind == NAME and last.text in _SLASHY_AFTER

    # -- strings -------------------------------------------------------------------------------------
    def quoted(self, i: int) -> tuple[int, str | None]:
        """A quoted string from its opening quote: where it ends and its value (None when interpolated)."""
        s = self.s
        quote = s[i] * 3 if s.startswith(s[i] * 3, i) else s[i]
        interpolates = quote[0] == '"'
        j, parts, dynamic = i + len(quote), [], False
        while True:
            if j >= self.n:
                self.problem(i, "a string is not closed")
                break
            if s.startswith(quote, j):
                j += len(quote)
                break
            c = s[j]
            if c == "\\":
                j = self.escape(j, parts)
                continue
            if c == "\n" and len(quote) == 1:
                self.problem(i, "a string is not closed on its line")
                break
            if c == "$" and interpolates:
                end = self.interpolation(j)
                if end is not None:
                    dynamic, j = True, end
                    continue
            parts.append(c)
            j += 1
        return j, None if dynamic else "".join(parts)

    def escape(self, j: int, parts: list[str]) -> int:
        s = self.s
        following = s[j + 1] if j + 1 < self.n else ""
        if following == "u" and _HEX4.match(s, j + 2):
            parts.append(chr(int(s[j + 2:j + 6], 16)))
            return j + 6
        octal = _OCTAL.match(s, j + 1) if following.isdigit() else None
        if octal:
            parts.append(chr(int(octal.group(0), 8)))
            return octal.end()
        if following == "\n":
            return j + 2  # a line continuation
        if following == "\r" and s.startswith("\n", j + 2):
            return j + 3
        parts.append(_ESCAPES.get(following, following))
        return j + 2

    def interpolation(self, j: int) -> int | None:
        """Where a ``$`` interpolation starting at ``j`` ends; None for a ``$`` that is only text."""
        following = self.s[j + 1] if j + 1 < self.n else ""
        if following == "{":
            return self.code(j + 2)
        if following.isalpha() or following == "_":
            return _DOTTED.match(self.s, j + 1).end()
        return None

    def code(self, j: int) -> int:
        """The end of the code in ``${...}`` that starts at ``j``, strings and braces nested in it."""
        s, depth, start = self.s, 0, j
        while j < self.n:
            c = s[j]
            if c in "'\"":
                j = self.quoted(j)[0]
                continue
            if s.startswith("//", j):
                end = s.find("\n", j)
                j = self.n if end < 0 else end
                continue
            if s.startswith("/*", j):
                end = s.find("*/", j + 2)
                j = self.n if end < 0 else end + 2
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                if depth == 0:
                    return j + 1
                depth -= 1
            j += 1
        self.problem(start, "an interpolation is not closed")
        return self.n

    def slashy(self, i: int) -> tuple[int, str | None] | None:
        """A slashy string from its slash, or None when no slash closes it (it was a division)."""
        s, j, parts, dynamic = self.s, i + 1, [], False
        while j < self.n:
            c = s[j]
            if c == "\\" and s.startswith("/", j + 1):
                parts.append("/")
                j += 2
                continue
            if c == "/":
                return j + 1, None if dynamic else "".join(parts)
            if c == "$":
                end = self.interpolation(j)
                if end is not None:
                    dynamic, j = True, end
                    continue
            parts.append(c)
            j += 1
        return None

    def dollar_slashy(self, i: int) -> tuple[int, str | None]:
        s, j, parts, dynamic = self.s, i + 2, [], False
        while j < self.n:
            if s.startswith("/$", j):
                return j + 2, None if dynamic else "".join(parts)
            if s.startswith("$$", j) or s.startswith("$/", j):
                parts.append(s[j + 1])
                j += 2
                continue
            if s[j] == "$":
                end = self.interpolation(j)
                if end is not None:
                    dynamic, j = True, end
                    continue
            parts.append(s[j])
            j += 1
        self.problem(i, "a dollar-slashy string is not closed")
        return self.n, None
