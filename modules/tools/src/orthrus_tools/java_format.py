"""Format Java sources with a pinned google-java-format, or check that they are formatted.

    orthrus-tools java-format check                # exit 1 lists files that would change
    orthrus-tools java-format apply                # rewrite them in place
    orthrus-tools java-format check src/main/java/a/B.java
    orthrus-tools java-format fetch                # download and verify the formatter only

The formatter JAR is downloaded once from Maven Central into ``.local/tools`` and checked
against its pinned SHA-256 on every run, so every machine and CI job formats alike. Settings
come from ``[java-format]`` in orthrus.toml: ``version`` and ``sha256`` (both, to change the
pin), ``paths`` (globs, default ``src/**/*.java``), ``exclude`` (globs), ``args`` (formatter
options such as ``--aosp``) and ``java`` (the JDK major version to run it with). Exit status:
0 formatted, 1 files need formatting or do not parse, 2 usage error, 3 no JDK or no network.
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import cli, jdk, pins
from .actions import match
from .cli import ENVIRONMENT, FINDING, OK, USAGE, Output, ToolError
from .config import project_root, tool_settings

VERSION = "1.35.0"
SHA256 = "bfb7f9ead6cd328389bc2da53860443bc0e805dfd08cc889bfdf43b26cb2a6e8"
URL = (
    "https://repo.maven.apache.org/maven2/com/google/googlejavaformat/google-java-format/"
    "{version}/google-java-format-{version}-all-deps.jar"
)
DEFAULT_PATHS = ["src/**/*.java"]
MIN_JAVA = 21
# Keep each command line well under Windows' 32,767 character limit.
MAX_COMMAND = 8000


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pin(settings: dict[str, Any]) -> tuple[str, str]:
    version = str(settings.get("version", VERSION))
    digest = settings.get("sha256")
    if digest is None:
        if version != VERSION:
            raise ToolError(USAGE, f"[java-format] version {version} needs its sha256 pinned too")
        digest = SHA256
    return version, str(digest).lower()


def formatter(root: Path, settings: dict[str, Any]) -> tuple[Path, str]:
    """The verified formatter JAR, downloaded when missing; and its version."""
    version, digest = pin(settings)
    name = f"google-java-format-{version}-all-deps.jar"
    path = root / ".local" / "tools" / name
    if path.is_file() and sha256(path) != digest:
        raise ToolError(
            FINDING, f"{path} does not match the pinned SHA-256; delete it to download again"
        )
    if not path.is_file():
        url = str(settings.get("url", URL)).format(version=version)
        pins.download(url, path, digest)
    return path, version


def java_executable(given: Path | None, settings: dict[str, Any]) -> Path:
    if given is not None:
        found = jdk.inspect(given if given.is_dir() else given.parent.parent)
        if found is None or not found.java.is_file():
            raise ToolError(USAGE, f"no JDK at {given}")
        return found.java
    major = settings.get("java")
    if major is not None:
        chosen = jdk.find(int(major))
        if chosen is None:
            raise ToolError(ENVIRONMENT, f"no Java {major} JDK found ([java-format] java)")
        return chosen.java
    candidates = [j for j in jdk.discover() if j.major >= MIN_JAVA]
    if not candidates:
        raise ToolError(
            ENVIRONMENT, f"the formatter needs Java {MIN_JAVA} or newer; install one or pass --java"
        )
    return max(candidates, key=jdk.Jdk.sort_key).java


def sources(root: Path, given: list[Path], settings: dict[str, Any]) -> list[Path]:
    """Java files from the arguments (files or folders), else from the ``paths`` globs."""
    found: set[Path] = set()
    if given:
        for item in given:
            path = item if item.is_absolute() else Path.cwd() / item
            if path.is_dir():
                found.update(path.rglob("*.java"))
            elif path.is_file():
                found.add(path)
            else:
                raise ToolError(USAGE, f"no such file or folder: {item}")
    else:
        for pattern in settings.get("paths", DEFAULT_PATHS):
            found.update(p for p in root.glob(str(pattern)) if p.is_file())
    excluded = [str(p) for p in settings.get("exclude", [])]
    kept = []
    for path in sorted(p.resolve() for p in found):
        try:
            name = path.relative_to(root).as_posix()
        except ValueError:
            name = path.as_posix()
        if not any(match(pattern, name) for pattern in excluded):
            kept.append(path)
    return kept


def chunks(items: list[str], limit: int = MAX_COMMAND) -> list[list[str]]:
    groups: list[list[str]] = [[]]
    size = 0
    for item in items:
        if groups[-1] and size + len(item) + 1 > limit:
            groups.append([])
            size = 0
        groups[-1].append(item)
        size += len(item) + 1
    return [group for group in groups if group]


def relative(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def format_files(
    java: Path, jar: Path, options: list[str], files: list[Path], root: Path, apply: bool
) -> tuple[list[str], list[str]]:
    """(files that change or would change, error lines) for one run of the formatter."""
    mode = ["--replace"] if apply else ["--dry-run", "--set-exit-if-changed"]
    names = [relative(root, path) for path in files]
    before = {name: sha256(root / name) for name in names} if apply else {}
    changed: list[str] = []
    errors: list[str] = []
    for group in chunks(names):
        command = [str(java), "-jar", str(jar), *options, *mode, *group]
        try:
            completed = subprocess.run(command, cwd=root, capture_output=True, text=True)
        except OSError as error:
            raise ToolError(ENVIRONMENT, f"cannot run {java}: {error}") from None
        if not apply:
            changed += [Path(line.strip()).as_posix() for line in completed.stdout.splitlines()]
        errors += [line for line in completed.stderr.splitlines() if line.strip()]
        if completed.returncode not in (0, 1):
            errors.append(f"the formatter exited with code {completed.returncode}")
    if apply:
        changed = [name for name in names if sha256(root / name) != before[name]]
    return [c for c in changed if c], errors


def build_parser() -> cli.Parser:
    parser = cli.parser(
        "orthrus-java-format",
        (__doc__ or "").splitlines()[0],
        epilog="Settings: docs/java-format.md.",
    )
    commands = cli.subcommands(parser)
    common = cli.Parser(add_help=False)
    common.add_argument(
        "--root", type=Path, metavar="DIR", help="project root (default: the Git work tree)"
    )
    common.add_argument(
        "--java", type=Path, metavar="PATH", help="the java executable or JDK home to run it with"
    )
    for name, text in (("check", "list files that need formatting"), ("apply", "format them")):
        sub = commands.add_parser(name, help=text, parents=[common])
        sub.add_argument(
            "paths", nargs="*", type=Path, metavar="PATH", help="files or folders (default: paths)"
        )
    commands.add_parser("fetch", help="download and verify the formatter", parents=[common])
    return parser


def run(args: argparse.Namespace, out: Output) -> int:
    root = (args.root or project_root()).resolve()
    if not root.is_dir():
        raise ToolError(USAGE, f"not a directory: {root}")
    settings = tool_settings(root, "java-format")
    jar, version = formatter(root, settings)
    info = {"version": version, "jar": relative(root, jar)}
    if args.command == "fetch":
        out.result = {"formatter": info}
        out.line(f"google-java-format {version} verified at {relative(root, jar)}")
        return OK
    files = sources(root, args.paths, settings)
    if not files:
        raise ToolError(USAGE, "no Java files to format (see [java-format] paths)")
    java = java_executable(args.java, settings)
    options = [str(option) for option in settings.get("args", [])]
    apply = args.command == "apply"
    changed, errors = format_files(java, jar, options, files, root, apply)
    out.result = {
        "formatter": info,
        "files": len(files),
        "changed": changed,
        "errors": errors,
    }
    verb = "formatted" if apply else "needs formatting"
    for name in changed:
        out.line(f"{verb}: {name}")
    for line in errors:
        out.error(line)
    out.line(
        f"{len(files)} files, {len(changed)} {'changed' if apply else 'to format'}"
        + (f", {len(errors)} error lines" if errors else "")
    )
    if errors or (changed and not apply):
        if changed and not apply:
            out.note("run: orthrus-tools java-format apply")
        return FINDING
    return OK


def main(argv: list[str] | None = None) -> int:
    return cli.run("java-format", build_parser, run, argv)


if __name__ == "__main__":
    sys.exit(main())
