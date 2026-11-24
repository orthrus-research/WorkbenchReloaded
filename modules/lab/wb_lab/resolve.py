"""lab.resolve: a launch resolved once into launch.json, the one file every later step reads.

A launchable project's project.toml, read at the commit in use, defines its launches:

    [launch.default]
    members = ["riven:mod@both", "cubature:mod@both", "quadrature:mod@client", "modularui@both"]
    runtime = "cleanroom"
    params = { seed = 8675309 }
    jvm = { client = ["-Dexample=1"] }
    place = { "helper@both" = "+Helper-1.0.jar" }

``<project>:<artifact>@<side>`` names a project's artifact: the product's own from the Store by the tree being
launched, another project's from its line in the set, or from a hold of that name, which wins as it does for builds.
``place`` names the file a member is placed under in an instance, where its name matters (a coremod that must sort
first); otherwise it keeps the Store's name.
``<third>[:<kind>]@<side>`` names a third entry's file (kind ``runtime`` unless given). A member's own project.toml may
add ``[launch.member]`` (``jvm.client``, ``jvm.server``, ``collect``, ``failures``), read at the commit its line names,
so a member's switch needs no landing in the product.

launch.json names every member by SHA-256 with its Store path and the mod ids its mcmod.info declares; a launch that
would load two builds of one mod id is refused before anything is written, as is a member the set or the Store lacks.
The code names no project."""

from __future__ import annotations

import fnmatch
import json
import re
import subprocess
import tomllib
import zipfile
from pathlib import Path

SCHEMA = 1
SIDES = ("both", "client", "server")
MEMBER = re.compile(r"^([a-z0-9][a-z0-9._-]*)(?::([a-z0-9][a-z0-9._-]*))?@(both|client|server)$")
LAUNCH_KEYS = {"members", "runtime", "params", "jvm", "collect", "failures", "place"}
PLACE = re.compile(r'^[^/\\:*?"<>|]+\.jar$')  # a bare file name: no folder, nothing Windows refuses
MEMBER_KEYS = {"jvm", "collect", "failures"}
RUNTIMES = ("cleanroom",)


class LaunchError(Exception):
    """A launch that cannot be resolved; the message names what is missing and, where there is one, the fix."""


def parse_member(text: str) -> tuple[str, str | None, str]:
    """(name, artifact or kind, side) of a member spec."""
    match = MEMBER.match(str(text))
    if not match:
        raise LaunchError(f"member {text!r} is not <project>:<artifact>@<side> or <third>[:<kind>]@<side> "
                          f"(sides: {', '.join(SIDES)})")
    return match.group(1), match.group(2), match.group(3)


def mod_ids(path: Path) -> list[str]:
    """The mod ids a JAR's mcmod.info declares (a list, or {modList: [...]}); none when it has no such file."""
    try:
        with zipfile.ZipFile(path) as archive:
            if "mcmod.info" not in archive.namelist():
                return []
            data = json.loads(archive.read("mcmod.info").decode("utf-8", errors="replace"))
    except (OSError, zipfile.BadZipFile, ValueError):
        return []
    entries = data.get("modList", []) if isinstance(data, dict) else data
    return sorted({str(e["modid"]) for e in entries if isinstance(e, dict) and e.get("modid")})


def _jvm(table, where: str) -> dict[str, list[str]]:
    table = table or {}
    if not isinstance(table, dict) or set(table) - {"client", "server", "both"}:
        raise LaunchError(f"{where}.jvm is a table of client, server and both, each a list of JVM arguments")
    out = {}
    for side, values in table.items():
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            raise LaunchError(f"{where}.jvm.{side} is a list of JVM arguments")
        out[side] = list(values)
    return out


def _add_jvm(target: dict[str, list[str]], jvm: dict[str, list[str]]):
    for side, values in jvm.items():
        for name in (("client", "server") if side == "both" else (side,)):
            target[name].extend(v for v in values if v not in target[name])


