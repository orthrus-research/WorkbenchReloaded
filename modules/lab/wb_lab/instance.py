"""lab.build: a game instance built from a resolved launch file, with a receipt naming every file by SHA-256.

The launch file (``lab.resolve``'s launch.json) is the only input that says what the game is: its members by SHA-256
from the Store, its runtime (kind, version, the exact JDK build, the runtime's own files by URL and SHA-256 in
``runtime.files``), its JVM arguments per side and its params. The builder reads nothing else about the game, and
resolves nothing again. It builds in stages:

1. runtime: each runtime file is taken from the Store by SHA-256, from a ``runtime_candidates`` folder, or downloaded
   from a host on the allow list (``runtime_hosts``, by default ``[modules.ci] store_hosts``), every redirect
   included, and kept in the Store. The server is installed by the runtime's own installer, run on the launch's JDK
   from the Store (``<store>/jdks/<build>/<system>/``), and the universal JAR it installs must be the launch's. The
   client is assembled from the launcher instance (``prism``) as ``lab.assemble`` does: libraries, the client JAR and
   assets by SHA-1 into the shared ``runtime_cache``, natives unpacked into the client folder.
2. content: each member's Store file is hashed again and refused unless it is the launch's bytes, then placed in
   ``server/mods`` and/or ``client/mods`` (by its side) under its ``place`` or its file name.
3. base: the files, JVM profile, player and command lines of the launch's base (launch.json ``base``: the base
   project's ``[launch.<name>]`` as lab.resolve read it), its overlay folder exported from the base project's
   repository by the tree id the launch names (``git archive``), then each launch's own overlay folder (a launch that
   inherits another launch lays that launch's first: launch.json ``overlays``), then the configure script
   (launch.json ``configure``, exported by its blob id; an inherited one is its own project's, which must be
   trusted) or the hook the request names: a Python file whose ``configure(instance, launch, params, placed)`` makes a launched product's own changes. An
   overlay's text files are written with the platform's line ends and ``{port}``, ``{seed}``, ``{player}`` and
   ``{player_uuid}`` filled in. A launch file that names no base, or a request naming one of ``wb_lab.bases``, takes
   Workbench's interim copy, with a warning. Workbench names no product; the script is the product's.
4. receipt: ``instance.json`` names every file by SHA-256 with its path relative to ``{instance}``, ``{cache}`` (the
   runtime cache) or ``{jdk:N}`` (the JDK of major N), the launch file's SHA-256, the builder's schema, and the
   server's and client's command lines and working folders with those placeholders.

One template is built per key: the launch file's bytes, the builder schema, the base and its overlay trees, the
configure script's bytes and the build's own parameters, each checked against the type the launch declares for it.
What a session chooses is never part of it: the server's port is the session's (``{port}`` stays in the client's
command line, and the session writes ``server-port``), as are its JVM options, window and views (``wb_lab.run``,
which reads the base's view keys and ready line from the receipt). A configure script runs only for a project in
``[lab] trusted``; ``[lab] eula`` records the machine owner's acceptance of the Minecraft EULA. Instances are made
from it by ``wb_lab.store``: read-only files (libraries, mods, natives) are hard links to one kept copy, everything
the game writes is the instance's own, and each instance carries its ``instance.json``. A folder without one is never
touched.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import traceback
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from wb_core import artifacts
from wb_core.module import Context, EnvironmentProblem, ParamError, sha256_file

from . import assemble
from . import machine as machines
from . import process as processes
from . import resolve as resolver
from .bases import INTERIM_BASES
from .store import MARKER, Layout, Store, StoreError, recipe_of, remove_tree

SCHEMA = "wb.lab.instance/1"
RECEIPT = "instance.json"
LAUNCH_COPY = "launch.json"
NAME = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
RUNTIME_PARTS = ("installer", "universal", "prism")
SIDES = ("server", "client")
# Where the Cleanroom installer puts the universal JAR and its argument files, under the server folder.
CLEANROOM_LIBRARY = "libraries/com/cleanroommc/cleanroom/{version}"
# What the game only reads: shared by hard link between a template and its instances.
LAYOUT = Layout(linkable_dirs=["server/libraries", "server/mods", "client/mods", "client/natives"],
                linkable_suffixes=[".jar"])
PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")
SPLICES = ("{jvm}", "{profile}", "{tweakers}")
INSTALL_TIMEOUT = 1800
# The port a template's server.properties is written with; a session writes its own (wb_lab.run).
DEFAULT_PORT = 25565
# What a session fills in a receipt's command lines, beside the roots.
SESSION_PLACEHOLDERS = ("port",)


def offline_uuid(name: str) -> str:
    """The UUID the game gives an offline player (an MD5 name UUID of ``OfflinePlayer:<name>``), written as the game
    writes it."""
    return str(uuid.UUID(bytes=hashlib.md5(("OfflinePlayer:" + name).encode("utf-8")).digest(), version=3))


# --- the machine -----------------------------------------------------------------------------------------------------


class Machine:
    """What this machine's config says, as the host names it: a configured container path is translated back."""

    def __init__(self):
        try:
            from wb.workspace import Workspace
            self.workspace = Workspace()
        except Exception:  # no machine config (a test, a bare checkout): paths are taken as given
            self.workspace = None

    def path(self, value) -> Path:
        return self.workspace.host_path(str(value)) if self.workspace else Path(value)

    def store_hosts(self) -> list[str]:
        return [str(h) for h in (self.workspace.ci.get("store_hosts") or [])] if self.workspace else []

    def repository(self, project: str) -> Path:
        """The registered repository of ``project``, which holds the trees a launch file names."""
        entry = self.workspace.projects().get(project) if self.workspace else None
        if entry is None or not Path(entry.repo).is_dir():
            raise EnvironmentProblem(f"project {project} is not registered on this machine ([modules.ci.projects]), "
                                     "so the launch/ folder its launch file names cannot be exported")
        return Path(entry.repo)


