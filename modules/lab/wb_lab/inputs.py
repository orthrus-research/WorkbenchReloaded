"""lab.inputs: bring a launch set to the state an instance is built from, before lab.ensure prepares one.

The steps are the instance profile's ``inputs`` (wb_lab.profile): each a name, a command template and the
checkout it runs in (the launcher's, or one of the profile's ``checkouts``), run in order on the host with the
profile's ``env``. A machine layer can give that env what only it knows, such as a pin cache. The code names no
project."""

from __future__ import annotations

import time
from pathlib import Path

from wb_core.module import Context, ParamError

from . import commands
from . import profile as profiles


def folder(ctx: Context, checkout: str) -> Path:
    """The launcher checkout, or one the profile's ``checkouts`` names."""
    return commands._profile_path(ctx, "{" + checkout + "}") if checkout else commands._launcher(ctx)


def inputs(ctx: Context, steps: list, profile: str = ""):
    p = commands._profile(ctx)
    declared = p["inputs"]
    if not declared:
        raise ParamError("the instance profile has no inputs (the steps that bring a launch set to its state)")
    names = [step["name"] for step in declared]
    unknown = [name for name in steps if name not in names]
    if unknown:
        raise ParamError(f"no input step {', '.join(unknown)} (the profile's: {', '.join(names)})")
    chosen = [step for step in declared if not steps or step["name"] in steps]
    results = []
    for step in chosen:
        cwd = folder(ctx, step.get("checkout") or "")
        command = profiles.command(step["run"], python=commands._python(ctx), cwd=str(cwd))
        ctx.progress("inputs", msg=step["name"])
        started = time.monotonic()
        done = ctx.run(command, cwd=cwd, env=commands._env(ctx))
        seconds = round(time.monotonic() - started, 1)
        log = ctx.out / f"inputs-{step['name']}.log"
        log.write_text(done.stdout, encoding="utf-8")
        ctx.output(f"inputs-{step['name']}-log", log)
        results.append({"name": step["name"], "exit": done.returncode, "seconds": seconds})
        if done.returncode != 0:
            last = done.stdout.strip().splitlines()[-1:] or [""]
            ctx.finding(f"input step {step['name']} exited {done.returncode}: {last[0]}")
            return {"steps": results}
    ctx.summary(f"{len(results)} input step(s) done: "
                + ", ".join(f"{r['name']} {r['seconds']:g} s" for r in results))
    return {"steps": results}
