"""GroovyScript's own rules for what runs and in what order, as GroovyScript 1.4 reads them.

runConfig.json names the paths each loader (preInit, init, postInit, in that order) runs; a path is
sanitized (both separators made the system's, trailing ones dropped), a path a later loader shares with an
earlier one (one a string prefix of the other) is dropped from the later loader, and a deprecated
``classes`` entry is moved into the loaders. Each path is walked and its files sorted by their whole path
string, so the system's separator orders them; a file another path already selected stays where it is,
unless this path names it more specifically (more separators), which moves it to the end. A file then runs
only if the preprocessor comments at its top allow it.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

STAGES = ("preInit", "init", "postInit")
SUFFIXES = (".groovy", ".gvy", ".gy", ".gsh")
PREPROCESSORS = ("NO_RUN", "DEBUG_ONLY", "NO_RELOAD", "MODS_LOADED", "SIDE", "PACKMODE")
FILE_SYSTEMS = {"windows": ("\\", False), "linux": ("/", True), "macos": ("/", False)}  # separator, case-sensitive
SIDES = ("client", "server")
_CAMEL = re.compile(r"[a-z]+[a-zA-Z]*|[A-Z][a-zA-Z]*")


@dataclass
class Config:
    pack_id: str
    pack_name: str
    debug: bool
    loaders: dict[str, list[str]]  # each loader's paths, sanitized, in / form
    packmodes: list[str]  # as GroovyScript compares them (lower_underscore)
    default_packmode: str | None
    integrate_packmode_mod: bool
    problems: list[dict] = field(default_factory=list)


@dataclass
class Preprocessor:
    name: str
    args: list[str]
    line: int


@dataclass
class Selected:
    file: str  # the file's path under the groovy folder, / separated, as on disk
    via: str  # the loader path that placed it
    specificity: int


def packmode_name(value: str) -> str:
    """A packmode as GroovyScript compares it: converted to lower_underscore."""
    value = value.strip()
    if _CAMEL.fullmatch(value):
        return re.sub(r"(?<!^)(?=[A-Z])", "_", value).lower()
    return value.lower().replace(" ", "_")


def sanitize(path: str) -> str:
    path = path.replace("\\", "/")
    while path.endswith("/"):
        path = path[:-1]
    return path


def _lenient(text: str) -> str:
    """JSON as Gson's lenient reader takes it, made strict: comments, single quotes and trailing commas."""
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c in "\"'":
            j, body = i + 1, []
            while j < n and text[j] != c:
                if text[j] == "\\" and j + 1 < n:
                    body.append("'" if text[j + 1] == "'" else text[j:j + 2])
                    j += 2
                    continue
                body.append('\\"' if text[j] == '"' else text[j])
                j += 1
            out.append('"' + "".join(body) + '"')
            i = j + 1
        elif text.startswith("//", i) or c == "#":
            i = text.find("\n", i) if "\n" in text[i:] else n
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
        else:
            out.append(c)
            i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


DEFAULT = json.dumps({"packName": "PlaceHolder name", "packId": "placeholdername", "version": "1.0.0", "debug": False,
                      "loaders": {"preInit": ["classes/", "preInit/"], "postInit": ["postInit/"]},
                      "packmode": {"values": [], "default": "", "integratePackmodeMod": False}})  # what it writes


def load(path: Path) -> Config:
    """runConfig.json as GroovyScript reads it (leniently, as Gson does), or the one it writes when there is
    none; ValueError when it holds no object."""
    if not path.exists():
        config = parse(DEFAULT)
        config.problems.append({"kind": "default", "detail": "there is no runConfig.json: GroovyScript writes its "
                                                             "default (classes/ and preInit/, then postInit/)"})
        return config
    return parse(path.read_text(encoding="utf-8-sig"))


