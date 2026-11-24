"""lab.resolve: a launch resolved once into launch.json, the one file every later step reads.

A launchable project's project.toml, read at the commit in use, defines its launches:

    [launch.default]
    members = ["riven:mod@both", "cubature:mod@both", "quadrature:mod@client", "modularui@both"]
    runtime = "cleanroom"
    params = { seed = 8675309 }
    jvm = { client = ["-Dexample=1"] }
    place = { "helper@both" = "+Helper-1.0.jar" }

``<project>:<artifact>@<side>`` names a project's artifact: the product's own from the Store by the tree being
launched, another project's from its line in the set, or from a hold of that name, which wins as it does for builds.
``place`` names the file a member is placed under in an instance, where its name matters (a coremod that must sort
first); otherwise it keeps the Store's name.
``<third>[:<kind>]@<side>`` names a third entry's file (kind ``runtime`` unless given). A member's own project.toml may
add ``[launch.member]`` (``jvm.client``, ``jvm.server``, ``collect``, ``failures``), read at the commit its line names,
so a member's switch needs no landing in the product.

A launch also names:

- ``base = "<project>:<name>"``: what every instance starts from, the ``[launch.<name>]`` of that project's
  project.toml, read at the commit the set's line names for it (a hold ``<project>@<product>`` wins; the product's own
  commit when the product is the base's project), with the ``launch/<overlay>`` folder at that commit. A base is a
  table without members (``BASE_KEYS``): the installer, the server's and client's command lines, the player, the JVM
  profile, the view keys a session sets, and an overlay folder whose ``both/``, ``server/`` and ``client/`` files are
  laid into the instance's sides. A base project whose commit has no such table yet falls back to Workbench's interim
  copy (``wb_lab.bases``), with a warning. A base project with no line in the set follows its branch in its home
  repository (origin's, else the local branch, else HEAD), as a check's tools do, and launch.json says so;
- ``base = "<project>:<launch>"`` naming another product's launch (a table with members): the launch inherits that
  launch, resolved at the base project's commit as that product would resolve it (its own artifact from the Store by
  the tree of that commit, so ``wb check`` in the base project at that commit makes it), with its members, its base
  (recursively, at most ``MAX_DEPTH`` launches deep, a cycle refused), configure script, overlays, typed params, JVM
  arguments, collect and failure patterns and tour rules; then adds its own members. Its own params replace the
  inherited ones of the same name (a bare value keeps the inherited type and limits), its own JVM arguments replace an
  inherited one with the same key (``-Dname``, ``-XX:name``, ``-Xmx``), its tour rules replace the inherited ones key
  by key, and its configure script, when it names one, replaces the inherited one. Drivers are not inherited: they
  run from the launching product's tree. One build per mod id holds across the union, and a member the base already
  names is refused. launch.json ``inherits`` lists the launches passed through, nearest first, each with its project,
  launch, commit, tree and how the commit was chosen (``line``, ``hold``, ``branch`` with its ref, or ``product``);
- a member of a project with no line in the set follows that project's branch the same way, its file from the Store
  by the tree of that commit;
- ``overlay``: the product's own folder under ``launch/``, laid over the base's;
- ``configure = "launch/configure.py"``: the product's script, run by lab.build only for a project in ``[lab] trusted``;
- ``params``: each a default value, whose type it fixes, or a table ``{ type, default, min, max, choices }``; lab.build
  refuses a value of another type or one the launch does not declare;
- ``tour`` (the rules lab.run drives a tour by) and ``drivers`` (``{ command = [...], cwd = "..." }`` per name);
- ``runtime``: ``"cleanroom"`` (the platform's Cleanroom), or a table naming another version of it for this launch:
  ``{ kind = "cleanroom", version = "0.7.7-alpha", installer = { url, file, sha256 }, universal = {...},
  prism = {...} }`` with that version's files by SHA-256 (lab.build fetches each from an allowed host), or
  ``{ kind = "cleanroom", version = "...", third = "<entry>" }`` taking them from a third entry of the set (its
  ``files`` by kind ``installer``, ``universal`` and ``prism``, kept in the Store). A launch that inherits another
  runs on that launch's runtime and may only name the same one.

launch.json records the launch definition's commit with the tree id of its ``launch/`` folder, and the base's
project, commit, table and overlay tree id, so lab.build exports exactly those trees and resolves nothing.

launch.json names every member by SHA-256 with its Store path and the mod ids its mcmod.info declares; a launch that
would load two builds of one mod id is refused before anything is written, as is a member the set or the Store lacks.
Its ``runtime`` names the kind, version and exact JDK build, and the runtime's own files by URL and SHA-256 in
``runtime.files``: the launch's own, a third entry's, or, for the platform's version, the platform's
(``platform.cleanroom.{installer,universal,prism}``) once it names them; ``runtime.source`` says which.
The code names no project."""

from __future__ import annotations

import fnmatch
import json
import re
import subprocess
import tomllib
import zipfile
from pathlib import Path

SCHEMA = 1
SIDES = ("both", "client", "server")
MEMBER = re.compile(r"^([a-z0-9][a-z0-9._-]*)(?::([a-z0-9][a-z0-9._-]*))?@(both|client|server)$")
LAUNCH_KEYS = {"members", "runtime", "params", "jvm", "collect", "failures", "place", "tour", "drivers", "base",
               "overlay", "configure"}
PLACE = re.compile(r'^[^/\\:*?"<>|]+\.jar$')  # a bare file name: no folder, nothing Windows refuses
MEMBER_KEYS = {"jvm", "collect", "failures"}
RUNTIMES = ("cleanroom",)
RUNTIME_FILES = ("installer", "universal", "prism")  # platform.cleanroom's files, copied into runtime.files
RUNTIME_KEYS = {"kind", "version", "third", *RUNTIME_FILES}
RUNTIME_VERSION = re.compile(r"^[0-9][A-Za-z0-9._+-]*$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
# A base: what lab.build starts every instance of a runtime from (wb_lab.instance names each key's use).
BASE_KEYS = {"summary", "runtime", "player", "installer", "memory", "jvm_profile", "jvm_args_file", "overlay", "files",
             "views", "eula", "ready", "server", "client"}
BASE_REQUIRED = ("installer", "server", "client")
JVM_PROFILE_KEYS = {"name", "collector", "initialHeapMiB", "maxHeapMiB", "arguments"}
BASE_NAME = re.compile(r"^([a-z0-9][a-z0-9._-]*):([A-Za-z0-9._-]+)$")
LAUNCH_FOLDER = "launch"
FOLDER = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]*(/[A-Za-z0-9_-][A-Za-z0-9._-]*)*$")  # relative, no '..'
CONFIGURE = re.compile(r"^launch/[A-Za-z0-9_-][A-Za-z0-9._-]*(/[A-Za-z0-9_-][A-Za-z0-9._-]*)*\.py$")
VIEW_KEY = re.compile(r"^(server|client)/[^:]+:.+$")  # <side>/<file>:<key>
# The tour rules lab.run reads (wb_lab.run.TOUR_DEFAULTS), each with its type.
TOUR_KEYS = {"setup": list, "spawn": str, "joined": str, "spectator": bool, "client_jvm": list, "captured": str,
             "capture_timeout": (int, float), "step": (int, float), "pause": (int, float), "leave": list,
             "leave_seconds": (int, float), "screenshots": str}
