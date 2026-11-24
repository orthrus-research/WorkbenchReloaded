"""``wb play``: resolve a launch, build its instance (or reuse it), and play a session of it on this machine's GPU.

    wb play [project][@ref|@set|@head][:launch] [--from launch.json] [--name N] [--tour F] [--measure]
            [--driver D] [--debug client|server|both] [--fresh-world|--keep-world] [--param k=v]...
            [--jvm OPT]... [--view k=v]... [--window WxH] [--edit side/file:key=value]... [--no-rebuild]
            [--desktop hidden]

Without a project it plays the worktree you stand in; a project named while you stand in one of its worktrees is
that worktree. The revision every line resolves at is the folder's .set, or the head where the folder has none
('set <rev> (this folder, N behind)' or 'set <rev> (head)'). --from replays a launch file's exact bytes and resolves
nothing. A driver (--driver) runs from the project's tree the launch was resolved from, its {root}: the worktree you
stand in, or the registered checkout at the line; with --from, a worktree of the launch's project you stand in at
its commit. A member the Store lacks is built first by submitting its producing check (ci.run) to the daemon; with
--no-rebuild the play refuses instead, naming the check, and the session is not evidence.

The instance is built from the launch file (lab.build: one template per launch file, base and params, reused when it
is already built), then the session runs (lab.run) under the GPU lease in a lane of its own, on this machine, as
wb-host would run them: the job id, launch file, instance and session folder are printed at once, the consoles are
streamed, and a failure prints the decisive line, the crash report and the next command. With --desktop hidden
(Windows) the client runs on a hidden desktop of its own that never takes the input, so a session, an open one
included, never takes the person's focus; an open session's commands.txt then reaches the client's fixture by its
'client <action> [<JSON>]' lines.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from pathlib import Path

TARGET = re.compile(r"^(?P<project>[a-z0-9][a-z0-9._-]*)?(?:@(?P<rev>[^:]+))?(?::(?P<launch>[A-Za-z0-9._-]+))?$")


def say(text: str):
    print(f"wb: {text}", file=sys.stderr, flush=True)


def parse_target(text: str | None) -> dict:
    match = TARGET.match(text or "")
    if not match:
        raise ValueError(f"{text!r} is not [project][@ref|@set|@head][:launch]")
    return {"project": match.group("project"), "rev": match.group("rev"), "launch": match.group("launch") or "default"}


def set_line(revision: str, head: str | None, behind: int | None, here: bool) -> str:
    """The resolution line: which set revision a play resolves at, and why."""
    if not here or revision == head:
        return f"set {revision[:7]} (head)"
    return f"set {revision[:7]} (this folder, {behind if behind is not None else '?'} behind)"


def pairs(items: list[str] | None, what: str) -> dict:
    out = {}
    for item in items or []:
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise ValueError(f"{what} {item!r} is not key=value")
        try:
            out[key] = json.loads(value)
        except ValueError:
            out[key] = value
    return out


# --- running a host routine here, as wb-host runs it ------------------------------------------------------------------


class Host:
    """This machine's settings and the lab module, for host routines run in this process."""

    def __init__(self):
        from wb_core.manifest import discover
        from wb_core.service import REPO_ROOT
        from wb_core.settings import resolve

        from . import config as wbconfig
        lab_path = str(REPO_ROOT / "modules" / "lab")
        if lab_path not in sys.path:
            sys.path.insert(0, lab_path)
        self.modules = {m.name: m for m in discover([REPO_ROOT / "modules"])}
        self.settings = resolve(wbconfig.load(), {name: m.config for name, m in self.modules.items()})
        from wb_host import lease
        self.lease = lease
        self.lock, self.coordination, self.aliases = lease.paths(self.settings)
        lab = self.settings.lab
        home = Path(os.path.expanduser(lab.get("home") or self.settings.runner["lab_home"]))
        self.lab_home = home if home.is_absolute() else (REPO_ROOT / home).resolve()
        os.environ.setdefault("WB_LAB_HOME", str(self.lab_home))
        os.environ["WB_LEASE_FILE"] = str(self.lock)
        os.environ["WB_COORDINATION_DIR"] = str(self.coordination)

    def context(self, job: str, command: str, params: dict, folder: Path, printer=None):
        from wb_core.module import Context

        def emit(event: dict):
            kind = event.get("kind")
            if kind == "log" and event.get("level") in ("INFO", "WARNING", "ERROR"):
                say(event.get("msg", ""))
            elif kind == "progress" and event.get("msg"):
                say(f"{event.get('phase')}: {event['msg']}")
            elif kind == "finding":
                say(f"FINDING: {event.get('summary')}")
            if printer:
                printer(event)
        spec = {"job": job, "command": command, "module": "lab", "params": params, "machine": "host",
                "module_config": self.settings.modules.get("lab") or {}, "lab": self.settings.lab,
                "work": str(folder / "work"), "out": str(folder / "out")}
        return Context(spec, emit)


