"""Check the Mixin configurations in JARs against a loader policy, from the archives' bytes (``mixin.doctor``).

Nothing is loaded or run. Every configuration a JAR's metadata names, and every JSON at its root shaped as
one, is checked for what the loader will do with it; each check's severity comes from the policy
(``wb_mixin.policy``):

- **parse**: the JAR and the configuration can be read, it is a JSON object, and the keys Mixin reads have
  their types (``mixins`` a list of names, ``required`` true or false, ``priority`` a whole number, ...);
- **fields**: it sets every key the policy requires;
- **classes**: it names its ``package``, and each name its ``mixins``, ``client`` and ``server`` lists hold is
  a class in the JAR under that package, carries ``@Mixin``, and is listed once in the JAR;
- **refmap**: the refmap it names is packaged and readable, and it names one when its Mixins remap selectors
  (an injector, accessor or invoker, or a ``targets`` string, with ``remap`` on, reaching a class the
  policy's ``remapped_packages`` cover);
- **compatibility**: ``compatibilityLevel`` is a level, and one the loader's Mixin defines;
- **coverage**: that level covers the class-file version of each of its Mixin classes (``JAVA_25`` covers 69,
  not the 70 of a class compiled for Java 26);
- **class-version**: no Mixin or plugin class is newer than the highest class-file version the runtime reads;
- **min-version**: ``minVersion`` is a version Mixin reads, and not above the loader's Mixin;
- **plugin**: its plugin class is in the JARs or on the class path;
- **targets**, given a class path: each ``@Mixin`` target, read through the refmap, is on the class path or in
  the JARs (a ``@Pseudo`` Mixin's need not be). A missing one is the check's severity in a required
  configuration, unless the policy says the loader skips such a Mixin, and a warning anywhere else;
- **injection-points**: each ``@At`` names one of Mixin's built-in points, a point a configuration of the JARs
  registers (``injectors.injectionPoints``), one in a namespace the loader provides, or a class that is there;
- **duplicates**: no configuration name is in two of the JARs given (nested JARs are not counted: a loader
  takes one copy of a bundled library);
- **registration**: the manifest (``MixinConfigs``), a loader's metadata (``fabric.mod.json``, ``mods.toml``)
  or a string constant in a class of the JARs (a loading plugin's list) names it.

Classes and annotations are read with ``wb_jar``; a selector the refmap does not map is used as written. A
configuration's ``parent`` is not followed.
"""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from collections import Counter, defaultdict
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from wb_jar import classes, mixins
from wb_jar.archive import MAX_CLASS_BYTES, Archive, archives
from wb_jar.archive import jars as find_jars
from wb_jar.archive import label as jar_label

from .policy import CHECKS, SHORT, Policy, class_version, java, level_of, span, version

PSEUDO = "Lorg/spongepowered/asm/mixin/Pseudo;"
AT = "Lorg/spongepowered/asm/mixin/injection/At;"
AT_CODE = "Lorg/spongepowered/asm/mixin/injection/InjectionPoint$AtCode;"
# Mixin's own injection points; every other one is registered by a configuration or a library.
BUILT_IN = frozenset({"HEAD", "RETURN", "TAIL", "INVOKE", "INVOKE_ASSIGN", "INVOKE_STRING", "FIELD", "NEW",
                      "CONSTANT", "JUMP", "LOAD", "STORE"})
_SELECTOR = re.compile(r"(.+?)(?::(?:FIRST|LAST|ONE))?")  # an @At value without its :FIRST, :LAST or :ONE
# The keys Mixin reads from a configuration, by the JSON type each must have.
TYPES = {"package": str, "refmap": str, "plugin": str, "minVersion": str, "compatibilityLevel": str, "target": str,
         "refmapWrapper": str, "parent": str, "mixins": list, "client": list, "server": list, "required": bool,
         "verbose": bool, "setSourceFile": bool, "priority": int, "mixinPriority": int, "injectors": dict,
         "overwrites": dict}
_TYPE_NAMES = {str: "a string", list: "a list of class names", bool: "true or false", int: "a whole number",
               dict: "a table"}
NAMED = 5  # classes named in one problem; the report lists every one
_RANK = {"warning": 1, "error": 2}


def _dotted(name: str) -> str:
    return name.replace("/", ".")


def _names(items: list[str]) -> str:
    shown = ", ".join(_dotted(i) for i in items[:NAMED])
    return shown + (f" and {len(items) - NAMED} more" if len(items) > NAMED else "")