DRIVER_KEYS = {"command", "cwd", "summary"}
PARAM = re.compile(r"^[a-z][a-z0-9_]*$")
PARAM_KEYS = {"type", "default", "min", "max", "choices", "summary"}
PARAM_TYPES = {"int": int, "float": float, "bool": bool, "string": str}
MAX_DEPTH = 4  # the most launches one launch may pass through to its base (itself included)
JVM_KEY = re.compile(r"^(-D[^=]+|-XX:[+-]?[A-Za-z0-9_]+|-Xm[sx]|-Xss)")  # what an inheriting launch's argument replaces


class LaunchError(Exception):
    """A launch that cannot be resolved; the message names what is missing and, where there is one, the fix."""


class MissingBuild(LaunchError):
    """A member the Store lacks, which ``missing`` (project, commit) builds: wb play submits that check first."""

    def __init__(self, message: str, project: str, commit: str):
        super().__init__(message)
        self.missing = (project, commit)


def parse_member(text: str) -> tuple[str, str | None, str]:
    """(name, artifact or kind, side) of a member spec."""
    match = MEMBER.match(str(text))
    if not match:
        raise LaunchError(f"member {text!r} is not <project>:<artifact>@<side> or <third>[:<kind>]@<side> "
                          f"(sides: {', '.join(SIDES)})")
    return match.group(1), match.group(2), match.group(3)


def mod_ids(path: Path) -> list[str]:
    """The mod ids a JAR's mcmod.info declares (a list, or {modList: [...]}); none when it has no such file."""
    try:
        with zipfile.ZipFile(path) as archive:
            if "mcmod.info" not in archive.namelist():
                return []
            data = json.loads(archive.read("mcmod.info").decode("utf-8", errors="replace"))
    except (OSError, zipfile.BadZipFile, ValueError):
        return []
    entries = data.get("modList", []) if isinstance(data, dict) else data
    return sorted({str(e["modid"]) for e in entries if isinstance(e, dict) and e.get("modid")})


def _jvm(table, where: str) -> dict[str, list[str]]:
    table = table or {}
    if not isinstance(table, dict) or set(table) - {"client", "server", "both"}:
        raise LaunchError(f"{where}.jvm is a table of client, server and both, each a list of JVM arguments")
    out = {}
    for side, values in table.items():
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            raise LaunchError(f"{where}.jvm.{side} is a list of JVM arguments")
        out[side] = list(values)
    return out


def _add_jvm(target: dict[str, list[str]], jvm: dict[str, list[str]]):
    for side, values in jvm.items():
        for name in (("client", "server") if side == "both" else (side,)):
            target[name].extend(v for v in values if v not in target[name])


def _jvm_key(argument: str) -> str | None:
    """What a JVM argument sets (``-Dname``, ``-XX:name`` with or without its +/-, ``-Xmx``), or None."""
    match = JVM_KEY.match(argument)
    return match.group(1).replace("-XX:+", "-XX:").replace("-XX:-", "-XX:") if match else None


def _override_jvm(target: dict[str, list[str]], jvm: dict[str, list[str]]):
    """An inheriting launch's own arguments: each replaces an inherited one with the same key, then joins."""
    for side, values in jvm.items():
        for name in (("client", "server") if side == "both" else (side,)):
            keys = {_jvm_key(v) for v in values} - {None}
            target[name] = [v for v in target[name] if _jvm_key(v) not in keys]
            target[name].extend(v for v in values if v not in target[name])


def _strings(value) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def param_value(name: str, value, spec: dict, where: str):
    """``value`` checked against a param's declared type (an int is a float too); the value as the type takes it."""
    kind = spec.get("type")
    if kind == "float" and isinstance(value, int) and not isinstance(value, bool):
        value = float(value)
    expected = PARAM_TYPES.get(kind)
    if expected is None or not isinstance(value, expected) or (kind in ("int", "float") and isinstance(value, bool)):
        raise LaunchError(f"{where}: param {name} is {kind}, not {value!r}")
    if "min" in spec and value < spec["min"] or "max" in spec and value > spec["max"]:
        raise LaunchError(f"{where}: param {name} is {spec.get('min', '')}..{spec.get('max', '')}, not {value!r}")
    if "choices" in spec and value not in spec["choices"]:
        raise LaunchError(f"{where}: param {name} is one of {', '.join(map(str, spec['choices']))}, not {value!r}")
    return value


def params_of(table: dict, where: str) -> tuple[dict, dict]:
    """(defaults, types) of a launch's params: a bare value is its default and fixes its type; a table declares
    ``type`` with an optional ``default``, ``min``, ``max`` (numbers), ``choices`` and ``summary``."""
    params = table.get("params") or {}
    if not isinstance(params, dict):
        raise LaunchError(f"{where}.params is a table of name = value or name = {{ type = ... }}")
    names = {bool: "bool", int: "int", float: "float", str: "string"}
    defaults, types = {}, {}
    for name, value in params.items():
        if not PARAM.match(name):
            raise LaunchError(f"{where}.params: {name!r} is not a lowercase param name")
        if isinstance(value, dict):
            for key in sorted(set(value) - PARAM_KEYS):
                raise LaunchError(f"{where}.params.{name}: unknown key {key}")
            if value.get("type") not in PARAM_TYPES:
                raise LaunchError(f"{where}.params.{name}.type is one of {', '.join(PARAM_TYPES)}")
            spec = {k: value[k] for k in ("type", "min", "max", "choices", "summary") if k in value}
            if ("min" in spec or "max" in spec) and spec["type"] not in ("int", "float"):
                raise LaunchError(f"{where}.params.{name}: min and max are for an int or a float")
            if "choices" in spec and (not isinstance(spec["choices"], list) or not spec["choices"]):
                raise LaunchError(f"{where}.params.{name}.choices lists the values it may take")
            for choice in spec.get("choices") or []:
                param_value(name, choice, {"type": spec["type"]}, where)
            if "default" in value:
                defaults[name] = param_value(name, value["default"], spec, where)
        else:
            kind = names.get(type(value))
            if kind is None:
                raise LaunchError(f"{where}.params.{name}: a default is a bool, int, float or string, not {value!r}")
            spec, defaults[name] = {"type": kind}, value
        types[name] = spec
    return defaults, types


