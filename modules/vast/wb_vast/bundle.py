"""Bundle manifests (spec 10.1): what a rental uploads, needs, runs and brings back, instead of a hard-coded
list of tars. ``vast.bundle-launch`` writes them from a launch file (``wb_vast.launch_bundle``); one may also be
written by hand::

    name = "turn-02"
    purpose = "measurement"
    [needs]
    jdk = ["26"]                       # the JDKs the bootstrap installs (none given: 26)
    gpu = true
    own_jdk = false                    # true: the bundle carries its JDK (a launch bundle), the bootstrap installs none
    [[upload]]
    path = "/srv/inputs/app.tar"        # a host path under a root, or relative to the bundle
    sha256 = "..."                     # optional: checked before anything is rented
    [[upload]]
    path = "files"
    contents = true                    # a folder's children, not the folder
    exclude = ["out"]
    [run]
    script = "files/run.sh"            # the rental runs bash /root/<its name> <args>
    args = ["/root/bundle"]
    progress = "^RUN "
    done = "RUN done"
    failed = "^RUN tour .* exit [^0]"
    results = ["/root/bundle/results.tar", "/root/run.log"]
    stop = "pkill -f launcher.py"      # what to stop when the cap comes first (default: the run script)
    partial = "cd /root/bundle && tar -cf results.tar results"   # how to pack what there is, then fetch it

A launch bundle is checked against its launch file on every load: a member, runtime file or JDK archive that is
missing or other bytes refuses it, before any quote. It also holds ``manifest.json`` (``wb.bundle.v1``) beside
``bundle.toml``: the SHA-256 of ``bundle.toml`` and of every file under ``files/``. Loading such a bundle refuses one
whose files changed, or gained a file, since it was made.
"""

from __future__ import annotations

import fnmatch
import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from wb_core.module import ParamError, sha256_file

REMOTE = "/root/bundle"  # where a rental's bundle lives: uploads go there, and the script is given it
MANIFEST = "manifest.json"
SCHEMA = "wb.bundle.v1"
FILES = "files"          # the folder of a made bundle whose children go to REMOTE


@dataclass
class Upload:
    local: Path
    contents: bool = False
    exclude: list[str] = field(default_factory=list)
    sha256: str | None = None
    to: str = REMOTE + "/"

    def files(self) -> list[Path]:
        if not self.contents:
            return [self.local]
        return [p for p in sorted(self.local.iterdir())
                if not any(fnmatch.fnmatch(p.name, pattern) for pattern in self.exclude)]


@dataclass
class Bundle:
    folder: Path
    name: str
    purpose: str
    jdks: list[str]
    gpu: bool
    uploads: list[Upload]
    script: Path
    args: list[str]
    progress: str
    done: str
    failed: str | None
    results: list[str]
    disk_gb: int
    stop: str
    partial: str | None
    own_jdk: bool = False
    launch: dict | None = None   # a launch bundle's bundle-launch.json
    ram_gb: int = 0              # the system memory the run needs; an offer with less is refused

    def upload_bytes(self) -> int:
        total = 0
        for upload in self.uploads:
            for path in upload.files():
                total += path.stat().st_size if path.is_file() else sum(
                    p.stat().st_size for p in path.rglob("*") if p.is_file())
        return total


CRLF = ("has Windows line endings (CRLF), which bash on the rental cannot run; check it out with LF line endings "
        "(for example `*.sh text eol=lf` in .gitattributes) or convert it")


def windows_line_endings(path: Path) -> bool:
    """Whether a script has CRLF line endings: a checkout on Windows with autocrlf makes them, and bash on a rental
    stops at the first line. Caught here, before anything is rented."""
    return b"\r\n" in path.read_bytes()


