"""Name mappings from development names to production names, read from the files builds use.

Formats, told apart by their content:

- **SRG**: ``CL: a b``, ``FD: a/f b/g``, ``MD: a/m (desc) b/n (desc)`` (``PK:`` lines are skipped);
- **TSRG**: a class line ``a b`` followed by tab-indented members, ``f g`` or ``m (desc) n``;
- **TSRG v2**: a ``tsrg2 left right ...`` header, then TSRG with one name per namespace, a field optionally
  with its descriptor; the first two namespaces are read;
- **CSRG** (one line per entry, as the Mixin annotation processor writes its member mappings): ``a b`` for a
  class, ``owner f g`` for a field, ``owner m (desc) n`` for a method.

Owners and descriptors are in development names; ``descriptor`` and ``owner`` translate them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_CLASS_IN_DESCRIPTOR = re.compile(r"L([^;]+);")


class MappingError(Exception):
    pass


@dataclass
class Mappings:
    classes: dict[str, str] = field(default_factory=dict)
    fields: dict[tuple[str, str], str] = field(default_factory=dict)
    """(owner, name) -> production name, owners in development names."""
    methods: dict[tuple[str, str, str], str] = field(default_factory=dict)
    """(owner, name, descriptor) -> production name, owners and descriptors in development names."""

    def owner(self, name: str) -> str:
        return self.classes.get(name, name)

    def descriptor(self, descriptor: str) -> str:
        return _CLASS_IN_DESCRIPTOR.sub(lambda m: f"L{self.owner(m.group(1))};", descriptor)

    def merge(self, other: Mappings) -> None:
        for mine, theirs in ((self.classes, other.classes), (self.fields, other.fields), (self.methods, other.methods)):
            for key, value in theirs.items():
                mine.setdefault(key, value)

    def reversed(self) -> Mappings:
        """The same names read the other way, for a file that maps production to development names."""
        out = Mappings(classes={prod: dev for dev, prod in self.classes.items()})
        for (owner, name), prod in self.fields.items():
            out.fields.setdefault((self.owner(owner), prod), name)
        for (owner, name, descriptor), prod in self.methods.items():
            out.methods.setdefault((self.owner(owner), prod, self.descriptor(descriptor)), name)
        return out

    def __len__(self) -> int:
        return len(self.classes) + len(self.fields) + len(self.methods)


def _class(mappings: Mappings, dev: str, prod: str) -> None:
    if dev != prod:
        mappings.classes.setdefault(dev, prod)


def _member(mappings: Mappings, owner: str, name: str, descriptor: str | None, prod: str) -> None:
    if descriptor and descriptor.startswith("("):
        mappings.methods.setdefault((owner, name, descriptor), prod)
    else:
        mappings.fields.setdefault((owner, name), prod)


def _split(path: str) -> tuple[str, str]:
    owner, _, name = path.rpartition("/")
    return owner, name


def parse(text: str) -> Mappings:
    lines = [line.rstrip("\r\n") for line in text.splitlines()]
    body = [line for line in lines if line.strip() and not line.lstrip().startswith("#")]
    mappings = Mappings()
    if not body:
        return mappings
    first = body[0].split()
    if first[0] == "tsrg2":
        _tsrg(mappings, body[1:], namespaces=len(first) - 1)
    elif first[0] in ("CL:", "FD:", "MD:", "PK:"):
        _srg(mappings, body)
    elif any(line[:1] in "\t " for line in body):
        _tsrg(mappings, body, namespaces=2)
    else:
        _csrg(mappings, body)
    return mappings


def _srg(mappings: Mappings, lines: list[str]) -> None:
    for number, line in enumerate(lines, 1):
        parts = line.split()
        if parts[0] == "CL:" and len(parts) >= 3:
            _class(mappings, parts[1], parts[2])
        elif parts[0] == "FD:" and len(parts) >= 3:
            owner, name = _split(parts[1])
            mappings.fields.setdefault((owner, name), _split(parts[2])[1])
        elif parts[0] == "MD:" and len(parts) >= 5:
            owner, name = _split(parts[1])
            mappings.methods.setdefault((owner, name, parts[2]), _split(parts[3])[1])
        elif parts[0] != "PK:":
            raise MappingError(f"line {number}: not an SRG line: {line[:80]}")


def _tsrg(mappings: Mappings, lines: list[str], namespaces: int) -> None:
    owner = None
    for number, line in enumerate(lines, 1):
        depth = (len(line) - len(line.lstrip("\t"))) or (1 if line[:1] == " " else 0)
        parts = line.split()
        if depth >= 2:
            continue  # a method's parameter, or its static marker (TSRG v2)
        if depth == 0:
            if len(parts) < 2:
                raise MappingError(f"line {number}: a class line needs two names: {line[:80]}")
            owner = parts[0]
            _class(mappings, parts[0], parts[1])
            continue
        if owner is None:
            raise MappingError(f"line {number}: a member before any class: {line[:80]}")
        if len(parts) >= 3 and parts[1].startswith("("):
            _member(mappings, owner, parts[0], parts[1], parts[2])
        elif len(parts) == namespaces + 1:
            _member(mappings, owner, parts[0], None, parts[2])  # a field with its descriptor
        elif len(parts) >= 2:
            _member(mappings, owner, parts[0], None, parts[1])
        else:
            raise MappingError(f"line {number}: not a member line: {line[:80]}")


def _csrg(mappings: Mappings, lines: list[str]) -> None:
    for number, line in enumerate(lines, 1):
        parts = line.split()
        if len(parts) == 2:
            _class(mappings, parts[0], parts[1])
        elif len(parts) == 3:
            mappings.fields.setdefault((parts[0], parts[1]), parts[2])
        elif len(parts) == 4 and parts[2].startswith("("):
            mappings.methods.setdefault((parts[0], parts[1], parts[2]), parts[3])
        else:
            raise MappingError(f"line {number}: not a CSRG line: {line[:80]}")


def load(paths: list[Path], reversed_paths: list[Path] = ()) -> Mappings:
    """Development -> production names from ``paths``, and from ``reversed_paths`` read the other way."""
    merged = Mappings()
    for path, reverse in [(p, False) for p in paths] + [(p, True) for p in reversed_paths]:
        try:
            mappings = parse(Path(path).read_text(encoding="utf-8", errors="replace"))
        except MappingError as error:
            raise MappingError(f"{Path(path).name}: {error}") from None
        merged.merge(mappings.reversed() if reverse else mappings)
    return merged