def _check_tour(tour, where: str):
    if not isinstance(tour, dict):
        raise LaunchError(f"{where}.tour is a table of the tour rules ({', '.join(sorted(TOUR_KEYS))})")
    for key, value in tour.items():
        expected = TOUR_KEYS.get(key)
        if expected is None:
            raise LaunchError(f"{where}: unknown key tour.{key} (known: {', '.join(sorted(TOUR_KEYS))})")
        if expected is list and not _strings(value) or expected is not list and (
                not isinstance(value, expected) or expected is not bool and isinstance(value, bool)):
            raise LaunchError(f"{where}: tour.{key} is " + ("a list of strings" if expected is list else
                                                             getattr(expected, "__name__", "a number")))


def _check_drivers(drivers, where: str):
    if not isinstance(drivers, dict):
        raise LaunchError(f"{where}.drivers is a table of name = {{ command = [...] }}")
    for name, driver in drivers.items():
        if not isinstance(driver, dict):
            raise LaunchError(f"{where}.drivers.{name} is a table with a command")
        for key in sorted(set(driver) - DRIVER_KEYS):
            raise LaunchError(f"{where}: unknown key drivers.{name}.{key}")
        if not _strings(driver.get("command")) or not driver["command"]:
            raise LaunchError(f"{where}.drivers.{name}.command is the driver's command line, a list of strings")
        if not isinstance(driver.get("cwd", ""), str):
            raise LaunchError(f"{where}.drivers.{name}.cwd is a folder")


def _check_overlay(value, where: str):
    if not isinstance(value, str) or not FOLDER.match(value) or ".." in value.split("/"):
        raise LaunchError(f"{where}.overlay is a folder under {LAUNCH_FOLDER}/ (like 'standalone'), not {value!r}")


def base_definition(table, where: str) -> dict:
    """A base's table (a [launch.<name>] without members), checked: known keys, its command lines, the JVM profile,
    the view keys, the overlay folder."""
    if not isinstance(table, dict):
        raise LaunchError(f"{where} is not a table")
    if "members" in table:
        raise LaunchError(f"{where} lists members, so it is a launch, not a base")
    for key in sorted(set(table) - BASE_KEYS):
        raise LaunchError(f"{where}: unknown key {key} (a base's keys: {', '.join(sorted(BASE_KEYS))})")
    for key in BASE_REQUIRED:
        if not _strings(table.get(key)) or not table[key]:
            raise LaunchError(f"{where}.{key} is a command line, a list of strings")
    for key in ("summary", "player", "memory", "jvm_args_file", "eula", "ready"):
        if not isinstance(table.get(key, ""), str):
            raise LaunchError(f"{where}.{key} is a string")
    if table.get("runtime", "cleanroom") not in RUNTIMES:
        raise LaunchError(f"{where}.runtime is one of {', '.join(RUNTIMES)}")
    profile = table.get("jvm_profile") or {}
    if not isinstance(profile, dict) or set(profile) - JVM_PROFILE_KEYS or not _strings(profile.get("arguments", [])):
        raise LaunchError(f"{where}.jvm_profile is a table of {', '.join(sorted(JVM_PROFILE_KEYS))}, its arguments a "
                          "list of JVM arguments")
    views = table.get("views") or {}
    if not isinstance(views, dict) or not all(_strings(v) and all(VIEW_KEY.match(k) for k in v)
                                              for v in views.values()):
        raise LaunchError(f"{where}.views is a table of view = ['<server|client>/<file>:<key>', ...]")
    files = table.get("files") or {}
    if not isinstance(files, dict) or set(files) - set(SIDES) or not all(
            isinstance(f, dict) and all(isinstance(t, str) for t in f.values()) for f in files.values()):
        raise LaunchError(f"{where}.files is a table of both, server and client, each of <file> = <text>")
    if "overlay" in table:
        _check_overlay(table["overlay"], where)
    return table


def parse_base(text, where: str) -> tuple[str, str]:
    """(project, name) of a launch's ``base``."""
    match = BASE_NAME.match(str(text)) if isinstance(text, str) else None
    if not match:
        raise LaunchError(f"{where}.base is '<project>:<name>' (its [launch.<name>]), not {text!r}")
    return match.group(1), match.group(2)


def definition(project_toml: dict, name: str, where: str = "project.toml") -> dict:
    """The [launch.<name>] table, checked: known keys, members parsed, a known runtime, the base, overlay, configure
    script, typed params, tour rules and drivers."""
    launches = project_toml.get("launch") or {}
    table = launches.get(name)
    if not isinstance(table, dict) or name == "member":
        defined = sorted(k for k in launches if k != "member")
        raise LaunchError(f"{where} has no [launch.{name}] (defined: {', '.join(defined) or 'none'})")
    if "members" not in table and any(key in table for key in BASE_REQUIRED):
        raise LaunchError(f"{where}: [launch.{name}] is a base, which starts no members of its own: a launch names it "
                          f"with base = '<project>:{name}'")
    for key in sorted(set(table) - LAUNCH_KEYS):
        raise LaunchError(f"{where}: unknown key launch.{name}.{key}")
    if "base" in table:
        parse_base(table["base"], f"{where}: launch.{name}")
    if "overlay" in table:
        _check_overlay(table["overlay"], f"{where}: launch.{name}")
    configure = table.get("configure")
    if configure is not None and (not isinstance(configure, str) or not CONFIGURE.match(configure)
                                  or ".." in configure.split("/")):
        raise LaunchError(f"{where}: launch.{name}.configure is a Python file under {LAUNCH_FOLDER}/ "
                          f"(launch/configure.py), not {configure!r}")
    params_of(table, f"{where}: launch.{name}")
    if "tour" in table:
        _check_tour(table["tour"], f"{where}: launch.{name}")
    if "drivers" in table:
        _check_drivers(table["drivers"], f"{where}: launch.{name}")
    for key in ("collect", "failures"):
        if key in table and not isinstance(table[key], list):
            raise LaunchError(f"{where}: launch.{name}.{key} is a list")
    members = table.get("members")
    if not isinstance(members, list) or not members:
        raise LaunchError(f"{where}: launch.{name}.members lists the launch's members")
    for member in members:
        parse_member(member)
    if len(set(members)) != len(members):
        raise LaunchError(f"{where}: launch.{name}.members names a member twice")
    place = table.get("place") or {}
    if not isinstance(place, dict):
        raise LaunchError(f"{where}: launch.{name}.place is a table of member = file name")
    for member, file in place.items():
        if member not in members:
            raise LaunchError(f"{where}: launch.{name}.place names {member}, which is not a member")
        if not isinstance(file, str) or not PLACE.match(file):
            raise LaunchError(f"{where}: launch.{name}.place.{member} is a JAR file name, not {file!r}")
    runtime_of(table, f"{where}: launch.{name}")
    _jvm(table.get("jvm"), f"{where}: launch.{name}")
    return table


