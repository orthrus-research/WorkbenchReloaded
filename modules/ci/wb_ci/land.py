"""set.land: one landing, under lock ``set``; and set.reach, what a landing would reach, before it runs.

The CLI (``wb land``) has fetched, checked that each commit descends from its line, run each commit's gate checks
with ci.run (producers first: a project that uses another one landing with it runs against a set already holding
that one's new line), asked set.reach which consumers the landing reaches, run their gate checks against the would-be
set, and exported the Set at the head it validated against. This job, holding lock ``set``:

1. composes the new set.json from that head: each landed commit descends from its line, and each artifact the
   project's manifest names is found in the Store, by its check and glob: exactly one file each;
2. checks again at that head: each landed project, and each consumer the landing reaches (whose [uses] names a
   project whose files changed, whose tools read a moved project's tree, that uses a moved third entry, or that
   builds with {gradle} when the platform moved), has a pass record in the Store for every gate check under the key
   it has in the new set, with the artifacts the pass records name;
3. applies the Set gate: no line or third entry with a URL a public set may not carry, and no launch of any project
   that would load two builds of one mod id (a member that declares no mod id is a warning);
4. composes the commit body (names, commits, checks, artifacts, what was reached; no paths) and scrubs it and
   set.json with every ref name the publish will push, the tag's included;
5. writes the landing record (state ``prepared``) and waits, holding the lock, while the CLI publishes with the
   caller's own git and moves the record. It settles when the record says landed or abandoned, or at its deadline.

It runs no check itself and submits no child job: a missing pass is refused with the command that makes it.

set.reach composes the same set without the lock, exports it under a candidate name beside the Set's exports (so a
check can run against it with set=<candidate>), applies the Set gate early, and returns the reached consumers, each
with whether its pass records already stand under its new keys."""

from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import subprocess
import time
import tomllib
from pathlib import Path

from wb_core import artifacts as store
from wb_core import landing as landings
from wb_core import passes
from wb_core import setfile
from wb_core.module import Context, EnvironmentProblem, ParamError

from . import commands
from .commands import engine

TAG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*(/[A-Za-z0-9][A-Za-z0-9._+-]*)*$")
GRADLE_WORD = "{env.WB_GRADLE}"  # what the engine makes of a {gradle} step


def _sets(ctx: Context) -> Path:
    return commands._store_root(ctx).parent / "sets"


def _scrub(ctx: Context, texts: dict[str, str]) -> list[str]:
    """Findings in the composed texts, under the machine's rules (ci's tool_env names the rules file)."""
    from orthrus_tools import scrub
    saved = {k: os.environ.get(k) for k in (ctx.config.get("tool_env") or {})}
    os.environ.update({k: str(v) for k, v in (ctx.config.get("tool_env") or {}).items()})
    try:
        rules = scrub.Rules.load(ctx.work)
        return [str(f) for where, text in texts.items() for f in scrub.check_text(text, where, rules)]
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _descends(ctx: Context, source: Path, old: str, new: str) -> bool:
    done = subprocess.run(["git", "--git-dir", str(commands._git_dir(ctx, source)), "merge-base", "--is-ancestor",
                           old, new], env={**os.environ, **commands.GIT_SAFE}, capture_output=True)
    return done.returncode == 0


def _tree(ctx: Context, source: Path, sha: str) -> str:
    return commands._git(ctx, ["--git-dir", str(commands._git_dir(ctx, source)), "rev-parse", f"{sha}^{{tree}}"])


def _any_pass(ctx: Context, name: str, records: Path, config, tree: str) -> set[str]:
    """The checks that have passed on ``tree`` under any key: a pass record in the Store, else CI's run summaries."""
    root = commands._store_root(ctx)
    done = {p.name.split(".")[0] for p in (root / passes.PASSES / name / tree).glob("*.json")} \
        if (root / passes.PASSES / name / tree).is_dir() else set()
    summaries = engine.passed_jobs(records, config.names) if records.is_dir() else {}
    return done.union(*[jobs for key, jobs in summaries.items() if key and key[0] == tree] or [set()])


