"""The world sampler contract: how a world command has a world generator sample its own world, with its own code
and without the game. A generator implements it with a small program in any language; world.golden and
world.preview drive it.

The caller gives the program as a command line, a list of strings run in a folder of the caller's choosing (the
generator's checkout), with placeholders:

    {windows}   the windows file to read, written afresh for every run (below)
    {out}       the file to write, JSON lines (below)
    {threads}   how many threads to sample with
    {order}     forward or reverse: the order to sample each window's own parts in (its tiles, rows or chunks)
    {python}    the interpreter running Workbench, for a sampler written in Python

``{{`` and ``}}`` stand for braces. The windows file, ``wb/world-windows/v1``:

    {
      "format": "wb/world-windows/v1",
      "threads": 4, "order": "reverse",           as {threads} and {order}
      "windows": [                                in the order to take them, threads allowing
        {"name": "origin",                        unique: it names the window's lines in the output
         "seed": 4242,                            an integer or a string, as the generator takes seeds
         "columns": [-512, -512, 511, 511],       x1,z1,x2,z2: block columns, corners inclusive, low first; or
         "box": [0, 0, 0, 63, 255, 63],           x1,y1,z1,x2,y2,z2: blocks, the same way (a window has one)
         "step": 8,                               every 8th block along each axis, from the low corner
         "layers": ["height", "biome"],           what to sample: names the generator defines
         "options": {"sea_level": 63},            the generator's own settings, passed on as given
         "raster": []}                            layers to return whole as well (for world.preview)
      ]
    }

A window samples x1, x1 + step, ... up to x2, (x2 - x1) // step + 1 positions, and so along z (and y, in a box).
The sampler writes a line per window to {out}, the windows in any order:

    {"window": "origin",
     "layers": {
       "height": {"sha256": "<64 hex>",           SHA-256 over the layer's values in a canonical byte order
                  "summary": {"min": 52, "max": 140},     optional: a few numbers or counts that say what
                                                  changed when the digest does, for people; never compared
                  "raster": {"width": 128, "height": 128, "values": [63, 64, ...]}},   layers in "raster" only
       "biome": {"sha256": "<64 hex>", "summary": {"ocean": 7383, "plains": 9001}}},
     "seconds": 1.25}                             optional: the window's own time, kept as a measurement

A window's layers may come in several lines instead, none twice; every layer asked for must come, and no other.

A canonical byte order depends on the window and its values alone: never on the threads, the order or the
windows sampled before. The one to use unless the generator has its own: the sampled positions in y, z, x order,
lowest first (z, x for columns), each value as text followed by "\\n", as world digests hash block states
(digest.py). A raster runs the same way, row by row: width values along x, height rows along z; numbers or text.

The sampler exits 0 once every window is written; any other exit fails the run, with the last lines it printed.
Everything it prints goes to the job log.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from wb_core.module import Context, EnvironmentProblem, ParamError

from .units import many

FORMAT = "wb/world-windows/v1"
PLACEHOLDERS = ("windows", "out", "threads", "order", "python")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")  # both matched whole: a name may become a file's
SHA256 = re.compile(r"[0-9a-f]{64}")
WINDOW_KEYS = {"name", "seed", "columns", "box", "step", "layers", "options"}
_TOKEN = re.compile(r"\{\{|\}\}|\{([^{}]*)\}")
_ABSOLUTE = re.compile(r"^(?:[A-Za-z]:[\\/]|/)")


@dataclass
class Window:
    """A part of a world to sample: where, how finely, with what seed and settings, and which layers."""

    name: str
    seed: int | str
    layers: tuple[str, ...]
    columns: tuple[int, ...] | None = None  # x1, z1, x2, z2, low first
    box: tuple[int, ...] | None = None      # x1, y1, z1, x2, y2, z2, low first
    step: int = 1
    options: dict = field(default_factory=dict)
    raster: tuple[str, ...] = ()

    def entry(self) -> dict:
        """The window as the windows file gives it."""
        area = {"columns": list(self.columns)} if self.columns is not None else {"box": list(self.box)}
        return {"name": self.name, "seed": self.seed, **area, "step": self.step, "layers": list(self.layers),
                "options": self.options, "raster": list(self.raster)}


def parse_window(entry, defaults: dict, where: str, extra: frozenset[str] = frozenset()) -> Window:
    """A window from its JSON form, ``defaults`` giving the seed, layers, step and options it leaves out (its own
    options are laid over those). Raises ValueError, saying what is wrong."""
    if not isinstance(entry, dict):
        raise ValueError(f"{where} is not a JSON object")
    name = entry.get("name")
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise ValueError(f"{where} needs a name of letters, digits, '.', '_' and '-'")
    where = f"window {name}"
    unknown = set(entry) - WINDOW_KEYS - extra
    if unknown:
        raise ValueError(f"{where} has unknown keys {sorted(unknown)}")
    seed = entry.get("seed", defaults.get("seed"))
    if isinstance(seed, bool) or not isinstance(seed, (int, str)):
        raise ValueError(f"{where} needs a seed, an integer or a string (its own or the file's)")
    layers = entry.get("layers", defaults.get("layers"))
    named = isinstance(layers, list) and all(isinstance(layer, str) and NAME.fullmatch(layer) for layer in layers)
    if not (named and layers and len(set(layers)) == len(layers)):
        raise ValueError(f"{where} needs layers, distinct names of letters, digits, '.', '_' and '-' (its own or "
                         "the file's)")
    if ("columns" in entry) == ("box" in entry):
        raise ValueError(f"{where} needs columns (x1,z1,x2,z2) or a box (x1,y1,z1,x2,y2,z2): one of the two")
    kind, size = ("columns", 4) if "columns" in entry else ("box", 6)
    area = entry[kind]
    if not (isinstance(area, list) and len(area) == size and all(_whole(value) for value in area)):
        raise ValueError(f"{where}: {kind} takes {size} whole numbers, two corners")
    half = size // 2
    corners = tuple(min(area[i], area[i + half]) for i in range(half)) + \
        tuple(max(area[i], area[i + half]) for i in range(half))
    step = entry.get("step", defaults.get("step", 1))
    if not _whole(step) or step < 1:
        raise ValueError(f"{where}: step must be a whole number, 1 or more")
    options: dict = {}
    for whose, value in (("the file's", defaults.get("options", {})), ("its", entry.get("options", {}))):
        if not isinstance(value, dict):
            raise ValueError(f"{where}: {whose} options are not a JSON object")
        options.update(value)
    return Window(name, seed, tuple(layers), step=step, options=options, **{kind: corners})


def command(ctx: Context, sampler: list[str]) -> list[str]:
    """The sampler's command line, checked, with its absolute host paths as the job sees them."""
    if not sampler or not str(sampler[0]).strip():
        raise ParamError("sampler: give the sampler's command line, its program first")
    marked = expand(sampler, {name: f"\0{name}\0" for name in PLACEHOLDERS})
    for name, what in (("windows", "the windows file it reads"), ("out", "the file it writes")):
        if not any(f"\0{name}\0" in arg for arg in marked):
            raise ParamError(f"sampler: the command line must pass {{{name}}}, {what}")
    return _translated(ctx, [str(arg) for arg in sampler])


