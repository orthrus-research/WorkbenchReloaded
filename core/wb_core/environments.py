"""Python environments made from a project's requirements files, once per set (keyed by their SHA-256 and the
interpreter's version), for commands that run a project's own Python: ci's jobs (``{python}``) and lab's scripts.
Where an environment lives is Workbench's business, never the project's, and nothing is written into its tree."""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
from pathlib import Path

from . import filelock
from .module import Context, EnvironmentProblem

VENV_SECONDS, PIP_SECONDS = 600, 1800
BUILD_SECONDS = VENV_SECONDS + PIP_SECONDS


def default_cache() -> Path:
    """Where a host keeps environments when its config names no folder."""
    base = os.environ.get("XDG_CACHE_HOME")
    return (Path(base) if base else Path.home() / ".cache") / "wb" / "python"


def console_python() -> str:
    """Workbench's own interpreter as a step should run it: on Windows the host runner runs under pythonw.exe, which
    has no console, so a program a step starts gets no output handles; python.exe beside it is the same Python."""
    exe = Path(sys.executable)
    if os.name == "nt" and exe.name.lower() == "pythonw.exe" and exe.with_name("python.exe").is_file():
        return str(exe.with_name("python.exe"))
    return sys.executable


def python_from(ctx: Context, files: list[Path], cache: Path, what: str) -> str:
    """The interpreter of the environment made from ``files`` (pip requirements files), built under ``cache`` the
    first time; Workbench's own interpreter when there are none. ``what`` names who asked, for messages."""
    if not files:
        return console_python()
    missing = [str(f) for f in files if not f.is_file()]
    if missing:
        raise EnvironmentProblem(f"no {', '.join(missing)} (named by {what})")
    digest = hashlib.sha256()
    for f in files:
        digest.update(f.read_bytes())
    digest.update(sys.version.encode())
    folder = cache / digest.hexdigest()[:20]
    python = folder / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not (folder / "ready").exists():
        # Two jobs may want one environment at once (two checks of one project): one builds it, the other waits for
        # it, never removing a folder the first is building. A lock outlives no build: venv and pip are given at most
        # BUILD_SECONDS together.
        try:
            with filelock.exclusive(folder.with_name(folder.name + ".lock"), stale=BUILD_SECONDS + 60,
                                    timeout=BUILD_SECONDS + 120, poll=0.5):
                if not (folder / "ready").exists():
                    ctx.progress("environment", msg=f"python from {', '.join(f.name for f in files)}")
                    _build(ctx, folder, python, files)
        except filelock.LockTimeout as error:
            raise EnvironmentProblem(f"the environment for {what} is still being built by another job: "
                                     f"{error}") from None
    return str(python)


def _build(ctx: Context, folder: Path, python: Path, files: list[Path]):
    shutil.rmtree(folder, ignore_errors=True)
    wanted = any(line.strip() and not line.lstrip().startswith("#")
                 for f in files for line in f.read_text(encoding="utf-8").splitlines())
    # Files that list nothing need no pip, which may have no network to reach.
    done = ctx.run([console_python(), "-m", "venv", *([] if wanted else ["--without-pip"]), str(folder)],
                   timeout=VENV_SECONDS)
    if done.returncode != 0:
        raise EnvironmentProblem(f"python -m venv failed: {done.stdout.strip()[-400:]}")
    if wanted:
        args = [str(python), "-m", "pip", "install", "--disable-pip-version-check", "--no-input", "-q"]
        for f in files:
            args += ["-r", str(f)]
        done = ctx.run(args, timeout=PIP_SECONDS)
        if done.returncode != 0:
            shutil.rmtree(folder, ignore_errors=True)
            raise EnvironmentProblem(f"pip install failed: {done.stdout.strip()[-600:]}")
    (folder / "ready").write_text("ok", encoding="ascii")