def _of_its(names: list[str]) -> str:
    return f"{len(names)} of its Mixins {'is' if len(names) == 1 else 'are'}"


@dataclass
class MixinFacts:
    name: str
    """Internal form, such as ``com/example/mixins/MixinWorld``."""
    side: str
    major: int = 0
    mixin: bool = False
    """It carries ``@Mixin``."""
    pseudo: bool = False
    targets: list[str] = field(default_factory=list)
    remaps: list[str] = field(default_factory=list)
    """The classes its remapped selectors reach: the targets of its injectors, accessors and invokers and of
    its ``targets`` strings, and the owners of its ``@At`` targets, where ``remap`` is on."""
    points: list[str] = field(default_factory=list)
    """Its ``@At`` values."""

    def as_json(self) -> dict:
        return {"class": _dotted(self.name), "side": self.side, "class_version": self.major,
                "java": java(self.major) if self.major else None, "mixin": self.mixin, "pseudo": self.pseudo,
                "targets": [_dotted(t) for t in self.targets], "injection_points": self.points}


@dataclass
class ConfigFacts:
    jar: str
    name: str
    depth: int
    declared: bool = False
    """The JAR's metadata names it."""
    digest: str = ""
    value: dict | None = None
    unreadable: str | None = None
    """Why it could not be read as a configuration."""
    wrong_types: list[str] = field(default_factory=list)
    package: str = ""
    required: bool = False
    refmap: str | None = None
    refmap_problem: str | None = None
    plugin: str | None = None
    plugin_major: int | None = None
    """The plugin's class-file version, where the plugin is in the same JAR."""
    mixins: list[MixinFacts] = field(default_factory=list)
    listing: list[str] = field(default_factory=list)
    """What is wrong with what its lists name."""

    def text(self, key: str) -> str | None:
        value = (self.value or {}).get(key)
        return value if isinstance(value, str) else None


@dataclass
class JarFacts:
    label: str
    depth: int
    sha256: str | None = None
    bytes: int | None = None
    configs: list[ConfigFacts] = field(default_factory=list)


@dataclass
class World:
    """What every check sees across the JARs: their classes and the class path's, which configuration names
    their metadata and string constants give, and the injection points their configurations register."""
    classpath: bool = False
    classes: set[str] = field(default_factory=set)
    declared: set[str] = field(default_factory=set)
    literals: set[str] = field(default_factory=set)
    codes: set[str] = field(default_factory=set)
    namespaces: set[str] = field(default_factory=set)
    path_jars: list[dict] = field(default_factory=list)

    def point(self, value: str, policy: Policy) -> str | None:
        """What is wrong with an ``@At`` value; None when the loader resolves it."""
        match = _SELECTOR.fullmatch(value)
        kind = match.group(1) if match else value
        namespace, colon, code = kind.partition(":")
        if kind in BUILT_IN or kind in self.codes or (colon and f"{namespace.upper()}:{code}" in self.codes):
            return None
        if colon:
            name = namespace.upper()
            if name in self.namespaces or policy.injection_namespaces is None or name in policy.injection_namespaces:
                return None
            provided = ", ".join(sorted(policy.injection_namespaces)) or "none"
            return (f"@At(\"{value}\") is in namespace {name}, which the loader does not provide "
                    f"(it provides {provided})")
        if "." in kind:
            if classes.internal(kind) in self.classes:
                return None
            return (f"@At(\"{value}\") names a class that is not in the JARs"
                    + (" or on the class path" if self.classpath else ""))
        return f"@At(\"{value}\") is not an injection point Mixin defines or a configuration of the JARs registers"


@dataclass
class Problem:
    check: str
    severity: str
    where: str
    detail: str
    jars: tuple[str, ...] = ()
    config: str | None = None

    def as_json(self) -> dict:
        return {"check": self.check, "severity": self.severity, "where": self.where, "detail": self.detail,
                "jars": list(self.jars), "config": self.config}


# -- reading -----------------------------------------------------------------------------------------
def _class_names(archive: Archive) -> set[str]:
    out = set()
    for name in archive.entries:
        if name.endswith(".class"):
            if name.startswith("META-INF/versions/") and name.count("/") > 3:
                name = name.split("/", 3)[3]
            out.add(name[:-6])
    return out