def runtime_of(table: dict, where: str) -> dict:
    """A launch's ``runtime``, checked: ``{kind}`` for a bare kind, else the table with its version and either its
    three files (each ``url`` on https, a bare ``file`` name and its ``sha256``) or the third entry it names."""
    value = table.get("runtime", "cleanroom")
    if isinstance(value, str):
        if value not in RUNTIMES:
            raise LaunchError(f"{where}.runtime is one of {', '.join(RUNTIMES)}, or a table naming its version")
        return {"kind": value}
    if not isinstance(value, dict):
        raise LaunchError(f"{where}.runtime is a kind ({', '.join(RUNTIMES)}) or a table {{ kind, version, ... }}")
    for key in sorted(set(value) - RUNTIME_KEYS):
        raise LaunchError(f"{where}: unknown key runtime.{key} (known: {', '.join(sorted(RUNTIME_KEYS))})")
    if value.get("kind", "cleanroom") not in RUNTIMES:
        raise LaunchError(f"{where}.runtime.kind is one of {', '.join(RUNTIMES)}")
    version = value.get("version")
    if not isinstance(version, str) or not RUNTIME_VERSION.match(version):
        raise LaunchError(f"{where}.runtime.version is the runtime's version (like 0.7.7-alpha), not {version!r}")
    out = {"kind": value.get("kind", "cleanroom"), "version": version}
    named = [part for part in RUNTIME_FILES if part in value]
    if "third" in value:
        if named:
            raise LaunchError(f"{where}.runtime names a third entry and files ({', '.join(named)}): one or the other")
        if not isinstance(value["third"], str) or not MEMBER.match(value["third"] + "@both"):
            raise LaunchError(f"{where}.runtime.third names a third entry of the set, not {value['third']!r}")
        return {**out, "third": value["third"]}
    if named:
        missing = [part for part in RUNTIME_FILES if part not in value]
        if missing:
            raise LaunchError(f"{where}.runtime names {', '.join(named)} but not {', '.join(missing)}: a runtime is "
                              "its installer, its universal JAR and its launcher instance")
        files = {}
        for part in RUNTIME_FILES:
            entry = value[part]
            if not isinstance(entry, dict) or set(entry) != {"url", "file", "sha256"}:
                raise LaunchError(f"{where}.runtime.{part} is {{ url, file, sha256 }}")
            if not str(entry["url"]).startswith("https://"):
                raise LaunchError(f"{where}.runtime.{part}.url is an https URL, not {entry['url']!r}")
            if not isinstance(entry["file"], str) or "/" in entry["file"] or "\\" in entry["file"] or \
                    not entry["file"].strip(". "):
                raise LaunchError(f"{where}.runtime.{part}.file is a bare file name, not {entry['file']!r}")
            if not isinstance(entry["sha256"], str) or not SHA256.match(entry["sha256"].lower()):
                raise LaunchError(f"{where}.runtime.{part}.sha256 is 64 hex digits, not {entry['sha256']!r}")
            files[part] = {"url": entry["url"], "file": entry["file"], "sha256": entry["sha256"].lower()}
        return {**out, "files": files}
    return out


def runtime_document(declared: dict, platform: dict, thirds: dict, store: Path, where: str) -> dict:
    """launch.json's ``runtime``: the kind and version, the platform's Java and its exact JDK build, and the
    runtime's own files (``files``) as the launch, its third entry or the platform names them (``source``)."""
    cleanroom = platform.get("cleanroom") or {}
    java = platform.get("java")
    version = declared.get("version") or cleanroom.get("version", "")
    runtime = {"kind": declared["kind"], "version": version, "java": java,
               "jdk": ((platform.get("jdk") or {}).get(str(java)) or {}).get("build", "")}
    if declared.get("files"):
        runtime.update(files=json.loads(json.dumps(declared["files"])), source="launch")
    elif declared.get("third"):
        name = declared["third"]
        third = thirds.get(name)
        if not isinstance(third, dict):
            raise LaunchError(f"{where}: runtime.third names {name}, which the set has no third entry for (it has: "
                              f"{', '.join(sorted(thirds)) or 'none'})")
        kinds = third.get("files") or {}
        missing = [part for part in RUNTIME_FILES if not kinds.get(part)]
        if missing:
            raise LaunchError(f"{where}: third entry {name} has no {', '.join(missing)} file (it has: "
                              f"{', '.join(sorted(kinds)) or 'none'})")
        files = {}
        for part in RUNTIME_FILES:
            try:
                file_name, _ = _store_file(store, kinds[part], None)
            except Exception as error:  # noqa: BLE001 - the Store's own error names the digest
                raise LaunchError(f"{where}: runtime {part} of third entry {name}: {error}; the landing that "
                                  "recorded it puts it there") from None
            files[part] = {"file": file_name, "sha256": kinds[part],
                           **({"url": third["urls"][part]} if isinstance(third.get("urls"), dict)
                              and third["urls"].get(part) else {})}
        runtime.update(files=files, source=f"third {name}")
    elif version == cleanroom.get("version"):
        # The runtime's own files as the platform names them (a landing of platform.cleanroom keeps each in the
        # Store): lab.build makes an instance from these, so the launch file carries them.
        files = {part: {key: cleanroom[part][key] for key in ("url", "file", "sha256") if key in cleanroom[part]}
                 for part in RUNTIME_FILES if isinstance(cleanroom.get(part), dict)}
        if files:
            runtime.update(files=files, source="platform")
    else:
        raise LaunchError(f"{where}: runtime {declared['kind']} {version} is not the platform's "
                          f"{cleanroom.get('version') or 'none'}, and the launch names none of its files: give its "
                          "installer, universal and prism as { url, file, sha256 }, or third = '<entry>'")
    return runtime


