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
# What a project's steps get of this worker's environment: what any process needs to run, never Workbench's own
# (WB_TOKEN, WB_URL, WB_CONFIG, ...), so a project's build cannot act as the job that runs it. The rest of a step's
# environment is what ci.run gives it: the JDKs, the Gradle home, the pin cache, the project's own env.
STEP_ENV = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "TZ", "TERM", "HOSTNAME",
            "TMPDIR", "TEMP", "TMP", "PYTHONDONTWRITEBYTECODE",
            "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
            "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE")
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
    """A folder ``[modules.ci]`` names, else its default in Workbench's state. On the host (ci.host-run) a configured
    container path is translated back through the machine's roots, and a default is under the lab home's
    ``wb-state``, since the container's state is not the host's to write; the records must be reachable from both."""
    value = ctx.config.get(key)
    if ctx.machine != "host":
        return Path(value) if value else default
    found = _host_form(str(value)) if value else None
    if found is not None:
        return found
    if key == "records":
        where = f"= {str(value)!r} is under no root this host has" if value else "is not set"
        raise EnvironmentProblem(f"[modules.ci] records {where}, so a host check's pass would be kept where ci.run "
                                 "never looks; set it to a folder in an rw root (the out root's ci/records)")
    return _host_state(ctx) / default.relative_to(_state())


def _host_form(value: str) -> Path | None:
    """A configured path as this host names it: a container path under a root is translated back; a container path
    under no root (the daemon's own state) has no host form here."""
    try:
        from wb.workspace import Workspace
        path = Workspace().host_path(value)
    except Exception:  # noqa: BLE001 - no machine config (a test, a bare checkout): the path as given
        path = Path(value)
    if os.name == "nt" and str(path) == str(Path(value)) and value.startswith("/"):
        return None
    return path


def _host_state(ctx: Context) -> Path:
    """Where ci.host-run keeps what it makes for itself (clean trees, the host's Gradle home, Python environments):
    the lab home's ``wb-state``."""
    from wb_lab import machine
    return machine.home(ctx) / "wb-state"


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
    if ctx.machine == "host":
        # toolchain.repo-init writes the rescue repository for builds in the container, which a host job cannot ask;
        # a host check's {gradle} builds on the set's Gradle and Store alone.
        (home / "init.d" / REPOSITORY_SCRIPT).unlink(missing_ok=True)
        return home, {"script": None, "versions": 0, "substitutions": 0, "host": True}
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


def _jobs_names(entry: dict) -> list[str]:
    """The files a commit's checks may be in: the one the project entry names, else project.toml then actions.toml."""
    if entry.get("config"):
        return [str(entry["config"]).replace("\\", "/")]
    return list(engine.config_names())


def _jobs_name(entry: dict) -> str:
    return " or ".join(_jobs_names(entry))


def _config_at(ctx: Context, entry: dict, records: Path, sha: str) -> engine.Config | None:
    """The engine's config from the jobs file in commit ``sha``; None when the commit has none. Status and the gate
    judge a commit by its own jobs file: the checkout's working tree may be on another branch, or older."""
    source = entry["source"]
    for name in _jobs_names(entry):
        # Not ctx.run: it merges git's stderr into the output, which here is the file itself.
        shown = subprocess.run(["git", "--git-dir", str(_git_dir(ctx, source)), "show", f"{sha}:{name}"],
                               env={**os.environ, **GIT_SAFE}, capture_output=True)
        if shown.returncode == 0:
            break
    else:
        return None
    copy = ctx.work / f"jobs-{sha[:12]}" / Path(name).name  # the file's own name says which kind it is
    copy.parent.mkdir(parents=True, exist_ok=True)
    copy.write_bytes(shown.stdout)
    try:
        config = engine.load(source, copy)
    except engine.ToolError as error:
        raise EnvironmentProblem(f"{_jobs_name(entry)} at {sha[:12]}: {error}") from None
    config.runs = records
    return config


def _engine(ctx: Context, root: Path, jobs_file: Path, argv: list[str], env: dict, python: str,
            names: tuple = STEP_ENV) -> dict:
    """The actions runner, under the project's interpreter: its ``{python}`` is the interpreter running it. Its
    steps get ``names`` of this worker's environment and ``env``."""
    args = [python, "-m", "orthrus_tools", "actions", *argv, "--root", str(root), "--config", str(jobs_file),
            "--json"]
    base = {key: value for key, value in os.environ.items() if key.upper() in names}
    env = {**base, **GIT_SAFE, **env, "PYTHONPATH": str(TOOLS_SRC)}
    done = ctx.run(args, cwd=root, env=env, timeout=None, log_output=False, inherit=False)
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
    """The project's checks file, copied outside the tree under its own name: an edit inside the tree would change the
    very tree the run vouches for. The run records go where ``--runs`` says, not where the file does."""
    path = next(root / name for name in _jobs_names(entry) if (root / name).is_file())
    copy = work / path.name
    copy.write_bytes(path.read_bytes())
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

