r"""Bundle templates: what a rental bundle is made from, named in config, so Workbench names no project.

A template is a table ``[modules.vast.templates.<name>]`` (in a config folder, the file
``modules/vast/templates/<name>.toml`` fills it). It names a folder of files and the entry script a rental runs, what
the run needs and how it is watched, the input archives it takes (each with an optional pinned SHA-256 and a recipe
``vast.lab-inputs`` builds it by), how a plan's game steps are written into the bundle, and which fetched results
become step outputs. Every key, with a worked example, is in ``modules/vast/docs/templates.md``.

Steps files and outputs are written through fields: ``{<role>.<param>}`` is a game step's parameter (``id`` and
``command`` name the step itself), ``{values.<key>}`` a value the bundle was given (a plan gives its ``[rental]``
table), ``{name}`` the bundle's name and ``{n}`` a step's place among its role's steps, from 1. ``{a|b|c}`` takes the
first alternative that has a value; one that is not a reference is text (``{prepare.seed|0}``, ``{play.options|}``);
``{{`` and ``}}`` are braces. A field that reads a role, outside an entry written once per step, joins that role's
steps with spaces.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex
import stat
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from wb_core.manifest import ROLES
from wb_core.module import sha256_file

from .bundle import REMOTE

STEPS_SCHEMA = "wb.bundle-steps.v1"

TEMPLATE_KEYS = ("summary", "folder", "script", "exclude", "purpose", "needs", "run", "inputs", "steps", "outputs")
NEEDS_KEYS = ("jdk", "gpu", "disk_gb")
RUN_KEYS = ("args", "progress", "done", "failed", "results", "stop", "partial", "unpack")
RECIPE_KEYS = ("repo", "commit", "steps", "artifacts", "base", "exclude", "jdk", "env")
INPUT_KEYS = ("sha256", "to") + RECIPE_KEYS
STEPS_KEYS = ("file", "format", "roles", "same", "files", "values")
FILE_KEYS = ("role", "param", "match", "into", "write")
STEP_KEYS = ("id", "command", "role", "params")

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,99}")
SHA256 = re.compile(r"(?:sha256:)?([0-9a-fA-F]{64})")
COMMIT = re.compile(r"[0-9a-fA-F]{7,64}")
IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
REFERENCE = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z0-9_-]+)+")
VARIABLE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
FIELD = re.compile(r"\{\{|\}\}|\{([^{}]*)\}")
COUNT = re.compile(r"(\d+)(?:(\+)|-(\d+))?")
UNRESOLVED = re.compile(r"\{[A-Za-z_][\w.\-]*\}")  # a plan reference not resolved yet (a check leaves it)


class TemplateError(ValueError):
    """A template's config is wrong; the message names the key."""


class Missing(Exception):
    """A field none of whose alternatives has a value."""


# ---------------------------------------------------------------------------------------------
# fields

@dataclass(frozen=True)
class Alternative:
    kind: str        # role, values, name, n or text
    head: str = ""   # the role, or the dotted key under values
    param: str = ""  # a role's step parameter
    text: str = ""


@dataclass
class Text:
    """A string with fields, parsed once and checked against the roles the template takes."""
    source: str
    parts: list = field(default_factory=list)  # str, or a field's alternatives

    @property
    def roles(self) -> set[str]:
        return {a.head for part in self.parts if isinstance(part, list) for a in part if a.kind == "role"}


def parse_text(source, where: str, roles: set[str]) -> Text:
    if not isinstance(source, str):
        raise TemplateError(f"{where}: expected text, not {type(source).__name__}")
    parsed, position = Text(source), 0
    for match in FIELD.finditer(source):
        _literal(source[position:match.start()], where, source, parsed.parts)
        if match.group(0) in ("{{", "}}"):
            parsed.parts.append(match.group(0)[0])
        else:
            alternatives = [_alternative(a.strip(), where, roles) for a in match.group(1).split("|")]
            read = sorted({a.head for a in alternatives if a.kind == "role"})
            if len(read) > 1:
                raise TemplateError(f"{where}: {{{match.group(1)}}} reads {' and '.join(read)}; a field reads one role")
            parsed.parts.append(alternatives)
        position = match.end()
    _literal(source[position:], where, source, parsed.parts)
    return parsed