def _member(ctx: Context, name: str, wanted: dict, base: dict) -> tuple[dict, str]:
    """The new line of one landed project, and the body line that says what moved."""
    entry = commands._project(ctx, name)
    records = commands._records(ctx, name)
    source = entry["source"]
    sha = commands._resolve(ctx, source, wanted["commit"])
    old = setfile.line(base, name)
    if old and old["commit"] != sha and not _descends(ctx, source, old["commit"], sha):
        raise ParamError(f"{name} {sha[:12]} does not descend from its line {old['commit'][:12]}: "
                         f"git rebase {old['commit'][:12]}, or git revert <range> to roll back, then wb land")
    config = commands._config_at(ctx, entry, records, sha)
    if config is None or config.path.name != engine.PROJECT_FILE:
        raise ParamError(f"{name} {sha[:12]} has no {engine.PROJECT_FILE}, so it has no line to land: convert it "
                         "first; until then it moves by its own route")
    tree = _tree(ctx, source, sha)
    done = _any_pass(ctx, name, records, config, tree)
    missing = [check for check in config.required() if check not in done]
    if missing:
        raise ParamError(f"{name} {sha[:12]} has not passed {', '.join(missing)}: wb check in {name} at that commit "
                         f"(or wb run ci.run project={name} commit={sha[:12]}), then wb land")
    kept = store.find(commands._store_root(ctx), name, tree) or {"artifacts": {}}
    files = {}
    for artifact, spec in (config.data.get("artifacts") or {}).items():
        # Matched on the path the build left the file at, where the Store knows it; by name for older records.
        matched = [f for f, item in kept["artifacts"].items()
                   if item.get("job") == spec["check"] and (fnmatch.fnmatch(item["from"].replace("\\", "/"), spec["file"])
                                                           if item.get("from") else
                                                           fnmatch.fnmatch(f, Path(spec["file"]).name))]
        if len(matched) != 1:
            raise EnvironmentProblem(f"{name} {sha[:12]}: the Store holds {len(matched)} file(s) of check "
                                     f"{spec['check']} matching {spec['file']} ({', '.join(matched) or 'none'}); "
                                     "a landing needs exactly one")
        item = kept["artifacts"][matched[0]]
        files[artifact] = {"sha256": item["sha256"], "file": matched[0], "glob": spec["file"], "check": spec["check"]}
        if spec.get("mod_id"):
            files[artifact]["mod_id"] = str(spec["mod_id"])
    line = {"branch": wanted.get("branch") or (old or {}).get("branch") or "main", "commit": sha, "artifacts": files}
    url = wanted.get("url") if "url" in wanted else (old or {}).get("url")
    if url:
        line["url"] = url
    moved = f"{old['commit'][:12]}..{sha[:12]}" if old else f"added at {sha[:12]}"
    if old and old["commit"] == sha:
        moved = f"unchanged at {sha[:12]}"
    body = (f"{name} {moved} on {line['branch']}: gate {', '.join(config.required()) or '(empty)'} passed; "
            + (", ".join(f"{a} {f['sha256'][:12]}" for a, f in sorted(files.items())) or "no artifacts"))
    return line, body


SET = "set"  # the Set's own repository, as a ci project: a change to its files lands like any change


def _set_change(ctx: Context, wanted: dict, base: str, current: dict) -> tuple[str, str | None, str]:
    """A change to the Set's own files: it descends from the head, leaves set.json alone (only a landing writes it),
    and has passed the Set's gate. Returns its commit, the tree id of its build-logic/ (None without one) and the
    body line."""
    entry = commands._project(ctx, SET)
    source = entry["source"]
    sha = commands._resolve(ctx, source, wanted["commit"])
    if not _descends(ctx, source, base, sha):
        raise ParamError(f"the Set change {sha[:12]} does not descend from the head {base[:12]}: git rebase "
                         f"{base[:12]} in the Set's worktree, then wb land set=<commit>")
    shown = subprocess.run(["git", "--git-dir", str(commands._git_dir(ctx, source)), "show", f"{sha}:set.json"],
                           env={**os.environ, **commands.GIT_SAFE}, capture_output=True, text=True, encoding="utf-8")
    try:
        theirs = setfile.parse(shown.stdout) if shown.returncode == 0 else None
    except setfile.SetError:
        theirs = None
    if theirs != current:
        raise ParamError(f"the Set change {sha[:12]} edits set.json, which only a landing writes: take that edit out "
                         "(a line moves by wb land <project>=<ref>, a hold by --hold)")
    records = commands._records(ctx, SET)
    config = commands._config_at(ctx, entry, records, sha)
    if config is None:
        raise ParamError(f"the Set change {sha[:12]} has no project.toml, so it has no gate to pass")
    tree = _tree(ctx, source, sha)
    # The Set's own checks run against the head: the logic its change carries is what the landing moves.
    done = _any_pass(ctx, SET, records, config, tree)
    missing = [check for check in config.required() if check not in done]
    if missing:
        raise ParamError(f"the Set change {sha[:12]} has not passed {', '.join(missing)}: wb check in the Set's "
                         "worktree, then wb land")
    logic = commands._git(ctx, ["--git-dir", str(commands._git_dir(ctx, source)), "rev-parse", "--verify", "-q",
                                f"{sha}:build-logic"], check=False) or None
    body = (f"set files {base[:12]}..{sha[:12]}: gate {', '.join(config.required()) or '(empty)'} passed"
            + (f"; logic {logic[:12]}" if logic else ""))
    return sha, logic, body


