"""ci.host-run: a project's host checks, run natively on the host from a clean clone, for a trusted project only.

A host check is one its project.toml marks ``on = "host"`` (or ``gpu = true``: only the host has the GPU). It never
runs in the container and is never in the gate, so it runs only when named (``wb check <name>``, or a workflow such
as ``all`` that takes it in). The route is ci.run's on the host side:

- the project is refused before anything runs unless the machine's ``[lab] trusted`` names it: a manifest cannot
  widen what the host runs;
- the commit (or, with commit=worktree, the checkout's working tree) is cloned clean, the files another project lends
  its checks are laid beside the clone, and its steps get the same identities, ``{gradle}`` launcher on the set and
  ``{python}`` as ci.run's, from the Store and folders the machine's roots name as the host sees them;
- the checks without a GPU run first, without the lease; the ``gpu = true`` checks then run under one turn of the
  GPU lease, their steps given the turn token, lane, lock file and coordination folder (``WB_TURN_TOKEN``,
  ``WB_TURN_LANE``, ``WB_LEASE_FILE``, ``WB_COORDINATION_DIR`` and the names ``[lab] env`` and ``[runner] token_env``
  add), so a launcher under them joins the held turn instead of waiting on it;
- the run records go into the project's records folder, where ci.run keeps its own, so a pass is recorded the same
  way (by tree) and ``--if-needed`` skips a check that already passed for it. Nothing is kept in the Store.
"""

from __future__ import annotations

import contextlib
import os
import shutil
from pathlib import Path

from wb_core.module import Context, EnvironmentProblem, ParamError

from . import commands as ci

# What a host check's steps get of this worker's environment beyond a container step's: what a native window and
# driver need (a display on Linux, the session and processor on Windows).
HOST_ENV = ci.STEP_ENV + ("DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "XAUTHORITY", "XDG_SESSION_TYPE",
                          "DBUS_SESSION_BUS_ADDRESS", "OS", "SESSIONNAME", "USERNAME", "USERDOMAIN", "HOMEDRIVE",
                          "HOMEPATH", "PUBLIC", "ALLUSERSPROFILE", "COMMONPROGRAMFILES", "COMMONPROGRAMFILES(X86)",
                          "PROGRAMW6432", "PROCESSOR_IDENTIFIER", "PROCESSOR_LEVEL", "PROCESSOR_REVISION")
TOKEN_ENV, LANE_ENV, LEASE_ENV, COORDINATION_ENV = "WB_TURN_TOKEN", "WB_TURN_LANE", "WB_LEASE_FILE", "WB_COORDINATION_DIR"


def require_trusted(ctx: Context, project: str):
    """Refuses a host check of ``project`` unless the machine's ``[lab] trusted`` names it."""
    from wb_lab import machine
    trusted = machine.lab(ctx)["trusted"]
    if project not in trusted:
        raise ParamError(f"host checks are {project}'s own code run natively, and {project} is not in [lab] trusted on "
                         f"this machine ({', '.join(trusted) or 'none trusted'}); add it there to let its host checks "
                         "run, or run its container checks alone")


def _turn_places(ctx: Context) -> tuple[Path, Path, dict[str, list[str]]]:
    """The lease's lock file and coordination folder (as wb-host names them to its jobs, else as the machine's config
    does), and the extra variable names each value of a turn goes in: ``[lab] env`` and ``[runner] token_env`` and
    ``lane_env``."""
    from wb_lab import machine
    lock, coordination = os.environ.get(LEASE_ENV, ""), os.environ.get(COORDINATION_ENV, "")
    names: dict[str, list[str]] = {"token": [], "lane": [], "lease": [], "coordination": []}
    try:
        from wb import config as wbconfig
        from wb_core.settings import resolve
        from wb_host import lease as leases
        settings = resolve(wbconfig.load())
        configured_lock, configured_coordination, aliases = leases.paths(settings)
        lock, coordination = lock or str(configured_lock), coordination or str(configured_coordination)
        for kind in ("token", "lane"):
            names[kind] += aliases.get(kind) or []
    except Exception as error:  # noqa: BLE001 - no machine config to read: wb-host's names alone
        if not (lock and coordination):
            raise EnvironmentProblem(f"no GPU lease to take: wb-host names none to this job, and the machine's config "
                                     f"cannot be read ({error})") from None
    for name, value in (machine.lab(ctx)["env"] or {}).items():
        if value in names and name not in names[value]:
            names[value].append(name)
    return Path(lock), Path(coordination), names


@contextlib.contextmanager
def _turn(ctx: Context, project: str, checks: list[str]):
    """Waits for a turn of the GPU lease in the lane ``check-<project>``, holds it while the block runs and releases
    it; yields the variables a step joins the turn by."""
    from wb_host import lease as leases
    lock, coordination, names = _turn_places(ctx)
    lane_name = f"check-{project}"
    ctx.progress("lease", msg=f"waiting for the GPU lease for {', '.join(checks)}")
    held, lane, token = leases.acquire(lock, coordination, note=f"wb check {project}: {' '.join(checks)}",
                                       job=ctx.job, cancelled=ctx.cancelled, lane_name=lane_name,
                                       report=lambda why: ctx.log.info(f"lease: {why}"))
    ctx.log.info(f"GPU lease taken in lane {lane_name} ({lock})")
    try:
        env = {}
        for kind, value in (("token", token), ("lane", lane_name), ("lease", str(lock)),
                            ("coordination", str(coordination))):
            default = {"token": TOKEN_ENV, "lane": LANE_ENV, "lease": LEASE_ENV, "coordination": COORDINATION_ENV}
            env.update({name: value for name in [default[kind], *names[kind]]})
        yield env
    finally:
        leases.release(held, lane, coordination)
        ctx.log.info("GPU lease released")