def load(folder: Path, resolve, *, verify_uploads: bool = True) -> Bundle:
    """``resolve`` maps a path written in the manifest (host or container form) to a readable local path.
    ``verify_uploads`` off skips hashing the pinned uploads (a maker that has just hashed them)."""
    folder = Path(folder)
    manifest = folder / "bundle.toml"
    if not manifest.is_file():
        raise ParamError(f"{folder} has no bundle.toml")
    try:
        data = tomllib.loads(manifest.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as error:
        raise ParamError(f"bundle.toml: {error}") from None
    from . import launch_bundle
    launched = launch_bundle.verify(folder)  # before the manifest: it names the member or JDK that differs
    verify_manifest(folder)

    def local(value: str) -> Path:
        candidate = folder / value
        if not value.startswith(("/", "\\")) and ":" not in value[:3] and candidate.exists():
            return candidate
        return Path(resolve(value))

    uploads = []
    for item in data.get("upload", []):
        path = local(item["path"])
        if not path.exists():
            raise ParamError(f"bundle upload {item['path']} does not exist")
        upload = Upload(path, bool(item.get("contents")), list(item.get("exclude", [])), item.get("sha256"),
                        item.get("to", REMOTE + "/"))
        if verify_uploads and upload.sha256 and path.is_file() and sha256_file(path) != upload.sha256:
            raise ParamError(f"bundle upload {item['path']} does not match its pinned sha256")
        uploads.append(upload)
    run = data.get("run") or {}
    script = folder / run.get("script", "run.sh")
    if not script.is_file():
        raise ParamError(f"bundle script {run.get('script', 'run.sh')} is missing")
    if windows_line_endings(script):
        raise ParamError(f"bundle script {run.get('script', 'run.sh')} {CRLF}")
    needs = data.get("needs") or {}
    return Bundle(folder=folder, name=data.get("name", folder.name), purpose=data.get("purpose", "measurement"),
                  jdks=[str(j) for j in needs.get("jdk", [])], gpu=bool(needs.get("gpu", False)), uploads=uploads,
                  script=script, args=[str(a) for a in run.get("args", [REMOTE])],
                  progress=run.get("progress", "^RUN "), done=run.get("done", "RUN done"), failed=run.get("failed"),
                  results=list(run.get("results", [f"{REMOTE}/results.tar", "/root/run.log"])),
                  disk_gb=int(needs.get("disk_gb", 32)),
                  stop=run.get("stop") or f"pkill -f '/root/{script.name}'",
                  partial=run.get("partial"),
                  own_jdk=bool(needs.get("own_jdk", False)), launch=launched,
                  ram_gb=int(needs.get("ram_gb", 0)))


# ---------------------------------------------------------------------------------------------
# a made bundle's manifest

def listed_files(folder: Path) -> list[dict]:
    """``bundle.toml`` and every file under ``files/``, by sorted POSIX path, with size and SHA-256."""
    paths = [folder / "bundle.toml"] if (folder / "bundle.toml").is_file() else []
    if (folder / FILES).is_dir():
        paths += sorted(p for p in (folder / FILES).rglob("*") if p.is_file())
    entries = [{"path": p.relative_to(folder).as_posix(), "bytes": p.stat().st_size, "sha256": sha256_file(p)}
               for p in paths]
    return sorted(entries, key=lambda e: e["path"])


def verify_manifest(folder: Path) -> None:
    """Refuses a made bundle whose listed files changed or went, or whose ``files/`` gained one; a bundle without a
    manifest (one written by hand) passes."""
    path = folder / MANIFEST
    if not path.is_file():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        entries = {str(e["path"]): str(e["sha256"]) for e in data.get("files", [])}
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        raise ParamError(f"{MANIFEST} is not a bundle manifest: {error}") from None
    for relative, digest in entries.items():
        parts = PurePosixPath(relative).parts
        if not parts or relative.startswith("/") or ".." in parts:
            raise ParamError(f"{MANIFEST} names {relative!r}, which is no path in the bundle")
        file = folder / relative
        if not file.is_file():
            raise ParamError(f"bundle file {relative} is gone since the bundle was made ({MANIFEST})")
        if sha256_file(file) != digest:
            raise ParamError(f"bundle file {relative} changed since the bundle was made ({MANIFEST})")
    if (folder / FILES).is_dir():
        for file in sorted(p for p in (folder / FILES).rglob("*") if p.is_file()):
            relative = file.relative_to(folder).as_posix()
            if relative not in entries:
                raise ParamError(f"bundle file {relative} is not in its {MANIFEST}; make the bundle again")


# ---------------------------------------------------------------------------------------------
# writing bundle.toml

def toml_value(value) -> str:
    """A TOML value for the few types a bundle.toml holds."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")  # a JSON string is a TOML string
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(v) for v in value) + "]"
    raise TypeError(f"no TOML form for {type(value).__name__}")


def render(head: dict, needs: dict, uploads: list[dict], run: dict, comment: str = "") -> str:
    """A bundle.toml: its top-level keys, ``[needs]``, one ``[[upload]]`` per upload and ``[run]``; None values are
    left out."""
    lines = [f"# {' '.join(comment.split())}"] if comment else []
    lines += [f"{key} = {toml_value(value)}" for key, value in head.items() if value is not None]
    lines += ["", "[needs]"] + [f"{key} = {toml_value(value)}" for key, value in needs.items() if value is not None]
    for upload in uploads:
        lines += ["", "[[upload]]"] + [f"{key} = {toml_value(value)}" for key, value in upload.items()
                                       if value is not None]
    lines += ["", "[run]"] + [f"{key} = {toml_value(value)}" for key, value in run.items() if value is not None]
    return "\n".join(lines) + "\n"
