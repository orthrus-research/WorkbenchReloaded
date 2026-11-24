"""``wb gc``: retention. Every folder Workbench creates has a keep rule; wb gc lists what the rules would remove and
removes exactly that list once its digest is given back.

    wb gc                       the list: each item, its class, its size and why it may go, then the list's digest
    wb gc --yes <digest>        removes exactly that list; refused, removing nothing, when the list now has another
    wb gc --only <class>        only these classes (repeatable), for the list and for --yes alike
    wb gc --rules               every class with its rule, and whether the nightly run may remove it
    wb gc --nightly             lists and removes the unattended classes only (for the scheduler; never the lab home)
    wb gc --schedule            the line that runs --nightly each night with this machine's scheduler

The classes (``RULES`` here and the daemon's ``wb_core.retention.DAEMON_RULES``):

    ci.runs          a CI run folder that holds no pass, untouched for [core.retention] days        unattended
    ci.copies        a run's copy of an artifact the Store keeps by the same SHA-256, as old         unattended
    store.files      a Store file no root names, untouched for a day; a crash's .part file           unattended
    releases         a release folder other than the one in service and the one before it           unattended
    lab.instances    an instance lab.build made (it holds instance.json), unused for the days       lab home
    lab.templates    a template no instance uses, unused for the days                                lab home
    lab.blobs        a shared file no template names and no instance links to                        lab home
    lab.legacy       an old-route instance lab.freeze froze, unchanged since, its evidence imported   lab home
    jobs.scratch, jobs.artifacts, logs.archives: the daemon's state folder (see wb_core.retention)  unattended

Roots, which no class ever removes or lists:

- a pass record: a run's summary.json that records a passed check, and every run a pass cache entry names;
- the Store's pass and gate records (passes/<project>/<tree>/<check>.<key>.json, gate/<project>/<tree>.json), never
  removed by age, every SHA-256 they name and every run folder a pass record names;
- every set export (<out>/sets/<revision>/set.json) and every SHA-256 any of them names: lines, holds, third
  entries, platform files;
- the Store's index entries and the files they name, seeded entries and their files, landing records and what
  they name, and the folders beside files/ (plugins, JDKs, Gradle);
- kept sessions and every byte their files name (``kept_roots``);
- the pin cache folder, the set exports, and any folder without the receipt its class needs: a legacy instance
  is listed only once lab.freeze has frozen it, never swept on sight.

A frozen legacy instance (``lab.legacy``) is listed only while its manifest (``<lab home>/legacy/<id>/frozen.json``)
says frozen and every file is still there with the size and time it recorded, nothing added; while its world was
imported into the world store (or it had none) and its tours and runs went into a kept session (or it had none);
and while none of its files is the only copy of bytes a root names (a SHA-256 the manifest recorded that a root
names and the Store does not keep). The manifest stays: it records every file by size, time and SHA-256.

Kept sessions: ``lab.keep`` marks a session folder as kept by writing ``kept.json`` in it. A session folder is
``<out>/sessions/<id>/`` (out is the Store's parent folder) or ``<lab home>/sessions/<id>/``. ``kept_roots`` returns,
for every kept session: its folder (nothing at, inside or above it is removed), every SHA-256 named in its
kept.json, session.json and launch.json (and in a launch file kept.json names under ``launch``), the SHA-256 of
each such launch file itself, and every string those files hold (an instance or template they name by its name or
key is kept).

Removal never follows a link: a link is removed as a link, and a folder's links are removed before the folder
(``wb.platform.remove_tree``). Lab-home items are removed only under the GPU lease, so never beside a game, and
the lab part of the list is listed again once the lease is held; if it changed, that part is refused.

The nightly run is ``wb gc --nightly`` from the operating system's scheduler (``wb gc --schedule`` prints the
line, as ``wb backup`` is scheduled); it never lists or removes a lab-home class."""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from wb_core.artifacts import sha256
from wb_core.retention import DAEMON_RULES, DAY, Item, Rule, age_days, digest, is_link, item_from, measure, remove

HEX64 = re.compile(r"\b[0-9a-f]{64}\b")
LEASE_POLL = 5.0