GRADLE_LAUNCHER = """import os, subprocess, sys
# Written by Workbench's ci for one run: the set's Gradle with the set's arguments.
args = {args!r}
sys.exit(subprocess.call([*args, *sys.argv[1:]]))
"""


def _stand_ins(ctx: Context, work: Path, wanted: dict) -> list[str]:
    """Gradle arguments for stand-ins: each project's files, as CI built them for the tree given, laid out in a folder at
    the paths its build left them (so set.base finds each by its glob) and named with -Pset.with.<name>."""
    import shutil as copier
    from wb_core import artifacts as store
    root = _store_root(ctx)
    args = []
    for name, tree in sorted((wanted or {}).items()):
        kept = store.find(root, name, str(tree))
        if not kept or not kept.get("artifacts"):
            raise EnvironmentProblem(f"the Store holds nothing of {name} built at tree {str(tree)[:12]}: run its checks "
                                     "first (wb check does, for --with)")
        folder = work / "stand-ins" / name
        for file_name, item in kept["artifacts"].items():
            target = folder / (item.get("from") or file_name).replace("\\", "/")
            target.parent.mkdir(parents=True, exist_ok=True)
            copier.copyfile(store.path_of(root, item["sha256"], file_name), target)
        args.append(f"-Pset.with.{name}={folder}")
        ctx.log.info(f"{name} stands in from tree {str(tree)[:12]}: {len(kept['artifacts'])} file(s)")
    return args


def _tools(ctx: Context, config, work: Path, revision: str) -> dict[str, str]:
    """Files a check reads from another project's tree (tools = ['<project>:<glob>']), exported at the commit the
    run's set names for that project (a project with no line: its branch in its home repository), into the folder beside the
    clone where its checkout would be, so the tools that look for a sibling checkout find them there. Returns each
    project with the commit its files came from."""
    import fnmatch
    import io
    import tarfile
    from wb_core import setfile
    wanted: dict[str, set[str]] = {}
    for job in config.jobs.values():
        for item in job.get("tools") or []:
            project, glob = str(item).split(":", 1)
            wanted.setdefault(project, set()).add(glob)
    if not wanted:
        return {}
    sets = _store_root(ctx).parent / "sets"
    revision = revision or setfile.read_head(sets) or ""
    try:
        lines = (setfile.read_export(sets, revision).get("projects") or {}) if revision else {}
    except setfile.SetError as error:
        raise EnvironmentProblem(f"tools: the set {revision[:12]} cannot be read: {error}") from None
    laid = {}
    for project, globs in sorted(wanted.items()):
        line = lines.get(project)
        entry = _project(ctx, project)
        source = entry["source"]
        if line:
            sha = _resolve(ctx, source, line["commit"])
        else:
            # A project with no line follows its branch in its home repository (origin's when it has a remote, never
            # whatever its home checkout happens to have checked out), and the run says which commit.
            branch = str(entry.get("branch") or "main")
            git_dir = str(_git_dir(ctx, source))
            ref = next((r for r in (f"refs/remotes/origin/{branch}", f"refs/heads/{branch}", "HEAD")
                        if _git(ctx, ["--git-dir", git_dir, "rev-parse", "--verify", "-q", f"{r}^{{commit}}"],
                                check=False)), "HEAD")
            sha = _resolve(ctx, source, ref)
            ctx.log.info(f"tools: {project} has no line in the set {revision[:12] or '(none)'}; {ref} at {sha[:12]}")
        git_dir = str(_git_dir(ctx, source))
        names = _git(ctx, ["--git-dir", git_dir, "ls-tree", "-r", "--name-only", sha]).splitlines()
        chosen = sorted(n for n in names if any(fnmatch.fnmatchcase(n, g) for g in globs))
        if not chosen:
            raise EnvironmentProblem(f"tools: nothing in {project} at {sha[:12]} matches {', '.join(sorted(globs))}")
        done = subprocess.run(["git", "--git-dir", git_dir, "archive", "--format=tar", sha, "--", *chosen],
                              env={**os.environ, **GIT_SAFE}, capture_output=True)
        if done.returncode != 0:
            raise EnvironmentProblem(f"tools: git archive of {project} at {sha[:12]} failed: "
                                     + done.stderr.decode(errors="replace")[-300:])
        target = work / source.name
        with tarfile.open(fileobj=io.BytesIO(done.stdout)) as archive:
            archive.extractall(target, filter="data")
        laid[project] = sha
        ctx.log.info(f"tools: {len(chosen)} file(s) of {project} at {sha[:12]} beside the clone ({target.name})")
    return laid


