"""Small file-system helpers for state that two jobs may write at once: an exclusive lock any process can take on
any folder it can write (including a mounted Windows folder seen from the container, where ``flock`` is not to be
relied on), and writes that land whole: a reader sees the old file or the new one, and once a write returns its
bytes are on the disk, so a crash or a power loss cannot leave a record that exists and is empty.

The lock is a file made with ``O_CREAT | O_EXCL``, which only one process can make. It names its holder and is
removed when the holder is done; a lock older than ``stale`` seconds is one whose holder died, and is broken."""

from __future__ import annotations

import contextlib
import errno
import json
import os
import secrets
import socket
import threading
import time
from pathlib import Path


class LockTimeout(Exception):
    pass


@contextlib.contextmanager
def exclusive(lock: Path, *, stale: float = 60.0, timeout: float = 120.0, poll: float = 0.05):
    """Holds ``lock`` (a file path) for the block. ``stale`` must exceed the longest time a holder keeps it."""
    lock.parent.mkdir(parents=True, exist_ok=True)
    owner = f"{socket.gethostname()} {os.getpid()} {threading.get_ident()} {secrets.token_hex(4)}"
    deadline = time.monotonic() + timeout
    while True:
        try:
            handle = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                age = time.time() - lock.stat().st_mtime
            except FileNotFoundError:
                continue  # released between the two calls: try again at once
            if age > stale:
                held_by = _holder(lock)
                time.sleep(poll)
                if _holder(lock) == held_by:  # the same dead holder, not one that took it just now
                    with contextlib.suppress(FileNotFoundError, PermissionError):
                        lock.unlink()
                continue
            if time.monotonic() > deadline:
                raise LockTimeout(f"{lock} is held (for {age:.0f} s): {_holder(lock)}") from None
            time.sleep(poll)
            continue
        except PermissionError:
            # Windows refuses to create a file another process is deleting: it is being released.
            if time.monotonic() > deadline:
                raise LockTimeout(f"{lock} could not be made") from None
            time.sleep(poll)
            continue
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(owner + "\n")
        break
    try:
        yield
    finally:
        if _holder(lock) == owner:
            with contextlib.suppress(FileNotFoundError, PermissionError):
                lock.unlink()


def _holder(lock: Path) -> str:
    try:
        return lock.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def replace(part: Path, target: Path, attempts: int = 50):
    """``part.replace(target)``, retried while Windows refuses it because a reader has ``target`` open."""
    for attempt in range(attempts):
        try:
            part.replace(target)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.02 * (attempt + 1))


def part_of(target: Path) -> Path:
    """A temporary name beside ``target`` that no other process or thread uses."""
    return target.with_name(f"{target.name}.{os.getpid()}.{threading.get_ident()}.{secrets.token_hex(4)}.part")


# fsync errors that mean the file system cannot sync (a share or mount that does not pass it on), not that the
# bytes were lost: the write goes ahead as well as that file system allows.
_CANNOT_SYNC = {errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EROFS, errno.EBADF}


def sync(handle) -> None:
    """Puts a file's written bytes on the disk (``handle`` is an open file or a descriptor)."""
    try:
        if hasattr(handle, "flush"):
            handle.flush()
        os.fsync(handle if isinstance(handle, int) else handle.fileno())
    except OSError as error:
        if error.errno not in _CANNOT_SYNC:
            raise


def sync_folder(folder: Path) -> None:
    """Puts a folder's entries on the disk, so a file moved into it stays moved. Windows journals a move itself and
    cannot open a folder for this."""
    if os.name == "nt":
        return
    try:
        handle = os.open(folder, os.O_RDONLY)
    except OSError:
        return
    try:
        sync(handle)
    finally:
        os.close(handle)


def write_whole(target: Path, data: bytes) -> Path:
    """Writes ``data`` to ``target`` whole or not at all, and on the disk before it returns."""
    target.parent.mkdir(parents=True, exist_ok=True)
    part = part_of(target)
    try:
        with open(part, "wb") as stream:
            stream.write(data)
            sync(stream)
        replace(part, target)
        sync_folder(target.parent)
    finally:
        part.unlink(missing_ok=True)
    return target


def write_json(target: Path, value, **dumps) -> Path:
    """Writes ``value`` to ``target`` as JSON, whole or not at all (see ``write_whole``)."""
    return write_whole(target, json.dumps(value, **({"indent": 1} | dumps)).encode("utf-8"))


def set_aside(path: Path) -> Path | None:
    """Moves a damaged file out of the way under a name no reader takes for a record (``<name>.damaged-<when>``),
    so the record can be written again and the damage is kept to be seen. None when it was gone already. Retried
    while Windows refuses it because a reader has the file open."""
    aside = path.with_name(f"{path.name}.damaged-{time.strftime('%Y%m%dT%H%M%S')}-{secrets.token_hex(2)}")
    try:
        replace(path, aside)
    except FileNotFoundError:
        return None
    return aside


ABSENT, DAMAGED = "absent", "damaged"


def record(path: Path) -> dict | str:
    """What a record file holds: its JSON object, ``ABSENT`` when there is no file, or ``DAMAGED`` when the file holds
    no object (empty, cut short, or not an object). A read that fails for any other reason raises: a file that
    could not be read is not known to be damaged, and must never be replaced as if it were."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ABSENT
    try:
        value = json.loads(text)
    except ValueError:
        return DAMAGED
    return value if isinstance(value, dict) else DAMAGED


@contextlib.contextmanager
def healing(target: Path):
    """Held while a damaged ``target`` is set aside and written again, so two writers that found the same damage do
    not set aside each other's new file: whoever takes it second finds the record whole, and leaves it."""
    with exclusive(target.with_name(target.name + ".heal"), stale=60.0):
        yield