def _ats(annotation: classes.Annotation) -> Iterator[classes.Annotation]:
    """Every ``@At`` an annotation holds, at any depth (an injector's ``at``, a ``@Slice``'s ``from``, ...)."""
    for value in annotation.values.values():
        for item in value if isinstance(value, list) else [value]:
            if isinstance(item, classes.Annotation):
                if item.type == AT:
                    yield item
                yield from _ats(item)


def _mixin(info: classes.ClassInfo, side: str, table: dict) -> MixinFacts:
    facts = MixinFacts(info.this_class, side, info.major)
    mixin = mixins.mixin_of(info, table)
    if mixin is None:
        return facts
    facts.mixin, facts.targets = True, mixin.targets
    facts.pseudo = info.annotation(PSEUDO) is not None
    reach: set[str] = set()
    if mixin.remap:
        reach.update(classes.internal(t) for t in info.annotation(mixins.MIXIN).strings("targets"))
        for method in info.declared_methods:
            for annotation in method.annotations:
                if (annotation.type in mixins.HANDLERS or annotation.type in mixins.ACCESSORS) \
                        and annotation.get("remap", True) is not False:
                    reach.update(mixin.targets)
                    reach.update(mixins.selector(target)[0] for point in _ats(annotation)
                                 if point.get("remap", True) is not False for target in point.strings("target"))
    facts.remaps = sorted(r for r in reach if r)
    facts.points = list(dict.fromkeys(point.get("value") for method in info.declared_methods
                                      for annotation in method.annotations for point in _ats(annotation)
                                      if isinstance(point.get("value"), str)))
    return facts


def _wrong_types(value: dict) -> list[str]:
    out = []
    for key, kind in TYPES.items():
        item = value.get(key)
        if item is None:
            continue
        if kind is list:
            fits = isinstance(item, list) and all(isinstance(e, str) for e in item)
        elif kind is int:
            fits = isinstance(item, int) and not isinstance(item, bool)
        else:
            fits = isinstance(item, kind)
        if not fits:
            out.append(f"{key} must be {_TYPE_NAMES[kind]}, not {json.dumps(item)[:60]}")
    return out


def _injectors(archive: Archive, injectors: Any, world: World) -> None:
    """The injection points a configuration registers (``injectors.injectionPoints``), each under its
    ``@AtCode`` namespace or else the configuration's ``injectors.namespace``."""
    if not isinstance(injectors, dict) or not isinstance(injectors.get("injectionPoints"), list):
        return
    default = injectors.get("namespace") if isinstance(injectors.get("namespace"), str) else ""
    if default:
        world.namespaces.add(default.upper())
    for name in injectors["injectionPoints"]:
        info = archive.read_class(classes.internal(name) + ".class") if isinstance(name, str) else None
        code = info.annotation(AT_CODE) if info is not None else None
        if code is None or not isinstance(code.get("value"), str):
            continue
        own = code.get("namespace")
        namespace = own if isinstance(own, str) and own else default
        world.codes.add(f"{namespace.upper()}:{code.get('value')}" if namespace else code.get("value"))
        if namespace:
            world.namespaces.add(namespace.upper())


