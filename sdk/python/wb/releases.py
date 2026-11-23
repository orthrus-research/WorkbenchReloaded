"""Releases: immutable copies of Workbench at a commit, so the hooks, the CLI on PATH and the host runner run code
that a merge cannot change under them.

    <releases>/<commit12>/      what ``git archive <commit>`` holds, plus RELEASE.json (commit, tree, tests, override)
    <releases>/current          a link to the release in service; wb deploy moves it last

<releases> is [paths] releases in machine config, else a folder named releases in the config folder. git config
wb.cli names <releases>/current/bin/wb, so a hook always runs the deployed release, never a working tree; a working
tree runs only when started as such (its own bin/wb, with WB_DEV=1)."""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import tarfile
import time
from pathlib import Path

from . import config as wbconfig

KEEP = 10


def root(config) -> Path:
    configured = ((config.values.get("paths") or {}).get("releases"))
    folder = Path(str(configured)) if configured else Path(config.folder or wbconfig.default_folder()) / "releases"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def current(config) -> Path:
    return root(config) / "current"


def cli_command(config) -> str:
    if os.name == "nt":
        return f"cmd.exe //c {current(config) / 'bin' / 'wb.cmd'}"
    return str(current(config) / "bin" / "wb")


def make(config, repo: Path, commit: str, record: dict) -> Path:
    """The release folder of ``commit`` (made once; a second deploy of the same commit reuses it)."""
    sha = commit.split("-")[0]
    target = root(config) / sha[:12]
    if (target / "RELEASE.json").is_file():
        return target
    part = target.with_name(target.name + ".part")
    shutil.rmtree(part, ignore_errors=True)
    archive = subprocess.run(["git", "-C", str(repo), "archive", "--format=tar", sha], capture_output=True, check=True)
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        tar.extractall(part, filter="data")
    (part / "RELEASE.json").write_text(json.dumps(dict(record, commit=commit, made=time.strftime("%Y-%m-%dT%H:%M:%S"),
                                                       made_at=time.time()), indent=1), encoding="utf-8")
    part.replace(target)
    return target


def _is_link(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(os.path, "isjunction", lambda _: False)(path))


def point(config, release: Path) -> Path | None:
    """Moves ``current`` to ``release``; returns the release it named before."""
    link = current(config)
    before = Path(os.path.realpath(link)) if link.exists() else None
    if link.exists() or link.is_symlink():
        if not _is_link(link):
            raise RuntimeError(f"{link} is a folder, not a link; move it away before deploying")
        os.rmdir(link) if os.name == "nt" else link.unlink()  # removes the link, never what it points at
    if os.name == "nt":
        done = subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(link), str(release)], capture_output=True, text=True)
        if done.returncode != 0:
            raise RuntimeError(f"could not link {link} to {release}: {(done.stderr or done.stdout).strip()}")
    else:
        os.symlink(release, link, target_is_directory=True)
    return before


def prune(config, keep: int = KEEP) -> list[str]:
    """Removes releases past the newest ``keep``, never the one in service or the one before it."""
    folder = root(config)
    link = current(config)
    serving = Path(os.path.realpath(link)) if link.exists() else None
    def made_at(path: Path) -> float:
        try:
            return float(json.loads((path / "RELEASE.json").read_text(encoding="utf-8")).get("made_at") or 0)
        except (OSError, ValueError):
            return 0.0

    made = sorted((p for p in folder.iterdir() if p.is_dir() and not _is_link(p) and (p / "RELEASE.json").is_file()),
                  key=made_at, reverse=True)
    removed = []
    for old in made[keep:]:
        if serving and serving.exists() and os.path.samefile(old, serving):
            continue
        shutil.rmtree(old, ignore_errors=True)
        removed.append(old.name)
    return removed