def definition(project_toml: dict, name: str, where: str = "project.toml") -> dict:
    """The [launch.<name>] table, checked: known keys, members parsed, a known runtime."""
    launches = project_toml.get("launch") or {}
    table = launches.get(name)
    if not isinstance(table, dict) or name == "member":
        defined = sorted(k for k in launches if k != "member")
        raise LaunchError(f"{where} has no [launch.{name}] (defined: {', '.join(defined) or 'none'})")
    for key in sorted(set(table) - LAUNCH_KEYS):
        raise LaunchError(f"{where}: unknown key launch.{name}.{key}")
    members = table.get("members")
    if not isinstance(members, list) or not members:
        raise LaunchError(f"{where}: launch.{name}.members lists the launch's members")
    for member in members:
        parse_member(member)
    if len(set(members)) != len(members):
        raise LaunchError(f"{where}: launch.{name}.members names a member twice")
    place = table.get("place") or {}
    if not isinstance(place, dict):
        raise LaunchError(f"{where}: launch.{name}.place is a table of member = file name")
    for member, file in place.items():
        if member not in members:
            raise LaunchError(f"{where}: launch.{name}.place names {member}, which is not a member")
        if not isinstance(file, str) or not PLACE.match(file):
            raise LaunchError(f"{where}: launch.{name}.place.{member} is a JAR file name, not {file!r}")
    runtime = table.get("runtime", "cleanroom")
    if runtime not in RUNTIMES:
        raise LaunchError(f"{where}: launch.{name}.runtime is one of {', '.join(RUNTIMES)}")
    _jvm(table.get("jvm"), f"{where}: launch.{name}")
    return table


def member_extras(project_toml: dict | None, where: str) -> dict:
    """A member's own [launch.member] table, checked; empty when it has none."""
    table = ((project_toml or {}).get("launch") or {}).get("member") or {}
    for key in sorted(set(table) - MEMBER_KEYS):
        raise LaunchError(f"{where}: unknown key launch.member.{key}")
    return {"jvm": _jvm(table.get("jvm"), f"{where}: launch.member"), "collect": list(table.get("collect") or []),
            "failures": list(table.get("failures") or [])}


def _store_file(store: Path, digest: str, name: str | None) -> tuple[str, Path]:
    """(file name, path) of a kept file; a third entry's name is the one file in its folder."""
    from wb_core import artifacts
    if name is None:
        folder = store / "files" / digest
        names = sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []
        if len(names) != 1:
            raise artifacts.StoreError(f"the Store has no file with SHA-256 {digest}")
        name = names[0]
    return name, artifacts.path_of(store, digest, name)


