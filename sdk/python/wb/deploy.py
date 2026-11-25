"""``wb deploy``: brings a Workbench commit into service on this machine, daemon and host runner together.

    wb deploy                       gate, release, image if its inputs changed, drain, restart both, check, record
    wb deploy --commit <sha>        the same for another commit (a clean export: the checkout is never switched);
                                    wb deploy --commit <previous> is the undo
    wb deploy --wait 20m            wait longer for running jobs to end (default 10m)
    wb deploy --now                 cancel what still runs after the wait, listing it first
    wb deploy --override "<why>"    skip the gate (a broken Workbench must be able to ship its own fix)

The gate has two parts, both recorded in the release's RELEASE.json. The container pass: CI's records must show the
commit's tree passed the checks its project.toml requires (``wb check`` at that commit runs them). The host suite:
tests/run_all.py on this machine, once per tree; a pass is kept in the machine's deploy record, so a tree that
already passed runs nothing. A part of the suite that is one of the project's host checks (``on = "host"``) and that
CI's records show passed on this tree with ``ci.host-run`` (``wb check core modules``, from a clean clone on this
machine) is not run again: only the parts without such a pass run here.

The container imports Workbench from a copy of the release in its state volume (image/code.sh), so the image is
built only when its own inputs changed (``compose.image_inputs``, compared with the digest the image is labelled
with); a Python-only deploy is a restart. While draining, no new root job starts; queued jobs stay queued and run
after the restart, and nothing running is cancelled unless --now says so.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import compose
from . import config as wbconfig
from .client import Client, WbError

TASK = "Workbench host runner"
IMAGE = "wb-core:dev"


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
    from wb_core import filelock
    filelock.write_json(_record_file(config), records)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True).stdout.strip()


# -- what is deployed -----------------------------------------------------------------------------------------------
def target(root: Path, commit: str | None) -> dict:
    """The commit to deploy: ``commit`` resolved in the checkout's repository, else the checkout's HEAD (``-dirty``
    when tracked files differ from it). {commit, sha, tree, head}: head is True for the checkout's own HEAD."""
    if commit:
        done = subprocess.run(["git", "-C", str(root), "rev-parse", "--verify", "-q", f"{commit}^{{commit}}"],
                              capture_output=True, text=True)
        if done.returncode != 0 or not done.stdout.strip():
            raise ValueError(f"{commit} is not a commit in {root}'s repository (fetch it first)")
        sha = done.stdout.strip()
        return {"commit": sha, "sha": sha, "tree": _git(root, "rev-parse", f"{sha}^{{tree}}"), "head": False}
    from wb_core.identity import source_commit
    found = source_commit(root) or ""
    sha = found.split("-")[0]
    if not sha:
        raise ValueError(f"{root} has no commit")
    return {"commit": found, "sha": sha, "tree": _git(root, "rev-parse", f"{sha}^{{tree}}"), "head": True}


# -- the gate -------------------------------------------------------------------------------------------------------
def container_pass(config, root: Path, sha: str) -> dict:
    """What CI's records say about ``sha``'s tree, read on the host with no daemon: {passed, project, required,
    runs, reason}. ``runs`` names the recorded runs of that tree that passed every required check."""
    from .workspace import Workspace, WorkspaceError
    answer = {"passed": False, "project": None, "required": [], "runs": [], "reason": None}
    try:
        workspace = Workspace(config)
        project = workspace.find(root)
        if project is None:
            answer["reason"] = (f"{root} is not a registered project, so CI keeps no record of its trees; register "
                                "its repository under [modules.ci.projects]")
            return answer
        answer["project"] = project.name
        actions = workspace.engine()
        with tempfile.TemporaryDirectory(prefix="wb-deploy-") as scratch:
            jobs = workspace.config_at(project, sha, Path(scratch))
            if jobs is None:
                answer["reason"] = f"{sha[:12]} has no {workspace.jobs_name(project)}, so it names no checks"
                return answer
            answer["required"] = list(jobs.required())
            refused = actions.gate_check(jobs, jobs.names[0], [f"refs/heads/deploy {sha} refs/heads/deploy {'0' * 40}"])
            runs, names = jobs.runs, tuple(jobs.names)
    except (WorkspaceError, OSError, subprocess.SubprocessError) as error:
        answer["reason"] = f"CI's records could not be read: {error}"
        return answer
    except Exception as error:  # the engine's own errors (actions.ToolError): a bad jobs file
        answer["reason"] = f"the jobs file could not be read: {error}"
        return answer
    if refused:
        answer["reason"] = refused[0]["reason"]
        return answer
    answer["passed"] = True
    tree = _git(root, "rev-parse", f"{sha}^{{tree}}")
    answer["runs"] = [run for run, passed in _passing_runs(Path(runs), tree, names)
                      if set(answer["required"]) <= passed]
    return answer


