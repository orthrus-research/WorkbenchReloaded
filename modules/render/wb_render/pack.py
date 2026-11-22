"""An OptiFine-format shader pack read the way its loader reads it, for render.validate.

The shaders root is the folder that holds the stage files: ``<pack>/shaders``, or the folder given when it holds the
stages itself. A stage is a file whose suffix names its kind (``STAGES``) in the root or in a world folder
(``world-1``, ``world1``, or a folder ``dimension.properties`` names). A program is the stages that share a folder and
a name: ``composite.vsh`` and ``composite.fsh`` are one program, ``world-1/composite.fsh`` is another.

Includes: ``#include "/lib/x.glsl"`` is taken from the shaders root, any other include from the including file's
folder. They are expanded where they stand, inactive preprocessor branches included, as the loader expands them
before it compiles, so every include must resolve.

Options, as the loader finds them in the sources a stage reaches:

    value option   #define NAME VALUE // [A B C]       the list of allowed values makes it an option
    switch         #define NAME  or  //#define NAME     on or off; only a name some source tests with #ifdef or
                                                        #ifndef is a switch
    const option   const int|float NAME = VALUE; // [A B C]    and    const bool NAME = true|false;

Properties files (``PROPERTIES``) are read as the loader reads them: ``#if``, ``#ifdef``, ``#ifndef``, ``#elif``,
``#else`` and ``#endif`` over option values and macros (a name that is neither is undefined, so it is 0),
``key = value`` lines, ``#`` and ``!`` comments, and ``\\`` continuations.
"""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

STAGES = {".vsh": "vert", ".tcs": "tesc", ".tes": "tese", ".gsh": "geom", ".fsh": "frag", ".csh": "comp"}
EXTENSIONS = {kind: "." + kind for kind in STAGES.values()}  # the suffix glslang reads a stage's kind from
NAMES = {"vert": "vertex", "tesc": "tessellation control", "tese": "tessellation evaluation", "geom": "geometry",
         "frag": "fragment", "comp": "compute"}
PROPERTIES = ("shaders.properties", "block.properties", "item.properties", "entity.properties")
ASSIGNMENTS = {"block.properties": "block.", "item.properties": "item.", "entity.properties": "entity."}
WORLD = re.compile(r"^world-?\d+$")
INCLUDE = re.compile(r'^\s*#\s*include\s+["<]([^">]+)[">]')
VERSION = re.compile(r"^\s*#\s*version\s+(\d+)(?:\s+([A-Za-z]+))?\s*(?://.*)?$")
DEFINE = re.compile(r"^(\s*)(//\s*)?#\s*define\s+([A-Za-z_]\w*)(?:[ \t]+([^/\r\n]*?))?[ \t]*(?://(.*))?$")
CONST = re.compile(r"^(\s*)const\s+(int|float|bool)\s+([A-Za-z_]\w*)\s*=\s*([^;]+?)\s*;\s*(?://(.*))?$")
TESTED = re.compile(r"^\s*#\s*ifn?def\s+([A-Za-z_]\w*)", re.MULTILINE)
DIRECTIVE = re.compile(r"^\s*#(ifdef|ifndef|if|elif|else|endif)\b(.*)$")
ALLOWED = re.compile(r"\[([^\]]+)\]")
MACRO = re.compile(r"^([A-Za-z_]\w*)(?:(?:\s*=\s*|\s+)(\S(?:.*\S)?))?$")


@dataclass(frozen=True)
class Problem:
    """One way the pack breaks its contract."""
    code: str
    message: str
    file: str = ""
    line: int = 0

    def __str__(self) -> str:
        where = f"{self.file}:{self.line}: " if self.file and self.line else f"{self.file}: " if self.file else ""
        return where + self.message

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "file": self.file, "line": self.line}