def _set_identity(ctx: Context, config, revision: str) -> dict[str, str]:
    """For a project that builds from a set (project.toml): WB_SET_IDENTITY, a digest of the set's platform and of the
    lines, holds and third entries the project uses, and WB_JDK_IDENTITY, a digest of the platform's JDK builds. Empty
    for a project still on actions.toml, or with no set revision."""
    import hashlib
    from wb_core import setfile
    if config.path.name != engine.PROJECT_FILE or not revision:
        return {}
    try:
        doc = setfile.read_export(_store_root(ctx).parent / "sets", revision)
    except setfile.SetError:
        return {}
    uses = set((config.data.get("uses") or {}).keys())
    lines = {n: line for n, line in (doc.get("projects") or {}).items() if n in uses}
    thirds = {n: entry for n, entry in (doc.get("third") or {}).items() if n in uses}
    holds = {n: hold for n, hold in (doc.get("holds") or {}).items()
             if n.split(":")[0].split("@")[0] in uses or n.split("@")[-1] == config.settings.get("name")}
    platform = doc.get("platform") or {}

    def digest(value) -> str:
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

    return {"WB_SET_IDENTITY": digest({"platform": platform, "lines": lines, "holds": holds, "third": thirds}),
            "WB_JDK_IDENTITY": digest(platform.get("jdk") or {})}


def _gradle_launcher(ctx: Context, work: Path, revision: str, extra: list | None = None) -> tuple[Path, str]:
    """The launcher {gradle} runs: the Gradle distribution the set's platform names, from the Store, with -Pset.dir
    (the run's export of the set), -Pset.store, -Pset.plugins, -Pset.exact=true and the platform's JDK builds as
    Gradle's only installations. Without that Gradle in the Store, the launcher says what fetches it."""
    from wb_core import setfile
    root = _store_root(ctx)
    sets = root.parent / "sets"
    launcher = work / "gradle-launcher.py"
    revision = revision or setfile.read_head(sets) or ""
    try:
        platform = setfile.read_export(sets, revision).get("platform") or {} if revision else {}
    except setfile.SetError:
        platform = {}
    version = str((platform.get("gradle") or {}).get("version") or "")
    found = sorted((root / "gradle" / version).glob("*/bin/" + ("gradle.bat" if os.name == "nt" else "gradle"))) \
        if version else []
    if not found:
        why = (f"no Gradle {version} in the Store for set {revision[:12]}: wb run store.platform" if version
               else "no set revision for this run: wb work sync, or name one with set=<revision>")
        launcher.write_text(f"import sys\nsys.exit('{{gradle}}: {why}')\n", encoding="utf-8")
        return launcher, "", ""
    system = "windows" if os.name == "nt" else "linux"
    jdks = []
    for major, jdk in sorted((platform.get("jdk") or {}).items()):
        jdks += [str(p) for p in sorted((root / "jdks" / str(jdk.get("build")) / system).glob("*")) if p.is_dir()]
    # Through sh off Windows: a distribution unpacked from a zip may carry no execute bit on bin/gradle.
    args = [*([] if os.name == "nt" else ["sh"]), str(found[0]), f"-Pset.dir={sets / revision}", f"-Pset.store={root}",
            f"-Pset.plugins={(root / 'plugins').resolve().as_uri()}",
            "-Pset.exact=true", "-Porg.gradle.java.installations.auto-detect=false",
            "-Porg.gradle.java.installations.auto-download=false"]
    if jdks:
        args.append(f"-Porg.gradle.java.installations.paths={','.join(jdks)}")
    args += list(extra or [])
    launcher.write_text(GRADLE_LAUNCHER.format(args=args), encoding="utf-8")
    # The same values for a ./gradlew a step script starts: Gradle reads org.gradle.project.* system properties from
    # GRADLE_OPTS as the build's properties, so that build resolves the same set, Store and JDKs.
    opts = " ".join("-Dorg.gradle.project." + a[2:] for a in args if a.startswith("-P") and " " not in a)
    return launcher, revision, opts


