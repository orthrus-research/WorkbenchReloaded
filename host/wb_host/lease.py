"""The GPU lease: one lock file and a folder of lanes that every GPU user on the machine follows (spec S19).

- The lease is the OS lock on byte 0 of the lock file; the owner is JSON from byte 1.
- Checks never take the lock: on Windows they read byte 0, which the holder's lock makes unreadable.
- Workbench takes its turn as the lane ``workbench`` in the coordination folder, with the queue's rule:
  the lease must have been free for the cooldown, and no live lane may be running or have waited longer.
- Launchers under a Workbench job join the held lease through WB_TURN_TOKEN (the owner's turnToken), or through
  their own variable names listed in ``[runner] token_env``.
- Any command takes a turn as a lane of its own (``turn``, ``wb gpu run``): one lane file per taker, served in
  ``waiting_since`` order. A lane that was already waiting keeps its place when its taker restarts; a lane whose
  heartbeat is old or whose process is gone holds nobody back, and ``stuck`` names it. A turn has a limit, past which
  its command is stopped and the lease released, and may be detached, so it outlives the shell that asked for it.

The old route's lock path and variable names stay usable as configured aliases: ``[runner] lease_file`` names the
lock, ``[runner] token_env`` and ``lane_env`` (and ``[lab] env``) the extra variables a turn's command gets.
"""

from __future__ import annotations

import json
import os
import subprocess
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
HEARTBEAT = 30.0
LIMIT = 3600.0  # the longest turn by default; past it the command is stopped and the lease released
FORGET_AFTER = 86400.0  # an idle lane silent this long is removed: it holds no place, and nobody reads it again


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
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = wintypes.HANDLE
        handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # access denied: it exists
        try:
            code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE: an exited process's handle may still open
        finally:
            kernel.CloseHandle(handle)
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
    forget(coordination)


def forget(coordination: Path | None, at: float | None = None) -> list[str]:
    """Removes the lane files that have said nothing for ``FORGET_AFTER``, idle or waiting: such a lane is live for
    nobody (a live lane beats every ``HEARTBEAT``), one lane per game session or command otherwise stays for good,
    and a waiter silent a day will not come back for its place. A lane marked running whose holder is gone stays for
    ``stuck`` to name and a person to clear. A lane written again since it was read is left. Returns the lanes it
    removed."""
    if coordination is None or not Path(coordination).is_dir():
        return []
    at = time.time() if at is None else at
    gone = []
    for name, entry in entries(coordination).items():
        beat = parse(entry.get("updatedUtc"))
        state = str(entry.get("state") or "")
        if state.startswith("running") or beat is None or at - beat < FORGET_AFTER or live(entry, at):
            continue
        path = Path(coordination) / f"{name}.json"
        try:
            if read_entry(path).get("updatedUtc") != entry.get("updatedUtc"):
                continue  # a turn of the same name wrote it since: it is in use again
            path.unlink()
            gone.append(name)
        except (OSError, ValueError):
            continue  # another turn wrote it just now, or it is open: the next release looks again
    return gone



# --- turns for any command: wb gpu --------------------------------------------------------------------------------


