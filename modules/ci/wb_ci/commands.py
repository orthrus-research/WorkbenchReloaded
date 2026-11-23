"""ci: a project's jobs, run by Workbench from clean trees, and the push gate over Workbench's own records.

A project is named in config, ``[modules.ci.projects.<name>]``::

    repo = '<the checkout>'               # under a configured root (only ever read); relative: projects_root
    config = "tools/actions.toml"         # its jobs, in the actions format (default: what orthrus.toml names,
                                          # else actions.toml)
    jdk = "26"                            # JAVA_HOME for its jobs (default: [modules.ci] jdk)
    env = { KEY = "value" }               # more environment for every job
    python = ["tools/requirements.txt"]   # its jobs' {python}: an environment from these requirements files

``ci.run`` clones the project, and each repository its jobs file names at the commit the project pins, into a work
folder, so no checkout is written. It runs the jobs there with the image's JDKs, a persistent Gradle home and the
project's Python environment as ``{python}``, and keeps the run records in Workbench's state. The Gradle home's
``init.d`` serves every build toolchain's rescue repository and substitutions, in the script ``toolchain.repo-init``
makes afresh for each run. ``ci.gate`` reads the records the way the actions push gate reads its own, and ``ci.hook``
installs a pre-push hook that asks it.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import wb_tools.adapter  # noqa: F401  (puts the absorbed orthrus engine on sys.path)
from orthrus_tools import actions as engine
from wb_build.files import write_atomic
from wb_core.module import Context, EnvironmentProblem, ParamError

TOOLS_SRC = Path(wb_tools.adapter.__file__).resolve().parents[1] / "src"
# Mounted checkouts belong to another user; reading them is all ci ever does.
GIT_SAFE = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "safe.directory", "GIT_CONFIG_VALUE_0": "*"}
PROJECT_KEYS = {"repo", "config", "jdk", "env", "python", "workflow", "gate"}  # gate = false: pushes are only scrubbed
REPOSITORY_SCRIPT = "workbench-repository.gradle"  # in the Gradle home's init.d: toolchain's rescue repository
COLD_SCRIPT = "workbench-cold.gradle"  # in the Gradle home's init.d: a cold run's builds use no build cache
COLD_INIT = b"""// Written by Workbench's ci module. A cold run (WB_COLD set) builds every task: no build cache is read or written.
if (System.getenv("WB_COLD")) {
    gradle.settingsEvaluated { settings ->
        settings.buildCache.local.enabled = false
        if (settings.buildCache.remote != null) settings.buildCache.remote.enabled = false
    }
}
"""


# ---------------------------------------------------------------------------------------------
# configuration and paths

def _state() -> Path:
    return Path(os.environ.get("WB_STATE_DIR") or "/var/lib/wb")


def _folder(ctx: Context, key: str, default: Path) -> Path:
    value = ctx.config.get(key)
    return Path(value) if value else default


def _records(ctx: Context, project: str) -> Path:
    return _folder(ctx, "records", _state() / "ci" / "records") / project


def _container(ctx: Context, value: str) -> Path:
    """A host path from config as this machine sees it (the container's mount, or the path itself natively)."""
    from wb_core.paths import PathError, PathMap
    from wb_core.settings import Root
    roots = [Root(**r) for r in ctx.spec.get("roots", [])]
    if not roots or ctx.machine == "host":
        return Path(value)
    try:
        return Path(PathMap(roots).resolve(value).container)
    except PathError as error:
        raise EnvironmentProblem(f"{value} is under no configured root ({error})") from None


def _project(ctx: Context, name: str) -> dict:
    projects = ctx.config.get("projects") or {}
    if name not in projects:
        raise ParamError(f"no project {name} in [modules.ci.projects] (have: {', '.join(sorted(projects)) or 'none'})")
    entry = dict(projects[name])
    unknown = sorted(set(entry) - PROJECT_KEYS)
    if unknown:
        raise EnvironmentProblem(f"modules.ci.projects.{name}: unknown key(s) {', '.join(unknown)} "
                                 f"(known: {', '.join(sorted(PROJECT_KEYS))})")
    if not entry.get("repo"):
        raise EnvironmentProblem(f"modules.ci.projects.{name}.repo is not set")
    entry["repo"] = _host_repo(ctx, name, str(entry["repo"]))
    entry["source"] = _container(ctx, entry["repo"])
    return entry


def _host_repo(ctx: Context, name: str, repo: str) -> str:
    """A project's checkout as the host names it. A relative ``repo`` (as a shared config writes it) is under the
    machine's ``[modules.ci] projects_root``."""
    import ntpath
    import posixpath
    windows = len(repo) > 2 and repo[1] == ":" and repo[2] in "\\/"
    if windows or repo.startswith(("/", "\\\\")):
        return repo
    root = ctx.config.get("projects_root")
    if not root:
        raise EnvironmentProblem(f"modules.ci.projects.{name}.repo = {repo!r} is relative, and [modules.ci] "
                                 "projects_root is not set")
    root = str(root)
    return ntpath.join(root, repo) if len(root) > 1 and root[1] == ":" else posixpath.join(root, repo)


def _git(ctx: Context, args: list[str], cwd: Path | None = None, check: bool = True) -> str:
    done = ctx.run(["git", *args], cwd=cwd, env=GIT_SAFE, log_output=False)
    if check and done.returncode != 0:
        raise EnvironmentProblem(f"git {' '.join(args[:4])} failed: {done.stdout.strip()[-400:]}")
    return done.stdout.strip()


def _git_dir(ctx: Context, checkout: Path) -> Path:
    """The git directory of a checkout. A linked worktree's ``.git`` file names its directory as the host wrote
    it (on Windows, a drive path the container cannot open), so that path is translated through the roots."""
    dot = checkout / ".git"
    if dot.is_dir():
        return dot
    if dot.is_file():
        line = dot.read_text(encoding="utf-8", errors="replace").strip()
        if line.startswith("gitdir:"):
            named = line.split(":", 1)[1].strip()
            path = Path(named)
            if not path.is_absolute() and not (len(named) > 1 and named[1] == ":"):
                return (checkout / path).resolve()
            return _container(ctx, named)
    raise ParamError(f"{checkout} is not a git checkout")


def _common_dir(git_dir: Path) -> Path:
    common = git_dir / "commondir"
    if common.is_file():
        return (git_dir / common.read_text(encoding="utf-8").strip()).resolve()
    return git_dir


def _resolve(ctx: Context, checkout: Path, commit: str) -> str:
    git_dir = _git_dir(ctx, checkout)
    return _git(ctx, ["--git-dir", str(git_dir), "rev-parse", "--verify", f"{commit}^{{commit}}"]).splitlines()[-1]


def _clone(ctx: Context, checkout: Path, target: Path, sha: str):
    """A clone that borrows the checkout's objects (writing nothing into them), at ``sha``."""
    common = _common_dir(_git_dir(ctx, checkout))
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    _git(ctx, ["clone", "--quiet", "--shared", "--no-checkout", str(common), str(target)])
    _git(ctx, ["-C", str(target), "checkout", "--quiet", "--detach", sha])


def _overlay_worktree(ctx: Context, checkout: Path, target: Path, excludes: list[str], settings: dict):
    """Makes the clone's working tree the tree the checkout's ``git add --all`` would commit, leaving it uncommitted
    over the clone's HEAD as it is in the checkout.

    The checkout's own git writes that tree, so its config, attributes and line-ending rules apply: a text file that
    autocrlf left with CRLF endings on disk is the LF blob the host would commit, and each file keeps the mode the
    host records, not the one a mounted folder reports. Copying the files instead would carry both into the clone.
    ``excludes`` and ``settings`` stand in for the host's excludes file and its system and global git config, which a
    container cannot read; the repository's own config still overrides them. Objects go only into the clone."""
    git_dir = _git_dir(ctx, checkout)
    private = target / ".git"
    exclude_file = private / "info" / "exclude"
    exclude_file.parent.mkdir(parents=True, exist_ok=True)
    with open(exclude_file, "a", encoding="utf-8") as handle:
        handle.write("\n".join(excludes) + "\n")
    env = {**GIT_SAFE, "GIT_DIR": str(git_dir), "GIT_WORK_TREE": str(checkout),
           "GIT_INDEX_FILE": str(private / "host-index"), "GIT_OBJECT_DIRECTORY": str(private / "objects")}
    if settings:  # the host's own layers, in place of the container's
        host_config = private / "host-config"
        host_config.write_text("", encoding="utf-8")
        for key, value in settings.items():
            _git(ctx, ["config", "--file", str(host_config), str(key), str(value).lower()
                       if isinstance(value, bool) else str(value)])
        env |= {"GIT_CONFIG_GLOBAL": str(host_config), "GIT_CONFIG_NOSYSTEM": "1"}

    def host_git(*args: str) -> str:
        done = ctx.run(["git", "-c", f"core.excludesFile={exclude_file.as_posix()}", "-c", "core.fsmonitor=false",
                        *args], env=env, log_output=False)
        if done.returncode != 0:
            raise EnvironmentProblem(f"git {args[0]} failed in {checkout}: {done.stdout.strip()[-400:]}")
        return done.stdout.strip()

    host_git("read-tree", "HEAD")
    host_git("add", "--all")
    tree = host_git("write-tree").splitlines()[-1].strip()
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", tree):
        raise EnvironmentProblem(f"git write-tree in {checkout} gave no tree: {tree[-200:]}")
    _git(ctx, ["-C", str(target), "read-tree", "--reset", "-u", tree])
    _git(ctx, ["-C", str(target), "reset", "--quiet"])


# ---------------------------------------------------------------------------------------------
# the job environment

def _jdk_env(ctx: Context, major: str) -> dict:
    jdks = dict(getattr(ctx, "_jdks", {}) or {})
    env = {}
    for key, home in jdks.items():
        if key.isdigit():
            env[f"JAVA_HOME_{key}_X64"] = home
    if major:
        if major not in jdks:
            raise EnvironmentProblem(f"JDK {major} is not available (have: {', '.join(sorted(jdks)) or 'none'})")
        env["JAVA_HOME"] = jdks[major]
        env["PATH"] = os.pathsep.join([str(Path(jdks[major]) / "bin"), os.environ.get("PATH", "")])
    return env


def _gradle_home(ctx: Context) -> tuple[Path, dict]:
    """A Gradle home that survives the container, told where the image's JDKs are and never to fetch others, and
    serving toolchain's rescue repository; with what it serves this run."""
    home = _folder(ctx, "gradle_home", _state() / "cache" / "gradle")
    home.mkdir(parents=True, exist_ok=True)
    jdks = sorted({v for k, v in (getattr(ctx, "_jdks", {}) or {}).items() if k[:1].isdigit()})
    lines = ["# written by Workbench's ci module", f"org.gradle.java.installations.paths={','.join(jdks)}",
             "org.gradle.java.installations.auto-detect=false", "org.gradle.java.installations.auto-download=false"]
    (home / "gradle.properties").write_text("\n".join(lines) + "\n", encoding="utf-8")
    cold = home / "init.d" / COLD_SCRIPT
    if not cold.is_file() or cold.read_bytes() != COLD_INIT:
        write_atomic(cold, COLD_INIT)
    return home, _serve_repository(ctx, home)


def _serve_repository(ctx: Context, home: Path) -> dict:
    """Gives every build of the run toolchain's rescue repository and substitutions: the init script
    ``toolchain.repo-init`` makes for builds in this container, from toolchain's config and the repository's receipts,
    asked afresh each run (inline, in this job's reservation, so it never queues) and kept in the Gradle home's
    ``init.d``, rewritten only when it changed. With nothing to serve, the home keeps none."""
    called = ctx.call_inline("toolchain.repo-init", machine="container")
    if called.get("outcome") != "ok":
        raise EnvironmentProblem(f"toolchain.repo-init ended {called.get('outcome')}: {called.get('summary')}")
    data = called.get("data") or {}
    target = home / "init.d" / REPOSITORY_SCRIPT
    if data.get("script"):
        script = data["script"].encode("utf-8")
        if not target.is_file() or target.read_bytes() != script:
            write_atomic(target, script)
    else:
        target.unlink(missing_ok=True)
    return {"script": data.get("sha256"), "versions": len(data.get("held") or []),
            "substitutions": len(data.get("substitutions") or {})}


def _python(ctx: Context, root: Path, requirements: list[str]) -> str:
    """The interpreter a project's jobs run under, which the jobs file names as ``{python}``: an environment built
    (once per set of requirements, by their SHA-256) from the requirements files the project's CI config lists, or
    Workbench's own interpreter when it lists none. Where an environment lives is Workbench's business, never the
    jobs file's, and nothing is written into the tree."""
    from wb_core.environments import python_from
    return python_from(ctx, [root / r for r in requirements], _folder(ctx, "python_cache", _state() / "cache" / "python"),
                       "the project's python requirements")


# ---------------------------------------------------------------------------------------------
# the actions engine

def _load(root: Path, entry: dict, records: Path) -> engine.Config:
    config = engine.load(root, Path(entry["config"]) if entry.get("config") else None)
    config.runs = records
    return config


def _jobs_name(entry: dict) -> str:
    return str(entry.get("config") or engine.DEFAULT_CONFIG).replace("\\", "/")


def _config_at(ctx: Context, entry: dict, records: Path, sha: str) -> engine.Config | None:
    """The engine's config from the jobs file in commit ``sha``; None when the commit has none. Status and the gate
    judge a commit by its own jobs file: the checkout's working tree may be on another branch, or older."""
    source = entry["source"]
    # Not ctx.run: it merges git's stderr into the output, which here is the file itself.
    shown = subprocess.run(["git", "--git-dir", str(_git_dir(ctx, source)), "show", f"{sha}:{_jobs_name(entry)}"],
                           env={**os.environ, **GIT_SAFE}, capture_output=True)
    if shown.returncode != 0:
        return None
    copy = ctx.work / f"jobs-{sha[:12]}.toml"
    copy.parent.mkdir(parents=True, exist_ok=True)
    copy.write_bytes(shown.stdout)
    try:
        config = engine.load(source, copy)
    except engine.ToolError as error:
        raise EnvironmentProblem(f"{_jobs_name(entry)} at {sha[:12]}: {error}") from None
    config.runs = records
    return config


def _engine(ctx: Context, root: Path, jobs_file: Path, argv: list[str], env: dict, python: str) -> dict:
    """The actions runner, under the project's interpreter: its ``{python}`` is the interpreter running it."""
    args = [python, "-m", "orthrus_tools", "actions", *argv, "--root", str(root), "--config", str(jobs_file),
            "--json"]
    env = {**GIT_SAFE, **env,
           "PYTHONPATH": os.pathsep.join(p for p in [str(TOOLS_SRC), os.environ.get("PYTHONPATH", "")] if p)}
    done = ctx.run(args, cwd=root, env=env, timeout=None, log_output=False)
    text = done.stdout
    start = text.find("\n{")
    start = 0 if text.startswith("{") else (start + 1 if start >= 0 else -1)
    if start < 0:
        raise EnvironmentProblem(f"the actions engine said nothing parseable (exit {done.returncode}): "
                                 f"{text.strip()[-600:]}")
    try:
        return json.loads(text[start:])
    except ValueError:
        raise EnvironmentProblem(f"the actions engine's JSON did not parse: {text.strip()[-600:]}") from None


def _jobs_copy(root: Path, entry: dict, records: Path, work: Path) -> Path:
    """The project's jobs file with its run records pointed at Workbench's. The copy sits outside the tree: an edit
    inside it would change the very tree the run vouches for."""
    import re
    import tomllib
    path = engine.config_path(root, Path(entry["config"]) if entry.get("config") else None)
    if not path.is_file():
        raise EnvironmentProblem(f"no jobs file at {path} (set modules.ci.projects.<name>.config)")
    text = path.read_text(encoding="utf-8")
    settings = tomllib.loads(text).get("settings") or {}
    line = f'runs = "{records.as_posix()}"'
    if "runs" in settings:
        text = re.sub(r"(?m)^\s*runs\s*=.*$", line, text, count=1)
    elif re.search(r"(?m)^\[settings\]\s*$", text):
        text = re.sub(r"(?m)^\[settings\]\s*$", "[settings]\n" + line, text, count=1)
    else:
        text = "[settings]\n" + line + "\n\n" + text
    copy = work / "jobs.toml"
    copy.write_text(text, encoding="utf-8")
    return copy


def _failing_tails(ctx: Context, jobs: dict):
    for name, job in jobs.items():
        if job.get("status") != "failed":
            continue
        if job.get("error") and all(step.get("exitCode") in (0, None) for step in job.get("steps") or []):
            # Every step passed and the job still failed (an artifact no file matches, ...): say why.
            ctx.reason("job", name, str(job["error"]), "fix the job in the jobs file, then run its checks again")
            continue
        for step in job.get("steps") or []:
            if step.get("exitCode") not in (0, None) and step.get("log"):
                try:
                    lines = Path(step["log"]).read_text(encoding="utf-8", errors="replace").splitlines()[-25:]
                except OSError:
                    continue
                ctx.log.warning(f"{name} / {step.get('name')} exited {step.get('exitCode')}:\n" + "\n".join(lines))
                ctx.reason("step", f"{name} / {step.get('name')}",
                           f"exited {step.get('exitCode')}: " + "\n".join(lines[-12:]),
                           f"read the step log: {ctx.host_path(step['log'])}")


# ---------------------------------------------------------------------------------------------
# commands

def run(ctx: Context, project: str, commit: str, workflow: str, jobs: list, if_needed: bool, keep_going: bool,
        keep: bool, dry_run: bool, checkout=None, cold: bool = False):
    entry = _project(ctx, project)
    source = entry["source"]
    tree_source = Path(checkout) if checkout else source  # whose working tree commit=worktree runs
    if checkout:
        if commit != "worktree":
            raise ParamError("checkout names a working tree to run; give it with commit=worktree")
        same = _common_dir(_git_dir(ctx, tree_source)).resolve() == _common_dir(_git_dir(ctx, source)).resolve()
        if not same:
            raise ParamError(f"{checkout} is not a checkout of {project}'s repository")
    records = _records(ctx, project)
    records.mkdir(parents=True, exist_ok=True)
    work = _folder(ctx, "workdir", _state() / "ci" / "trees") / f"{project}-{ctx.job}"
    root = work / source.name
    worktree = commit == "worktree"
    sha = _resolve(ctx, tree_source, "HEAD" if worktree else commit)
    ctx.progress("clone", msg=f"{project} at {sha[:12]}" + (" with its working tree" if worktree else ""))
    try:
        _clone(ctx, source, root, sha)
        if worktree:
            _overlay_worktree(ctx, tree_source, root, list(ctx.config.get("excludes") or []),
                              dict(ctx.config.get("git_config") or {}))
        if not (root / _jobs_name(entry)).is_file():
            raise ParamError(f"{project} {sha[:12]} has no {_jobs_name(entry)}, so there are no jobs to run"
                             + ("" if commit != "HEAD" else "; that is the checkout's HEAD, so name another with "
                                "commit=<sha or branch>"))
        config = _load(root, entry, records)
        pinned = {}
        for repository in config.repositories[1:]:
            relative = os.path.relpath(repository.path, root)
            other = (source / relative).resolve()
            at = "HEAD"
            if repository.pin_file:
                try:
                    lock = json.loads((root / repository.pin_file).read_text(encoding="utf-8"))
                    pin = engine.pin_value(lock, repository.pin_key)
                except (OSError, ValueError, KeyError, TypeError):
                    raise EnvironmentProblem(f"{repository.pin_file} names no {repository.name} commit") from None
                at = str(pin)
            other_sha = _resolve(ctx, other, at)
            ctx.progress("clone", msg=f"{repository.name} at {other_sha[:12]}")
            _clone(ctx, other, (root / relative).resolve(), other_sha)
            pinned[repository.name] = other_sha
        jobs_file = _jobs_copy(root, entry, records, work)
        major = str(entry["jdk"] if "jdk" in entry else ctx.config.get("jdk", "26"))  # "": no JAVA_HOME
        pins = _folder(ctx, "pin_cache", _state() / "cache" / "pins")
        pins.mkdir(parents=True, exist_ok=True)
        gradle_home, served = _gradle_home(ctx)
        env = {**_jdk_env(ctx, major), "GRADLE_USER_HOME": str(gradle_home), "WB_PIN_CACHE": str(pins),
               **{k: str(v) for k, v in (entry.get("env") or {}).items()}, **({"WB_COLD": "1"} if cold else {})}
        python = _python(ctx, root, [str(r) for r in entry.get("python") or []])
        targets = [*([workflow] if workflow else []), *[str(j) for j in jobs]] or [config.workflow]
        # The engine runs inside this job's reservation: the job id names its run, and its budget is what the
        # scheduler holds for the job, not the machine.
        argv = ["run", *targets, "--run-id", ctx.job]
        if ctx.reservation.get("mem_gb"):
            argv += ["--memory", str(ctx.reservation["mem_gb"])]
        # Cold: every job runs, none vouched for by an earlier pass or its input cache, and no build cache is read.
        argv += ["--no-cache"] if cold else (["--if-needed"] if if_needed else [])
        argv += ["--keep-going"] if keep_going else []
        argv += ["--dry-run"] if dry_run else []
        ctx.progress("jobs", msg=" ".join([*targets, *argv[len(targets) + 3:]]))
        document = _engine(ctx, root, jobs_file, argv, env, python)
        _failing_tails(ctx, (document.get("result") or {}).get("jobs") or {})
    finally:
        if not keep:
            shutil.rmtree(work, ignore_errors=True)
    result = document.get("result") or {}
    status = document.get("status")
    jobs_done = result.get("jobs") or {}
    for name, job in jobs_done.items():
        seconds = sum(step.get("seconds") or 0 for step in job.get("steps") or [])
        if seconds:
            ctx.measure("job_seconds", round(seconds, 1), "s", job=name)
    run_id = result.get("runId")
    if run_id and (records / run_id).is_dir():
        ctx.output("run", records / run_id)
    stored = {} if result.get("treesChanged") else _store_artifacts(ctx, project, sha, result.get("trees") or {},
                                                                     records, run_id, jobs_done)
    failed = [n for n, j in jobs_done.items() if j.get("status") == "failed"]
    passed = [n for n, j in jobs_done.items() if j.get("status") == "passed"]
    trees = result.get("trees") or {}
    if status == "finding" or failed:
        ctx.finding(f"{project}: {', '.join(failed) or 'a job'} failed at {sha[:12]}")
    elif status == "usage":
        raise ParamError(document.get("summary") or "; ".join(m.get("text", "") for m in document.get("messages") or [])
                         or "the actions engine refused the arguments")
    elif status == "environment":
        raise EnvironmentProblem(document.get("summary") or "the actions engine could not run")
    else:
        ctx.summary(f"{project} at {sha[:12]}: {len(passed)} job(s) passed"
                    + (f", {len(jobs_done) - len(passed)} skipped" if len(jobs_done) > len(passed) else "")
                    + ("" if jobs_done else f" ({document.get('summary') or 'nothing to run'})"))
    return {"project": project, "commit": sha, "worktree": worktree, "pinned": pinned, "run": run_id,
            "trees": trees, "passed": passed, "failed": failed, "repository": served, "stored": stored,
            "engine": result}


def _store_artifacts(ctx: Context, project: str, sha: str, trees: dict, records: Path, run_id: str | None,
                     jobs_done: dict) -> dict:
    """Keeps what each passed producing job built (its ``artifacts``) in the Store, by SHA-256 and under the
    project's tree, and in the pin cache. The copy comes from the run record, which the engine wrote after the
    job passed and its trees held still; a run whose trees moved records no pass and keeps nothing."""
    from wb_core import artifacts as store
    tree = next(iter(trees.values()), None)  # the project's own tree comes first
    if not run_id or not tree:
        return {}
    root = _folder(ctx, "store", _state() / "store")
    pins = _folder(ctx, "pin_cache", _state() / "cache" / "pins")
    kept = {}
    for name, job in jobs_done.items():
        if job.get("status") != "passed" or job.get("cached"):
            continue
        for file_name, item in (job.get("artifacts") or {}).items():
            source = records / run_id / name / "artifacts" / file_name
            try:
                store.put(root, source, item["sha256"], pins)
            except (OSError, store.StoreError) as error:
                ctx.reason("store", file_name, f"not kept: {error}", None)
                continue
            kept[file_name] = {"sha256": item["sha256"], "bytes": item.get("bytes"), "job": name, "run": run_id}
    if kept:
        store.record(root, project, tree, sha, kept)
        ctx.log.info(f"kept {len(kept)} artifact(s) of {project} tree {tree[:12]} in the Store: "
                     + ", ".join(f"{n} {k['sha256'][:12]}" for n, k in kept.items()))
    return kept


# ---------------------------------------------------------------------------------------------
# the Store's own commands

def _store_root(ctx: Context) -> Path:
    return _folder(ctx, "store", _state() / "store")


def store_verify(ctx: Context, project: str, commit: str = "", again: bool = False):
    """Rebuilds what the Store keeps for a project cold, at the commit it was built from, and records whether the
    bytes came out the same. By default every tree not yet verified; ``commit`` picks one, ``again`` redoes them."""
    from wb_core import artifacts as store
    _project(ctx, project)
    root = _store_root(ctx)
    chosen = store.entries(root, project)
    if commit:
        chosen = [e for e in chosen if any(c.startswith(commit) for c in e.get("commits") or [])]
        if not chosen:
            raise ParamError(f"the Store keeps nothing of {project} built at {commit}")
    elif not again:
        chosen = [e for e in chosen if not e.get("verified")]
    verdicts, failed = [], []
    for entry in chosen:
        built_at = entry["commits"][0]
        jobs = sorted({item["job"] for item in entry["artifacts"].values()})
        ctx.progress("rebuild", msg=f"{project} tree {entry['tree'][:12]} at {built_at[:12]}: {', '.join(jobs)}")
        envelope = ctx.call("ci.run", project=project, commit=built_at, jobs=jobs, cold=True, if_needed=False)
        ran = ((envelope.get("data") or {}).get("engine") or {}).get("jobs") or {}
        differ = []
        for name, item in entry["artifacts"].items():
            rebuilt = ((ran.get(item["job"]) or {}).get("artifacts") or {}).get(name) or {}
            if rebuilt.get("sha256") != item["sha256"]:
                differ.append({"name": name, "kept": item["sha256"], "rebuilt": rebuilt.get("sha256")})
        complete = envelope.get("outcome") == "ok" and all(
            (ran.get(job) or {}).get("status") == "passed" for job in jobs)
        verdict = {"job": envelope.get("job"), "commit": built_at, "reproduced": complete and not differ,
                   "differ": differ, **({} if complete else {"rebuild": envelope.get("outcome") or "failed"})}
        store.note_verified(root, project, entry["tree"], verdict)
        verdicts.append({"tree": entry["tree"], **verdict})
        if not verdict["reproduced"]:
            failed.append(entry["tree"])
            what = (", ".join(f"{d['name']} {d['kept'][:12]} -> {(d['rebuilt'] or 'nothing')[:12]}" for d in differ)
                    or f"the rebuild ended {verdict.get('rebuild')}")
            ctx.reason("store", f"{project} {entry['tree'][:12]}", f"did not rebuild to its bytes: {what}",
                       f"wb job {envelope.get('job')}")
    if failed:
        ctx.finding(f"{project}: {len(failed)} of {len(chosen)} tree(s) did not rebuild to the Store's bytes")
    else:
        ctx.summary(f"{project}: {len(chosen)} tree(s) rebuilt cold to the Store's bytes"
                    if chosen else f"{project}: nothing in the Store waits for a rebuild")
    return {"project": project, "verified": verdicts}


def store_export(ctx: Context, to: str, project: str = "", commit: str = ""):
    """Copies what the Store keeps (one project's, or its tree at one commit) and every seeded file into a folder
    laid out as a Store, for a backup or another machine's ``ci.store-import``."""
    from wb_core import artifacts as store
    root = _store_root(ctx)
    trees = None
    if commit:
        if not project:
            raise ParamError("commit names a build of one project; give project too")
        trees = {e["tree"] for e in store.entries(root, project)
                 if any(c.startswith(commit) for c in e.get("commits") or [])}
        if not trees:
            raise ParamError(f"the Store keeps nothing of {project} built at {commit}")
    try:
        counts = store.export(root, Path(to), project or None, trees)
    except store.StoreError as error:
        raise EnvironmentProblem(str(error)) from None
    ctx.summary(f"exported {counts['entries']} build(s), {counts['files']} file(s) and {counts['seeded']} seeded "
                f"file(s) to {ctx.host_path(to)}")
    return counts


def store_import(ctx: Context, source: str):
    """Takes a folder ``ci.store-export`` wrote into the Store, every file re-hashed first."""
    from wb_core import artifacts as store
    try:
        counts = store.import_from(_store_root(ctx), Path(source),
                                   _folder(ctx, "pin_cache", _state() / "cache" / "pins"))
    except store.StoreError as error:
        raise EnvironmentProblem(str(error)) from None
    ctx.summary(f"imported {counts['files']} file(s) and {counts['seeded']} new seeded file(s)")
    return counts


def gate(ctx: Context, project: str, refs: list, refs_file=None):
    """The push gate: every required job must have passed on each pushed commit's tree (with the trees it pins),
    in Workbench's records. ``refs`` (or the lines of ``refs_file``) are git's pre-push lines:
    <local ref> <local sha> <remote ref> <remote sha>."""
    entry = _project(ctx, project)
    records = _records(ctx, project)
    lines = [str(r) for r in refs]
    if refs_file:
        lines += [line for line in Path(refs_file).read_text(encoding="utf-8").splitlines() if line.strip()]
    os.environ.update(GIT_SAFE)  # the engine's own git calls read the mounted checkouts too
    refused: list[dict] = []
    required: dict[str, None] = {}  # in order, across the pushed commits' own jobs files
    for line in lines:
        fields = line.split()
        if len(fields) != 4 or set(fields[1]) == {"0"}:  # a deleted ref has nothing to check
            continue
        config = _config_at(ctx, entry, records, fields[1])
        if config is None:
            refused.append({"ref": fields[0], "commit": fields[1],
                            "reason": f"it has no {_jobs_name(entry)}, so Workbench has no jobs to check it by "
                                      "(merge the CI setup into it, or push with --no-verify)"})
            continue
        refused += engine.gate_check(config, config.names[0], [line])
        required |= dict.fromkeys(config.required())
    if refused:
        for item in refused:
            ctx.log.warning(f"{item['ref']} {item['commit'][:12]}: {item['reason']}")
        ctx.finding(f"{len(refused)} ref(s) refused: {refused[0]['reason']}; run `wb run ci.run project={project}` "
                    "on the commit, then push again")
    elif required:
        ctx.summary(f"{project}: every pushed commit has passed {', '.join(required)}")
    else:
        ctx.summary(f"{project}: nothing pushed to check")
    return {"project": project, "refused": refused, "required": list(required)}


def status(ctx: Context, project: str, commit: str):
    entry = _project(ctx, project)
    records = _records(ctx, project)
    source = entry["source"]
    sha = _resolve(ctx, source, commit)
    config = _config_at(ctx, entry, records, sha)
    if config is None:
        raise ParamError(f"{project} {sha[:12]} has no {_jobs_name(entry)}, so Workbench has no jobs to check it by")
    tree = _git(ctx, ["--git-dir", str(_git_dir(ctx, source)), "rev-parse", f"{sha}^{{tree}}"])
    passed = engine.passed_jobs(records, config.names) if records.is_dir() else {}
    done = set()
    for key, jobs in passed.items():
        if key and key[0] == tree:
            done |= jobs
    required = config.required()
    missing = [j for j in required if j not in done]
    if missing:
        ctx.finding(f"{project} {sha[:12]} has not passed {', '.join(missing)}")
    else:
        ctx.summary(f"{project} {sha[:12]} has passed every job of {config.workflow}")
    return {"project": project, "commit": sha, "tree": tree, "passed": sorted(done), "missing": missing}


HOOK = """#!/bin/sh
# Written by Workbench (ci.hook): a push needs every required job passed in Workbench's records.
dir="$(cd "$(git rev-parse --git-common-dir)" && pwd)"
file="$dir/wb-push-refs.txt"
cat > "$file"
[ -s "$file" ] || exit 0
command -v cygpath >/dev/null 2>&1 && file="$(cygpath -w "$file")"
{wb} run ci.gate project={project} "refs_file=$file"
code=$?
[ $code -eq 0 ] && exit 0
[ $code -eq 1 ] && exit 1
echo "Workbench could not check this push (exit $code): start it with 'wb up', or skip the gate with git push --no-verify" >&2
exit 1
"""


def _wb_command(repo_root: Path) -> str:
    """How a hook (run by git's sh) starts the wb CLI on this platform."""
    if os.name == "nt":
        cmd = repo_root / "bin" / "wb.cmd"
        if " " in str(cmd):
            raise EnvironmentProblem(f"{cmd} has a space in its path, which a hook cannot pass to cmd.exe")
        return "cmd.exe //c " + str(cmd).replace("\\", "\\\\")
    return '"' + (repo_root / "bin" / "wb").as_posix() + '"'


def hook(ctx: Context, project: str, install: bool):
    """Installs (or removes) the pre-push hook in the project's own checkout. Runs on the host, where git runs."""
    from wb_core.service import REPO_ROOT
    entry = _project(ctx, project)
    source = Path(entry["repo"])
    common = _common_dir(_git_dir(ctx, source))
    target = common / "hooks" / "pre-push"
    wb = _wb_command(REPO_ROOT)
    if install:
        if target.exists() and "Written by Workbench (ci.hook)" not in target.read_text(encoding="utf-8",
                                                                                      errors="replace"):
            backup = target.with_name("pre-push.before-workbench")
            if not backup.exists():
                target.replace(backup)
                ctx.log.info(f"kept the previous hook as {backup.name}")
            else:
                target.unlink()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(HOOK.replace("{wb}", wb).replace("{project}", project), encoding="utf-8", newline="\n")
        os.chmod(target, 0o755)
        ctx.summary(f"pre-push hook installed in {common} (every worktree of the checkout shares it)")
    else:
        if target.exists() and "Written by Workbench (ci.hook)" in target.read_text(encoding="utf-8", errors="replace"):
            target.unlink()
            backup = target.with_name("pre-push.before-workbench")
            if backup.exists():
                backup.replace(target)
        ctx.summary(f"pre-push hook removed from {common}")
    return {"project": project, "hook": str(target), "installed": install}
