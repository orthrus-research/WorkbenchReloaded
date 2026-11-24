"""The publication allow-list behind ``release.check`` and every ``release.publish``: what may be published, read
from the files alone, with no network.

Rules are named under ``[modules.release.rules.<name>]``. A channel names its own (``rules = "<name>"``), and
``default`` applies when nothing names one; with neither, the built-in values below apply::

    java = 8                                    # the target Java feature version (no default: unchecked)
    production = ["examplemod-{version}.jar"]   # file-name globs of the production JARs (default: every JAR)
    extras = ["*-sources.jar", "*.pom"]         # the other files allowed beside them (default: sources and javadoc
                                                # JARs, POMs and Gradle module files)
    dev_classifiers = ["dev", "deobf"]          # classifiers that mark a JAR for development (default: dev, deobf,
                                                # sources, javadoc)
    dependencies = ["org.example:*"]            # group:artifact[:version] globs a POM or Gradle module file may
                                                # declare; an allow-list, so an unlisted dependency is a violation
    manifest = { Implementation-Title = "examplemod" }   # attributes a production JAR's manifest must carry
                                                # ({version} and {java} are filled in)
    versions = ["name", "manifest", "mod", "pom", "module"]  # where the version is read and must agree
    licence = { name = "Apache-2.0", url = "<licence URL>" }   # the licence a POM Workbench writes declares (a
                                                # central channel needs it; distribution = "repo" may be added)

A file matching ``extras`` is an extra; otherwise one matching ``production`` must be a production JAR; any other
file is a violation, and so is a publication without a production JAR. A production JAR holds classes (not only
sources), carries no development classifier, and has no class above the target, nor do the JARs nested in it
(classes under ``META-INF/versions/<n>/`` may go up to n). Every JVM attribute declaring a target
(``X-Compile-Target-JDK`` in a manifest, ``org.gradle.jvm.version`` in a Gradle module file) names the target. The
files a Gradle module file lists are among the files, at their sizes and digests. Versions are read from production
JAR names, their manifests (``Implementation-Version``) and mod metadata (mcmod.info, fabric.mod.json,
quilt.mod.json, mods.toml), POMs and module files; they must all equal the version given, or agree with each other.
"""

from __future__ import annotations

import fnmatch
import hashlib
import io
import json
import re
import tomllib
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from xml.etree import ElementTree

from wb_core import types
from wb_core.module import EnvironmentProblem, ParamError, canonical

SOURCES = ("name", "manifest", "mod", "pom", "module")
EXTRAS = ("*-sources.jar", "*-javadoc.jar", "*.pom", "*.module", "pom-default.xml", "module.json")
FIELDS = {
    "java": {"type": "int", "optional": True, "min": 1, "max": 99},
    "production": {"type": "list", "items": "string", "default": ["*.jar"]},
    "extras": {"type": "list", "items": "string", "default": list(EXTRAS)},
    "dev_classifiers": {"type": "list", "items": "string", "default": ["dev", "deobf", "sources", "javadoc"]},
    "dependencies": {"type": "list", "items": "string", "default": []},
    "manifest": {"type": "object", "default": {}},
    "versions": {"type": "list", "items": {"type": "enum", "values": list(SOURCES)}, "default": list(SOURCES)},
    "licence": {"type": "object", "optional": True,
                "fields": {"name": {"type": "string"}, "url": {"type": "string"},
                           "distribution": {"type": "string", "optional": True}}},
}
MAGIC = b"\xca\xfe\xba\xbe"
MAX_NESTED = 64 * 2**20   # a nested JAR is read into memory to be opened; a larger one is reported, not read
MAX_METADATA = 4 * 2**20
VERSIONED = re.compile(r"^META-INF/versions/(\d+)/")
PLACEHOLDER = re.compile(r"\$\{[^}]*\}|^@[A-Za-z_.]+@$")
RUNTIME_VERSION = "${file.jarVersion}"  # filled in by the loader from the manifest, so it is not a version here
JVM_ATTRIBUTE = "org.gradle.jvm.version"
TARGET_ATTRIBUTES = ("X-Compile-Target-JDK",)
SHOWN = ("Implementation-Version", "Multi-Release", "Build-Jdk-Spec", "Created-By", "X-Compile-Target-JDK")
MOD_FILES = ("mcmod.info", "fabric.mod.json", "quilt.mod.json", "META-INF/mods.toml", "META-INF/neoforge.mods.toml")