@dataclass
class Option:
    name: str
    kind: str          # "value" or "switch"
    default: object    # a value token, or a bool for a switch
    allowed: list
    file: str
    line: int

    def accepts(self, value) -> bool:
        return value in self.allowed


@dataclass
class Program:
    name: str                                            # "composite", "world-1/composite"
    stages: dict[str, str] = field(default_factory=dict)  # kind -> stage file

    def graphics(self) -> list[str]:
        return [self.stages[kind] for kind in ("vert", "tesc", "tese", "geom", "frag") if kind in self.stages]


@dataclass
class Flat:
    """A stage with its includes expanded: the text, and where each of its lines came from."""
    text: str
    origin: list[tuple[str, int]]

    def where(self, line: int) -> tuple[str, int]:
        return self.origin[line - 1] if 0 < line <= len(self.origin) else ("", 0)


def macro(text: str) -> tuple[str, str | None]:
    """``NAME``, ``NAME VALUE`` or ``NAME=VALUE`` as (name, value)."""
    match = MACRO.match(str(text).strip())
    if not match:
        raise ValueError(f"not a macro: {text!r} (use NAME, NAME VALUE or NAME=VALUE)")
    return match[1], match[2]


def token(value) -> object:
    """An option value as the pack writes it: a bool for a switch, else the text of the value."""
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return repr(value)
    return str(value)


def code_of(line: str, in_comment: bool) -> tuple[str, bool]:
    """A line with its comments blanked, and whether a block comment is still open at its end."""
    out, index = [], 0
    while index < len(line):
        if in_comment:
            end = line.find("*/", index)
            if end < 0:
                return "".join(out), True
            index, in_comment = end + 2, False
            out.append(" ")
        elif line.startswith("//", index):
            break
        elif line.startswith("/*", index):
            in_comment, index = True, index + 2
        else:
            out.append(line[index])
            index += 1
    return "".join(out), in_comment


def _number(text: str):
    try:
        return int(text, 0)
    except ValueError:
        pass
    try:
        return float(text.rstrip("fF"))
    except ValueError:
        return text