def _lab_root(ctx: Context, machine: Machine) -> Path:
    configured = str(ctx.config.get("instances") or "")
    if configured:
        return machine.path(configured)
    home = os.environ.get("WB_LAB_HOME", "")
    if not home:
        raise EnvironmentProblem("no folder for instances: set [modules.lab] instances, or run lab.build on wb-host, "
                                 "which names the lab home")
    return Path(home) / "instances"


def _runtime_cache(ctx: Context, machine: Machine) -> Path:
    configured = str(ctx.config.get("runtime_cache") or "")
    return machine.path(configured) if configured else assemble._cache(ctx)


# --- the launch file -------------------------------------------------------------------------------------------------


def read_launch(path: Path) -> tuple[bytes, dict]:
    """The launch file's bytes and document, checked: schema 1, a Cleanroom runtime, its members."""
    if not path.is_file():
        raise ParamError(f"launch: {path} is not a file (wb run lab.resolve folder=<worktree> writes one)")
    data = path.read_bytes()
    try:
        doc = json.loads(data)
    except ValueError as error:
        raise ParamError(f"launch: {path.name} is not JSON ({error})") from None
    if not isinstance(doc, dict) or doc.get("schema") != 1:
        raise ParamError(f"launch: {path.name} is not a launch file of schema 1")
    runtime = doc.get("runtime") or {}
    if runtime.get("kind") != "cleanroom":
        raise ParamError(f"launch: {path.name} names runtime {runtime.get('kind')!r}; lab.build builds cleanroom")
    for key in ("version", "java", "jdk"):
        if not runtime.get(key):
            raise ParamError(f"launch: {path.name} names no runtime.{key}; resolve the launch again on a set whose "
                             "platform names it")
    if not doc.get("members") or not doc.get("store"):
        raise ParamError(f"launch: {path.name} names no members or no Store")
    return data, doc


def runtime_files(doc: dict) -> dict[str, dict]:
    """The runtime's files the launch names, each {url, file, sha256}; refused with the fix when any is missing."""
    files = (doc.get("runtime") or {}).get("files") or {}
    missing = [part for part in RUNTIME_PARTS if not isinstance(files.get(part), dict)
               or not all(files[part].get(key) for key in ("url", "file", "sha256"))]
    if missing:
        version = doc["runtime"]["version"]
        raise ParamError(f"the launch file names no runtime file for {', '.join(missing)} (runtime.files): land the "
                         f"platform's Cleanroom files (wb land platform.cleanroom={version}), then resolve the launch "
                         "again (wb run lab.resolve folder=<worktree>) so its launch file carries them")
    return {part: files[part] for part in RUNTIME_PARTS}


def jdk_home(store: Path, build: str, system: str) -> Path:
    """The JDK build the launch names, unpacked in the Store by store.platform."""
    folder = store / "jdks" / build / system
    homes = [folder] if (folder / "bin").is_dir() else sorted(p for p in folder.iterdir() if (p / "bin").is_dir()) \
        if folder.is_dir() else []
    if len(homes) != 1:
        raise EnvironmentProblem(f"the Store has no JDK {build} for {system} ({folder}); fetch the platform of the "
                                 "launch's set: wb run store.platform revision=<set>")
    return homes[0]


def java_of(home: Path, system: str) -> Path:
    return home / "bin" / ("java.exe" if system == "windows" else "java")