def _config(archive: Archive, name: str, declared: bool, world: World) -> ConfigFacts | None:
    """One configuration's facts; None for a JSON at the root that is not a configuration."""
    facts = ConfigFacts(archive.label, name, archive.depth, declared=declared)
    data = archive.read(name, MAX_CLASS_BYTES)
    if data is None:
        if not declared:
            return None
        facts.unreadable = ("the JAR's metadata names it, but it is not packaged" if name not in archive.entries
                            else "it cannot be read")
        return facts
    facts.digest = hashlib.sha256(data).hexdigest()
    try:
        value = json.loads(data.decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError) as error:
        if not declared:
            return None
        facts.unreadable = f"not JSON: {error}"
        return facts
    if not isinstance(value, dict) or not (declared or mixins.is_config(value)):
        if not declared:
            return None
        facts.unreadable = "the JAR's metadata names it, but it is not a JSON object"
        return facts
    facts.value, facts.wrong_types = value, _wrong_types(value)
    package = facts.text("package")
    facts.package = package.replace(".", "/").strip("/") if package else ""
    facts.required = value.get("required") is True
    facts.refmap = facts.text("refmap")
    table: dict = {}
    if facts.refmap:
        if facts.refmap not in archive.entries:
            facts.refmap_problem = f"its refmap {facts.refmap} is not packaged"
        else:
            table, error = mixins.refmap(archive, facts.refmap)
            facts.refmap_problem = error
    if facts.text("plugin"):
        facts.plugin = classes.internal(facts.text("plugin"))
        info = archive.read_class(facts.plugin + ".class")
        facts.plugin_major = info.major if info is not None and info.this_class else None
    _injectors(archive, value.get("injectors"), world)
    seen: dict[str, str] = {}
    for side in mixins.SIDES:
        entries = value.get(side)
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, str) or not entry.strip():
                continue  # parse reports the wrong type
            relative = entry.strip().replace(".", "/")
            resolved = f"{facts.package}/{relative}" if facts.package else relative
            if resolved in seen:
                facts.listing.append(f"lists {_dotted(resolved)} twice in {side}" if seen[resolved] == side
                                     else f"lists {_dotted(resolved)} in both {seen[resolved]} and {side}")
                continue
            seen[resolved] = side
            if resolved + ".class" not in archive.entries:
                if facts.package and relative + ".class" in archive.entries:
                    how = (" by its full name" if relative.startswith(facts.package + "/")
                           else f", a class outside its package {_dotted(facts.package)}")
                    facts.listing.append(f"lists {entry.strip()}{how}: Mixin reads names under the package, so it "
                                         f"looks for {_dotted(resolved)}")
                else:
                    facts.listing.append(f"lists {_dotted(resolved)}, which is not in the JAR")
                continue
            info = archive.read_class(resolved + ".class")
            if info is None or not info.this_class:
                facts.listing.append(f"lists {_dotted(resolved)}, whose class file cannot be read")
                continue
            mixin = _mixin(info, side, table)
            if not mixin.mixin:
                facts.listing.append(f"lists {_dotted(resolved)}, which carries no @Mixin")
            facts.mixins.append(mixin)
    return facts


def _literals(archive: Archive) -> set[str]:
    """The string constants of the archive's classes that name a .json resource."""
    found: set[str] = set()
    for entry in archive.class_entries():
        data = archive.read(entry, MAX_CLASS_BYTES)
        if data is None or b".json" not in data:
            continue
        info = classes.read(data)
        found.update(info.utf8[i] for i in info.string_literals if info.utf8.get(i, "").endswith(".json"))
    return found


def _read(archive: Archive, world: World) -> JarFacts:
    jar = JarFacts(archive.label, archive.depth)
    world.classes |= _class_names(archive)
    named = mixins.declared(archive)
    world.declared.update(named)
    roots = sorted(n for n in archive.names if "/" not in n and n.endswith(".json"))
    for name in dict.fromkeys(named + roots):
        facts = _config(archive, name, name in named, world)
        if facts is not None:
            jar.configs.append(facts)
    listed: dict[str, str] = {}
    for facts in jar.configs:
        for mixin in facts.mixins:
            first = listed.setdefault(mixin.name, facts.name)
            if first != facts.name:
                facts.listing.append(f"lists {_dotted(mixin.name)}, which {first} lists too")
    world.literals |= _literals(archive)
    return jar