def expand(sampler: list[str], values: dict[str, str]) -> list[str]:
    """The command line with its placeholders filled in."""
    def fill(match: re.Match) -> str:
        token = match.group(0)
        if token in ("{{", "}}"):
            return token[0]
        if match.group(1) not in values:
            known = ", ".join(f"{{{name}}}" for name in PLACEHOLDERS)
            raise ParamError(f"sampler: {token} is no placeholder (there are {known}; {{{{ and }}}} are braces)")
        return values[match.group(1)]
    return [_TOKEN.sub(fill, str(arg)) for arg in sampler]


def cores() -> int:
    """The cores this process may use, at least 2, so that a run on them has threads at work together."""
    count = getattr(os, "process_cpu_count", os.cpu_count)()
    return max(2, count or 2)


@dataclass
class Run:
    """One run of a sampler over its windows, and what it wrote."""

    number: int
    threads: int
    order: str                                                          # {order}: forward or reverse
    label: str                                                          # for people: "4 threads, reverse order"
    windows: list[str]                                                  # in the order the windows file gave them
    seconds: float = 0.0
    digests: dict[str, dict[str, dict]] = field(default_factory=dict)  # window, layer: sha256, summary, raster
    timings: dict[str, float] = field(default_factory=dict)            # window: its seconds, as the sampler said

    def describe(self) -> dict:
        return {"run": self.number, "threads": self.threads, "order": self.order, "windows": self.windows,
                "seconds": round(self.seconds, 3), "windowSeconds": self.timings}