RULES = [
    Rule("ci.runs", "a CI run folder (a project's records/<run>/, or a stand-in run) that holds no pass and that no "
         "pass cache entry names, untouched for [core.retention] days", True),
    Rule("ci.copies", "a run's copy of an artifact (<run>/<check>/artifacts/<file>) whose bytes the Store keeps "
         "under the same name and SHA-256, once the run is [core.retention] days old", True),
    Rule("store.files", "a Store file (files/<sha256>/) that no index entry, set export, seeded entry, landing "
         "record, pass or gate record or kept session names, untouched for a day; a partial write (.part) a day old",
         True),
    Rule("releases", "a release folder other than the one in service and the one deployed before it", True),
    Rule("lab.instances", "an instance lab.build made (it holds instance.json) that no kept session names, unused "
         "for [core.retention] days", False, lab=True),
    Rule("lab.templates", "a template that no instance was made from and no kept session names, unused for "
         "[core.retention] days", False, lab=True),
    Rule("lab.blobs", "a shared read-only file that no template names and no instance links to", False, lab=True),
    Rule("lab.legacy", "an old-route instance lab.freeze froze, unchanged since its manifest, whose world and "
         "sessions were imported and none of whose files is the only copy of bytes a root names; its manifest "
         "stays", False, lab=True),
]


class GcError(Exception):
    pass


# ---------------------------------------------------------------------------------------------------------------
# where things are

@dataclass
class Places:
    store: Path | None = None            # the Store: files/, index/, seeded/, landings/, passes/, gate/
    records: Path | None = None          # CI's run records: <records>/<project>/<run>/
    sets: Path | None = None             # set exports: <sets>/<revision>/set.json
    out: Path | None = None              # the out folder sessions live under
    lab_home: Path | None = None
    releases: Path | None = None
    previous_release: str | None = None  # the release folder deployed before the one in service
    lease_file: Path | None = None
    coordination: Path | None = None
    days: int = 30
    now: float = field(default_factory=time.time)


def places_from(config) -> Places:
    from . import deploy, releases
    from .workspace import REPO, Workspace
    workspace = Workspace(config)
    store = workspace.store_root()
    records = workspace.folder("records")
    runner = config.values.get("runner") or {}
    # [lab] home, else the lab home wb-host names ([runner] lab_home), as wb play and the doctor read it.
    home = (config.values.get("lab") or {}).get("home") or runner.get("lab_home") or "../Lab"
    lab = Path(os.path.expanduser(str(home)))
    lab = lab if lab.is_absolute() else (REPO / lab).resolve()
    previous = ((deploy._records(config).get("releases") or [{}])[-1] or {}).get("previous")
    return Places(
        store=store, records=records,
        sets=store.parent / "sets" if store else None, out=store.parent if store else None,
        lab_home=lab, releases=releases.root(config), previous_release=str(previous)[:12] if previous else None,
        lease_file=Path(runner.get("lease_file") or lab / "lease" / "gpu.lock"),
        coordination=Path(runner["coordination_dir"]) if runner.get("coordination_dir") else lab / "coordination",
        days=int(((config.values.get("core") or {}).get("retention") or {}).get("days") or 30))


# ---------------------------------------------------------------------------------------------------------------
# roots

def _json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _hexes(value) -> set[str]:
    """Every SHA-256 a JSON value names, anywhere in it."""
    return set(HEX64.findall(json.dumps(value))) if value is not None else set()


def _strings(value) -> set[str]:
    found = set()
    stack = [value]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            stack.extend(node.values())
            stack.extend(node.keys())
        elif isinstance(node, list):
            stack.extend(node)
        elif isinstance(node, str):
            found.add(node)
            found.add(node.replace("\\", "/"))
    return found


@dataclass
class Kept:
    paths: list[Path] = field(default_factory=list)
    sha256: set[str] = field(default_factory=set)
    words: set[str] = field(default_factory=set)