@dataclass
class Rules:
    name: str | None
    java: int | None
    production: list[str]
    extras: list[str]
    dev_classifiers: list[str]
    dependencies: list[str]
    manifest: dict
    versions: list[str]
    licence: dict | None = None

    def as_dict(self) -> dict:
        return asdict(self)

    def digest(self) -> str:
        return hashlib.sha256(canonical(self.as_dict()).encode("utf-8")).hexdigest()


def load(config: dict, name: str | None, *, given: bool, overrides: dict | None = None) -> Rules:
    """The rules named (by a parameter when ``given``, else by a channel), else ``default``, else the built-in ones;
    ``overrides`` (parameters) replace single rules."""
    named = config.get("rules") or {}
    if not isinstance(named, dict):
        raise EnvironmentProblem("modules.release.rules must be a table of rule sets")
    if name is not None and name not in named:
        message = f"no rules {name!r} in [modules.release.rules] (have: {', '.join(sorted(named)) or 'none'})"
        raise ParamError(message) if given else EnvironmentProblem(message)
    chosen = name if name is not None else ("default" if "default" in named else None)
    raw = named.get(chosen, {}) if chosen else {}
    if not isinstance(raw, dict):
        raise EnvironmentProblem(f"modules.release.rules.{chosen} must be a table")
    try:
        values = types.validate(FIELDS, raw, where="keys")
    except types.ValueError_ as error:
        raise EnvironmentProblem(f"modules.release.rules.{chosen}.{error.field}: {error.detail}") from None
    for key, value in (overrides or {}).items():
        if value is not None:
            values[key] = value
    if not all(isinstance(k, str) and isinstance(v, (str, int)) for k, v in values["manifest"].items()):
        raise EnvironmentProblem(f"modules.release.rules.{chosen}.manifest maps attribute names to values")
    values.setdefault("java", None)
    return Rules(name=chosen, **values)


@dataclass
class Item:
    """One file to check, under the name it is published by."""

    path: Path
    name: str
    sha256: str
    bytes: int


@dataclass
class Report:
    violations: list[str] = field(default_factory=list)
    files: list[dict] = field(default_factory=list)
    roles: dict[str, str] = field(default_factory=dict)
    primary: str | None = None
    version: str | None = None


@dataclass
class _Found:
    """What the files declare, gathered for the checks across files."""

    versions: list = field(default_factory=list)       # (source kind, where, value)
    dependencies: list = field(default_factory=list)   # (where, group, artifact, version)
    modules: list = field(default_factory=list)        # (module file name, its parsed content)


def inspect(items: list[Item], rules: Rules, version: str | None) -> Report:
    report, found = Report(), _Found()
    production = [_pattern(p, version) for p in rules.production]
    extras = [_pattern(p, version) for p in rules.extras]
    for item in items:
        facts = {"name": item.name, "bytes": item.bytes, "sha256": item.sha256}
        role = _role(item.name, production, extras)
        facts["role"] = report.roles[item.name] = role
        if role == "other":
            report.violations.append(f"{item.name} is neither a production JAR ({', '.join(production) or 'none'}) "
                                     f"nor an allowed extra ({', '.join(extras) or 'none'})")
        elif role == "production":
            report.primary = report.primary or item.name
            _production(item, rules, version, facts, report, found)
        lower = item.name.lower()
        if lower.endswith(".pom") or lower == "pom-default.xml":
            _pom(item, facts, report, found)
        elif lower.endswith(".module") or lower == "module.json":
            _module(item, rules, facts, report, found)
        report.files.append(facts)
    if report.primary is None:
        report.violations.append(f"no production JAR among the files (production: {', '.join(production) or 'none'})")
    _dependencies(found, rules, report)
    _module_files(found, {item.name: item for item in items}, report)
    _versions(found, rules, version, report)
    return report


