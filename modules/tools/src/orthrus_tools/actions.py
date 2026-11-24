"""Run a project's build and test jobs locally, like a small CI, and gate pushes on them.

    orthrus-tools actions list                     # workflows and jobs in actions.toml
    orthrus-tools actions run ci                   # a workflow, or jobs by name
    orthrus-tools actions run ci --if-needed       # only the jobs not yet passed for these trees
    orthrus-tools actions run quick                # the jobs the uncommitted changes can affect
    orthrus-tools actions run ci --dry-run         # print the commands only
    orthrus-tools actions status                   # has the workflow passed for these trees?
    orthrus-tools actions report                   # timing.md for the latest run
    orthrus-tools actions gate install             # a pre-push hook that requires the workflow

Jobs and workflows live in ``actions.toml`` at the project root (``[actions] config`` in
orthrus.toml names another file). Jobs run concurrently within a core and memory budget, each
after the jobs it ``needs``; a job's steps run in order and each step's output goes to
``.local/actions/<run>/<job>/<NN-step>.log``. Every run ends with a ``summary.json`` that
records the Git trees it tested, for the project and any other ``[repositories]``: the trees
their working trees would commit, so a pass before a commit names that commit's tree. The push
gate reads those records. A job that lists ``inputs`` is a pure check: when those files, its
steps and the configured identity match an earlier pass, that pass stands. Only one run at a
time. Exit status: 0 passed, 1 a job failed (or, for status and the gate, has not passed),
2 usage error.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import fnmatch
import glob
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import cli
from .cli import FINDING, OK, USAGE, Output, ToolError
from .config import project_root, tool_settings

SUMMARY_SCHEMA = "orthrus.actions.summary.v1"
DEFAULT_CONFIG = "actions.toml"
PROJECT_FILE = "project.toml"  # a project's manifest (schema 1); read before actions.toml when both are present
DEFAULT_RUNS = ".local/actions"
DEFAULT_SHARE = 0.25
DEFAULT_MEMORY = 4.0
MEMORY_FRACTION = 0.75
TIMING = "timing "
PIPE_GRACE = 5.0  # seconds a step's output is still read after its process exited
HOOK_MARKER = "orthrus-tools actions gate"
HOOK = """#!/bin/sh
# Installed by `orthrus-tools actions gate install`: refuses a push whose commits have not
# passed the {workflow} workflow. `git push --no-verify` skips it.
exec "{python}" -m orthrus_tools actions gate check --root "{root}" {repository} "$@"
"""
# A hook exports these for its own repository; they would point git at it from another one.
GIT_LOCATION = {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_PREFIX"}


class Stopped(Exception):
    """A run given a ``stop`` event saw it set: its steps were killed and no new job started."""


class Environment:
    """``{env.NAME}`` in a placeholder is the environment variable NAME of the run's environment, or empty."""

    def __init__(self, environ: Any = None) -> None:
        self._environ = environ

    def __getattr__(self, name: str) -> str:
        if name.startswith("_"):
            raise AttributeError(name)
        return (os.environ if self._environ is None else self._environ).get(name, "")


@dataclass
class Repository:
    name: str
    path: Path
    pin_file: str | None = None
    pin_key: str = "commit"
    # False for a checkout that jobs only build pinned commits from: its own tree is not what a
    # run tests, so it is not recorded, matched or gated, and its new commits leave passes standing.
    tested: bool = True
    # The records folder (beside this project's) of the project this repository is: when set, a pinned commit
    # must have passed that project's own required checks, so a consumer need not build and test it again.
    passed_by: str | None = None


@dataclass
class Config:
    root: Path
    path: Path
    data: dict[str, Any]
    repositories: list[Repository]
    runs: Path
    # What a run sees, given explicitly by a caller that runs the engine as a library (see ``execute``). Left None,
    # the engine reads this process's environment, its own interpreter and the identity variables, as the CLI does.
    environ: dict[str, str] | None = None
    python: str | None = None
    identity: dict[str, Any] | None = None
    stop: threading.Event | None = None

    @property
    def env(self) -> Any:
        """The environment the run's steps start from and ``{env.NAME}`` reads."""
        return os.environ if self.environ is None else self.environ

    @property
    def set_identity(self) -> str:
        """The set's identity (a tree of its own in the pass key): ``identity["set"]``, else ``WB_SET_IDENTITY``."""
        if self.identity is not None:
            return str(self.identity.get("set") or "")
        return self.env.get(SET_IDENTITY, "")

    @property
    def jdk_identity(self) -> str:
        """The platform's JDK digest (in a pure check's key): ``identity["jdk"]``, else ``WB_JDK_IDENTITY``."""
        if self.identity is not None:
            return str(self.identity.get("jdk") or "")
        return self.env.get(JDK_IDENTITY, "")

    @property
    def tools_roots(self) -> dict[str, str]:
        """Where the caller laid out each project's files a check reads: ``identity["tools"]``, else the JSON in
        ``WB_TOOLS_ROOTS``."""
        if self.identity is not None:
            return {str(k): str(v) for k, v in (self.identity.get("tools") or {}).items()}
        return dict(json.loads(self.env.get(TOOLS_ROOTS) or "{}"))

    @property
    def settings(self) -> dict[str, Any]:
        return dict(self.data.get("settings", {}))

    @property
    def jobs(self) -> dict[str, dict[str, Any]]:
        return dict(self.data.get("jobs", {}))

    @property
    def workflows(self) -> dict[str, list[str]]:
        return dict(self.data.get("workflows", {}))

    @property
    def workflow(self) -> str:
        """The workflow ``quick`` narrows and the push gate requires."""
        return str(self.settings.get("workflow", "ci"))

    @property
    def tested(self) -> list[Repository]:
        """The repositories whose trees a run records: the project first, then the others."""
        return [repository for repository in self.repositories if repository.tested]

    @property
    def names(self) -> tuple[str, ...]:
        """The trees a pass is keyed on: each tested repository's, and the run's set identity when it builds from a
        set (``WB_SET_IDENTITY``), so a platform or line move needs a new pass."""
        own = tuple(repository.name for repository in self.tested)
        return own + ((SET_TREE,) if self.set_identity else ())

    def required(self) -> list[str]:
        if self.workflow not in self.workflows:
            raise ToolError(USAGE, f"{self.path.name} has no [workflows] {self.workflow}")
        return list(self.workflows[self.workflow])


# --- configuration ---------------------------------------------------------------------------


def config_path(root: Path, given: Path | None = None) -> Path:
    if given is not None:
        return given if given.is_absolute() else root / given
    configured = tool_settings(root, "actions").get("config")
    if configured:
        return root / str(configured)
    return root / (PROJECT_FILE if (root / PROJECT_FILE).is_file() else DEFAULT_CONFIG)


def config_names() -> tuple[str, str]:
    """The files a commit's checks may live in, the manifest first."""
    return PROJECT_FILE, DEFAULT_CONFIG


def on_host(job: dict) -> bool:
    """Whether a check runs on the host: ``on = "host"``, or ``gpu = true`` (only the host has the GPU). Such a check
    never runs in a container and cannot be in the gate."""
    return job.get("on") == "host" or bool(job.get("gpu"))


# project.toml (schema 1): what a repository says about its checks and the files they make.
PROJECT_KEYS = {"schema", "project", "uses", "artifacts", "checks", "gate", "launch", "settings"}
PROJECT_TABLE_KEYS = {"name", "python", "branch", "own", "reserve"}
CHECK_KEYS = {"description", "steps", "needs", "inputs", "tools", "locks", "memory", "share", "priority", "on",
              "gpu", "timeout", "exclusive"}
ARTIFACT_KEYS = {"check", "file", "mod_id"}