def _passing_runs(runs: Path, tree: str, names) -> list[tuple[str, set[str]]]:
    """Each recorded run that vouches for ``tree``, oldest first, with the checks it passed. A run vouches as the
    engine's passed_jobs says: it recorded every tree its jobs file names (``names``, the project's first), and none
    of them changed while it ran."""
    found = []
    for summary in sorted(runs.glob("*/summary.json")):
        try:
            data = json.loads(summary.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        tested = data.get("trees") or {}
        if data.get("treesChanged", True) or not names or not all(tested.get(n) for n in names) \
                or tested.get(names[0]) != tree:
            continue
        found.append((summary.parent.name, {n for n, r in (data.get("jobs") or {}).items()
                                            if isinstance(r, dict) and r.get("status") == "passed"}))
    return found


def host_passes(config, root: Path, sha: str) -> dict:
    """The project's host checks (``on = "host"``) at ``sha`` that CI's records show passed on its tree: {checks:
    [the host checks], passed: [those with a pass], runs: [the runs that passed them]}. Empty when the records or the
    jobs file cannot be read: the suite then runs whole."""
    from .workspace import Workspace
    answer = {"checks": [], "passed": [], "runs": []}
    try:
        workspace = Workspace(config)
        project = workspace.find(root)
        if project is None:
            return answer
        actions = workspace.engine()
        with tempfile.TemporaryDirectory(prefix="wb-deploy-") as scratch:
            jobs = workspace.config_at(project, sha, Path(scratch))
            if jobs is None:
                return answer
            answer["checks"] = [name for name, check in jobs.jobs.items() if actions.on_host(check)]
            runs, names = Path(jobs.runs), tuple(jobs.names)
        tree = _git(root, "rev-parse", f"{sha}^{{tree}}")
        for run, passed in _passing_runs(runs, tree, names):
            new = [name for name in answer["checks"] if name in passed and name not in answer["passed"]]
            if new:
                answer["passed"] += new
                answer["runs"].append(run)
    except Exception:  # noqa: BLE001 - what cannot be read is no pass: the whole suite runs
        return {"checks": [], "passed": [], "runs": []}
    return answer


def _remove(folder: Path):
    def writable(function, path, _):
        os.chmod(path, stat.S_IWRITE)
        function(path)
    shutil.rmtree(folder, onerror=writable)


def suite_parts(root: Path, sha: str) -> tuple[str, ...] | None:
    """The parts the tree's own tests/run_all.py runs (its PARTS); None when they cannot be read, and the whole
    suite runs."""
    import ast
    import re
    done = subprocess.run(["git", "-C", str(root), "show", f"{sha}:tests/run_all.py"], capture_output=True,
                          text=True, encoding="utf-8", errors="replace")
    found = re.search(r"^PARTS\s*=\s*(\([^)]*\))", done.stdout, re.MULTILINE) if done.returncode == 0 else None
    try:
        parts = ast.literal_eval(found.group(1)) if found else None
    except (ValueError, SyntaxError):
        return None
    return tuple(parts) if isinstance(parts, tuple) and all(isinstance(p, str) for p in parts) and parts else None


def host_suite(root: Path, sha: str, head: bool, parts: list[str] | None = None) -> dict:
    """Runs tests/run_all.py on this machine (only ``parts``, when given): in the checkout for its own HEAD, else in
    a shared clone at ``sha`` (its own folder, removed after), so the checkout's branch and files never change."""
    started = time.time()
    chosen = [arg for part in (parts or []) for arg in ("--part", part)]
    if head:
        done = subprocess.run([sys.executable, "-u", str(root / "tests" / "run_all.py"), *chosen], cwd=root)
    else:
        scratch = Path(tempfile.mkdtemp(prefix="wb-deploy-suite-"))
        try:
            clone = scratch / "workbench"
            subprocess.run(["git", "clone", "-q", "--shared", "--no-checkout", str(root), str(clone)], check=True)
            subprocess.run(["git", "-C", str(clone), "-c", "core.autocrlf=false", "checkout", "-q", "--detach", sha],
                           check=True)
            done = subprocess.run([sys.executable, "-u", str(clone / "tests" / "run_all.py"), *chosen], cwd=clone)
        finally:
            _remove(scratch)
    return {"passed": done.returncode == 0, "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "seconds": round(time.time() - started), **({"parts": list(parts)} if parts else {})}


def gate(config, root: Path, deployed: dict, records: dict, override: str | None) -> tuple[bool, dict]:
    """Both parts of the gate for ``deployed``: (deployable, {ci, tests}). With an override, nothing runs."""
    tree = deployed["tree"]
    ci = container_pass(config, root, deployed["sha"])
    if override:
        _say(f"the gate is skipped: {override}")
        return True, {"ci": ci, "tests": records["tested"].get(tree)}
    if not ci["passed"]:
        _say(f"the container pass is missing for tree {tree[:12]}: {ci['reason']}; run `wb check` in a worktree at "
             f"{deployed['sha'][:12]}, then deploy again (or deploy with --override)")
        return False, {"ci": ci, "tests": records["tested"].get(tree)}
    _say(f"tree {tree[:12]} passed {', '.join(ci['required']) or 'its checks'} in the container"
         + (f" (run {ci['runs'][-1]})" if ci["runs"] else ""))
    tested = records["tested"].get(tree)
    if tested and tested.get("passed"):
        _say(f"tree {tree[:12]} passed the host suite at {tested['at']}")
        return True, {"ci": ci, "tests": tested}
    host = host_passes(config, root, deployed["sha"])
    every = suite_parts(root, deployed["sha"]) if host["passed"] else None
    recorded = [part for part in every or () if part in host["passed"]]
    if recorded:
        parts = [part for part in every if part not in recorded]
        _say(f"tree {tree[:12]} passed {', '.join(recorded)} on this host (run {', '.join(host['runs'])}), so the "
             f"host suite runs only {', '.join(parts) or 'nothing'}")
        tested = host_suite(root, deployed["sha"], deployed["head"], parts) if parts else \
            {"passed": True, "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "seconds": 0, "parts": []}
        tested["host_runs"] = host["runs"]
    else:
        _say(f"running the host suite on tree {tree[:12]} (about 15 minutes; `wb check {' '.join(host['checks'])}` "
             "records the host checks' passes first, so a deploy runs only the rest; --override skips it)"
             if host["checks"] else f"running the host suite on tree {tree[:12]} (about 15 minutes; --override "
                                    "skips it)")
        tested = host_suite(root, deployed["sha"], deployed["head"])
    records["tested"][tree] = tested
    _save(config, records)
    if not tested["passed"]:
        _say("the host suite failed, so nothing was deployed; fix it, or deploy with --override")
        return False, {"ci": ci, "tests": tested}
    return True, {"ci": ci, "tests": tested}


# -- the image ------------------------------------------------------------------------------------------------------
def image_step(release: Path, tools: list[str], tag: str = IMAGE) -> dict:
    """Builds the image from the release only when its inputs changed: {digest, built, reason, ok}."""
    digest = compose.inputs_digest(release, tools)
    reason = compose.build_reason(tag, digest, compose.image_label(tag))
    if reason is None:
        _say(f"the image's inputs are unchanged ({digest[:12]}): no image build")
        return {"digest": digest, "built": False, "reason": None, "ok": True}
    _say(f"building the image: {reason}")
    ok = compose.build(tag, tools, root=release) == 0
    return {"digest": digest, "built": True, "reason": reason, "ok": ok}


# -- the processes --------------------------------------------------------------------------------------------------
def _restart_host_runner() -> str:
    if os.name != "nt":
        return "not Windows: restart the host runner with `wb host serve` where it runs"
    script = (f"Stop-ScheduledTask -TaskName '{TASK}' -ErrorAction SilentlyContinue; Start-Sleep -Seconds 2; "
              f"Start-ScheduledTask -TaskName '{TASK}'")
    done = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                          capture_output=True, text=True)
    return "restarted" if done.returncode == 0 else f"could not restart it: {(done.stderr or done.stdout).strip()}"


def _task_script() -> str | None:
    """The script the host runner's scheduled task starts (Windows), or None."""
    if os.name != "nt":
        return None
    done = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                           f"(Get-ScheduledTask -TaskName '{TASK}' -ErrorAction SilentlyContinue).Actions.Arguments"],
                          capture_output=True, text=True)
    return done.stdout.strip().strip('"') or None