def parse(text: str) -> Config:
    problems: list[dict] = []
    try:
        raw = json.loads(text)
    except ValueError:
        raw = json.loads(_lenient(text))
        problems.append({"kind": "lenient", "detail": "runConfig.json is not strict JSON (comments, single quotes or "
                                                      "trailing commas); GroovyScript reads it leniently, other tools "
                                                      "may not"})
    if not isinstance(raw, dict):
        raise ValueError("runConfig.json does not hold a JSON object")
    loaders = raw.get("loaders")
    if not isinstance(loaders, dict):
        problems.append({"kind": "loaders", "detail": "runConfig.json defines no loaders object: no script runs"})
        loaders = {}
    loaders = {name: list(value) if isinstance(value, list) else value for name, value in loaders.items()}
    if "classes" in raw:
        problems.append({"kind": "classes", "detail": "runConfig.json has a classes entry, which GroovyScript reports "
                                                      "as deprecated and moves into the loaders"})
        _migrate(raw["classes"], loaders)
    kept: dict[str, list[str]] = {}
    claimed: list[tuple[str, str]] = []
    for loader, value in loaders.items():
        if not isinstance(value, list):
            problems.append({"kind": "loaders", "detail": f"loader {loader} is not a list of paths"})
            value = []
        paths: list[str] = []
        for element in value:
            if not isinstance(element, (str, int, float, bool)):
                problems.append({"kind": "loaders", "detail": f"loader {loader} holds {element!r}, not a path"})
                continue
            entry = sanitize(str(element))
            if entry in paths:
                continue
            clash = [(other, taken) for other, taken in claimed if taken.startswith(entry) or entry.startswith(taken)]
            if clash:
                other, taken = clash[0]
                longer = taken if len(taken) > len(entry) else entry
                problems.append({"kind": "multiple-loaders", "loader": loader, "path": entry, "detail":
                                 f"files in '{longer}' are configured for loaders {other} and {loader}: GroovyScript "
                                 f"logs a fatal error and drops '{entry}' from {loader}"})
                continue
            paths.append(entry)
        kept[loader] = paths
        claimed += [(loader, entry) for entry in paths]
    for loader, paths in kept.items():
        if loader not in STAGES and paths:
            problems.append({"kind": "unknown-loader", "loader": loader, "detail":
                             f"loader '{loader}' is not one GroovyScript runs ({', '.join(STAGES)}): its paths never "
                             "run"})
    packmode = raw.get("packmode") if isinstance(raw.get("packmode"), dict) else {}
    values = packmode.get("values", packmode.get("types", []))
    modes = list(dict.fromkeys(packmode_name(str(v)) for v in values if isinstance(v, str) and v.strip())) \
        if isinstance(values, list) else []
    default = packmode.get("default")
    default = packmode_name(default) if isinstance(default, str) and default.strip() else None
    if default and modes and default not in modes:
        problems.append({"kind": "packmode",
                         "detail": f"the default packmode {default} is not one of {', '.join(modes)}"})
    pack_id = raw.get("packId") if isinstance(raw.get("packId"), str) else ""
    return Config(pack_id=pack_id, pack_name=str(raw.get("packName") or ""), debug=raw.get("debug") is True,
                  loaders=kept, packmodes=modes, default_packmode=default,
                  integrate_packmode_mod=packmode.get("integratePackmodeMod") is True, problems=problems)


def _migrate(classes, loaders: dict) -> None:
    def append(loader: str, value) -> None:
        entry = loaders.setdefault(loader, [])
        if isinstance(entry, list):
            entry.append(value)
    if isinstance(classes, list):
        for value in classes:
            if isinstance(value, (str, int, float, bool)):
                append("preInit", value)
    elif isinstance(classes, dict):
        for loader, value in classes.items():
            for item in value if isinstance(value, list) else [value]:
                if isinstance(item, (str, int, float, bool)):
                    append(loader, item)


def namespace(config: Config) -> str:
    """The namespace GroovyScript gives the pack's own names: its packId when valid."""
    return config.pack_id if re.fullmatch(r"[a-z0-9_]+", config.pack_id or "") else "groovyscript"