# ---------------------------------------------------------------------------------------------
# one file

def _pattern(pattern: str, version: str | None) -> str:
    return pattern.replace("{version}", version or "*")


def _role(name: str, production: list[str], extras: list[str]) -> str:
    if any(fnmatch.fnmatchcase(name, p) for p in extras):
        return "extra"
    if any(fnmatch.fnmatchcase(name, p) for p in production):
        return "production"
    return "other"


def carries(text: str | None, version: str) -> bool:
    """Whether ``text`` names ``version`` as a whole version (1.2 is not in 1.2.3, nor in 11.2)."""
    return bool(text) and re.search(rf"(?<![0-9.]){re.escape(version)}(?![0-9]|\.[0-9])", text) is not None


def classifiers(stem: str, version: str | None) -> list[str]:
    """The classifier parts of a JAR's name: what follows the version (examplemod-1.0-dev -> dev)."""
    if version:
        at = stem.rfind("-" + version)
        if at >= 0:
            return [t for t in stem[at + 1 + len(version):].split("-") if t]
    tokens = stem.split("-")
    for index, token in enumerate(tokens[1:], start=1):
        if token[:1].isdigit():
            return [t for t in tokens[index + 1:] if t]
    return tokens[-1:] if len(tokens) > 1 else []


def java_level(value) -> int | None:
    """8 from 8, "8", "1.8" or "17"; None when it names no Java version."""
    text = str(value).strip()
    if text.startswith("1."):
        text = text[2:]
    return int(text) if text.isdigit() else None


def _production(item: Item, rules: Rules, version: str | None, facts: dict, report: Report, found: _Found):
    stem = item.name[:-4] if item.name.lower().endswith(".jar") else item.name
    dev = {c.lower() for c in rules.dev_classifiers}
    marked = [c for c in classifiers(stem, version) if c.lower() in dev]
    if marked:
        report.violations.append(f"{item.name} is a development JAR (classifier {marked[0]}), not a production JAR")
    if version and "name" in rules.versions and not carries(stem, version):
        report.violations.append(f"{item.name}: the file name does not carry version {version}")
    try:
        archive = zipfile.ZipFile(item.path)
    except (zipfile.BadZipFile, OSError) as error:
        report.violations.append(f"{item.name} is not a JAR: {error}")
        return
    with archive:
        attributes = manifest(archive)
        facts["manifest"] = {k: attributes[k] for k in SHOWN if k in attributes}
        scan = _scan(archive, item.name, rules.java)
        facts.update(classes=scan.classes, java=scan.java, nested=scan.nested)
        if scan.classes == 0:
            report.violations.append(
                f"{item.name} holds only sources ({scan.sources} .java files), not a production JAR" if scan.sources
                else f"{item.name} holds no classes, so it is not a production JAR")
        for label, over in scan.over.items():
            top = max(feature for _, feature in over)
            names = ", ".join(name for name, _ in over[:3]) + (f" and {len(over) - 3} more" if len(over) > 3 else "")
            report.violations.append(f"{label}: {len(over)} class(es) need Java {top} (class version {top + 44}), "
                                     f"above the target Java {rules.java}: {names}")
        report.violations += [f"{name} is not a class file" for name in scan.broken]
        _manifest_rules(item, attributes, rules, version, report)
        if attributes.get("Implementation-Version"):
            found.versions.append(("manifest", f"{item.name} manifest Implementation-Version",
                                   attributes["Implementation-Version"]))
        found.versions += [("mod", where, value) for where, value in _mod_versions(archive, item.name, report)]