def host_run(ctx: Context, project: str, checks: list, commit: str = "HEAD", if_needed: bool = True,
             keep_going: bool = False, keep: bool = False, dry_run: bool = False, checkout=None, set: str = "",
             stand_ins: dict | None = None):
    checks = [str(name) for name in checks]
    if not checks:
        raise ParamError("name the host checks to run (checks=[...]); wb check <name> splits them out for you")
    entry = ci._project(ctx, project)
    require_trusted(ctx, project)  # before anything is cloned, laid out or run
    work = ci._folder(ctx, "workdir", ci._state() / "ci" / "trees") / f"{project}-{ctx.job}"
    documents: list[dict] = []
    try:
        tree = ci._checkout(ctx, project, entry, commit, checkout, work)
        root, sha, config, records = tree["root"], tree["commit"], tree["config"], tree["records"]
        jobs = config.jobs
        unknown = [name for name in checks if name not in jobs]
        if unknown:
            raise ParamError(f"{project} {sha[:12]} has no check {', '.join(unknown)} (has: {', '.join(jobs)})")
        container = [name for name in checks if not ci.engine.on_host(jobs[name])]
        if container:
            raise ParamError(f"{', '.join(container)} run(s) in the container, not on the host: ci.run runs them "
                             "(wb check splits them out for you)")
        jobs_file = ci._jobs_copy(root, entry, records, work)
        prepared = ci._environment(ctx, entry, config, root, work, set, stand_ins, False)
        if stand_ins:
            records = records / "stand-ins"  # against stand-in bytes a run vouches for nothing (see ci.run)
            records.mkdir(parents=True, exist_ok=True)
        plain = [name for name in checks if not jobs[name].get("gpu")]
        gpu = [name for name in checks if jobs[name].get("gpu")]
        groups = [(names, lease) for names, lease in ((plain, False), (gpu, True)) if names]
        for index, (names, lease) in enumerate(groups):
            run_id = ctx.job if index == 0 else f"{ctx.job}-gpu"
            argv = ci._engine_argv(ctx, names, run_id, records, if_needed, keep_going, dry_run)
            if lease and if_needed and not dry_run:
                # No turn of the GPU only to find the checks passed: the engine's plan says what is left to run.
                probe = ci._engine(ctx, root, jobs_file, [*argv, "--dry-run"], prepared["env"], prepared["python"],
                                   HOST_ENV)
                if probe.get("status") == "ok" and not (probe.get("result") or {}).get("order"):
                    ctx.log.info(f"{', '.join(names)} already passed for these trees; no turn of the GPU lease taken")
                    documents.append({**probe, "lease": False})
                    continue
            with (_turn(ctx, project, names) if lease and not dry_run else contextlib.nullcontext({})) as turn:
                ctx.progress("checks", msg=" ".join(names) + (" under the GPU lease" if lease and not dry_run else ""))
                document = ci._engine(ctx, root, jobs_file, argv, {**prepared["env"], **turn}, prepared["python"],
                                      HOST_ENV)
            document["lease"] = lease and not dry_run
            documents.append(document)
            ci._failing_tails(ctx, (document.get("result") or {}).get("jobs") or {})
            failed = [n for n, j in ((document.get("result") or {}).get("jobs") or {}).items()
                      if j.get("status") == "failed"]
            if (failed or document.get("status") in ("usage", "environment")) and not keep_going:
                break
    finally:
        if not keep:
            shutil.rmtree(work, ignore_errors=True)
    jobs_done: dict = {}
    runs, trees, leased = [], {}, []
    for document in documents:
        result = document.get("result") or {}
        jobs_done.update(result.get("jobs") or {})
        trees = trees or result.get("trees") or {}
        if result.get("runId"):
            runs.append(result["runId"])
            if (records / result["runId"]).is_dir():
                ctx.output("run" if len(runs) == 1 else f"run-{len(runs)}", records / result["runId"])
        if document.get("lease"):
            leased += list(result.get("jobs") or {})
    for name, job in jobs_done.items():
        seconds = sum(step.get("seconds") or 0 for step in job.get("steps") or [])
        if seconds:
            ctx.measure("job_seconds", round(seconds, 1), "s", job=name)
    passed = [n for n, j in jobs_done.items() if j.get("status") == "passed"]
    failed = [n for n, j in jobs_done.items() if j.get("status") == "failed"]
    worst = next((d for d in documents if d.get("status") in ("usage", "environment")), None)
    worst = worst or next((d for d in documents if d.get("status") == "finding"), None) or documents[-1]
    ci._conclude(ctx, project, sha, {**worst, "status": "finding" if failed else worst.get("status")}, jobs_done,
                 passed)
    if ctx.outcome == "ok":
        ctx.summary(f"{ctx.summary_text} on the host" + (f", {', '.join(leased)} under the GPU lease" if leased else ""))
    return {"project": project, "commit": sha, "worktree": tree["worktree"], "runs": runs, "trees": trees,
            "passed": passed, "failed": failed, "leased": leased, "set": prepared["set"], "tools": prepared["tools"],
            "stand_ins": dict(stand_ins or {}), "engine": [d.get("result") or {} for d in documents]}
