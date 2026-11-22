"""Download build and runtime inputs pinned by SHA-256, and verify them offline.

A lock file names every third-party file a project needs but does not commit (JARs, archives,
installers), where it comes from and its exact SHA-256. Nothing is accepted unless its bytes
match, so a changed upstream release or a tampered mirror fails loudly instead of building.

    orthrus-pins fetch                  # download what is missing or wrong, verify everything
    orthrus-pins verify                 # offline: exit 1 if any pinned file is missing or differs
    orthrus-pins add NAME URL           # download once and record its hash (check it upstream!)
    orthrus-pins hash FILE [FILE ...]   # print SHA-256 digests

The lock file (default ``pins.lock.json`` at the project root; ``--lock`` or the ``lock``
setting of ``[pins]`` in ``orthrus.toml`` override it)::

    {"pins": [{"name": "Demonica-0.9.0.jar",
               "url": "https://github.com/.../Demonica-0.9.0.jar",
               "sha256": "2416...",
               "path": "build-inputs/Demonica-0.9.0.jar"}]}

``path`` is relative to the project root and defaults to ``<dest>/<name>``, where ``dest``
(``--dest`` or the ``dest`` setting) defaults to ``.local/pins``. Downloads go to a ``.part``
file first and replace the target only after the hash matches.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from . import __version__, cli
from .cli import ENVIRONMENT, FINDING, OK, USAGE, Output, ToolError
from .config import project_root, tool_settings

DEFAULT_LOCK = "pins.lock.json"
DEFAULT_DEST = ".local/pins"
USER_AGENT = f"orthrus-pins/{__version__}"
CHUNK = 1 << 20


@dataclass(frozen=True)
class Pin:
    name: str
    url: str
    sha256: str
    path: str | None = None

    @staticmethod
    def from_dict(data: dict[str, Any]) -> Pin:
        pin = Pin(
            str(data["name"]), str(data["url"]), str(data["sha256"]).lower(), data.get("path")
        )
        if len(pin.sha256) != 64 or any(c not in "0123456789abcdef" for c in pin.sha256):
            raise ValueError(f"{pin.name}: sha256 must be 64 hexadecimal characters")
        if not pin.url.startswith("https://"):
            raise ValueError(f"{pin.name}: only https:// URLs are accepted")
        if pin.path is not None:
            pure = PurePosixPath(pin.path)
            if pure.is_absolute() or ".." in pure.parts or ":" in pin.path:
                raise ValueError(f"{pin.name}: path must be relative to the project root")
        return pin

    def target(self, root: Path, dest: str) -> Path:
        return root / (self.path or f"{dest}/{self.name}")

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"name": self.name, "url": self.url, "sha256": self.sha256}
        if self.path:
            data["path"] = self.path
        return data


CACHE_ENV = "WB_PIN_CACHE"


def cache_folder() -> Path | None:
    """A folder of files named by their SHA-256 that every project on the machine shares (``WB_PIN_CACHE``), so a
    pinned file is downloaded or built once; None when unset."""
    value = os.environ.get(CACHE_ENV)
    return Path(value) if value else None


def from_cache(digest: str, target: Path) -> bool:
    """Copies the cached file with this digest to ``target``; False when the cache has none (or a damaged one)."""
    folder = cache_folder()
    stored = folder / digest if folder else None
    if stored is None or not stored.is_file() or sha256(stored) != digest:
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".part")
    shutil.copyfile(stored, partial)
    partial.replace(target)
    return True


def to_cache(path: Path, digest: str) -> None:
    """Keeps a verified file in the cache under its digest (atomically; an existing entry stays)."""
    folder = cache_folder()
    if folder is None:
        return
    folder.mkdir(parents=True, exist_ok=True)
    stored = folder / digest
    if stored.is_file():
        return
    partial = folder / f".{digest}.{os.getpid()}.part"
    shutil.copyfile(path, partial)
    partial.replace(stored)


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def load_lock(path: Path) -> list[Pin]:
    data = json.loads(path.read_text(encoding="utf-8"))
    pins = [Pin.from_dict(entry) for entry in data.get("pins", [])]
    names = [pin.name for pin in pins]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ValueError("duplicate pin names: " + ", ".join(duplicates))
    return pins


def write_lock(path: Path, pins: list[Pin]) -> None:
    document = {"pins": [pin.to_dict() for pin in sorted(pins, key=lambda p: p.name.lower())]}
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8", newline="\n")


def download(url: str, target: Path, expected: str | None, attempts: int = 3) -> str:
    """Download to ``target`` via a ``.part`` file; returns the digest. Refuses a mismatch."""
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".part")
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            digest = hashlib.sha256()
            with (
                urllib.request.urlopen(request, timeout=120) as response,
                partial.open("wb") as out,
            ):
                while chunk := response.read(CHUNK):
                    digest.update(chunk)
                    out.write(chunk)
            actual = digest.hexdigest()
            if expected is not None and actual != expected:
                partial.unlink(missing_ok=True)
                raise ToolError(
                    FINDING, f"{target.name}: SHA-256 {actual} does not match the pinned {expected}"
                )
            partial.replace(target)
            return actual
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            last_error = error
            partial.unlink(missing_ok=True)
            if attempt + 1 < attempts:
                time.sleep(2**attempt)
    raise ToolError(
        ENVIRONMENT, f"{target.name}: download failed after {attempts} attempts: {last_error}"
    )


def status(pin: Pin, root: Path, dest: str) -> str:
    target = pin.target(root, dest)
    if not target.is_file():
        return "missing"
    return "present" if sha256(target) == pin.sha256 else "differs"


def _resolve(args: argparse.Namespace) -> tuple[Path, Path, str]:
    root = (args.root or project_root()).resolve()
    settings = tool_settings(root, "pins")
    lock = root / (args.lock or settings.get("lock") or DEFAULT_LOCK)
    dest = args.dest or settings.get("dest") or DEFAULT_DEST
    return root, lock, dest


def build_parser() -> cli.Parser:
    parser = cli.parser(
        "orthrus-pins", __doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--root", type=Path, metavar="DIR", help="project root (default: the Git work tree)"
    )
    parser.add_argument("--lock", metavar="FILE", help="lock file path relative to the root")
    parser.add_argument("--dest", metavar="DIR", help="default directory for pins without a path")
    commands = cli.subcommands(parser)
    fetch = commands.add_parser("fetch", help="download missing or differing pins")
    fetch.add_argument(
        "--only", action="append", default=[], metavar="NAME", help="only this pin name"
    )
    commands.add_parser("verify", help="check every pinned file offline")
    add = commands.add_parser("add", help="download a new pin and record its hash")
    add.add_argument("name")
    add.add_argument("url")
    add.add_argument("--path", help="where the file goes, relative to the root")
    hashes = commands.add_parser("hash", help="print SHA-256 digests")
    hashes.add_argument("files", type=Path, nargs="+")
    return parser


def run(args: argparse.Namespace, out: Output) -> int:
    if args.command == "hash":
        digests = []
        for file in args.files:
            if not file.is_file():
                raise ToolError(USAGE, f"no such file: {file}")
            digests.append({"file": file.as_posix(), "sha256": sha256(file)})
            out.line(f"{digests[-1]['sha256']}  {file.as_posix()}")
        out.result = {"files": digests}
        return OK

    root, lock, dest = _resolve(args)
    if args.command == "add":
        pins = load_lock(lock) if lock.is_file() else []
        if any(pin.name == args.name for pin in pins):
            raise ToolError(USAGE, f"{args.name} is already pinned in {lock.name}")
        try:
            provisional = Pin.from_dict(
                {"name": args.name, "url": args.url, "sha256": "0" * 64, "path": args.path}
            )
        except ValueError as error:
            raise ToolError(USAGE, str(error)) from None
        digest = download(args.url, provisional.target(root, dest), None)
        pins.append(Pin(args.name, args.url, digest, args.path))
        write_lock(lock, pins)
        out.result = {"pinned": {"name": args.name, "url": args.url, "sha256": digest}}
        out.line(f"pinned   {args.name} sha256 {digest}")
        out.warn("Compare this digest with one the publisher states before trusting it.")
        return OK

    if not lock.is_file():
        raise ToolError(USAGE, f"no lock file at {lock}")
    try:
        pins = load_lock(lock)
    except (ValueError, KeyError) as error:
        raise ToolError(USAGE, f"{lock.name}: {error}") from None
    if args.command == "verify":
        states = [{"name": pin.name, "state": status(pin, root, dest)} for pin in pins]
        for entry in states:
            out.line(f"{entry['state']:8} {entry['name']}")
        out.result = {"pins": states}
        return FINDING if any(entry["state"] != "present" for entry in states) else OK

    selected = [pin for pin in pins if not args.only or pin.name in args.only]
    unknown = sorted(set(args.only) - {pin.name for pin in pins})
    if unknown:
        raise ToolError(USAGE, "not in the lock file: " + ", ".join(unknown))
    done: list[dict[str, Any]] = []
    out.result = {"pins": done}
    for pin in selected:
        state = status(pin, root, dest)
        if state == "present":
            done.append({"name": pin.name, "state": "present"})
            out.line(f"present  {pin.name}")
            continue
        target = pin.target(root, dest)
        cached = from_cache(pin.sha256, target)
        if not cached:
            download(pin.url, target, pin.sha256)
            to_cache(target, pin.sha256)
        done.append({"name": pin.name, "state": "cached" if cached else "fetched",
                     "replaced": state == "differs"})
        out.line(
            f"{'cached' if cached else 'fetched':8} {pin.name}"
            + (" (replaced a differing file)" if state == "differs" else "")
        )
    return OK


def main(argv: list[str] | None = None) -> int:
    return cli.run("pins", build_parser, run, argv)


if __name__ == "__main__":
    sys.exit(main())