def manifest(archive: zipfile.ZipFile) -> dict[str, str]:
    """The main attributes of a JAR's manifest (continuation lines joined)."""
    try:
        raw = archive.read("META-INF/MANIFEST.MF")
    except (KeyError, zipfile.BadZipFile, OSError, EOFError):
        return {}
    attributes: dict[str, str] = {}
    key = None
    for line in raw.decode("utf-8", "replace").splitlines():
        if not line.strip():
            break  # the main section ends at the first empty line
        if line.startswith(" ") and key:
            attributes[key] += line[1:]
            continue
        name, sep, value = line.partition(":")
        if sep:
            key = name.strip()
            attributes[key] = value.strip()
    return attributes


@dataclass
class _Scan:
    classes: int = 0
    sources: int = 0
    java: int | None = None
    over: dict = field(default_factory=dict)     # label -> [(entry, Java version)] above the target
    broken: list = field(default_factory=list)
    nested: list = field(default_factory=list)


def _scan(archive: zipfile.ZipFile, label: str, java: int | None, depth: int = 0) -> _Scan:
    scan = _Scan()
    for info in archive.infolist():
        name = info.filename
        if info.is_dir():
            continue
        if name.endswith(".java"):
            scan.sources += 1
        elif name.endswith(".class"):
            try:
                with archive.open(info) as handle:
                    head = handle.read(8)
            except (zipfile.BadZipFile, OSError, EOFError, RuntimeError):
                head = b""
            if len(head) < 8 or head[:4] != MAGIC:
                scan.broken.append(f"{label}!/{name}")
                continue
            feature = int.from_bytes(head[6:8], "big") - 44
            versioned = VERSIONED.match(name)
            if versioned:  # a multi-release JAR's class for Java n and later
                limit = max(java, int(versioned.group(1))) if java is not None else None
            else:
                limit = java
                scan.classes += 1
                scan.java = feature if scan.java is None else max(scan.java, feature)
            if limit is not None and feature > limit:
                scan.over.setdefault(label, []).append((name, feature))
        elif name.lower().endswith(".jar") and depth == 0:
            if info.file_size > MAX_NESTED:
                scan.nested.append({"name": name, "skipped": f"larger than {MAX_NESTED >> 20} MiB"})
                continue
            try:
                inner = zipfile.ZipFile(io.BytesIO(archive.read(info)))
            except (zipfile.BadZipFile, OSError, EOFError, RuntimeError):
                scan.nested.append({"name": name, "skipped": "not a readable JAR"})
                continue
            with inner:
                nested = _scan(inner, f"{label}!/{name}", java, depth + 1)
            scan.nested.append({"name": name, "classes": nested.classes, "java": nested.java})
            for key, value in nested.over.items():
                scan.over.setdefault(key, []).extend(value)
            scan.broken += nested.broken
    return scan


def _fill(template: str, version: str | None, java: int | None) -> str | None:
    if ("{version}" in template and not version) or ("{java}" in template and java is None):
        return None  # names a value this check was not given
    return template.replace("{version}", version or "").replace("{java}", str(java))


def _manifest_rules(item: Item, attributes: dict, rules: Rules, version: str | None, report: Report):
    for attribute, expected in rules.manifest.items():
        wanted = _fill(str(expected), version, rules.java)
        if wanted is None:
            continue
        actual = attributes.get(attribute)
        if actual is None:
            report.violations.append(f"{item.name}: the manifest has no {attribute} (the rules want {wanted})")
        elif actual != wanted:
            report.violations.append(f"{item.name}: manifest {attribute} is {actual}, not {wanted}")
    if rules.java is not None:
        for attribute in TARGET_ATTRIBUTES:
            if attribute in attributes and java_level(attributes[attribute]) != rules.java:
                report.violations.append(f"{item.name}: manifest {attribute} is {attributes[attribute]}, but the "
                                         f"target is Java {rules.java}")


