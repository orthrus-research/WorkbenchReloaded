"""What this machine is, so the rest of Workbench never branches on the OS itself.

Workbench is platform-neutral: the same config and commands on Windows, Linux and macOS. The differences sit
here, in one place:

- ``os_name``: windows, linux or macos.
- ``docker_flavour``: ``desktop`` (Docker Desktop: the engine runs in a VM and ``host.docker.internal`` reaches
  the host's loopback) or ``native`` (an engine on this kernel: the container reaches a loopback-only host
  service only through host networking).
- ``link``: a directory link that the platform's tools follow: an NTFS junction on Windows (no privilege
  needed), a symbolic link elsewhere.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


def os_name() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def docker_flavour() -> str:
    """``desktop`` or ``native``; ``WB_DOCKER_FLAVOUR`` overrides the probe."""
    explicit = os.environ.get("WB_DOCKER_FLAVOUR")
    if explicit in ("desktop", "native"):
        return explicit
    if os_name() != "linux":
        return "desktop"  # Docker on Windows and macOS always runs its engine in a VM
    try:
        done = subprocess.run(["docker", "info", "--format", "{{.OperatingSystem}}"], capture_output=True, text=True,
                              timeout=20)
        return "desktop" if "Docker Desktop" in done.stdout else "native"
    except (OSError, subprocess.SubprocessError):
        return "native"


@dataclass(frozen=True)
class Profile:
    os: str
    docker: str

    @property
    def host_networking(self) -> bool:
        """A native Linux engine: the container shares the host's network, so 127.0.0.1 is the same loopback on
        both sides and nothing is published beyond it."""
        return self.docker == "native"

    @property
    def host_url(self) -> str:
        return "http://127.0.0.1:7378" if self.host_networking else "http://host.docker.internal:7378"


def profile() -> Profile:
    return Profile(os_name(), docker_flavour())


def make_link(link: Path, target: Path) -> str:
    """Makes ``link`` point at the directory ``target``; returns the kind made."""
    link, target = Path(link), Path(target)
    if link.exists() or link.is_symlink():
        raise FileExistsError(f"{link} already exists")
    if os_name() == "windows":
        import _winapi
        _winapi.CreateJunction(str(target.resolve()), str(link))
        return "junction"
    os.symlink(target.resolve(), link, target_is_directory=True)
    return "symlink"


def is_link(path: Path) -> bool:
    path = Path(path)
    if path.is_symlink() or os.path.isjunction(path):  # a broken junction too
        return True
    if os_name() == "windows" and path.exists():
        return bool(getattr(os.lstat(path), "st_reparse_tag", 0))
    return False


def remove_link(path: Path):
    """Removes a link made by ``make_link`` without touching what it points at."""
    path = Path(path)
    if not is_link(path):
        raise ValueError(f"{path} is not a link")
    if os_name() == "windows" and not path.is_symlink():
        os.rmdir(path)  # a junction is removed like an empty directory; its target is untouched
    else:
        path.unlink()


def remove_tree(path: Path):
    """Removes a folder without following a link anywhere in it: every link (a symlink or a junction) is removed
    first, as a link, so nothing it points at is touched; then the rest, read-only files too, and paths past
    Windows' 260 characters. Given a link, it removes only the link."""
    path = Path(path)
    if is_link(path):
        remove_link(path)
        return
    if not path.exists():
        return
    folders = [path]
    while folders:
        for entry in os.scandir(folders.pop()):
            child = Path(entry.path)
            if is_link(child):
                remove_link(child)
            elif entry.is_dir(follow_symlinks=False):
                folders.append(child)

    def writable(function, target, _):
        os.chmod(target, 0o700)
        function(target)

    long = "\\\\?\\" + str(path.resolve()) if os_name() == "windows" else str(path)
    shutil.rmtree(long, onexc=writable)


def same_volume(a: Path, b: Path) -> bool:
    """True when a rename from ``a`` into ``b`` is a rename, not a copy."""
    a, b = Path(a).resolve(), Path(b).resolve()
    probe = b
    while not probe.exists():
        probe = probe.parent
    return os.stat(a).st_dev == os.stat(probe).st_dev


def launcher(repo: Path) -> list[str]:
    """The command that runs this checkout's ``wb`` with no installation and no global PYTHONPATH."""
    if os_name() == "windows":
        return [str(repo / "bin" / "wb.cmd")]
    return [str(repo / "bin" / "wb")]


def which(name: str) -> str | None:
    return shutil.which(name)