def run(ctx: Context, sampler: list[str], cwd: Path, windows: list[Window], *, number: int, threads: int,
        order: str, label: str) -> Run:
    """Runs the sampler once over the windows, in their order, and reads what it wrote. The run's windows file,
    output and log are kept in run-<number> of the output folder. Raises EnvironmentProblem when the sampler cannot
    start, fails, or writes what the contract does not allow."""
    folder = ctx.out / f"run-{number}"
    folder.mkdir(parents=True, exist_ok=True)
    windows_file, out = folder / "windows.json", folder / "digests.jsonl"
    out.unlink(missing_ok=True)
    windows_file.write_text(json.dumps({"format": FORMAT, "threads": threads, "order": order,
                                        "windows": [window.entry() for window in windows]}, indent=2) + "\n",
                            encoding="utf-8")
    args = expand(sampler, {"windows": str(windows_file), "out": str(out), "threads": str(threads),
                            "order": order, "python": sys.executable})
    ctx.progress(f"run {number}", None, f"{many(len(windows), 'window')} on {label}")
    started = time.monotonic()
    try:
        done = ctx.run(_program(args, cwd), cwd=cwd)
    except OSError as error:
        raise EnvironmentProblem(f"the sampler could not start: {error}") from None
    seconds = time.monotonic() - started
    (folder / "sampler.log").write_text(done.stdout + "\n" if done.stdout else "", encoding="utf-8")
    ctx.output(f"run-{number}", folder)
    if done.returncode != 0:
        said = [line.strip() for line in done.stdout.splitlines() if line.strip()][-3:]
        raise EnvironmentProblem(f"the sampler exited {done.returncode} on run {number} ({label}): "
                                 f"{' | '.join(said) or 'it printed nothing'}")
    digests, timings, problems = read(out, windows)
    if problems:
        for problem in problems:
            ctx.log.warning(f"run {number}: {problem}")
        more = f" (and {many(len(problems) - 1, 'other problem')}, in the job log)" if len(problems) > 1 else ""
        raise EnvironmentProblem(f"run {number} ({label}) broke the sampler contract: {problems[0]}{more}")
    return Run(number, threads, order, label, [window.name for window in windows], seconds, digests, timings)


def read(path: Path, windows: list[Window]) -> tuple[dict[str, dict[str, dict]], dict[str, float], list[str]]:
    """What a sampler wrote: the digests by window and layer, the windows' own times, and every way the output
    breaks the contract."""
    asked = {window.name: window for window in windows}
    digests: dict[str, dict[str, dict]] = {}
    timings: dict[str, float] = {}
    problems: list[str] = []
    try:
        handle = path.open(encoding="utf-8-sig")  # a byte order mark, as some Windows tools write, is no fault
    except FileNotFoundError:
        return {}, {}, [f"it wrote no {path.name}"]
    with handle:
        try:
            for number, line in enumerate(handle, 1):
                if line.strip():
                    _line(number, line, asked, digests, timings, problems)
        except UnicodeDecodeError as error:
            problems.append(f"{path.name} is not UTF-8 text ({error.reason})")
    for window in windows:
        missing = [layer for layer in window.layers if layer not in digests.get(window.name, {})]
        if missing:
            problems.append(f"window {window.name} has no digest of {', '.join(missing)}")
    return digests, timings, problems