def _jdk_identity(home: Path, system: str) -> dict[str, Path]:
    """The JDK files a receipt names: what makes it this build."""
    jvm = "bin/server/jvm.dll" if system == "windows" else "lib/server/libjvm.so"
    return {rel: home / rel for rel in ("release", java_of(home, system).relative_to(home).as_posix(), "lib/modules",
                                        jvm) if (home / rel).is_file()}


# --- runtime files ---------------------------------------------------------------------------------------------------


def allowed(url: str, hosts: list[str]) -> bool:
    """An https URL on an allowed host (or a subdomain of one); the rule of store.platform."""
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and any(host == h.lower() or host.endswith("." + h.lower()) for h in hosts)


class _Hops(urllib.request.HTTPRedirectHandler):
    """Follows a redirect only to an allowed host: the list governs every hop."""

    def __init__(self, hosts: list[str]):
        self.hosts = hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not allowed(newurl, self.hosts):
            raise EnvironmentProblem(f"{req.full_url} redirects to {newurl}, a host outside the allowed hosts")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def runtime_file(ctx: Context, store: Path, part: str, entry: dict, hosts: list[str], candidates: list[Path]) -> Path:
    """A runtime file by SHA-256: the Store's copy, else a candidate folder's, else downloaded; kept in the Store."""
    digest, name, url = entry["sha256"].lower(), entry["file"], entry["url"]
    kept = store / "files" / digest / name
    if kept.is_file():
        if sha256_file(kept) != digest:
            raise EnvironmentProblem(f"the Store's {kept} no longer hashes to {digest}; it was changed outside "
                                     "Workbench")
        return kept
    for folder in candidates:
        candidate = folder / name
        if candidate.is_file() and sha256_file(candidate) == digest:
            ctx.log.info(f"runtime {part}: {name} from {candidate}")
            return artifacts.put(store, candidate, digest)
    if not allowed(url, hosts):
        raise ParamError(f"runtime {part}: {url} is on a host outside the allowed hosts ({', '.join(hosts) or 'none'}; "
                         "[modules.lab] runtime_hosts, by default [modules.ci] store_hosts), and the Store has no "
                         f"{name} with SHA-256 {digest[:12]}")
    ctx.progress("runtime", msg=f"downloading {name}")
    store.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=store) as scratch:
        target = Path(scratch) / name
        hasher = hashlib.sha256()
        try:
            with urllib.request.build_opener(_Hops(hosts)).open(url, timeout=60) as response, \
                    open(target, "wb") as handle:
                while block := response.read(1 << 20):
                    hasher.update(block)
                    handle.write(block)
                    ctx.check_cancel()
        except OSError as error:
            raise EnvironmentProblem(f"runtime {part}: could not download {url}: {error}") from None
        if hasher.hexdigest() != digest:
            raise EnvironmentProblem(f"runtime {part}: {url} is {hasher.hexdigest()}, not the {digest} the launch "
                                     "names; nothing kept")
        return artifacts.put(store, target, digest)


# --- stages ----------------------------------------------------------------------------------------------------------


def _fill(text: str, values: dict[str, str], where: str) -> str:
    def one(match: re.Match) -> str:
        key = match.group(1)
        if key not in values:
            raise ParamError(f"{where}: {{{key}}} is no placeholder here (known: {', '.join(sorted(values))})")
        return values[key]
    return PLACEHOLDER.sub(one, text)


def _fill_known(text: str, values: dict[str, str]) -> str:
    """Fills only the names given: a file's own braces (a config's ``general {``) stay as they are."""
    return PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), text)


def install_server(ctx: Context, base: dict, installer: Path, java: Path, home: Path, target: Path, version: str):
    values = {"java": str(java), "installer": str(installer), "target": str(target), "version": version,
              "memory": str(base.get("memory") or "")}
    argv = [_fill(str(item), values, "the base's installer") for item in base["installer"]]
    target.mkdir(parents=True, exist_ok=True)
    env = {"JAVA_HOME": str(home), "PATH": str(home / "bin") + os.pathsep + os.environ.get("PATH", "")}
    ctx.progress("install", msg=f"{installer.name} into the template's server folder")
    ran = processes.run(ctx, argv, cwd=target.parent, timeout=INSTALL_TIMEOUT, env=env)
    (ctx.out / "installer.log").write_text(ran.output + "\n", encoding="utf-8")
    if ran.code is None:
        raise EnvironmentProblem(f"the installer was stopped at its limit ({processes.span(INSTALL_TIMEOUT)})")
    if ran.code != 0:
        last = [line for line in ran.output.splitlines() if line.strip()][-1:] or ["no output"]
        raise EnvironmentProblem(f"the installer exited {ran.code}: {last[0][:200]} (installer.log in the job's "
                                 "out folder)")


