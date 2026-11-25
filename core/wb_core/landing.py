"""Landing records: what one landing composed and how far its publish got, in ``<store>/landings/<id>.json``.

A record is written by ``set.land`` once its checks have passed (state ``prepared``) and moved by the CLI that
publishes it:

    prepared     composed and scrubbed; nothing pushed
    refs-pushed  each member commit is on its remote under refs/landed/<id>; no branch moved
    set-pushed   the Set holds the new revision (the commit point); set-named branches may still be behind
    landed       every set-named branch is at its line and the landing refs are gone
    abandoned    stopped before the Set moved; any landing refs it left are deleted by the next wb land

The job holds lock ``set`` until the record says landed or abandoned, or until its deadline passes. A record left at
set-pushed is finished by the next wb land without running a check; one left before it is cleaned up."""

from __future__ import annotations

import json
import time
from pathlib import Path

from . import filelock

STATES = ("prepared", "refs-pushed", "set-pushed", "landed", "abandoned")
OPEN = ("prepared", "refs-pushed", "set-pushed")


class LandingError(Exception):
    pass


def folder(store: Path) -> Path:
    return store / "landings"


def path(store: Path, landing: str) -> Path:
    return folder(store) / f"{landing}.json"


def write(store: Path, record: dict) -> dict:
    if record.get("state") not in STATES:
        raise LandingError(f"unknown landing state {record.get('state')!r}")
    record = {**record, "updated": time.time()}
    filelock.write_json(path(store, record["id"]), record)  # on the disk before the next step of the publish
    return record


def read(store: Path, landing: str) -> dict | None:
    try:
        return json.loads(path(store, landing).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def move(store: Path, landing: str, state: str, **extra) -> dict:
    record = read(store, landing)
    if record is None:
        raise LandingError(f"no landing record {landing}")
    return write(store, {**record, **extra, "state": state})


def unfinished(store: Path) -> list[dict]:
    found = []
    for item in sorted(folder(store).glob("*.json")):
        try:
            record = json.loads(item.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if record.get("state") in OPEN:
            found.append(record)
    return found


def with_refs(store: Path) -> list[dict]:
    """Abandoned landings whose landing refs may still be on a remote."""
    found = []
    for item in sorted(folder(store).glob("*.json")):
        try:
            record = json.loads(item.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if record.get("state") == "abandoned" and not record.get("refs_dropped"):
            found.append(record)
    return found


def _every(store: Path) -> list[dict]:
    found = []
    for item in sorted(folder(store).glob("*.json")):
        try:
            found.append(json.loads(item.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return found


def naming(store: Path, project: str, commit: str, states=("refs-pushed", "set-pushed"),
           finished: bool = False) -> dict | None:
    """The open landing that names ``commit`` for ``project``, when one is publishing; else None. With
    ``finished``, a landing that has landed counts too (``states`` then may name ``landed``)."""
    for record in (_every(store) if finished else unfinished(store)):
        member = (record.get("projects") or {}).get(project) or {}
        if record.get("state") in states and member.get("commit") == commit:
            return record
    return None
