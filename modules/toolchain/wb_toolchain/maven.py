"""The Maven repository layout the rescue repository keeps: coordinates and their paths, minimal POMs, plugin markers,
``maven-metadata.xml`` and the digest files beside each file, written as Gradle and Maven read them.

Coordinates are ``group:artifact:version[:classifier][@ext]`` (``ext`` is ``jar`` unless given), and live at
``<group as folders>/<artifact>/<version>/<artifact>-<version>[-<classifier>].<ext>``; the version's POM is
``<artifact>-<version>.pom`` there. A Gradle plugin's marker is the POM-only ``<id>:<id>.gradle.plugin:<version>``,
which depends on the JAR implementing the plugin: it is how ``plugins { id '<id>' version '<version>' }`` finds it.
Versions are ordered the way Gradle orders them.
"""

from __future__ import annotations

import hashlib
import re
import time
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from xml.sax.saxutils import escape

GROUP = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]*(?:\.[A-Za-z0-9_][A-Za-z0-9_-]*)*")
NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.+-]*")
EXT = re.compile(r"[A-Za-z0-9]+(?:\.[A-Za-z0-9]+)*")
MARKER = ".gradle.plugin"
POM_NAMESPACE = "http://maven.apache.org/POM/4.0.0"


class CoordinatesError(ValueError):
    pass


@dataclass(frozen=True, order=True)
class Coordinates:
    group: str
    artifact: str
    version: str
    classifier: str = ""
    ext: str = "jar"

    def __str__(self) -> str:
        text = f"{self.group}:{self.artifact}:{self.version}"
        if self.classifier:
            text += f":{self.classifier}"
        return text + (f"@{self.ext}" if self.ext != "jar" else "")

    @property
    def module(self) -> str:
        """``group:artifact:version``, the component a build asks for."""
        return f"{self.group}:{self.artifact}:{self.version}"

    @property
    def folder(self) -> str:
        """The version's folder, relative to the repository."""
        return f"{self.group.replace('.', '/')}/{self.artifact}/{self.version}"

    @property
    def path(self) -> str:
        """The file's path, relative to the repository."""
        suffix = f"-{self.classifier}" if self.classifier else ""
        return f"{self.folder}/{self.artifact}-{self.version}{suffix}.{self.ext}"

    @property
    def is_pom(self) -> bool:
        return self.ext == "pom" and not self.classifier

    @property
    def pom(self) -> "Coordinates":
        """The coordinates of this version's POM."""
        return Coordinates(self.group, self.artifact, self.version, "", "pom")

    @property
    def plugin(self) -> str | None:
        """The plugin id when these are a plugin marker's coordinates."""
        return self.group if self.artifact == self.group + MARKER else None


def parse(text: str, *, plain: bool = False) -> Coordinates:
    """``group:artifact:version[:classifier][@ext]``; ``plain``: only ``group:artifact:version``."""
    value = str(text).strip()
    body, at, ext = value.partition("@")
    parts = body.split(":")
    form = "group:artifact:version" if plain else "group:artifact:version[:classifier][@ext]"
    if len(parts) not in ((3,) if plain else (3, 4)) or (plain and at):
        raise CoordinatesError(f"{value!r} is not {form}")
    group, artifact, version = parts[:3]
    classifier = parts[3] if len(parts) == 4 else ""
    if not GROUP.fullmatch(group):
        raise CoordinatesError(f"{value!r}: the group {group!r} is not dot-separated names")
    for what, part in (("artifact", artifact), ("version", version), ("classifier", classifier)):
        if (part or what != "classifier") and (not NAME.fullmatch(part) or ".." in part):
            raise CoordinatesError(f"{value!r}: the {what} {part!r} is not a name (letters, digits and . _ + -)")
    if at and not EXT.fullmatch(ext):
        raise CoordinatesError(f"{value!r}: the extension {ext!r} is not a file extension")
    if ext == "pom" and classifier:
        raise CoordinatesError(f"{value!r}: a POM belongs to the version, not to a classifier")
    return Coordinates(group, artifact, version, classifier, ext or "jar")


def plugin_id(text: str) -> str:
    value = str(text).strip()
    if not GROUP.fullmatch(value):
        raise CoordinatesError(f"{value!r} is not a plugin id (dot-separated names, such as com.example.plugin)")
    return value


def marker(plugin: str, version: str) -> Coordinates:
    """The marker of plugin ``plugin`` at ``version``."""
    return Coordinates(plugin, plugin + MARKER, version, "", "pom")


# ---------------------------------------------------------------------------------------------
# version order

SPECIAL = {"dev": -1, "rc": 1, "snapshot": 2, "final": 3, "ga": 4, "release": 5, "sp": 6}


def _parts(version: str) -> list[str]:
    return [p for piece in re.split(r"[._+-]", version) for p in re.findall(r"\d+|\D+", piece)]