def _literal(chunk: str, where: str, source: str, parts: list):
    if "{" in chunk or "}" in chunk:
        raise TemplateError(f"{where}: unbalanced brace in {source!r} (a brace itself is {{{{ or }}}})")
    if chunk:
        parts.append(chunk)


def _alternative(text: str, where: str, roles: set[str]) -> Alternative:
    if text in ("name", "n"):
        return Alternative(text)
    if REFERENCE.fullmatch(text):
        head, _, rest = text.partition(".")
        if head == "values":
            return Alternative("values", head=rest)
        if head in roles:
            if "." in rest:
                raise TemplateError(f"{where}: {{{text}}}: a step's parameter is <role>.<param>")
            return Alternative("role", head=head, param=rest)
        known = ", ".join([*sorted(roles), "values"])
        raise TemplateError(f"{where}: {{{text}}} names neither a role the template takes nor values (known: "
                            f"{known}); text in a field has no dot")
    return Alternative("text", text=text)


@dataclass
class Scope:
    """What fields read: the bundle's name, its values, and the game steps by role."""
    name: str
    values: dict
    by_role: dict[str, list[dict]]


def render(text: Text, scope: Scope, step: dict | None = None, n: int | None = None) -> str:
    out = []
    for part in text.parts:
        if isinstance(part, str):
            out.append(part)
            continue
        role = next((a.head for a in part if a.kind == "role"), None)
        steps = scope.by_role.get(role, []) if role is not None and step is None else []
        if steps:
            out.append(" ".join(_first(part, scope, s, i) for i, s in enumerate(steps, start=1)))
        else:
            out.append(_first(part, scope, step, n))
    return "".join(out)


def _first(alternatives: list[Alternative], scope: Scope, step: dict | None, n: int | None) -> str:
    for alternative in alternatives:
        kind = alternative.kind
        if kind == "text":
            return alternative.text
        if kind == "name":
            return scope.name
        if kind == "n":
            if n is not None:
                return str(n)
            continue
        if kind == "values":
            value = _dig(scope.values, alternative.head)
        elif step is None or step["role"] != alternative.head:
            continue
        elif alternative.param in ("id", "command"):
            value = step[alternative.param]
        else:
            value = step["params"].get(alternative.param)
        if value is not None:
            return _string(value)
    wanted = " | ".join(_spelled(a) for a in alternatives)
    raise Missing(f"step {step['id']} gives no {wanted}" if step else f"nothing gives {wanted}")


def _spelled(alternative: Alternative) -> str:
    if alternative.kind == "role":
        return f"{alternative.head}.{alternative.param}"
    if alternative.kind == "values":
        return f"values.{alternative.head}"
    return alternative.kind if alternative.kind in ("name", "n") else repr(alternative.text)


def _dig(values, dotted: str):
    for part in dotted.split("."):
        values = values.get(part) if isinstance(values, dict) else None
    return values


def _string(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return " ".join(_string(v) for v in value if v is not None)
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True)
    return str(value)


# ---------------------------------------------------------------------------------------------
# the template

@dataclass
class Recipe:
    """How vast.lab-inputs builds an input: steps in a clean tree of ``repo`` at ``commit``, then the files the
    ``artifacts`` globs match (relative to ``base``, less ``exclude``) packed into a tar."""
    repo: str
    commit: str
    steps: list[str]
    artifacts: list[str]
    base: str = ""
    exclude: list[str] = field(default_factory=list)
    jdk: str = ""
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class Input:
    name: str             # its file name, in the lab folder and on the rental
    sha256: str | None    # pinned: other bytes are refused
    to: str               # the folder on the rental it is uploaded to
    recipe: Recipe | None


@dataclass
class FileRule:
    """A step parameter whose values name files: each value matching ``match`` (a file-name glob) is carried into
    the bundle under ``into`` and written as its path on the rental (``remote``) or in the bundle (``relative``)."""
    role: str
    param: str
    match: str
    into: str
    write: str


@dataclass
class Steps:
    file: str                                  # the steps file, in the bundle
    format: str                                # env or json
    roles: dict[str, tuple[int, int | None]]   # the game steps it takes: role -> (fewest, most)
    same: list[str]                            # parameters every game step gives alike
    files: list[FileRule]
    values: list[tuple[str, Text, bool]]       # env: (variable, value, once per step)


