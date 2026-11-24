"""release: publish built mod artifacts to GitHub releases, Modrinth, CurseForge, Maven repositories, Maven Central
(``central``, wb_release/central.py) and our own Maven repository in a bucket (``mirror``, wb_release/mirror.py).

``release.check`` holds files to the publication allow-list (``wb_release/rules.py``) with no network: one finding
per violation.

``release.publish`` spends: like money, every publish waits for an explicit yes, and no setting, budget or schedule
can give it. Its quote is the publication plan at zero cost: the channel, where it publishes and the project, the
version and how it is released (type, visibility, name, tag, game versions, loaders, relations), every file with its
SHA-256, size and role, the notes' digest, and the rules the files passed. A plan is only quoted for files that pass
the channel's rules. When the approved job runs, it copies every file and the notes into its work folder, hashes the
copies and builds the plan again from them; if anything differs from the approved plan (a rebuilt file, new notes, a
changed channel or rule), it asks again with a new quote instead of publishing. The copies are what it uploads, so
exactly the approved bytes go out. It then asks the channel what it already holds, and never publishes over a version
there. Every publish, a stopped one too, leaves a receipt (the output ``receipt``, a file
``receipt-<channel>-<version>.json``): the channel, the ids and URLs the channel returned, every file's digests and
whether the channel's own digests agree.

With ``set=<project>@<revision>`` instead of files, a publish takes exactly the bytes the set names for that
project's line: each artifact from the Store by its SHA-256, published under the name the line gives it, and refused
when the Store's copy no longer hashes to it. The plan records the set revision and the line's commit, and a github
release's tag is made at that commit unless ``commit`` is given: the release is the last stage of the pipeline, not
a rebuild.

``release.status`` reads what a channel holds for a version, compared with local files when given.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from wb_core.module import Context, EnvironmentProblem, ParamError, canonical

from . import channels
from . import rules as rulebook
from .transport import SAFE_NAME, ChannelError, Http

NOTES_LIMIT = 1 << 20
SETTING_PARAMS = ("release_type", "visibility", "name", "tag", "commit", "game_versions", "loaders", "dependencies")


@dataclass
class Source:
    """A file to publish: where it is read from (the job's copy, when it runs), and its digests."""

    path: Path
    host_path: str
    name: str        # the name it is published under
    local_name: str
    bytes: int
    sha256: str
    sha1: str
    sha512: str
    md5: str

    @classmethod
    def measure(cls, path: Path, host_path: str, copy_to: Path | None = None) -> "Source":
        digests = {algo: hashlib.new(algo, usedforsecurity=False) for algo in ("sha256", "sha1", "sha512", "md5")}
        size = 0
        with open(path, "rb") as source, (open(copy_to, "xb") if copy_to else contextlib.nullcontext()) as copy:
            for block in iter(lambda: source.read(1 << 20), b""):
                size += len(block)
                for digest in digests.values():
                    digest.update(block)
                if copy is not None:
                    copy.write(block)
        return cls(path=copy_to or path, host_path=host_path, name=path.name, local_name=path.name, bytes=size,
                   **{algo: digest.hexdigest() for algo, digest in digests.items()})

    def item(self) -> rulebook.Item:
        return rulebook.Item(self.path, self.name, self.sha256, self.bytes)


@dataclass
class Notes:
    host_path: str
    data: bytes

    @property
    def text(self) -> str:
        return self.data.decode("utf-8-sig")

    def plan(self) -> dict:
        first = next((line.strip() for line in self.text.splitlines() if line.strip()), "")
        return {"path": self.host_path, "sha256": hashlib.sha256(self.data).hexdigest(), "bytes": len(self.data),
                "first_line": first[:120]}


# ---------------------------------------------------------------------------------------------
# release.check

def check(ctx: Context, files: list, version: str | None = None, channel: str | None = None,
          rules: str | None = None, java: int | None = None, production: list | None = None,
          extras: list | None = None, dependencies: list | None = None):
    chan = channels.load(ctx.config, channel) if channel else None
    applied = rulebook.load(ctx.config, rules if rules is not None else (chan.rules if chan else None),
                            given=rules is not None,
                            overrides={"java": java, "production": production, "extras": extras,
                                       "dependencies": dependencies})
    sources = _sources(ctx, files)
    violations = []
    if chan is not None and chan.kind in channels.MAVEN_LAYOUT:
        if not version:
            raise ParamError(f"checking files for a {chan.kind} channel needs their version: the layout names them "
                             "by it")
        violations += channels.maven_names(chan, version, sources, need_pom=chan.kind != "central")
        if chan.kind == "central":
            from . import central
            found = central.problems(chan, version, sources, applied.licence, applied.name)
            violations += found
            if not found and not violations:
                sources = _complete(ctx, chan, version, sources, applied)
    report = rulebook.inspect([s.item() for s in sources], applied, version)
    violations += report.violations
    for violation in violations:
        ctx.finding(violation)
    named = f" {applied.name!r}" if applied.name else " (built in)"
    if violations:
        ctx.summary(f"{len(violations)} publication rule violation(s) in {len(sources)} file(s) under the rules"
                    f"{named}; first: {violations[0]}")
    else:
        ctx.summary(f"{len(sources)} file(s) pass the publication rules{named}: production JAR {report.primary}, "
                    + (f"Java {applied.java}" if applied.java else "no Java target declared, so classes unchecked")
                    + (f", version {report.version}" if report.version else ""))
    return {"violations": violations, "files": report.files, "primary": report.primary, "version": report.version,
            "rules": applied.as_dict()}


# ---------------------------------------------------------------------------------------------
# release.publish: the quote is the plan

def quote_publish(ctx: Context, channel: str, version: str, files: list | None = None, notes: str | None = None,
                  release_type=None, visibility=None, name=None, tag=None, commit=None, game_versions=None,
                  loaders=None, dependencies=None, set=None, artifacts=None):
    chan = channels.load(ctx.config, channel)
    _mounted(ctx, chan)
    given = {"release_type": release_type, "visibility": visibility, "name": name, "tag": tag, "commit": commit,
             "game_versions": game_versions, "loaders": loaders, "dependencies": dependencies}
    sources, origin = _chosen(ctx, files, set, artifacts)
    sources = _complete(ctx, chan, version, sources)
    plan = _plan(ctx, chan, version, sources, _notes(ctx, notes), _at_line(chan, given, origin), origin)
    return _quote(plan)


def publish(ctx: Context, channel: str, version: str, files: list | None = None, notes: str | None = None,
            release_type=None, visibility=None, name=None, tag=None, commit=None, game_versions=None,
            loaders=None, dependencies=None, set=None, artifacts=None):
    approved = ctx.quote or {}
    if not isinstance(approved.get("plan"), dict):
        raise EnvironmentProblem("there is no approved plan; a publish runs only after a yes on its quote")
    chan = channels.load(ctx.config, channel)
    given = {"release_type": release_type, "visibility": visibility, "name": name, "tag": tag, "commit": commit,
             "game_versions": game_versions, "loaders": loaders, "dependencies": dependencies}
    ctx.progress("stage", msg="copying the files into the job and hashing the copies")
    sources, origin = _chosen(ctx, files, set, artifacts, stage=True)
    sources = _complete(ctx, chan, version, sources)
    note = _notes(ctx, notes)
    plan = _plan(ctx, chan, version, sources, note, _at_line(chan, given, origin), origin)
    changes = _changes(approved["plan"], plan)
    if changes:
        ctx.log.warning("the plan differs from the approved one: " + "; ".join(changes))
        ctx.requote(_quote(plan), "; ".join(changes))
    _mounted(ctx, chan)
    token = ctx.secret(chan.secret)
    second = channels.SECOND_SECRET.get(chan.kind)
    status_token = ctx.secret(chan.settings[second]) if second and (chan.kind != "curseforge"
                                                                     or chan.settings.get("status_url")) else None
    client = channels.client(chan, _http(ctx), token, status_token)
    receipt = channels.Receipt(channel=chan.name, kind=chan.kind, endpoint=chan.base_url, project=chan.project,
                               version=version, job=ctx.job, quote=approved.get("id"),
                               plan_sha256=approved.get("plan_sha256"),
                               notes_sha256=plan["notes"]["sha256"] if plan.get("notes") else None)
    if chan.kind == "curseforge" and not chan.settings.get("status_url"):
        ctx.log.warning(f"{chan.name}: no status_url, so nothing was asked before uploading")
    else:
        ctx.progress("precheck", msg=f"asking {chan.name} what it holds for {version}")
        try:
            held = client.holdings(version, plan, [f["name"] for f in plan["files"]])
        except ChannelError as error:
            raise EnvironmentProblem(f"{chan.kind} channel {chan.name}: {error}; nothing was uploaded") from None
        if held.conflicts:
            for conflict in held.conflicts:
                ctx.finding(conflict)
            ctx.summary(f"{chan.name} already holds {version}, so nothing was published: {held.conflicts[0]}")
            return {"published": False, "held": held.as_dict()}
    try:
        client.publish(ctx, plan, sources, note.text if note else "", receipt)
        receipt.complete = True
    except BaseException as error:
        encoded = base64.b64encode(token.encode("utf-8")).decode("ascii") if token else None  # as a Bearer sends it
        receipt.error = _redact(str(error) or type(error).__name__, token, status_token, encoded)
        _receipt(ctx, receipt)
        if isinstance(error, ChannelError):
            raise EnvironmentProblem(f"publishing to {chan.name} stopped: {error}; {_progress(receipt, plan)} "
                                     "(see its receipt)") from None
        raise
    _receipt(ctx, receipt)
    differing = [entry for entry in receipt.files if entry["verified"] is False]
    for entry in differing:
        ctx.finding(f"{entry['name']} was uploaded, but {chan.name} reports other bytes for it "
                    f"(by {entry['verified_by']}): {entry.get('remote')}")
    for text in receipt.release.get("mismatched") or []:
        ctx.finding(f"{chan.name} published {version}, but {text}")
    where = receipt.release.get("url") or receipt.release.get("id")
    done = "uploaded" if receipt.release.get("next") else "published"
    ctx.summary(f"{done} {version} to {chan.kind} channel {chan.name} ({chan.project}): {len(receipt.files)} "
                f"file(s){', ' + str(where) if where else ''}"
                + (f"; the channel reports other bytes for {', '.join(e['name'] for e in differing)}"
                   if differing else "")
                + (f"; {receipt.release['next']}" if receipt.release.get("next") else ""))
    return {"published": True, "receipt": receipt.as_dict()}


def _plan(ctx: Context, chan: channels.Channel, version: str, sources: list[Source], note: Notes | None,
          given: dict, origin: dict | None = None) -> dict:
    """The publication plan: everything the yes covers, from the files as read now."""
    settings = channels.resolve(chan, given, version)
    if chan.kind in channels.MAVEN_LAYOUT:
        problems = channels.maven_names(chan, version, sources)
        if problems:
            raise ParamError("; ".join(problems))
    applied = rulebook.load(ctx.config, chan.rules, given=False)
    report = rulebook.inspect([s.item() for s in sources], applied, version)
    if report.violations:
        shown = "; ".join(report.violations[:4])
        more = f"; and {len(report.violations) - 4} more" if len(report.violations) > 4 else ""
        raise ParamError(f"{len(report.violations)} publication rule violation(s), so nothing was quoted "
                         f"(release.check channel={chan.name} lists them): {shown}{more}")
    plan = {"channel": chan.name, "kind": chan.kind, "endpoint": chan.base_url, "project": chan.project,
            "version": version, **settings,
            "files": [{"name": s.name, "path": s.host_path, "sha256": s.sha256, "bytes": s.bytes,
                       "role": report.roles[s.name], "primary": s.name == report.primary} for s in sources],
            "notes": note.plan() if note else None,
            "rules": {"name": applied.name, "sha256": applied.digest(), "java": applied.java},
            "precheck": channels.precheck(chan)}
    if origin is not None:
        plan["set"] = origin
    if chan.kind == "github":
        plan["upload_endpoint"] = chan.settings.get("upload_url") or chan.base_url
    elif chan.kind == "curseforge":
        plan["changelog_type"] = chan.settings["changelog_type"]
    if chan.kind in channels.MAVEN_LAYOUT:
        group, artifact = chan.project.split(":", 1)
        plan["maven"] = {"folder": f"{group.replace('.', '/')}/{artifact}/{version}",
                         "checksums": chan.settings["checksums"],
                         "metadata": chan.settings.get("metadata", chan.kind == "mirror")}
    if chan.kind == "central":
        plan["central"] = {"publishing_type": chan.settings["publishing_type"],
                           "signing_key": chan.settings["signing"]["key"], "javadoc": chan.settings["javadoc"],
                           "signatures": "an .asc for every file, made when the publish runs"}
    elif chan.kind == "mirror":
        plan["mirror"] = {"bucket": chan.settings["bucket"], "public_url": chan.settings["public_url"],
                          "group": chan.settings["group"], "region": chan.settings["region"]}
    return json.loads(json.dumps(plan))  # the plan as it reads back from the approval


def _complete(ctx: Context, chan: channels.Channel, version: str, sources: list[Source],
              applied: rulebook.Rules | None = None) -> list[Source]:
    """The files a channel publishes beyond those given: for central, the POM Workbench writes and a placeholder
    javadoc JAR when the channel asks for one."""
    if chan.kind != "central":
        return sources
    from . import central
    problems = channels.maven_names(chan, version, sources, need_pom=False)
    applied = applied or rulebook.load(ctx.config, chan.rules, given=False)
    problems += central.problems(chan, version, sources, applied.licence, applied.name)
    if problems:
        raise ParamError("; ".join(problems))
    return central.complete(chan, version, sources, applied.licence, ctx.work / "written",
                            lambda path, label: Source.measure(path, label))


def _quote(plan: dict) -> dict:
    digest = hashlib.sha256(canonical(plan).encode("utf-8")).hexdigest()
    files = plan["files"]
    listing = ", ".join(f"{f['name']} ({_size(f['bytes'])}, sha256 {f['sha256'][:12]})" for f in files[:4])
    listing += f" and {len(files) - 4} more" if len(files) > 4 else ""
    how = " ".join(x for x in (plan.get("visibility"), plan.get("release_type")) if x)
    where = f"{plan['kind']} channel {plan['channel']} ({plan['project']} at {plan['endpoint']})"
    notes = plan.get("notes")
    line = plan.get("set")
    if line:
        where += (f", the bytes set {line['revision'][:12]} names for {line['project']} at "
                  f"{line['commit'][:12]}")
    central = plan.get("central")
    if central:
        where += (f", {central['publishing_type']}, signed by {central['signing_key']}"
                  + (", with a placeholder javadoc JAR" if central["javadoc"] == "placeholder" else ""))
    if plan.get("mirror"):
        where += f", read back from {plan['mirror']['public_url']}"
    question = (f"Publish version {plan['version']} to {where} as {how}, "
                f"{'with notes ' + notes['sha256'][:12] if notes else 'without notes'}: {listing}?")
    return {"summary": f"publish {plan['version']} to {plan['kind']} channel {plan['channel']}: {len(files)} file(s), "
                       f"{how}",
            "max_cost": 0.0, "currency": "USD", "plan": plan, "plan_sha256": digest,
            "prompt": {"question": question, "options": [
                {"label": "Approve", "description": f"Publish exactly these files (plan {digest[:12]}); if anything "
                                                    "differs when it runs, you are asked again."},
                {"label": "Decline", "description": "Cancel the job; nothing is published."}]}}


def _changes(before: dict, after: dict) -> list[str]:
    """What differs between the approved plan and the plan now, in words."""
    out = []
    old = {f["name"]: f for f in before.get("files") or []}
    new = {f["name"]: f for f in after.get("files") or []}
    for name in dict.fromkeys([*old, *new]):
        a, b = old.get(name), new.get(name)
        if a is None:
            out.append(f"{name} was added")
        elif b is None:
            out.append(f"{name} was removed")
        elif a.get("sha256") != b.get("sha256"):
            out.append(f"{name} changed: sha256 {str(a.get('sha256'))[:12]} -> {str(b.get('sha256'))[:12]}, "
                       f"{a.get('bytes')} -> {b.get('bytes')} bytes")
        elif a != b:
            out.append(f"{name}: " + ", ".join(f"{k} {a.get(k)} -> {b.get(k)}" for k in sorted({*a, *b})
                                               if a.get(k) != b.get(k)))
    if list(old) != list(new) and set(old) == set(new):
        out.append("the files are in another order")
    for key in sorted({*before, *after} - {"files"}):
        if before.get(key) == after.get(key):
            continue
        if key == "notes":
            out.append("the notes changed" if before.get(key) and after.get(key) else
                       "notes were added" if after.get(key) else "the notes were removed")
        elif key == "rules":
            out.append(f"the rules {after[key].get('name') or '(built in)'} changed")
        else:
            out.append(f"{key}: {before.get(key)!r} -> {after.get(key)!r}")
    return out


# ---------------------------------------------------------------------------------------------
# release.status

def status(ctx: Context, channel: str, version: str, files: list | None = None, tag: str | None = None):
    chan = channels.load(ctx.config, channel)
    settings = channels.resolve(chan, {"tag": tag}, version, strict=False)
    sources = _sources(ctx, files) if files else []
    if chan.kind in channels.MAVEN_LAYOUT and sources:
        problems = channels.maven_names(chan, version, sources, need_pom=False)
        if problems:
            raise ParamError("; ".join(problems))
    token = _optional_secret(ctx, chan.secret)
    second = channels.SECOND_SECRET.get(chan.kind)
    status_token = _optional_secret(ctx, chan.settings.get(second) if second else None)
    client = channels.client(chan, _http(ctx), token, status_token)
    ctx.progress("ask", msg=f"asking {chan.name} what it holds for {version}")
    try:
        held = client.holdings(version, settings, [s.name for s in sources])
    except ChannelError as error:
        raise EnvironmentProblem(f"{chan.kind} channel {chan.name}: {error}") from None
    local = {s.name: s for s in sources}
    same = 0
    for entry in held.files:
        source = local.get(entry["name"])
        if source is None:
            continue
        verdict, by = channels.compare(source, entry)
        entry["local"] = {"path": source.host_path, "sha256": source.sha256, "bytes": source.bytes, "same": verdict,
                          "by": by}
        same += verdict is True
        if verdict is False:
            ctx.finding(f"{entry['name']}: {chan.name} holds other bytes than {source.host_path} (by {by})")
    absent = [name for name in local if name not in {e["name"] for e in held.files}]
    if not held.exists:
        ctx.summary(f"{chan.kind} channel {chan.name} ({chan.project}) holds nothing for {version}")
    elif not ctx.findings:
        ctx.summary(f"{chan.kind} channel {chan.name} ({chan.project}) holds {version}: {len(held.files)} file(s)"
                    + (f", {same} of {len(local)} local file(s) the same" if local else "")
                    + (f"; not there: {', '.join(absent)}" if absent else ""))
    return {"exists": held.exists, "channel": chan.name, "kind": chan.kind, "project": chan.project,
            "version": version, "release": held.release, "files": held.files, "absent": absent}


# ---------------------------------------------------------------------------------------------
# shared

def _chosen(ctx: Context, files: list | None, spec: str | None, artifacts: list | None, *,
            stage: bool = False) -> tuple[list[Source], dict | None]:
    """The files a publish sends, and the set line they come from: the files given, or with ``set`` the Store's
    bytes the line names. Exactly one of the two."""
    if spec and files:
        raise ParamError("give files or set, not both: with set the files are the ones the set names")
    if spec:
        return _set_sources(ctx, spec, artifacts, stage=stage)
    if artifacts:
        raise ParamError("artifacts picks from a set line: give set=<project>@<set revision> with it")
    return _sources(ctx, files or [], stage=stage), None


def _set_sources(ctx: Context, spec: str, artifacts: list | None, *,
                 stage: bool = False) -> tuple[list[Source], dict]:
    """The Store's bytes a set line names, checked against the line's SHA-256 and named as the line names them."""
    from wb_core import setfile
    project, _, wanted = spec.partition("@")
    configured = str(ctx.config.get("store") or "")
    root = Path(configured)
    if not configured or not root.is_dir():
        where = f" at {ctx.host_path(configured)}" if configured else ""
        raise EnvironmentProblem(f"no Store to publish from{where}: [paths] out names it (<out>/store)")
    sets = root.parent / "sets"
    revision = _revision(sets, wanted)
    try:
        doc = setfile.read_export(sets, revision)
    except setfile.SetError as error:
        raise EnvironmentProblem(f"{error}: wb work sync exports it") from None
    line = (doc.get("projects") or {}).get(project)
    if not line:
        raise ParamError(f"set {revision[:12]} has no line for {project}; its lines: "
                         f"{', '.join(sorted(doc.get('projects') or {})) or 'none'}")
    named = line.get("artifacts") or {}
    keys = list(named)
    order = list(artifacts) if artifacts else sorted(keys, key=lambda key: (key != "mod", keys.index(key)))
    missing = [key for key in order if key not in named]
    if missing:
        raise ParamError(f"{project}'s line in set {revision[:12]} has no artifact {', '.join(missing)}; it has "
                         f"{', '.join(keys) or 'none'}")
    folder = None
    if stage:
        folder = ctx.work / "staged"
        shutil.rmtree(folder, ignore_errors=True)
        folder.mkdir(parents=True)
    out = []
    for index, key in enumerate(order):
        entry = named[key]
        digest, name = str(entry.get("sha256") or ""), str(entry.get("file") or "")
        if not SAFE_NAME.match(name):
            raise ParamError(f"{project}:{key}: the line names the file {name!r}, which is not a name a channel takes")
        kept = _kept(root, digest, name, f"{project}:{key}")
        copy = folder / f"{index:02d}-{name}" if folder else None
        source = Source.measure(kept, ctx.host_path(str(kept)), copy)
        if source.sha256 != digest:
            raise EnvironmentProblem(f"{project}:{key}: the copy of {name} hashes to {source.sha256[:12]}, and set "
                                     f"{revision[:12]} names {digest[:12]}; nothing was published")
        source.name = source.local_name = name
        out.append(source)
    origin = {"revision": revision, "project": project, "commit": str(line.get("commit") or ""), "artifacts": order}
    return out, origin


def _kept(root: Path, digest: str, name: str, what: str) -> Path:
    """The Store's file for ``digest``, re-hashed: under the line's name, else the one file the Store keeps for it."""
    from wb_core import artifacts as store_files
    folder = root / "files" / digest
    try:
        if (folder / name).is_file():
            return store_files.path_of(root, digest, name)
        names = sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []
        if len(names) != 1:
            raise store_files.StoreError(f"the Store has no file with SHA-256 {digest}")
        return store_files.path_of(root, digest, names[0])
    except store_files.StoreError as error:
        raise EnvironmentProblem(f"{what}: {error}; nothing was published") from None


def _revision(sets: Path, wanted: str) -> str:
    """A set revision as its export folder names it: the head, a full revision, or a prefix of exactly one."""
    from wb_core import setfile
    if wanted == "head":
        head = setfile.read_head(sets)
        if not head:
            raise EnvironmentProblem(f"no set head at {sets}: wb work sync exports one")
        return head
    found = sorted(p.name for p in sets.iterdir() if p.is_dir() and p.name.startswith(wanted)) if sets.is_dir() else []
    if len(found) != 1:
        what = "no export" if not found else f"{len(found)} exports"
        raise ParamError(f"set revision {wanted}: {what} under {sets} match it"
                         + ("; wb work sync exports the head" if not found else ""))
    return found[0]


def _at_line(chan: channels.Channel, given: dict, origin: dict | None) -> dict:
    """With a set line, a github tag is made at the commit the line names, unless a commit is given; the other
    kinds take no commit."""
    if origin is None or given.get("commit") or "commit" not in channels.SETTINGS[chan.kind]:
        return given
    return {**given, "commit": origin["commit"]}


def _sources(ctx: Context, files: list, *, stage: bool = False) -> list[Source]:
    if not files:
        raise ParamError("files is empty: name the files to publish")
    folder = None
    if stage:
        folder = ctx.work / "staged"
        shutil.rmtree(folder, ignore_errors=True)
        folder.mkdir(parents=True)
    out = []
    for index, value in enumerate(files):
        path = Path(value)
        if not path.is_file():
            raise ParamError(f"{ctx.host_path(value)} is not a file")
        if not SAFE_NAME.match(path.name):
            raise ParamError(f"{path.name}: a published file's name has letters, digits and . _ + - only")
        copy = folder / f"{index:02d}-{path.name}" if folder else None
        out.append(Source.measure(path, ctx.host_path(value), copy))
    names = [s.name for s in out]
    twice = [name for name in dict.fromkeys(names) if names.count(name) > 1]
    if twice:
        raise ParamError(f"{', '.join(twice)} given twice")
    return out


def _notes(ctx: Context, notes: str | None) -> Notes | None:
    if not notes:
        return None
    path = Path(notes)
    if not path.is_file():
        raise ParamError(f"notes {ctx.host_path(notes)} is not a file")
    if path.stat().st_size > NOTES_LIMIT:
        raise ParamError(f"notes {ctx.host_path(notes)} is over {NOTES_LIMIT >> 20} MiB")
    data = path.read_bytes()
    try:
        data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ParamError(f"notes {ctx.host_path(notes)} is not UTF-8 text") from None
    return Notes(ctx.host_path(notes), data)


def _mounted(ctx: Context, chan: channels.Channel):
    """The channel's secrets are mounted (they are not read until the publish runs)."""
    needed = [("secret", chan.secret)]
    if chan.kind == "curseforge" and chan.settings.get("status_url"):
        needed.append(("status_secret", chan.settings.get("status_secret")))
    elif chan.kind == "mirror":
        needed.append(("key_id_secret", chan.settings["key_id_secret"]))
    elif chan.kind == "central":
        from . import central
        central.gpg_program()
        signing = chan.settings["signing"]
        needed.append(("signing.secret", signing["secret"]))
        if signing.get("keyring"):
            needed.append(("signing.keyring", signing["keyring"]))
    for key, name in needed:
        if not name:
            raise EnvironmentProblem(f"channel {chan.name} names no {key}: set {chan.where}.{key}")
        path = (ctx.spec.get("secrets") or {}).get(name)
        if not path or not Path(path).is_file():
            raise EnvironmentProblem(f"channel {chan.name}'s {key} {name} is not mounted: add [secrets.{name}] "
                                     "host = '<file>' to the config and restart")


def _optional_secret(ctx: Context, name: str | None) -> str | None:
    """A secret for reading, when it is mounted; reads of a public channel need none."""
    path = (ctx.spec.get("secrets") or {}).get(name) if name else None
    return ctx.secret(name) if path and Path(path).is_file() else None


def _http(ctx: Context) -> Http:
    return Http(str(ctx.config.get("user_agent") or "workbench-release"), float(ctx.config.get("timeout") or 120),
                log=lambda line: ctx.log.debug(line))


def _receipt(ctx: Context, receipt: channels.Receipt):
    receipt.finished = channels._now()
    path = ctx.out / f"receipt-{re.sub(r'[^A-Za-z0-9._-]', '_', receipt.channel)}-{receipt.version}.json"
    path.write_text(json.dumps(receipt.as_dict(), indent=2) + "\n", encoding="utf-8")
    ctx.output("receipt", path, "application/json")


def _redact(text: str, *secrets: str | None) -> str:
    """The receipt is a file of the job's, which the core's log redaction does not reach."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted]")
    return text


def _progress(receipt: channels.Receipt, plan: dict) -> str:
    done = f"{len(receipt.files)} of {len(plan['files'])} file(s) uploaded"
    if receipt.release.get("id") is not None:
        return f"{receipt.release.get('url') or receipt.release['id']} exists on {plan['kind']}, with {done}"
    return done


def _size(count: int) -> str:
    for unit, scale in (("MiB", 1 << 20), ("KiB", 1 << 10)):
        if count >= scale:
            return f"{count / scale:.1f} {unit}"
    return f"{count} bytes"