def from_project(data: dict, name: str) -> dict:
    """A project manifest as the engine's own table: each check a job, the gate a workflow, each artifact's glob on
    the check that makes it. Unknown keys are refused, naming the file and the key."""
    def refuse(text: str):
        raise ToolError(USAGE, f"{name}: {text}")

    if data.get("schema") != 1:
        refuse("schema must be 1")
    for key in sorted(set(data) - PROJECT_KEYS):
        refuse(f"unknown key {key}")
    project = data.get("project") or {}
    for key in sorted(set(project) - PROJECT_TABLE_KEYS):
        refuse(f"unknown key project.{key}")
    if not isinstance(project.get("name"), str) or not project["name"]:
        refuse("project.name is missing")
    checks = data.get("checks") or {}
    if not isinstance(checks, dict) or not checks:
        refuse("no [checks]")
    jobs: dict[str, dict] = {}
    for check, entry in checks.items():
        if "gate" in entry:
            refuse(f"checks.{check}.gate: gate is a top-level key, so it goes above the first [table]; below one, "
                   "TOML puts it in that table")
        for key in sorted(set(entry) - CHECK_KEYS):
            refuse(f"unknown key checks.{check}.{key}")
        steps = []
        for index, step in enumerate(entry.get("steps") or [], 1):
            if isinstance(step, list):  # {gradle}: the set's Gradle, through the launcher Workbench writes per run
                step = [part for word in step for part in
                        (["{python}", "{env.WB_GRADLE}"] if word == "{gradle}" else [word])]
            if isinstance(step, dict) and isinstance(step.get("run"), list):
                step = {**step, "run": [part for word in step["run"] for part in
                                        (["{python}", "{env.WB_GRADLE}"] if word == "{gradle}" else [word])]}
            if isinstance(step, list):  # an argv list, named by what it runs
                words = [w for w in step if not (w.startswith("{") and w.endswith("}")) and not w.startswith("-")] or step
                if step[:2] == ["{python}", "{env.WB_GRADLE}"]:
                    words = ["gradle", *[w for w in step[2:] if not w.startswith("-")]]
                steps.append({"name": " ".join(Path(w).name for w in words[:2]) or f"step {index}", "run": step})
            elif isinstance(step, dict):
                steps.append(step)
            else:
                refuse(f"checks.{check}.steps: a step is an argument list or {{ name, run, cwd }}")
        tools = entry.get("tools") or []
        if not isinstance(tools, list) or not all(isinstance(t, str) and re.fullmatch(r"[a-z0-9][a-z0-9._-]*:[^:]+", t)
                                                  for t in tools):
            refuse(f"checks.{check}.tools lists files another project's tree holds, each '<project>:<glob>'")
        jobs[check] = {**{k: v for k, v in entry.items() if k not in ("steps", "tools")}, "steps": steps,
                       **({"tools": list(tools)} if tools else {})}
    artifacts = data.get("artifacts") or {}
    for artifact, entry in artifacts.items():
        for key in sorted(set(entry) - ARTIFACT_KEYS):
            refuse(f"unknown key artifacts.{artifact}.{key}")
        check, glob = entry.get("check"), entry.get("file")
        if check not in jobs:
            refuse(f"artifacts.{artifact}.check names no check: {check}")
        if not isinstance(glob, str) or not glob:
            refuse(f"artifacts.{artifact}.file is the glob that finds its one file")
        jobs[check].setdefault("artifacts", []).append(glob)
    gate = data.get("gate", [])
    if not isinstance(gate, list) or not all(g in jobs for g in gate):
        refuse("gate lists checks by name")
    for check in gate:
        if on_host(jobs[check]):
            refuse(f"gate: {check} runs on the host or the GPU, so a push cannot wait for it")
    settings = {"name": project["name"], "workflow": "gate",
                "identity_files": ["{env.JAVA_HOME}/release"], **(data.get("settings") or {})}
    return {"settings": settings, "workflows": {"gate": list(gate), "all": list(jobs)}, "jobs": jobs,
            "project": project, "artifacts": artifacts, "uses": data.get("uses") or {}}


def load(root: Path, given: Path | None = None, *, environ: dict[str, str] | None = None) -> Config:
    """The jobs file of ``root`` (``given`` names another). ``environ`` is the environment the run will see (a
    repository's ``env`` location is read from it); None reads this process's."""
    path = config_path(root, given)
    if not path.is_file():
        raise ToolError(USAGE, f"no actions configuration at {path} (see docs/actions.md)")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ToolError(USAGE, f"{path.name}: {error}") from None
    if path.name == PROJECT_FILE or ("checks" in data and "schema" in data):
        data = from_project(data, path.name)
    settings = data.get("settings", {})
    project = str(settings.get("name", "project"))
    repositories = [Repository(project, root)]
    for name, entry in data.get("repositories", {}).items():
        if not isinstance(entry, dict) or "path" not in entry:
            raise ToolError(USAGE, f"{path.name}: [repositories.{name}] needs a path")
        variable = entry.get("env")
        location = (os.environ if environ is None else environ).get(variable) if variable else None
        checkout = Path(location) if location else root / str(entry["path"])
        pin = entry.get("pin", {})
        tested = entry.get("tested", True)
        if not isinstance(tested, bool) or (pin and not tested):
            raise ToolError(USAGE, f"{path.name}: [repositories.{name}] tested is true or false, "
                                   "and a pinned repository is tested")
        passed_by = entry.get("passed")
        if passed_by is not None and (not isinstance(passed_by, str) or not passed_by or not pin):
            raise ToolError(USAGE, f"{path.name}: [repositories.{name}] passed names the project whose records "
                                   "show the pinned commit passed its own checks, and needs a pin")
        repositories.append(
            Repository(name, checkout.resolve(), pin.get("file"), str(pin.get("key", "commit")), tested,
                       passed_by=passed_by)
        )
    runs = root / str(settings.get("runs", DEFAULT_RUNS))
    config = Config(root, path, data, repositories, runs, environ=environ)
    validate(config)
    return config


def validate(config: Config) -> None:
    name = config.path.name
    jobs = config.jobs
    if not jobs:
        raise ToolError(USAGE, f"{name} defines no [jobs]")
    for job, entry in jobs.items():
        steps = entry.get("steps")
        if not isinstance(steps, list) or not steps:
            raise ToolError(USAGE, f"{name}: job {job} has no steps")
        for step in steps:
            run = step.get("run") if isinstance(step, dict) else None
            if not isinstance(run, list) or not run or not all(isinstance(a, str) for a in run):
                raise ToolError(USAGE, f"{name}: every step of {job} needs run = [arguments]")
            if not isinstance(step.get("name"), str):
                raise ToolError(USAGE, f"{name}: every step of {job} needs a name")
        for need in entry.get("needs", []):
            if need not in jobs:
                raise ToolError(USAGE, f"{name}: job {job} needs unknown job {need}")
        artifacts = entry.get("artifacts", [])
        if not isinstance(artifacts, list) or not all(isinstance(a, str) and a for a in artifacts):
            raise ToolError(USAGE, f"{name}: job {job} lists its artifacts as glob patterns")
        if artifacts and "inputs" in entry:
            raise ToolError(USAGE, f"{name}: job {job} keeps artifacts, so it builds them every run and cannot "
                                   "be a cached check with inputs")
    for workflow, members in config.workflows.items():
        for member in members:
            if member not in jobs:
                raise ToolError(USAGE, f"{name}: workflow {workflow} lists unknown job {member}")
    if "quick" in jobs or "quick" in config.workflows:
        raise ToolError(USAGE, f"{name}: 'quick' is reserved for the changed-paths selection")


