"""Releases: immutable copies of Workbench at a commit, so the hooks, the CLI on PATH and the host runner run code
that a merge cannot change under them.

    <releases>/<commit12>/      what ``git archive <commit>`` holds, plus RELEASE.json: commit, tree, the host
                                suite's result (tests), the container pass CI recorded for the tree (ci), the image
                                inputs digest (image), and override or bootstrap when either applies
    <releases>/current          a link to the release in service; wb deploy moves it last

The container runs a copy of the release in its state volume (image/code.sh), so the image holds no Workbench code.

Old releases are wb gc's class ``releases``: every release but the one in service and the one deployed before it.

<releases> is WB_RELEASES, else [paths] releases in machine config, else ~/.local/share/wb/releases (never the config
folder, whose folders are all read as config, and not AppData, which a packaged app redirects). git config
wb.cli names <releases>/current/bin/wb, so a hook always runs the deployed release, never a working tree; a working
tree runs only when started as such: `wb dev <verb>` (WB_DEV=1)."""

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



def root(config) -> Path:
    """Where releases live: WB_RELEASES, else [paths] releases, else the user's local application data. Never inside
    the config folder, where every folder is read as config (a release carries example config files)."""
    named = os.environ.get("WB_RELEASES") or os.path.expanduser(str(config.get("paths.releases") or "")) or None
    # The home folder, not AppData: a packaged app's writes to AppData are redirected into its own package folder,
    # which no process outside it (a terminal's git, the scheduled host runner) can see.
    folder = Path(named) if named else Path.home() / ".local" / "share" / "wb" / "releases"
    settings = Path(config.folder or wbconfig.default_folder()).resolve()
    if settings == folder.resolve() or settings in folder.resolve().parents:
        raise RuntimeError(f"releases may not live in the config folder {settings}: every folder there is read as "
                           "config; set [paths] releases to another folder")
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def current(config) -> Path:
    return root(config) / "current"


def serving(config) -> Path | None:
    """The release folder in service (what ``current`` names, by its real path), or None when there is none."""
    link = current(config)
    if not link.exists():
        return None
    release = root(config) / Path(os.path.realpath(link)).name
    return release if (release / "RELEASE.json").is_file() else None


def info(release: Path) -> dict:
    """A release's RELEASE.json, or {} when it has none."""
    try:
        return json.loads((Path(release) / "RELEASE.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


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