def _mod_versions(archive: zipfile.ZipFile, label: str, report: Report) -> list[tuple[str, str]]:
    found = []
    names = set(archive.namelist())
    for path in MOD_FILES:
        if path not in names:
            continue
        try:
            info = archive.getinfo(path)
            if info.file_size > MAX_METADATA:
                continue
            text = archive.read(info).decode("utf-8-sig", "replace")
            if path == "mcmod.info":
                value = json.loads(text)
                entries = value.get("modList") if isinstance(value, dict) else value
                for entry in entries if isinstance(entries, list) else []:
                    if isinstance(entry, dict) and "version" in entry:
                        found.append((f"{label} mcmod.info ({entry.get('modid', '?')})", str(entry["version"])))
            elif path == "fabric.mod.json":
                value = json.loads(text)
                if isinstance(value, dict) and "version" in value:
                    found.append((f"{label} fabric.mod.json", str(value["version"])))
            elif path == "quilt.mod.json":
                loader = json.loads(text).get("quilt_loader") or {}
                if "version" in loader:
                    found.append((f"{label} quilt.mod.json", str(loader["version"])))
            else:
                for mod in tomllib.loads(text).get("mods") or []:
                    if isinstance(mod, dict) and "version" in mod:
                        found.append((f"{label} {path} ({mod.get('modId', '?')})", str(mod["version"])))
        except (ValueError, AttributeError, KeyError, zipfile.BadZipFile, OSError) as error:
            report.violations.append(f"{label}: {path} cannot be read ({error})")
    return found


def read_pom(data: bytes) -> dict:
    root = ElementTree.fromstring(data)
    ns = root.tag[:root.tag.index("}") + 1] if root.tag.startswith("{") else ""
    if root.tag != f"{ns}project":
        raise ValueError(f"the root element is {root.tag}, not project")

    def text(node, path: str) -> str | None:
        found = node.find("/".join(ns + part for part in path.split("/")))
        return found.text.strip() if found is not None and found.text and found.text.strip() else None

    dependencies = [{"group": text(d, "groupId"), "artifact": text(d, "artifactId"), "version": text(d, "version"),
                     "scope": text(d, "scope") or "compile"}
                    for d in root.findall(f"{ns}dependencies/{ns}dependency")]
    return {"group": text(root, "groupId") or text(root, "parent/groupId"), "artifact": text(root, "artifactId"),
            "version": text(root, "version") or text(root, "parent/version"), "dependencies": dependencies}


def read_module(data: bytes) -> dict:
    value = json.loads(data.decode("utf-8-sig"))
    if not isinstance(value, dict) or not ("component" in value or "variants" in value):
        raise ValueError("it has no component or variants")
    component = value.get("component") or {}
    variants = []
    for variant in value.get("variants") or []:
        if not isinstance(variant, dict):
            continue
        dependencies = []
        for dependency in variant.get("dependencies") or []:
            wanted = dependency.get("version") or {}
            version = (wanted.get("strictly") or wanted.get("requires") or wanted.get("prefers")) \
                if isinstance(wanted, dict) else None
            dependencies.append({"group": dependency.get("group"), "module": dependency.get("module"),
                                 "version": version})
        files = [{k: f.get(k) for k in ("name", "url", "size", "sha256")}
                 for f in variant.get("files") or [] if isinstance(f, dict)]
        variants.append({"name": variant.get("name"), "jvm": (variant.get("attributes") or {}).get(JVM_ATTRIBUTE),
                         "dependencies": dependencies, "files": files})
    return {"group": component.get("group"), "module": component.get("module"), "version": component.get("version"),
            "variants": variants}


def _pom(item: Item, facts: dict, report: Report, found: _Found):
    try:
        pom = read_pom(item.path.read_bytes())
    except (ElementTree.ParseError, ValueError, OSError) as error:
        report.violations.append(f"{item.name} is not a readable POM: {error}")
        return
    facts["coordinates"] = f"{pom['group']}:{pom['artifact']}:{pom['version']}"
    facts["dependencies"] = [f"{d['group']}:{d['artifact']}:{d['version']} ({d['scope']})" for d in pom["dependencies"]]
    if pom["version"]:
        found.versions.append(("pom", item.name, pom["version"]))
    for d in pom["dependencies"]:
        found.dependencies.append((f"{item.name} ({d['scope']})", d["group"], d["artifact"], d["version"]))


