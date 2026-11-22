"""ci.matrix: a project's command run once per Python on a host, for portability (a lane with no GPU).

The checkout's commit (``commit``, by default its HEAD) is cloned clean once, and for each of ``pythons`` the
command runs in that clone with ``{python}`` naming the interpreter: in ``folder`` inside it, with ``env`` added to
its environment (the record keeps the variables' names, not their values). A Python is named by version (``3.12``:
found through the ``py`` launcher on Windows, as ``python3.12`` elsewhere) or by its path. With ``requirements``,
each version gets an environment of its own, made from those files (relative to the checkout's top), so the run
tests the packages that version resolves.
Each version's exit code, time and last lines are kept; a failing version, or one the host lacks, is a finding.
Host routines without a GPU run beside a lease holder, so the matrix never waits for a game.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from wb_core.module import Context, ParamError

from wb_build import checkout as checkouts

SCHEMA = "wb.ci.matrix/1"


def find_python(spec: str) -> str | None:
    """The interpreter a version (3.12) or a path names on this host, or None."""
    if os.sep in spec or "/" in spec:
        return spec if Path(spec).is_file() else None
    probe = (["py", f"-{spec}"] if os.name == "nt" and shutil.which("py") else
             [shutil.which(f"python{spec}") or f"python{spec}"])
    try:
        done = subprocess.run([*probe, "-c", "import sys; print(sys.executable)"], capture_output=True, text=True,
                              timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    path = done.stdout.strip().splitlines()[-1:] if done.returncode == 0 else []
    return path[0] if path and Path(path[0]).is_file() else None


def python_version(python: str) -> str:
    """An interpreter's version (3.12.4), or empty when it does not answer."""
    try:
        done = subprocess.run([python, "-c", "import sys; print(sys.version.split()[0])"], capture_output=True,
                              text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def inside(name: str, what: str) -> str:
    """A path relative to the checkout's top that stays inside it, as POSIX; else a usage error."""
    path = Path(name)
    if path.is_absolute() or path.drive or ".." in path.parts:
        raise ParamError(f"{what}: {name} must be relative to the checkout's top, inside it")
    return path.as_posix()


def matrix(ctx: Context, checkout: str, command: list, pythons: list, commit: str, requirements: list,
           timeout: float, folder: str = "", env: dict | None = None):
    root = Path(checkout)
    if not command or not any("{python}" in str(item) for item in command):
        raise ParamError("command: it must name the interpreter as {python}")
    if not pythons:
        raise ParamError("pythons: name at least one Python (a version such as 3.12, or a path)")
    folder = inside(folder, "folder") if folder else ""
    requirements = [inside(str(r), "requirements") for r in requirements]
    extra = {str(name): str(value) for name, value in (env or {}).items()}
    # HEAD is the checkout's own: a linked worktree's HEAD lives in its git dir, not in the shared one clone_at reads
    named = checkouts.git(["--git-dir", str(checkouts.git_dir(ctx, root)), "rev-parse", "--verify", "--quiet",
                           f"{commit or 'HEAD'}^{{commit}}"], check=False)
    if named.returncode != 0:
        raise ParamError(f"no commit {commit or 'HEAD'} in {ctx.host_path(root)}")
    tree = ctx.work / "tree"
    full, _ = checkouts.clone_at(ctx, root, named.stdout.strip(), tree)
    try:
        return _lanes(ctx, root, tree, full, command, pythons, requirements, timeout, folder, extra)
    finally:
        shutil.rmtree(tree, ignore_errors=True)


def _lanes(ctx: Context, root: Path, tree: Path, full: str, command: list, pythons: list, requirements: list,
           timeout: float, folder: str, extra: dict) -> dict:
    from wb_lab import process as processes  # the lab's runner: a timeout that kills the tree, cancelling

    where = tree / folder if folder else tree
    if not where.is_dir():
        raise ParamError(f"folder: {folder} is not a folder in {full[:12]}")
    missing = [r for r in requirements if not (tree / r).is_file()]
    if missing:
        raise ParamError(f"requirements: {', '.join(missing)} not in {full[:12]}")
    results, problems = [], []
    for index, spec in enumerate(pythons):
        ctx.check_cancel()
        interpreter = find_python(str(spec))
        if interpreter is None:
            problems.append(f"no Python {spec} on this host")
            results.append({"python": spec, "found": None})
            continue
        python = interpreter
        if requirements:
            ctx.progress("environment", msg=f"Python {spec}")
            venv = ctx.work / "venvs" / str(index)
            made = processes.run(ctx, [interpreter, "-m", "venv", str(venv)], cwd=tree, timeout=600)
            python = str(venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))
            pip = [python, "-m", "pip", "install", "-q", *[a for r in requirements for a in ("-r", str(tree / r))]]
            installed = processes.run(ctx, pip, cwd=tree, timeout=1800) if made.code == 0 else made
            if installed.code != 0:
                problems.append(f"Python {spec}: its environment could not be made: "
                                f"{(installed.output.strip().splitlines() or ['no output'])[-1][:200]}")
                results.append({"python": spec, "found": interpreter, "environment": False})
                continue
        ctx.progress("run", msg=f"Python {spec}")
        argv = [str(item).replace("{python}", python) for item in command]
        ran = processes.run(ctx, argv, cwd=where, timeout=timeout, env=extra)
        version = python_version(python)
        log = ctx.out / f"python-{version or index}.log"
        log.write_text(ran.output + "\n", encoding="utf-8")
        ctx.output(log.stem, log)
        tail = [line for line in ran.output.splitlines() if line.strip()][-3:]
        results.append({"python": spec, "found": interpreter, "version": version, "exit": ran.code,
                        "seconds": ran.seconds, "tail": tail})
        if ran.code is None:
            problems.append(f"Python {version or spec}: stopped at its timeout")
        elif ran.code != 0:
            problems.append(f"Python {version or spec} exited {ran.code}: {(tail or ['no output'])[-1][:200]}")
    record = {"schema": SCHEMA, "checkout": ctx.host_path(root), "commit": full, "command": command,
              "folder": folder, "env": sorted(extra), "platform": sys.platform, "results": results}
    (ctx.out / "matrix.json").write_text(json.dumps(record, indent=1) + "\n", encoding="utf-8")
    ctx.output("matrix", ctx.out / "matrix.json")
    for problem in problems:
        ctx.finding(problem)
    if not problems:
        ctx.summary(f"{full[:12]} passes on " + ", ".join(r["version"] or str(r["python"]) for r in results)
                    + f" ({sys.platform})")
    return {"commit": full, "results": results, "problems": problems}
