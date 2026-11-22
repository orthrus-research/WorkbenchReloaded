"""A world save on disk: level.dat and its block registry, a dimension's region files, the part of them a
selection takes, and the rule that refuses a world a game may still have open."""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from wb_core.module import EnvironmentProblem, ParamError

from . import nbt, units
from .region import LAYOUTS, EntryError, Layout, Region
from .units import Problems, Unit


@dataclass(frozen=True)
class Selection:
    """The part of a world a command reads: a box of blocks (corners inclusive) and region file names."""

    low: tuple[int, int, int] | None = None
    high: tuple[int, int, int] | None = None
    names: frozenset[str] = frozenset()

    @classmethod
    def parse(cls, box: list[int] | None, regions: list[str]) -> Selection:
        if box is None:
            return cls(names=frozenset(regions))
        if len(box) != 6:
            raise ParamError("box takes six numbers: x1,y1,z1,x2,y2,z2, two block corners, inclusive")
        low = tuple(min(box[axis], box[axis + 3]) for axis in range(3))
        high = tuple(max(box[axis], box[axis + 3]) for axis in range(3))
        return cls(low, high, frozenset(regions))

    def describe(self) -> dict:
        return {"box": [*self.low, *self.high] if self.low else None, "regions": sorted(self.names)}

    def takes_file(self, layout: Layout, name: str, coords: tuple[int, ...]) -> bool:
        if self.names and not {name, name.removesuffix(layout.suffix)} & self.names:
            return False
        return self._meets(coords, layout.edge * 16, layout.dims)

    def takes_unit(self, layout: Layout, pos: tuple[int, ...]) -> bool:
        return self._meets(pos, 16, layout.dims)

    def takes_cube(self, pos: tuple[int, int, int]) -> bool:
        return self._meets(pos, 16, 3)

    def holds(self, block) -> bool:
        """Whether a block position (whole or fractional) lies in the box."""
        return self.low is None or all(low <= value < high + 1 for low, value, high in zip(self.low, block, self.high))

    def ranges(self, section: tuple[int, int, int]) -> tuple[range, range, range] | None:
        """The box's local x, y and z inside a section; None when the box holds all of it."""
        if self.low is None:
            return None
        spans = tuple(range(max(self.low[axis] - value * 16, 0), min(self.high[axis] - value * 16, 15) + 1)
                      for axis, value in enumerate(section))
        return None if all(len(span) == 16 for span in spans) else spans

    def _meets(self, pos: tuple[int, ...], size: int, dims: int) -> bool:
        """Whether the box meets something at ``pos`` that spans ``size`` blocks a side."""
        if self.low is None:
            return True
        axes = (0, 1, 2) if dims == 3 else (0, 2)
        return all(self.low[axis] < (value + 1) * size and value * size <= self.high[axis]
                   for axis, value in zip(axes, pos))