def _point_hooks(wanted: str):
    if subprocess.run(["git", "config", "--global", "--get", "wb.cli"], capture_output=True,
                      text=True).stdout.strip() != wanted:
        subprocess.run(["git", "config", "--global", "wb.cli", wanted], check=True)
        _say(f"the hooks now run the release in service: git config --global wb.cli = {wanted}")


def _running(client: Client) -> list:
    return [j for j in client.jobs(state="running", limit=200)]


def drain(client: Client, wait: str, now: bool) -> bool:
    """Holds new root jobs and waits for running ones; False (and the drain lifted) when some still run without
    --now."""
    try:
        client.request("POST", "/v1/drain", {"on": True})
    except WbError as error:
        if error.status == 404:  # a daemon from before drains: it cannot hold new jobs, but it can list them
            _say("the running daemon has no drain; it is restarted only when nothing runs (or with --now)")
        else:
            _say(f"the daemon does not answer ({error}); starting it fresh")
            return True
    deadline = time.time() + _duration(wait)
    running = _running(client)
    if running:
        _say(f"waiting up to {wait} for {len(running)} running job(s): "
             + ", ".join(f"{j['job']} {j['command']}" for j in running))
    while running and time.time() < deadline:
        time.sleep(3)
        running = _running(client)
    if running:
        if not now:
            client.request("POST", "/v1/drain", {"on": False})
            _say("still running, so nothing was deployed: " + ", ".join(f"{j['job']} {j['command']}" for j in running)
                 + "; deploy later, wait longer with --wait, or cancel them with --now")
            return False
        for job in running:
            _say(f"cancelling {job['job']} {job['command']}")
            client.job(job["job"]).cancel()
        end = time.time() + 180
        while _running(client) and time.time() < end:
            time.sleep(2)
    return True


