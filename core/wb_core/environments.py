"""Python environments made from a project's requirements files, once per set (keyed by their SHA-256 and the
interpreter's version), for commands that run a project's own Python: ci's jobs (``{python}``) and lab's scripts.
Where an environment lives is Workbench's business, never the project's, and nothing is written into its tree."""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
from pathlib import Path

from .module import Context, EnvironmentProblem


def default_cache() -> Path:
    """Where a host keeps environments when its config names no folder."""
    base = os.environ.get("XDG_CACHE_HOME")
    return (Path(base) if base else Path.home() / ".cache") / "wb" / "python"


def python_from(ctx: Context, files: list[Path], cache: Path, what: str) -> str:
    """The interpreter of the environment made from ``files`` (pip requirements files), built under ``cache`` the
    first time; Workbench's own interpreter when there are none. ``what`` names who asked, for messages."""
    if not files:
        return sys.executable
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
        ctx.progress("environment", msg=f"python from {', '.join(f.name for f in files)}")
        _build(ctx, folder, python, files)
    return str(python)


def _build(ctx: Context, folder: Path, python: Path, files: list[Path]):
    shutil.rmtree(folder, ignore_errors=True)
    wanted = any(line.strip() and not line.lstrip().startswith("#")
                 for f in files for line in f.read_text(encoding="utf-8").splitlines())
    # Files that list nothing need no pip, which may have no network to reach.
    done = ctx.run([sys.executable, "-m", "venv", *([] if wanted else ["--without-pip"]), str(folder)], timeout=600)
    if done.returncode != 0:
        raise EnvironmentProblem(f"python -m venv failed: {done.stdout.strip()[-400:]}")
    if wanted:
        args = [str(python), "-m", "pip", "install", "--disable-pip-version-check", "--no-input", "-q"]
        for f in files:
            args += ["-r", str(f)]
        done = ctx.run(args, timeout=1800)
        if done.returncode != 0:
            shutil.rmtree(folder, ignore_errors=True)
            raise EnvironmentProblem(f"pip install failed: {done.stdout.strip()[-600:]}")
    (folder / "ready").write_text("ok", encoding="ascii")