# -- which files, in what order --------------------------------------------------------------------------
def locate(root: Path, entry: str, case_sensitive: bool) -> tuple[Path | None, str]:
    """The file or folder a loader path names under ``root``, and how: exact, case (only a case-insensitive
    file system finds it), missing, or outside (it leaves the groovy folder, which this analysis does not)."""
    parts = [p for p in entry.split("/") if p not in ("", ".")]
    if entry.startswith("/") or re.match(r"^[A-Za-z]:", entry) or ".." in parts:
        return None, "outside"
    current, how = root, "exact"
    for part in parts:
        try:
            names = os.listdir(current)
        except OSError:
            return None, "missing"
        if part in names:
            current = current / part
            continue
        folded = [n for n in names if n.casefold() == part.casefold()]
        if not folded:
            return None, "missing"
        if case_sensitive:
            return None, "case"
        current, how = current / folded[0], "case"
    return current, how


def walk(target: Path, follow: bool) -> list[Path]:
    """The script files at or under ``target``; linked folders are entered only when ``follow``, once each."""
    if target.is_file():
        return [target] if target.name.endswith(SUFFIXES) else []
    found, seen = [], set()
    for folder, dirs, files in os.walk(target, followlinks=follow):
        if follow:
            real = os.path.realpath(folder)
            if real in seen:
                dirs[:] = []
                continue
            seen.add(real)
        dirs.sort()
        found += [Path(folder) / name for name in sorted(files) if name.endswith(SUFFIXES)]
    return found


def stage_files(root: Path, config: Config, stage: str, file_system: str) -> list[Selected]:
    """The files a loader runs, in order, before preprocessors; follows links only in debug, as GroovyScript."""
    separator, case_sensitive = FILE_SYSTEMS[file_system]
    chosen: dict[str, Selected] = {}
    for entry in config.loaders.get(stage, []):
        target, how = locate(root, entry, case_sensitive)
        if target is None:
            continue
        specificity = entry.count("/")
        files = walk(target, config.debug)
        keyed = []
        for path in files:
            below = path.relative_to(target).as_posix() if path != target else ""
            keyed.append(((separator + below.replace("/", separator)).encode("utf-16-be"), path))
        for _, path in sorted(keyed, key=lambda pair: pair[0]):
            relative = path.relative_to(root).as_posix()
            key = relative if case_sensitive else relative.casefold()
            held = chosen.get(key)
            if held is None:
                chosen[key] = Selected(relative, entry, specificity)
            elif specificity > held.specificity:
                del chosen[key]
                chosen[key] = Selected(relative, entry, specificity)
    return list(chosen.values())


# -- preprocessors -------------------------------------------------------------------------------------------
def _trim(text: str) -> str:
    """Java's String.trim: every character up to a space, from both ends."""
    start, end = 0, len(text)
    while start < end and text[start] <= " ":
        start += 1
    while end > start and text[end - 1] <= " ":
        end -= 1
    return text[start:end]


def _near(head: str) -> str | None:
    name = re.sub(r"[\s\-]+", "_", head.strip().lstrip("*").strip()).upper()
    return name if name in PREPROCESSORS else None