def tail(path: Path, label: str, stop: threading.Event):
    """Prints a console log as it grows, until ``stop``."""
    while not path.exists() and not stop.is_set():
        stop.wait(0.2)
    if stop.is_set() and not path.exists():
        return
    with open(path, encoding="utf-8", errors="replace") as stream:
        while True:
            line = stream.readline()
            if line:
                print(f"[{label}] {line.rstrip()}", file=sys.stderr, flush=True)
            elif stop.is_set():
                return
            else:
                stop.wait(0.2)


# --- resolving ------------------------------------------------------------------------------------------------------


def resolve_launch(host: Host, target: dict, job: str, folder: Path, rebuild: bool) -> tuple[Path, Path]:
    """lab.resolve at the folder's .set (or the head), building a member the Store lacks first, unless not asked.
    Returns the launch file and the project's tree it was resolved from (a driver's {root})."""
    from wb_core.module import ParamError
    from wb_lab import resolve as resolver

    from . import setwork
    from .workspace import Workspace, WorkspaceError, git
    workspace = Workspace()
    here = Path.cwd()
    try:
        project, top = workspace.project_for(here)
    except WorkspaceError:
        project, top = None, None
    if target["project"] and (project is None or project.name != target["project"]):
        named = workspace.projects().get(target["project"])
        if named is None:
            raise ValueError(f"no registered project {target['project']}")
        project, top = named, named.repo  # outside its worktrees: its registered checkout, read at the set's line
    if project is None:
        raise ValueError(f"{here} is in no registered project's worktree; name one (wb play riven) or give --from")
    if target["rev"] not in (None, "set", "head"):
        raise ValueError(f"@{target['rev']}: play a ref from a worktree at it (wb work start <branch> "
                         f"{project.name}), or replay its launch file with --from")
    head = setwork.head(workspace)
    found = setwork.find(top)
    if target["rev"] == "head" or found is None:
        revision, behind, own = head, None, False
    else:
        revision, own = found[1]["set"], True
        count = git(workspace.set_home(), "rev-list", "--count", f"{revision}..{head}", check=False)
        behind = int(count) if count and count.isdigit() else None
    if revision:
        say(set_line(revision, head, behind, own))
    if top == project.repo and revision and not (project.common and Path.cwd().resolve().is_relative_to(top)):
        # outside the project's worktrees: its line in the set, which its registered checkout must have checked out
        line = json.loads(git(workspace.set_home(), "show", f"{revision}:set.json") or "{}")
        commit = ((line.get("projects") or {}).get(project.name) or {}).get("commit")
        at = git(top, "rev-parse", "HEAD", check=False)
        if commit and at != commit:
            raise ValueError(f"{project.name}@set is {commit[:12]}, and {top} has {str(at)[:12]} checked out; play "
                             f"from a worktree at the line (wb work start <branch> {project.name}), or --from")
    out = host.lab_home / "launch" / f"{project.name}-{target['launch']}-{job}.json"
    for attempt in (1, 2):
        ctx = host.context(job, "lab.resolve", {}, folder / f"resolve-{attempt}")
        try:
            result = resolver.resolve_command(ctx, str(top), launch=target["launch"], out=str(out))
            return Path(result["launch"]), Path(top)
        except ParamError as error:
            missing = getattr(error.__cause__, "missing", None)
            if missing is None or attempt == 2:
                raise
            name, commit = missing
            if not rebuild:
                raise ValueError(f"{error}; --no-rebuild builds nothing: run wb check in {name} at {commit[:12]}, "
                                 "then play again") from None
            build_member(name, commit)
    raise AssertionError("unreachable")