def _module(item: Item, rules: Rules, facts: dict, report: Report, found: _Found):
    try:
        module = read_module(item.path.read_bytes())
    except (ValueError, UnicodeDecodeError, OSError) as error:
        report.violations.append(f"{item.name} is not a readable Gradle module file: {error}")
        return
    facts["component"] = f"{module['group']}:{module['module']}:{module['version']}"
    facts["variants"] = [{"name": v["name"], JVM_ATTRIBUTE: v["jvm"]} for v in module["variants"]]
    if module["version"]:
        found.versions.append(("module", f"{item.name} component", str(module["version"])))
    for variant in module["variants"]:
        jvm = variant["jvm"]
        if rules.java is not None and jvm is not None and java_level(jvm) != rules.java:
            report.violations.append(f"{item.name} variant {variant['name']}: {JVM_ATTRIBUTE} is {jvm}, but the "
                                     f"target is Java {rules.java}")
        for d in variant["dependencies"]:
            found.dependencies.append((f"{item.name} variant {variant['name']}", d["group"], d["module"], d["version"]))
    found.modules.append((item.name, module))


# ---------------------------------------------------------------------------------------------
# across files

def allowed(group, artifact, version, patterns: list[str]) -> bool:
    short = f"{group}:{artifact}"
    return any(fnmatch.fnmatchcase(short, p) or (version and fnmatch.fnmatchcase(f"{short}:{version}", p))
               for p in patterns)


def _dependencies(found: _Found, rules: Rules, report: Report):
    refused: dict[str, list[str]] = {}
    for where, group, artifact, version in found.dependencies:
        if not allowed(group, artifact, version, rules.dependencies):
            refused.setdefault(f"{group}:{artifact}" + (f":{version}" if version else ""), []).append(where)
    for coordinates, wheres in refused.items():
        report.violations.append(f"{', '.join(dict.fromkeys(wheres))} declares {coordinates}, which is not on the "
                                 f"dependency allow-list ({', '.join(rules.dependencies) or 'empty'})")


def _module_files(found: _Found, items: dict[str, Item], report: Report):
    for module_name, module in found.modules:
        problems: dict[str, tuple[str, list[str]]] = {}
        for variant in module["variants"]:
            for entry in variant["files"]:
                name = Path(str(entry.get("url") or entry.get("name") or "")).name
                if not name:
                    continue
                item = items.get(name)
                if item is None:
                    problem = f"{module_name} lists {name}, which is not among the files"
                elif entry.get("sha256") and str(entry["sha256"]).lower() != item.sha256:
                    problem = (f"{module_name} lists {name} with SHA-256 {str(entry['sha256'])[:12]}, but the file has "
                               f"{item.sha256[:12]}: the module file is from another build")
                elif entry.get("size") is not None and int(entry["size"]) != item.bytes:
                    problem = (f"{module_name} lists {name} at {entry['size']} bytes, but the file has {item.bytes}: "
                               "the module file is from another build")
                else:
                    continue
                problems.setdefault(name, (problem, []))[1].append(str(variant["name"]))
        for problem, variants in problems.values():
            report.violations.append(f"{problem} (variants {', '.join(variants)})")


def _versions(found: _Found, rules: Rules, version: str | None, report: Report):
    checked = []
    for kind, where, value in found.versions:
        if kind not in rules.versions or value == RUNTIME_VERSION:
            continue
        if PLACEHOLDER.search(value):
            report.violations.append(f"{where} carries the unexpanded placeholder {value}")
            continue
        checked.append((where, value))
    if version:
        report.violations += [f"{where} says version {value}, not {version}" for where, value in checked
                              if value != version]
        report.version = version
        return
    distinct = list(dict.fromkeys(value for _, value in checked))
    if len(distinct) > 1:
        report.violations.append("versions disagree: " + "; ".join(f"{where} says {value}" for where, value in checked))
    report.version = distinct[0] if len(distinct) == 1 else None
