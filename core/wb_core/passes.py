"""Pass and gate records in the Store: what a check vouched for, under the key it ran with.

    <store>/passes/<project>/<tree>/<check>.<key>.json   one passed check: its key's parts, the artifacts it made,
                                                         the set revision it ran under, the job, reproduced
    <store>/gate/<project>/<tree>.json                   every check of that tree's gate list passed under one set
                                                         identity: the file the push gate reads

A check's key is a digest of its name, the tree it ran on and the set identity of its run (the engine's ``@set``
tree: the platform, and the lines, holds, third entries and tools commits the project uses). It fails closed: the
tree covers the check's steps and inputs, and any move of what the project uses gives a new key, so a check is never
vouched for by a run against other bytes. A pure check may still be skipped inside a run by the engine's input cache.

Only ci.run writes these, after the engine passed a check and the tree held still; a run against stand-in bytes
writes nothing. A record is written once and never replaced; neither kind is removed by age (wb gc keeps them and
every file they name)."""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

from . import filelock

PASSES = "passes"
GATE = "gate"
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class PassError(Exception):
    pass


def _name(value: str, what: str) -> str:
    if not NAME.match(str(value or "")):
        raise PassError(f"{what} {value!r} is not a name a record can be filed under")
    return str(value)


def key(check: str, tree: str, identity: str) -> str:
    """The key of ``check`` on ``tree`` under the set identity ``identity`` ('' for a run with no set)."""
    text = json.dumps({"check": check, "tree": tree, "set": identity or ""}, sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def path(store: Path, project: str, tree: str, check: str, digest: str) -> Path:
    return (store / PASSES / _name(project, "project") / _name(tree, "tree")
            / f"{_name(check, 'check')}.{_name(digest, 'key')}.json")


def _write_once(target: Path, value: dict) -> bool:
    """Writes a record that is never replaced. A file in its place that holds no record (a crash left it empty or
    cut short) is no record: it is set aside, and this one written, so one crash cannot keep a tree from passing."""
    if target.exists():
        try:
            if filelock.record(target) != filelock.DAMAGED:  # whole, or gone just now: it stands
                return False
        except OSError:
            return False  # not readable just now: never taken for damage
        with filelock.healing(target):
            try:
                if filelock.record(target) not in (filelock.DAMAGED, filelock.ABSENT):
                    return False  # another writer healed it first
            except OSError:
                return False
            filelock.set_aside(target)
            filelock.write_json(target, value, sort_keys=True)
        return True
    filelock.write_json(target, value, sort_keys=True)
    return True


def write(store: Path, project: str, tree: str, check: str, digest: str, record: dict) -> Path:
    """Records that ``check`` passed on ``tree`` under ``digest``; an existing record stands as it is."""
    target = path(store, project, tree, check, digest)
    _write_once(target, {"schema": 1, "project": project, "tree": tree, "check": check, "key": digest,
                         "written": time.strftime("%Y-%m-%dT%H:%M:%S"), "reproduced": "pending", **record})
    return target


def find(store: Path, project: str, tree: str, check: str, digest: str) -> dict | None:
    try:
        return json.loads(path(store, project, tree, check, digest).read_text(encoding="utf-8"))
    except (OSError, ValueError, PassError):
        return None


def passed(store: Path, project: str, tree: str, keys: dict[str, str]) -> set[str]:
    """The checks of ``keys`` ({check: key}) that have a pass record under their key."""
    return {check for check, digest in keys.items() if find(store, project, tree, check, digest) is not None}


def records(store: Path, project: str | None = None):
    """Every pass record's path, or one project's."""
    base = store / PASSES
    if not base.is_dir():
        return []
    pattern = f"{project}/*/*.json" if project else "*/*/*.json"
    return sorted(p for p in base.glob(pattern) if p.is_file())


def gate_path(store: Path, project: str, tree: str) -> Path:
    return store / GATE / _name(project, "project") / f"{_name(tree, 'tree')}.json"


def write_gate(store: Path, project: str, tree: str, record: dict) -> Path:
    """Records that every gate check of ``tree`` passed under one set identity; an existing record stands."""
    target = gate_path(store, project, tree)
    _write_once(target, {"schema": 1, "project": project, "tree": tree,
                         "written": time.strftime("%Y-%m-%dT%H:%M:%S"), **record})
    return target


def read_gate(store: Path, project: str, tree: str) -> dict | None:
    try:
        return json.loads(gate_path(store, project, tree).read_text(encoding="utf-8"))
    except (OSError, ValueError, PassError):
        return None