def kept_roots(out: Path | None, lab_home: Path | None) -> Kept:
    """What kept sessions keep: their folders, the SHA-256s and the strings their files name (see the module
    docstring). This is the interface lab.keep writes to: a kept.json in a session folder."""
    kept = Kept()
    for base in (out, lab_home):
        folder = Path(base) / "sessions" if base else None
        for marker in sorted(folder.glob("*/kept.json")) if folder and folder.is_dir() else []:
            session = marker.parent
            kept.paths.append(session)
            note = _json(marker)
            docs = [note]
            launches = [session / "launch.json"]
            named = (note or {}).get("launch") if isinstance(note, dict) else None
            if isinstance(named, str) and named:
                launches.append(Path(named) if Path(named).is_absolute() else session / named)
            docs.append(_json(session / "session.json"))
            for launch in launches:
                if launch.is_file():
                    kept.sha256.add(sha256(launch))
                    docs.append(_json(launch))
            for doc in docs:
                kept.sha256 |= _hexes(doc)
                kept.words |= _strings(doc)
    return kept


@dataclass
class Roots:
    sha256: set[str]
    kept: Kept
    passes: set[Path]  # run folders a pass needs

    def holds(self, path: Path) -> bool:
        """Whether ``path`` is a kept folder, inside one or above one."""
        path = Path(os.path.abspath(path))
        for folder in self.kept.paths:
            folder = Path(os.path.abspath(folder))
            if path == folder or folder in path.parents or path in folder.parents:
                return True
        return False


def _set_hexes(places: Places) -> set[str]:
    found = set()
    if places.sets and places.sets.is_dir():
        for path in places.sets.glob("*/set.json"):
            try:
                found |= set(HEX64.findall(path.read_text(encoding="utf-8")))
            except OSError:
                raise GcError(f"the set export {path} cannot be read, so its files cannot be told apart; nothing is "
                              "listed") from None
    return found


def _store_hexes(store: Path | None) -> set[str]:
    found = set()
    if store is None:
        return found
    for path in (store / "index").glob("*/*.json") if (store / "index").is_dir() else []:
        found |= _hexes(_json(path)) or set()
    for folder in ("seeded", "landings"):
        for path in (store / folder).glob("*.json") if (store / folder).is_dir() else []:
            found |= _hexes(_json(path))
            if folder == "seeded":
                found.add(path.stem)
    for path in _pass_records(store):  # pass and gate records: every file a pass vouched for
        found |= _hexes(_json(path)) or set()
    return found


def _pass_records(store: Path | None) -> list[Path]:
    """The Store's pass records (passes/<project>/<tree>/<check>.<key>.json) and gate records
    (gate/<project>/<tree>.json)."""
    if store is None:
        return []
    found = []
    for folder, pattern in (("passes", "*/*/*.json"), ("gate", "*/*.json")):
        if (store / folder).is_dir():
            found += sorted(p for p in (store / folder).glob(pattern) if p.is_file() and not is_link(p))
    return found


def _recorded_runs(store: Path | None, records: Path | None) -> set[Path]:
    """The run folders the Store's pass records name (<records>/<project>/<run>)."""
    needed = set()
    if records is None:
        return needed
    for path in _pass_records(store):
        data = _json(path) or {}
        run, project = data.get("run"), data.get("project")
        if isinstance(run, str) and run and isinstance(project, str) and project:
            needed.add(records / project / run)
    return needed


def _pass_runs(records: Path | None) -> set[Path]:
    """Run folders a pass needs: those whose summary.json records a passed check, and those a cache entry names."""
    needed = set()
    if records is None or not records.is_dir():
        return needed
    for project in (p for p in records.iterdir() if p.is_dir() and not is_link(p)):
        for summary in project.glob("*/summary.json"):
            data = _json(summary)
            if data is None and summary.is_file():
                needed.add(summary.parent)  # unreadable: kept, never guessed
                continue
            if any((job or {}).get("status") == "passed" for job in ((data or {}).get("jobs") or {}).values()):
                needed.add(summary.parent)
        for folder in (project, project / "stand-ins"):  # a stand-in run's engine keeps its cache one level down
            for entry in (folder / "cache").glob("*.json") if (folder / "cache").is_dir() else []:
                run = (_json(entry) or {}).get("runId")
                if run:
                    needed.add(folder / str(run))
    return needed