def resolve(*, product: str, commit: str, tree: str, launch: str, table: dict, set_doc: dict, revision: str,
            store: Path, product_artifacts: dict, product_index: dict | None, extras=None) -> dict:
    """The launch file for ``table`` (a checked [launch.<name>]) of ``product`` at ``commit``/``tree`` on set
    ``revision``. ``product_artifacts`` is the product's [artifacts] (each with its ``file`` glob), ``product_index``
    the Store's index of its tree; ``extras(project, commit)`` returns that member's project.toml or None."""
    from wb_core import artifacts
    lines, holds, thirds = set_doc.get("projects") or {}, set_doc.get("holds") or {}, set_doc.get("third") or {}
    members, owners = [], {}
    jvm = {"client": [], "server": []}
    collect, failures = list(table.get("collect") or []), list(table.get("failures") or [])
    for spec in table["members"]:
        name, part, side = parse_member(spec)
        entry = {"member": spec, "name": name, "side": side}
        file_name = None
        if name == product:
            if not part:
                raise LaunchError(f"member {spec}: name the product's artifact ({product}:<artifact>@<side>)")
            glob = (product_artifacts.get(part) or {}).get("file")
            if not glob:
                raise LaunchError(f"member {spec}: {product} has no [artifacts.{part}]")
            if product_index is None:
                raise LaunchError(f"CI has not built {product} tree {tree[:12]} (commit {commit[:12]}), so the Store "
                                  f"has no {product}:{part}: run wb check in the worktree, then resolve again")
            found = [(file, item) for file, item in (product_index.get("artifacts") or {}).items()
                     if fnmatch.fnmatch(str(item.get("from") or file), glob) or fnmatch.fnmatch(file, Path(glob).name)]
            if len(found) != 1:
                raise LaunchError(f"member {spec}: the Store's index of {product} tree {tree[:12]} has "
                                  f"{len(found)} files matching {glob}")
            file_name, item = found[0]
            entry.update(kind="product", artifact=part, sha256=item["sha256"], commit=commit, tree=tree)
            source_project, source_commit = product, commit
        elif part and f"{name}:{part}" in holds and part in (holds[f"{name}:{part}"].get("artifacts") or {}):
            hold = holds[f"{name}:{part}"]
            item = hold["artifacts"][part]
            file_name = item.get("file")
            entry.update(kind="hold", artifact=part, sha256=item["sha256"], commit=hold.get("commit"),
                         reason=hold.get("reason", ""))
            source_project, source_commit = name, hold.get("commit")
        elif part and name in lines:
            item = (lines[name].get("artifacts") or {}).get(part)
            if not item:
                raise LaunchError(f"member {spec}: {name}'s line names no artifact {part} "
                                  f"(it has: {', '.join(sorted(lines[name].get('artifacts') or {})) or 'none'})")
            file_name = item.get("file")
            entry.update(kind="line", artifact=part, sha256=item["sha256"], commit=lines[name]["commit"])
            source_project, source_commit = name, lines[name]["commit"]
        elif name in thirds:
            kind = part or "runtime"
            third = thirds[name]
            # A coordinate's files by kind; a file without one (from the drop folder) is its runtime file.
            files = third.get("files") or ({"runtime": third["sha256"]} if third.get("sha256") else {})
            digest = files.get(kind)
            if not digest:
                raise LaunchError(f"member {spec}: third entry {name} has no {kind} file "
                                  f"(it has: {', '.join(sorted(files)) or 'none'})")
            file_name = third.get("file") if not third.get("files") else None
            entry.update(kind="third", artifact=kind, sha256=digest, module=third.get("module", ""),
                         licence=third.get("licence", ""), **({"source": third["source"]} if third.get("source") else {}))
            source_project, source_commit = None, None
        else:
            known = sorted({*lines, *thirds, product})
            raise LaunchError(f"member {spec}: the set names no project or third entry {name} "
                              f"(known: {', '.join(known)})" + ("" if part or name not in lines else
                                                               f"; a project member names its artifact ({name}:mod)"))
        try:
            file_name, path = _store_file(store, entry["sha256"], file_name)
        except artifacts.StoreError as error:
            fix = (f"wb check in {source_project} at {str(source_commit)[:12]}" if source_project and
                   entry["kind"] != "hold" else "the landing that recorded it puts it there")
            raise LaunchError(f"member {spec}: {error}; {fix}") from None
        ids = mod_ids(path)
        if entry["kind"] == "third" and thirds[name].get("mod_id") and thirds[name]["mod_id"] not in ids:
            ids = sorted({*ids, thirds[name]["mod_id"]})
        entry.update(file=file_name, path=str(path.relative_to(store)).replace("\\", "/"), mod_ids=ids)
        if spec in (table.get("place") or {}):
            entry["place"] = table["place"][spec]
        for mod_id in ids:
            owners.setdefault(mod_id, []).append(spec)
        if source_project and source_project != product and extras is not None:
            own = member_extras(extras(source_project, source_commit), f"{source_project} {str(source_commit)[:12]}")
            _add_jvm(jvm, own["jvm"])
            collect += [c for c in own["collect"] if c not in collect]
            failures += [f for f in own["failures"] if f not in failures]
        members.append(entry)
    doubled = {mod_id: specs for mod_id, specs in owners.items() if len(specs) > 1}
    if doubled:
        raise LaunchError("the launch would load two builds of one mod id: "
                          + "; ".join(f"{mod_id} from {', '.join(specs)}" for mod_id, specs in sorted(doubled.items())))
    _add_jvm(jvm, _jvm(table.get("jvm"), f"launch.{launch}"))
    platform = set_doc.get("platform") or {}
    java = platform.get("java")
    runtime = {"kind": table.get("runtime", "cleanroom"),
               "version": (platform.get("cleanroom") or {}).get("version", ""),
               "java": java, "jdk": ((platform.get("jdk") or {}).get(str(java)) or {}).get("build", "")}
    return {"schema": SCHEMA, "project": product, "launch": launch, "commit": commit, "tree": tree,
            "set": revision, "store": str(store), "runtime": runtime, "members": members, "jvm": jvm,
            "collect": collect, "failures": failures, "params": dict(table.get("params") or {})}