class Pack:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self._lines: dict[str, list[str]] = {}
        self._includes: dict[str, list[tuple[int, str, str | None]]] = {}
        self._closures: dict[str, list[str]] = {}
        self._listing: dict[Path, set[str]] = {}
        self.read_problems: list[Problem] = []
        self.folders = self._program_folders()
        self.stages = sorted(rel for rel in self._files() if self._is_stage(rel))
        self.programs = self._programs()
        self.reach = {stage: self._closure(stage, []) for stage in self.stages}
        self.sources = list(dict.fromkeys(f for stage in self.stages for f in self.reach[stage]))
        self.options = self._options()

    # -- files -----------------------------------------------------------------------------------
    def _files(self) -> list[str]:
        return sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob("*") if p.is_file())

    def _program_folders(self) -> set[str]:
        folders = {""}
        for path in self.root.iterdir() if self.root.is_dir() else []:
            if path.is_dir() and WORLD.match(path.name):
                folders.add(path.name)
        dimensions = self.root / "dimension.properties"
        if dimensions.is_file():
            for line in self.lines("dimension.properties"):
                key = line.split("=", 1)[0].strip()
                if key.startswith("dimension."):
                    folders.add(key.removeprefix("dimension."))
        return folders

    def _is_stage(self, rel: str) -> bool:
        folder, _, name = rel.rpartition("/")
        return folder in self.folders and os.path.splitext(name)[1].lower() in STAGES

    def kind(self, rel: str) -> str:
        return STAGES[os.path.splitext(rel)[1].lower()]

    def lines(self, rel: str) -> list[str]:
        if rel not in self._lines:
            raw = (self.root / rel).read_bytes()
            self._lines[rel] = raw.decode("utf-8-sig", errors="replace").splitlines()
        return self._lines[rel]

    def _programs(self) -> dict[str, Program]:
        programs: dict[str, Program] = {}
        for stage in self.stages:
            name = stage[: -len(os.path.splitext(stage)[1])]
            programs.setdefault(name, Program(name)).stages[self.kind(stage)] = stage
        return programs

    # -- includes --------------------------------------------------------------------------------
    def _exact(self, path: Path) -> bool:
        """Whether every part of ``path`` (under the root) exists with exactly this case: a zip is read case-sensitively."""
        parts = path.relative_to(self.root).parts
        current = self.root
        for part in parts:
            if current not in self._listing:
                self._listing[current] = set(os.listdir(current)) if current.is_dir() else set()
            if part not in self._listing[current]:
                return False
            current = current / part
        return True

    def includes(self, rel: str) -> list[tuple[int, str, str | None]]:
        """(line, reference, target) of every include in a file; the target is None when it does not resolve."""
        if rel in self._includes:
            return self._includes[rel]
        found = []
        for number, line in enumerate(self.lines(rel), 1):
            match = INCLUDE.match(line)
            if not match:
                continue
            reference = match[1].replace("\\", "/")
            base = self.root if reference.startswith("/") else (self.root / rel).parent
            target = Path(os.path.normpath(base / reference.lstrip("/")))
            if not target.is_relative_to(self.root):
                self.read_problems.append(Problem("include-escapes", f'#include "{reference}" leaves the shaders root',
                                                  rel, number))
                found.append((number, reference, None))
            elif not target.is_file():
                self.read_problems.append(Problem("include-missing", f'#include "{reference}" names no file', rel, number))
                found.append((number, reference, None))
            else:
                relative = target.relative_to(self.root).as_posix()
                if not self._exact(target):
                    self.read_problems.append(Problem(
                        "include-case", f'#include "{reference}" matches a file only when case is ignored; a zipped '
                        "pack is read case-sensitively", rel, number))
                found.append((number, reference, relative))
        self._includes[rel] = found
        return found

    def _closure(self, rel: str, stack: list[str]) -> list[str]:
        """Every file ``rel`` reaches through its includes, itself first."""
        if rel in self._closures:
            return self._closures[rel]
        if rel in stack:
            cycle = stack[stack.index(rel):] + [rel]
            self.read_problems.append(Problem("include-cycle", "include cycle: " + " -> ".join(cycle), rel))
            return []
        stack.append(rel)
        out = [rel]
        for _, _, target in self.includes(rel):
            if target:
                out.extend(f for f in self._closure(target, stack) if f not in out)
        stack.pop()
        self._closures[rel] = out
        return out

    def flatten(self, stage: str, values: dict | None = None, defines: list | tuple = ()) -> Flat:
        """The stage as one source: includes expanded, the options in ``values`` set where they are declared, and
        ``defines`` (pairs of name and value) inserted after ``#version``."""
        values = values or {}
        out: list[str] = []
        origin: list[tuple[str, int]] = []

        def expand(rel: str, chain: tuple[str, ...]):
            for number, line in enumerate(self.lines(rel), 1):
                if INCLUDE.match(line):
                    entry = next(e for e in self.includes(rel) if e[0] == number)
                    target = entry[2]
                    if target is None or target in chain + (rel,):
                        out.append(f"// unresolved include {entry[1]}")
                        origin.append((rel, number))
                        continue
                    out.append(f"// begin include {entry[1]}")
                    origin.append((rel, number))
                    expand(target, chain + (rel,))
                    out.append(f"// end include {entry[1]}")
                    origin.append((rel, number))
                    continue
                out.append(self._set_option(line, values) if values else line)
                origin.append((rel, number))

        expand(stage, ())
        if defines:
            at = next((i + 1 for i, line in enumerate(out) if VERSION.match(line)), 0)
            out[at:at] = [f"#define {name}" + (f" {value}" if value not in (None, "") else "") for name, value in defines]
            origin[at:at] = [("<profile macros>", 0)] * len(defines)
        return Flat("\n".join(out) + "\n", origin)

    def _set_option(self, line: str, values: dict) -> str:
        match = DEFINE.match(line)
        if match and match[3] in values and match[3] in self.options:
            indent, disabled, name, value, comment = match[1], match[2], match[3], match[4], match[5]
            option = self.options[name]
            if option.kind == "switch" and not value:
                return f"{indent}{'' if values[name] else '//'}#define {name}"
            if option.kind == "value" and value and not disabled:
                return f"{indent}#define {name} {values[name]}" + (f" //{comment}" if comment else "")
            return line
        match = CONST.match(line)
        if match and match[3] in values and match[3] in self.options:
            indent, kind, name, comment = match[1], match[2], match[3], match[5]
            value = values[name]
            text = ("true" if value else "false") if kind == "bool" else value
            return f"{indent}const {kind} {name} = {text};" + (f" //{comment}" if comment else "")
        return line

    # -- options ---------------------------------------------------------------------------------
    def _options(self) -> dict[str, Option]:
        tested = {name for rel in self.sources for name in TESTED.findall("\n".join(self.lines(rel)))}
        options: dict[str, Option] = {}
        for rel in self.sources:
            for number, line in enumerate(self.lines(rel), 1):
                match = DEFINE.match(line)
                if match:
                    disabled, name, value, comment = bool(match[2]), match[3], (match[4] or "").strip(), match[5] or ""
                    allowed = ALLOWED.search(comment)
                    if value and allowed and not disabled:
                        options.setdefault(name, Option(name, "value", value, allowed[1].split(), rel, number))
                    elif not value and name in tested:
                        options.setdefault(name, Option(name, "switch", not disabled, [True, False], rel, number))
                    continue
                match = CONST.match(line)
                if match:
                    kind, name, value, comment = match[2], match[3], match[4].strip(), match[5] or ""
                    allowed = ALLOWED.search(comment)
                    if kind == "bool" and value in ("true", "false"):
                        options.setdefault(name, Option(name, "switch", value == "true", [True, False], rel, number))
                    elif kind != "bool" and allowed:
                        options.setdefault(name, Option(name, "value", value, allowed[1].split(), rel, number))
        return options

    def defaults(self) -> dict:
        return {name: option.default for name, option in self.options.items()}

    def check_values(self, values: dict, where: str) -> list[str]:
        """What is wrong with a set of option values: names the pack does not declare, values it does not allow."""
        wrong = []
        for name, value in values.items():
            option = self.options.get(name)
            if option is None:
                wrong.append(f"{where} sets {name}, which the pack does not declare as an option")
            elif option.kind == "switch" and not isinstance(value, bool):
                wrong.append(f"{where} sets the switch {name} to {value!r}; use true or false")
            elif option.kind == "value" and value not in option.allowed:
                wrong.append(f"{where} sets {name} to {value}, not one of {' '.join(option.allowed)}")
        return wrong

    def environment(self, values: dict, defines=()) -> dict:
        """Option values and macros as the properties preprocessor sees them: a switch that is off is undefined."""
        env: dict = {}
        for name, value in {**self.defaults(), **values}.items():
            if value is True:
                env[name] = 1
            elif value is not False:
                env[name] = _number(str(value))
        for name, value in defines:
            env[name] = _number(value) if value not in (None, "") else 1
        return env

    # -- properties ------------------------------------------------------------------------------
    def properties(self, name: str, env: dict | None = None) -> tuple[dict[str, tuple[str, int]], list[Problem]]:
        """The active ``key -> (value, line)`` of a properties file, and the problems found reading it."""
        if not (self.root / name).is_file():
            return {}, []
        env = env or {}
        props: dict[str, tuple[str, int]] = {}
        problems: list[Problem] = []
        frames: list[dict] = []
        active = True
        pending, started = "", 0
        for number, raw in enumerate(self.lines(name), 1):
            directive = DIRECTIVE.match(raw)
            if directive:
                if pending:
                    problems.append(Problem("property-continuation", "a conditional interrupts a continued line",
                                            name, number))
                    pending, started = "", 0
                kind, argument = directive[1], directive[2].split("//", 1)[0].strip()
                if kind in ("if", "ifdef", "ifndef"):
                    condition = self._condition(kind, argument, env, name, number, problems)
                    frames.append({"parent": active, "taken": condition, "else": False})
                    active = active and condition
                elif kind == "elif":
                    if not frames or frames[-1]["else"]:
                        problems.append(Problem("property-conditional", "#elif without #if", name, number))
                        continue
                    frame = frames[-1]
                    condition = self._condition("if", argument, env, name, number, problems)
                    active = frame["parent"] and not frame["taken"] and condition
                    frame["taken"] = frame["taken"] or condition
                elif kind == "else":
                    if not frames or frames[-1]["else"]:
                        problems.append(Problem("property-conditional", "#else without #if, or a second #else",
                                                name, number))
                        continue
                    frame = frames[-1]
                    frame["else"] = True
                    active = frame["parent"] and not frame["taken"]
                    frame["taken"] = True
                else:
                    if not frames:
                        problems.append(Problem("property-conditional", "#endif without #if", name, number))
                        continue
                    active = frames.pop()["parent"]
                continue
            if not active:
                continue
            line = pending + raw.strip() if pending else raw.strip()
            if line.endswith("\\"):
                pending, started = line[:-1], started or number
                continue
            at, pending, started = started or number, "", 0
            if not line or line.startswith(("#", "!")):
                continue
            match = re.match(r"^((?:\\.|[^=:\s\\])+)\s*[=:]?\s*(.*)$", line)
            if not match:
                problems.append(Problem("property-malformed", f"cannot read {line[:60]!r}", name, at))
                continue
            key, value = match[1], match[2].strip()
            if key in props:
                problems.append(Problem("property-duplicate", f"{key} is set twice (first at line {props[key][1]})",
                                        name, at))
            props[key] = (value, props[key][1] if key in props else at)
        if pending:
            problems.append(Problem("property-continuation", "the last line continues into nothing", name))
        if frames:
            problems.append(Problem("property-conditional", "a conditional is never closed with #endif", name))
        return props, problems

    @staticmethod
    def _condition(kind: str, argument: str, env: dict, name: str, number: int, problems: list) -> bool:
        if kind in ("ifdef", "ifndef"):
            if not re.fullmatch(r"[A-Za-z_]\w*", argument):
                problems.append(Problem("property-conditional", f"#{kind} needs one name, not {argument!r}", name, number))
                return False
            return (argument in env) == (kind == "ifdef")
        try:
            return bool(_Expression(argument, env).value())
        except ValueError as error:
            problems.append(Problem("property-conditional", f"cannot evaluate #{kind} {argument}: {error}", name, number))
            return False

    # -- the contract ----------------------------------------------------------------------------
    def contract(self, *, version: str = "", required=(), environments=({},)) -> list[Problem]:
        problems: list[Problem] = list(self.read_problems)
        if not self.stages:
            problems.append(Problem("no-stages", "no shader stages (.vsh, .fsh, .gsh, .csh, .tcs, .tes) in the shaders "
                                                 "root or its world folders"))
        problems += self._pairing()
        for name in required:
            program = self.programs.get(name)
            if program is None or not {"vert", "frag"} <= set(program.stages):
                problems.append(Problem("program-missing", f"the pack has no {name}.vsh and {name}.fsh"))
        problems += self._versions(version)
        for env in environments:
            problems += self._property_problems(env)
        return list(dict.fromkeys(problems))

    def _pairing(self) -> list[Problem]:
        problems = []
        for program in self.programs.values():
            kinds = set(program.stages)
            if kinds == {"comp"}:
                continue
            for kind, partner in (("vert", "frag"), ("frag", "vert"), ("tesc", "tese"), ("tese", "tesc")):
                if kind in kinds and partner not in kinds:
                    problems.append(Problem("stage-unpaired", f"{program.name} has a {NAMES[kind]} stage but no "
                                                              f"{NAMES[partner]} stage", program.stages[kind]))
            for kind in ("geom", "tesc", "tese"):
                if kind in kinds and not {"vert", "frag"} <= kinds:
                    problems.append(Problem("stage-unpaired", f"{program.name} has a {NAMES[kind]} stage but not both "
                                                              "a vertex and a fragment stage", program.stages[kind]))
        return problems

    def version_of(self, stage: str) -> tuple[str | None, tuple[str, int]]:
        """The ``#version`` a stage begins with (includes expanded), normalised, and where it stands."""
        flat = self.flatten(stage)
        in_comment = False
        for index, line in enumerate(flat.text.splitlines(), 1):
            text, in_comment = code_of(line, in_comment)
            text = text.strip()
            if not text:
                continue
            match = VERSION.match(text)
            if not match:
                return None, flat.where(index)
            number, profile = int(match[1]), (match[2] or "").lower()
            if not profile and number >= 150:
                profile = "core"
            return f"{number} {profile}".strip(), flat.where(index)
        return None, (stage, 0)

    def _versions(self, version: str) -> list[Problem]:
        problems = []
        found: dict[str, list[str]] = {}
        for stage in self.stages:
            declared, (file, line) = self.version_of(stage)
            if declared is None:
                problems.append(Problem("version-missing", "the stage does not begin with #version", stage))
                continue
            found.setdefault(declared, []).append(stage)
        if version and version != "any":
            wanted = self.normal_version(version)
            for declared, stages in found.items():
                if declared != wanted:
                    problems += [Problem("version-differs", f"declares #version {declared}, not {wanted}", s)
                                 for s in stages]
        elif not version and len(found) > 1:
            common = max(found, key=lambda v: len(found[v]))
            for declared, stages in sorted(found.items()):
                if declared != common:
                    shown = ", ".join(stages[:5]) + (f" and {len(stages) - 5} more" if len(stages) > 5 else "")
                    problems.append(Problem("version-inconsistent", f"#version {declared} in {shown}, while "
                                                                    f"{len(found[common])} stage(s) declare {common}"))
        return problems

    @staticmethod
    def normal_version(text: str) -> str:
        parts = str(text).split()
        if not parts or not parts[0].isdigit():
            raise ValueError(f"not a #version: {text!r} (e.g. '460 compatibility')")
        number = int(parts[0])
        profile = parts[1].lower() if len(parts) > 1 else ("core" if number >= 150 else "")
        return f"{number} {profile}".strip()

    def _property_problems(self, env: dict) -> list[Problem]:
        problems: list[Problem] = []
        for name in PROPERTIES:
            props, found = self.properties(name, env)
            problems += found
            prefix = ASSIGNMENTS.get(name)
            if prefix:
                problems += self._assignments(name, prefix, props)
            if name == "shaders.properties":
                problems += self._references(props)
        return problems

    @staticmethod
    def _assignments(name: str, prefix: str, props: dict) -> list[Problem]:
        problems, owner = [], {}
        for key, (value, line) in props.items():
            if not key.startswith(prefix):
                continue
            if not key[len(prefix):].isdigit():
                problems.append(Problem("assignment-id", f"{key}: the id after {prefix} is not a number", name, line))
            for entry in value.split():
                if entry in owner and owner[entry] != key:
                    problems.append(Problem("assignment-duplicate", f"{entry} is assigned to {owner[entry]} and {key}",
                                            name, line))
                owner.setdefault(entry, key)
        return problems

    def pack_profiles(self, props: dict) -> dict[str, str]:
        return {key.removeprefix("profile."): value for key, (value, _) in props.items() if key.startswith("profile.")}

    def _references(self, props: dict) -> list[Problem]:
        problems = []
        screens = {key.removeprefix("screen.") for key in props if key.startswith("screen.")
                   and not key.endswith(".columns")}
        profiles = self.pack_profiles(props)
        for key, (value, line) in props.items():
            if key in ("screen", "sliders") or key.startswith("screen.") and not key.endswith(".columns"):
                for entry in value.split():
                    if entry in ("<empty>", "<profile>", "*"):
                        continue
                    if entry.startswith("[") and entry.endswith("]"):
                        if key != "sliders" and entry[1:-1] not in screens:
                            problems.append(Problem("screen-undefined", f"{key} links to {entry}, but there is no "
                                                                        f"screen.{entry[1:-1]}", "shaders.properties",
                                                    line))
                        continue
                    if entry not in self.options:
                        problems.append(Problem("option-undefined", f"{key} names {entry}, which no source declares as "
                                                                    "an option", "shaders.properties", line))
            elif key.startswith("profile."):
                problems += [Problem("profile-invalid", message, "shaders.properties", line)
                             for message in self._profile_tokens(key, value, profiles)]
        for name in profiles:
            try:
                self.resolve_profile(profiles, name)
            except ValueError as error:
                if "cycle" in str(error):  # an undefined parent is named once, above
                    problems.append(Problem("profile-invalid", str(error), "shaders.properties",
                                            props[f"profile.{name}"][1]))
        return problems

    def _profile_tokens(self, key: str, value: str, profiles: dict) -> list[str]:
        wrong = []
        for entry in value.split():
            if entry.startswith("profile."):
                if entry.removeprefix("profile.") not in profiles:
                    wrong.append(f"{key} takes {entry}, which is not defined")
                continue
            if entry.lstrip("!").startswith("program."):
                continue
            name, separator, setting = entry.partition(":")
            if not separator:
                name, separator, setting = entry.partition("=")
            name = name.lstrip("!")
            option = self.options.get(name)
            if option is None:
                wrong.append(f"{key} sets {name}, which no source declares as an option")
            elif separator and option.kind == "value" and setting not in option.allowed:
                wrong.append(f"{key} sets {name} to {setting}, not one of {' '.join(option.allowed)}")
            elif separator and option.kind == "switch" and setting not in ("true", "false"):
                wrong.append(f"{key} sets the switch {name} to {setting}")
            elif not separator and option.kind == "value":
                wrong.append(f"{key} names the value option {name} without a value")
        return wrong

    def resolve_profile(self, profiles: dict[str, str], name: str, chain: tuple = ()) -> dict:
        """The option values a pack profile (``profile.NAME`` in shaders.properties) sets, inheritance followed."""
        if name in chain:
            raise ValueError("profile inheritance cycle: " + " -> ".join(chain + (name,)))
        if name not in profiles:
            raise ValueError(f"profile.{name} is not defined")
        values: dict = {}
        for entry in profiles[name].split():
            if entry.startswith("profile."):
                values.update(self.resolve_profile(profiles, entry.removeprefix("profile."), chain + (name,)))
                continue
            if entry.lstrip("!").startswith("program."):
                continue
            option, separator, setting = entry.partition(":")
            if not separator:
                option, separator, setting = entry.partition("=")
            off = option.startswith("!")
            option = option.lstrip("!")
            known = self.options.get(option)
            if known is None:
                continue
            if separator:
                values[option] = setting == "true" if known.kind == "switch" else setting
            elif known.kind == "switch":
                values[option] = not off
        return values

    def selected(self, program: str, only: list[str], exclude: list[str]) -> bool:
        base = program.rpartition("/")[2]
        if only and not any(fnmatch.fnmatchcase(program, p) or fnmatch.fnmatchcase(base, p) for p in only):
            return False
        return not any(fnmatch.fnmatchcase(program, p) or fnmatch.fnmatchcase(base, p) for p in exclude)