def _digest(path: Path) -> str:
    with open(path, "rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _jar_paths(roots: list[Path], host: Callable[[Path], str]) -> list[tuple[Path, str]]:
    """Each JAR at or under the roots once, with its label: its path under the folder searched, or its host
    path where two JARs would share a label."""
    found: dict[Path, str] = {}
    for root in roots:
        for jar in find_jars(root):
            found.setdefault(jar.resolve(), jar_label(jar, root))
    shared = Counter(found.values())
    return [(path, host(path) if shared[name] > 1 else name) for path, name in found.items()]


def _index_classpath(roots: list[Path], world: World, host: Callable[[Path], str]) -> None:
    """The class names of the class path: its JARs', and those of the class files in a folder given."""
    for root in roots:
        found = 0
        for jar in find_jars(root):
            entry: dict[str, Any] = {"jar": host(jar)}
            try:
                with zipfile.ZipFile(jar) as opened:
                    names = _class_names(Archive(jar.name, opened))
                entry.update(classes=len(names), sha256=_digest(jar))
                world.classes |= names
                found += len(names)
            except (zipfile.BadZipFile, OSError) as error:
                entry["error"] = f"not a readable JAR: {error}"
            world.path_jars.append(entry)
        if root.is_dir():
            names = {p.relative_to(root).with_suffix("").as_posix() for p in root.rglob("*.class") if p.is_file()}
            if names:
                world.path_jars.append({"folder": host(root), "classes": len(names)})
                world.classes |= names
                found += len(names)
        if not found:
            world.path_jars.append({"jar": host(root), "error": "no readable JAR or class file"})


# -- judging -----------------------------------------------------------------------------------------
def _registered(facts: ConfigFacts, world: World) -> str | None:
    """How a configuration is registered: by some JAR's metadata, by a string constant in a class, or not."""
    if facts.declared or facts.name in world.declared:
        return "metadata"
    return "code" if facts.name in world.literals else None


def _judge(facts: ConfigFacts, policy: Policy, world: World, problems: list[Problem]) -> dict[str, str]:
    """Problems with one configuration under the policy, added to ``problems``; returns each check's status:
    ok, warning, error, off, or skipped (it could not be made)."""
    status = {name: "off" if policy.level(name) == "off" else "ok" for name in CHECKS}
    where = f"{facts.jar}!/{facts.name}"

    def report(check: str, detail: str, severity: str | None = None) -> None:
        if policy.level(check) == "off":
            return
        level = severity or policy.level(check)
        problems.append(Problem(check, level, where, detail, (facts.jar,), facts.name))
        if _RANK[level] > _RANK.get(status[check], 0):
            status[check] = level

    if facts.unreadable:
        report("parse", facts.unreadable)
        return {name: value if name == "parse" or value == "off" else "skipped" for name, value in status.items()}
    value = facts.value or {}
    for line in facts.wrong_types:
        report("parse", line)
    missing = [key for key in policy.required_fields if value.get(key) is None]
    if missing:
        report("fields", f"does not set {', '.join(missing)}, which the policy requires")
    if not facts.package:
        report("classes", "names no package")
    for line in facts.listing:
        report("classes", line)
    found = [m for m in facts.mixins if m.mixin]

    if facts.refmap and facts.refmap_problem:
        report("refmap", facts.refmap_problem)
    elif not facts.refmap:
        needing = [m.name for m in found if any(policy.remapped(r) for r in m.remaps)]
        if needing:
            report("refmap", f"names no refmap, but its Mixins remap selectors: {_names(needing)}")

    text = facts.text("compatibilityLevel")
    level = level_of(text) if text is not None else None
    if text is not None and level is None:
        report("compatibility", f"compatibilityLevel {text!r} is not a Mixin compatibility level")
    elif level is not None and policy.levels is not None and level not in policy.levels:
        report("compatibility",
               f"compatibilityLevel JAVA_{level} is not one the loader defines ({span(policy.levels)})")
    if level is not None:
        covered = class_version(level)
        above: dict[int, list[str]] = defaultdict(list)
        for mixin in found:
            if mixin.major > covered:
                above[mixin.major].append(mixin.name)
        for major, names in sorted(above.items()):
            report("coverage", f"compatibilityLevel JAVA_{level} covers class version {covered} ({java(covered)}), "
                               f"but {_of_its(names)} class version {major} ({java(major)}): {_names(names)}")

    highest = policy.max_class_version
    if highest is not None:
        newer: dict[int, list[str]] = defaultdict(list)
        for mixin in found:
            if mixin.major > highest:
                newer[mixin.major].append(mixin.name)
        for major, names in sorted(newer.items()):
            report("class-version", f"{_of_its(names)} class version {major} ({java(major)}), above the runtime's "
                                    f"highest, {highest} ({java(highest)}): {_names(names)}")
        if facts.plugin_major is not None and facts.plugin_major > highest:
            report("class-version", f"its plugin {_dotted(facts.plugin)} is class version {facts.plugin_major} "
                                    f"({java(facts.plugin_major)}), above the runtime's highest, {highest} "
                                    f"({java(highest)})")

    wanted = facts.text("minVersion")
    if wanted is not None:
        parts, why = version(wanted)
        if why == "malformed":
            report("min-version", f"minVersion {wanted!r} is not a version Mixin reads, so it reads it as no minimum")
        elif why == "overflow":
            report("min-version", f"minVersion {wanted} has a part above {SHORT}, which Mixin refuses")
        elif policy.version_parts is not None and parts > policy.version_parts:
            report("min-version", f"minVersion {wanted} is above the loader's Mixin {policy.mixin_version}")

    if facts.plugin and facts.plugin not in world.classes:
        report("plugin", f"its plugin {_dotted(facts.plugin)} is not in the JARs"
                         + (" or on the class path" if world.classpath else ""))

    if world.classpath:
        strict = facts.required and not policy.required_may_target_missing
        for mixin in found:
            missing_targets = [t for t in mixin.targets if t not in world.classes]
            if missing_targets and not mixin.pseudo:
                report("targets", f"{_dotted(mixin.name)} targets {_names(missing_targets)}, "
                                  f"{'which is' if len(missing_targets) == 1 else 'which are'} not on the class path",
                       None if strict else "warning")
    elif status["targets"] != "off":
        status["targets"] = "skipped"

    for mixin in found:
        for point in mixin.points:
            why = world.point(point, policy)
            if why:
                report("injection-points", f"{_dotted(mixin.name)}: {why}")

    if _registered(facts, world) is None:
        report("registration", "no manifest key, loader entry or class names it, so nothing registers it")
    return status


def examine(roots: list[Path], classpath: list[Path], policy: Policy, *, nested: bool = True,
            host: Callable[[Path], str] = str) -> dict[str, Any]:
    """The full report of mixin.doctor over the JARs at or under ``roots``."""
    world = World(classpath=bool(classpath))
    _index_classpath(classpath, world, host)
    errors: list[dict] = []
    jars: list[JarFacts] = []
    for path, name in _jar_paths(roots, host):
        start = len(errors)
        for archive in archives(path, errors, nested):
            if archive.depth == 0:
                archive.label = name  # its nested JARs take their labels from it as they are opened
            jars.append(_read(archive, world))
            if archive.depth == 0:
                jars[-1].sha256, jars[-1].bytes = _digest(path), path.stat().st_size
        for error in errors[start:]:
            if error["jar"] == path.name:
                error["jar"] = name
    problems = [Problem("parse", policy.level("parse"), e["jar"], e["error"], (e["jar"],))
                for e in errors if policy.level("parse") != "off"]
    configs = [c for jar in jars for c in jar.configs]
    statuses = [_judge(c, policy, world, problems) for c in configs]
    copies: dict[str, list[int]] = defaultdict(list)
    for index, facts in enumerate(configs):
        if facts.depth == 0 and facts.unreadable is None:
            copies[facts.name].append(index)
    for name, indexes in sorted(copies.items()):
        if len(indexes) > 1 and policy.level("duplicates") != "off":
            level = policy.level("duplicates")
            owners = tuple(configs[i].jar for i in indexes)
            problems.append(Problem("duplicates", level, name, f"is in {len(owners)} JARs: {', '.join(owners)}",
                                    owners, name))
            for i in indexes:
                statuses[i]["duplicates"] = level

    def tally(jar: str | None = None, config: ConfigFacts | None = None) -> dict[str, int]:
        hits = [p for p in problems if (jar is None or jar in p.jars)
                and (config is None or (p.config == config.name and config.jar in p.jars))]
        return {"errors": sum(p.severity == "error" for p in hits),
                "warnings": sum(p.severity == "warning" for p in hits)}

    config_rows = []
    for facts, status in zip(configs, statuses, strict=True):
        config_rows.append({
            "jar": facts.jar, "config": facts.name, "required": facts.required, "registered": _registered(facts, world),
            "package": _dotted(facts.package), "compatibility_level": facts.text("compatibilityLevel"),
            "min_version": facts.text("minVersion"), "refmap": facts.refmap,
            "plugin": _dotted(facts.plugin) if facts.plugin else None, "sha256": facts.digest or None,
            "mixins": [m.as_json() for m in facts.mixins], "checks": status, **tally(config=facts)})
    jar_rows = [{"jar": j.label, "nested": j.depth > 0, "sha256": j.sha256, "bytes": j.bytes,
                 "configs": [c.name for c in j.configs], **tally(jar=j.label)} for j in jars]
    jar_rows += [{"jar": e["jar"], "nested": "!/" in e["jar"], "error": e["error"], "configs": [],
                  **tally(jar=e["jar"])} for e in errors]
    counts = {"jars": sum(1 for j in jars if j.depth == 0), "archives": len(jars), "configs": len(configs),
              "mixins": sum(1 for c in configs for m in c.mixins if m.mixin),
              "errors": sum(p.severity == "error" for p in problems),
              "warnings": sum(p.severity == "warning" for p in problems),
              "checks": dict(sorted(Counter(p.check for p in problems).items()))}
    return {"schema": "wb.mixin.doctor/1", "policy": policy.as_json(), "passed": counts["errors"] == 0,
            "counts": counts, "jars": jar_rows, "configs": config_rows,
            "classpath": {"jars": world.path_jars, "classes": sum(j.get("classes", 0) for j in world.path_jars)}
            if classpath else None,
            "problems": [p.as_json() for p in problems]}
