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
        pattern = Path(spec["file"]).name
        matched = [f for f, item in kept["artifacts"].items()
                   if item.get("job") == spec["check"] and fnmatch.fnmatch(f, pattern)]
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


def land(ctx: Context, landing: str, base: str, projects: dict, deadline: float = 600, caller: str = ""):
    if not projects:
        raise ParamError("nothing to land: name at least one project")
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
    for name in sorted(projects):
        ctx.progress("validate", msg=f"{name} at {str(projects[name].get('commit'))[:12]}")
        lines[name], line_text = _member(ctx, name, projects[name], current)
        body.append(line_text)
    composed = setfile.with_lines(current, lines)
    text = setfile.dump(composed)
    moved = [n for n in sorted(lines) if (setfile.line(current, n) or {}).get("commit") != lines[n]["commit"]]
    subject = "Land " + ", ".join(f"{n} {lines[n]['commit'][:8]}" for n in (moved or sorted(lines)))
    message = subject + "\n\n" + "\n".join(body) + f"\n\nLanding {landing} (job {ctx.job}).\n"
    refs = [f"refs/landed/{landing}"] + [f"refs/heads/{lines[n]['branch']}" for n in sorted(lines)]
    findings = _scrub(ctx, {"set.json": text, "message": message, "refs": "\n".join(refs)})
    if findings:
        for finding in findings:
            ctx.reason("scrub", finding.split(":")[0], finding, "change what the scrub names, then wb land again")
        raise ParamError(f"the scrub refused the landing ({len(findings)} finding(s)); nothing was published")
    record = landings.write(root, {
        "id": landing, "state": "prepared", "job": ctx.job, "caller": caller, "base": base,
        "projects": {n: {"commit": lines[n]["commit"], "branch": lines[n]["branch"],
                         "old": (setfile.line(current, n) or {}).get("commit")} for n in sorted(lines)},
        "set_json": text, "message": message, "created": time.time(), "deadline": time.time() + deadline})
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