def _line(number: int, line: str, asked: dict[str, Window], digests: dict, timings: dict, problems: list) -> None:
    try:
        entry = json.loads(line)
    except json.JSONDecodeError as error:
        problems.append(f"line {number} is not JSON ({error.msg})")
        return
    if not isinstance(entry, dict) or not isinstance(entry.get("window"), str) \
            or not isinstance(entry.get("layers"), dict):
        problems.append(f"line {number} is not an object with a window and its layers")
        return
    window = asked.get(entry["window"])
    if window is None:
        problems.append(f"line {number} is of window {entry['window']!r}, which was not asked for")
        return
    seconds = entry.get("seconds")
    if isinstance(seconds, (int, float)) and not isinstance(seconds, bool):
        timings[window.name] = round(timings.get(window.name, 0.0) + seconds, 6)
    kept = digests.setdefault(window.name, {})
    for layer, value in entry["layers"].items():
        where = f"layer {layer} of window {window.name}"
        if layer not in window.layers:
            problems.append(f"line {number} has {where}, which was not asked for")
        elif layer in kept:
            problems.append(f"line {number} has {where} a second time")
        else:
            try:
                kept[layer] = _layer(value, layer in window.raster)
            except ValueError as error:
                problems.append(f"line {number}: {where} {error}")


def _layer(value, raster: bool) -> dict:
    if not isinstance(value, dict):
        raise ValueError("is not a JSON object")
    sha = value.get("sha256")
    if not isinstance(sha, str) or not SHA256.fullmatch(sha.lower()):
        raise ValueError("has no sha256 of 64 hex digits")
    out = {"sha256": sha.lower()}
    if value.get("summary") is not None:
        if not isinstance(value["summary"], dict):
            raise ValueError("has a summary that is not a JSON object")
        out["summary"] = value["summary"]
    if raster:
        out["raster"] = _raster(value.get("raster"))
    return out


def _raster(value) -> dict:
    if not isinstance(value, dict):
        raise ValueError("has no raster, though one was asked for")
    width, height, values = value.get("width"), value.get("height"), value.get("values")
    if not all(_whole(side) and side > 0 for side in (width, height)):
        raise ValueError("has a raster without a width and height of 1 or more")
    if not isinstance(values, list) or len(values) != width * height:
        count = len(values) if isinstance(values, list) else "no"
        raise ValueError(f"has a raster of {count} values where width x height is {width * height}")
    return {"width": width, "height": height, "values": values}


def _program(args: list[str], cwd: Path) -> list[str]:
    """A program named by a path relative to the working folder, taken from there on every system (Windows would
    look for it beside the worker instead)."""
    first = args[0]
    if ("/" in first or "\\" in first) and not Path(first).is_absolute() and (cwd / first).is_file():
        return [str(cwd / first), *args[1:]]
    return args


def _translated(ctx: Context, sampler: list[str]) -> list[str]:
    """Absolute paths under a root, as the job sees them (in the container, the root's mount); any other path is
    the machine's own (/usr/bin/java) and stays as it is."""
    from wb_core.paths import PathError, PathMap
    from wb_core.settings import Root
    mapping = PathMap([Root(**root) for root in ctx.spec.get("roots", [])])
    out = []
    for arg in sampler:
        flag, eq, value = arg.partition("=") if arg.startswith("-") and "=" in arg else ("", "", arg)
        if _ABSOLUTE.match(value):
            try:
                value = mapping.resolve(value).container
            except PathError:
                pass
        out.append(flag + eq + value)
    return out


def _whole(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)
