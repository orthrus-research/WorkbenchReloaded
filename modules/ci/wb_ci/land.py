"""set.land: one landing, under lock ``set``.

The CLI (``wb land``) has fetched, checked that each commit descends from its line, run each commit's gate checks
with ci.run, and exported the Set at the head it validated against. This job, holding lock ``set``:

1. checks again at that head: each commit descends from its line, and its tree has a pass for every gate check;
2. finds each artifact the project's manifest names in the Store, by its check and glob: exactly one file each;
3. composes the new set.json and the commit body (names, commits, checks, artifacts; no paths) and scrubs them with
   every ref name the publish will push;
4. writes the landing record (state ``prepared``) and waits, holding the lock, while the CLI publishes with the
   caller's own git and moves the record. It settles when the record says landed or abandoned, or at its deadline.

It runs no check itself and submits no child job: a missing pass is refused with the command that makes it."""

from __future__ import annotations

import fnmatch
import os
import re
import subprocess
import time
from pathlib import Path

from wb_core import artifacts as store
from wb_core import landing as landings
from wb_core import setfile
from wb_core.module import Context, EnvironmentProblem, ParamError

from . import commands
from .commands import engine


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
    tree = commands._git(ctx, ["--git-dir", str(commands._git_dir(ctx, source)), "rev-parse", f"{sha}^{{tree}}"])
    passed = engine.passed_jobs(records, config.names) if records.is_dir() else {}
    done = set().union(*[jobs for key, jobs in passed.items() if key and key[0] == tree] or [set()])
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
    config = commands._config_at(ctx, entry, commands._records(ctx, SET), sha)
    if config is None:
        raise ParamError(f"the Set change {sha[:12]} has no project.toml, so it has no gate to pass")
    tree = commands._git(ctx, ["--git-dir", str(commands._git_dir(ctx, source)), "rev-parse", f"{sha}^{{tree}}"])
    records = commands._records(ctx, SET)
    passed = engine.passed_jobs(records, config.names) if records.is_dir() else {}
    done = set().union(*[jobs for key, jobs in passed.items() if key and key[0] == tree] or [set()])
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


def land(ctx: Context, landing: str, base: str, projects: dict, deadline: float = 600, caller: str = "",
         holds: dict | None = None, drop_holds: list | None = None, third: dict | None = None):
    holds, drop_holds, third = holds or {}, list(drop_holds or []), third or {}
    projects = dict(projects or {})
    change_wanted = projects.pop(SET, None)
    if not projects and not holds and not drop_holds and not change_wanted and not third:
        raise ParamError("nothing to land: name a project, a hold or a hold to drop")
    sets = _sets(ctx)
    root = commands._store_root(ctx)
    head = setfile.read_head(sets)
    if head and head != base:
        # Another landing moved the Set while this one waited on the lock: compose from the head it left.
        ctx.log.info(f"the Set moved from {base[:12]} to {head[:12]} while this landing waited; composing from it")
        base = head
    try:
        current = setfile.read_export(sets, base)
    except setfile.SetError as error:
        raise ParamError(f"{error}; wb land exports the head before it submits") from None
    if landings.read(root, landing):
        raise ParamError(f"landing {landing} exists already")
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
    if change and logic != (current.get("platform") or {}).get("logic"):
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
    text = setfile.dump(composed)
    moved = [n for n in sorted(lines) if (setfile.line(current, n) or {}).get("commit") != lines[n]["commit"]]
    parts = [f"{n} {lines[n]['commit'][:8]}" for n in (moved or sorted(lines))]
    parts += [f"hold {n}" for n in sorted(holds)] + [f"drop hold {n}" for n in drop_holds]
    parts += [f"third {n}" for n in sorted(third)]
    parts = ([f"set files {change[:8]}"] if change else []) + parts
    subject = "Land " + ", ".join(parts)
    message = subject + "\n\n" + "\n".join(body) + f"\n\nLanding {landing} (job {ctx.job}).\n"
    refs = [f"refs/landed/{landing}"] + [f"refs/heads/{lines[n]['branch']}" for n in sorted(lines)]
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
        "change": change, "third": sorted(third), "holds": sorted(holds) + list(drop_holds), "set_json": text, "message": message, "created": time.time(),
        "deadline": time.time() + deadline})
    ctx.progress("publishing", msg=f"landing {landing}: waiting for wb land to publish (lock set held)")
    ctx.log.info(f"landing {landing} prepared: {subject}")
    while True:
        record = landings.read(root, landing) or record
        state = record.get("state")
        if state == "landed":
            ctx.summary(f"landed: {subject[5:]} (set {str(record.get('revision', ''))[:12]})")
            return {"landing": landing, "state": state, "revision": record.get("revision"), "lines": lines}
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