@dataclass
class Output:
    name: Text
    path: Text          # in the rental's out folder, once fetched (and unpacked)
    role: str | None    # written once per step of this role


@dataclass
class Template:
    name: str
    summary: str
    folder: str
    script: str
    exclude: list[str]
    purpose: str
    jdks: list[str]
    gpu: bool
    disk_gb: int
    run: dict
    unpack: list[str]
    inputs: dict[str, Input]
    steps: Steps | None
    outputs: list[Output]


def parse(name: str, raw) -> Template:
    where = f"modules.vast.templates.{name}"
    if not NAME.fullmatch(str(name)):
        raise TemplateError(f"{where}: a template's name is letters, digits and . _ + -")
    table = _table(raw, where)
    _known(table, TEMPLATE_KEYS, where)
    folder = table.get("folder")
    if not isinstance(folder, str) or not folder.strip():
        raise TemplateError(f"{where}.folder is not set (the folder of the template's files)")
    script = _relative(table.get("script", "run.sh"), f"{where}.script")
    exclude = [_glob_text(g, f"{where}.exclude") for g in _list(table.get("exclude", []), f"{where}.exclude")]
    needs = _table(table.get("needs", {}), f"{where}.needs")
    _known(needs, NEEDS_KEYS, f"{where}.needs")
    run = _table(table.get("run", {}), f"{where}.run")
    _known(run, RUN_KEYS, f"{where}.run")
    results = [_text_value(r, f"{where}.run.results") for r in
               _list(run.get("results", [f"{REMOTE}/results.tar", "/root/run.log"]), f"{where}.run.results")]
    fetched = {PurePosixPath(r).name for r in results}
    unpack = []
    for item in _list(run.get("unpack", []), f"{where}.run.unpack"):
        if item not in fetched:
            raise TemplateError(f"{where}.run.unpack: {item!r} is not the name of a fetched result "
                                f"({', '.join(sorted(fetched)) or 'none'})")
        unpack.append(item)
    watched = {"args": [str(a) for a in _list(run.get("args", [REMOTE]), f"{where}.run.args")],
               "progress": _text_value(run.get("progress", "^RUN "), f"{where}.run.progress"),
               "done": _text_value(run.get("done", "RUN done"), f"{where}.run.done"),
               "failed": _optional_text(run.get("failed"), f"{where}.run.failed"), "results": results,
               "stop": _optional_text(run.get("stop"), f"{where}.run.stop"),
               "partial": _optional_text(run.get("partial"), f"{where}.run.partial")}
    for key in ("progress", "failed"):
        if watched[key] is not None:
            try:
                re.compile(watched[key])
            except re.error as error:
                raise TemplateError(f"{where}.run.{key}: not a regular expression ({error})") from None
    inputs = {str(n): _input(str(n), v, f"{where}.inputs") for n, v in
              _table(table.get("inputs", {}), f"{where}.inputs").items()}
    steps = _steps(table["steps"], f"{where}.steps") if table.get("steps") else None
    roles = set(steps.roles) if steps else set()
    outputs = []
    for key, value in _table(table.get("outputs", {}), f"{where}.outputs").items():
        named = parse_text(str(key), f"{where}.outputs: {key!r}", roles)
        path = parse_text(value, f"{where}.outputs.{key}", roles)
        read = named.roles | path.roles
        if len(read) > 1:
            raise TemplateError(f"{where}.outputs.{key}: an output is written once per step of one role, and this one "
                                f"reads {' and '.join(sorted(read))}")
        outputs.append(Output(named, path, next(iter(read), None)))
    return Template(name=str(name), summary=_text_value(table.get("summary", ""), f"{where}.summary"),
                    folder=folder.strip(), script=script, exclude=exclude,
                    purpose=_text_value(table.get("purpose", "measurement"), f"{where}.purpose"),
                    jdks=[str(j) for j in _list(needs.get("jdk", []), f"{where}.needs.jdk")],
                    gpu=_bool(needs.get("gpu", False), f"{where}.needs.gpu"),
                    disk_gb=_int(needs.get("disk_gb", 32), f"{where}.needs.disk_gb", 16, 500),
                    run=watched, unpack=unpack, inputs=inputs, steps=steps, outputs=outputs)


