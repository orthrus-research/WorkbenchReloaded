"""lab.inputs: bring a launch set to the state an instance is built from, before lab.ensure prepares one, and
check that the projects in it pin the same commits of what they share.

Both are the instance profile's (wb_lab.profile). ``inputs`` are the steps: each a name, a command template and the
checkout it runs in (the launcher's, or one of the profile's ``checkouts``), run in order on the host with the
profile's ``env``; a machine layer can give that env what only it knows, such as a pin cache. ``agree`` names each
shared dependency and where each project pins it. The code names no project."""

from __future__ import annotations

import re
import time
from pathlib import Path

from wb_core.module import Context, EnvironmentProblem, ParamError

from . import commands
from . import profile as profiles


def folder(ctx: Context, checkout: str) -> Path:
    """The launcher checkout, or one the profile's ``checkouts`` names."""
    return commands._profile_path(ctx, "{" + checkout + "}") if checkout else commands._launcher(ctx)


def agreement(ctx: Context) -> dict:
    """Each dependency the profile's ``agree`` names, with the commit each project pins it at (read from its pin
    file by the pattern's first group), and whether they agree: each commit a prefix of the others, since pin files
    write short and full hashes."""
    found = {}
    for dependency, pins in commands._profile(ctx)["agree"].items():
        seen = []
        for pin in pins:
            checkout = pin.get("checkout") or ""
            path = folder(ctx, checkout) / pin["file"]
            if not path.is_file():
                raise EnvironmentProblem(f"agree.{dependency}: there is no {path}")
            match = re.search(pin["pattern"], path.read_text(encoding="utf-8"))
            if not match:
                raise EnvironmentProblem(f"agree.{dependency}: nothing in {path} matches its pattern")
            seen.append({"checkout": checkout or "launcher", "file": pin["file"], "commit": match.group(1).lower()})
        commits = [pin["commit"] for pin in seen]
        found[dependency] = {"agrees": all(a.startswith(b) or b.startswith(a) for a in commits for b in commits),
                             "pins": seen}
    return found


def disagreements(found: dict) -> list[str]:
    """One line per dependency whose pins disagree: who pins which commit."""
    return [f"{dependency}: " + ", ".join(f"{pin['checkout']} {pin['file']} {pin['commit'][:12]}"
                                         for pin in entry["pins"])
            for dependency, entry in found.items() if not entry["agrees"]]


def check_agreement(ctx: Context, allow: bool) -> list[str]:
    """The disagreements, refused unless ``allow``: an instance built from pins that disagree loads one project's
    build of a dependency against another's."""
    lines = disagreements(agreement(ctx))
    if lines and not allow:
        raise EnvironmentProblem("the launch set's pins disagree (allow_disagreement=true to build it anyway): "
                                 + "; ".join(lines))
    return lines


def inputs(ctx: Context, steps: list, allow_disagreement: bool = False, profile: str = ""):
    p = commands._profile(ctx)
    declared = p["inputs"]
    if not declared and not p["agree"]:
        raise ParamError("the instance profile has no inputs (the steps that bring a launch set to its state) and "
                         "nothing to agree on")
    names = [step["name"] for step in declared]
    unknown = [name for name in steps if name not in names]
    if unknown:
        raise ParamError(f"no input step {', '.join(unknown)} (the profile's: {', '.join(names)})")
    found = agreement(ctx)
    disagree = disagreements(found)
    if disagree and not allow_disagreement:
        ctx.finding("the launch set's pins disagree, so no step ran: " + "; ".join(disagree))
        return {"agreement": found, "steps": []}
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
            return {"agreement": found, "steps": results}
    agreed = f"{len(found)} shared dependenc{'y' if len(found) == 1 else 'ies'} agree" if found and not disagree \
        else (f"pins disagree, allowed: {'; '.join(disagree)}" if disagree else "")
    ctx.summary(f"{len(results)} input step(s) done"
                + (": " + ", ".join(f"{r['name']} {r['seconds']:g} s" for r in results) if results else "")
                + (f"; {agreed}" if agreed else ""))
    return {"agreement": found, "steps": results}