# --- the job ------------------------------------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> str | None:
    done = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, encoding="utf-8")
    return done.stdout if done.returncode == 0 else None


def _toml_at(repo: Path, commit: str, where: str) -> dict | None:
    if _git(repo, "cat-file", "-e", f"{commit}^{{commit}}") is None:
        raise LaunchError(f"{where}: {repo} does not have commit {commit[:12]}; fetch it (git -C {repo} fetch)")
    text = _git(repo, "show", f"{commit}:project.toml")
    if text is None:
        return None
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise LaunchError(f"{where}: project.toml at {commit[:12]} does not parse: {error}") from None


def resolve_command(ctx, folder: str, launch: str = "default", out: str = ""):
    from wb.setwork import resolve as set_revision
    from wb.workspace import Workspace, WorkspaceError
    from wb_core import artifacts
    from wb_core.module import EnvironmentProblem, ParamError
    place = Path(folder)
    top_text = _git(place, "rev-parse", "--show-toplevel")
    if top_text is None:
        raise ParamError(f"{place} is not in a git worktree")
    top = Path(top_text.strip())
    if _git(top, "status", "--porcelain", "--untracked-files=no"):
        raise ParamError(f"{top} has uncommitted changes: a launch describes a commit, whose tree CI built; commit "
                         "them and run wb check first")
    commit = _git(top, "rev-parse", "HEAD").strip()
    tree = _git(top, "rev-parse", "HEAD^{tree}").strip()
    try:
        workspace = Workspace()
        store = workspace.store_root()
        home = workspace.set_home()
    except (WorkspaceError, OSError) as error:
        raise EnvironmentProblem(f"no workspace to resolve against: {error}") from None
    if store is None or home is None:
        raise EnvironmentProblem("this machine has no Store or no Set (modules.ci.store, workspace.set)")
    revision = set_revision(workspace, top, say=ctx.log.info)
    shown = _git(home, "show", f"{revision}:set.json")
    if shown is None:
        raise EnvironmentProblem(f"the Set has no revision {revision}")
    set_doc = json.loads(shown)
    try:
        own = _toml_at(top, commit, "the product")
        if own is None:
            raise LaunchError(f"{top} has no project.toml at {commit[:12]}, so it defines no launch")
        product = str((own.get("project") or {}).get("name") or "")
        table = definition(own, launch, f"{product or top.name} project.toml")
        registered = workspace.projects()

        def extras(project: str, at: str):
            entry = registered.get(project)
            if entry is None or not entry.repo.is_dir():
                raise LaunchError(f"member project {project} is not registered on this machine "
                                  "([modules.ci.projects]), so its [launch.member] cannot be read")
            return _toml_at(entry.repo, at, f"member {project}")

        document = resolve(product=product, commit=commit, tree=tree, launch=launch, table=table, set_doc=set_doc,
                           revision=revision, store=store, product_artifacts=own.get("artifacts") or {},
                           product_index=artifacts.find(store, product, tree), extras=extras)
    except LaunchError as error:
        raise ParamError(str(error)) from None
    target = Path(out) if out else top / ".local" / "launch" / f"{launch}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(document, indent=1) + "\n"
    target.write_text(text, encoding="utf-8")
    (ctx.out / "launch.json").write_text(text, encoding="utf-8")
    ctx.output("launch", ctx.out / "launch.json")
    ctx.summary(f"launch {product}:{launch} at {commit[:12]} on set {revision[:7]}: {len(document['members'])} "
                f"members, written to {target}")
    return {"launch": str(target), "set": revision, "commit": commit,
            "members": {m["member"]: m["sha256"] for m in document["members"]}}