def _input(name: str, raw, where: str) -> Input:
    where = f'{where}."{name}"'
    if not NAME.fullmatch(name):
        raise TemplateError(f"{where}: an input is named by its file name (letters, digits and . _ + -)")
    table = _table(raw, where)
    _known(table, INPUT_KEYS, where)
    pinned = None
    if table.get("sha256") is not None:
        match = SHA256.fullmatch(str(table["sha256"]).strip())
        if not match:
            raise TemplateError(f"{where}.sha256: not a SHA-256 (64 hex digits)")
        pinned = match.group(1).lower()
    to = str(table.get("to", REMOTE + "/"))
    if not to.startswith("/") or ".." in to.split("/"):
        raise TemplateError(f"{where}.to: a folder on the rental, absolute (default {REMOTE}/)")
    recipe = None
    if any(key in table for key in RECIPE_KEYS):
        missing = [key for key in ("repo", "commit", "artifacts") if not table.get(key)]
        if missing:
            raise TemplateError(f"{where}: a recipe needs repo, commit and artifacts (missing {', '.join(missing)})")
        if not COMMIT.fullmatch(str(table["commit"])):
            raise TemplateError(f"{where}.commit: a commit, 7 to 64 hex digits (pin it, a branch moves)")
        if not name.endswith(".tar"):
            raise TemplateError(f"{where}: a recipe makes a tar archive, so the input's name ends in .tar")
        steps = _list(table.get("steps", []), f"{where}.steps")
        if not all(isinstance(s, str) and s.strip() for s in steps):
            raise TemplateError(f"{where}.steps: commands, as text")
        recipe = Recipe(repo=_text_value(table["repo"], f"{where}.repo"), commit=str(table["commit"]).lower(),
                        steps=list(steps),
                        artifacts=[_glob_text(g, f"{where}.artifacts")
                                   for g in _list(table["artifacts"], f"{where}.artifacts")],
                        base=_relative(table.get("base", ""), f"{where}.base", empty=True),
                        exclude=[_glob_text(g, f"{where}.exclude") for g in
                                 _list(table.get("exclude", []), f"{where}.exclude")],
                        jdk=str(table.get("jdk") or ""),
                        env={str(k): str(v) for k, v in _table(table.get("env", {}), f"{where}.env").items()})
    return Input(name=name, sha256=pinned, to=to if to.endswith("/") else to + "/", recipe=recipe)


def _steps(raw, where: str) -> Steps:
    table = _table(raw, where)
    _known(table, STEPS_KEYS, where)
    if not table.get("file"):
        raise TemplateError(f"{where}.file is not set (the file in the bundle the steps are written to)")
    file = _relative(table["file"], f"{where}.file")
    form = table.get("format") or ("json" if file.lower().endswith(".json") else "env")
    if form not in ("env", "json"):
        raise TemplateError(f"{where}.format: env (shell variables) or json")
    roles = {}
    for role, count in _table(table.get("roles", {}), f"{where}.roles").items():
        if role not in ROLES:
            raise TemplateError(f"{where}.roles: {role} is not a role (commands declare {', '.join(sorted(ROLES))})")
        roles[role] = _count(count, f"{where}.roles.{role}")
    if not roles:
        raise TemplateError(f'{where}.roles: name the game steps the template takes, e.g. play = "1+"')
    same = [str(p) for p in _list(table.get("same", []), f"{where}.same")]
    for param in same:
        if not IDENT.fullmatch(param):
            raise TemplateError(f"{where}.same: {param!r} is not a parameter name")
    files = [_file_rule(rule, roles, f"{where}.files[{index}]")
             for index, rule in enumerate(_list(table.get("files", []), f"{where}.files"))]
    values = []
    for key, value in _table(table.get("values", {}), f"{where}.values").items():
        if form == "json":
            raise TemplateError(f"{where}.values: a json steps file holds the steps themselves; values are for env")
        per_step = "{n}" in key
        if not VARIABLE.fullmatch(key.replace("{n}", "1")):
            raise TemplateError(f"{where}.values: {key!r} is not a variable name (letters, digits and _, with {{n}} "
                                "for one per step)")
        parsed = parse_text(value, f"{where}.values.{key}", set(roles))
        if per_step and len(parsed.roles) != 1:
            raise TemplateError(f"{where}.values.{key}: a variable with {{n}} is written once per step of one role, so "
                                "its value reads exactly one role")
        values.append((key, parsed, per_step))
    if form == "env" and not values:
        raise TemplateError(f"{where}.values: an env steps file needs its variables")
    return Steps(file=file, format=form, roles=roles, same=same, files=files, values=values)


