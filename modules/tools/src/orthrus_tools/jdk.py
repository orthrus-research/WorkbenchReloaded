"""Find an installed JDK by major version, or by exact version, on Windows, Linux or macOS.

    orthrus-jdk find 26                     # path of a java executable for Java 26
    orthrus-jdk find 26 --exact 26.0.2.1    # only that exact release
    orthrus-jdk find 25 --vendor Adoptium --home
    orthrus-jdk list                        # every JDK found, newest first

Candidates, in order: ``--java``, ``JAVA_HOME_<major>_X64`` / ``_ARM64`` (set by GitHub's
setup-java), ``JAVA_HOME``, ``java`` on ``PATH``, then the usual install locations for this
platform, IntelliJ's ``~/.jdks``, Gradle's ``~/.gradle/jdks`` and SDKMAN. A JDK's version and
vendor come from its ``release`` file, so nothing is executed unless that file is missing.
Exit status: 0 found, 2 usage error, 3 no matching JDK on this machine.
"""

from __future__ import annotations

import argparse
import os
import platform
import re
import shutil
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import cli
from .cli import ENVIRONMENT, OK, Output, ToolError

EXECUTABLE = "java.exe" if os.name == "nt" else "java"


@dataclass(frozen=True)
class Jdk:
    home: Path
    version: str
    vendor: str

    @property
    def java(self) -> Path:
        return self.home / "bin" / EXECUTABLE

    @property
    def major(self) -> int:
        first = self.version.split(".")[0].split("+")[0].split("-")[0]
        # Java 8 and earlier report "1.8.0_x"
        if first == "1" and "." in self.version:
            return int(self.version.split(".")[1])
        return int(first) if first.isdigit() else 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "home": str(self.home),
            "java": str(self.java),
            "version": self.version,
            "major": self.major,
            "vendor": self.vendor,
        }

    def sort_key(self) -> tuple[int, ...]:
        return tuple(int(part) for part in re.findall(r"\d+", self.version))


def _home_of(java: Path) -> Path:
    return java.resolve().parent.parent


def _release(home: Path) -> dict[str, str]:
    release = home / "release"
    values: dict[str, str] = {}
    if release.is_file():
        for line in release.read_text(encoding="utf-8", errors="replace").splitlines():
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"')
    return values


