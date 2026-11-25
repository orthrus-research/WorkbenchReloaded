"""Path translation between the caller's host paths and the container's mounts (spec 4.3).

Callers may pass Windows paths (``D:\\x``), WSL paths (``/mnt/d/x``) or container paths. Each maps to the  (scrub: allow)
root it falls under; a path under no root is refused. Results report host paths in the caller's style.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass

from .settings import Root

_WINDOWS = re.compile(r"^([A-Za-z]):[\\/]")
_WSL = re.compile(r"^/mnt/([A-Za-z])(/|$)")


class PathError(Exception):
    """A path no root takes as asked. ``fix`` says what to do instead."""

    def __init__(self, message: str, fix: str = "add a root under [paths.roots]"):
        super().__init__(message)
        self.fix = fix


def _win_norm(path: str) -> str:
    return path.replace("/", "\\").rstrip("\\") or path


def _to_wsl(windows: str) -> str:
    drive, rest = windows[0].lower(), windows[2:].replace("\\", "/")
    return f"/mnt/{drive}{rest}".rstrip("/") or "/"


def _from_wsl(path: str) -> str:
    match = _WSL.match(path)
    drive = match.group(1).upper()
    rest = path[len(match.group(0)):]
    return f"{drive}:\\" + rest.replace("/", "\\")


def _is_under(path: str, root: str, windows: bool) -> str | None:
    """The relative part of ``path`` under ``root``, or None."""
    if windows:
        p, r = _win_norm(path), _win_norm(root)
        pl, rl = p.lower(), r.lower()
        if rl.endswith(":"):
            rl += "\\"
            r += "\\"
        if pl == rl.rstrip("\\"):
            return ""
        prefix = rl if rl.endswith("\\") else rl + "\\"
        if pl.startswith(prefix):
            return p[len(prefix):].replace("\\", "/")
        return None
    p, r = posixpath.normpath(path), posixpath.normpath(root)
    if p == r:
        return ""
    prefix = r if r.endswith("/") else r + "/"
    return p[len(prefix):] if p.startswith(prefix) else None


@dataclass
class Resolved:
    container: str
    root: Root
    relative: str


class PathMap:
    def __init__(self, roots: list[Root]):
        self.roots = roots

    def resolve(self, value: str, *, need_write: bool = False) -> Resolved:
        if not isinstance(value, str) or not value:
            raise PathError("expected a path")
        candidates = []
        if _WINDOWS.match(value):
            candidates.append(("windows", value))
        elif _WSL.match(value):
            candidates.append(("windows", _from_wsl(value)))
            candidates.append(("posix", value))
        elif value.startswith("/"):
            candidates.append(("posix", value))
        else:
            raise PathError(f"{value!r} is relative; pass an absolute path")
        for root in sorted(self.roots, key=lambda r: -len(r.host)):
            host_windows = bool(_WINDOWS.match(root.host))
            for style, path in candidates:
                relative = None
                if style == "windows" and host_windows:
                    relative = _is_under(path, root.host, True)
                elif style == "posix" and not host_windows:
                    relative = _is_under(path, root.host, False)
                if relative is None and style == "posix":
                    relative = _is_under(path, root.container, False)
                if relative is not None:
                    if ".." in relative.split("/"):
                        raise PathError(f"{value!r} leaves root {root.name}")
                    if need_write and root.mode != "rw":
                        writable = [r for r in self.roots if r.mode == "rw"]
                        named = ", ".join(f"{r.host} ({r.name})" for r in writable)
                        raise PathError(f"{value!r} is under root {root.name}, which is read-only"
                                        + (f"; write under {named}" if writable else ""),
                                        fix=f"give a folder under {named}" if writable else
                                        "add a read-write root under [paths.roots]")
                    container = root.container if not relative else posixpath.join(root.container, relative) \
                        if not _WINDOWS.match(root.container) else root.container.rstrip("\\") + "\\" + \
                        relative.replace("/", "\\")
                    return Resolved(container=container, root=root, relative=relative)
        raise PathError(f"{value!r} is under no configured root (see [paths.roots]; "
                        f"{'roots: ' + ', '.join(r.name for r in self.roots) if self.roots else 'none configured'})")

    def to_host(self, container_path: str, style: str = "windows") -> str:
        """The host path of a container path, in the caller's style; unchanged if under no root."""
        for root in sorted(self.roots, key=lambda r: -len(r.container)):
            relative = _is_under(container_path, root.container, bool(_WINDOWS.match(root.container)))
            if relative is None:
                continue
            if _WINDOWS.match(root.host):
                windows = root.host.rstrip("\\/") + ("\\" + relative.replace("/", "\\") if relative else "")
                if len(root.host.rstrip("\\/")) == 2 and not relative:
                    windows += "\\"
                return windows if style == "windows" else _to_wsl(windows)
            return posixpath.join(root.host, relative) if relative else root.host
        return container_path
