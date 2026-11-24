"""The instance store: instances as recipes over shared layers, instead of a fresh full copy per run.

An instance is a function of its inputs, so the lab keeps the inputs once and builds instances from them:

- A **recipe** is the hash of everything an instance is built from (for lab.build: the launch file, its base and
  parameters). The same recipe gives the same instance, so it is reused, not built again.
- A **template** is one prepared instance per recipe, kept in the store. Its read-only parts live once in the
  content-addressed **blob** store.
- An **instance** is a working copy of a template: read-only files are hard links into the blobs (no space, no
  time), everything the game writes is a private copy, and the files that name the instance are rewritten.
- A **world snapshot** keeps a generated world (and its caches) by content; restoring it is a private copy,
  because the game writes region files in place.

Which parts are worlds, which files are rewritten and which are only read is the store's ``Layout`` (lab.build's is
wb_lab.instance.LAYOUT). Shared blobs are marked read-only, so an accidental write fails loudly instead of
changing every instance.
Hard links work on NTFS and on Linux and macOS filesystems; store and instances must share a volume.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path

MARKER = ".wb-instance.json"


@dataclass
class Layout:
    """The parts of an instance the store treats differently."""
    world_parts: list[str] = field(default_factory=list)
    rewrite: list[str] = field(default_factory=list)
    linkable_dirs: list[str] = field(default_factory=list)
    linkable_suffixes: list[str] = field(default_factory=list)

    def linkable(self, relative: str) -> bool:
        """Files the game only reads: shared by hard link. Everything else is a private copy."""
        rel = relative.replace("\\", "/")
        if any(rel == part or rel.startswith(part + "/") for part in self.world_parts):
            return False
        if any(rel.startswith(part + "/") for part in self.linkable_dirs):
            return True
        return any(rel.endswith(suffix) for suffix in self.linkable_suffixes)


class StoreError(Exception):
    pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_only(path: Path):
    os.chmod(path, stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)


def _writable(path: Path):
    os.chmod(path, stat.S_IREAD | stat.S_IWRITE)


def _files(root: Path):
    for directory, _dirs, files in os.walk(root):
        for name in files:
            path = Path(directory) / name
            yield path, path.relative_to(root).as_posix()


@dataclass
class Store:
    root: Path
    layout: Layout = field(default_factory=Layout)

    @property
    def blobs(self) -> Path:
        return self.root / "blobs"

    @property
    def templates(self) -> Path:
        return self.root / "templates"

    @property
    def worlds(self) -> Path:
        return self.root / "worlds"

    def blob(self, digest: str) -> Path:
        return self.blobs / digest[:2] / digest

    def same_volume(self, other: Path) -> bool:
        self.root.mkdir(parents=True, exist_ok=True)
        probe = Path(other)
        while not probe.exists():
            probe = probe.parent
        return os.stat(self.root).st_dev == os.stat(probe).st_dev

    # -- blobs -----------------------------------------------------------------------------------
    def intern(self, path: Path, digest: str | None = None) -> str:
        """Puts a file's content in the blob store and replaces the file with a hard link to it."""
        digest = digest or sha256(path)
        target = self.blob(digest)
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            os.link(path, target)
            _read_only(target)
        elif not os.path.samefile(path, target):
            _writable(path)
            path.unlink()
            os.link(target, path)
        else:
            _read_only(target)
        return digest

    def link_into(self, digest: str, destination: Path):
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.link(self.blob(digest), destination)

    def copy_out(self, digest: str, destination: Path):
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.blob(digest), destination)

    # -- templates -------------------------------------------------------------------------------
    def template(self, recipe: str) -> Path | None:
        folder = self.templates / recipe
        return folder if (folder / "template.json").is_file() else None

    def adopt_template(self, prepared: Path, recipe: str, inputs: dict, seen_as: list[Path] = ()) -> Path:
        """Moves a freshly prepared instance into the store as the recipe's template, interning its read-only
        files. ``prepared`` must be on the store's volume (a rename, not a copy). ``seen_as`` are other spellings of
        its folder the launcher may have written into it (through a link, as a snapshot's lab folders are)."""
        folder = self.templates / recipe
        if folder.exists():
            raise StoreError(f"template {recipe} already exists")
        folder.parent.mkdir(parents=True, exist_ok=True)
        folder.mkdir()
        os.rename(prepared, folder / "instance")
        links = {}
        for path, rel in _files(folder / "instance"):
            if self.layout.linkable(rel):
                links[rel] = self.intern(path)
        manifest = {"recipe": recipe, "inputs": inputs, "prepared_as": str(prepared), "name": prepared.name,
                    "seen_as": [str(p) for p in seen_as if str(p) != str(prepared)],
                    "created": time.time(), "links": links}
        (folder / "template.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
        return folder

    def touch(self, folder: Path):
        (folder / "last-used").write_text(str(time.time()), encoding="ascii")

    # -- instances -------------------------------------------------------------------------------
    def materialize(self, recipe: str, destination: Path) -> dict:
        """A working instance from the recipe's template at ``destination``: links for read-only files, copies
        for the rest, the instance's own path and name written into the two files that hold them."""
        folder = self.template(recipe)
        if folder is None:
            raise StoreError(f"no template for recipe {recipe}")
        if destination.exists():
            raise StoreError(f"{destination} already exists")
        manifest = json.loads((folder / "template.json").read_text(encoding="utf-8"))
        source = folder / "instance"
        old_root, old_name = Path(manifest["prepared_as"]), manifest["name"]
        linked = copied = 0
        for path, rel in _files(source):
            target = destination / rel
            if rel in manifest["links"]:
                self.link_into(manifest["links"][rel], target)
                linked += path.stat().st_size
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
                copied += path.stat().st_size
        for rel in self.layout.rewrite:
            for root in [old_root, *map(Path, manifest.get("seen_as") or [])]:
                rewrite_paths(destination / rel, root, destination, old_name, destination.name)
        record = {"schema": 1, "recipe": recipe, "template": str(folder), "created": time.time(),
                  "links": manifest["links"], "world": None}
        (destination / MARKER).write_text(json.dumps(record, indent=1), encoding="utf-8")
        self.touch(folder)
        return {"linked_bytes": linked, "copied_bytes": copied}

    def remove(self, instance: Path):
        """Removes an instance; shared blobs are untouched and stay read-only."""
        instance = Path(instance)
        marker = instance / MARKER
        links = json.loads(marker.read_text(encoding="utf-8")).get("links", {}) if marker.is_file() else {}
        remove_tree(instance)
        for digest in set(links.values()):
            if self.blob(digest).exists():
                _read_only(self.blob(digest))

    # -- worlds ----------------------------------------------------------------------------------
    def snapshot(self, instance: Path, label: str, key: dict) -> dict:
        folder = self.worlds / label
        if folder.exists():
            raise StoreError(f"world snapshot {label} already exists")
        if not self.layout.world_parts:
            raise StoreError("the layout names no world_parts, so there is no world to snapshot")
        files, total = {}, 0
        for part in self.layout.world_parts:
            base = instance / part
            if not base.exists():
                continue
            for path, rel in _files(base):
                if path.name == "session.lock":
                    continue
                digest = sha256(path)
                target = self.blob(digest)
                if not target.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(path, target)  # a copy: the instance keeps writing its own file
                    _read_only(target)
                files[f"{part}/{rel}"] = digest
                total += path.stat().st_size
        if not any(name.startswith(self.layout.world_parts[0] + "/") for name in files):
            raise StoreError(f"{instance.name} has no generated world to snapshot")
        folder.mkdir(parents=True)
        record = {"label": label, "key": key, "files": files, "bytes": total, "created": time.time(),
                  "from": instance.name}
        (folder / "world.json").write_text(json.dumps(record, indent=1), encoding="utf-8")
        return record

    def world(self, label: str) -> dict:
        path = self.worlds / label / "world.json"
        if not path.is_file():
            raise StoreError(f"no world snapshot {label}")
        return json.loads(path.read_text(encoding="utf-8"))

    def restore(self, instance: Path, label: str) -> int:
        record = self.world(label)
        for part in self.layout.world_parts:
            if (instance / part).exists():
                remove_tree(instance / part)
        for rel, digest in record["files"].items():
            self.copy_out(digest, instance / rel)
        self.touch(self.worlds / label)
        return record["bytes"]

    # -- housekeeping ----------------------------------------------------------------------------
    def last_used(self, folder: Path) -> float:
        marker = folder / "last-used"
        try:
            return float(marker.read_text(encoding="ascii"))
        except (OSError, ValueError):
            return folder.stat().st_mtime

    def referenced(self) -> set[str]:
        digests = set()
        for manifest in self.templates.glob("*/template.json"):
            digests.update(json.loads(manifest.read_text(encoding="utf-8"))["links"].values())
        for manifest in self.worlds.glob("*/world.json"):
            digests.update(json.loads(manifest.read_text(encoding="utf-8"))["files"].values())
        return digests

    def orphan_blobs(self) -> list[Path]:
        """Blobs no template or snapshot names and no instance links to."""
        keep = self.referenced()
        out = []
        for path in self.blobs.glob("*/*") if self.blobs.exists() else []:
            if path.name not in keep and path.stat().st_nlink <= 1:
                out.append(path)
        return out


def rewrite_paths(path: Path, old_root: Path, new_root: Path, old_name: str, new_name: str):
    """Writes an instance's own folder and name into a JSON file copied from its template."""
    if not path.is_file():
        return
    text = path.read_text(encoding="utf-8")
    for old, new in ((str(old_root), str(new_root)),
                     (str(old_root).replace("\\", "/"), str(new_root).replace("\\", "/")),
                     (json.dumps(str(old_root))[1:-1], json.dumps(str(new_root))[1:-1])):
        text = text.replace(old, new)
    try:
        data = json.loads(text)
    except ValueError:
        path.write_text(text, encoding="utf-8")
        return
    if isinstance(data, dict) and data.get("name") == old_name:
        data["name"] = new_name
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def remove_tree(path: Path):
    """Removes a folder by the unlink-first routine (``wb.platform.remove_tree``): every link in it (a symlink or a
    junction) is removed as a link before anything else, so nothing it points at is touched, then the rest, read-only
    hard links to the blobs included (Windows refuses to delete a read-only file)."""
    from wb.platform import remove_tree as unlink_first
    unlink_first(Path(path))


def recipe_of(inputs: dict) -> str:
    return hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()[:24]