def resolve(config: Config, targets: Iterable[str]) -> list[str]:
    """Expand workflows to jobs and add what they need, in declaration order."""
    jobs, workflows = config.jobs, config.workflows
    wanted: list[str] = []
    for target in targets:
        if target in workflows:
            wanted += workflows[target]
        elif target in jobs:
            wanted.append(target)
        else:
            raise ToolError(USAGE, f"unknown workflow or job: {target} (see actions list)")
    ordered: list[str] = []

    def visit(name: str, path: tuple[str, ...] = ()) -> None:
        if name in path:
            raise ToolError(USAGE, "dependency cycle: " + " -> ".join((*path, name)))
        if name in ordered:
            return
        for need in jobs[name].get("needs", []):
            visit(need, (*path, name))
        ordered.append(name)

    for name in wanted:
        visit(name)
    return ordered


def placeholders(config: Config, run_id: str) -> dict[str, Any]:
    """Values for ``{name}`` in steps: python, run_id, run_dir, root, each repository by name,
    each interpreter, ``env.NAME`` and the ``[placeholders]`` table, in that order."""
    values: dict[str, Any] = {
        "python": config.python or sys.executable,
        "run_id": run_id,
        "run_dir": str(config.runs / run_id),
        "root": str(config.root),
        "env": Environment(config.environ),
    }
    for repository in config.repositories:
        values[repository.name] = str(repository.path)
    for key, candidates in config.data.get("interpreters", {}).items():
        options = [candidates] if isinstance(candidates, str) else list(candidates)
        paths = [config.root / option for option in options]
        values[key] = str(next((path for path in paths if path.is_file()), paths[0]))
    for key, template in config.data.get("placeholders", {}).items():
        values[key] = expand_one(str(template), values, f"[placeholders] {key}")
    return values


def expand_one(argument: str, values: dict[str, Any], where: str) -> str:
    try:
        return argument.format_map(values)
    except (KeyError, AttributeError, IndexError, ValueError) as error:
        raise ToolError(USAGE, f"{where}: cannot expand {argument!r} ({error})") from None


def expand(arguments: list[str], values: dict[str, Any], where: str = "step") -> list[str]:
    return [expand_one(argument, values, where) for argument in arguments]


# --- trees and passes ------------------------------------------------------------------------


def git(repo: Path, *arguments: str, index: Path | None = None, environ: Any = None) -> str:
    environment = {k: v for k, v in (os.environ if environ is None else environ).items() if k not in GIT_LOCATION}
    if index is not None:
        environment["GIT_INDEX_FILE"] = str(index)
    return subprocess.run(
        ["git", *arguments],
        cwd=repo,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    ).stdout.strip()


def working_tree(repo: Path, exclude: Path | None = None, environ: Any = None) -> str | None:
    """The tree ``git add --all`` would commit in repo, written through a scratch copy of its
    index so the real one is untouched; None without a checkout. ``exclude`` (the runs folder)
    stays out of the tree even when the project does not ignore it. git runs in ``environ`` (None: this
    process's environment)."""
    if not (repo / ".git").exists():
        return None
    environ = os.environ if environ is None else environ
    index = repo / git(repo, "rev-parse", "--git-path", "index", environ=environ)
    with tempfile.TemporaryDirectory() as scratch:
        scratch_index = Path(scratch) / "index"
        if index.is_file():
            # The copy keeps the index's file stamps, so only changed files are read again.
            shutil.copyfile(index, scratch_index)
        pathspec = ["--", "."]
        if exclude is not None and exclude.resolve().is_relative_to(repo.resolve()):
            relative = exclude.resolve().relative_to(repo.resolve()).as_posix()
            ignored = subprocess.run(
                ["git", "check-ignore", "-q", relative + "/"],
                cwd=repo,
                capture_output=True,
                env={k: v for k, v in environ.items() if k not in GIT_LOCATION},
            )
            # git refuses an exclude pathspec naming an ignored path, which needs none anyway.
            if ignored.returncode != 0:
                pathspec.append(f":(exclude){relative}")
        git(repo, "add", "--all", *pathspec, index=scratch_index, environ=environ)
        return git(repo, "write-tree", index=scratch_index, environ=environ)


def pin_value(document: Any, key: str) -> Any:
    """The commit a pin key names in a lock file: a top-level key, else a dotted path into nested tables
    (``orthlib.commit``). KeyError when it names nothing."""
    if isinstance(document, dict) and key in document:
        return document[key]
    value = document
    for part in key.split("."):
        if not isinstance(value, dict) or part not in value:
            raise KeyError(key)
        value = value[part]
    return value


# A run that builds from a set is given two digests of it: the platform and the lines and third entries the project
# uses (a tree of its own in the pass key), and the platform's JDK builds (in a pure check's key, which replaces
# identity_files). A platform or line move then needs a new pass; a pure check reruns only on a JDK move.
SET_IDENTITY = "WB_SET_IDENTITY"
JDK_IDENTITY = "WB_JDK_IDENTITY"
SET_TREE = "@set"  # not a project name ([a-z0-9._-]), so it never takes a repository's place (the Set is "set")
# Where ci.run laid out each project's files a check reads (tools): {"<project>": "<folder>"} as JSON.
TOOLS_ROOTS = "WB_TOOLS_ROOTS"


def tools_files(job: dict[str, Any], roots: dict[str, str] | None = None) -> list[tuple[str, Path]]:
    """The files a check's tools globs match where ci.run laid them out (``roots``, else ``WB_TOOLS_ROOTS``), each
    with its label (<project>:<path>)."""
    if roots is None:
        roots = json.loads(os.environ.get(TOOLS_ROOTS) or "{}")
    found = []
    for item in job.get("tools") or []:
        project, _, glob = str(item).partition(":")
        if project in roots:
            base = Path(roots[project])
            found += [(f"{project}:{p.relative_to(base).as_posix()}", p) for p in base.glob(glob) if p.is_file()]
    return sorted(set(found))


def trees(config: Config) -> dict[str, str | None]:
    """Each repository's working tree, and the set identity when the run has one; None where git could not read one."""
    found: dict[str, str | None] = {}
    for repository in config.tested:
        try:
            found[repository.name] = working_tree(repository.path, config.runs, config.env)
        except (OSError, subprocess.SubprocessError):
            found[repository.name] = None
    if config.set_identity:
        found[SET_TREE] = config.set_identity
    return found


def passed_jobs(runs: Path, names: tuple[str, ...]) -> dict[tuple[str, ...], set[str]]:
    """The jobs passed so far, by the trees (in ``names`` order) they ran on. A run counts when
    it recorded every tree and none changed while it ran."""
    passed: dict[tuple[str, ...], set[str]] = {}
    for path in sorted(runs.glob("*/summary.json")):
        try:
            summary = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        tested = summary.get("trees") or {}
        if summary.get("treesChanged", True) or not all(tested.get(n) for n in names):
            continue
        key = tuple(tested[n] for n in names)
        jobs = passed.setdefault(key, set())
        jobs.update(n for n, r in summary.get("jobs", {}).items() if r.get("status") == "passed")
    return passed


def match(pattern: str, path: str) -> bool:
    """Whether a ``/``-separated path matches a glob in which ``**`` crosses folders."""

    def parts(pat: list[str], name: list[str]) -> bool:
        if not pat:
            return not name
        if pat[0] == "**":
            return any(parts(pat[1:], name[index:]) for index in range(len(name) + 1))
        return bool(name) and fnmatch.fnmatchcase(name[0], pat[0]) and parts(pat[1:], name[1:])

    return parts(pattern.strip("/").split("/"), path.strip("/").split("/"))


def affected(rules: list[dict[str, Any]], paths: list[str], everything: list[str]) -> set[str]:
    """The jobs of the first rule each path matches; every job for a path no rule matches."""
    jobs: set[str] = set()
    for path in paths:
        rule = next((r for r in rules if any(match(p, path) for p in r.get("paths", []))), None)
        jobs.update(everything if rule is None else rule.get("jobs", []))
    return jobs


