"""Check that named hook targets still exist in JARs, and digest packaged resources (``jar.hook-targets``).

A hook target is what a trace filter, a profiler, a transformer or a Mixin names by hand, so an update of
the JAR that moves or renames it breaks the hook without a compile error:

- ``a.b.C``: the class exists;
- ``a.b.C::name``: it declares a method of that name; ``a.b.C::name(I)V`` with that descriptor (one that
  stops after ``)`` matches the parameters only);
- ``a.b.C#name``: it declares a field of that name; ``a.b.C#name:I`` with that type.

Resource patterns are globs over entry names (``*`` within a folder, ``**`` across folders); each match is
digested (SHA-256), so a change in what the JAR ships shows, and an expected digest pins an entry.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import classes
from .archive import archives
from orthrus_tools.scrub import glob_match  # noqa: E402 - the gitignore-like globs the scrub uses


@dataclass(frozen=True)
class Hook:
    text: str
    owner: str
    kind: str  # class, method, field
    name: str | None = None
    descriptor: str | None = None


def parse_hook(text: str) -> Hook:
    raw = text.strip()
    if "::" in raw:
        owner, member = raw.split("::", 1)
        name, paren, rest = member.partition("(")
        hook = Hook(text, classes.internal(owner), "method", name.strip(), paren + rest if paren else None)
    elif "#" in raw:
        owner, member = raw.split("#", 1)
        name, colon, descriptor = member.partition(":")
        hook = Hook(text, classes.internal(owner), "field", name.strip(), descriptor.strip() if colon else None)
    else:
        hook = Hook(text, classes.internal(raw), "class")
    if not hook.owner or (hook.kind != "class" and not hook.name):
        raise ValueError(f"not a hook target: {text!r} (use a.b.C, a.b.C::method(desc) or a.b.C#field)")
    return hook


def _declares(info: classes.ClassInfo, hook: Hook) -> list[str]:
    """The declarations in a class that satisfy a hook, as name + descriptor."""
    if hook.kind == "class":
        return [hook.owner.replace("/", ".")]
    members = info.declared_methods if hook.kind == "method" else info.declared_fields
    found = []
    for member in members:
        if member.name != hook.name:
            continue
        wanted = hook.descriptor
        if wanted is None or member.descriptor == wanted or (
                hook.kind == "method" and wanted.endswith(")") and member.descriptor.startswith(wanted)):
            found.append(member.name + (member.descriptor if hook.kind == "method" else ":" + member.descriptor))
    return found


def _digest(archive, name: str) -> tuple[str, int]:
    with archive.zip.open(archive.entries[name]) as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest(), archive.entries[name].file_size


def check(root: Path, hooks: list[Hook], patterns: list[str], expect: dict[str, str]) -> dict[str, Any]:
    errors: list[dict] = []
    found: dict[str, list[dict]] = {h.text: [] for h in hooks}
    near: dict[str, list[str]] = {h.text: [] for h in hooks}  # the class is there, the member is not
    matched: dict[str, list[dict]] = {p: [] for p in patterns}
    pinned: dict[str, list[dict]] = {e: [] for e in expect}
    jars = []
    for archive in archives(root, errors):
        if archive.depth == 0:
            path = root if root.is_file() else root / archive.label
            with open(path, "rb") as handle:
                jars.append({"jar": archive.label, "sha256": hashlib.file_digest(handle, "sha256").hexdigest(),
                             "bytes": path.stat().st_size})
        for hook in hooks:
            candidates = [hook.owner + ".class"] + [n for n in archive.entries
                                                    if n.startswith("META-INF/versions/") and n.endswith("/" + hook.owner + ".class")]
            for entry in candidates:
                info = archive.read_class(entry) if entry in archive.entries else None
                if info is None or info.this_class != hook.owner:
                    continue
                declared = _declares(info, hook)
                if declared:
                    found[hook.text].append({"jar": archive.label, "entry": entry, "declares": declared})
                elif hook.kind != "class":
                    members = info.declared_methods if hook.kind == "method" else info.declared_fields
                    near[hook.text] += [m.name + m.descriptor for m in members if m.name.lower() == hook.name.lower()
                                        or hook.name.lower() in m.name.lower()][:10]
        for name in archive.names:
            for pattern in patterns:
                if glob_match(name, pattern):
                    sha, size = _digest(archive, name)
                    matched[pattern].append({"jar": archive.label, "entry": name, "sha256": sha, "bytes": size})
            if name in expect:
                sha, _ = _digest(archive, name)
                pinned[name].append({"jar": archive.label, "sha256": sha})
    targets = [{"target": h.text, "kind": h.kind, "found": bool(found[h.text]), "where": found[h.text],
                "similar": sorted(set(near[h.text]))} for h in hooks]
    resources = [{"pattern": p, "entries": matched[p]} for p in patterns]
    pins = []
    for entry, wanted in expect.items():
        seen = pinned[entry]
        pins.append({"entry": entry, "expected": wanted, "found": seen,
                     "passed": bool(seen) and all(s["sha256"] == wanted for s in seen)})
    return {"schema": "wb.jar.hook-targets/1", "jars": jars, "targets": targets, "resources": resources,
            "pins": pins, "errors": errors}