def main(args) -> int:
    root = compose.repo_root()
    if root is None:
        _say("needs a Workbench checkout")
        return 3
    if not (root / ".git").exists():
        _say(f"{root} is a release, not a checkout; run wb deploy from the Workbench checkout to deploy its commit")
        return 2
    config = wbconfig.load()
    from wb_core.identity import short
    try:
        deployed = target(root, getattr(args, "commit", None))
    except (ValueError, subprocess.CalledProcessError) as error:
        _say(str(error))
        return 2
    commit, tree = deployed["commit"], deployed["tree"]
    records = _records(config)
    _say(f"deploying {short(commit)} from {root}" + ("" if deployed["head"] else " (an export; the checkout is not "
                                                                                  "switched)"))

    # 1. The gate.
    override = args.override
    if commit.endswith("-dirty") and not override:
        _say("the checkout has uncommitted changes to tracked files; commit them, deploy a commit with --commit, or "
             "deploy with --override")
        return 2
    deployable, results = gate(config, root, deployed, records, override)
    if not deployable:
        return 1

    # 2. The release: an immutable copy of the commit, which the hooks, the CLI, the host runner and (copied into
    # its state volume) the daemon run.
    from . import releases
    tools = [str(t) for t in (config.get("core.image.tools", []) or [])]
    sha = deployed["sha"]
    release = releases.make(config, root, sha, {"tree": tree, "override": override, "tests": results["tests"],
                                                "ci": results["ci"]})
    _say(f"release {release}")

    # 3. The image, only when its inputs changed.
    image = image_step(release, tools)
    if not image["ok"]:
        _say("the image did not build; nothing was deployed")
        return 3

    # 4. The drain.
    client = Client()
    if not drain(client, args.wait, args.now):
        return 1

    # 5. Both processes, together.
    _say("restarting the daemon on the release")
    compose.down(config)
    try:
        health = compose.up(config, release=release)
    except (compose.ComposeError, wbconfig.ConfigError) as error:
        _say(f"the daemon did not come back: {error}")
        return 3
    before = releases.point(config, release)
    _point_hooks(releases.cli_command(config))
    _say(f"current release: {release.name}" + (f" (was {before.name})" if before and before != release else ""))
    task = _task_script()
    if task is not None and not task.startswith(str(releases.current(config))):
        _say(f"the host runner's task still starts {task}; point it at the release once with "
             f"`{releases.current(config) / 'bin' / ('wb.cmd' if os.name == 'nt' else 'wb')} host install --yes`")
    _say(f"the host runner: {_restart_host_runner()}")
    host = None
    for _ in range(30):
        time.sleep(1)
        try:
            health = Client().health()
            host = health.get("host") or {}
            if host.get("commit") == sha or os.name != "nt":
                break
        except WbError:
            continue

    # 6. Check and record: a release is the clean commit, whatever the checkout held.
    problems = []
    if health.get("commit") != sha:
        problems.append(f"the daemon runs {short(health.get('commit'))}")
    if os.name == "nt" and (host or {}).get("commit") != sha:
        problems.append(f"the host runner runs {short((host or {}).get('commit'))}")
    previous = (records["releases"] or [{}])[-1].get("commit")
    records["releases"].append({"commit": commit, "tree": tree, "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                "override": override, "previous": previous, "tests": results["tests"],
                                "ci": {k: results["ci"].get(k) for k in ("passed", "runs", "reason")},
                                "image": {k: image[k] for k in ("digest", "built", "reason")},
                                "problems": problems})
    records["releases"] = records["releases"][-100:]
    _save(config, records)  # releases past this one and the one before it are wb gc's (class releases)
    if problems:
        _say("deployed with problems: " + "; ".join(problems))
        return 3
    _say(f"{short(commit)} is in service: daemon and host runner"
         + (f" (was {short(previous)})" if previous and previous != commit else ""))
    return 0