THIRD_DIR = "third"  # beside the Store: <out>/third/<name>/ holds the JAR a third entry without a coordinate comes from


def _third_file(ctx: Context, name: str, wanted: dict) -> tuple[dict, str]:
    """A third-party file with no Maven publication: the one JAR in <out>/third/<name>/, seeded into the Store with
    where it came from, and landed as {file, sha256, source, licence, mod_id}, which build-logic's set.third and the
    launch read from the Store. A SHA-256 given must be the file's."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", name):
        raise ParamError(f"third.{name}: a name is lowercase letters, digits, '.', '_' and '-'")
    root = commands._store_root(ctx)
    folder = root.parent / THIRD_DIR / name
    shown = ctx.host_path(folder)
    jars = sorted(p for p in folder.glob("*.jar") if p.is_file()) if folder.is_dir() else []
    if len(jars) != 1:
        raise ParamError(f"third.{name}: put its one JAR in {shown} (found {len(jars)})")
    source, licence = str(wanted.get("source") or ""), str(wanted.get("licence") or "")
    if not source or not licence:
        raise ParamError(f"third.{name}: a file without a coordinate needs where it came from (--source) and its "
                         "licence (--licence)")
    digest = store.sha256(jars[0])
    expected = str(wanted.get("sha256") or "").lower()
    if expected and expected != digest:
        raise ParamError(f"third.{name}: {jars[0].name} in {shown} is {digest[:12]}, not the {expected[:12]} given")
    store.seed(root, jars[0], f"third:{name}", source, f"the set's third entry {name}",
               commands._folder(ctx, "pin_cache", commands._state() / "cache" / "pins"))
    entry = {"file": jars[0].name, "sha256": digest, "source": source, "licence": licence}
    if wanted.get("mod_id"):
        entry["mod_id"] = str(wanted["mod_id"])
    return entry, f"third {name} {jars[0].name} from {source} ({licence}): {digest[:12]}"


def _third(ctx: Context, name: str, wanted: dict) -> tuple[dict, str]:
    """A third-party entry: each file is fetched from its Maven repository (from a host in store_hosts, every redirect
    checked), checked against the SHA-256 given, and kept in the Store. Returns the entry and the body line. An entry
    without a module comes from the drop folder instead (_third_file)."""
    import tempfile
    if not wanted.get("module") and not wanted.get("repository"):
        return _third_file(ctx, name, wanted)
    from . import platform
    root = commands._store_root(ctx)
    hosts = [str(h) for h in ctx.config.get("store_hosts") or []]
    module = str(wanted.get("module") or "")
    parts = module.split(":")
    if len(parts) != 3 or not all(parts):
        raise ParamError(f"third.{name}: module is group:artifact:version, not {module!r}")
    group, artifact, version = parts
    repository = str(wanted.get("repository") or "").rstrip("/")
    problem = setfile.url_problem(repository)
    if problem:
        raise ParamError(f"third.{name}: its repository is not a URL a public set may carry ({problem})")
    files = {}
    for kind, item in (wanted.get("files") or {}).items():
        classifier = str(item.get("classifier") or "")
        file_name = f"{artifact}-{version}{'-' + classifier if classifier else ''}.jar"
        url = f"{repository}/{group.replace('.', '/')}/{artifact}/{version}/{file_name}"
        digest = str(item.get("sha256") or "")
        if not platform._allowed(url, hosts):
            raise ParamError(f"third.{name}: {url} is on a host outside [modules.ci] store_hosts")
        if not (root / "files" / digest / file_name).is_file():
            ctx.progress("fetch", msg=f"third.{name} {kind}: {file_name}")
            with tempfile.TemporaryDirectory(dir=root) as scratch:
                downloaded = Path(scratch) / file_name
                platform._download(ctx, url, digest, downloaded, hosts)
                store.put(root, downloaded, digest, commands._folder(ctx, "pin_cache", commands._state() / "cache" / "pins"))
        files[kind] = digest
    if not files:
        raise ParamError(f"third.{name}: name at least one file")
    entry = {"module": module, "repository": repository, "files": files}
    for key in ("licence", "mod_id"):
        if wanted.get(key):
            entry[key] = str(wanted[key])
    return entry, (f"third {name} {module} ({wanted.get('licence') or 'licence not given'}): "
                   + ", ".join(f"{k} {d[:12]}" for k, d in sorted(files.items())))


def _hold(ctx: Context, name: str, hold: dict) -> tuple[dict, str]:
    """A hold's entry: its files must be seeded in the Store (bytes no container builds, with their source)."""
    root = commands._store_root(ctx)
    files = {}
    for artifact, item in (hold.get("artifacts") or {}).items():
        try:
            store.path_of(root, item["sha256"], item["file"])
        except (store.StoreError, KeyError) as error:
            raise ParamError(f"hold {name}: {error}; seed it first (wb store add)") from None
        if not (root / "seeded" / f"{item['sha256']}.json").is_file():
            raise ParamError(f"hold {name}: {item['file']} is in the Store but was not seeded with its source")
        files[artifact] = {"sha256": item["sha256"], "file": item["file"]}
    entry = {"commit": hold["commit"], "artifacts": files, "reason": hold["reason"],
             "since": hold.get("since") or time.strftime("%Y-%m-%d")}
    if hold.get("source"):
        entry["source"] = hold["source"]
    return entry, f"hold {name} at {hold['commit'][:12]}: {hold['reason']}"