def _file_rule(raw, roles: dict, where: str) -> FileRule:
    table = _table(raw, where)
    _known(table, FILE_KEYS, where)
    if table.get("role") not in roles:
        raise TemplateError(f"{where}.role: one of the roles the template takes ({', '.join(roles)})")
    param = str(table.get("param") or "")
    if not IDENT.fullmatch(param):
        raise TemplateError(f"{where}.param: the step parameter that names files")
    match = str(table.get("match", "*"))
    if not match or "/" in match or "\\" in match:
        raise TemplateError(f"{where}.match: a glob on the file name (default *)")
    write = table.get("write", "relative")
    if write not in ("relative", "remote"):
        raise TemplateError(f"{where}.write: relative (its path in the bundle) or remote (its path on the rental)")
    return FileRule(role=table["role"], param=param, match=match,
                    into=_relative(table.get("into", ""), f"{where}.into", empty=True), write=write)


def _count(value, where: str) -> tuple[int, int | None]:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value, value
    if isinstance(value, str):
        match = COUNT.fullmatch(value.strip())
        if match:
            low = int(match.group(1))
            if match.group(2):
                return low, None
            high = int(match.group(3)) if match.group(3) else low
            if high >= low:
                return low, high
    raise TemplateError(f'{where}: a count of steps: 1, "1+" or "1-4"')


def count_text(low: int, high: int | None) -> str:
    if high is None:
        return f"at least {low}"
    if low == high:
        return f"exactly {low}"
    return f"at most {high}" if low == 0 else f"{low} to {high}"


def _known(table: dict, keys: tuple, where: str):
    unknown = sorted(str(k) for k in set(table) - set(keys))
    if unknown:
        raise TemplateError(f"{where}: unknown key(s) {', '.join(unknown)} (known: {', '.join(keys)})")