def member_extras(project_toml: dict | None, where: str) -> dict:
    """A member's own [launch.member] table, checked; empty when it has none."""
    table = ((project_toml or {}).get("launch") or {}).get("member") or {}
    for key in sorted(set(table) - MEMBER_KEYS):
        raise LaunchError(f"{where}: unknown key launch.member.{key}")
    return {"jvm": _jvm(table.get("jvm"), f"{where}: launch.member"), "collect": list(table.get("collect") or []),
            "failures": list(table.get("failures") or [])}


def _store_file(store: Path, digest: str, name: str | None) -> tuple[str, Path]:
    """(file name, path) of a kept file; a third entry's name is the one file in its folder."""
    from wb_core import artifacts
    if name is None:
        folder = store / "files" / digest
        names = sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []
        if len(names) != 1:
            raise artifacts.StoreError(f"the Store has no file with SHA-256 {digest}")
        name = names[0]
    return name, artifacts.path_of(store, digest, name)


def _object(objects, project: str, commit: str, path: str, kind: str, where: str) -> str:
    """The id of the ``kind`` (tree or blob) at ``path`` in ``project`` at ``commit``; refused when there is none."""
    if objects is None:
        raise LaunchError(f"{where}: {path} names a folder or file of {project}, and nothing here reads its "
                          "repository")
    found = objects(project, commit, path)
    if not found or found[0] != kind:
        what = "folder" if kind == "tree" else "file"
        raise LaunchError(f"{where}: {project} has no {what} {path} at {commit[:12]}")
    return found[1]


def pick_commit(project: str, *, product: str, commit: str, set_doc: dict, follow, where: str) -> dict:
    """The commit a launch reads ``project`` at, and how it was chosen: the product's own commit; a hold
    ``<project>@<product>``; the set's line; else, for a project with no line, its branch in its home repository
    (``follow(project)`` gives (commit, ref), or None for a project this machine does not know)."""
    if project == product:
        return {"commit": commit, "chosen": "product"}
    held = (set_doc.get("holds") or {}).get(f"{project}@{product}") or {}
    if held.get("commit"):
        return {"commit": held["commit"], "chosen": "hold"}
    line = (set_doc.get("projects") or {}).get(project) or {}
    if line.get("commit"):
        return {"commit": line["commit"], "chosen": "line"}
    found = follow(project) if follow is not None else None
    if found:
        return {"commit": found[0], "chosen": "branch", "ref": found[1]}
    raise LaunchError(f"{where}: the set has no line for {project}"
                      + ("" if follow is None else f", and {project} is not registered on this machine, so its branch "
                                                   "cannot be followed"))


def base_of(*, name: str, product: str, commit: str, product_toml: dict | None, set_doc: dict, extras, objects,
            warn=None, follow=None, picked: dict | None = None) -> dict:
    """The launch file's ``base``: the base project's [launch.<name>] at the commit the set names for it (or its
    branch, for a project with no line), checked, and its overlay folder's tree id; Workbench's interim copy, with a
    warning, when that commit has none yet."""
    from .bases import INTERIM_BASES
    project, launch = parse_base(name, "launch")
    picked = picked or pick_commit(project, product=product, commit=commit, set_doc=set_doc, follow=follow,
                                   where=f"base {name}")
    at = picked["commit"]
    if project == product:
        toml = product_toml
    else:
        if extras is None:
            raise LaunchError(f"base {name}: nothing here reads {project}'s project.toml")
        toml = extras(project, at)
    where = f"base {name} ({project} {at[:12]} project.toml [launch.{launch}])"
    table = ((toml or {}).get("launch") or {}).get(launch)
    record = {"name": name, "project": project, "commit": at, **{k: picked[k] for k in ("chosen", "ref")
                                                                  if k in picked}}
    if table is None:
        if name not in INTERIM_BASES:
            raise LaunchError(f"{project} at {at[:12]} has no [launch.{launch}], so there is no base {name}")
        if warn:
            warn(f"INTERIM BASE: {project} at {at[:12]} has no [launch.{launch}] yet, so base {name} is Workbench's "
                 f"interim copy (wb_lab/bases.py); it goes once {project}'s line carries [launch.{launch}]")
        return {**record, "source": "interim", "definition": json.loads(json.dumps(INTERIM_BASES[name])),
                "overlay": None}
    base_definition(table, where)
    overlay = None
    if "overlay" in table:
        path = f"{LAUNCH_FOLDER}/{table['overlay']}"
        overlay = {"path": path, "tree": _object(objects, project, at, path, "tree", where)}
    return {**record, "source": "project.toml", "definition": json.loads(json.dumps(table)), "overlay": overlay}


def _indexed(spec: str, project: str, tree: str, glob: str, index: dict) -> tuple[str, dict]:
    """(file, item) of the one file of a Store index that matches an [artifacts] glob."""
    found = [(file, item) for file, item in (index.get("artifacts") or {}).items()
             if fnmatch.fnmatch(str(item.get("from") or file), glob) or fnmatch.fnmatch(file, Path(glob).name)]
    if len(found) != 1:
        raise LaunchError(f"member {spec}: the Store's index of {project} tree {tree[:12]} has "
                          f"{len(found)} files matching {glob}")
    return found[0]


def _tree_of(objects, project: str, commit: str, where: str) -> str:
    found = objects(project, commit, "") if objects is not None else None
    if not found or found[0] != "tree":
        raise LaunchError(f"{where}: nothing here reads {project}'s tree at {commit[:12]}")
    return found[1]


