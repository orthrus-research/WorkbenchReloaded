import json
import shutil
import time
from pathlib import Path

from wb_core.module import Context, EnvironmentProblem, ParamError


def echo(ctx: Context, text: str, times: int):
    ctx.progress("echo", 0.5)
    ctx.log.info(f"echo {text!r} x{times}")
    path = ctx.out / "echo.json"
    path.write_text(json.dumps({"text": text * times}), encoding="utf-8")
    ctx.output("echo", path, "application/json")
    ctx.measure("length", len(text) * times, unit="chars")
    ctx.summary(f"echoed {len(text) * times} characters")
    return {"text": text * times, "greeting": ctx.config.get("greeting", "hello")}


def sleep(ctx: Context, seconds: float):
    steps = max(1, int(seconds))
    for step in range(steps):
        ctx.progress("sleeping", step / steps, f"{step} of {steps} s")
        ctx.sleep(seconds / steps)
    ctx.summary(f"slept {seconds:.0f} s")
    return {"seconds": seconds}


def fail(ctx: Context, outcome: str):
    if outcome == "finding":
        ctx.finding("a finding, as asked")
        return {}
    if outcome == "usage":
        raise ParamError("a usage error, as asked")
    if outcome == "environment":
        raise EnvironmentProblem("an environment problem, as asked")
    raise RuntimeError("a crash, as asked")


def spend_quote(ctx: Context, price: float, raise_to=None):
    return {"summary": "a zero-cost self-test rental", "card": "none", "dph": price, "cap_minutes": 1,
            "max_cost": round(price / 60, 6)}


def spend(ctx: Context, price: float, raise_to=None):
    quoted = ctx.quote or {}
    if raise_to is not None and quoted.get("dph", 0) < raise_to:
        ctx.requote(dict(spend_quote(ctx, raise_to), dph=raise_to), f"price rose to {raise_to}")
    ctx.cost(0.0, rental="selftest")
    ctx.summary(f"spent nothing on quote {quoted.get('id')}")
    return {"quote": quoted.get("id")}


def copy(ctx: Context, source: str):
    src = Path(source)
    target = ctx.out / src.name
    shutil.copy2(src, target)
    ctx.output("copy", target)
    return {"name": src.name, "host_source": ctx.host_path(src)}


def sizes(ctx: Context, sources: list):
    values = [Path(s).stat().st_size for s in sources]
    ctx.summary(f"{len(values)} file(s), {sum(values)} bytes")
    return {"sizes": values}


def checkout(ctx: Context, repo: str):
    done = ctx.run(["git", "-c", "safe.directory=*", "-C", repo, "rev-parse", "HEAD"], log_output=False)
    if done.returncode != 0:
        raise ParamError(f"{repo} is not a git checkout with a commit")
    head = done.stdout.strip().splitlines()[-1]
    ctx.summary(f"HEAD is {head[:12]}")
    return {"head": head}


def chain(ctx: Context, text: str):
    inner = ctx.call("selftest.echo", text=text)
    ctx.summary(f"inner job {inner['job']} {inner['outcome']}")
    return {"inner": inner["job"], "inner_outcome": inner["outcome"]}


def host_echo(ctx: Context, text: str):
    import os
    token = os.environ.get("WB_TURN_TOKEN")
    ctx.summary(f"ran on {ctx.machine} with{'' if token else 'out'} the lease token")
    return {"text": text, "machine": ctx.machine, "lease_token": bool(token), "pid": os.getpid()}