# -- composing ----------------------------------------------------------------------------------------------------

def _compose(ctx: Context, base: str, projects: dict, holds: dict, drop_holds: list, third: dict,
             platform: dict) -> dict:
    """The set a landing makes from the head: its lines, holds, third entries and platform values, with the body
    lines that say what moved. Composing from the head means a landing that waited on the lock builds on the one
    before it."""
    projects = dict(projects or {})
    change_wanted = projects.pop(SET, None)
    sets = _sets(ctx)
    head = setfile.read_head(sets)
    if head and head != base:
        # Another landing moved the Set while this one waited: compose from the head it left.
        ctx.log.info(f"the Set moved from {base[:12]} to {head[:12]} while this landing waited; composing from it")
        base = head
    try:
        current = setfile.read_export(sets, base)
    except setfile.SetError as error:
        raise ParamError(f"{error}; wb land exports the head before it submits") from None
    lines, body = {}, []
    change = logic = None
    if change_wanted:
        ctx.progress("validate", msg=f"the Set's files at {str(change_wanted.get('commit'))[:12]}")
        change, logic, change_text = _set_change(ctx, change_wanted, base, current)
        body.append(change_text)
    for name in sorted(projects):
        ctx.progress("validate", msg=f"{name} at {str(projects[name].get('commit'))[:12]}")
        lines[name], line_text = _member(ctx, name, projects[name], current)
        body.append(line_text)
    composed = setfile.with_lines(current, lines)
    logic_moved = bool(change and logic != (current.get("platform") or {}).get("logic"))
    if logic_moved:
        composed.setdefault("platform", {})["logic"] = logic  # every project with a {gradle} check is reached
    for name in drop_holds:
        if name not in (composed.get("holds") or {}):
            raise ParamError(f"there is no hold {name} to drop")
        del composed["holds"][name]
        body.append(f"hold {name} dropped")
    for name in sorted(holds):
        composed.setdefault("holds", {})[name], hold_text = _hold(ctx, name, holds[name])
        body.append(hold_text)
    for name in sorted(third):
        composed.setdefault("third", {})[name], third_text = _third(ctx, name, third[name])
        body.append(third_text)
    if platform:
        from . import platform as platforms
        for key in sorted(platform):
            ctx.progress("platform", msg=f"platform.{key} = {platform[key]}")
            composed["platform"], platform_text = platforms.resolve(ctx, key, str(platform[key]),
                                                                    composed.get("platform") or {})
            body.append(platform_text)
    return {"base": base, "current": current, "composed": composed, "lines": lines, "body": body,
            "change": change, "logic_moved": logic_moved}


# -- reach --------------------------------------------------------------------------------------------------------

