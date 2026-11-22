"""Bundle manifests (spec 10.1): what a rental uploads, needs, runs and brings back, instead of a hard-coded
list of tars.

    name = "cal-02"
    purpose = "calibration"
    [needs]
    jdk = ["26"]
    gpu = true
    [[upload]]
    path = 'D:\\work\\linux-lab\\riven.tar'   # a host path under a root, or relative to the bundle  (scrub: allow)
    sha256 = "..."                                         # optional: checked before anything is rented
    [[upload]]
    path = "cal-01"
    contents = true                                        # a folder's children, not the folder
    exclude = ["out", "windows*"]
    [run]
    script = "run.sh"
    args = ["/root/bundle"]
    progress = "^RUN "
    done = "RUN done"
    failed = "^RUN tour .* exit [^0]"
    results = ["/root/bundle/results.tar", "/root/run.log"]
    stop = "pkill -f runtime.py"            # what to stop when the cap comes first (default: the run script)
    partial = "cd /root/bundle && tar -cf results.tar results"   # how to pack what there is, then fetch it
"""

from __future__ import annotations

import fnmatch
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from wb_core.module import ParamError, sha256_file


@dataclass
class Upload:
    local: Path
    contents: bool = False
    exclude: list[str] = field(default_factory=list)
    sha256: str | None = None
    to: str = "/root/bundle/"

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

    def upload_bytes(self) -> int:
        total = 0
        for upload in self.uploads:
            for path in upload.files():
                total += path.stat().st_size if path.is_file() else sum(
                    p.stat().st_size for p in path.rglob("*") if p.is_file())
        return total


def load(folder: Path, resolve) -> Bundle:
    """``resolve`` maps a path written in the manifest (host or container form) to a readable local path."""
    folder = Path(folder)
    manifest = folder / "bundle.toml"
    if not manifest.is_file():
        raise ParamError(f"{folder} has no bundle.toml")
    try:
        data = tomllib.loads(manifest.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as error:
        raise ParamError(f"bundle.toml: {error}") from None

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
                        item.get("to", "/root/bundle/"))
        if upload.sha256 and path.is_file() and sha256_file(path) != upload.sha256:
            raise ParamError(f"bundle upload {item['path']} does not match its pinned sha256")
        uploads.append(upload)
    run = data.get("run") or {}
    script = folder / run.get("script", "run.sh")
    if not script.is_file():
        raise ParamError(f"bundle script {script.name} is missing")
    needs = data.get("needs") or {}
    return Bundle(folder=folder, name=data.get("name", folder.name), purpose=data.get("purpose", "measurement"),
                  jdks=[str(j) for j in needs.get("jdk", [])], gpu=bool(needs.get("gpu", False)), uploads=uploads,
                  script=script, args=[str(a) for a in run.get("args", ["/root/bundle"])],
                  progress=run.get("progress", "^RUN "), done=run.get("done", "RUN done"), failed=run.get("failed"),
                  results=list(run.get("results", ["/root/bundle/results.tar", "/root/run.log"])),
                  disk_gb=int(needs.get("disk_gb", 32)),
                  stop=run.get("stop") or f"pkill -f '/root/{script.name}'",
                  partial=run.get("partial"))
