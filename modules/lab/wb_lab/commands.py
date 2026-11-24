"""lab: host routines (run natively by wb-host): the GPU's state, and a turn of routines under one GPU lease.

The instance profile's routines (a project launcher's prepare and play, its inputs, checkouts, store, scripts and
lab move) are retired: a game is built from a launch file (lab.resolve, lab.build) and played in a session of it
(lab.run, wb play). ``lab.prepare`` and ``lab.play`` stay declared only as a rental plan's game steps, which
vast.bundle writes into a bundle; on this machine they refuse and name wb play."""

from __future__ import annotations

import shutil
from pathlib import Path

from wb_core.module import Context, EnvironmentProblem, ParamError

RETIRED_HERE = ("{command} no longer runs on this machine: build and play a launch with wb play (lab.resolve, "
                "lab.build, lab.run). It stays a rental plan's game step, which vast.bundle writes into the bundle.")


def gpu(ctx: Context):
    if not shutil.which("nvidia-smi"):
        raise EnvironmentProblem("nvidia-smi is not on PATH on the host")
    done = ctx.run(["nvidia-smi", "--query-gpu=name,driver_version,memory.total,pcie.link.gen.current,"
                    "pcie.link.width.current", "--format=csv,noheader"], check=True, log_output=False)
    name, driver, memory, gen, width = [p.strip() for p in done.stdout.splitlines()[0].split(",")]
    ctx.machine_info(gpu=name, driver=driver)
    ctx.summary(f"{name}, driver {driver}, {memory}, PCIe gen {gen} x{width}")
    return {"gpu": name, "driver": driver, "memory": memory, "pcie": f"gen{gen} x{width}"}


def prepare(ctx: Context, **_params):
    """A rental plan's prepare step; on this machine, retired."""
    raise ParamError(RETIRED_HERE.format(command="lab.prepare"))


def play(ctx: Context, **_params):
    """A rental plan's play step; on this machine, retired."""
    raise ParamError(RETIRED_HERE.format(command="lab.play"))


def turn(ctx: Context, steps: list, continue_on_finding: bool):
    """Runs several lab routines in this one process, so the lease wb-host took for this job is held from the
    first step to the last and nothing else can start between them."""
    from wb_core import types
    from wb_core.manifest import parse
    manifest = parse(Path(__file__).with_name("module.toml"), "wb_lab")
    routines = {c.full: c for c in manifest.commands if c.turn}  # module.toml marks them: turn = true
    done = []
    for index, step in enumerate(steps, start=1):
        ctx.check_cancel()
        name = step.get("command")
        if name not in routines:
            raise ParamError(f"turn step {index}: {name} is not a routine a turn can run ({', '.join(routines)})")
        command = routines[name]
        try:
            params = types.validate(command.params, step.get("params") or {})
        except types.ValueError_ as error:
            raise ParamError(f"turn step {index} ({name}): {error}") from None
        label = step.get("id") or f"step-{index}"
        ctx.progress(label, (index - 1) / len(steps), name)
        before = len(ctx.findings)
        result = globals()[command.handler.rpartition(":")[2]](ctx, **params)
        failed = len(ctx.findings) > before
        done.append({"id": label, "command": name, "result": result, "finding": failed})
        if failed and not continue_on_finding:
            ctx.log.warning(f"stopping at {label}: {ctx.findings[-1]}")
            break
    if ctx.outcome == "ok":
        ctx.summary(f"turn of {len(done)} step(s) finished under one lease")
    return {"steps": done}