def _checkout(ctx: Context, project: str, entry: dict, commit: str, checkout, work: Path) -> dict:
    """The clean tree a run tests, under ``work``: the project cloned at ``commit`` (with its working tree laid over it
    for commit=worktree) and each repository its jobs file pins, at its pin. Returns the clone's root, the commit,
    the engine's config with the run records' folder and the pinned commits."""
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
    root = work / source.name
    worktree = commit == "worktree"
    sha = _resolve(ctx, tree_source, "HEAD" if worktree else commit)
    ctx.progress("clone", msg=f"{project} at {sha[:12]}" + (" with its working tree" if worktree else ""))
    _clone(ctx, source, root, sha)
    if worktree:
        if ctx.machine == "host":  # the host's git reads its own excludes and config
            _overlay_worktree(ctx, tree_source, root, [], {})
        else:
            _overlay_worktree(ctx, tree_source, root, list(ctx.config.get("excludes") or []),
                              dict(ctx.config.get("git_config") or {}))
    if not any((root / name).is_file() for name in _jobs_names(entry)):
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
    return {"root": root, "commit": sha, "worktree": worktree, "config": config, "pinned": pinned,
            "records": records}


def _environment(ctx: Context, entry: dict, config, root: Path, work: Path, set: str, stand_ins: dict | None,
                 cold: bool) -> dict:
    """What a run's steps get: the JDKs, the Gradle home, the pin cache, the {gradle} launcher on the set, the files
    another project's tree lends its checks, the set's identities and the project's env; and {python}."""
    major = str(entry["jdk"] if "jdk" in entry else ctx.config.get("jdk", "26"))  # "": no JAVA_HOME
    pins = _folder(ctx, "pin_cache", _state() / "cache" / "pins")
    pins.mkdir(parents=True, exist_ok=True)
    gradle_home, served = _gradle_home(ctx)
    launcher, set_revision, gradle_opts = _gradle_launcher(ctx, work, set, _stand_ins(ctx, work, stand_ins or {}))
    tools = _tools(ctx, config, work, set_revision or set)
    identity = _set_identity(ctx, config, set_revision or set)
    if tools:  # a pure check's key covers the files it reads from another project, as laid out here
        identity["WB_TOOLS_ROOTS"] = json.dumps({p: str(work / _project(ctx, p)["source"].name) for p in tools})
    env = {**_jdk_env(ctx, major), "GRADLE_USER_HOME": str(gradle_home), "WB_PIN_CACHE": str(pins),
           "WB_GRADLE": str(launcher), **({"GRADLE_OPTS": gradle_opts} if gradle_opts else {}),
           **{k: str(v) for k, v in (entry.get("env") or {}).items()}, **({"WB_COLD": "1"} if cold else {}),
           **identity}
    python = _python(ctx, root, [str(r) for r in entry.get("python") or config.data.get("project", {}).get("python")
                                 or []])
    return {"env": env, "python": python, "served": served, "set": set_revision, "tools": tools}


def _engine_argv(ctx: Context, targets: list[str], run_id: str, records: Path, if_needed: bool, keep_going: bool,
                 dry_run: bool, cold: bool = False) -> list[str]:
    # The engine runs inside this job's reservation: the job id names its run, and its budget is what the
    # scheduler holds for the job, not the machine.
    argv = ["run", *targets, "--run-id", run_id, "--runs", str(records)]
    if ctx.reservation.get("mem_gb"):
        argv += ["--memory", str(ctx.reservation["mem_gb"])]
    # Cold: every job runs, none vouched for by an earlier pass or its input cache, and no build cache is read.
    argv += ["--no-cache"] if cold else (["--if-needed"] if if_needed else [])
    argv += ["--keep-going"] if keep_going else []
    argv += ["--dry-run"] if dry_run else []
    return argv


def _container_targets(ctx: Context, config, targets: list[str]) -> list[str]:
    """The targets a container run is given: as asked, unless they take in host checks (``on = "host"`` or
    ``gpu = true``; ``all`` names every check), which only ci.host-run runs. Then the container checks they take in,
    by name, and a log line naming the host checks left out; nothing else left is refused."""
    try:
        ordered = engine.resolve(config, targets)
    except engine.ToolError:
        return targets  # the engine says what is wrong with them
    host = [name for name in ordered if engine.on_host(config.jobs[name])]
    if not host:
        return targets
    rest = [name for name in ordered if name not in host]
    needing = [name for name in rest if set(config.jobs[name].get("needs") or []) & set(host)]
    if needing:
        raise ParamError(f"{', '.join(needing)} need(s) {', '.join(host)}, which run(s) on the host; a container "
                         "check cannot wait for a host check")
    if not rest:
        raise ParamError(f"{', '.join(host)} run(s) on the host, not in the container: wb check {' '.join(host)} "
                         "runs them there (ci.host-run)")
    ctx.log.info(f"left to the host: {', '.join(host)} (wb check {' '.join(host)} runs them there)")
    return rest