def quick_jobs(config: Config, tested: dict[str, str | None]) -> tuple[list[str], dict[str, int]]:
    """The workflow's jobs that each repository's changes since its HEAD can affect, and how
    many paths changed in each; every job when a tree could not be read."""
    required = config.required()
    rules = config.data.get("quick", {})
    wanted: set[str] = set()
    changed: dict[str, int] = {}
    for repository in config.tested:
        tree = tested.get(repository.name)
        if tree is None:
            wanted.update(required)
            continue
        try:
            paths = git(
                repository.path, "diff", "--no-renames", "--name-only", "HEAD^{tree}", tree
            ).splitlines()
        except (OSError, subprocess.SubprocessError):  # no commit yet
            wanted.update(required)
            continue
        changed[repository.name] = len(paths)
        wanted |= affected(rules.get(repository.name, []), paths, required)
    return [job for job in required if job in wanted], changed


# --- the budget ------------------------------------------------------------------------------


def physical_memory_gib() -> float:
    """Installed memory in GiB, or 16 when it cannot be read."""
    try:
        if sys.platform == "win32":
            import ctypes

            class Status(ctypes.Structure):
                _fields_ = [
                    ("length", ctypes.c_ulong),
                    ("load", ctypes.c_ulong),
                    ("total", ctypes.c_ulonglong),
                    ("available", ctypes.c_ulonglong),
                    ("totalPage", ctypes.c_ulonglong),
                    ("availablePage", ctypes.c_ulonglong),
                    ("totalVirtual", ctypes.c_ulonglong),
                    ("availableVirtual", ctypes.c_ulonglong),
                    ("availableExtended", ctypes.c_ulonglong),
                ]

            status = Status()
            status.length = ctypes.sizeof(Status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return float(status.total) / 2**30
            return 16.0
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2**30
    except (OSError, ValueError, AttributeError):
        return 16.0


class Budget:
    """Which jobs may start beside the running ones. A job claims ``share`` of the cores,
    ``memory`` GiB and its ``locks``; claims must fit the machine and no lock be held twice.
    ``exclusive`` jobs run alone. A job always starts when nothing else runs."""

    def __init__(
        self,
        jobs: dict[str, Any],
        concurrency: int,
        memory: float,
        share: float = DEFAULT_SHARE,
        job_memory: float = DEFAULT_MEMORY,
    ) -> None:
        self.jobs = jobs
        self.concurrency = concurrency
        self.memory = memory
        self.share = share
        self.job_memory = job_memory
        self.running: set[str] = set()

    def claim(self, name: str) -> tuple[float, float, set[str]]:
        job = self.jobs[name]
        if job.get("exclusive") or job.get("native"):
            return 1.0, self.memory, {"*"}
        return (
            float(job.get("share", self.share)),
            float(job.get("memory", self.job_memory)),
            set(job.get("locks", [])),
        )

    def fits(self, name: str) -> bool:
        if not self.running:
            return True
        if self.concurrency and len(self.running) >= self.concurrency:
            return False
        share, memory, locks = self.claim(name)
        held: set[str] = set()
        for other in self.running:
            other_share, other_memory, other_locks = self.claim(other)
            share, memory = share + other_share, memory + other_memory
            held |= other_locks
        if "*" in held or "*" in locks or locks & held:
            return False
        return share <= 1.0 + 1e-9 and memory <= self.memory + 1e-9


def budget(config: Config, concurrency: int, memory: float | None = None) -> Budget:
    """The run's budget: ``memory`` GiB when the caller reserved it (a Workbench job's
    reservation), else ``memory_fraction`` of the machine's memory."""
    settings = config.settings
    if memory is None:
        memory = physical_memory_gib() * float(settings.get("memory_fraction", MEMORY_FRACTION))
    return Budget(
        config.jobs,
        concurrency,
        memory,
        float(settings.get("share", DEFAULT_SHARE)),
        float(settings.get("memory", DEFAULT_MEMORY)),
    )


# --- the input cache -------------------------------------------------------------------------


def glob_files(root: Path, pattern: str) -> list[Path]:
    """Files a glob matches; relative globs start at root, absolute ones at their fixed part."""
    path = Path(pattern)
    if not path.is_absolute():
        return [p for p in root.glob(pattern) if p.is_file()]
    parts = path.parts
    fixed = next((i for i, part in enumerate(parts) if any(c in part for c in "*?[")), len(parts))
    base = Path(*parts[:fixed])
    rest = "/".join(parts[fixed:])
    if not rest:
        return [base] if base.is_file() else []
    return [p for p in base.glob(rest) if p.is_file()]


def identity(config: Config, values: dict[str, Any]) -> list[str]:
    """What else a pure check's verdict depends on: ``identity_files`` contents and
    ``identity_env`` values from ``[settings]``."""
    lines = []
    settings = config.settings
    for template in settings.get("identity_files", []):
        name = expand_one(str(template), values, "[settings] identity_files")
        target = Path(name) if Path(name).is_absolute() else config.root / name
        try:
            lines.append(f"{template}: {hashlib.sha256(target.read_bytes()).hexdigest()}")
        except OSError:
            lines.append(f"{template}: missing")
    for variable in settings.get("identity_env", []):
        lines.append(f"${variable}={config.env.get(variable, '')}")
    if config.jdk_identity:
        lines.append(f"platform jdk {config.jdk_identity}")
    return lines


def input_key(config: Config, name: str, values: dict[str, Any]) -> str | None:
    """A digest of everything a cacheable job reads: its steps, the files its ``inputs`` match
    (by path and content) and the identity; None for a job without inputs."""
    job = config.jobs[name]
    patterns = job.get("inputs")
    if not patterns:
        return None
    expanded = expand([str(p) for p in patterns], values, f"inputs of {name}")
    digest = hashlib.sha256()
    digest.update(json.dumps([name, job["steps"], patterns], sort_keys=True).encode())
    files = sorted({p for pattern in expanded for p in glob_files(config.root, pattern)})
    for path in files:
        digest.update(input_label(config, path).encode() + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    for label, path in tools_files(job, config.tools_roots):  # another project's files, as laid out for the run
        digest.update(label.encode() + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    for line in identity(config, values):
        digest.update(line.encode() + b"\0")
    return digest.hexdigest()


def input_label(config: Config, path: Path) -> str:
    """An input file's name in a cache key: relative to the repository holding it, so a key is the
    same wherever the checkouts are (a clean clone is in a new folder for every run)."""
    for repository in config.repositories:
        try:
            relative = path.resolve().relative_to(repository.path.resolve()).as_posix()
        except ValueError:
            continue
        return relative if repository is config.repositories[0] else f"{repository.name}:{relative}"
    return path.as_posix()


def cached_pass(config: Config, name: str, key: str | None) -> dict[str, Any] | None:
    if key is None:
        return None
    try:
        record: dict[str, Any] = json.loads(
            (config.runs / "cache" / f"{name}-{key}.json").read_text(encoding="utf-8")
        )
        return record
    except (OSError, ValueError):
        return None


def record_pass(
    config: Config, name: str, key: str | None, run_id: str, result: dict[str, Any]
) -> None:
    if key is None or result["status"] != "passed":
        return
    cache = config.runs / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    record = {"job": name, "key": key, "runId": run_id, "steps": result["steps"]}
    # Whole or not at all: two runs of one project may pass the same key at once, and a reader must never see half.
    target = cache / f"{name}-{key}.json"
    part = target.with_name(f"{target.name}.{os.getpid()}.{threading.get_ident()}.part")
    try:
        part.write_text(json.dumps(record, indent=2), encoding="utf-8")
        for attempt in range(50):
            try:
                part.replace(target)
                break
            except PermissionError:  # Windows: a reader has the record open; it is the same pass either way
                if attempt == 49:
                    raise
                time.sleep(0.02 * (attempt + 1))
    finally:
        part.unlink(missing_ok=True)


# --- running ---------------------------------------------------------------------------------


def step_log_name(index: int, name: str) -> str:
    return f"{index:02d}-" + (re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-") or "step") + ".log"


def run_step(
    command: list[str],
    log: Path,
    cwd: Path,
    env: dict[str, str],
    echo: Any = None,
    *,
    base: Any = None,
    stop: threading.Event | None = None,
) -> tuple[int, float, list[Any]]:
    """(exit code, seconds, timing records). A step may print ``timing {json}`` lines, which the
    summary keeps and ``actions report`` tabulates. A command that cannot start exits 127. The step's
    environment is ``base`` (None: this process's) with ``env`` over it. When ``stop`` is set while the step
    runs, the step and what it started are killed."""
    started = time.monotonic()
    timings: list[Any] = []
    with log.open("w", encoding="utf-8", errors="replace") as stream:
        stream.write("$ " + " ".join(command) + "\n\n")
        stream.flush()
        try:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                env={**(os.environ if base is None else base), "PYTHONUNBUFFERED": "1", **env},
                # A step that may be stopped leads a session of its own, so killing its group reaches what it started.
                start_new_session=stop is not None and os.name != "nt",
            )
        except OSError as error:
            stream.write(f"cannot start {command[0]}: {error}\n")
            return 127, time.monotonic() - started, timings
        assert process.stdout is not None
        lock = threading.Lock()

        def pump() -> None:
            try:
                for line in process.stdout:
                    with lock:
                        stream.write(line)
                    if line.startswith(TIMING):
                        try:
                            timings.append(json.loads(line[len(TIMING) :]))
                        except ValueError:
                            pass
                    if echo is not None:
                        echo(line.rstrip("\n"))
            except (OSError, ValueError):
                pass

        # A step ends when its process exits: a process it started that keeps the pipe open (a Gradle daemon)
        # gets a few seconds to finish writing and is then left behind, never waited for.
        reader = threading.Thread(target=pump, name="step-output", daemon=True)
        reader.start()
        code = _wait(process, stop)
        if code is None:
            with lock:
                stream.write("\n(stopped: the run was stopped)\n")
            code = process.wait()
        reader.join(PIPE_GRACE)
        with lock:
            if reader.is_alive():
                stream.write("\n(the step exited; output a process it started still holds open is not kept)\n")
            stream.flush()
        if not reader.is_alive():
            process.stdout.close()  # the engine may live on in its caller's process: no pipe is left behind
    return code, time.monotonic() - started, timings


def _wait(process: subprocess.Popen[str], stop: threading.Event | None) -> int | None:
    """The step's exit code; None when ``stop`` was set first, after killing the step and what it started."""
    if stop is None:
        return process.wait()
    while True:
        try:
            return process.wait(timeout=0.25)
        except subprocess.TimeoutExpired:
            if stop.is_set():
                _kill_tree(process)
                return None


def _kill_tree(process: subprocess.Popen[str]) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False)
    else:
        try:
            os.killpg(process.pid, 9)
        except OSError:
            pass
    try:
        process.kill()
    except OSError:
        pass


def tail(log: Path, lines: int = 15) -> str:
    text = log.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join("    | " + line for line in text[-lines:])


def run_job(
    config: Config,
    name: str,
    values: dict[str, Any],
    run_dir: Path,
    out: Output,
    verbose: bool,
) -> dict[str, Any]:
    """Run one job's steps in order, stopping at the first failure."""
    job = config.jobs[name]
    steps: list[dict[str, Any]] = []
    status = "passed"
    for index, step in enumerate(job["steps"], 1):
        where = f"job {name}, step {step['name']}"
        command = expand(step["run"], values, where)
        cwd_text = expand_one(str(step.get("cwd", ".")), values, where)
        cwd = Path(cwd_text) if Path(cwd_text).is_absolute() else config.root / cwd_text
        env = {k: expand_one(str(v), values, where) for k, v in step.get("env", {}).items()}
        log = run_dir / name / step_log_name(index, step["name"])
        log.parent.mkdir(parents=True, exist_ok=True)

        def echo(line: str) -> None:
            out.line(f"    [{name}] {line}")

        code, seconds, timings = run_step(command, log, cwd, env, echo if verbose else None, base=config.env,
                                          stop=config.stop)
        entry: dict[str, Any] = {
            "name": step["name"],
            "exitCode": code,
            "seconds": round(seconds, 1),
            "log": log.relative_to(config.root).as_posix()
            if log.is_relative_to(config.root)
            else str(log),
        }
        if timings:
            entry["timing"] = timings
        steps.append(entry)
        ok = code == 0
        out.line(f"  {'ok  ' if ok else 'FAIL'} {name} / {step['name']:<22} {seconds:7.1f} s")
        if not ok:
            out.line(tail(log))
            status = "failed"
            break
    result: dict[str, Any] = {"status": status, "steps": steps}
    if status == "passed" and job.get("artifacts"):
        try:
            result["artifacts"] = keep_artifacts(config, name, values, run_dir)
            out.line(f"  kept {len(result['artifacts'])} artifact(s) of {name}")
        except ToolError as error:
            result.update(status="failed", error=error.message)
            out.line(f"  FAIL {name}: {error.message}")
    return result


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def keep_artifacts(config: Config, name: str, values: dict[str, Any], run_dir: Path) -> dict[str, dict[str, Any]]:
    """Copy the files a passed job's ``artifacts`` globs match (relative to the root, or absolute; ``**`` crosses
    folders) into ``<run>/<job>/artifacts``, each with its SHA-256, so what a run built stays with the run that
    vouches for it. A pattern that matches no file, or two files of one name, fails the job."""
    target = run_dir / name / "artifacts"
    kept: dict[str, dict[str, Any]] = {}
    for pattern in config.jobs[name]["artifacts"]:
        expanded = expand_one(pattern, values, f"job {name}, artifacts")
        found = [Path(p) if Path(p).is_absolute() else config.root / p
                 for p in glob.glob(expanded, root_dir=config.root, recursive=True)]
        files = sorted(path for path in found if path.is_file())
        if not files:
            raise ToolError(FINDING, f"no file matches its artifact {pattern}")
        for source in files:
            if source.name in kept:
                raise ToolError(FINDING, f"two of its artifacts are named {source.name}")
            target.mkdir(parents=True, exist_ok=True)
            destination = target / source.name
            shutil.copy2(source, destination)
            kept[source.name] = {
                "sha256": file_sha256(destination),
                "bytes": destination.stat().st_size,
                "from": source.relative_to(config.root).as_posix()
                if source.is_relative_to(config.root)
                else str(source),
            }
    return kept


def run_jobs(
    config: Config,
    order: list[str],
    values: dict[str, Any],
    run_dir: Path,
    results: dict[str, dict[str, Any]],
    out: Output,
    *,
    keep_going: bool = False,
    verbose: bool = False,
    concurrency: int = 0,
    use_cache: bool = True,
    machine: Budget | None = None,
    memory: float | None = None,
) -> None:
    """Run the ordered jobs, several at once within the budget, each after the jobs it needs.
    Jobs start in priority order (``priority``, higher first, then declaration order). After a
    failure no new job starts unless keep_going; every job not run is recorded as skipped."""
    jobs = config.jobs
    plan = machine or budget(config, concurrency, memory)
    pending = sorted(order, key=lambda n: (-jobs[n].get("priority", 0), order.index(n)))
    active: dict[concurrent.futures.Future[dict[str, Any]], str] = {}
    keys: dict[str, str | None] = {}
    begun: dict[str, float] = {}
    clock = time.monotonic()
    workers = concurrency or max(1, len(order))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        while pending or active:
            if config.stop is not None and config.stop.is_set():
                # The running steps are being killed, so their jobs end failed; nothing new starts.
                for name in pending:
                    results[name] = {"status": "skipped", "reason": "stopped"}
                pending.clear()
            failed = any(r["status"] == "failed" for r in results.values())
            for name in list(pending):
                needs = jobs[name].get("needs", [])
                blocked = [n for n in needs if n in results and results[n]["status"] != "passed"]
                if blocked or (failed and not keep_going):
                    pending.remove(name)
                    reason = f"needs {', '.join(blocked)}" if blocked else "after failure"
                    results[name] = {"status": "skipped", "reason": reason}
                    out.line(f"- {name:<16} skipped ({reason})")
                    continue
                if any(n not in results for n in needs if n in order):
                    continue
                if not plan.fits(name):
                    continue
                pending.remove(name)
                key = input_key(config, name, values)
                earlier = cached_pass(config, name, key) if use_cache else None
                if earlier is not None:
                    results[name] = {
                        "status": "passed",
                        "cached": earlier.get("runId"),
                        "steps": [],
                        "inputKey": key,
                    }
                    out.line(f"= {name:<16} passed for these inputs in run {earlier.get('runId')}")
                    continue
                plan.running.add(name)
                out.line(f"> {name}")
                keys[name] = key
                begun[name] = time.monotonic() - clock
                future = pool.submit(run_job, config, name, values, run_dir, out, verbose)
                active[future] = name
            if not active:
                continue
            done, _ = concurrent.futures.wait(
                active,
                timeout=None if config.stop is None else 0.25,
                return_when=concurrent.futures.FIRST_COMPLETED,
            )
            for future in done:
                name = active.pop(future)
                plan.running.discard(name)
                try:
                    results[name] = future.result()
                except ToolError as error:
                    results[name] = {"status": "failed", "steps": [], "error": error.message}
                    out.error(f"{name}: {error.message}")
                except Exception as error:  # a runner bug, not a step failure
                    results[name] = {"status": "failed", "steps": [], "error": repr(error)}
                    out.error(f"{name}: {error!r}")
                if keys[name] is not None:
                    results[name]["inputKey"] = keys[name]
                results[name]["begun"] = round(begun[name], 1)
                results[name]["ended"] = round(time.monotonic() - clock, 1)
                # Only inputs that held still for the whole job may vouch for it later.
                if keys[name] == input_key(config, name, values):
                    record_pass(config, name, keys[name], values["run_id"], results[name])
    if config.stop is not None and config.stop.is_set():
        raise Stopped("the run was stopped")


def new_run(runs: Path, run_id: str | None = None) -> tuple[str, Path]:
    """A run id and its folder, made here. Making the folder is what claims the id, so two runs
    in one records folder never share one and need no lock. A caller's id (a Workbench job id)
    must be new; otherwise the id is the time, numbered when two runs start in one second."""
    runs.mkdir(parents=True, exist_ok=True)
    if run_id is not None:
        if not run_id or run_id in (".", "..", "cache") or re.search(r"[\\/:]", run_id):
            raise ToolError(USAGE, f"--run-id {run_id!r} is not a folder name")
        try:
            (runs / run_id).mkdir()
        except FileExistsError:
            raise ToolError(USAGE, f"run {run_id} already exists in {runs}") from None
        return run_id, runs / run_id
    stem = time.strftime("%Y%m%d-%H%M%S")
    for number in range(1, 1000):
        candidate = stem if number == 1 else f"{stem}-{number}"
        try:
            (runs / candidate).mkdir()
        except FileExistsError:
            continue
        return candidate, runs / candidate
    raise ToolError(USAGE, f"no free run id for {stem} in {runs}")


# --- the push gate ---------------------------------------------------------------------------


def refusal(
    config: Config,
    repository: str,
    commit: str,
    passed: dict[tuple[str, ...], set[str]],
    required: list[str],
) -> str | None:
    """Why commit may not be pushed from repository, or None when every required job passed on
    its tree in one run combination. A project commit also fixes the tree of each repository
    it pins (``[repositories.<name>] pin``); other trees may be any that were tested with it."""
    names = config.names
    by_name = {r.name: r for r in config.tested}
    if repository not in by_name:
        raise ToolError(USAGE, f"unknown repository {repository}; known: {', '.join(names)}")
    tree = git(by_name[repository].path, "rev-parse", f"{commit}^{{tree}}")
    fixed = {names.index(repository): tree}
    label = f"{repository} tree {tree[:8]}"
    if repository == names[0]:
        for other in config.tested[1:]:
            if not other.pin_file:
                continue
            try:
                text = git(config.root, "show", f"{commit}:{other.pin_file}")
                pin = str(pin_value(json.loads(text), other.pin_key))
            except (subprocess.CalledProcessError, ValueError, KeyError, TypeError):
                return f"its {other.pin_file} names no {other.name} commit"
            try:
                pinned = git(other.path, "rev-parse", "--verify", f"{pin}^{{tree}}")
            except (OSError, subprocess.CalledProcessError):
                return f"its {other.name} pin {pin[:8]} is not in the {other.name} checkout"
            fixed[names.index(other.name)] = pinned
            label += f" with {other.name} {pin[:8]}"
            if other.passed_by:
                upstream = upstream_refusal(config, other, pin)
                if upstream:
                    return upstream
    candidates = [
        jobs for key, jobs in passed.items() if all(key[i] == t for i, t in fixed.items())
    ]
    done = max(candidates, key=lambda jobs: len(jobs & set(required)), default=set())
    missing = [job for job in required if job not in done]
    return f"{label} has not passed {', '.join(missing)}" if missing else None


def file_at(repository: Path, commit: str) -> tuple[str, str] | None:
    """The checks file a commit carries, as (file name, text): its project.toml, else its actions.toml."""
    for file_name in config_names():
        try:
            return file_name, git(repository, "show", f"{commit}:{file_name}")
        except (OSError, subprocess.CalledProcessError):
            continue
    return None


def upstream_refusal(config: Config, repository: Repository, commit: str) -> str | None:
    """Why ``commit`` of ``repository`` has not passed its own project's required checks (read from that commit's
    own jobs file, against that project's records beside this one's), or None when it has."""
    records = config.runs.parent / str(repository.passed_by)
    found = file_at(repository.path, commit)
    if found is None:
        return f"its {repository.name} pin {commit[:8]} has no {PROJECT_FILE} or {DEFAULT_CONFIG}, so nothing shows it passed"
    file_name, text = found
    with tempfile.TemporaryDirectory(prefix="upstream-") as scratch:
        copy = Path(scratch) / file_name
        copy.write_text(text + "\n", encoding="utf-8")
        upstream = load(repository.path, copy)
        upstream.runs = records
        passed = passed_jobs(records, upstream.names) if records.is_dir() else {}
        reason = refusal(upstream, upstream.names[0], commit, passed, upstream.required())
    if reason:
        return (f"its {repository.name} pin {commit[:8]} has not passed its own checks ({reason}); run them "
                f"in {repository.name} at that commit")
    return None


def gate_check(config: Config, repository: str, lines: Iterable[str]) -> list[dict[str, str]]:
    """Refusals for git's pre-push lines: <local ref> <local sha> <remote ref> <remote sha>."""
    required = config.required()
    passed = passed_jobs(config.runs, config.names)
    refused = []
    for line in lines:
        fields = line.split()
        if len(fields) != 4 or set(fields[1]) == {"0"}:  # a deleted ref has nothing to check
            continue
        try:
            reason = refusal(config, repository, fields[1], passed, required)
        except (OSError, subprocess.CalledProcessError):
            reason = "git cannot read that commit"
        if reason:
            refused.append({"ref": fields[0], "commit": fields[1], "reason": reason})
    return refused


def gate_install(config: Config, only: list[str], force: bool, out: Output) -> int:
    python = Path(sys.executable).as_posix()
    failed = 0
    installed = []
    for repository in config.tested:
        if only and repository.name not in only:
            continue
        try:
            hooks = git(repository.path, "rev-parse", "--git-path", "hooks")
        except (OSError, subprocess.CalledProcessError):
            out.error(f"{repository.name}: no Git checkout at {repository.path}")
            failed += 1
            continue
        hook = repository.path / hooks / "pre-push"
        if hook.exists() and not force:
            if HOOK_MARKER not in hook.read_text(encoding="utf-8", errors="replace"):
                out.error(f"{hook} is another hook; --force replaces it")
                failed += 1
                continue
        hook.parent.mkdir(parents=True, exist_ok=True)
        text = HOOK.format(
            python=python,
            root=config.root.as_posix(),
            repository=repository.name,
            workflow=config.workflow,
        )
        hook.write_text(text, encoding="utf-8", newline="\n")
        hook.chmod(0o755)
        installed.append(str(hook))
        out.line(f"installed {hook}")
    out.result = {"installed": installed}
    return FINDING if failed else OK


# --- the timing report -----------------------------------------------------------------------


def _table(headers: list[str], rows: list[list[Any]]) -> list[str]:
    def cell(value: Any) -> str:
        if value is None or value == "":
            return ""
        if isinstance(value, float):
            return f"{value:.1f}"
        return str(value).replace("|", "\\|")

    align = ["---" if i == 0 else "---:" for i in range(len(headers))]
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(align) + " |"]
    lines += ["| " + " | ".join(cell(v) for v in row) + " |" for row in rows]
    return lines


def report(summary: dict[str, Any]) -> str:
    """Markdown: every job and step with its time, then the timing records steps printed."""
    lines = [f"# Timing for run {summary.get('runId')}", ""]
    lines.append(
        f"Targets: {' '.join(summary.get('targets', []))}; "
        f"{round(summary.get('seconds') or 0, 1)} s."
    )
    lines += ["", "## Jobs", ""]
    rows: list[list[Any]] = []
    for job, result in summary.get("jobs", {}).items():
        steps = result.get("steps", [])
        status = result.get("status")
        if result.get("cached"):
            status = f"{status} (cached from {result['cached']})"
        seconds = float(sum(s.get("seconds", 0) for s in steps))
        rows.append([job, status, result.get("begun"), result.get("ended"), seconds])
    lines += _table(["Job", "Status", "Begun", "Ended", "Seconds"], rows)
    rows = [
        [f"{job} / {step['name']}", step.get("exitCode"), step.get("seconds")]
        for job, result in summary.get("jobs", {}).items()
        for step in result.get("steps", [])
    ]
    if rows:
        lines += ["", "## Steps", ""] + _table(["Step", "Exit", "Seconds"], rows)
    records = [
        (job, step["name"], record)
        for job, result in summary.get("jobs", {}).items()
        for step in result.get("steps", [])
        for record in step.get("timing", [])
        if isinstance(record, dict)
    ]
    if records:
        rows = []
        for job, step, record in records:
            label = next(
                (str(record[k]) for k in ("name", "check", "command", "phase") if k in record),
                "",
            )
            numbers = ", ".join(
                f"{key} {value:.1f}" if isinstance(value, float) else f"{key} {value}"
                for key, value in record.items()
                if isinstance(value, int | float) and not isinstance(value, bool)
            )
            rows.append([f"{job} / {step}", label, numbers])
        lines += ["", "## Timing records", ""] + _table(["Step", "Record", "Values"], rows)
    return "\n".join(lines) + "\n"


# --- the command -----------------------------------------------------------------------------


def _common() -> cli.Parser:
    common = cli.Parser(add_help=False)
    common.add_argument(
        "--root", type=Path, metavar="DIR", help="project root (default: the Git work tree)"
    )
    common.add_argument(
        "--config", type=Path, metavar="FILE", help="checks file (default: project.toml, else actions.toml)"
    )
    common.add_argument(
        "--runs", type=Path, metavar="DIR", help="the run records folder (default: the file's own setting)"
    )
    return common


def build_parser() -> cli.Parser:
    parser = cli.parser(
        "orthrus-actions",
        (__doc__ or "").splitlines()[0],
        epilog="Configuration: docs/actions.md.",
    )
    commands = cli.subcommands(parser)
    common = [_common()]
    commands.add_parser("list", help="show workflows and jobs", parents=common)
    run = commands.add_parser("run", help="run workflows or jobs", parents=common)
    run.add_argument("targets", nargs="+", metavar="TARGET", help="workflows, jobs or quick")
    run.add_argument("--dry-run", action="store_true", help="print the plan only")
    run.add_argument("--keep-going", action="store_true", help="continue after a failure")
    run.add_argument("--verbose", "-v", action="store_true", help="stream step output")
    run.add_argument("--no-cache", action="store_true", help="run cacheable jobs anyway")
    run.add_argument(
        "--jobs",
        "-j",
        type=int,
        default=0,
        metavar="N",
        help="jobs at once (default: what the budget allows; 1 runs them one at a time)",
    )
    run.add_argument(
        "--if-needed", action="store_true", help="skip jobs already passed for these trees"
    )
    run.add_argument("--run-id", metavar="ID", help="the run's id (default: the time); must be new")
    run.add_argument(
        "--memory",
        type=float,
        metavar="GIB",
        help="memory the run's jobs may claim together (default: memory_fraction of the machine)",
    )
    status = commands.add_parser(
        "status", help="which jobs of a workflow passed for the current trees", parents=common
    )
    status.add_argument("workflow", nargs="?", help="default: [settings] workflow, else ci")
    report_parser = commands.add_parser(
        "report", help="write timing.md for a run and print it", parents=common
    )
    report_parser.add_argument("run_id", nargs="?", metavar="RUN", help="default: the latest")
    gate = commands.add_parser("gate", help="the pre-push gate")
    gate_commands = cli.subcommands(gate)
    gate_commands.dest = "gate_command"
    install = gate_commands.add_parser("install", help="write the pre-push hooks", parents=common)
    install.add_argument(
        "--repository", action="append", default=[], metavar="NAME", help="only this one"
    )
    install.add_argument("--force", action="store_true", help="replace another pre-push hook")
    check = gate_commands.add_parser(
        "check", help="the hook's check; git lists refs on stdin", parents=common
    )
    check.add_argument("repository", metavar="REPOSITORY", help="the repository pushing")
    check.add_argument("remote", nargs="*", help="git's remote name and URL (unused)")
    return parser


def _list(config: Config, out: Output) -> int:
    out.result = {
        "config": config.path.relative_to(config.root).as_posix()
        if config.path.is_relative_to(config.root)
        else str(config.path),
        "repositories": {r.name: str(r.path) for r in config.repositories},
        "workflow": config.workflow,
        "workflows": config.workflows,
        "jobs": {
            name: {
                "description": job.get("description", ""),
                "needs": job.get("needs", []),
                "cacheable": bool(job.get("inputs")),
                "exclusive": bool(job.get("exclusive") or job.get("native")),
            }
            for name, job in config.jobs.items()
        },
    }
    out.line("Workflows:")
    for name, jobs in config.workflows.items():
        out.line(f"  {name:<16} {', '.join(jobs)}")
    out.line(f"  {'quick':<16} the {config.workflow} jobs the uncommitted changes can affect")
    out.line("Jobs:")
    for name, job in config.jobs.items():
        flags = " [exclusive]" if job.get("exclusive") or job.get("native") else ""
        flags += " [cached]" if job.get("inputs") else ""
        out.line(f"  {name:<16} {job.get('description', '')}{flags}")
    return OK


def _run(config: Config, args: argparse.Namespace, out: Output) -> int:
    targets: list[str] = list(args.targets)
    tested: dict[str, str | None] | None = None
    plan: dict[str, Any] = {"targets": targets}
    out.result = plan
    if "quick" in targets:
        tested = trees(config)
        jobs, changed = quick_jobs(config, tested)
        counts = ", ".join(f"{count} in {name}" for name, count in changed.items())
        out.line(f"quick: changed paths {counts or 'none'}; to run: {', '.join(jobs) or 'none'}")
        plan["quick"] = {"changed": changed, "jobs": jobs}
        targets = [t for t in targets if t != "quick"] + jobs
        if not targets:
            plan["order"] = []
            return OK
    order = resolve(config, targets)
    if args.if_needed:
        tested = tested or trees(config)
        key = tuple(tested.get(name) or "" for name in config.names)
        done = passed_jobs(config.runs, config.names).get(key, set())
        already = [name for name in order if name in done]
        if already:
            out.line("already passed for these trees: " + ", ".join(already))
        plan["alreadyPassed"] = already
        remaining = [name for name in order if name not in done]
        if not remaining:
            plan["order"] = []
            return OK
        # Keep what a remaining job needs: a run's artifacts belong to that run.
        order = resolve(config, remaining)
    plan["order"] = order
    if args.dry_run:
        values = placeholders(config, "<run-id>")
        commands: dict[str, list[list[str]]] = {}
        for name in order:
            out.line(f"{name}:")
            commands[name] = []
            for step in config.jobs[name]["steps"]:
                command = expand(step["run"], values, f"job {name}")
                commands[name].append(command)
                out.line("  $ " + " ".join(command))
        plan["commands"] = commands
        return OK
    results: dict[str, dict[str, Any]] = {}
    started = time.monotonic()
    run_id, run_dir = new_run(config.runs, args.run_id)
    values = placeholders(config, run_id)
    tested = tested or trees(config)
    out.line(f"run {run_id}: {', '.join(order)}  (logs in {run_dir})")
    try:
        run_jobs(
            config,
            order,
            values,
            run_dir,
            results,
            out,
            keep_going=args.keep_going,
            verbose=args.verbose,
            concurrency=args.jobs,
            use_cache=not args.no_cache,
            memory=args.memory,
        )
    finally:
        for name in order:
            results.setdefault(name, {"status": "skipped", "reason": "interrupted"})
        summary = {
            "schema": SUMMARY_SCHEMA,
            "runId": run_id,
            "targets": list(args.targets),
            "order": order,
            "trees": tested,
            "treesChanged": trees(config) != tested,
            "seconds": round(time.monotonic() - started, 1),
            "jobs": results,
        }
        (run_dir / "summary.json").write_text(json.dumps(summary, indent=2), "utf-8")
    out.result = summary
    failed = [n for n, r in results.items() if r["status"] == "failed"]
    skipped = [n for n, r in results.items() if r["status"] == "skipped"]
    verdict = "FAILED" if failed else "PASSED"
    out.line(
        f"{verdict} in {summary['seconds']} s; failed: {', '.join(failed) or 'none'}; "
        f"skipped: {', '.join(skipped) or 'none'}"
    )
    if summary["treesChanged"]:
        out.warn("files changed during the run, so it vouches for no tree")
    return FINDING if failed else OK


def _status(config: Config, workflow: str | None, out: Output) -> int:
    name = workflow or config.workflow
    if name not in config.workflows:
        raise ToolError(USAGE, f"unknown workflow: {name}")
    required = config.workflows[name]
    tested = trees(config)
    key = tuple(tested.get(n) or "" for n in config.names)
    done = passed_jobs(config.runs, config.names).get(key, set())
    missing = [job for job in required if job not in done]
    out.result = {
        "workflow": name,
        "trees": tested,
        "passed": [job for job in required if job in done],
        "missing": missing,
    }
    for job in required:
        out.line(f"{'passed ' if job in done else 'missing'} {job}")
    if missing:
        out.note(f"run: orthrus-tools actions run {name} --if-needed")
    return FINDING if missing else OK


def _report(config: Config, run_id: str | None, out: Output) -> int:
    if run_id:
        path = config.runs / run_id / "summary.json"
    else:
        found = sorted(config.runs.glob("*/summary.json"), key=lambda p: p.stat().st_mtime)
        if not found:
            raise ToolError(USAGE, f"no runs under {config.runs}")
        path = found[-1]
    try:
        summary = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ToolError(USAGE, f"cannot read {path}: {error}") from None
    text = report(summary)
    target = path.parent / "timing.md"
    target.write_text(text, encoding="utf-8", newline="\n")
    out.result = {"runId": summary.get("runId"), "report": str(target), "markdown": text}
    out.line(text.rstrip("\n"))
    out.note(f"wrote {target}")
    return OK


def run(args: argparse.Namespace, out: Output) -> int:
    root = (args.root or project_root()).resolve()
    if not root.is_dir():
        raise ToolError(USAGE, f"not a directory: {root}")
    config = load(root, args.config)
    if getattr(args, "runs", None):
        config.runs = args.runs
    if args.command == "list":
        return _list(config, out)
    if args.command == "run":
        return _run(config, args, out)
    if args.command == "status":
        return _status(config, args.workflow, out)
    if args.command == "report":
        return _report(config, args.run_id, out)
    out.command = f"gate {args.gate_command}"
    if args.gate_command == "install":
        return gate_install(config, args.repository, args.force, out)
    refused = gate_check(config, args.repository, sys.stdin)
    out.result = {"refused": refused}
    for entry in refused:
        out.error(f"refusing {entry['ref']} ({entry['commit'][:8]}): {entry['reason']}.")
    if refused:
        out.error(
            "Check out the pushed commits with nothing uncommitted, run "
            f"`orthrus-tools actions run {config.workflow} --if-needed`, and push again."
        )
        return FINDING
    return OK


def execute(
    root: Path,
    targets: list[str],
    *,
    config: Path | None = None,
    runs: Path | None = None,
    run_id: str | None = None,
    environ: dict[str, str] | None = None,
    python: str | None = None,
    identity: dict[str, Any] | None = None,
    stop: threading.Event | None = None,
    if_needed: bool = False,
    keep_going: bool = False,
    dry_run: bool = False,
    no_cache: bool = False,
    memory: float | None = None,
    jobs: int = 0,
) -> dict[str, Any]:
    """``actions run --json`` as a library call: the same document (schema, status, result, messages), with nothing
    read from this process's environment or interpreter.

    ``environ`` is the whole environment the steps start from and git runs in (None: this process's); ``python`` is
    what ``{python}`` names (None: this interpreter); ``identity`` carries the set's identity (``set``), the
    platform's JDK digest (``jdk``) and where another project's files were laid out (``tools``: {project: folder}),
    which the CLI reads from WB_SET_IDENTITY, WB_JDK_IDENTITY and WB_TOOLS_ROOTS. When ``stop`` is set the running
    steps are killed and ``Stopped`` is raised after the run's summary is written; KeyboardInterrupt is not caught.
    Steps still run as processes of their own; only the engine runs in the caller."""
    out = Output("actions", True, io.StringIO(), io.StringIO())
    out.command = "run"
    try:
        loaded = load(root.resolve(), config, environ=environ)
        loaded.python, loaded.identity, loaded.stop = python, identity, stop
        if runs is not None:
            loaded.runs = runs
        arguments = argparse.Namespace(
            targets=list(targets), if_needed=if_needed, keep_going=keep_going, dry_run=dry_run,
            no_cache=no_cache, memory=memory, jobs=jobs, run_id=run_id, verbose=False,
        )
        code = _run(loaded, arguments, out)
    except ToolError as error:
        if error.result is not None:
            out.result = error.result
        out.error(error.message)
        code = error.code
    return out.document(code)


def main(argv: list[str] | None = None) -> int:
    return cli.run("actions", build_parser, run, argv)


if __name__ == "__main__":
    sys.exit(main())
