"""File operations the build and toolchain commands share: digests of file sets, copies that land whole or not
at all, ``.sha256`` sidecars, and the lock that concurrent writers of one folder share."""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import time
import uuid
from pathlib import Path

from wb_core.module import Context, EnvironmentProblem, sha256_file


def set_digest(digests: dict[str, str]) -> str:
    """One SHA-256 for a set of named files: over sorted ``<name>\\0<sha256>\\n`` lines, the line format of the
    core's tree digest, so a set and a folder holding exactly those files agree."""
    digest = hashlib.sha256()
    for name in sorted(digests):
        digest.update(f"{name}\0{digests[name]}\n".encode("utf-8"))
    return digest.hexdigest()


def _temporary(target: Path) -> Path:
    return target.with_name(f".{target.name}.{uuid.uuid4().hex[:12]}.part")


def replace(source: Path, target: Path, attempts: int = 20) -> None:
    """``os.replace``, retried for a moment: on Windows a reader holding the target open makes it fail briefly."""
    for attempt in range(attempts):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.1)


def copy_verified(source: Path, target: Path, digest: str) -> None:
    """Copies ``source`` to ``target`` through a temporary file beside it, which takes the target's place only once
    its bytes have ``digest``: a reader finds the old file or the new one, never part of one."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = _temporary(target)
    try:
        shutil.copyfile(source, temporary)
        actual = sha256_file(temporary)
        if actual != digest:
            raise EnvironmentProblem(f"{source.name} changed while it was copied (SHA-256 {actual[:12]}, "
                                     f"expected {digest[:12]})")
        replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def write_atomic(path: Path, data: bytes) -> None:
    """Writes ``data`` to ``path`` through a temporary file beside it, so the file is never seen half-written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = _temporary(path)
    try:
        temporary.write_bytes(data)
        replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def sidecar_text(name: str, digest: str) -> bytes:
    """A ``.sha256`` file's content, in the format ``sha256sum`` writes and checks."""
    return f"{digest}  {name}\n".encode("utf-8")


def write_sidecar(path: Path, digest: str) -> Path:
    """``<file>.sha256`` beside ``path``; rewritten only when it does not already say ``digest``."""
    side = path.with_name(path.name + ".sha256")
    text = sidecar_text(path.name, digest)
    if not side.is_file() or side.read_bytes() != text:
        write_atomic(side, text)
    return side


def _try_lock(handle) -> bool:
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(handle) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def locked(path: Path, ctx: Context | None = None, patience: float = 120.0):
    """Holds an exclusive lock on the first byte of ``path`` (created when missing). Every process that locks the same
    file this way waits for the others, so a read-modify-write of a shared file never loses another writer's change.
    Waits up to ``patience`` seconds; cancelling the job stops the wait."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as handle:
        deadline = time.monotonic() + patience
        while not _try_lock(handle):
            if time.monotonic() > deadline:
                raise EnvironmentProblem(f"{path.name} stayed locked by another writer for {patience:.0f} s")
            if ctx is not None:
                ctx.sleep(0.05)
            else:
                time.sleep(0.05)
        try:
            yield
        finally:
            _unlock(handle)