def _manifest(ctx: Context, name: str, commit: str):
    """The engine's config of project ``name`` at ``commit``, and its project.toml as written; (None, None) when it
    is not registered here, lacks the commit or has no project.toml."""
    try:
        entry = commands._project(ctx, name)
        config = commands._config_at(ctx, entry, commands._records(ctx, name), commit)
    except (ParamError, EnvironmentProblem, OSError):
        return None, None
    if config is None or config.path.name != engine.PROJECT_FILE:
        return None, None
    try:
        raw = tomllib.loads(config.path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        raw = {}
    return config, raw


def _gate_checks(config) -> list[str]:
    try:
        return engine.resolve(config, config.required())
    except engine.ToolError:
        return list(config.required())


def _reach(ctx: Context, before: dict, after: dict, landed: set[str], logic_moved: bool) -> tuple[dict, list[str]]:
    """The consumers a landing reaches, {name: {commit, why}}, and notes for the person. Edges are read from each
    project's project.toml at the commit the new set names: [uses] naming a project whose files changed (a
    byte-identical JAR reaches nobody), tools reading a moved project's tree, [uses] naming a moved third entry, and
    a {gradle} gate check when the platform moved. Projects landing themselves are checked as landed, not here."""
    old_lines, new_lines = before.get("projects") or {}, after.get("projects") or {}
    moved = {n for n, line in new_lines.items() if (old_lines.get(n) or {}).get("commit") != line.get("commit")}
    changed, notes = set(), []
    for name in sorted(moved):
        old = {a: i.get("sha256") for a, i in ((old_lines.get(name) or {}).get("artifacts") or {}).items()}
        new = {a: i.get("sha256") for a, i in (new_lines[name].get("artifacts") or {}).items()}
        if name in old_lines and old == new:
            notes.append(f"{name}'s files are byte-identical to its line's, so it reaches nobody through [uses]")
        else:
            changed.add(name)
    third_moved = {n for n in set(before.get("third") or {}) | set(after.get("third") or {})
                   if (before.get("third") or {}).get(n) != (after.get("third") or {}).get(n)}
    platform_moved = logic_moved or (before.get("platform") or {}) != (after.get("platform") or {})
    if not (changed or moved or third_moved or platform_moved):
        return {}, notes
    reached = {}
    for name in sorted(new_lines):
        if name in landed or name == SET:
            continue
        commit = new_lines[name]["commit"]
        config, _ = _manifest(ctx, name, commit)
        if config is None:
            notes.append(f"{name}'s project.toml at {commit[:12]} cannot be read here (not registered, or the "
                         "commit is not in its checkout), so whether this landing reaches it is not known")
            continue
        gate = _gate_checks(config)
        uses = set(config.data.get("uses") or {})
        tools = {str(t).split(":")[0] for check in gate for t in config.jobs[check].get("tools") or []}
        gradle = any(GRADLE_WORD in (step.get("run") or []) for check in gate
                     for step in config.jobs[check].get("steps") or [] if isinstance(step, dict))
        why = [f"uses {n}" for n in sorted(uses & changed)]
        why += [f"reads {n}'s files" for n in sorted((tools & moved) - {name})]
        why += [f"uses third {n}" for n in sorted(uses & third_moved)]
        if platform_moved and gradle:
            why.append("builds with {gradle} and the platform moved")
        if why:
            reached[name] = {"commit": commit, "why": why}
    return reached, notes


def _unpassed(ctx: Context, name: str, commit: str, doc: dict, revision: str = "") -> tuple[str, list[str]]:
    """The tree of ``name`` at ``commit``, and the gate checks with no pass record under the key they have in the
    set ``doc``."""
    entry = commands._project(ctx, name)
    config = commands._config_at(ctx, entry, commands._records(ctx, name), commit)
    if config is None:
        raise ParamError(f"{name} {commit[:12]} has no {engine.PROJECT_FILE}")
    tree = _tree(ctx, entry["source"], commit)
    _, keys = commands.pass_keys(ctx, config, tree, doc, revision)
    required = config.required()
    have = passes.passed(commands._store_root(ctx), name, tree, {c: keys[c] for c in required if c in keys})
    return tree, [check for check in required if check not in have]


def _artifacts_match(ctx: Context, name: str, line: dict, doc: dict) -> list[str]:
    """Where a landed line's files differ from what its producing checks' pass records name."""
    entry = commands._project(ctx, name)
    config = commands._config_at(ctx, entry, commands._records(ctx, name), line["commit"])
    tree = _tree(ctx, entry["source"], line["commit"])
    _, keys = commands.pass_keys(ctx, config, tree, doc)
    root = commands._store_root(ctx)
    found = []
    for artifact, item in (line.get("artifacts") or {}).items():
        record = passes.find(root, name, tree, item["check"], keys.get(item["check"], "")) or {}
        named = (record.get("artifacts") or {}).get(item["file"])
        if named and named.get("sha256") != item["sha256"]:
            found.append(f"{name}:{artifact} is {item['sha256'][:12]} in the Store but {named['sha256'][:12]} in "
                         f"{item['check']}'s pass record")
    return found


# -- the Set gate -------------------------------------------------------------------------------------------------

def _kept(root: Path, digest: str, file_name: str | None) -> Path | None:
    folder = root / "files" / str(digest)
    if file_name and (folder / file_name).is_file():
        return folder / file_name
    found = sorted(p for p in folder.glob("*") if p.is_file() and not p.name.endswith(".part")) \
        if folder.is_dir() else []
    return found[0] if found else None


def _launch_problems(ctx: Context, doc: dict) -> tuple[list[str], list[str]]:
    """The launches of every project with a line, read at that line's commit, each against the set ``doc``: those
    that would load two builds of one mod id (lab.resolve's rule), and members that declare no mod id. A launch that
    inherits another (base = '<project>:<launch>' with members) takes that one's members too."""
    from wb_lab import resolve as launches
    root = commands._store_root(ctx)
    lines, holds, thirds = doc.get("projects") or {}, doc.get("holds") or {}, doc.get("third") or {}
    raws: dict[str, dict] = {}

    def raw(name: str) -> dict:
        if name not in raws:
            line = lines.get(name)
            raws[name] = (_manifest(ctx, name, line["commit"])[1] or {}) if line else {}
        return raws[name]

    warnings: list[str] = []

    def members(name: str, launch: str, depth: int, where: str) -> list[tuple]:
        table = (raw(name).get("launch") or {}).get(launch)
        if not isinstance(table, dict) or not isinstance(table.get("members"), list):
            return []
        found = []
        base = table.get("base")
        match = launches.BASE_NAME.match(base) if isinstance(base, str) else None
        if match and depth < launches.MAX_DEPTH:
            found += [(spec, digest, file, declared, via or base)
                      for spec, digest, file, declared, via in members(match.group(1), match.group(2), depth + 1,
                                                                       where)]
        for spec in table["members"]:
            try:
                member, part, _ = launches.parse_member(spec)
            except launches.LaunchError:
                continue  # lab.resolve refuses it with its own message
            hold = holds.get(f"{member}:{part}") if part else None
            if hold and part in (hold.get("artifacts") or {}):
                item = hold["artifacts"][part]
                found.append((spec, item.get("sha256"), item.get("file"), item.get("mod_id"), None))
            elif part and member in lines:
                item = (lines[member].get("artifacts") or {}).get(part)
                if not item:
                    warnings.append(f"{where}: member {spec} names an artifact {member}'s line does not have")
                    continue
                found.append((spec, item.get("sha256"), item.get("file"), item.get("mod_id"), None))
            elif member in thirds:
                entry = thirds[member]
                files = entry.get("files") or ({"runtime": entry["sha256"]} if entry.get("sha256") else {})
                digest = files.get(part or "runtime")
                if digest:
                    found.append((spec, digest, None if entry.get("files") else entry.get("file"),
                                  entry.get("mod_id"), None))
            else:
                warnings.append(f"{where}: member {spec} is not in the set")
        return found

    problems = []
    for name in sorted(lines):
        for launch, table in sorted((raw(name).get("launch") or {}).items()):
            if launch == "member" or not isinstance(table, dict) or "members" not in table:
                continue
            where = f"{name} launch.{launch}"
            owners: dict[str, list[str]] = {}
            for spec, digest, file_name, declared, via in members(name, launch, 1, where):
                label = spec + (f" (from {via})" if via else "")
                path = _kept(root, digest, file_name) if digest else None
                ids = set(launches.mod_ids(path)) if path else set()
                if declared:
                    ids.add(str(declared))
                if not ids:
                    warnings.append(f"{where}: member {label} declares no mod id (no mcmod.info, no mod_id), so a "
                                    "second build of it would not be seen")
                for mod_id in sorted(ids):
                    owners.setdefault(mod_id, []).append(label)
            refusal = launches.doubled(owners)
            if refusal:
                problems.append(f"{where}: {refusal}")
    return problems, sorted(set(warnings))


def _set_gate(ctx: Context, composed: dict) -> tuple[list[str], list[str]]:
    """The Set gate's refusals for a composed set (a URL a public set may not carry, a launch with two builds of one
    mod id), and its warnings, which also go to the log."""
    problems = setfile.private_urls(composed)
    launch_problems, warnings = _launch_problems(ctx, composed)
    for warning in warnings:
        ctx.log.warning(warning)
    return problems + launch_problems, warnings


def _candidate(text: str) -> str:
    return "candidate-" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


# -- the commands -------------------------------------------------------------------------------------------------

def reach(ctx: Context, base: str, projects: dict, holds: dict | None = None, drop_holds: list | None = None,
          third: dict | None = None, platform: dict | None = None):
    """What a landing of these values would reach, without the lock: the would-be set (exported as a candidate, so
    a check can run against it), the Set gate's verdict on it, and each reached consumer with whether its gate checks
    already have pass records under their new keys."""
    holds, drop_holds, third, platform = holds or {}, list(drop_holds or []), third or {}, platform or {}
    made = _compose(ctx, base, projects or {}, holds, drop_holds, third, platform)
    composed = made["composed"]
    problems, warnings = _set_gate(ctx, composed)
    if problems:
        for problem in problems:
            ctx.reason("set-gate", problem.split(":")[0], problem, "change what it names, then wb land again")
        raise ParamError(f"the Set gate refuses the landing: {'; '.join(problems)}")
    text = setfile.dump(composed)
    candidate = _candidate(text)
    setfile.export(_sets(ctx), candidate, text)
    landed = set(made["lines"])
    reached, notes = _reach(ctx, made["current"], composed, landed, made["logic_moved"])
    for name, item in reached.items():
        _, missing = _unpassed(ctx, name, item["commit"], composed, candidate)
        item["stands"] = not missing
        item["missing"] = missing
    for note in notes:
        ctx.log.info(note)
    ctx.summary(f"would reach {len(reached)} consumer(s)"
                + (": " + ", ".join(f"{n} ({', '.join(i['why'])})" for n, i in sorted(reached.items())) if reached
                   else "") + f"; candidate {candidate}")
    return {"candidate": candidate, "base": made["base"], "reached": reached, "notes": notes, "warnings": warnings}


def land(ctx: Context, landing: str, base: str, projects: dict, deadline: float = 600, caller: str = "",
         holds: dict | None = None, drop_holds: list | None = None, third: dict | None = None,
         platform: dict | None = None, tag: str = ""):
    holds, drop_holds, third, platform = holds or {}, list(drop_holds or []), third or {}, platform or {}
    projects = dict(projects or {})
    if not projects and not holds and not drop_holds and not third and not platform:
        raise ParamError("nothing to land: name a project, a hold, a third entry or a platform value")
    tagged = sorted(n for n in projects if n != SET)
    if tag and (not TAG.match(tag) or ".." in tag or tag.endswith(".lock")):
        raise ParamError(f"--tag {tag!r} is not a tag name git takes")
    if tag and not tagged:
        raise ParamError("--tag names the landed commit of a project, and this landing lands none")
    root = commands._store_root(ctx)
    if landings.read(root, landing):
        raise ParamError(f"landing {landing} exists already")
    made = _compose(ctx, base, projects, holds, drop_holds, third, platform)
    base, current, composed, lines, body = (made[k] for k in ("base", "current", "composed", "lines", "body"))
    change = made["change"]
    # Checked again at the head, under the keys the new set gives: each landed project, then each reached consumer.
    unmet, mismatched = [], []
    for name in sorted(lines):
        _, missing = _unpassed(ctx, name, lines[name]["commit"], composed)
        if missing:
            unmet.append(f"{name} {lines[name]['commit'][:12]} has not passed {', '.join(missing)} against the set it "
                         "lands in")
        mismatched += _artifacts_match(ctx, name, lines[name], composed)
    reached, notes = _reach(ctx, current, composed, set(lines), made["logic_moved"])
    for name, item in sorted(reached.items()):
        _, missing = _unpassed(ctx, name, item["commit"], composed)
        if missing:
            unmet.append(f"{name} {item['commit'][:12]} ({', '.join(item['why'])}) has not passed "
                         f"{', '.join(missing)} against the landed set")
        else:
            body.append(f"reached {name} at {item['commit'][:12]} ({', '.join(item['why'])}): gate passed")
    body += notes
    if mismatched:
        raise ParamError("the Store and the pass records disagree, so nothing lands: " + "; ".join(mismatched))
    if unmet:
        raise ParamError("not every check the landing reaches has passed under its new key: " + "; ".join(unmet)
                         + " (wb land runs them; another landing may have moved what they use while this one waited)")
    problems, warnings = _set_gate(ctx, composed)
    if problems:
        for problem in problems:
            ctx.reason("set-gate", problem.split(":")[0], problem, "change what it names, then wb land again")
        raise ParamError(f"the Set gate refuses the landing: {'; '.join(problems)}")
    if tag:
        body.append(f"tag {tag} on " + ", ".join(f"{n} {lines[n]['commit'][:12]}" for n in tagged if n in lines))
    text = setfile.dump(composed)
    moved = [n for n in sorted(lines) if (setfile.line(current, n) or {}).get("commit") != lines[n]["commit"]]
    parts = [f"{n} {lines[n]['commit'][:8]}" for n in (moved or sorted(lines))]
    parts += [f"hold {n}" for n in sorted(holds)] + [f"drop hold {n}" for n in drop_holds]
    parts += [f"third {n}" for n in sorted(third)] + [f"platform.{k} {platform[k]}" for k in sorted(platform)]
    parts = ([f"set files {change[:8]}"] if change else []) + parts
    subject = "Land " + ", ".join(parts)
    message = subject + "\n\n" + "\n".join(body) + f"\n\nLanding {landing} (job {ctx.job}).\n"
    refs = [f"refs/landed/{landing}"] + [f"refs/heads/{lines[n]['branch']}" for n in sorted(lines)]
    if tag:
        refs.append(f"refs/tags/{tag}")
    for name in list(holds) + drop_holds:
        refs.append(name)  # hold names are published too, in set.json and the message
    findings = _scrub(ctx, {"set.json": text, "message": message, "refs": "\n".join(refs)})
    if findings:
        for finding in findings:
            ctx.reason("scrub", finding.split(":")[0], finding, "change what the scrub names, then wb land again")
        raise ParamError(f"the scrub refused the landing ({len(findings)} finding(s)); nothing was published")
    record = landings.write(root, {
        "id": landing, "state": "prepared", "job": ctx.job, "caller": caller, "base": base,
        "projects": {n: {"commit": lines[n]["commit"], "branch": lines[n]["branch"],
                         "old": (setfile.line(current, n) or {}).get("commit")} for n in sorted(lines)},
        "change": change, "third": sorted(third), "platform": sorted(platform),
        "holds": sorted(holds) + list(drop_holds), "set_json": text, "message": message, "created": time.time(),
        "deadline": time.time() + deadline, "reached": reached, "notes": notes, "warnings": warnings,
        **({"tag": tag} if tag else {})})
    ctx.progress("publishing", msg=f"landing {landing}: waiting for wb land to publish (lock set held)")
    ctx.log.info(f"landing {landing} prepared: {subject}")
    while True:
        record = landings.read(root, landing) or record
        state = record.get("state")
        if state == "landed":
            ctx.summary(f"landed: {subject[5:]} (set {str(record.get('revision', ''))[:12]})"
                        + (f"; reached {', '.join(sorted(reached))}" if reached else ""))
            return {"landing": landing, "state": state, "revision": record.get("revision"), "lines": lines,
                    "reached": reached}
        if state == "abandoned":
            ctx.finding(f"landing {landing} was abandoned before the Set moved: {record.get('why') or 'no reason'}")
            return {"landing": landing, "state": state}
        if time.time() > record.get("deadline", 0):
            if state == "set-pushed":
                ctx.finding(f"landing {landing} landed, branch behind: the next wb land finishes it")
                return {"landing": landing, "state": state, "revision": record.get("revision")}
            landings.move(root, landing, "abandoned", why="the publish was not confirmed before the deadline")
            ctx.finding(f"landing {landing} was not published before its deadline; nothing moved, and the next "
                        "wb land removes any landing refs it left")
            return {"landing": landing, "state": "abandoned"}
        if ctx.cancelled.wait(1.0):
            if state in ("prepared", "refs-pushed"):
                landings.move(root, landing, "abandoned", why="the landing job was cancelled")
            raise EnvironmentProblem(f"landing {landing} cancelled at {state}")
