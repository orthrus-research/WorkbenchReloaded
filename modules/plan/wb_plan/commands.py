"""plan: check a plan before any GPU time is spent, then run it on the host or on a rented GPU."""

from __future__ import annotations

import json
import os
import tarfile
from pathlib import Path

from wb_core.module import Context, ParamError

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
        problems += _check_rental(ctx, plan, registry)
        if not _writable_out(ctx):
            problems.append("a rental plan needs --out under a configured rw root, so later steps can read "
                            "the fetched results")
    jobs = [[s.id for s in group] for group in plans.groups(plan, registry)]
    return problems, warnings, {"jobs": jobs, "steps": len(plan.steps), "registry": registry}


def _writable_out(ctx: Context) -> bool:
    """Whether the job's out folder is under a configured rw root (always, when none are configured)."""
    from wb_core.paths import PathError, PathMap
    from wb_core.settings import Root
    roots = [Root(**r) for r in ctx.spec.get("roots", [])]
    if not roots:
        return True
    try:
        PathMap(roots).resolve(str(ctx.out), need_write=True)
    except PathError:
        return False
    return True


def _check_rental(ctx: Context, plan: plans.Plan, registry: dict) -> list[str]:
    """What keeps a plan's game steps from running on a rental: its [rental] table, and whether the bundle template
    takes them (a vast.bundle check, which writes nothing)."""
    problems = [f"a rental plan needs rental.{key}" for key in ("offer", "max_dph") if key not in plan.rental]
    game = plans.game_steps(plan, registry)
    if not game:
        problems.append("a rental plan has no game steps: no step's command declares a role (module.toml role = "
                        "\"prepare\" or \"play\")")
    for step in game:
        for ref in plans.references(step.params):
            if ref.startswith("steps."):
                problems.append(f"step {step.id}: a game step is written into the rental's bundle before any step "
                                f"runs, so {{{ref}}} has no value yet")
    lab = "lab" not in plan.rental or _local(ctx, str(plan.rental["lab"])).is_dir()
    if not lab:
        problems.append(f"rental.lab is not a folder: {plan.rental['lab']}")
    idle = [name for name in ("vast.bundle", "vast.rent") if not (registry.get(name) or {}).get("active")]
    for name in idle:
        reasons = "; ".join(r["detail"] for r in (registry.get(name) or {}).get("reasons", [])) or "not installed"
        problems.append(f"a rental needs {name}, which is not active: {reasons}")
    if game and lab and not idle:
        checked = _bundle(ctx, plan, registry, _known_before_running(plan), dry_run=True)
        if checked.get("state") != "settled" or checked.get("outcome") not in ("ok", "finding"):
            problems.append(f"rental: the bundle could not be checked: {checked.get('summary')}")
        else:
            problems += [f"rental: {p}" for p in (checked.get("data") or {}).get("problems") or []]
    return problems


def _known_before_running(plan: plans.Plan):
    """Resolves what a check can resolve: inputs; a step's results stay references, for the make to check."""
    def lookup(ref: str):
        head, _, rest = ref.partition(".")
        if head == "inputs" and rest in plan.inputs:
            return plan.inputs[rest].get("path")
        return "{" + ref + "}"
    return lookup


def _bundle(ctx: Context, plan: plans.Plan, registry: dict, lookup, *, dry_run: bool, out: str | None = None) -> dict:
    """Has vast.bundle write (or, with ``dry_run``, check) the plan's game steps as its template declares."""
    params = {"steps": plans.rental_steps(plan, registry, lookup), "values": plans.rental_values(plan),
              "name": plan.name, "dry_run": dry_run}
    if plan.rental.get("template"):
        params["template"] = str(plan.rental["template"])
    if plan.rental.get("lab"):
        params["lab"] = ctx.host_path(_local(ctx, str(plan.rental["lab"])))
    return _wait(ctx, ctx.call("vast.bundle", out=out, **params))


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
        game = plans.game_steps(loaded, registry)
        envelope, refused = _rent(ctx, loaded, lookup, registry)
        if refused:
            ctx.finding(f"refused before renting: {refused}")
            return {"plan": loaded.name, "machine": machine, "ran": ran}
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


def _rent(ctx: Context, plan: plans.Plan, lookup, registry: dict) -> tuple[dict | None, str | None]:
    """Writes the game steps into a bundle as the plan's template declares (vast.bundle) and runs it with vast.rent,
    which waits for a yes. The results the template names become the game steps' outputs. Returns the rental's
    envelope, or why the bundle was refused (nothing is rented then)."""
    made = _bundle(ctx, plan, registry, lookup, dry_run=False, out=ctx.host_path(ctx.out / "bundle"))
    data = made.get("data") or {}
    if made.get("state") != "settled" or made.get("outcome") != "ok" or not data.get("bundle"):
        return None, f"the bundle was not made: {made.get('summary')}"
    rent = {"offer": str(plan.rental["offer"]), "bundle": data["bundle"], "max_dph": plan.rental["max_dph"],
            "cap": plan.rental.get("cap", "90m")}
    envelope = _wait(ctx, ctx.call("vast.rent", out=ctx.host_path(ctx.out / "rental"), **rent))
    fetched = ctx.out / "rental"
    for name in data.get("unpack") or []:
        archive = fetched / name
        if archive.is_file():
            try:
                with tarfile.open(archive) as handle:
                    handle.extractall(fetched, filter="data")
            except (tarfile.TarError, OSError) as error:
                ctx.log.warning(f"could not unpack {name} from the rental: {error}")
    for output in data.get("outputs") or []:
        path = fetched / output["path"]
        if path.exists():
            ctx.output(output["name"], path)
            envelope.setdefault("outputs", []).append({"name": output["name"], "host_path": ctx.host_path(path)})
    return envelope, None
