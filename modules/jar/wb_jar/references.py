"""Find the classes that subclass, override or call given targets, without loading them (``jar.references``).

A target is a class (``a.b.C`` or ``a/b/C``) or a method (``a.b.C::name`` for any descriptor,
``a.b.C::name(I)V`` for one; a descriptor that stops after ``)`` matches the parameters only).

- **subclass**: a class that extends or implements the target class, at any depth.
- **override**: a class deriving from the method's owner that declares a method of the same name (and
  descriptor), neither static nor private.
- **call**: an invocation, or a method handle bound by ``invokedynamic`` (a method reference), of the
  target method through its owner or any class deriving from it; for a class target, of any method
  through the class itself (constructors included).

Ancestry is followed through the searched JARs and the hierarchy JARs (the game's classes, a library's);
a class whose ancestry leaves them is not seen to derive from anything beyond.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import classes
from .archive import MAX_CLASS_BYTES, archives

KINDS = ("subclass", "override", "call")
CAP = 1000  # rows kept per target and kind; the counts are always whole


@dataclass(frozen=True)
class Target:
    text: str
    owner: str
    name: str | None = None
    descriptor: str | None = None

    def matches(self, name: str, descriptor: str) -> bool:
        if name != self.name:
            return False
        if self.descriptor is None:
            return True
        if self.descriptor.endswith(")"):
            return descriptor.startswith(self.descriptor)
        return descriptor == self.descriptor


def parse_target(text: str) -> Target:
    owner, sep, member = text.strip().partition("::")
    if not owner.strip() or (sep and not member.strip()):
        raise ValueError(f"not a target: {text!r} (use a.b.C, a.b.C::method or a.b.C::method(desc))")
    if not sep:
        return Target(text, classes.internal(owner))
    name, paren, rest = member.strip().partition("(")
    return Target(text, classes.internal(owner), name, paren + rest if paren else None)


class Hierarchy:
    def __init__(self) -> None:
        self.parents: dict[str, tuple[str, ...]] = {}
        self.children: dict[str, set[str]] = defaultdict(set)

    def add(self, name: str, super_name: str, interfaces: list[str]) -> None:
        parents = tuple(p for p in (super_name, *interfaces) if p)
        self.parents.setdefault(name, parents)
        for parent in parents:
            self.children[parent].add(name)

    def descendants(self, name: str) -> set[str]:
        found, queue = set(), deque([name])
        while queue:
            for child in self.children.get(queue.popleft(), ()):
                if child not in found:
                    found.add(child)
                    queue.append(child)
        return found

    def ancestry(self, name: str, target: str) -> list[str]:
        """The shortest chain from ``name`` up to ``target`` (both included); empty if none."""
        previous: dict[str, str | None] = {name: None}
        queue = deque([name])
        while queue:
            current = queue.popleft()
            if current == target:
                chain = []
                while current is not None:
                    chain.append(current.replace("/", "."))
                    current = previous[current]
                return chain[::-1]
            for parent in self.parents.get(current, ()):
                if parent not in previous:
                    previous[parent] = current
                    queue.append(parent)
        return []


def search(root: Path, targets: list[Target], kinds: list[str], hierarchy_root: Path | None) -> dict[str, Any]:
    errors: list[dict] = []
    tree = Hierarchy()
    searched: dict[str, list[str]] = defaultdict(list)  # class -> the JARs it is in
    unreadable: list[str] = []
    for source, is_searched in ((hierarchy_root, False), (root, True)):
        if source is None:
            continue
        for archive in archives(source, errors):
            for entry, info in archive.each_class():
                if not info.this_class:
                    if is_searched:
                        unreadable.append(f"{archive.label}!/{entry}")
                    continue
                tree.add(info.this_class, info.super_name, info.interfaces)
                if is_searched:
                    if archive.label not in searched[info.this_class]:  # once per JAR (versioned copies too)
                        searched[info.this_class].append(archive.label)
                    if not info.complete:
                        unreadable.append(f"{archive.label}!/{entry}")
    derived = {t.owner: tree.descendants(t.owner) for t in targets}
    rows: dict[str, dict[str, list[dict]]] = {t.text: {k: [] for k in kinds} for t in targets}
    counts: dict[str, dict[str, int]] = {t.text: {k: 0 for k in kinds} for t in targets}

    def add(target: Target, kind: str, row: dict) -> None:
        counts[target.text][kind] += 1
        if len(rows[target.text][kind]) < CAP:
            rows[target.text][kind].append(row)

    if "subclass" in kinds:
        for target in targets:
            if target.name is None:
                for name in sorted(derived[target.owner] & searched.keys()):
                    for jar in searched[name]:
                        add(target, "subclass", {"jar": jar, "class": name.replace("/", "."),
                                                 "ancestry": tree.ancestry(name, target.owner)})
    method_targets = [t for t in targets if t.name is not None]
    wanted_classes = set().union(*(derived[t.owner] for t in method_targets)) if method_targets else set()
    tokens = {t.name.encode() for t in method_targets} | {t.owner.encode() for t in targets if t.name is None}
    needs_bodies = "call" in kinds or ("override" in kinds and method_targets)
    if needs_bodies:
        for archive in archives(root, errors):
            for entry in archive.class_entries():
                data = archive.read(entry, MAX_CLASS_BYTES)
                if data is None:
                    continue
                name = entry[:-6]
                if name not in wanted_classes and not any(token in data for token in tokens):
                    continue  # names none of the targets: nothing to find in it
                info = classes.read(data)
                if archive.label not in searched.get(info.this_class, ()):
                    continue
                _visit(info, archive.label, targets, kinds, derived, add)
    return {"targets": [{"target": t.text, "kind": "method" if t.name else "class", "counts": counts[t.text],
                         **rows[t.text]} for t in targets],
            "classes": len(searched), "unreadable": unreadable, "errors": errors}


def _visit(info: classes.ClassInfo, jar: str, targets: list[Target], kinds: list[str],
           derived: dict[str, set[str]], add) -> None:
    dotted = info.this_class.replace("/", ".")
    for target in targets:
        if target.name is None or "override" not in kinds or info.this_class not in derived[target.owner]:
            continue
        for method in info.declared_methods:
            if (target.matches(method.name, method.descriptor) and not method.static and not method.private
                    and method.name not in ("<init>", "<clinit>")):
                add(target, "override", {"jar": jar, "class": dotted, "method": method.name + method.descriptor})
    if "call" not in kinds:
        return
    for method in info.declared_methods:
        for how, owner, name, descriptor in info.calls(method):
            for target in targets:
                if target.name is None:
                    hit = owner == target.owner
                else:
                    hit = target.matches(name, descriptor) and (owner == target.owner or owner in derived[target.owner])
                if hit:
                    add(target, "call", {"jar": jar, "class": dotted, "in": method.name + method.descriptor,
                                         "calls": f"{owner.replace('/', '.')}::{name}{descriptor}", "how": how})