def check_members(doc: dict) -> list[dict]:
    """Every member's Store file hashed again, before anything is placed; each with the sides and name it takes."""
    store = Path(doc["store"])
    out, seen = [], {}
    for member in doc["members"]:
        source = store / member["path"]
        if not source.is_file():
            raise ParamError(f"launch member {member['member']}: the Store has no {member['path']}; nothing was "
                             "placed (resolve the launch again, or run the check that makes it)")
        actual = sha256_file(source)
        if actual != member["sha256"]:
            raise ParamError(f"launch member {member['member']}: the Store's {member['path']} is {actual[:12]}, not "
                             f"the launch's {member['sha256'][:12]}; nothing was placed")
        sides = list(SIDES) if member["side"] == "both" else [member["side"]]
        name = member.get("place") or member["file"]
        for side in sides:
            other = seen.setdefault((side, name), member["member"])
            if other != member["member"]:
                raise ParamError(f"launch members {other} and {member['member']} would both be {side}/mods/{name}")
        out.append({**member, "source": source, "sides": sides, "name": name})
    return out


def place_members(members: list[dict], instance: Path) -> dict[str, dict[str, Path]]:
    placed: dict[str, dict[str, Path]] = {}
    for member in members:
        for side in member["sides"]:
            target = instance / side / "mods" / member["name"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(member["source"], target)
            placed.setdefault(member["member"], {})[side] = target
    return placed


def _git_bytes(repo: Path, *args: str) -> bytes:
    done = subprocess.run(["git", "-C", str(repo), "-c", "core.autocrlf=false", *args], capture_output=True)
    if done.returncode != 0:
        raise EnvironmentProblem(f"git {' '.join(args[:2])} in {repo} failed: "
                                 f"{done.stderr.decode('utf-8', 'replace').strip()[:200]} (fetch the commit the "
                                 "launch file names, or resolve the launch again)")
    return done.stdout


def export_tree(repo: Path, tree: str, target: Path) -> Path:
    """The tree ``tree`` of ``repo`` as files in ``target`` (git archive): exactly the bytes the launch file names."""
    if target.is_dir():
        return target
    scratch = target.with_name(target.name + f".{os.getpid()}.tmp")
    if scratch.exists():
        remove_tree(scratch)
    scratch.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(_git_bytes(repo, "archive", "--format=tar", tree))) as archive:
        archive.extractall(scratch, filter="data")
    os.replace(scratch, target)
    return target


def export_blob(repo: Path, blob: str, target: Path) -> Path:
    if not target.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_git_bytes(repo, "cat-file", "blob", blob))
    return target


def write_overlay(folder: Path, instance: Path, values: dict[str, str]):
    """An overlay folder's ``both/``, ``server/`` and ``client/`` files laid into the instance's sides: a text file
    with the platform's line ends (as the game writes its own) and the known placeholders filled, other files as
    they are."""
    for part in ("both", *SIDES):
        source = folder / part
        for path in sorted(p for p in source.rglob("*") if p.is_file()) if source.is_dir() else []:
            relative = path.relative_to(source)
            data = path.read_bytes()
            for side in (SIDES if part == "both" else (part,)):
                target = instance / side / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    text = data.decode("utf-8")
                except UnicodeDecodeError:
                    target.write_bytes(data)
                    continue
                target.write_text(_fill_known(text.replace("\r\n", "\n"), values), encoding="utf-8")


def write_base(base: dict, instance: Path, values: dict[str, str], eula: bool, overlays: list[Path] = ()):
    files = base.get("files") or {}
    for side in SIDES:
        for relative, text in {**(files.get("both") or {}), **(files.get(side) or {})}.items():
            target = instance / side / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(_fill_known(text, values), encoding="utf-8")  # the platform's line ends, as the game's
    for folder in overlays:
        write_overlay(folder, instance, values)
    profile = base.get("jvm_profile") or {}
    (instance / "server" / "user_jvm_args.txt").write_text(
        str(base.get("jvm_args_file") or "") + "\n".join(profile.get("arguments") or []) + "\n", encoding="utf-8")
    if eula:
        (instance / "server" / "eula.txt").write_text(str(base.get("eula") or "eula=true\n"), encoding="utf-8")


def load_configure(path: Path):
    spec = importlib.util.spec_from_file_location(f"wb_lab_configure_{sha256_file(path)[:12]}", path)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as error:
        raise ParamError(f"configure: {path.name} does not load: {error}") from None
    if not callable(getattr(module, "configure", None)):
        raise ParamError(f"configure: {path.name} has no configure(instance, launch, params, placed)")
    return module.configure


