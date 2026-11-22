"""plan: check a plan before any GPU time is spent, then run it on the host or on a rented GPU."""

from __future__ import annotations

import json
import os
import shutil
import tarfile
import time
from pathlib import Path

from wb_core.module import Context, EnvironmentProblem, ParamError

from . import plan as plans

TERMINAL = ("settled", "cancelled", "interrupted")


def _client():
    from wb.client import Client
    client = Client(os.environ.get("WB_URL"), os.environ.get("WB_TOKEN"))
    client.path_style = "posix"
    return client


def _local(ctx: Context, value: str) -> Path:
    """A path written in the plan (host form) as a path this process can read."""
    from wb_core.paths import PathError, PathMap
    from wb_core.settings import Root
    roots = [Root(**r) for r in ctx.spec.get("roots", [])]
    if roots:
        try:
            return Path(PathMap(roots).resolve(value).container)
        except PathError:
            pass
    return Path(value)


def _load(ctx: Context, path: str) -> plans.Plan:
    try:
        return plans.load(Path(path))
    except plans.PlanError as error:
        raise ParamError(f"plan: {error}") from None


def _check(ctx: Context, plan: plans.Plan, machine: str) -> tuple[list[str], list[str], dict]:
    problems, warnings = [], []
    if not plan.steps:
        problems.append("the plan has no steps")
    if not plan.protocol:
        warnings.append("the plan declares no protocol (what it measures, what counts as a pass)")
    registry = {c["name"]: c for c in _client().commands()}
    for name, item in plan.inputs.items():
        if "path" not in item:
            problems.append(f"input {name} has no path")
            continue
        local = _local(ctx, item["path"])
        if not local.is_file():
            problems.append(f"input {name} does not exist: {item['path']}")
        elif item.get("sha256") and plans.sha256(local) != item["sha256"].lower().removeprefix("sha256:"):
            problems.append(f"input {name} drifted: {item['path']} no longer matches its pinned sha256")
        elif not item.get("sha256"):
            warnings.append(f"input {name} is not pinned by sha256")
    seen: dict[str, plans.Step] = {}
    for step in plan.steps:
        if step.id in seen:
            problems.append(f"step id {step.id} is used twice")
        command = registry.get(step.command)
        if command is None:
            problems.append(f"step {step.id}: unknown command {step.command}")
        elif not command["active"]:
            problems.append(f"step {step.id}: {step.command} is not active: "
                            + "; ".join(r["detail"] for r in command["reasons"]))
        else:
            schema = command["params"]
            unknown = set(step.params) - set(schema.get("properties", {}))
            if unknown:
                problems.append(f"step {step.id}: unknown parameters {sorted(unknown)} for {step.command}")
            missing = set(schema.get("required", [])) - set(step.params)
            if missing:
                problems.append(f"step {step.id}: missing parameters {sorted(missing)} for {step.command}")
            if command["spends"]:
                warnings.append(f"step {step.id}: {step.command} spends money and will wait for approval")
        for ref in plans.references(step.params):
            head, _, rest = ref.partition(".")
            if head == "inputs":
                if rest not in plan.inputs:
                    problems.append(f"step {step.id}: no input {rest}")
            elif head == "steps":
                target = rest.split(".")[0]
                if target not in seen:
                    problems.append(f"step {step.id}: {{{ref}}} names a step that does not run before it")
            else:
                problems.append(f"step {step.id}: cannot resolve {{{ref}}}")
        seen[step.id] = step
    gpu = [s for s in plan.steps if (registry.get(s.command) or {}).get("resources", {}).get("gpu")]
    consumed = {r.split(".")[1] for s in plan.steps for r in plans.references(s.params) if r.startswith("steps.")}
    if gpu and not any(s.id in consumed for s in gpu) and not any(s.command.startswith(("frames.", "ledger."))
                                                                  for s in plan.steps):
        warnings.append("GPU steps produce evidence nothing in the plan scores or records")
    if machine == "rental":
        for key in ("offer", "max_dph", "lab"):
            if key not in plan.rental:
                problems.append(f"a rental plan needs rental.{key}")
        if "lab" in plan.rental:
            lab = _local(ctx, plan.rental["lab"])
            missing = [t for t in ("riven.tar", "cubature.tar", "pins.tar", "cache.tar") if not (lab / t).is_file()]
            if missing:
                problems.append(f"rental.lab lacks {', '.join(missing)}")
        try:
            plans.plan_env(plan, {}, {str(p.params.get("tour")): Path(str(p.params.get("tour"))).name
                                      for p in plan.steps if plans.role(registry, p.command) == "play"}, registry)
        except (plans.PlanError, KeyError) as error:
            problems.append(f"rental: {error}")
        try:
            ctx.host_path(ctx.out)
            if ctx.host_path(ctx.out) == str(ctx.out) and ctx.spec.get("roots"):
                problems.append("a rental plan needs --out under a configured rw root, so later steps can read "
                                "the fetched results")
        except Exception:  # noqa: BLE001
            pass
    jobs = [[s.id for s in group] for group in plans.groups(plan, registry)]
    return problems, warnings, {"jobs": jobs, "steps": len(plan.steps), "registry": registry}