def _inherit(*, table: dict, launch: str, product: str, commit: str, set_doc: dict, revision: str, store: Path,
             extras, objects, product_toml, warn, follow, chain) -> tuple[dict | None, dict | None, dict | None]:
    """(parent, link, picked) for a launch whose ``base`` names another launch (a table with members): that launch
    resolved at the base project's commit as that product resolves it, the record of the step for ``inherits``, and
    how the commit was chosen. (None, None, picked) when the base is a base table; all None without a base."""
    from wb_core import artifacts
    if "base" not in table:
        return None, None, None
    project, name = parse_base(table["base"], f"launch.{launch}")
    where = f"base {table['base']}"
    picked = pick_commit(project, product=product, commit=commit, set_doc=set_doc, follow=follow, where=where)
    at = picked["commit"]
    if project == product and at == commit:
        toml = product_toml
    elif extras is None:
        raise LaunchError(f"{where}: nothing here reads {project}'s project.toml")
    else:
        toml = extras(project, at)
    found = ((toml or {}).get("launch") or {}).get(name)
    if not isinstance(found, dict) or "members" not in found:
        return None, None, picked  # a base table, or none: base_of reads it
    trail = [*chain, f"{product}:{launch}"]
    key = f"{project}:{name}"
    if key in trail:
        raise LaunchError(f"the launch's bases go round: {' -> '.join([*trail, key])}")
    if len(trail) >= MAX_DEPTH:
        raise LaunchError(f"the launch's bases are more than {MAX_DEPTH} launches deep: {' -> '.join([*trail, key])}")
    checked = definition(toml, name, f"{project} {at[:12]} project.toml")
    tree = _tree_of(objects, project, at, where)
    parent = resolve(product=project, commit=at, tree=tree, launch=name, table=checked, set_doc=set_doc,
                     revision=revision, store=store, product_artifacts=(toml or {}).get("artifacts") or {},
                     product_index=artifacts.find(store, project, tree), extras=extras, objects=objects,
                     product_toml=toml, warn=warn, follow=follow, chain=tuple(trail))
    link = {"name": key, "project": project, "launch": name, "commit": at, "tree": tree,
            **{k: picked[k] for k in ("chosen", "ref") if k in picked},
            "launch_tree": (parent.get("definition") or {}).get("launch_tree")}
    return parent, link, picked


def _inherited_params(table: dict, parent: dict, where: str) -> tuple[dict, dict]:
    """The inherited params with the launch's own over them: a bare value keeps the inherited type and limits, a
    table replaces the declaration."""
    own_defaults, own_types = params_of(table, where)
    defaults, types = dict(parent.get("params") or {}), dict(parent.get("param_types") or {})
    for name, value in (table.get("params") or {}).items():
        if not isinstance(value, dict) and name in types:
            defaults[name] = param_value(name, value, types[name], where)
            continue
        types[name] = own_types[name]
        defaults.pop(name, None)
        if name in own_defaults:
            defaults[name] = own_defaults[name]
    return defaults, types


