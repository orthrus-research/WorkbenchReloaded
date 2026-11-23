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
            if "set" in pin:  # the line the Set names: what a converted project builds against
                seen.append({"checkout": "set", "file": "line", "commit": _set_line(dependency, pin["set"])})
                continue
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


def _set_line(dependency: str, project: str) -> str:
    from wb.workspace import Workspace
    line = Workspace().set_line(project)
    if not line:
        raise EnvironmentProblem(f"agree.{dependency}: the Set on this machine has no line for {project}")
    return line["commit"].lower()


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


def from_store(ctx: Context, step: dict, cwd: Path) -> dict | None:
    """The step's files from the Store when CI built this checkout's tree: the checkout must be clean (its tracked
    files are its HEAD), and the Store must hold every file the step names. None: the step runs as written."""
    import shutil
    import subprocess
    from wb_core import artifacts
    from wb.workspace import Workspace, WorkspaceError
    store = step["store"]
    def git(*args):
        done = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
        return done.stdout.strip() if done.returncode == 0 else None
    tree = git("rev-parse", "HEAD^{tree}")
    if tree is None:
        return None
    if git("status", "--porcelain", "--untracked-files=no"):
        ctx.log.info(f"{step['name']}: the checkout has changes, so CI's JARs do not stand for it; building")
        return None
    try:
        workspace = Workspace()
        root = workspace.host_path(str(workspace.ci.get("store") or ""))
    except (WorkspaceError, OSError) as error:
        ctx.log.info(f"{step['name']}: no Store to read ({error}); building")
        return None
    index = artifacts.find(root, store["project"], tree) if str(root) else None
    held = (index or {}).get("artifacts", {})
    missing = [name for name in store["files"] if name not in held]
    if missing:
        ctx.log.info(f"{step['name']}: the Store has no {', '.join(missing)} of {store['project']} tree {tree[:12]}; "
                     "building")
        return None
    started = time.monotonic()
    placed = {}
    for name, target in store["files"].items():
        kept = artifacts.path_of(root, held[name]["sha256"], name)
        destination = cwd / target
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(kept, destination)
        placed[name] = held[name]["sha256"]
    ctx.log.info(f"{step['name']}: took {len(placed)} file(s) CI built for {store['project']} tree {tree[:12]} "
                 "from the Store")
    return {"name": step["name"], "exit": 0, "seconds": round(time.monotonic() - started, 1),
            "from_store": {"project": store["project"], "tree": tree, "files": placed}}


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
        if step.get("store"):
            taken = from_store(ctx, step, cwd)
            if taken is not None:
                results.append(taken)
                continue
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