def run_configure(ctx: Context, path: Path, instance: Path, doc: dict, params: dict, placed: dict) -> dict:
    hook = load_configure(path)
    ctx.progress("configure", msg=path.name)
    try:
        record = hook(instance=instance, launch=json.loads(json.dumps(doc)), params=dict(params),
                      placed={spec: dict(sides) for spec, sides in placed.items()})
    except Exception as error:
        ctx.log.error(traceback.format_exc())
        raise EnvironmentProblem(f"configure: {path.name} failed: {type(error).__name__}: {error}") from None
    if record is not None and not isinstance(record, dict):
        raise ParamError(f"configure: {path.name} returned {type(record).__name__}, not a table or nothing")
    return json.loads(json.dumps(record or {}, default=str))


def command(template: list, values: dict[str, str], splices: dict[str, list[str]], where: str) -> list[str]:
    out = []
    for item in template:
        if item in SPLICES:
            out.extend(splices[item])
        else:
            out.append(_fill(str(item), values, where))
    return out


def expand(argv: list[str], roots: dict[str, str]) -> list[str]:
    """A receipt's command line with its roots filled in: ``{"instance": ..., "cache": ..., "jdk:26": ...}``."""
    pattern = re.compile(r"\{(instance|cache|jdk:\d+)\}")
    return [pattern.sub(lambda m: roots[m.group(1)].rstrip("/\\"), token) for token in argv]


def _relative(path: Path, roots: dict[str, Path]) -> str:
    for name, root in roots.items():
        try:
            return "{" + name + "}/" + path.relative_to(root).as_posix()
        except ValueError:
            continue
    raise EnvironmentProblem(f"{path} lies under none of the receipt's roots ({', '.join(roots)})")


def _tree(root: Path, skip: set[str]) -> dict[str, dict]:
    files = {}
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        relative = path.relative_to(root).as_posix()
        if relative not in skip:
            files["{instance}/" + relative] = {"sha256": sha256_file(path), "bytes": path.stat().st_size}
    return files


# --- the command -----------------------------------------------------------------------------------------------------


def template_inputs(launch_sha: str, base_name: str, base: dict, configure_sha: str | None, params: dict,
                    eula: bool, system: str, arch: str, overlays: list[str] = ()) -> dict:
    base_sha = hashlib.sha256(json.dumps(base, sort_keys=True).encode("utf-8")).hexdigest()
    return {"builder": SCHEMA, "launch": launch_sha, "base": base_name, "base_sha256": base_sha,
            "overlays": list(overlays), "configure": configure_sha, "params": params, "eula": bool(eula),
            "system": system, "arch": arch}


