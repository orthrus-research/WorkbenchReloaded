"""Check every class in JARs for its class file version, names and forbidden references.

    orthrus-tools jar-audit build/libs/app.jar --release 21
    orthrus-tools jar-audit core.jar --release 26 --deny net.minecraft --deny org.lwjgl
    orthrus-tools jar-audit                       # the [jar-audit] jars in orthrus.toml

Nothing is loaded or run: each class is read with ``orthrus_tools.classfile``. A finding is a
class whose version is not the release's (``META-INF/versions/N/`` entries must be N's), a
preview class (unless ``--allow-preview``), a malformed or truncated class, an entry whose
path disagrees with the class it declares, a duplicate entry, an archive over the size limits,
or a structural reference to a denied package: in the constant pool's classes or in any
descriptor or generic signature. String literals alone are not references; they are listed
separately when they name a denied package. Exit status: 0 clean, 1 findings, 2 usage error.
"""

from __future__ import annotations

import argparse
import re
import sys
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any

from . import classfile, cli
from .cli import FINDING, OK, USAGE, Output, ToolError
from .config import project_root, tool_settings

MAX_ENTRIES = 100_000
MAX_CLASS_BYTES = 16 * 2**20
MAX_EXPANDED_BYTES = 1024 * 2**20
PREVIEW_MINOR = 0xFFFF
VERSIONED = re.compile(r"^META-INF/versions/(\d+)/(.+)$")
# A reference type inside a descriptor or signature: L<internal name> then ; or <
REFERENCE = re.compile(r"L([A-Za-z_$][\w$]*(?:/[\w$]+)*)[;<]")


def internal(package: str) -> str:
    return package.strip().strip("./").replace(".", "/")


def denied(name: str, prefixes: list[str]) -> str | None:
    """The denied package (as given, internal form) that name belongs to, if any."""
    for prefix in prefixes:
        if name == prefix or name.startswith(prefix + "/"):
            return prefix
    return None


def references(parsed: classfile.ClassFile) -> tuple[set[str], set[str]]:
    """(structural references, names only string literals mention), as internal names."""
    structural: set[str] = set()
    for name in parsed.class_names:
        if name.startswith("["):  # an array class: its element type is a descriptor
            structural.update(REFERENCE.findall(name))
        elif name:
            structural.add(name)
    literals: set[str] = set()
    for index, text in parsed.utf8.items():
        found = set(REFERENCE.findall(text))
        if index in parsed.string_literals:
            literals.update(found)
            literals.add(text.replace(".", "/"))
        else:
            structural.update(found)
    return structural, literals - structural


def audit_jar(
    path: Path,
    release: int | None,
    deny: list[str],
    allow_preview: bool = False,
) -> dict[str, Any]:
    """Findings for one JAR: a list of {entry, kind, detail}, plus what was counted."""
    findings: list[dict[str, str]] = []
    versions: Counter[str] = Counter()
    mentions: set[str] = set()
    result: dict[str, Any] = {"path": str(path), "classes": 0, "findings": findings}

    def finding(entry: str, kind: str, detail: str) -> None:
        findings.append({"entry": entry, "kind": kind, "detail": detail})

    try:
        archive = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as error:
        finding("", "unreadable", str(error))
        return result
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_ENTRIES:
            finding("", "limit", f"{len(infos)} entries, more than {MAX_ENTRIES}")
            return result
        names = Counter(info.filename for info in infos)
        for name, count in sorted(names.items()):
            if count > 1:
                finding(name, "duplicate", f"{count} entries with this name")
        expanded = 0
        for info in infos:
            name = info.filename
            if not name.endswith(".class") or info.is_dir():
                continue
            if info.file_size > MAX_CLASS_BYTES:
                finding(name, "limit", f"{info.file_size} bytes, more than {MAX_CLASS_BYTES}")
                continue
            expanded += info.file_size
            if expanded > MAX_EXPANDED_BYTES:
                finding(name, "limit", f"classes expand past {MAX_EXPANDED_BYTES} bytes")
                break
            try:
                data = archive.read(info)
            except (zipfile.BadZipFile, OSError, ValueError) as error:
                finding(name, "unreadable", str(error))
                continue
            result["classes"] += 1
            parsed = classfile.parse(data)
            if parsed.major == 0:
                finding(name, "malformed", "not a class file")
                continue
            versions[f"{parsed.major}.{parsed.minor}"] += 1
            versioned = VERSIONED.match(name)
            path_name = versioned.group(2) if versioned else name
            if release is not None:
                expected = int(versioned.group(1)) + 44 if versioned else release + 44
                if parsed.major != expected:
                    finding(
                        name,
                        "version",
                        f"class file {parsed.major}.{parsed.minor}, expected {expected} "
                        f"(Java {expected - 44})",
                    )
            if parsed.minor == PREVIEW_MINOR and not allow_preview:
                finding(name, "preview", f"compiled with preview features ({parsed.major}.65535)")
            elif parsed.minor not in (0, PREVIEW_MINOR) and parsed.major >= 56:
                finding(name, "version", f"unexpected minor version {parsed.minor}")
            if not parsed.complete:
                finding(name, "malformed", "truncated or malformed class file")
                continue
            if (
                path_name != "module-info.class"
                and path_name[: -len(".class")] != parsed.this_class
            ):
                finding(name, "name", f"declares {parsed.this_class or 'no name'}")
            structural, literals = references(parsed)
            for reference in sorted(structural):
                package = denied(reference, deny)
                if package and not denied(parsed.this_class, [package]):
                    finding(name, "reference", f"refers to {reference.replace('/', '.')}")
            mentions.update(m for m in literals if denied(m, deny))
    result["versions"] = dict(sorted(versions.items()))
    result["stringMentions"] = sorted(m.replace("/", ".") for m in mentions)
    return result


