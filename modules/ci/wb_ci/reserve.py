"""ci.run's memory reservation, read at submit from the jobs file of the commit it will run (resources.mem_from).

The daemon calls ``mem_gb`` in its own process when a ci.run is submitted, so this file uses only the standard library
and the core. The answer is the project's declared ``[project] reserve`` (project.toml), else the largest memory any
one of its container checks claims: the engine runs inside the reservation and always starts a check that runs alone,
so that is the least that lets every check have what it claims, and checks that fit together run side by side. A
project that wants its checks to run in parallel declares a reserve. With no answer (no such project, a commit the
daemon cannot read) the manifest's ``mem_gb`` stands."""

from __future__ import annotations

import ntpath
import posixpath
import subprocess
import tomllib
from pathlib import Path

DEFAULT_MEMORY = 4.0  # GiB a check claims without its own ``memory``: the engine's DEFAULT_MEMORY
JOBS_FILES = ("project.toml", "actions.toml")  # the engine's config_names(), the manifest first
SAFE = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "safe.directory", "GIT_CONFIG_VALUE_0": "*"}


def mem_gb(config: dict, roots: list, params: dict) -> tuple[float, str] | None:
    """(GiB, why) for a ci.run with ``params`` under ci's module ``config``, or None."""
    project = (config.get("projects") or {}).get(str(params.get("project") or ""))
    if not isinstance(project, dict) or not project.get("repo"):
        return None
    repo = _local(roots, _host_repo(config, str(project["repo"])))
    names = [str(project["config"]).replace("\\", "/")] if project.get("config") else list(JOBS_FILES)
    commit = str(params.get("commit") or "HEAD")
    for name in names:
        if commit == "worktree":
            folder = _local(roots, str(params["checkout"])) if params.get("checkout") else repo
            path = folder / name
            text = path.read_text(encoding="utf-8") if path.is_file() else None
        else:
            text = _show(repo, commit, name)
        if text is not None:
            return claim(tomllib.loads(text), Path(name).name)
    return None


def claim(data: dict, name: str) -> tuple[float, str] | None:
    """The reservation a parsed jobs file asks for: its project's reserve, else its largest single claim."""
    manifest = "checks" in data and "schema" in data
    if manifest:
        reserve = (data.get("project") or {}).get("reserve")
        if reserve is not None:
            return float(reserve), f"[project] reserve = {float(reserve):g} in {name}"
    jobs = data.get("checks" if manifest else "jobs") or {}
    default = float((data.get("settings") or {}).get("memory", DEFAULT_MEMORY))
    claims = {job: float(entry.get("memory", default)) for job, entry in jobs.items()
              if isinstance(entry, dict) and not (entry.get("on") == "host" or entry.get("gpu"))
              and not (entry.get("exclusive") or entry.get("native"))}
    if not claims:
        return None
    largest = max(claims, key=lambda job: claims[job])
    return claims[largest], f"the largest claim in {name}: {largest}, {claims[largest]:g} GB"


def _show(repo: Path, commit: str, name: str) -> str | None:
    import os
    done = subprocess.run(["git", "-C", str(repo), "show", f"{commit}:{name}"], capture_output=True, timeout=20,
                          env={**os.environ, **SAFE})
    return done.stdout.decode("utf-8") if done.returncode == 0 else None


def _host_repo(config: dict, repo: str) -> str:
    """As ci's own lookup: a relative repo is under ``projects_root``."""
    if (len(repo) > 2 and repo[1] == ":" and repo[2] in "\\/") or repo.startswith(("/", "\\\\")):
        return repo
    root = str(config.get("projects_root") or "")
    if not root:
        return repo
    return ntpath.join(root, repo) if len(root) > 1 and root[1] == ":" else posixpath.join(root, repo)


def _local(roots: list, value: str) -> Path:
    """A host path as the daemon sees it: through the configured roots, as ci's workers translate it."""
    from wb_core.paths import PathError, PathMap
    if not roots:
        return Path(value)
    try:
        return Path(PathMap(roots).resolve(value).container)
    except PathError:
        return Path(value)