def check(ctx: Context, plan: str, machine=None):
    loaded = _load(ctx, plan)
    machine = machine or loaded.machine
    problems, warnings, shape = _check(ctx, loaded, machine)
    for text in warnings:
        ctx.log.warning(text)
    if problems:
        ctx.finding(f"{len(problems)} problem(s): {problems[0]}")
    else:
        ctx.summary(f"plan {loaded.name} is ready: {shape['steps']} step(s) in {len(shape['jobs'])} job(s) on {machine}")
    return {"plan": loaded.name, "machine": machine, "problems": problems, "warnings": warnings,
            "jobs": shape["jobs"], "steps": shape["steps"]}


def _wait(ctx: Context, envelope: dict) -> dict:
    """Waits for a child job, including while it waits for someone to approve its quote."""
    client = _client()
    job = client.job(envelope["job"])
    noted = False
    while envelope.get("state") not in TERMINAL:
        if envelope.get("state") == "awaiting_approval" and not noted:
            quote = (envelope.get("approval") or {}).get("quote") or {}
            ctx.log.warning(f"waiting for approval of {envelope['job']}: wb approve {envelope['job']} --quote "
                            f"{quote.get('id')}")
            ctx.progress("approval", msg=f"{envelope['job']} awaits a yes")
            noted = True
        ctx.check_cancel()
        envelope = dict(job.wait(timeout=30, stop_at_approval=False))
    return envelope


def _lookup(plan: plans.Plan, ctx: Context, done: dict[str, dict]):
    def lookup(ref: str):
        head, _, rest = ref.partition(".")
        if head == "inputs":
            return plan.inputs[rest]["path"]
        if head == "steps":
            step_id, kind, *name = rest.split(".", 2)
            envelope = done.get(step_id)
            if envelope is None:
                raise ParamError(f"{{{ref}}}: step {step_id} has not run")
            if kind == "outputs":
                for output in envelope.get("outputs", []):
                    if output["name"] == name[0]:
                        return output["host_path"]
                raise ParamError(f"{{{ref}}}: step {step_id} produced no output {name[0]}")
            if kind == "data":
                return (envelope.get("data") or {}).get(name[0])
            if kind == "job":
                return envelope["job"]
        raise ParamError(f"cannot resolve {{{ref}}}")
    return lookup


def _accepted(step: plans.Step, envelope: dict) -> bool:
    return envelope.get("state") == "settled" and envelope.get("outcome") in step.accept