class _Expression:
    """A preprocessor ``#if`` expression over a dict of macros; an undefined name is 0."""

    TOKEN = re.compile(r"\s*(?:(0[xX][0-9a-fA-F]+|\d+\.\d*|\.\d+|\d+)[fFuU]?|([A-Za-z_]\w*)|(\|\||&&|==|!=|<=|>=|[-+*/%<>!()]))")

    def __init__(self, text: str, env: dict):
        self.env = env
        self.tokens: list[tuple[str, object]] = []
        position = 0
        text = text.strip()
        while position < len(text):
            match = self.TOKEN.match(text, position)
            if not match or match.end() == position:
                raise ValueError(f"unexpected {text[position:position + 10]!r}")
            if match[1]:
                self.tokens.append(("number", _number(match[1])))
            elif match[2]:
                self.tokens.append(("name", match[2]))
            else:
                self.tokens.append(("op", match[3]))
            position = match.end()
            while position < len(text) and text[position].isspace():
                position += 1
        self.index = 0

    def value(self):
        if not self.tokens:
            raise ValueError("an empty condition")
        result = self._binary(0)
        if self.index != len(self.tokens):
            raise ValueError(f"unexpected {self.tokens[self.index][1]!r}")
        return result

    LEVELS = [("||",), ("&&",), ("==", "!="), ("<", ">", "<=", ">="), ("+", "-"), ("*", "/", "%")]

    def _peek(self):
        return self.tokens[self.index] if self.index < len(self.tokens) else (None, None)

    def _binary(self, level: int):
        if level == len(self.LEVELS):
            return self._unary()
        left = self._binary(level + 1)
        while self._peek()[0] == "op" and self._peek()[1] in self.LEVELS[level]:
            operator = self._peek()[1]
            self.index += 1
            right = self._binary(level + 1)
            try:
                left = {"||": lambda a, b: int(bool(a) or bool(b)), "&&": lambda a, b: int(bool(a) and bool(b)),
                        "==": lambda a, b: int(a == b), "!=": lambda a, b: int(a != b), "<": lambda a, b: int(a < b),
                        ">": lambda a, b: int(a > b), "<=": lambda a, b: int(a <= b), ">=": lambda a, b: int(a >= b),
                        "+": lambda a, b: a + b, "-": lambda a, b: a - b, "*": lambda a, b: a * b,
                        "/": lambda a, b: a // b if isinstance(a, int) and isinstance(b, int) else a / b,
                        "%": lambda a, b: a % b}[operator](left, right)
            except (TypeError, ZeroDivisionError) as error:
                raise ValueError(str(error)) from None
        return left

    def _unary(self):
        kind, item = self._peek()
        if kind == "op" and item in ("!", "-", "+"):
            self.index += 1
            operand = self._unary()
            try:
                return int(not operand) if item == "!" else -operand if item == "-" else +operand
            except TypeError as error:
                raise ValueError(str(error)) from None
        return self._primary()

    def _primary(self):
        kind, item = self._peek()
        self.index += 1
        if kind == "number":
            return item
        if kind == "name" and item == "defined":
            parenthesised = self._peek() == ("op", "(")
            if parenthesised:
                self.index += 1
            name_kind, name = self._peek()
            if name_kind != "name":
                raise ValueError("defined needs a name")
            self.index += 1
            if parenthesised:
                if self._peek() != ("op", ")"):
                    raise ValueError("defined( without )")
                self.index += 1
            return int(name in self.env)
        if kind == "name":
            return self.env.get(item, 0)
        if (kind, item) == ("op", "("):
            inner = self._binary(0)
            if self._peek() != ("op", ")"):
                raise ValueError("( without )")
            self.index += 1
            return inner
        raise ValueError(f"unexpected {item!r}" if item is not None else "the condition ends early")
