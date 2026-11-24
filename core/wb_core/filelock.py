"""Small file-system helpers for state that two jobs may write at once: an exclusive lock any process can take on
any folder it can write (including a mounted Windows folder seen from the container, where ``flock`` is not to be
relied on), and an atomic JSON write.

The lock is a file made with ``O_CREAT | O_EXCL``, which only one process can make. It names its holder and is
removed when the holder is done; a lock older than ``stale`` seconds is one whose holder died, and is broken."""

from __future__ import annotations

import contextlib
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


def write_json(target: Path, value) -> Path:
    """Writes ``value`` to ``target`` whole or not at all: a reader sees the old file or the new one, never a part."""
    target.parent.mkdir(parents=True, exist_ok=True)
    part = part_of(target)
    try:
        part.write_text(json.dumps(value, indent=1), encoding="utf-8")
        replace(part, target)
    finally:
        part.unlink(missing_ok=True)
    return target