def preprocessors(text: str) -> tuple[list[Preprocessor], list[dict]]:
    """The preprocessors GroovyScript reads at the top of a file, and comments that look like one but are not
    read as one (misspelled, or after GroovyScript stopped reading)."""
    found: list[Preprocessor] = []
    problems: list[dict] = []
    in_block, stop, why = False, None, "the first line of code"
    lines = re.split(r"\r\n|\r|\n", text)
    for number, raw in enumerate(lines, 1):
        line = _trim(raw)
        if not line:
            continue
        one_line = False
        if line.startswith("/*"):
            if line.endswith("*/") and len(line) >= 4:
                line, one_line = _trim(line[2:-2]), True
            elif "*/" in line:
                stop, why = number, "code after a block comment"
                break
            else:
                in_block, line = True, _trim(line[2:])
            if not line:
                continue
        if line.startswith("//"):
            line = _trim(line[2:])
            if not line:
                continue
        elif not in_block:
            stop = number
            why = "a one-line /* */ comment" if one_line else why
            break
        if in_block and line.endswith("*/"):
            in_block = False
        head = line.split(":", 1)[0]
        if head.upper() in PREPROCESSORS:
            args = _split_args(line.split(":", 1)[1]) if ":" in line else []
            found.append(Preprocessor(head.upper(), args, number))
        elif _near(head):
            problems.append({"kind": "unread-preprocessor", "line": number, "detail":
                             f"line {number} looks like the {_near(head)} preprocessor but GroovyScript does not read "
                             f"it as one: write '// {_near(head)}{': ...' if ':' in line else ''}'"})
    if stop is not None:
        for number in range(stop, len(lines) + 1):
            match = re.match(r"\s*(?://|/\*+|\*)\s*([A-Za-z_\- ]+?)\s*(?::|\*/|$)", lines[number - 1])
            if match and _near(match.group(1)):
                name = _near(match.group(1))
                problems.append({"kind": "late-preprocessor", "line": number, "detail":
                                 f"the {name} comment on line {number} is not read: GroovyScript stops reading "
                                 f"preprocessors at line {stop} ({why})"})
    return found, problems


def _split_args(text: str) -> list[str]:
    parts = text.split(",")
    while parts and parts[-1] == "":
        parts.pop()  # as Java's split drops trailing empty strings
    return [_trim(p) for p in parts]


def check(found: list[Preprocessor], config: Config) -> list[dict]:
    """Preprocessor arguments GroovyScript reports as errors when the file is loaded."""
    problems = []
    for prep in found:
        if prep.name == "SIDE" and (len(prep.args) != 1 or prep.args[0].upper() not in ("CLIENT", "SERVER")):
            problems.append({"kind": "side", "line": prep.line, "detail":
                             f"SIDE on line {prep.line} takes one argument, client or server; GroovyScript logs an "
                             "error and runs the file on both sides"})
        if prep.name == "PACKMODE":
            known = config.packmodes
            for arg in prep.args:
                if not config.integrate_packmode_mod and packmode_name(arg) not in known:
                    problems.append({"kind": "packmode", "line": prep.line, "detail":
                                     f"PACKMODE on line {prep.line} names {arg}, which is not a packmode "
                                     f"runConfig.json defines ({', '.join(known) or 'none'}); GroovyScript logs an "
                                     "error"})
            if not prep.args:
                problems.append({"kind": "packmode", "line": prep.line, "detail":
                                 f"PACKMODE on line {prep.line} names no packmode: the file never runs"})
    return problems


def allows(found: list[Preprocessor], *, packmode: str | None, side: str, debug: bool,
           mods: set[str] | None) -> tuple[bool, str | None]:
    """Whether a file runs in a configuration, and why not. ``mods`` None: MODS_LOADED assumed met."""
    for prep in found:
        if prep.name == "NO_RUN":
            return False, "NO_RUN"
        if prep.name == "DEBUG_ONLY" and not debug:
            return False, "DEBUG_ONLY, and debug is off"
        if prep.name == "SIDE" and len(prep.args) == 1 and prep.args[0].upper() in ("CLIENT", "SERVER") \
                and prep.args[0].lower() != side:
            return False, f"SIDE {prep.args[0].lower()}"
        if prep.name == "PACKMODE" and (packmode is None or packmode not in {packmode_name(a) for a in prep.args}):
            return False, f"PACKMODE {', '.join(prep.args)}"
        if prep.name == "MODS_LOADED" and mods is not None:
            absent = [a for a in prep.args if a not in mods]
            if absent:
                return False, f"MODS_LOADED {', '.join(absent)} (absent)"
    return True, None