def build_template(ctx: Context, staging: Path, *, data: bytes, doc: dict, base_name: str, base: dict,
                   configure: Path | None, params: dict, eula: bool, key: str, machine: Machine,
                   overlays: list[Path] = (), base_source: dict | None = None, configure_from: dict | None = None
                   ) -> dict:
    """Builds the template for ``key`` in ``staging`` (which becomes ``{instance}``) and returns its receipt."""
    system, arch = assemble.host_system()
    runtime = doc["runtime"]
    version, major = str(runtime["version"]), str(runtime["java"])
    store = Path(doc["store"])
    files = runtime_files(doc)
    home = jdk_home(store, str(runtime["jdk"]), system)
    java = java_of(home, system)
    members = check_members(doc)
    hosts = [str(h) for h in ctx.config.get("runtime_hosts") or []] or machine.store_hosts()
    candidates = [machine.path(p) for p in ctx.config.get("runtime_candidates") or []]
    kept = {part: runtime_file(ctx, store, part, files[part], hosts, candidates)
            for part in ("installer", "prism")}

    # 1. runtime: the server by its installer, the client from the launcher instance
    install_server(ctx, base, kept["installer"], java, home, staging / "server", version)
    library = CLEANROOM_LIBRARY.format(version=version)
    universal = staging / "server" / library / files["universal"]["file"]
    if not universal.is_file():
        raise EnvironmentProblem(f"the installer put no {files['universal']['file']} in {library}")
    if sha256_file(universal) != files["universal"]["sha256"]:
        raise EnvironmentProblem(f"the installer's {universal.name} is {sha256_file(universal)[:12]}, not the launch's "
                                 f"{files['universal']['sha256'][:12]}; review the platform's Cleanroom files")
    artifacts.put(store, universal, files["universal"]["sha256"])  # every runtime file in the Store
    cache = _runtime_cache(ctx, machine)
    built, problems, _ = assemble.build_runtime(
        ctx, kept["prism"], cache, staging / "client" / "natives", system=system, arch=arch,
        library_folders=[machine.path(p) for p in ctx.config.get("library_candidates") or []],
        asset_folders=[machine.path(p) for p in ctx.config.get("asset_candidates") or []])
    if built is None:
        raise EnvironmentProblem(f"the client runtime could not be assembled: {problems[0]}"
                                 + (f" (and {len(problems) - 1} more)" if len(problems) > 1 else ""))
    if not built.main_class or not built.asset_index:
        raise EnvironmentProblem("the launcher instance names no main class or no asset index")

    # 2. content
    ctx.progress("content", msg=f"{len(members)} member(s)")
    placed = place_members(members, staging)

    # 3. base, then the configure hook
    player = str(base.get("player") or "Player")
    values = {"port": str(DEFAULT_PORT), "seed": str(params.get("seed", "")), "player": player,
              "player_uuid": offline_uuid(player)}
    write_base(base, staging, values, eula, overlays)
    configured = None
    if configure is not None:
        configured = {"file": configure.name, "sha256": sha256_file(configure), **(configure_from or {}),
                      "record": run_configure(ctx, configure, staging, doc, params, placed)}
    (staging / LAUNCH_COPY).write_bytes(data)

    # 4. receipt
    ctx.progress("receipt")
    roots = {"instance": staging, "cache": cache, f"jdk:{major}": home}
    jdk_files = {f"{{jdk:{major}}}/{rel}": {"sha256": sha256_file(path), "bytes": path.stat().st_size}
                 for rel, path in _jdk_identity(home, system).items()}
    cache_files = {}
    for item in built.items:
        path = built.library(item)
        cache_files[_relative(path, roots)] = {"sha256": sha256_file(path), "sha1": item.sha1,
                                               "bytes": path.stat().st_size}
    cache_files[_relative(built.client, roots)] = {"sha256": sha256_file(built.client), "sha1": built.client_sha1,
                                                   "bytes": built.client.stat().st_size}
    index = built.asset_index
    cache_files[_relative(index["path"], roots)] = {"sha256": sha256_file(index["path"]), "sha1": index["sha1"],
                                                    "bytes": index["path"].stat().st_size}
    instance_files = _tree(staging, {RECEIPT, MARKER})
    for member in members:
        for side in member["sides"]:
            entry = instance_files.get(f"{{instance}}/{side}/mods/{member['name']}")
            if entry is None or entry["sha256"] != member["sha256"]:
                raise EnvironmentProblem(f"launch member {member['member']} changed while it was placed")
    separator = ";" if system == "windows" else ":"
    classpath = separator.join([_relative(built.client, roots)] + [_relative(built.library(i), roots)
                                                                   for i in built.items])
    shared = {"java": _relative(java, roots), "cache": "{cache}", "version": version, "classpath": classpath,
              "main_class": built.main_class, "player": player, "player_uuid": values["player_uuid"],
              "assets": "{cache}/assets", "asset_index": str(index["id"]), "port": "{port}",
              "args_file": "win_args.txt" if system == "windows" else "unix_args.txt"}
    jvm = doc.get("jvm") or {}
    profile = list((base.get("jvm_profile") or {}).get("arguments") or [])
    commands = {}
    for side in SIDES:
        game = "{instance}/" + side
        splices = {"{jvm}": list(jvm.get(side) or []), "{profile}": profile,
                   "{tweakers}": [t for name in built.tweakers for t in ("--tweakClass", name)]}
        argv = command(base[side], {**shared, "game": game}, splices, f"the base's {side} command")
        # Where a session's own JVM options go: after the launch's (the {jvm} splice), before the profile's.
        before = base[side].index("{jvm}") if "{jvm}" in base[side] else 0
        commands[side] = {"cwd": game, "command": argv, "jvm_at": before + len(splices["{jvm}"])}
    return {
        "schema": SCHEMA,
        "key": key,
        "launch": {"sha256": hashlib.sha256(data).hexdigest(), "file": LAUNCH_COPY,
                   **{k: doc.get(k) for k in ("project", "launch", "commit", "tree", "set")},
                   "members": {m["member"]: m["sha256"] for m in members}},
        "base": base_name,
        "base_source": base_source,
        "configure": configured,
        "params": params,
        # What a session reads of the base (wb_lab.run): the view keys and the server's ready line.
        "session": {"placeholders": list(SESSION_PLACEHOLDERS), "server_properties": "{instance}/server/server.properties",
                    "default_port": DEFAULT_PORT, "views": dict(base.get("views") or {}),
                    "ready": str(base.get("ready") or "")},
        "eula": bool(eula),
        "system": system,
        "arch": arch,
        "roots": {"instance": "the instance folder",
                  "cache": "the runtime cache ([modules.lab] runtime_cache): libraries, the client JAR and assets",
                  f"jdk:{major}": f"JDK {runtime['jdk']} ({system}), the launch's build, from the Store"},
        "jdk": {major: {"build": runtime["jdk"]}},
        "runtime": {"kind": runtime["kind"], "version": version,
                    "files": {part: {"file": files[part]["file"], "sha256": files[part]["sha256"]}
                              for part in RUNTIME_PARTS},
                    "components": built.components, "main_class": built.main_class, "tweakers": built.tweakers,
                    "asset_index": {"id": index["id"], "path": _relative(index["path"], roots),
                                    "objects": index["objects"]},
                    "natives": "{instance}/client/natives"},
        "jvm_profile": base.get("jvm_profile"),
        "player": {"name": player, "uuid": values["player_uuid"]},
        "members": [{"member": m["member"], "sha256": m["sha256"],
                     "placed": [f"{{instance}}/{side}/mods/{m['name']}" for side in m["sides"]]} for m in members],
        "server": commands["server"],
        "client": commands["client"],
        "files": {**instance_files, **cache_files, **jdk_files},
    }