def roots(places: Places) -> Roots:
    kept = kept_roots(places.out, places.lab_home)
    return Roots(_set_hexes(places) | _store_hexes(places.store) | kept.sha256, kept,
                 _pass_runs(places.records) | _recorded_runs(places.store, places.records))


# ---------------------------------------------------------------------------------------------------------------
# the host's classes

def _item(cls: str, path: Path, why: str) -> Item:
    size, stamp = measure(path)
    return Item(cls, str(path), size, stamp, why)


def _runs(records: Path):
    for project in sorted(p for p in records.iterdir() if p.is_dir() and not is_link(p)):
        for run in sorted(p for p in project.iterdir() if p.is_dir() and not is_link(p) and p.name not in
                          ("cache", "stand-ins")):
            yield project, run, False
        stand_ins = project / "stand-ins"
        if stand_ins.is_dir() and not is_link(stand_ins):
            for run in sorted(p for p in stand_ins.iterdir() if p.is_dir() and not is_link(p) and p.name != "cache"):
                yield project, run, True


def ci_runs(places: Places, found: Roots) -> list[Item]:
    items = []
    if places.records is None or not places.records.is_dir():
        return items
    cutoff = places.now - places.days * DAY
    for project, run, stand_in in _runs(places.records):
        if run in found.passes or found.holds(run):
            continue
        item = _item("ci.runs", run, "")
        if item.stamp >= cutoff:
            continue
        summary = _json(run / "summary.json")
        if stand_in:
            what = "a run against stand-in bytes, which vouches for nothing"
        elif summary is None:
            what = "a run that never finished (no summary.json)"
        else:
            failed = sorted(n for n, j in (summary.get("jobs") or {}).items() if (j or {}).get("status") != "passed")
            what = f"a run with no passed check ({', '.join(failed[:3]) or 'none ran'})"
        item.why = f"{project.name}: {what}, untouched for {age_days(item.stamp, places.now)} days"
        items.append(item)
    return items


def ci_copies(places: Places, found: Roots, removed: set[str]) -> list[Item]:
    """Artifact copies in runs that stay; ``removed`` are the paths already listed (a run going whole, a Store
    file going)."""
    items = []
    files = places.store / "files" if places.store else None
    if places.records is None or files is None or not places.records.is_dir() or not files.is_dir():
        return items
    kept: dict[tuple[str, int], set[str]] = {}  # (name, size) -> the SHA-256s the Store keeps it under
    for folder in files.iterdir():
        if folder.is_dir() and not is_link(folder) and str(folder) not in removed:
            for path in folder.iterdir():
                if path.is_file() and not is_link(path):
                    kept.setdefault((path.name, path.stat().st_size), set()).add(folder.name)
    cutoff = places.now - places.days * DAY
    for project, run, _ in _runs(places.records):
        if str(run) in removed or found.holds(run):
            continue
        for copy in sorted(run.glob("*/artifacts/*")):
            if not copy.is_file() or is_link(copy):
                continue
            size, stamp = measure(copy)
            if stamp >= cutoff or (copy.name, size) not in kept:
                continue
            digest_ = sha256(copy)
            if digest_ in kept[(copy.name, size)]:
                items.append(Item("ci.copies", str(copy), size, stamp,
                                  f"{project.name}: the Store keeps these bytes as files/{digest_[:12]}/{copy.name}"))
    return items


def store_files(places: Places, found: Roots) -> list[Item]:
    items = []
    files = places.store / "files" if places.store else None
    if files is None or not files.is_dir():
        return items
    day_ago = places.now - DAY
    for folder in sorted(p for p in files.iterdir() if p.is_dir() and not is_link(p)):
        if folder.name in found.sha256:
            for part in sorted(folder.glob("*.part")):
                item = _item("store.files", part, "")
                if item.stamp < day_ago:
                    item.why = f"a partial write a crash left, {age_days(item.stamp, places.now)} days old"
                    items.append(item)
            continue
        if not HEX64.fullmatch(folder.name):
            continue  # not a file the Store wrote: left alone
        item = _item("store.files", folder, "")
        if item.stamp < day_ago:
            names = ", ".join(sorted(p.name for p in folder.iterdir())[:3])
            item.why = (f"{names}: no index entry, set export, seeded entry, landing or kept session names "
                        f"{folder.name[:12]}")
            items.append(item)
    return items