def resolve(*, product: str, commit: str, tree: str, launch: str, table: dict, set_doc: dict, revision: str,
            store: Path, product_artifacts: dict, product_index: dict | None, extras=None, objects=None,
            product_toml: dict | None = None, warn=None, follow=None, chain: tuple = ()) -> dict:
    """The launch file for ``table`` (a checked [launch.<name>]) of ``product`` at ``commit``/``tree`` on set
    ``revision``. ``product_artifacts`` is the product's [artifacts] (each with its ``file`` glob), ``product_index``
    the Store's index of its tree; ``extras(project, commit)`` returns that member's project.toml or None;
    ``objects(project, commit, path)`` the (type, id) of a path in that project's repository at that commit (the root
    tree for path ''), or None; ``product_toml`` is the product's own project.toml and ``warn`` takes what the person
    should see. ``follow(project)`` gives (commit, ref) of a project with no line, its branch in its home repository,
    or None for a project this machine does not know. ``chain`` names the launches that inherit this one."""
    from wb_core import artifacts
    lines, holds, thirds = set_doc.get("projects") or {}, set_doc.get("holds") or {}, set_doc.get("third") or {}
    parent, link, picked = _inherit(table=table, launch=launch, product=product, commit=commit, set_doc=set_doc,
                                    revision=revision, store=store, extras=extras, objects=objects,
                                    product_toml=product_toml, warn=warn, follow=follow, chain=chain)
    members, owners = [], {}
    jvm = {"client": [], "server": []}
    collect, failures = [], []
    declared = runtime_of(table, f"launch.{launch}")
    if parent is not None:
        inherited = parent["runtime"]
        named = declared.get("version")
        if declared["kind"] != inherited["kind"] or (named and (named != inherited.get("version") or (
                declared.get("files") and declared["files"] != inherited.get("files")))):
            raise LaunchError(f"launch.{launch}: runtime {declared['kind']} {named or ''}".rstrip() + " is not its "
                              f"base {link['name']}'s {inherited['kind']} {inherited.get('version', '')}".rstrip()
                              + ": a launch runs on the runtime of the launch it inherits")
        for member in parent["members"]:
            entry = {**member, "via": member.get("via") or link["name"]}
            members.append(entry)
            for mod_id in entry.get("mod_ids") or []:
                owners.setdefault(mod_id, []).append(f"{entry['member']} (from {entry['via']})")
        jvm = {side: list(values) for side, values in parent["jvm"].items()}
        collect, failures = list(parent.get("collect") or []), list(parent.get("failures") or [])
    collect += [c for c in table.get("collect") or [] if c not in collect]
    failures += [f for f in table.get("failures") or [] if f not in failures]
    inherited = {m["member"]: m["via"] for m in members}
    for spec in table["members"]:
        if spec in inherited:
            raise LaunchError(f"member {spec}: the base {inherited[spec]} already names it")
        name, part, side = parse_member(spec)
        entry = {"member": spec, "name": name, "side": side}
        file_name = None
        found = None
        if name == product:
            if not part:
                raise LaunchError(f"member {spec}: name the product's artifact ({product}:<artifact>@<side>)")
            glob = (product_artifacts.get(part) or {}).get("file")
            if not glob:
                raise LaunchError(f"member {spec}: {product} has no [artifacts.{part}]")
            if product_index is None:
                if chain:
                    raise MissingBuild(f"base {product}:{launch}: CI has not built {product} tree {tree[:12]} (commit "
                                       f"{commit[:12]}), so the Store has no {product}:{part}: run wb check in "
                                       f"{product} at {commit[:12]}, then resolve again", product, commit)
                raise MissingBuild(f"CI has not built {product} tree {tree[:12]} (commit {commit[:12]}), so the Store "
                                   f"has no {product}:{part}: run wb check in the worktree, then resolve again",
                                   product, commit)
            file_name, item = _indexed(spec, product, tree, glob, product_index)
            entry.update(kind="product", artifact=part, sha256=item["sha256"], commit=commit, tree=tree)
            source_project, source_commit = product, commit
        elif part and f"{name}:{part}" in holds and part in (holds[f"{name}:{part}"].get("artifacts") or {}):
            hold = holds[f"{name}:{part}"]
            item = hold["artifacts"][part]
            file_name = item.get("file")
            entry.update(kind="hold", artifact=part, sha256=item["sha256"], commit=hold.get("commit"),
                         reason=hold.get("reason", ""))
            source_project, source_commit = name, hold.get("commit")
        elif part and name in lines:
            item = (lines[name].get("artifacts") or {}).get(part)
            if not item:
                raise LaunchError(f"member {spec}: {name}'s line names no artifact {part} "
                                  f"(it has: {', '.join(sorted(lines[name].get('artifacts') or {})) or 'none'})")
            file_name = item.get("file")
            entry.update(kind="line", artifact=part, sha256=item["sha256"], commit=lines[name]["commit"])
            source_project, source_commit = name, lines[name]["commit"]
        elif name in thirds:
            kind = part or "runtime"
            third = thirds[name]
            # A coordinate's files by kind; a file without one (from the drop folder) is its runtime file.
            files = third.get("files") or ({"runtime": third["sha256"]} if third.get("sha256") else {})
            digest = files.get(kind)
            if not digest:
                raise LaunchError(f"member {spec}: third entry {name} has no {kind} file "
                                  f"(it has: {', '.join(sorted(files)) or 'none'})")
            file_name = third.get("file") if not third.get("files") else None
            entry.update(kind="third", artifact=kind, sha256=digest, module=third.get("module", ""),
                         licence=third.get("licence", ""), **({"source": third["source"]} if third.get("source") else {}))
            source_project, source_commit = None, None
        elif part and follow is not None and (found := follow(name)):
            # A project with no line: its branch in its home repository, its file from the Store by that tree.
            at, ref = found
            own_toml = extras(name, at) if extras is not None else None
            glob = (((own_toml or {}).get("artifacts") or {}).get(part) or {}).get("file")
            if not glob:
                raise LaunchError(f"member {spec}: {name} has no line in the set, and at {at[:12]} ({ref}) its "
                                  f"project.toml declares no [artifacts.{part}]")
            at_tree = _tree_of(objects, name, at, f"member {spec}")
            index = artifacts.find(store, name, at_tree)
            if index is None:
                raise MissingBuild(f"member {spec}: {name} has no line, so it follows {ref} at {at[:12]}, and CI has "
                                   f"not built its tree {at_tree[:12]}: run wb check in {name} at {at[:12]}, then "
                                   "resolve again", name, at)
            file_name, item = _indexed(spec, name, at_tree, glob, index)
            entry.update(kind="branch", artifact=part, sha256=item["sha256"], commit=at, tree=at_tree, ref=ref)
            source_project, source_commit = name, at
        else:
            known = sorted({*lines, *thirds, product})
            raise LaunchError(f"member {spec}: the set names no project or third entry {name} "
                              f"(known: {', '.join(known)})" + ("" if part or name not in lines else
                                                               f"; a project member names its artifact ({name}:mod)"))
        try:
            file_name, path = _store_file(store, entry["sha256"], file_name)
        except artifacts.StoreError as error:
            fix = (f"wb check in {source_project} at {str(source_commit)[:12]}" if source_project and
                   entry["kind"] != "hold" else "the landing that recorded it puts it there")
            if source_project and entry["kind"] != "hold" and source_commit:
                raise MissingBuild(f"member {spec}: {error}; {fix}", source_project, source_commit) from None
            raise LaunchError(f"member {spec}: {error}; {fix}") from None
        ids = mod_ids(path)
        if entry["kind"] == "third" and thirds[name].get("mod_id") and thirds[name]["mod_id"] not in ids:
            ids = sorted({*ids, thirds[name]["mod_id"]})
        entry.update(file=file_name, path=str(path.relative_to(store)).replace("\\", "/"), mod_ids=ids)
        if spec in (table.get("place") or {}):
            entry["place"] = table["place"][spec]
        for mod_id in ids:
            owners.setdefault(mod_id, []).append(spec)
        if source_project and source_project != product and extras is not None:
            own = member_extras(extras(source_project, source_commit), f"{source_project} {str(source_commit)[:12]}")
            _add_jvm(jvm, own["jvm"])
            collect += [c for c in own["collect"] if c not in collect]
            failures += [f for f in own["failures"] if f not in failures]
        members.append(entry)
    doubled = {mod_id: specs for mod_id, specs in owners.items() if len(specs) > 1}
    if doubled:
        raise LaunchError("the launch would load two builds of one mod id: "
                          + "; ".join(f"{mod_id} from {', '.join(specs)}" for mod_id, specs in sorted(doubled.items())))
    own_jvm = _jvm(table.get("jvm"), f"launch.{launch}")
    if parent is not None:
        _override_jvm(jvm, own_jvm)
    else:
        _add_jvm(jvm, own_jvm)
    if parent is not None:
        runtime = json.loads(json.dumps(parent["runtime"]))  # the inherited launch's runtime, as it resolved it
    else:
        runtime = runtime_document(declared, set_doc.get("platform") or {}, thirds, store, f"launch.{launch}")
    if parent is not None:
        defaults, types = _inherited_params(table, parent, f"launch.{launch}")
    else:
        defaults, types = params_of(table, f"launch.{launch}")
    document = {"schema": SCHEMA, "project": product, "launch": launch, "commit": commit, "tree": tree,
                "set": revision, "store": str(store), "runtime": runtime, "members": members, "jvm": jvm,
                "collect": collect, "failures": failures, "params": defaults, "param_types": types}
    if parent is not None:
        document["inherits"] = [link, *(parent.get("inherits") or [])]
    # The definition's commit and its launch/ folder, the product's overlay and configure script in it, and the base.
    found = objects(product, commit, LAUNCH_FOLDER) if objects is not None else None
    document["definition"] = {"commit": commit, "launch_tree": found[1] if found and found[0] == "tree" else None}
    where = f"{product} {commit[:12]} launch.{launch}"
    if "overlay" in table:
        path = f"{LAUNCH_FOLDER}/{table['overlay']}"
        document["overlay"] = {"project": product, "commit": commit, "path": path,
                               "tree": _object(objects, product, commit, path, "tree", where)}
    if parent is not None:
        # Every launch's own overlay in the order they are laid: the farthest base launch's first, this one's last.
        overlays = list(parent.get("overlays") or ([parent["overlay"]] if parent.get("overlay") else []))
        overlays += [document["overlay"]] if "overlay" in document else []
        if overlays:
            document["overlays"] = overlays
    if "configure" in table:
        document["configure"] = {"project": product, "commit": commit, "path": table["configure"],
                                 "blob": _object(objects, product, commit, table["configure"], "blob", where)}
    elif parent is not None and parent.get("configure"):
        document["configure"] = parent["configure"]
    if parent is not None:
        if parent.get("base"):
            document["base"] = parent["base"]
    elif "base" in table:
        document["base"] = base_of(name=table["base"], product=product, commit=commit, product_toml=product_toml,
                                   set_doc=set_doc, extras=extras, objects=objects, warn=warn, follow=follow,
                                   picked=picked)
    # How a session drives the game (lab.run): the tour rules (an inheriting launch's over its base's, key by key) and
    # the drivers, which run from this product's tree and so are never inherited.
    tour = {**((parent or {}).get("tour") or {}), **(table.get("tour") or {})}
    if tour:
        document["tour"] = json.loads(json.dumps(tour))
    if table.get("drivers"):
        document["drivers"] = json.loads(json.dumps(table["drivers"]))
    return document