def _table(value, where: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise TemplateError(f"{where} must be a table")
    return value


def _list(value, where: str) -> list:
    if not isinstance(value, list):
        raise TemplateError(f"{where} must be a list")
    return value


def _bool(value, where: str) -> bool:
    if not isinstance(value, bool):
        raise TemplateError(f"{where}: true or false")
    return value


def _int(value, where: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise TemplateError(f"{where}: a whole number from {low} to {high}")
    return value


def _text_value(value, where: str) -> str:
    if not isinstance(value, str):
        raise TemplateError(f"{where}: expected text")
    return value


def _optional_text(value, where: str) -> str | None:
    return None if value is None else _text_value(value, where)


def _relative(value, where: str, *, empty: bool = False) -> str:
    """A path relative to a folder, in POSIX form, that stays inside it."""
    text = _text_value(value if value is not None else "", where).replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    text = text.rstrip("/")
    if not text:
        if empty:
            return ""
        raise TemplateError(f"{where}: a path relative to its folder")
    if text.startswith("/") or ":" in text or ".." in text.split("/"):
        raise TemplateError(f"{where}: {value!r} must be a path relative to its folder that stays inside it")
    return text


def _glob_text(value, where: str) -> str:
    text = _text_value(value, where)
    glob_regex(text, where)
    return text


# ---------------------------------------------------------------------------------------------
# globs and folders

def glob_regex(pattern: str, where: str) -> re.Pattern:
    """A glob over POSIX paths: ``**`` crosses folders, ``*`` and ``?`` stay within one (as build.archive's)."""
    text = pattern.replace("\\", "/").strip()
    text = text[2:] if text.startswith("./") else text
    text = text.rstrip("/")
    if not text or text.startswith("/") or ":" in text or ".." in text.split("/"):
        raise TemplateError(f"{where}: {pattern!r} must be a glob relative to its folder")
    regex, i = "", 0
    while i < len(text):
        if text.startswith("**/", i):
            regex, i = regex + "(?:.*/)?", i + 3
        elif text.startswith("**", i):
            regex, i = regex + ".*", i + 2
        elif text[i] == "*":
            regex, i = regex + "[^/]*", i + 1
        elif text[i] == "?":
            regex, i = regex + "[^/]", i + 1
        else:
            regex, i = regex + re.escape(text[i]), i + 1
    return re.compile(regex)


def matches(path: str, patterns: list[re.Pattern]) -> bool:
    """Whether a glob matches the path or a folder above it (``exclude = ["build"]`` leaves out all of build/)."""
    parts = path.split("/")
    return any(p.fullmatch("/".join(parts[:n])) for p in patterns for n in range(1, len(parts) + 1))


def _linked(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)  # a reparse point


def collect(root: Path, include: list[re.Pattern] | None, exclude: list[re.Pattern]) -> tuple[list[str], list[str]]:
    """The regular files under ``root`` (sorted POSIX paths relative to it) that ``include`` matches (every file when
    it is None) and ``exclude`` does not, and the links and special files that would have been taken."""
    files, refused = [], []
    for folder, dirs, names in os.walk(root):
        here = Path(folder)
        prefix = here.relative_to(root).as_posix()
        prefix = "" if prefix == "." else prefix + "/"
        walk = []
        for name in sorted(dirs):
            relative = prefix + name
            if name == ".git" or matches(relative, exclude):
                continue
            if _linked(here / name):
                if include is None or matches(relative, include):
                    refused.append(relative)
            else:
                walk.append(name)
        dirs[:] = walk
        for name in sorted(names):
            relative = prefix + name
            if name == ".git" or (include is not None and not matches(relative, include)) or matches(relative, exclude):
                continue
            path = here / name
            if _linked(path) or not stat.S_ISREG(path.lstat().st_mode):
                refused.append(relative)
            else:
                files.append(relative)
    return sorted(files), sorted(refused)


# ---------------------------------------------------------------------------------------------
# a plan's game steps, written as the template declares

@dataclass
class Compiled:
    text: str                                              # the steps file ("" when the template takes none)
    copies: dict[str, Path] = field(default_factory=dict)  # path in the bundle -> the local file carried there
    steps: list[dict] = field(default_factory=list)        # the steps as written (file values rewritten)
    outputs: list[dict] = field(default_factory=list)      # {step, name, path}
    problems: list[str] = field(default_factory=list)


def compile_steps(template: Template, steps: list, values: dict, name: str, local, *,
                  check_files: bool = True) -> Compiled:
    """The steps file for ``steps`` (each ``{id, command, role, params}``, references resolved), the files their
    parameters name (found through ``local``, which maps a value to a readable path) and the outputs the results
    become, with every reason the template cannot take them. ``check_files`` off (a check before a plan has run)
    leaves values that are still plan references for the make to check."""
    out = Compiled("")
    spec = template.steps
    if spec is None:
        if steps:
            out.problems.append(f"template {template.name} takes no game steps (it declares no steps table), and "
                                f"{len(steps)} were given")
        out.outputs = expand_outputs(template, Scope(name, values, {}), out.problems)
        return out
    taken, seen = [], set()
    for index, raw in enumerate(steps, start=1):
        if not isinstance(raw, dict):
            out.problems.append(f"step {index} is not a table of id, command, role and params")
            continue
        sid = str(raw.get("id") or f"step-{index}")
        unknown = sorted(set(raw) - set(STEP_KEYS))
        params = raw.get("params") or {}
        if unknown or not isinstance(params, dict):
            out.problems.append(f"step {sid}: a game step is id, command, role and params (a table)")
            continue
        if sid in seen:
            out.problems.append(f"step id {sid} is given twice")
        seen.add(sid)
        role = raw.get("role")
        if role not in spec.roles:
            out.problems.append(f"step {sid}: template {template.name} takes no {role or 'role-less'} step (it takes "
                                f"{', '.join(spec.roles)})")
            continue
        taken.append({"id": sid, "command": str(raw.get("command") or ""), "role": role,
                      "params": json.loads(json.dumps(params))})
    for role, (low, high) in spec.roles.items():
        count = sum(1 for s in taken if s["role"] == role)
        if count < low or (high is not None and count > high):
            out.problems.append(f"template {template.name} takes {count_text(low, high)} {role} step(s), and "
                                f"{count} were given")
    for param in spec.same:
        given = {s["id"]: s["params"].get(param) for s in taken}
        absent = [sid for sid, value in given.items() if value is None]
        if absent:
            out.problems.append(f"step {absent[0]} gives no {param}, and template {template.name} needs every game "
                                f"step to give the same {param}")
        elif len({json.dumps(v, sort_keys=True) for v in given.values()}) > 1:
            out.problems.append(f"template {template.name} needs every game step to give the same {param}, and they "
                                f"give {', '.join(sorted({_string(v) for v in given.values()}))}")
    _carry(template, taken, local, check_files, out)
    out.steps = taken
    scope = Scope(name, values, {role: [s for s in taken if s["role"] == role] for role in spec.roles})
    if out.problems:
        return out
    if spec.format == "json":
        positions: dict[str, int] = {}
        listed = []
        for step in taken:
            positions[step["role"]] = positions.get(step["role"], 0) + 1
            listed.append({**step, "n": positions[step["role"]]})
        out.text = json.dumps({"schema": STEPS_SCHEMA, "bundle": name, "template": template.name, "values": values,
                               "steps": listed}, indent=1, ensure_ascii=False) + "\n"
    else:
        lines = [f"# written by Workbench for bundle {_one_line(name)} from template {template.name}"]
        for key, value, per_step in spec.values:
            try:
                if per_step:
                    role = next(iter(value.roles))
                    for index, step in enumerate(scope.by_role.get(role, []), start=1):
                        written = render(value, scope, step, index)
                        lines.append(f"{key.replace('{n}', str(index))}={shlex.quote(written)}")
                else:
                    lines.append(f"{key}={shlex.quote(render(value, scope))}")
            except Missing as error:
                out.problems.append(f"{spec.file}: {key}: {error}")
        out.text = "\n".join(lines) + "\n"
    out.outputs = expand_outputs(template, scope, out.problems)
    return out


def _carry(template: Template, taken: list[dict], local, check_files: bool, out: Compiled):
    """Carries the files the steps' parameters name into the bundle, rewriting each value to its new path."""
    origin: dict[str, str] = {}
    for step in taken:
        for rule in template.steps.files:
            if rule.role != step["role"] or rule.param not in step["params"]:
                continue

            def carry(value, step=step, rule=rule):
                if not isinstance(value, str) or not fnmatch.fnmatchcase(re.split(r"[\\/]", value)[-1].lower(),
                                                                         rule.match.lower()):
                    return value
                if not check_files and UNRESOLVED.search(value):
                    return value
                source = Path(local(value))
                if not source.is_file():
                    out.problems.append(f"step {step['id']}: {rule.param} names {value}, which is not a file")
                    return value
                relative = f"{rule.into}/{source.name}" if rule.into else source.name
                held = out.copies.get(relative)
                if held is not None and held != source and sha256_file(held) != sha256_file(source):
                    out.problems.append(f"step {step['id']}: {value} would go to {relative}, where {origin[relative]} "
                                        "already put another file of that name")
                    return value
                out.copies.setdefault(relative, source)
                origin.setdefault(relative, f"step {step['id']}")
                return f"{REMOTE}/{relative}" if rule.write == "remote" else relative
            value = step["params"][rule.param]
            step["params"][rule.param] = [carry(v) for v in value] if isinstance(value, list) else carry(value)


def expand_outputs(template: Template, scope: Scope, problems: list[str]) -> list[dict]:
    """The outputs the fetched results become: each of the template's, once per step of the role it reads."""
    listed = []
    for output in template.outputs:
        steps = list(enumerate(scope.by_role.get(output.role, []), start=1)) if output.role else [(None, None)]
        for index, step in steps:
            try:
                name, path = render(output.name, scope, step, index), render(output.path, scope, step, index)
            except Missing as error:
                problems.append(f"outputs: {output.name.source}: {error}")
                continue
            parts = PurePosixPath(path.replace("\\", "/")).parts
            if not name or not parts or path.startswith(("/", "\\")) or ".." in parts or ":" in path:
                problems.append(f"outputs: {output.name.source} writes {name!r} at {path!r}, which is no path in the "
                                "rental's results")
                continue
            listed.append({"step": step["id"] if step else None, "name": name, "path": "/".join(parts)})
    names = [o["name"] for o in listed]
    for duplicate in sorted({n for n in names if names.count(n) > 1}):
        problems.append(f"outputs: {duplicate} is written twice")
    return listed


def _one_line(text: str) -> str:
    return " ".join(str(text).split())