def run(ctx: Context, plan: str, machine=None):
    loaded = _load(ctx, plan)
    machine = machine or loaded.machine
    problems, warnings, shape = _check(ctx, loaded, machine)
    if problems:
        ctx.finding(f"refused before any work: {problems[0]}")
        return {"plan": loaded.name, "problems": problems, "ran": []}
    ctx.log.info(f"protocol: {loaded.protocol or '(none declared)'}")
    done: dict[str, dict] = {}
    ran = []
    lookup = _lookup(loaded, ctx, done)
    registry = shape["registry"]
    remaining = list(loaded.steps)
    if machine == "rental":
        game = [s for s in remaining if plans.role(registry, s.command)]
        envelope = _rent(ctx, loaded, game, lookup, registry)
        ran.append({"steps": [s.id for s in game], "job": envelope["job"], "outcome": envelope.get("outcome")})
        if envelope.get("outcome") not in ("ok", "finding") or envelope.get("state") != "settled":
            ctx.finding(f"the rental ended {envelope.get('state')} {envelope.get('outcome')}: {envelope.get('summary')}")
            return {"plan": loaded.name, "machine": machine, "ran": ran}
        for step in game:
            done[step.id] = envelope
        remaining = [s for s in remaining if not plans.role(registry, s.command)]
    for group in plans.groups(plans.Plan(**{**loaded.__dict__, "steps": remaining}), registry):
        ctx.check_cancel()
        lone = group[0].command
        if len(group) > 1 or (loaded.turn and plans.turnable(registry, lone)
                              and (registry.get(lone) or {}).get("resources", {}).get("gpu")):
            steps = [{"id": s.id, "command": s.command, "params": plans.substitute(s.params, lookup)} for s in group]
            ctx.progress("turn", msg=", ".join(s.id for s in group))
            envelope = _wait(ctx, ctx.call("lab.turn", steps=steps))
        else:
            step = group[0]
            ctx.progress(step.id, msg=step.command)
            envelope = _wait(ctx, ctx.call(step.command, **plans.substitute(step.params, lookup)))
        for step in group:
            done[step.id] = envelope
        ran.append({"steps": [s.id for s in group], "job": envelope["job"], "outcome": envelope.get("outcome"),
                    "summary": envelope.get("summary")})
        if not all(_accepted(s, envelope) for s in group):
            ctx.finding(f"stopped at {group[-1].id}: {envelope.get('state')} {envelope.get('outcome')} — "
                        f"{envelope.get('summary')}")
            break
    if ctx.outcome == "ok":
        ctx.summary(f"plan {loaded.name}: {len(loaded.steps)} step(s) in {len(ran)} job(s) on {machine}")
    record = ctx.out / "plan-run.json"
    record.write_text(json.dumps({"plan": loaded.name, "machine": machine, "protocol": loaded.protocol, "ran": ran},
                                 indent=1), encoding="utf-8")
    ctx.output("plan-run", record)
    return {"plan": loaded.name, "machine": machine, "ran": ran}


def _rent(ctx: Context, plan: plans.Plan, game: list, lookup, registry: dict) -> dict:
    """Compiles the game steps into a riven-tours bundle and runs it with vast.rent (which waits for a yes)."""
    bundle = ctx.out / "bundle"
    jars = bundle / "jars"
    jars.mkdir(parents=True, exist_ok=True)
    jar_names, tour_names = {}, {}
    for step in game:
        params = plans.substitute(step.params, lookup)
        if plans.role(registry, step.command) == "prepare":
            for arg in params.get("args", []):
                local = _local(ctx, str(arg))
                if str(arg).lower().endswith(".jar") and local.is_file():
                    shutil.copy2(local, jars / local.name)
                    jar_names[str(arg)] = f"/root/bundle/jars/{local.name}"
            step.params = {**step.params, "args": params.get("args", [])}
        else:
            local = _local(ctx, str(params["tour"]))
            if not local.is_file():
                raise ParamError(f"tour {params['tour']} does not exist")
            shutil.copy2(local, bundle / local.name)
            tour_names[str(params["tour"])] = local.name
            step.params = {**step.params, "tour": str(params["tour"])}
    env, labels = plans.plan_env(plans.Plan(**{**plan.__dict__, "steps": game}), jar_names, tour_names, registry)
    (bundle / "plan.env").write_text(env, encoding="utf-8", newline="\n")
    lab = ctx.host_path(_local(ctx, plan.rental["lab"]))
    made = _wait(ctx, ctx.call("vast.bundle-riven", out=ctx.host_path(bundle / "made"),
                               plan=ctx.host_path(bundle), lab=lab, name=plan.name))
    if made.get("outcome") != "ok":
        raise EnvironmentProblem(f"could not make the bundle: {made.get('summary')}")
    rent = {"offer": str(plan.rental["offer"]), "bundle": ctx.host_path(bundle / "made"),
            "max_dph": plan.rental["max_dph"], "cap": plan.rental.get("cap", "90m")}
    envelope = _wait(ctx, ctx.call("vast.rent", out=ctx.host_path(ctx.out / "rental"), **rent))
    results = ctx.out / "rental" / "results.tar"
    if results.is_file():
        with tarfile.open(results) as archive:
            archive.extractall(ctx.out / "rental", filter="data")
        for label in labels:
            for suffix, name in (("captures", f"{label}-captures"), ("client-latest.log", f"{label}-client-log"),
                                 ("play.log", f"{label}-play-log")):
                path = ctx.out / "rental" / "results" / f"{label}-{suffix}"
                if path.exists():
                    ctx.output(name, path)
                    envelope.setdefault("outputs", []).append({"name": name, "host_path": ctx.host_path(path)})
    return envelope