def write_entry(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    temporary.write_text(json.dumps(value, indent=1), encoding="utf-8")
    for attempt in range(50):  # Windows refuses the replace while a reader has the file open, for milliseconds
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if attempt == 49:
                temporary.unlink(missing_ok=True)
                raise
            time.sleep(0.02)


def read_entry(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def lane_name(text: str) -> str:
    if not text or not all(c.isalnum() or c in "-_." for c in text) or text.startswith("."):
        raise ValueError(f"lane {text!r}: letters, digits, - _ and . only")
    return text


def status(lock: Path, coordination: Path | None, at: float | None = None) -> dict:
    """The holder, the waiters in the order they are served, and the other lanes."""
    at = time.time() if at is None else at
    held = occupied(lock)
    lanes = {}
    for name, entry in entries(coordination).items():
        beat = parse(entry.get("updatedUtc"))
        lanes[name] = {"state": entry.get("state"), "waiting_since": entry.get("waiting_since"),
                       "heartbeat_age": None if beat is None else round(at - beat), "live": live(entry, at),
                       "pid": entry.get("pid"), "note": entry.get("note"), "session": entry.get("session"),
                       "quiet": bool(entry.get("quiet")), "limit": entry.get("limit")}
    waiting = sorted((n for n, e in lanes.items() if str(e["state"] or "").startswith("waiting") and e["live"]),
                     key=lambda n: parse(lanes[n]["waiting_since"]) or float("inf"))
    since = None if held else free_since(lock, coordination)
    return {"lease": {"file": str(lock), "held": held, "holder": owner(lock) if held else None,
                      "last": None if held else owner(lock),
                      "free_for": None if held or since is None else round(max(0.0, at - since))},
            "waiters": waiting, "lanes": lanes, "stuck": stuck(lock, coordination, at, lanes)}


def stuck(lock: Path, coordination: Path | None, at: float | None = None, lanes: dict | None = None) -> list[dict]:
    """What needs a person: lanes marked running whose holder is gone, a lease left free while a lane with no live
    process waits, and a lease held past the longest turn."""
    at = time.time() if at is None else at
    if lanes is None:
        lanes = {name: {"state": e.get("state"), "live": live(e, at), "note": e.get("note"),
                        "session": e.get("session")} for name, e in entries(coordination).items()}
    held = occupied(lock)
    since = None if held else free_since(lock, coordination)
    free_for = None if held or since is None else at - since
    issues = []
    for name, lane in lanes.items():
        state = str(lane.get("state") or "")
        if lane.get("live") or not state.startswith(("waiting", "running")):
            continue
        if state.startswith("running"):
            issues.append({"lane": name, "problem": "marked running, but its holder is gone", "note": lane.get("note"),
                           "fix": f"wb gpu leave {name}"})
        elif free_for is not None and free_for >= PASS_AFTER:
            issues.append({"lane": name, "problem": f"waiting with no live process while the lease has been free "
                                                    f"{free_for // 60:.0f} min", "note": lane.get("note"),
                           "fix": f"wb gpu leave {name}"})
    holder = owner(lock) if held else None
    started = parse((holder or {}).get("startedUtc"))
    limit = float((holder or {}).get("limit") or LIMIT)
    if held and started and at - started > limit + 600:
        issues.append({"lane": (holder or {}).get("lane"), "problem": f"the lease has been held "
                       f"{(at - started) / 60:.0f} min, past the turn's limit", "note": (holder or {}).get("note"),
                       "fix": "find the holder's process (wb gpu shows its pid)"})
    return issues


def leave(coordination: Path, lane: str):
    """Withdraws a lane's request (or clears a stale entry): it goes idle and loses its place."""
    path = Path(coordination) / f"{lane_name(lane)}.json"
    entry = read_entry(path)
    entry.update(state="idle", waiting_since=None, updatedUtc=stamp(), pid=os.getpid(), left=stamp())
    write_entry(path, entry)


def stop_tree(process: subprocess.Popen):
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)], capture_output=True)
    else:
        try:
            os.killpg(process.pid, 9)
        except OSError:
            process.kill()