def run(ctx: Context, project: str, commit: str, workflow: str, jobs: list, if_needed: bool, keep_going: bool,
        keep: bool, dry_run: bool, checkout=None, cold: bool = False, set: str = "", stand_ins: dict | None = None):
    entry = _project(ctx, project)
    work = _folder(ctx, "workdir", _state() / "ci" / "trees") / f"{project}-{ctx.job}"
    try:
        tree = _checkout(ctx, project, entry, commit, checkout, work)
        root, sha, worktree, config, pinned = (tree[k] for k in ("root", "commit", "worktree", "config", "pinned"))
        records = tree["records"]
        jobs_file = _jobs_copy(root, entry, records, work)
        prepared = _environment(ctx, entry, config, root, work, set, stand_ins, cold)
        served, set_revision, tools = prepared["served"], prepared["set"], prepared["tools"]
        if stand_ins:
            # Against stand-in bytes a run vouches for nothing: its records go one level down, where the gate, which
            # reads <records>/<run>/summary.json, never counts them, and its step logs stay readable.
            records = records / "stand-ins"
            records.mkdir(parents=True, exist_ok=True)
        targets = [*([workflow] if workflow else []), *[str(j) for j in jobs]] or [config.workflow]
        targets = _container_targets(ctx, config, targets)
        argv = _engine_argv(ctx, targets, ctx.job, records, if_needed, keep_going, dry_run, cold)
        ctx.progress("jobs", msg=" ".join([*targets, *argv[len(targets) + 3:]]))
        document = _engine(ctx, root, jobs_file, argv, prepared["env"], prepared["python"])
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
    stored = {} if result.get("treesChanged") or stand_ins else _store_artifacts(ctx, project, sha, result.get("trees") or {},
                                                                     records, run_id, jobs_done)
    failed = [n for n, j in jobs_done.items() if j.get("status") == "failed"]
    passed = [n for n, j in jobs_done.items() if j.get("status") == "passed"]
    trees = result.get("trees") or {}
    _conclude(ctx, project, sha, document, jobs_done, passed)
    return {"project": project, "commit": sha, "worktree": worktree, "pinned": pinned, "run": run_id,
            "trees": trees, "passed": passed, "failed": failed, "repository": served, "stored": stored,
            "engine": result, "set": set_revision, "stand_ins": dict(stand_ins or {}), "tools": tools}


def _conclude(ctx: Context, project: str, sha: str, document: dict, jobs_done: dict, passed: list):
    """The run's outcome from the engine's document: a failed job is a finding, a refusal a usage or environment
    problem, else a summary of what passed."""
    status = document.get("status")
    failed = [n for n, j in jobs_done.items() if j.get("status") == "failed"]
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
            kept[file_name] = {"sha256": item["sha256"], "bytes": item.get("bytes"), "job": name, "run": run_id,
                               "from": item.get("from")}  # where in the tree the build left it
    for file_name, item in kept.items():
        if project == "set" and file_name.startswith("set-plugins-") and file_name.endswith(".zip"):
            placed = _store_plugins(root, store.path_of(root, item["sha256"], file_name))
            ctx.log.info(f"the Set's plugins from {file_name}: {placed} file(s) added to the Store's plugins folder")
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


def _store_plugins(root: Path, packed: Path) -> int:
    """Unpacks the Set's plugins (a Maven folder its logic check packed, one version per logic id) into the Store's
    plugins folder. A file is written once and never replaced; Maven's metadata files are left out, since a build asks
    for an exact version and every landed logic id stays resolvable."""
    import zipfile
    target = root / "plugins"
    placed = 0
    with zipfile.ZipFile(packed) as archive:
        for info in archive.infolist():
            name = info.filename
            if info.is_dir() or Path(name).name.startswith("maven-metadata"):
                continue
            path = (target / name).resolve()
            if not path.is_relative_to(target.resolve()):
                raise EnvironmentProblem(f"{packed.name}: {name} would be written outside the plugins folder")
            data = archive.read(info)
            if path.is_file():
                if path.read_bytes() != data:
                    raise EnvironmentProblem(f"the Store's plugins folder holds other bytes at {name}; a logic id's "
                                             "files are never replaced")
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            part = path.with_name(path.name + f".{os.getpid()}.part")
            part.write_bytes(data)
            part.replace(path)
            placed += 1
    return placed


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
