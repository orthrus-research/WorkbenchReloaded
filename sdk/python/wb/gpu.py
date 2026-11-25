"""``wb gpu``: the GPU lease on this machine, and a turn under it for any command.

    wb gpu [--json]                                     the holder, the waiters in the order they are served, stuck lanes
    wb gpu run [--lane L] [--note T] [--limit 1h] [--detach] [--quiet] -- <command ...>
    wb gpu leave <lane>                                 withdraw a lane's request (or clear a stale one)
    wb gpu stuck [--json]                               only what needs a person; exit 1 when there is any

A turn waits in its lane until the lease has been free for the cooldown and no live lane waited longer, holds the
lease while the command runs, and releases it. The command gets the turn token in WB_TURN_TOKEN (and the names
[runner] token_env and [lab] env give it), so a launcher under it joins the held lease. A lane keeps its place when
its turn is started again; without --lane, the lane is named after the folder and the command, so the same request
restarted is the same lane. --detach runs the turn as its own process, which outlives the shell (its log is under
<coordination>/logs).
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path


def _lease():
    from wb_host import lease
    return lease


def _paths():
    from wb_core.settings import resolve

    from . import config as wbconfig
    return _lease().paths(resolve(wbconfig.load()))


def default_lane(command: list[str]) -> str:
    digest = hashlib.sha256(json.dumps([str(Path.cwd()), command]).encode("utf-8")).hexdigest()[:8]
    return f"run-{digest}"


def show(report: dict, every: bool = False) -> str:
    lease = report["lease"]
    if lease["held"]:
        holder = lease["holder"] or {}
        lines = [f"lease: held by {holder.get('lane') or holder.get('name') or 'an unnamed holder'} (pid "
                 f"{holder.get('pid')}, since {holder.get('startedUtc')})"
                 + (f": {holder['note']}" if holder.get("note") else "")]
    else:
        last = lease.get("last") or {}
        free = "" if lease["free_for"] is None else f" {lease['free_for']} s"
        lines = [f"lease: free{free}" + (f" (last: {last.get('lane') or last.get('name')})" if last else "")]
    if report["waiters"]:
        lines.append("waiting, in the order they are served:")
        for index, name in enumerate(report["waiters"], start=1):
            lane = report["lanes"][name]
            lines.append(f"  {index}. {name:<20} since {lane['waiting_since']}  {lane['note'] or ''}".rstrip())
    others = [n for n in report["lanes"] if n not in report["waiters"]]
    quiet = [n for n in others if not every and not report["lanes"][n]["live"]
             and str(report["lanes"][n]["state"] or "") == "idle"]
    others = [n for n in others if n not in quiet]
    if others:
        lines.append("lanes:")
        for name in others:
            lane = report["lanes"][name]
            lines.append(f"  {name:<20} {str(lane['state']):<8} {'live' if lane['live'] else 'stale':<5} "
                         f"beat {lane['heartbeat_age']} s  {lane['note'] or ''}".rstrip())
    if quiet:
        lines.append(f"{len(quiet)} idle lane(s) with no live process not shown (wb gpu --all); each is removed once "
                     "it has been silent a day")
    for issue in report["stuck"]:
        lines.append(f"stuck: {issue['lane']}: {issue['problem']} (fix: {issue['fix']})")
    return "\n".join(lines)


def main(args) -> int:
    from .cli import _duration
    lease = _lease()
    lock, coordination, aliases = _paths()
    action = args.action or "status"
    if action == "status":
        report = lease.status(lock, coordination)
        print(json.dumps(report, indent=1, default=str) if args.json else show(report, getattr(args, "all", False)))
        return 0
    if action == "stuck":
        issues = lease.stuck(lock, coordination)
        print(json.dumps(issues, indent=1) if args.json else
              "\n".join(f"{i['lane']}: {i['problem']} (fix: {i['fix']})" for i in issues) or "nothing is stuck")
        return 1 if issues else 0
    rest = list(args.rest or [])
    if action == "leave":
        if len(rest) != 1:
            print("wb: wb gpu leave <lane>", file=sys.stderr)
            return 2
        lease.leave(coordination, rest[0])
        print(f"lane {rest[0]} left the queue")
        return 0
    split = rest.index("--") if "--" in rest else 0
    own, command = rest[:split], rest[split + 1:] if "--" in rest else rest
    if own:  # the turn's options may follow "run": wb gpu run --note T -- <command>
        import argparse
        parser = argparse.ArgumentParser(prog="wb gpu run")
        for flag in ("--lane", "--note", "--limit"):
            parser.add_argument(flag)
        parser.add_argument("--detach", action="store_true")
        parser.add_argument("--quiet", action="store_true")
        given = parser.parse_args(own)
        for key, value in vars(given).items():
            if value not in (None, False):
                setattr(args, key, value)
    if not command:
        print("wb: wb gpu run [--note T] [--limit D] [--detach] [--quiet] -- <command ...>", file=sys.stderr)
        return 2
    lane = args.lane or default_lane(command)
    limit = _duration(args.limit) if args.limit else lease.LIMIT
    if args.detach:
        own = ["--lane", lane, "--limit", str(limit)] + (["--note", args.note] if args.note else []) \
            + (["--quiet"] if args.quiet else [])
        argv = [sys.executable, "-m", "wb", "gpu", "run", *own, "--", *command]
        print(json.dumps(lease.detach(argv, coordination, lane, cwd=Path.cwd())))
        return 0
    return lease.turn(lock, coordination, lane, command, note=args.note or "", limit=limit, quiet=args.quiet,
                      token_env=aliases["token"], lane_env=aliases["lane"],
                      log=lambda line: print(f"wb gpu: {line}", file=sys.stderr, flush=True))