def release_folders(places: Places, found: Roots) -> list[Item]:
    items = []
    folder = places.releases
    if folder is None or not folder.is_dir():
        return items
    current = folder / "current"
    serving = Path(os.path.realpath(current)).name if (current.exists() or is_link(current)) else None

    def made_at(path: Path) -> float:
        return float((_json(path / "RELEASE.json") or {}).get("made_at") or 0)

    made = sorted((p for p in folder.iterdir() if p.is_dir() and not is_link(p) and (p / "RELEASE.json").is_file()),
                  key=made_at, reverse=True)
    previous = places.previous_release
    if previous is None or previous == serving:
        previous = next((p.name for p in made if p.name != serving), None)
    for release in made:
        if release.name in (serving, previous) or found.holds(release):
            continue
        items.append(_item("releases", release, f"release {release.name}: neither in service ({serving or 'none'}) "
                                                f"nor the one before it ({previous or 'none'})"))
    return items


def _last_used(folder: Path) -> float:
    newest = folder.stat().st_mtime
    for child in folder.iterdir():
        try:
            newest = max(newest, child.stat().st_mtime)
        except OSError:
            pass
    used = folder / "last-used"
    try:
        newest = max(newest, float(used.read_text(encoding="ascii")))
    except (OSError, ValueError):
        pass
    return newest


def _kept_by_words(found: Roots, *names: str) -> bool:
    return any(name and name in found.kept.words for name in names)


def _instances_root(places: Places) -> Path | None:
    return places.lab_home / "instances" if places.lab_home else None


def lab_instances(places: Places, found: Roots) -> list[Item]:
    items = []
    root = _instances_root(places)
    if root is None or not root.is_dir():
        return items
    cutoff = places.now - places.days * DAY
    for instance in sorted(p for p in root.iterdir() if p.is_dir() and not is_link(p) and not p.name.startswith(".")):
        receipt = _json(instance / "instance.json")
        if not isinstance(receipt, dict):
            continue  # not made by lab.build: never touched
        launch = str(((receipt.get("launch") or {}).get("sha256")) or "")
        if found.holds(instance) or launch in found.sha256 or _kept_by_words(
                found, instance.name, str(instance), str(instance).replace("\\", "/"), str(receipt.get("key") or "")):
            continue
        used = _last_used(instance)
        if used >= cutoff:
            continue
        item = _item("lab.instances", instance, "")
        item.why = f"instance {instance.name} (template {str(receipt.get('key') or '?')[:12]}), unused for " \
                   f"{age_days(int(used), places.now)} days"
        items.append(item)
    return items