def turn(lock: Path, coordination: Path, lane: str, command: list[str], *, note: str = "", limit: float = LIMIT,
         quiet: bool = False, session: str | None = None, token_env: list[str] = (), lane_env: list[str] = (),
         extra_env: dict | None = None, log=print, cancelled: threading.Event | None = None,
         poll: float | None = None) -> int:
    """Queues ``lane``, waits for its turn, holds the lease while ``command`` runs (at most ``limit`` seconds), then
    releases it; returns the command's exit code. The command gets the turn token in WB_TURN_TOKEN and every name
    in ``token_env``, the lane in WB_TURN_LANE and ``lane_env``, so launchers under it join the held lease."""
    lane = lane_name(lane)
    cancelled = cancelled or threading.Event()
    path = Path(coordination) / f"{lane}.json"
    earlier = read_entry(path)
    # A lane that was already waiting keeps its place: a restarted turn is the same request.
    since = earlier.get("waiting_since") if str(earlier.get("state", "")).startswith("waiting") else None
    mine = parse(since) or time.time()
    entry = {"state": "waiting", "waiting_since": stamp(mine), "note": note, "session": session,
             "command": list(command), "pid": os.getpid(), "quiet": bool(quiet), "limit": limit,
             "updatedUtc": stamp()}
    guard = threading.Lock()

    def publish(**changes):
        with guard:
            entry.update(changes, updatedUtc=stamp(), pid=os.getpid())
            write_entry(path, entry)

    publish()
    stop = threading.Event()

    def beat():
        while not stop.wait(HEARTBEAT):
            try:
                publish()
            except OSError as error:  # one missed beat is harmless; a dead heartbeat thread is not
                log(f"{stamp()} heartbeat write failed: {error}")
    threading.Thread(target=beat, daemon=True).start()
    log(f"{stamp()} queued {lane} (waiting since {entry['waiting_since']})")
    held, reason, held_seen = None, None, None
    try:
        while held is None:
            if cancelled.is_set():
                raise InterruptedError("cancelled while waiting for the lease")
            at = time.time()
            if occupied(lock):
                held_seen = at
                holder = owner(lock) or {}
                why = f"lease held by {holder.get('lane') or holder.get('name') or 'another job'}"
            else:
                since = free_since(lock, coordination)
                free_for = float("inf") if since is None else max(0.0, at - max(since, held_seen or 0))
                ok, why = may_take(lane, mine, entries(coordination), free_for, at)
                if ok:
                    token = uuid.uuid4().hex
                    try:
                        held = Lease(lock, {"pid": os.getpid(), "name": f"turn-{lane}", "lane": lane, "turnToken": token,
                                            "note": note, "quiet": bool(quiet), "limit": limit, "startedUtc": stamp()})
                        break
                    except OSError:
                        held_seen = time.time()
                        why = "another taker took the lease first"
            if why != reason:
                log(f"{stamp()} waiting: {why}")
                reason = why
            cancelled.wait(POLL if poll is None else poll)
        publish(state="running", startedUtc=stamp())
        log(f"{stamp()} running {lane}: {' '.join(command)}")
        env = dict(os.environ, **(extra_env or {}))
        env.update({name: token for name in [TOKEN_ENV, *token_env]})
        env.update({name: lane for name in [LANE_ENV, *lane_env]})
        env.update({"WB_LEASE_FILE": str(lock), "WB_COORDINATION_DIR": str(coordination)})
        flags = {"start_new_session": True} if os.name != "nt" else {}
        try:
            process = subprocess.Popen(list(command), env=env, **flags)
        except OSError as error:
            log(f"{stamp()} cannot start {command[0]}: {error}")
            return 127
        try:
            code = process.wait(timeout=limit)
        except subprocess.TimeoutExpired:
            log(f"{stamp()} the turn passed its {limit:.0f} s limit; stopping its command")
            stop_tree(process)
            code = process.wait()
        except KeyboardInterrupt:
            stop_tree(process)
            process.wait()
            raise
        log(f"{stamp()} turn finished with exit {code}")
        return code
    finally:
        stop.set()
        if held is not None:
            held.close()
            write_entry(Path(coordination) / "last-release.json", {"utc": stamp(), "lane": lane})
            log(f"{stamp()} released the lease")
        publish(state="idle", waiting_since=None, **({"releasedUtc": stamp()} if held is not None else {}))
        forget(coordination)


def detach(argv: list[str], coordination: Path, lane: str, cwd: Path | None = None) -> dict:
    """Runs ``argv`` (a ``wb gpu run`` without --detach) as a process of its own that outlives the shell: no console,
    a new process group, and out of the shell's job where the job allows it. Its log is in <coordination>/logs."""
    logs = Path(coordination) / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    log = logs / f"{lane_name(lane)}-{time.strftime('%Y%m%d-%H%M%S')}.log"
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    with open(log, "w", encoding="utf-8") as stream:
        def launch(creationflags: int) -> subprocess.Popen:
            return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT,
                                    cwd=str(cwd) if cwd else None, close_fds=True, creationflags=creationflags,
                                    start_new_session=sys.platform != "win32")
        try:
            process = launch(flags | getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0))
            breakaway = sys.platform == "win32"
        except OSError:  # the shell's job allows no breakaway: the turn still has no console to lose
            process = launch(flags)
            breakaway = False
    return {"detached": process.pid, "log": str(log), "left_job": breakaway}


def paths(settings) -> tuple[Path, Path, dict]:
    """The lock, the coordination folder and the alias names, as this machine's config names them."""
    runner = settings.runner
    lab = getattr(settings, "lab", None) or {}
    home = Path(os.path.expanduser(lab.get("home") or runner["lab_home"]))
    if not home.is_absolute():
        home = (Path(__file__).resolve().parents[2] / home).resolve()
    lock = Path(runner.get("lease_file") or home / "lease" / "gpu.lock")
    coordination = Path(runner.get("coordination_dir") or home / "coordination")
    env = lab.get("env") or {}
    aliases = {"token": [*runner.get("token_env", []), *[n for n, v in env.items() if v == "token"]],
               "lane": [*runner.get("lane_env", []), *[n for n, v in env.items() if v == "lane"]]}
    return lock, coordination, aliases