class Save:
    """One world save, and one dimension of it. Problems met while reading collect in ``problems``."""

    def __init__(self, root: Path, dimension: str = ""):
        if not root.is_dir():
            raise ParamError(f"{root.name} is not a folder: a world save is the folder that holds level.dat")
        self.root = root
        self.folder = root / _inside(dimension)
        self.level = _document(root / "level.dat")
        self.registry = block_registry(self.level)
        self.layouts = [layout for layout in LAYOUTS if (self.folder / layout.folder).is_dir()]
        self.problems = Problems()
        if not self.layouts:
            where = f"the dimension folder {dimension}" if dimension else "it"
            raise ParamError(f"{root.name} is not a world save: {where} has no region, region3d or region2d folder")

    def describe(self) -> dict:
        cubic = _document(self.folder / "data" / "cubicChunksData.dat")
        return {"level": level_facts(self.level), "mods": _mods(self.level), "registry": len(self.registry),
                "layouts": [layout.folder for layout in self.layouts],
                "cubic": nbt.to_json(cubic.get("data")) if cubic else None}

    def check_stopped(self, quiet: float, now: float | None = None) -> None:
        """Refuses a world a game may still have open. Newer games hold an OS lock on session.lock; 1.12 only
        writes its start time there, so a world that has a session.lock and was written within ``quiet``
        seconds counts as open (a running game saves at every autosave, 45 s apart). A world without a
        session.lock was never opened, or was copied out of one."""
        lock = self.root / "session.lock"
        if not lock.is_file():
            return
        if _held(lock):
            raise EnvironmentProblem("the world is open in a game: another program holds its session.lock")
        if quiet <= 0:
            return
        age = (time.time() if now is None else now) - self._last_write()
        if age < quiet:
            raise EnvironmentProblem(f"the world may be open in a game: it has a session.lock and was written "
                                     f"{max(age, 0):.0f} s ago; stop the game first, or pass quiet=0 for a "
                                     "world you know is stopped")

    def files(self, layout: Layout, selection: Selection) -> list[Path]:
        folder = self.folder / layout.folder
        found = []
        for path in sorted(folder.glob("*" + layout.suffix)) if folder.is_dir() else []:
            coords = layout.coords(path.name)
            if coords is not None and path.is_file() and selection.takes_file(layout, path.name, coords):
                found.append(path)
        return found

    def unmatched(self, selection: Selection) -> set[str]:
        """The selection's region names that name no file of this save."""
        names = set()
        for layout in self.layouts:
            for path in (self.folder / layout.folder).glob("*" + layout.suffix):
                names |= {path.name, path.name.removesuffix(layout.suffix)}
        return set(selection.names) - names

    @contextmanager
    def region(self, layout: Layout, name: str) -> Iterator[Region | None]:
        path = self.folder / layout.folder / name
        if not path.is_file():
            yield None
            return
        with Region(path, layout) as region:
            for problem in region.problems:
                self.problems.add(f"{layout.folder}/{name}", problem)
            yield region

    def unit(self, region: Region, index: int) -> Unit | None:
        """The unit at a region entry, or None when it cannot be read (the reason goes to ``problems``)."""
        pos = region.layout.position(region.coords, index)
        where = f"{region.layout.folder}/{region.path.name} entry {index}"
        try:
            raw = region.read(index)
            tree = nbt.parse(raw)
        except (EntryError, nbt.NbtError) as error:
            self.problems.add(where, str(error))
            return None
        said = units.claimed(region.layout.kind, tree)
        if said is not None and said != pos:  # the game moves such a unit to where its entry is; so do we
            self.problems.add(where, f"says it is at {_xyz(said)} but its entry is at {_xyz(pos)}")
        return Unit(region.layout.kind, pos, where, tree, raw)

    def units(self, selection: Selection, progress: Callable[[int, int, str], None] | None = None) -> Iterator[Unit]:
        files = [(layout, path) for layout in self.layouts for path in self.files(layout, selection)]
        for done, (layout, path) in enumerate(files):
            if progress:
                progress(done, len(files), path.name)
            with self.region(layout, path.name) as region:
                for index in region.indices() if region else ():
                    if selection.takes_unit(layout, layout.position(region.coords, index)):
                        unit = self.unit(region, index)
                        if unit is not None:
                            yield unit

    def held(self, layout: Layout) -> set[tuple[int, ...]]:
        """Every position the layout's region tables list; nothing is inflated."""
        found = set()
        for path in self.files(layout, Selection()):
            with self.region(layout, path.name) as region:
                found.update(layout.position(region.coords, index) for index in region.indices())
        return found

    def _last_write(self) -> float:
        paths = [self.root / "session.lock", self.root / "level.dat"]
        for layout in self.layouts:
            paths += (self.folder / layout.folder).glob("*" + layout.suffix)
        return max(_mtime(path) for path in paths)


@dataclass
class Pair:
    pos: tuple[int, ...]
    a: Unit | None
    b: Unit | None
    in_a: bool  # whether a's region table lists the position, read or not
    in_b: bool


