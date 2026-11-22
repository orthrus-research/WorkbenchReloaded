"""The GPU lease: one lock file and a folder of lanes that every GPU user on the machine follows (spec S19).

- The lease is the OS lock on byte 0 of the lock file; the owner is JSON from byte 1.
- Checks never take the lock: on Windows they read byte 0, which the holder's lock makes unreadable.
- Workbench takes its turn as the lane ``workbench`` in the coordination folder, with the queue's rule:
  the lease must have been free for the cooldown, and no live lane may be running or have waited longer.
- Launchers under a Workbench job join the held lease through WB_TURN_TOKEN (the owner's turnToken), or through
  their own variable names listed in ``[runner] token_env``.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

TOKEN_ENV = "WB_TURN_TOKEN"
LANE_ENV = "WB_TURN_LANE"
COOLDOWN = 15.0
PASS_AFTER = 600.0
LIVE_FOR = 300.0
POLL = 5.0


def stamp(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or time.time(), timezone.utc).isoformat()


def parse(value) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def occupied(path: Path) -> bool:
    try:
        stream = open(path, "rb", buffering=0)
    except OSError:
        return False
    with stream:
        if sys.platform == "win32":
            try:
                stream.read(1)
                return False
            except OSError:
                return True
        import fcntl
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        return False


def owner(path: Path) -> dict | None:
    try:
        with open(path, "rb", buffering=0) as stream:
            stream.seek(1)
            value = json.loads(stream.read(65536).decode("utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


class Lease:
    def __init__(self, path: Path, owner_info: dict):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = os.fdopen(os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600), "r+b", buffering=0)
        try:
            if os.path.getsize(self.path) == 0:
                self.stream.write(b" ")
            self.stream.seek(0)
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.stream.close()
            raise
        self.owner = owner_info
        self.stream.seek(1)
        self.stream.write(json.dumps(owner_info).encode("utf-8"))
        self.stream.truncate()

    def close(self):
        if self.stream.closed:
            return
        try:
            self.stream.seek(1)
            self.stream.write(json.dumps(dict(self.owner, releasedUtc=stamp())).encode("utf-8"))
            self.stream.truncate()
        except OSError:
            pass
        self.stream.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
        finally:
            self.stream.close()


class Lane:
    """This process's entry in the coordination folder, with a heartbeat every 30 s."""

    def __init__(self, folder: Path | None, name: str = "workbench"):
        self.folder, self.name = (Path(folder) if folder else None), name
        self.entry: dict = {}
        self.stop = threading.Event()
        self.thread = None

    def publish(self, **fields):
        if self.folder is None:
            return
        self.entry.update(fields, updatedUtc=stamp(), pid=os.getpid())
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self.folder / f"{self.name}.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.entry, indent=1), encoding="utf-8")
        os.replace(temporary, path)

    def start_heartbeat(self):
        def beat():
            while not self.stop.wait(30):
                self.publish()
        self.thread = threading.Thread(target=beat, daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        self.publish(state="idle", waiting_since=None)


def entries(folder: Path | None) -> dict[str, dict]:
    if folder is None or not Path(folder).is_dir():
        return {}
    out = {}
    for path in Path(folder).glob("*.json"):
        if path.name.startswith(".") or path.stem == "last-release":
            continue
        try:
            out[path.stem] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return out


def _pid_alive(pid) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if sys.platform == "win32":
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def live(entry: dict, at: float) -> bool:
    beat = parse(entry.get("updatedUtc"))
    return beat is not None and at - beat < LIVE_FOR and _pid_alive(entry.get("pid"))


def free_since(lock: Path, folder: Path | None) -> float | None:
    candidates = []
    if folder:
        release = Path(folder) / "last-release.json"
        try:
            candidates.append(parse(json.loads(release.read_text(encoding="utf-8")).get("utc")))
        except (OSError, ValueError):
            pass
    candidates.append(parse((owner(lock) or {}).get("releasedUtc")))
    if lock.exists():
        candidates.append(os.path.getmtime(lock))
    candidates = [c for c in candidates if c]
    return max(candidates) if candidates else None


def may_take(lane: str, mine: float, lanes: dict[str, dict], free_for: float, at: float) -> tuple[bool, str]:
    if free_for < COOLDOWN:
        return False, f"lease free {free_for:.0f} s of the {COOLDOWN:.0f} s cooldown"
    for name, entry in lanes.items():
        if name == lane or not live(entry, at):
            continue
        if str(entry.get("state", "")).startswith("running"):
            return False, f"{name} is running"
        since = parse(entry.get("waiting_since"))
        if str(entry.get("state", "")).startswith("waiting") and since is not None and since < mine:
            if free_for < PASS_AFTER:
                return False, f"{name} has waited since {entry.get('waiting_since')}"
    return True, "our turn"


def acquire(lock: Path, coordination: Path | None, *, note: str, job: str, cancelled: threading.Event,
            report=None, lane_name: str = "workbench", poll: float = POLL) -> tuple[Lease, Lane, str]:
    """Waits for Workbench's turn and takes the lease; returns the lease, the lane and the turn token."""
    lane = Lane(coordination, lane_name)
    mine = time.time()
    lane.publish(state="waiting", waiting_since=stamp(mine), note=note, job=job)
    lane.start_heartbeat()
    reason = None
    held_seen = None
    try:
        while not cancelled.is_set():
            at = time.time()
            if occupied(lock):
                held_seen = at
                why = f"held by {(owner(lock) or {}).get('lane') or (owner(lock) or {}).get('name') or 'another job'}"
            else:
                since = free_since(lock, coordination)
                free_for = float("inf") if since is None else max(0.0, at - max(since, held_seen or 0))
                ok, why = may_take(lane_name, mine, entries(coordination), free_for, at)
                if ok:
                    token = uuid.uuid4().hex
                    try:
                        lease = Lease(lock, {"pid": os.getpid(), "lane": lane_name, "turnToken": token, "note": note,
                                             "job": job, "startedUtc": stamp()})
                        lane.publish(state="running", startedUtc=stamp())
                        return lease, lane, token
                    except OSError:
                        held_seen = time.time()
                        why = "another job took the lease first"
            if why != reason and report:
                report(why)
            reason = why
            cancelled.wait(poll)
        raise InterruptedError("cancelled while waiting for the lease")
    except BaseException:
        lane.close()
        raise


def release(lease: Lease, lane: Lane, coordination: Path | None):
    lease.close()
    if coordination:
        try:
            (Path(coordination) / "last-release.json").write_text(json.dumps({"utc": stamp(), "lane": lane.name}),
                                                                   encoding="utf-8")
        except OSError:
            pass
    lane.close()
