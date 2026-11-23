"""``wb deploy``: brings a Workbench commit into service on this machine, daemon and host runner together.

    wb deploy                       test, build, drain, restart both, check both run the commit, record it
    wb deploy --wait 20m            wait longer for running jobs to end (default 10m)
    wb deploy --now                 cancel what still runs after the wait, listing it first
    wb deploy --override "<why>"    skip the test gate (a broken Workbench must be able to ship its own fix)

The gate is the test suite of the checkout's tree, run on this machine once per tree; a pass is kept in the
machine's deploy record, so deploying a tree that already passed runs nothing. While draining, no new root job
starts; queued jobs stay queued and run after the restart, and nothing running is cancelled unless --now says so.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from . import compose
from . import config as wbconfig
from .client import Client, WbError

TASK = "Workbench host runner"


def _say(text: str):
    print(f"wb deploy: {text}", file=sys.stderr, flush=True)


def _duration(text: str) -> float:
    units = {"s": 1, "m": 60, "h": 3600}
    return float(text[:-1]) * units[text[-1]] if text and text[-1] in units else float(text)


def _record_file(config) -> Path:
    folder = Path(config.folder or wbconfig.default_folder()) / "run"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / "deploys.json"


def _records(config) -> dict:
    path = _record_file(config)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"tested": {}, "releases": []}


def _save(config, records: dict):
    path = _record_file(config)
    path.with_suffix(".part").write_text(json.dumps(records, indent=1), encoding="utf-8")
    path.with_suffix(".part").replace(path)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True).stdout.strip()


def _restart_host_runner() -> str:
    if os.name != "nt":
        return "not Windows: restart the host runner with `wb host serve` where it runs"
    script = (f"Stop-ScheduledTask -TaskName '{TASK}' -ErrorAction SilentlyContinue; Start-Sleep -Seconds 2; "
              f"Start-ScheduledTask -TaskName '{TASK}'")
    done = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                          capture_output=True, text=True)
    return "restarted" if done.returncode == 0 else f"could not restart it: {(done.stderr or done.stdout).strip()}"


def _running(client: Client) -> list:
    return [j for j in client.jobs(state="running", limit=200)]


def main(args) -> int:
    root = compose.repo_root()
    if root is None:
        _say("needs a Workbench checkout")
        return 3
    config = wbconfig.load()
    from wb_core.identity import short, source_commit
    commit = source_commit(root)
    tree = _git(root, "rev-parse", "HEAD^{tree}")
    records = _records(config)
    _say(f"deploying {short(commit)} from {root}")

    # 1. The gate.
    override = args.override
    if commit and commit.endswith("-dirty") and not override:
        _say("the checkout has uncommitted changes to tracked files; commit them, or deploy with --override")
        return 2
    tested = records["tested"].get(tree)
    if override:
        _say(f"the test gate is skipped: {override}")
    elif tested and tested.get("passed"):
        _say(f"tree {tree[:12]} passed the suite at {tested['at']}")
    else:
        _say(f"running the suite on tree {tree[:12]} (about 15 minutes; --override skips it)")
        started = time.time()
        done = subprocess.run([sys.executable, "-u", str(root / "tests" / "run_all.py")], cwd=root)
        records["tested"][tree] = {"passed": done.returncode == 0, "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                   "seconds": round(time.time() - started)}
        _save(config, records)
        if done.returncode != 0:
            _say("the suite failed, so nothing was deployed; fix it, or deploy with --override")
            return 1

    # 2. The image (Docker reuses every layer the change did not touch).
    _say("building the image")
    tools = [str(t) for t in (config.get("core.image.tools", []) or [])]
    if compose.build("wb-core:dev", tools) != 0:
        _say("the image did not build; nothing was deployed")
        return 3

    # 3. The drain.
    client = Client()
    try:
        client.request("POST", "/v1/drain", {"on": True})
        drained = True
    except WbError as error:
        if error.status == 404:  # a daemon from before drains: it cannot hold new jobs, but it can list them
            _say("the running daemon has no drain; it is restarted only when nothing runs (or with --now)")
            drained = True
        else:
            _say(f"the daemon does not answer ({error}); starting it fresh")
            drained = False
    if drained:
        deadline = time.time() + _duration(args.wait)
        running = _running(client)
        if running:
            _say(f"waiting up to {args.wait} for {len(running)} running job(s): "
                 + ", ".join(f"{j['job']} {j['command']}" for j in running))
        while running and time.time() < deadline:
            time.sleep(3)
            running = _running(client)
        if running:
            if not args.now:
                client.request("POST", "/v1/drain", {"on": False})
                _say("still running, so nothing was deployed: "
                     + ", ".join(f"{j['job']} {j['command']}" for j in running)
                     + "; deploy later, wait longer with --wait, or cancel them with --now")
                return 1
            for job in running:
                _say(f"cancelling {job['job']} {job['command']}")
                client.job(job["job"]).cancel()
            end = time.time() + 180
            while _running(client) and time.time() < end:
                time.sleep(2)

    # 4. Both processes, together.
    _say("restarting the daemon")
    compose.down(config)
    try:
        health = compose.up(config)
    except (compose.ComposeError, wbconfig.ConfigError) as error:
        _say(f"the daemon did not come back: {error}")
        return 3
    _say(f"the host runner: {_restart_host_runner()}")
    host = None
    for _ in range(30):
        time.sleep(1)
        try:
            health = Client().health()
            host = health.get("host") or {}
            if host.get("commit") == commit or os.name != "nt":
                break
        except WbError:
            continue

    # 5. Check and record.
    problems = []
    if health.get("commit") != commit:
        problems.append(f"the daemon runs {short(health.get('commit'))}")
    if os.name == "nt" and (host or {}).get("commit") != commit:
        problems.append(f"the host runner runs {short((host or {}).get('commit'))}")
    previous = (records["releases"] or [{}])[-1].get("commit")
    records["releases"].append({"commit": commit, "tree": tree, "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                "override": override, "previous": previous,
                                "tests": records["tested"].get(tree), "problems": problems})
    records["releases"] = records["releases"][-100:]
    _save(config, records)
    if problems:
        _say("deployed with problems: " + "; ".join(problems))
        return 3
    _say(f"{short(commit)} is in service: daemon and host runner"
         + (f" (was {short(previous)})" if previous and previous != commit else ""))
    return 0