def inspect(home: Path) -> Jdk | None:
    """A JDK at ``home``, or ``None`` when there is no java executable there."""
    if not (home / "bin" / EXECUTABLE).is_file():
        return None
    release = _release(home)
    version = release.get("JAVA_RUNTIME_VERSION") or release.get("JAVA_VERSION", "")
    vendor = release.get("IMPLEMENTOR", "")
    if not version:
        try:
            completed = subprocess.run(
                [str(home / "bin" / EXECUTABLE), "-version"],
                capture_output=True,
                text=True,
                timeout=20,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        match = re.search(r'version "([^"]+)"', completed.stderr + completed.stdout)
        if not match:
            return None
        version = match[1]
    return Jdk(home.resolve(), version, vendor)


def _children(root: Path, suffix: str = "") -> Iterator[Path]:
    if root.is_dir():
        for child in sorted(root.iterdir(), reverse=True):
            yield child / suffix if suffix else child


def candidate_homes(major: int | None, given: Path | None = None) -> Iterator[Path]:
    if given:
        yield _home_of(given) if given.is_file() else given
    if major is not None:
        for arch in ("X64", "ARM64", "AARCH64"):
            value = os.environ.get(f"JAVA_HOME_{major}_{arch}")
            if value:
                yield Path(value)
    if os.environ.get("JAVA_HOME"):
        yield Path(os.environ["JAVA_HOME"])
    on_path = shutil.which("java")
    if on_path:
        yield _home_of(Path(on_path))
    home = Path.home()
    if os.name == "nt":
        program_roots = {
            Path(os.environ.get(name, default))
            for name, default in (
                ("ProgramFiles", r"C:\Program Files"),  # scrub: allow
                ("ProgramW6432", r"C:\Program Files"),  # scrub: allow
            )
        }
        local = os.environ.get("LOCALAPPDATA")
        if local:
            program_roots.add(Path(local) / "Programs")
        vendors = ("Eclipse Adoptium", "Java", "Microsoft", "Zulu", "Amazon Corretto", "BellSoft")
        for root in sorted(program_roots):
            for vendor in vendors:
                yield from _children(root / vendor)
        yield from _children(home / "scoop" / "apps", "current")
    elif sys.platform == "darwin":
        for root in (
            Path("/Library/Java/JavaVirtualMachines"),
            home / "Library/Java/JavaVirtualMachines",
        ):
            yield from _children(root, "Contents/Home")
    else:
        for root in (Path("/usr/lib/jvm"), Path("/usr/java"), Path("/opt/java"), Path("/opt/jdk"), Path("/opt")):
            yield from _children(root)
    yield from _children(home / ".jdks")
    yield from _children(home / ".gradle" / "jdks")
    yield from _children(home / ".sdkman" / "candidates" / "java")


def discover(major: int | None = None, given: Path | None = None) -> list[Jdk]:
    seen: set[Path] = set()
    found = []
    for home in candidate_homes(major, given):
        try:
            key = home.resolve()
        except OSError:
            continue
        if key in seen:
            continue
        seen.add(key)
        jdk = inspect(home)
        if jdk is not None:
            found.append(jdk)
    return found


def find(
    major: int, exact: str | None = None, vendor: str | None = None, given: Path | None = None
) -> Jdk | None:
    """The first matching JDK in candidate order (so an explicit choice wins over a newer one)."""
    for jdk in discover(major, given):
        if jdk.major != major:
            continue
        if exact and not (jdk.version == exact or jdk.version.startswith(exact + "+")):
            continue
        if vendor and vendor.lower() not in jdk.vendor.lower():
            continue
        return jdk
    return None


def build_parser() -> cli.Parser:
    parser = cli.parser(
        "orthrus-jdk", __doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = cli.subcommands(parser)
    f = commands.add_parser("find", help="print the java executable of a matching JDK")
    f.add_argument("major", type=int)
    f.add_argument(
        "--exact",
        metavar="VERSION",
        help="exact version, such as 26.0.2.1 (a +build suffix may follow)",
    )
    f.add_argument(
        "--vendor", metavar="NAME", help="substring of the JDK's IMPLEMENTOR, such as Adoptium"
    )
    f.add_argument(
        "--java", type=Path, metavar="PATH", help="try this java executable or JDK home first"
    )
    f.add_argument("--home", action="store_true", help="print the JDK home instead")
    commands.add_parser("list", help="list every JDK found")
    return parser


def run(args: argparse.Namespace, out: Output) -> int:
    if args.command == "list":
        jdks = sorted(discover(), key=Jdk.sort_key, reverse=True)
        out.result = {"jdks": [entry.to_dict() for entry in jdks]}
        for entry in jdks:
            out.line(f"{entry.version:24} {entry.vendor or '(unknown vendor)':28} {entry.home}")
        if not jdks:
            raise ToolError(ENVIRONMENT, f"no JDK found on this {platform.system()} machine")
        return OK
    jdk = find(args.major, args.exact, args.vendor, args.java)
    if jdk is None:
        wanted = args.exact or f"Java {args.major}"
        raise ToolError(ENVIRONMENT, f"no {wanted} JDK found; install one or pass --java PATH")
    out.result = {"jdk": jdk.to_dict()}
    out.line(str(jdk.home if args.home else jdk.java))
    return OK


def main(argv: list[str] | None = None) -> int:
    return cli.run("jdk", build_parser, run, argv)


if __name__ == "__main__":
    sys.exit(main())