def build_parser() -> cli.Parser:
    parser = cli.parser("orthrus-jar-audit", (__doc__ or "").splitlines()[0])
    parser.add_argument(
        "jars",
        nargs="*",
        type=Path,
        metavar="JAR",
        help="JARs to audit (default: [jar-audit] jars)",
    )
    parser.add_argument(
        "--release", type=int, metavar="N", help="the Java release every class must target"
    )
    parser.add_argument(
        "--deny",
        action="append",
        default=[],
        metavar="PACKAGE",
        help="a package no class may refer to, such as net.minecraft",
    )
    parser.add_argument("--allow-preview", action="store_true", help="accept preview classes")
    parser.add_argument(
        "--root", type=Path, metavar="DIR", help="project root (default: the Git work tree)"
    )
    return parser


def run(args: argparse.Namespace, out: Output) -> int:
    root = (args.root or project_root()).resolve()
    settings = tool_settings(root, "jar-audit")
    jars: list[Path] = [p if p.is_absolute() else Path.cwd() / p for p in args.jars]
    if not jars:
        for pattern in settings.get("jars", []):
            jars += sorted(p for p in root.glob(str(pattern)) if p.is_file())
        if not jars:
            raise ToolError(USAGE, "name the JARs, or list them as [jar-audit] jars")
    missing = [str(p) for p in jars if not p.is_file()]
    if missing:
        raise ToolError(USAGE, "no such file: " + ", ".join(missing))
    release = args.release if args.release is not None else settings.get("release")
    if release is not None and (not isinstance(release, int) or release < 1):
        raise ToolError(USAGE, f"--release must be a Java release number, not {release!r}")
    deny = [internal(p) for p in (args.deny or settings.get("deny", [])) if internal(p)]
    allow_preview = args.allow_preview or bool(settings.get("allow_preview", False))
    reports = [audit_jar(path, release, deny, allow_preview) for path in jars]
    total = sum(len(report["findings"]) for report in reports)
    out.result = {
        "release": release,
        "deny": [p.replace("/", ".") for p in deny],
        "jars": reports,
        "findings": total,
    }
    for report in reports:
        name = Path(report["path"]).name
        for entry in report["findings"]:
            where = f"{name}!{entry['entry']}" if entry["entry"] else name
            out.line(f"{entry['kind']:10} {where}: {entry['detail']}")
        versions = ", ".join(f"{v} x{n}" for v, n in report.get("versions", {}).items())
        out.line(
            f"{'FAIL' if report['findings'] else 'ok'}   {name}: {report['classes']} classes"
            + (f" ({versions})" if versions else "")
            + (f", {len(report['findings'])} findings" if report["findings"] else "")
        )
        if report.get("stringMentions"):
            out.note(
                f"{name}: string literals name denied classes (not references): "
                + ", ".join(report["stringMentions"][:10])
            )
    return FINDING if total else OK


def main(argv: list[str] | None = None) -> int:
    return cli.run("jar-audit", build_parser, run, argv)


if __name__ == "__main__":
    sys.exit(main())