def lab_templates(places: Places, found: Roots) -> list[Item]:
    items = []
    root = _instances_root(places)
    templates = root / ".store" / "templates" if root else None
    if templates is None or not templates.is_dir():
        return items
    used_by = set()
    for instance in (p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")):
        marker = _json(instance / ".wb-instance.json") or {}
        if marker.get("template"):
            used_by.add(Path(str(marker["template"])).name)
        receipt = _json(instance / "instance.json") or {}
        if receipt.get("key"):
            used_by.add(str(receipt["key"]))
    cutoff = places.now - places.days * DAY
    for template in sorted(p for p in templates.iterdir() if p.is_dir() and not is_link(p)):
        if not (template / "template.json").is_file() or template.name in used_by or found.holds(template):
            continue
        receipt = _json(template / "instance" / "instance.json") or {}
        launch = str(((receipt.get("launch") or {}).get("sha256")) or "")
        if launch in found.sha256 or _kept_by_words(found, template.name):
            continue
        used = _last_used(template)
        if used >= cutoff:
            continue
        item = _item("lab.templates", template, "")
        item.why = f"template {template.name}: no instance was made from it, unused for " \
                   f"{age_days(int(used), places.now)} days"
        items.append(item)
    return items


def lab_blobs(places: Places, found: Roots) -> list[Item]:
    items = []
    root = _instances_root(places)
    store = root / ".store" if root else None
    if store is None or not (store / "blobs").is_dir():
        return items
    named = set()
    for manifest in (store / "templates").glob("*/template.json"):
        named |= set(((_json(manifest) or {}).get("links") or {}).values())
    for manifest in (store / "worlds").glob("*/world.json"):
        named |= set(((_json(manifest) or {}).get("files") or {}).values())
    for blob in sorted((store / "blobs").glob("*/*")):
        if not blob.is_file() or is_link(blob) or blob.name in named or blob.name in found.sha256:
            continue
        if blob.stat().st_nlink > 1:
            continue
        items.append(_item("lab.blobs", blob, f"blob {blob.name[:12]}: no template or world names it and no "
                                              "instance links to it"))
    return items


def _walk_files(root: Path):
    """(relative name, lstat) of every file under ``root``; links are never entered and are reported with None."""
    stack = [root]
    while stack:
        folder = stack.pop()
        with os.scandir(folder) as entries:
            for entry in entries:
                child = Path(entry.path)
                rel = child.relative_to(root).as_posix()
                if entry.is_symlink() or entry.is_junction() or is_link(child):
                    yield rel, None
                elif entry.is_dir(follow_symlinks=False):
                    stack.append(child)
                else:
                    yield rel, entry.stat(follow_symlinks=False)


def _frozen_as_recorded(instance: Path, manifest: dict) -> str | None:
    """None when every file the manifest records is there with its size and time and nothing was added (its links
    as recorded); else what differs."""
    files = manifest.get("files") or {}
    links = {str(link.get("path")) for link in manifest.get("links") or []}
    seen = 0
    for rel, st in _walk_files(instance):
        if st is None:
            if rel not in links:
                return f"{rel} is a new link"
            continue
        entry = files.get(rel)
        if entry is None:
            return f"{rel} was added"
        if st.st_size != entry.get("size") or st.st_mtime_ns != entry.get("mtime_ns"):
            return f"{rel} changed"
        seen += 1
    return None if seen == len(files) else f"{len(files) - seen} file(s) are gone"


def lab_legacy(places: Places, found: Roots) -> list[Item]:
    items = []
    root = places.lab_home / "legacy" if places.lab_home else None
    if root is None or not root.is_dir():
        return items
    for manifest_path in sorted(root.glob("*/frozen.json")):
        manifest = _json(manifest_path)
        if not isinstance(manifest, dict) or manifest.get("schema") != "wb.lab.frozen/1" \
                or manifest.get("state") != "frozen":
            continue
        instance = Path(str(manifest.get("instance") or ""))
        if not str(manifest.get("instance") or "") or not instance.is_dir() or is_link(instance) \
                or any(is_link(parent) for parent in instance.parents) or found.holds(instance):
            continue
        world = manifest.get("world") or {}
        if world.get("path") and not world.get("label"):
            continue  # its world was not imported: the instance is still its only copy
        session = manifest.get("session")
        if session is not None and not (isinstance(session, dict) and session.get("kept")):
            continue
        named = {str(e.get("sha256")) for e in (manifest.get("files") or {}).values() if e.get("sha256")}
        only = [sha for sha in named & found.sha256
                if not (places.store and (places.store / "files" / sha).is_dir())]
        if only:
            continue  # holds the only copy of bytes a root names
        if _frozen_as_recorded(instance, manifest) is not None:
            continue
        item = _item("lab.legacy", instance, "")
        item.why = (f"legacy instance {instance.name}, frozen {str(manifest.get('frozen') or '?')[:10]} and unchanged "
                    f"since: {len(manifest.get('files') or {})} files recorded in {manifest_path}; world "
                    f"{world.get('label') or 'none'}, session {(session or {}).get('session') or 'none'}")
        items.append(item)
    return items


def host_items(places: Places, only: set[str] | None = None, unattended: bool = False) -> list[Item]:
    """The host's candidates, every class (or ``only`` these, or only the unattended ones), in one pass over one
    reading of the roots."""
    wanted = {r.name for r in RULES if (not only or r.name in only) and (r.unattended or not unattended)}
    found = roots(places)
    items: list[Item] = []
    if "ci.runs" in wanted or "ci.copies" in wanted:
        runs = ci_runs(places, found)
        files = store_files(places, found)
        if "ci.runs" in wanted:
            items += runs
        if "store.files" in wanted:
            items += files
        if "ci.copies" in wanted:
            items += ci_copies(places, found, {i.path for i in runs} | {i.path for i in files})
    elif "store.files" in wanted:
        items += store_files(places, found)
    if "releases" in wanted:
        items += release_folders(places, found)
    for name, finder in (("lab.instances", lab_instances), ("lab.templates", lab_templates),
                         ("lab.blobs", lab_blobs), ("lab.legacy", lab_legacy)):
        if name in wanted:
            items += finder(places, found)
    return items


# ---------------------------------------------------------------------------------------------------------------
# the list, and its removal

LAB = {r.name for r in RULES if r.lab}


@dataclass
class Listing:
    items: list[Item]
    digest: str
    daemon: str | None  # why the daemon's classes are missing, or None


def listing(places: Places, daemon=None, only: set[str] | None = None, unattended: bool = False) -> Listing:
    """The whole list: the host's classes and, when ``daemon`` answers, the daemon's."""
    items = host_items(places, only, unattended)
    missing = None
    daemon_wanted = [r.name for r in DAEMON_RULES if not only or r.name in only]
    if daemon is not None and daemon_wanted:
        try:
            answer = daemon("GET", None)
            items += [item_from(i) for i in answer.get("items") or [] if i.get("cls") in daemon_wanted]
        except Exception as error:  # the daemon down: its classes are simply not in this list
            missing = str(error)
    elif daemon_wanted:
        missing = "not asked"
    items.sort(key=lambda i: (i.cls, i.path))
    return Listing(items, digest(items), missing)


def _take_lease(places: Places, say):
    from wb_host import lease as leases
    cancelled = threading.Event()
    return leases.acquire(places.lease_file, places.coordination, note="wb gc: removing lab-home items",
                          job="gc", cancelled=cancelled, report=lambda why: say(f"waiting for the GPU lease: {why}"),
                          lane_name="gc", poll=LEASE_POLL)


def apply(places: Places, given: str, daemon=None, only: set[str] | None = None, say=print) -> dict:
    """Removes exactly the list whose digest is ``given``; refuses, removing nothing, when the list now differs."""
    now = listing(places, daemon, only)
    if not given or not (now.digest == given or (len(given) >= 12 and now.digest.startswith(given))):
        raise GcError(f"the list changed since it was shown (its digest is now {now.digest}, not {given}); nothing "
                      "was removed: run wb gc again and check the new list")
    removed, refused = [], []
    host = [i for i in now.items if i.where == "host" and i.cls not in LAB]
    lab = [i for i in now.items if i.where == "host" and i.cls in LAB]
    remote = [i for i in now.items if i.where == "daemon"]
    for item in host:
        remove(Path(item.path))
        removed.append(item.path)
    if lab:
        held = _take_lease(places, say)
        try:
            again = [i for i in host_items(places, LAB & (only or LAB)) if i.cls in LAB]
            if digest(again) != digest(lab):
                refused += [i.path for i in lab]
                say("wb gc: the lab-home part of the list changed while waiting for the GPU lease; it was not "
                    "removed (run wb gc again)")
            else:
                for item in lab:
                    remove(Path(item.path))
                    removed.append(item.path)
        finally:
            from wb_host import lease as leases
            leases.release(held[0], held[1], places.coordination)
    if remote:
        answer = daemon("POST", {"remove": [i.as_dict() for i in remote]})
        removed += answer.get("removed") or []
        refused += answer.get("refused") or []
    return {"digest": now.digest, "removed": removed, "refused": refused,
            "bytes": sum(i.bytes for i in now.items if i.path in set(removed))}


def nightly(places: Places, daemon=None) -> dict:
    """The unattended classes only, listed and removed at once; the lab home is never among them."""
    now = listing(places, daemon, unattended=True)
    host = [i for i in now.items if i.where == "host" and i.cls not in LAB]
    remote = [i for i in now.items if i.where == "daemon"]
    removed = []
    for item in host:
        remove(Path(item.path))
        removed.append(item.path)
    refused = []
    if remote:
        answer = daemon("POST", {"remove": [i.as_dict() for i in remote]})
        removed += answer.get("removed") or []
        refused += answer.get("refused") or []
    return {"digest": now.digest, "removed": removed, "refused": refused,
            "bytes": sum(i.bytes for i in now.items if i.path in set(removed)), "daemon": now.daemon}


# ---------------------------------------------------------------------------------------------------------------
# the CLI

def _size(n: int) -> str:
    for unit, scale in (("GB", 1e9), ("MB", 1e6), ("kB", 1e3)):
        if n >= scale:
            return f"{n / scale:.1f} {unit}"
    return f"{n} B"


def _daemon():
    from .client import Client

    def call(method: str, body):
        client = Client()
        return client.request(method, "/v1/gc", body)
    return call


def schedule_line() -> str:
    from . import config as wbconfig
    from . import releases
    wb = releases.current(wbconfig.load()) / "bin" / ("wb.cmd" if os.name == "nt" else "wb")
    if os.name == "nt":
        return f'schtasks /Create /SC DAILY /ST 03:30 /TN "Workbench gc" /TR "\\"{wb}\\" gc --nightly"'
    return f"30 3 * * * {wb} gc --nightly"


def main(args) -> int:
    from . import config as wbconfig
    if args.rules:
        rules = RULES + DAEMON_RULES
        if args.json:
            print(json.dumps([vars(r) for r in rules], indent=1))
            return 0
        for rule in rules:
            mark = "lab home, under the GPU lease" if rule.lab else ("unattended" if rule.unattended else "attended")
            print(f"{rule.name:<15} {mark}\n    {rule.rule}")
        return 0
    if args.schedule:
        print(schedule_line())
        return 0
    known = {r.name for r in RULES + DAEMON_RULES}
    only = set(args.only or [])
    if only - known:
        print(f"wb gc: no class {', '.join(sorted(only - known))} (classes: {', '.join(sorted(known))})",
              file=sys.stderr)
        return 2
    try:
        places = places_from(wbconfig.load())
        if args.nightly:
            done = nightly(places, _daemon())
            print(json.dumps(done, indent=1) if args.json else
                  f"wb gc --nightly: removed {len(done['removed'])} item(s), {_size(done['bytes'])}"
                  + (f"; {len(done['refused'])} refused" if done["refused"] else "")
                  + (f"; the daemon's classes were not listed ({done['daemon']})" if done["daemon"] else ""))
            return 0
        if args.yes:
            done = apply(places, args.yes, _daemon(), only or None,
                         say=lambda text: print(text, file=sys.stderr, flush=True))
            print(json.dumps(done, indent=1) if args.json else
                  f"removed {len(done['removed'])} item(s), {_size(done['bytes'])}"
                  + (f"; refused (changed meanwhile): {len(done['refused'])}" if done["refused"] else ""))
            return 1 if done["refused"] else 0
        found = listing(places, _daemon(), only or None)
    except GcError as error:
        print(f"wb gc: {error}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps({"items": [i.as_dict() for i in found.items], "digest": found.digest,
                          "bytes": sum(i.bytes for i in found.items), "daemon": found.daemon}, indent=1))
        return 0
    for item in found.items:
        print(f"{item.cls:<15} {_size(item.bytes):>9}  {item.path}\n{'':<27}{item.why}")
    total = sum(i.bytes for i in found.items)
    classes = sorted({i.cls for i in found.items})
    print(f"\n{len(found.items)} item(s), {_size(total)}, in {len(classes)} class(es)"
          + (f"; the daemon's classes are not in this list ({found.daemon})" if found.daemon else ""))
    print(f"digest {found.digest}")
    if found.items:
        only_flags = "".join(f" --only {c}" for c in sorted(only))
        print(f"remove exactly this list: wb gc --yes {found.digest}{only_flags}")
    return 0