# --- the job ------------------------------------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> str | None:
    done = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, encoding="utf-8")
    return done.stdout if done.returncode == 0 else None


def _toml_at(repo: Path, commit: str, where: str) -> dict | None:
    if _git(repo, "cat-file", "-e", f"{commit}^{{commit}}") is None:
        raise LaunchError(f"{where}: {repo} does not have commit {commit[:12]}; fetch it (git -C {repo} fetch)")
    text = _git(repo, "show", f"{commit}:project.toml")
    if text is None:
        return None
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise LaunchError(f"{where}: project.toml at {commit[:12]} does not parse: {error}") from None


def object_at(repo: Path, commit: str, path: str) -> tuple[str, str] | None:
    """(type, id) of ``path`` in ``repo`` at ``commit``, or None when it has no such path."""
    found = _git(repo, "rev-parse", "--verify", "--quiet", f"{commit}:{path}")
    if found is None or not found.strip():
        return None
    kind = _git(repo, "cat-file", "-t", found.strip())
    return (kind.strip(), found.strip()) if kind else None


def resolve_command(ctx, folder: str, launch: str = "default", out: str = ""):
    from wb.setwork import resolve as set_revision
    from wb.workspace import Workspace, WorkspaceError
    from wb_core import artifacts
    from wb_core.module import EnvironmentProblem, ParamError
    place = Path(folder)
    top_text = _git(place, "rev-parse", "--show-toplevel")
    if top_text is None:
        raise ParamError(f"{place} is not in a git worktree")
    top = Path(top_text.strip())
    if _git(top, "status", "--porcelain", "--untracked-files=no"):
        raise ParamError(f"{top} has uncommitted changes: a launch describes a commit, whose tree CI built; commit "
                         "them and run wb check first")
    commit = _git(top, "rev-parse", "HEAD").strip()
    tree = _git(top, "rev-parse", "HEAD^{tree}").strip()
    try:
        workspace = Workspace()
        store = workspace.store_root()
        home = workspace.set_home()
    except (WorkspaceError, OSError) as error:
        raise EnvironmentProblem(f"no workspace to resolve against: {error}") from None
    if store is None or home is None:
        raise EnvironmentProblem("this machine has no Store or no Set (modules.ci.store, workspace.set)")
    revision = set_revision(workspace, top, say=ctx.log.info)
    shown = _git(home, "show", f"{revision}:set.json")
    if shown is None:
        raise EnvironmentProblem(f"the Set has no revision {revision}")
    set_doc = json.loads(shown)
    try:
        own = _toml_at(top, commit, "the product")
        if own is None:
            raise LaunchError(f"{top} has no project.toml at {commit[:12]}, so it defines no launch")
        product = str((own.get("project") or {}).get("name") or "")
        table = definition(own, launch, f"{product or top.name} project.toml")
        registered = workspace.projects()

        def repository(project: str) -> Path:
            if project == product:
                return top
            entry = registered.get(project)
            if entry is None or not entry.repo.is_dir():
                raise LaunchError(f"project {project} is not registered on this machine ([modules.ci.projects]), so "
                                  "its [launch.member] and launch/ folder cannot be read")
            return entry.repo

        def extras(project: str, at: str):
            return _toml_at(repository(project), at, f"project {project}")

        def objects(project: str, at: str, path: str):
            return object_at(repository(project), at, path)

        def follow(project: str):
            # A project with no line follows its branch in its home repository: origin's when it has a remote, else
            # the local branch, else HEAD (the rule a check's tools follow), and the summary says which commit.
            entry = registered.get(project)
            if entry is None or not entry.repo.is_dir():
                return None
            branch = str((getattr(entry, "entry", None) or {}).get("branch") or "main")
            for ref in (f"refs/remotes/origin/{branch}", f"refs/heads/{branch}", "HEAD"):
                sha = _git(entry.repo, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}")
                if sha and sha.strip():
                    ctx.log.info(f"{project} has no line in set {revision[:12]}: {ref} at {sha.strip()[:12]}")
                    return sha.strip(), ref
            return None

        warnings = []

        def warn(text: str):
            warnings.append(text)
            ctx.log.warning(text)

        document = resolve(product=product, commit=commit, tree=tree, launch=launch, table=table, set_doc=set_doc,
                           revision=revision, store=store, product_artifacts=own.get("artifacts") or {},
                           product_index=artifacts.find(store, product, tree), extras=extras, objects=objects,
                           product_toml=own, warn=warn, follow=follow)
    except MissingBuild as error:
        raise ParamError(str(error)) from error  # wb play reads what builds it from the cause
    except LaunchError as error:
        raise ParamError(str(error)) from None
    target = Path(out) if out else top / ".local" / "launch" / f"{launch}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(document, indent=1) + "\n"
    target.write_text(text, encoding="utf-8")
    (ctx.out / "launch.json").write_text(text, encoding="utf-8")
    ctx.output("launch", ctx.out / "launch.json")
    base = document.get("base")
    based = f", base {base['name']} at {base['commit'][:12]}" + (" (INTERIM COPY)" if base["source"] == "interim"
                                                                 else "") if base else ""
    chain = document.get("inherits") or []
    inherited = "".join(f", inherits {step['name']} at {step['commit'][:12]} ({step['chosen']}"
                        + (f" {step['ref']}" if step.get("ref") else "") + ")" for step in chain)
    ctx.summary(f"launch {product}:{launch} at {commit[:12]} on set {revision[:7]}: {len(document['members'])} "
                f"members{inherited}{based}, written to {target}")
    return {"launch": str(target), "set": revision, "commit": commit,
            "members": {m["member"]: m["sha256"] for m in document["members"]},
            **({"base": {k: base[k] for k in ("name", "commit", "source")}} if base else {}),
            **({"inherits": [{k: step[k] for k in ("name", "commit", "chosen") if k in step} for step in chain]}
               if chain else {}),
            **({"warnings": warnings} if warnings else {})}