def paired(a: Save, b: Save, layout: Layout, selection: Selection, wanted: set | None = None) -> Iterator[Pair]:
    """The units two saves keep at the same positions, region file by region file."""
    names = sorted({path.name for save in (a, b) for path in save.files(layout, selection)})
    for name in names:
        coords = layout.coords(name)
        with a.region(layout, name) as first, b.region(layout, name) as second:
            listed_a = set(first.indices()) if first else set()
            listed_b = set(second.indices()) if second else set()
            for index in sorted(listed_a | listed_b):
                pos = layout.position(coords, index)
                if selection.takes_unit(layout, pos) and (wanted is None or pos in wanted):
                    yield Pair(pos, a.unit(first, index) if index in listed_a else None,
                               b.unit(second, index) if index in listed_b else None,
                               index in listed_a, index in listed_b)


def block_registry(level: nbt.Compound | None) -> dict[int, str]:
    """Saved block ids and their names, from Forge's record in level.dat: FML.Registries from 1.8 on, the
    FML.ItemData (block names marked by a leading \\u0001) or BlockData lists before. Empty without one."""
    fml = _dig(level, "FML")
    ids = _dig(fml, "Registries", "minecraft:blocks", "ids")
    if isinstance(ids, list):
        return _ids(ids, "")
    for name, mark in (("ItemData", "\x01"), ("BlockData", "")):
        if isinstance(_dig(fml, name), list):
            return _ids(fml[name], mark)
    return {}


def _ids(entries: list, mark: str) -> dict[int, str]:
    found = {}
    for entry in entries:
        name, number = _dig(entry, "K"), _dig(entry, "V")
        if isinstance(name, str) and isinstance(number, int) and name.startswith(mark):
            found[int(number)] = name[len(mark):].removeprefix("\x01")
    return found


def level_facts(level: nbt.Compound | None) -> dict:
    data = _dig(level, "Data") or {}
    seed = data.get("RandomSeed")
    return nbt.to_json({
        "name": data.get("LevelName"), "seed": seed if seed is not None else _dig(data, "WorldGenSettings", "seed"),
        "generator": data.get("generatorName"), "options": data.get("generatorOptions"),
        "version": _dig(data, "Version", "Name"), "dataVersion": data.get("DataVersion"), "time": data.get("Time")})


def level_time(level: nbt.Compound | None) -> int | None:
    time_ = _dig(level, "Data", "Time")
    return int(time_) if isinstance(time_, int) else None


def _mods(level: nbt.Compound | None) -> list[dict]:
    listed = _dig(level, "FML", "ModList")
    return [{"id": str(entry.get("ModId")), "version": str(entry.get("ModVersion"))}
            for entry in listed if isinstance(entry, dict)] if isinstance(listed, list) else []


def _document(path: Path) -> nbt.Compound | None:
    if not path.is_file():
        return None
    try:
        return nbt.parse(nbt.inflate(path.read_bytes()))
    except nbt.NbtError as error:
        raise ParamError(f"{path.name} cannot be read: it {error}") from None


def _inside(dimension: str) -> str:
    path = PurePosixPath(dimension.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts or any(":" in part for part in path.parts):
        raise ParamError(f"dimension {dimension!r} must name a folder inside the save, such as DIM-1")
    return str(path)


def _held(lock: Path) -> bool:
    """Whether another program holds an OS lock on the file, as newer games do while a world is open."""
    try:
        with open(lock, "rb") as handle:
            if os.name == "nt":
                handle.read(1)  # a range another process has locked cannot be read
            else:
                import fcntl
                fcntl.lockf(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
                fcntl.lockf(handle, fcntl.LOCK_UN)
    except (BlockingIOError, PermissionError):
        return True
    return False


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _dig(tree, *keys):
    for key in keys:
        if not isinstance(tree, dict):
            return None
        tree = tree.get(key)
    return tree


def _xyz(pos: tuple[int, ...]) -> str:
    return ",".join(map(str, pos))