INTERIM_DEFAULT = "cubature:standalone"  # the base of a launch file resolved before launches named one


def base_of(ctx: Context, doc: dict, requested: str) -> tuple[str, dict, list[dict], dict]:
    """(name, table, overlays, source) of the base an instance starts from: the launch file's, or the interim copy a
    request names (``wb_lab.bases``); a launch file without one takes the interim copy, with a warning. ``overlays`` are
    the base's folder and then the product's, each {project, tree, path}."""
    launched = doc.get("base")
    own = doc.get("overlay")
    # A launch that inherits another names every launch's overlay in order (``overlays``), else only its own.
    overlays = list(doc.get("overlays") or ([own] if own else []))
    if requested:
        if requested not in INTERIM_BASES:
            raise ParamError(f"base: {requested!r} is no interim base (known: {', '.join(sorted(INTERIM_BASES))}); "
                             "leave it empty to build from the launch's base")
        if launched:
            ctx.log.warning(f"INTERIM BASE: base={requested} replaces the launch's base {launched['name']} with "
                            "Workbench's interim copy")
        return requested, INTERIM_BASES[requested], overlays, {"name": requested, "source": "interim"}
    if not launched:
        ctx.log.warning(f"INTERIM BASE: the launch file names no base (it was resolved before launches named one), so "
                        f"the instance starts from Workbench's interim copy of {INTERIM_DEFAULT}; resolve it again")
        return INTERIM_DEFAULT, INTERIM_BASES[INTERIM_DEFAULT], overlays, {"name": INTERIM_DEFAULT,
                                                                             "source": "interim"}
    if launched.get("source") == "interim":
        ctx.log.warning(f"INTERIM BASE: {launched['project']} at {str(launched['commit'])[:12]} has no "
                        f"[launch.{launched['name'].split(':', 1)[1]}], so base {launched['name']} is Workbench's "
                        "interim copy (wb_lab/bases.py)")
    if launched.get("overlay"):
        overlays.insert(0, {"project": launched["project"], **launched["overlay"]})
    source = {k: launched.get(k) for k in ("name", "project", "commit", "source")}
    source["overlay"] = (launched.get("overlay") or {}).get("tree")
    return launched["name"], launched["definition"], overlays, source


def checked_params(doc: dict, params: dict | None) -> dict:
    """The launch's params with the request's over them, each checked against the type the launch declares; a launch
    file resolved before params were typed takes them as they are."""
    types = doc.get("param_types")
    merged = dict(doc.get("params") or {})
    for name, value in (params or {}).items():
        if types is not None:
            if name not in types:
                raise ParamError(f"params: the launch declares no param {name} (it declares: "
                                 f"{', '.join(sorted(types)) or 'none'})")
            try:
                value = resolver.param_value(name, value, types[name], "params")
            except resolver.LaunchError as error:
                raise ParamError(str(error)) from None
        merged[name] = value
    return merged


def sourced(sources: Path, item: str, name: str | None = None) -> Path:
    """A tree or blob a launch file names, as a bundle carries it: ``<sources>/<id>/`` (a tree's files) or
    ``<sources>/<id>/<name>`` (a blob)."""
    path = sources / item / name if name else sources / item
    if not (path.is_file() if name else path.is_dir()):
        raise ParamError(f"sources: {sources} has no {item[:12]}" + (f"/{name}" if name else "/") + " (the launch "
                         "file names it; make the bundle again)")
    return path