def replay_root(doc: dict) -> Path | None:
    """For --from: the worktree you stand in when it is one of the launch's project, else None."""
    from .workspace import Workspace, WorkspaceError
    try:
        project, top = Workspace().project_for(Path.cwd())
    except WorkspaceError:
        return None
    return Path(top) if project is not None and project.name == doc.get("project") else None


def driver_root(doc: dict, top: Path | None, head: str | None) -> Path:
    """The tree a driver runs from ({root} and its working folder): the project's worktree the launch describes.
    Refused when there is none, or when its HEAD is another commit than the launch's (the driver's code would not
    be the commit the session records)."""
    if top is None:
        raise ValueError(f"--driver runs the program from {doc.get('project')}'s tree: play from a worktree of "
                         f"{doc.get('project')} at {str(doc.get('commit'))[:12]}")
    commit = str(doc.get("commit") or "")
    if commit and head and head != commit:
        raise ValueError(f"--driver: {top} has {head[:12]} checked out, and the launch describes {commit[:12]}; "
                         "play from a worktree at the launch's commit")
    return top


def build_member(project: str, commit: str):
    """Submits the producing check of a member the Store lacks and waits for it."""
    from .cli import _client
    client = _client()
    job = client.submit("ci.run", {"project": project, "commit": commit})
    say(f"{job.id}  ci.run {project} at {commit[:12]}: the Store lacks a member it makes (wb show {job.id})")
    envelope = job.wait(None)
    if envelope.get("outcome") != "ok":
        raise ValueError(f"{project} at {commit[:12]} did not build: {envelope.get('summary')}")


# --- the verb ---------------------------------------------------------------------------------------------------------