def compare(left: str, right: str) -> int:
    """Gradle's version order: numeric parts by value and above words; ``dev`` below every word; ``rc``, ``snapshot``,
    ``final``, ``ga``, ``release`` and ``sp`` above the rest, in that order; one more numeric part is newer
    (1.1 < 1.1.0), one more word older (1.1-rc < 1.1)."""
    a, b = _parts(left), _parts(right)
    for x, y in zip(a, b):
        if x == y:
            continue
        if x.isdigit() and y.isdigit():
            if int(x) != int(y):
                return -1 if int(x) < int(y) else 1
            continue
        if x.isdigit() != y.isdigit():
            return 1 if x.isdigit() else -1
        sx, sy = SPECIAL.get(x.lower()), SPECIAL.get(y.lower())
        if sx is not None or sy is not None:
            difference = (sx or 0) - (sy or 0)
            if difference:
                return -1 if difference < 0 else 1
            continue
        return -1 if x < y else 1
    if len(a) == len(b):
        return 0
    longer, sign = (a, 1) if len(a) > len(b) else (b, -1)
    return sign if longer[min(len(a), len(b))].isdigit() else -sign


def ordered(versions) -> list[str]:
    import functools
    return sorted(set(versions), key=functools.cmp_to_key(compare))


# ---------------------------------------------------------------------------------------------
# the files

def digest_text(digest: str) -> bytes:
    """A ``.sha256`` or ``.sha1`` file as Maven repositories publish them: the hex digest alone."""
    return digest.encode("ascii")


def digests(data: bytes) -> tuple[str, str]:
    return hashlib.sha256(data).hexdigest(), hashlib.sha1(data).hexdigest()


def _dependency(dependency: Coordinates) -> list[str]:
    lines = ["    <dependency>", f"      <groupId>{escape(dependency.group)}</groupId>",
             f"      <artifactId>{escape(dependency.artifact)}</artifactId>",
             f"      <version>{escape(dependency.version)}</version>"]
    if dependency.classifier:
        lines.append(f"      <classifier>{escape(dependency.classifier)}</classifier>")
    if dependency.ext != "jar":
        lines.append(f"      <type>{escape(dependency.ext)}</type>")
    return lines + ["    </dependency>"]


def pom(coordinates: Coordinates, packaging: str, dependencies: list[Coordinates], note: str) -> bytes:
    """A minimal POM: the coordinates, the packaging (``jar``, or ``pom`` for a marker) and compile dependencies."""
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             f'<project xmlns="{POM_NAMESPACE}" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
             f'xsi:schemaLocation="{POM_NAMESPACE} https://maven.apache.org/xsd/maven-4.0.0.xsd">',
             f"  <!-- {escape(note).replace('--', '- -')} -->",
             "  <modelVersion>4.0.0</modelVersion>",
             f"  <groupId>{escape(coordinates.group)}</groupId>",
             f"  <artifactId>{escape(coordinates.artifact)}</artifactId>",
             f"  <version>{escape(coordinates.version)}</version>",
             f"  <packaging>{escape(packaging)}</packaging>"]
    if dependencies:
        lines.append("  <dependencies>")
        for dependency in dependencies:
            lines += _dependency(dependency)
        lines.append("  </dependencies>")
    return ("\n".join(lines + ["</project>"]) + "\n").encode("utf-8")


def pom_coordinates(data: bytes) -> tuple[str | None, str | None, str | None]:
    """The group, artifact and version a POM declares (group and version may come from its parent)."""
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError as error:
        raise CoordinatesError(f"not a POM: {error}") from None
    namespace = root.tag[:root.tag.index("}") + 1] if root.tag.startswith("{") else ""
    if root.tag != f"{namespace}project":
        raise CoordinatesError(f"not a POM: the root element is <{root.tag.removeprefix(namespace)}>")

    def text(*path: str) -> str | None:
        node = root.find("/".join(namespace + p for p in path))
        return node.text.strip() if node is not None and node.text else None
    return (text("groupId") or text("parent", "groupId"), text("artifactId"),
            text("version") or text("parent", "version"))


def metadata(group: str, artifact: str, versions, updated: float) -> bytes:
    """``maven-metadata.xml`` for an artifact: its versions in Gradle's order, the newest, the newest release."""
    listed = ordered(versions)
    releases = [v for v in listed if not v.endswith("-SNAPSHOT")]
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', "<metadata>", f"  <groupId>{escape(group)}</groupId>",
             f"  <artifactId>{escape(artifact)}</artifactId>", "  <versioning>"]
    if listed:
        lines.append(f"    <latest>{escape(listed[-1])}</latest>")
    if releases:
        lines.append(f"    <release>{escape(releases[-1])}</release>")
    lines.append("    <versions>")
    lines += [f"      <version>{escape(v)}</version>" for v in listed]
    lines += ["    </versions>", f"    <lastUpdated>{time.strftime('%Y%m%d%H%M%S', time.gmtime(updated))}</lastUpdated>",
              "  </versioning>", "</metadata>"]
    return ("\n".join(lines) + "\n").encode("utf-8")


def metadata_path(group: str, artifact: str) -> str:
    return f"{group.replace('.', '/')}/{artifact}/maven-metadata.xml"
