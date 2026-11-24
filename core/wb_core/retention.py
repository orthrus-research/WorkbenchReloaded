"""Retention: every folder Workbench creates has a keep rule, and a removal is always a list shown first.

A **class** is one kind of removable thing with one rule (``Rule``). Each candidate is an ``Item``: its class, its
path, its size, the newest modification time in it, and why its rule lets it go. A list's **digest** covers every
item's class, place, path, size and time, so ``wb gc --yes <digest>`` removes exactly the list that was shown and is
refused when anything in it changed. Classes marked ``unattended`` may be removed by the nightly run; a class in
the lab home never is, and is removed only under the GPU lease.

The daemon's own classes live in its state folder, which only the daemon sees (in the container); the host CLI asks
for them over the API (GET and POST ``/v1/gc``) and adds them to its list. Everything else is listed on the host
(``wb.gc``)."""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from wb.platform import is_link, remove_link, remove_tree

DAY = 86400.0


@dataclass(frozen=True)
class Rule:
    name: str
    rule: str            # what the class removes, and when
    unattended: bool     # the nightly run may remove it
    lab: bool = False    # in the lab home: removed only under the GPU lease, never at night
    where: str = "host"  # host, or daemon (its state folder)


@dataclass
class Item:
    cls: str
    path: str
    bytes: int
    stamp: int           # the newest modification time in it, in whole seconds
    why: str
    where: str = "host"

    def key(self) -> list:
        return [self.cls, self.where, self.path, self.bytes, self.stamp]

    def as_dict(self) -> dict:
        return asdict(self)


def item_from(data: dict) -> Item:
    return Item(str(data["cls"]), str(data["path"]), int(data["bytes"]), int(data["stamp"]), str(data["why"]),
                str(data.get("where") or "host"))


def digest(items: list[Item]) -> str:
    """The digest of a list: 16 hex digits over every item's class, place, path, size and time, in a fixed order."""
    keys = sorted(item.key() for item in items)
    return hashlib.sha256(json.dumps(keys, separators=(",", ":")).encode("utf-8")).hexdigest()[:16]


def measure(path: Path) -> tuple[int, int]:
    """(bytes, newest modification time) of a file or a folder, never following a link (a link counts as nothing
    but itself)."""
    path = Path(path)
    try:
        info = os.lstat(path)
    except OSError:
        return 0, 0
    if is_link(path) or not os.path.isdir(path):
        return (0 if is_link(path) else info.st_size), int(info.st_mtime)
    total, newest = 0, int(info.st_mtime)
    folders = [path]
    while folders:
        try:
            entries = list(os.scandir(folders.pop()))
        except OSError:
            continue
        for entry in entries:
            if is_link(Path(entry.path)):
                continue  # a link is removed as a link: neither its size nor its time is the folder's
            try:
                stat = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            newest = max(newest, int(stat.st_mtime))
            if entry.is_dir(follow_symlinks=False):
                folders.append(Path(entry.path))
            else:
                total += stat.st_size
    return total, newest


def remove(path: Path):
    """Removes a file, a link (never what it points at) or a folder (every link in it unlinked first)."""
    path = Path(path)
    if is_link(path):
        remove_link(path)
    elif path.is_dir():
        remove_tree(path)
    elif path.exists():
        try:
            path.unlink()
        except PermissionError:
            os.chmod(path, 0o600)
            path.unlink()


def age_days(stamp: int, now: float) -> int:
    return int((now - stamp) // DAY)


# ---------------------------------------------------------------------------------------------------------------
# the daemon's classes: its state folder

DAEMON_RULES = [
    Rule("jobs.scratch", "a settled job's folder (its working files), once the job settled more than [core.retention] "
         "days ago and none of its artifacts is pinned; the job's record and envelope stay", True, where="daemon"),
    Rule("jobs.artifacts", "a file in the daemon's artifact store that no job settled within [core.retention] days "
         "and no pinned artifact names, untouched as long", True, where="daemon"),
    Rule("logs.archives", "a rolled log archive older than [core.retention] days", True, where="daemon"),
]


def daemon_items(state: Path, db, days: int, now: float | None = None) -> list[Item]:
    """The daemon's candidates, from its database and its state folder."""
    now = time.time() if now is None else now
    cutoff = now - days * DAY
    found = []
    pinned = {r["job"] for r in db.query("SELECT job FROM artifact WHERE pinned = 1")}
    for row in db.query("SELECT id, settled FROM job WHERE state IN ('settled', 'cancelled', 'interrupted') "
                        "AND settled < ?", (cutoff,)):
        folder = state / "jobs" / row["id"]
        if folder.exists() and row["id"] not in pinned:
            size, stamp = measure(folder)
            found.append(Item("jobs.scratch", str(folder), size, stamp,
                              f"job {row['id']} settled {age_days(int(row['settled']), now)} days ago", "daemon"))
    referenced = {r["sha256"] for r in db.query("SELECT sha256 FROM artifact a JOIN job j ON j.id = a.job "
                                                "WHERE j.settled >= ? OR a.pinned = 1", (cutoff,))}
    store = state / "store"
    for path in sorted(store.rglob("*")) if store.is_dir() else []:
        if path.is_file() and not is_link(path) and path.name not in referenced:
            size, stamp = measure(path)
            if stamp < cutoff:
                found.append(Item("jobs.artifacts", str(path), size, stamp,
                                  f"no recent or pinned job names it; untouched for {age_days(stamp, now)} days",
                                  "daemon"))
    for path in sorted((state / "logs").rglob("*.gz")) if (state / "logs").is_dir() else []:
        size, stamp = measure(path)
        if stamp < cutoff:
            found.append(Item("logs.archives", str(path), size, stamp,
                              f"a rolled log, {age_days(stamp, now)} days old", "daemon"))
    return found


def remove_listed(current: list[Item], wanted: list[Item]) -> dict:
    """Removes the items ``wanted`` when every one is still a candidate in ``current``, unchanged (class, path, size
    and time); otherwise removes nothing and names the paths that are not."""
    now = {tuple(item.key()) for item in current}
    missing = [item.path for item in wanted if tuple(item.key()) not in now]
    if missing:
        return {"removed": [], "refused": missing}
    removed = []
    for item in wanted:
        remove(Path(item.path))
        removed.append(item.path)
    return {"removed": removed, "refused": []}