def main(args) -> int:
    t0 = time.monotonic()
    try:
        target = parse_target(args.target)
        params, view = pairs(args.param, "--param"), pairs(args.view, "--view")
    except ValueError as error:
        say(str(error))
        return 2
    if args.measure and not args.tour:
        say("--measure plays a tour: give --tour <file>")
        return 2
    if args.measure and (args.debug or args.no_rebuild):
        say("--measure refuses a session that is not evidence (--debug, --no-rebuild)")
        return 2
    host = Host()
    from wb_core import process as owned_processes
    from wb_core.module import EnvironmentProblem, ParamError
    # As a worker does: the game and everything it starts die with this process, a kill of wb play included.
    owned_processes.own_tree()
    from wb_lab import instance as instances
    from wb_lab import run as runs
    session = runs.new_id()
    job = "p_" + session.replace("-", "")
    folder = host.lab_home / "play" / session
    try:
        if args.from_file:
            launch = Path(args.from_file).resolve()
            say(f"launch {launch} (replayed as it is; nothing resolved)")
            doc = json.loads(launch.read_text(encoding="utf-8"))
            top = replay_root(doc) if args.driver else None
        else:
            launch, top = resolve_launch(host, target, job, folder, rebuild=not args.no_rebuild)
            doc = json.loads(launch.read_text(encoding="utf-8"))
        if args.driver:
            from .workspace import git
            root = driver_root(doc, top, git(top, "rev-parse", "HEAD", check=False) if top else None)
    except (ParamError, EnvironmentProblem, ValueError, OSError) as error:
        say(f"refused: {error}")
        return 2 if isinstance(error, (ParamError, ValueError)) else 3
    name = args.name or f"{doc['project']}-{doc['launch']}".lower()
    kind = "measure" if args.measure else "tour" if args.tour else "driver" if args.driver else "open"
    run_params = {"instance": name, "kind": kind, "session": session, "jvm": list(args.jvm or []), "view": view,
                  "edits": list(args.edit or []), "window": args.window or "", "debug": args.debug or "",
                  "fresh_world": bool(args.fresh_world), "no_rebuild": bool(args.no_rebuild),
                  "desktop": getattr(args, "desktop", None) or ""}
    if args.tour:
        run_params["tour"] = str(Path(args.tour).resolve())
    if args.measure:
        run_params["launch"] = str(launch)
    if args.driver:
        run_params["driver"] = args.driver
        run_params["driver_root"] = str(root)
    ctx = host.context(job, "lab.run", run_params, folder / "run")
    sessions = runs.sessions_root(ctx)
    print(f"job       {job}")
    print(f"launch    {launch}")
    print(f"instance  {instances._lab_root(ctx, instances.Machine()) / name}")
    print(f"session   {sessions / session}  ({kind}" + (", hidden desktop" if run_params["desktop"] else "") + ")",
          flush=True)
    if kind == "open":
        print(f"commands  {sessions / session / 'commands.txt'}  (server console lines; 'client <action> [<JSON>]' "
              "for the client's fixture)", flush=True)
    try:
        built = instances.build(host.context(job, "lab.build", {}, folder / "build"), str(launch), name,
                                params=params, configure=str(Path(args.configure).resolve()) if args.configure else None)
    except (ParamError, EnvironmentProblem) as error:
        say(f"refused: {error}")
        return 2 if isinstance(error, ParamError) else 3
    how = "built from a new template" if built["built"] else "reused" if built["reused"] else "made from the template"
    say(f"instance {name}: {how} {built['template']}")
    stop = threading.Event()
    tails = [threading.Thread(target=tail, args=(sessions / session / f"{side}-console.log", side, stop), daemon=True)
             for side in ("server", "client")]
    for thread in tails:
        thread.start()
    lane = f"play-{session}"
    asked = time.monotonic()
    cancelled = threading.Event()
    held = None
    try:
        held = host.lease.acquire(host.lock, host.coordination, note=f"wb play {doc['project']}:{doc['launch']} "
                                  f"{kind}", job=job, cancelled=cancelled, lane_name=lane,
                                  report=lambda why: say(f"lease: {why}"), poll=1.0)
        lease_wait = time.monotonic() - asked
        token = held[2]
        for name_ in ["WB_TURN_TOKEN", *host.aliases["token"]]:
            os.environ[name_] = token
        for name_ in ["WB_TURN_LANE", *host.aliases["lane"]]:
            os.environ[name_] = lane
        began = time.monotonic()
        try:
            result = runs.run(ctx, **run_params)
        except (ParamError, EnvironmentProblem) as error:
            say(f"refused: {error}")
            return 2 if isinstance(error, ParamError) else 3
    except KeyboardInterrupt:
        cancelled.set()
        say("interrupted")
        return 4
    finally:
        if held is not None:
            host.lease.release(held[0], held[1], host.coordination)
        for name_ in ["WB_TURN_TOKEN", "WB_TURN_LANE", *host.aliases["token"], *host.aliases["lane"]]:
            os.environ.pop(name_, None)
        stop.set()
        for thread in tails:
            thread.join(2)
    record = json.loads((Path(result["folder"]) / runs.SESSION).read_text(encoding="utf-8"))
    jvm = record["timings"].get("server_jvm")
    if jvm is not None:
        print(f"timing    {began - t0 - lease_wait + jvm:.2f} s from wb play to the server's JVM start: resolve and "
              f"build {asked - t0:.2f} s, the session's receipt check {record['timings'].get('verified', 0):.2f} s "
              f"(the lease wait, {lease_wait:.2f} s, not counted)")
    failure = record.get("failure")
    if failure:
        print(f"FAILED    {failure['summary']}")
        if failure.get("line"):
            print(f"  line    {failure['line']}")
        if failure.get("crash_report"):
            print(f"  crash   {failure['crash_report']}")
        print(f"  next    {failure['next']}")
        return 1
    if record.get("tour"):
        print(f"tour      {record['tour']['captured']} of {record['tour']['views']} views captured: "
              f"{record['tour']['folder']}")
    if record.get("debug"):
        print(f"debug     attach with {record['debug']['attach']} (not evidence)")
    print(f"ok        session {session} {record['state']}; {Path(result['folder']) / runs.SESSION}")
    return 0 if ctx.outcome == "ok" else 1