def build(ctx: Context, launch: str, name: str, base: str = "", params: dict | None = None,
          eula: bool = False, configure: str | None = None, fresh: bool = False, store: str = "",
          sources: str = ""):
    if not NAME.match(name):
        raise ParamError(f"name: {name!r} is not a lowercase instance name (letters, digits, . _ -)")
    machine = Machine()
    data, doc = read_launch(Path(launch))
    if store:  # a copy of the launch's Store files elsewhere (a rental's bundle); the launch file's bytes are kept
        doc = {**doc, "store": str(Path(store))}
    carried = Path(sources) if sources else None
    runtime_files(doc)  # refused with the fix before anything is made
    base, base_doc, overlays, base_source = base_of(ctx, doc, base)
    project = str(doc.get("project") or "")
    exports = ctx.work / "exports"
    hook, configure_from = (Path(configure) if configure else None), None
    if hook is not None and not hook.is_file():
        raise ParamError(f"configure: {configure} is not a file")
    if hook is not None and doc.get("configure"):
        ctx.log.warning(f"configure: {hook.name} replaces the launch's {doc['configure']['path']}")
    if hook is not None or doc.get("configure"):  # before anything is made: the script is its project's code
        what = f"configure hook {hook.name}" if hook else f"configure script {doc['configure']['path']}"
        # An inherited script is its own project's (the base launch's), so that project is the one trusted.
        owner = project if hook is not None else str(doc["configure"].get("project") or project)
        machines.require_trusted(ctx, owner, what)
    if hook is None and doc.get("configure"):
        script = doc["configure"]
        hook = sourced(carried, script["blob"], Path(script["path"]).name) if carried else export_blob(
            machine.repository(script["project"]), script["blob"], exports / script["blob"] / Path(script["path"]).name)
        configure_from = {k: script[k] for k in ("project", "commit", "path", "blob")}
    eula = bool(eula) or bool(machines.lab(ctx)["eula"])
    merged = checked_params(doc, params)
    system, arch = assemble.host_system()
    launch_sha = hashlib.sha256(data).hexdigest()
    inputs = template_inputs(launch_sha, base, base_doc, sha256_file(hook) if hook else None, merged, eula,
                             system, arch, [o["tree"] for o in overlays])
    key = recipe_of(inputs)
    root = _lab_root(ctx, machine)
    store = Store(root / ".store", LAYOUT)
    destination = root / name
    if destination.exists():
        receipt = destination / RECEIPT
        if not receipt.is_file():
            raise ParamError(f"{destination} exists and has no {RECEIPT}, so lab.build did not make it; it is left "
                             "alone (choose another name)")
        current = json.loads(receipt.read_text(encoding="utf-8")).get("key")
        if current == key and not fresh:
            ctx.summary(f"instance {name} is already built from this launch (template {key}); fresh=true makes it "
                        "again")
            return {"instance": str(destination), "template": key, "reused": True, "built": False}
        if not fresh:
            raise ParamError(f"instance {name} was built from template {current}, not {key} (another launch or other "
                             "parameters); fresh=true replaces it, or choose another name")
        store.remove(destination)

    built = False
    if store.template(key) is None:
        staging = root / ".store" / "staging" / f"{key}-{os.getpid()}"
        if staging.exists():
            remove_tree(staging)
        staging.mkdir(parents=True)
        try:
            folders = [sourced(carried, o["tree"]) if carried else
                       export_tree(machine.repository(o["project"]), o["tree"], exports / o["tree"]) for o in overlays]
            receipt = build_template(ctx, staging, data=data, doc=doc, base_name=base, base=base_doc,
                                     configure=hook, params=merged, eula=eula, key=key, machine=machine,
                                     overlays=folders, base_source=base_source, configure_from=configure_from)
            (staging / RECEIPT).write_text(json.dumps(receipt, indent=1) + "\n", encoding="utf-8")
            try:
                store.adopt_template(staging, key, inputs)
                built = True
            except StoreError:  # another build of the same key adopted first: its template is the same
                pass
        finally:
            if staging.exists():
                remove_tree(staging)
    ctx.progress("instance", msg=f"{name} from template {key}")
    copied = store.materialize(key, destination)
    receipt = json.loads((destination / RECEIPT).read_text(encoding="utf-8"))
    receipt["name"] = name
    (destination / RECEIPT).write_text(json.dumps(receipt, indent=1) + "\n", encoding="utf-8")
    ctx.output("instance.json", destination / RECEIPT)
    ctx.summary(f"instance {name} from launch {doc['project']}:{doc['launch']} at {str(doc['commit'])[:12]} "
                f"({len(doc['members'])} members, {'a new' if built else 'the kept'} template {key}): "
                f"{copied['linked_bytes'] >> 20} MiB linked, {copied['copied_bytes'] >> 20} MiB copied")
    return {"instance": str(destination), "template": key, "reused": False, "built": built}
