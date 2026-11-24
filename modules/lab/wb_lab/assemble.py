"""lab.assemble: a game runtime made from launcher metadata, so a client can be started without a launcher.

A launcher instance (a Prism or MultiMC instance folder, or the .zip it exports) names its components in order in
``mmc-pack.json``, with a patch for each component it changes (``patches/<uid>.json``) and the local libraries its
patches mark ``"MMC-hint": "local"`` (``libraries/``). A component the instance carries no patch for is read from
``[modules.lab] metadata``, a URL template with ``{uid}`` and ``{version}``.

Each library's ``rules`` are followed for the target system and architecture, and a later component's library
replaces an earlier one of the same group, artifact and classifier. Every file is fetched against the SHA-1 its
metadata names, into a cache shared by every runtime (``[modules.lab] runtime_cache``): the libraries, the platform's
natives, the client JAR and, unless ``assets=false``, the asset index and its objects. A file is looked for first in
the cache, then by name in the ``library_candidates`` folders (objects in ``asset_candidates``), and only then
downloaded; bytes other than the named SHA-1 are never kept. The natives are unpacked into the out folder's
``natives/``.

``runtime.json`` (``wb.lab.runtime.v1``) says how to start the client: the main class, the tweakers, the arguments the
components add, the libraries in class-path order (each with its SHA-1), the client JAR, the natives and the assets.
Its paths are host paths when the cache lies under a configured root. Nothing is started.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import threading
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from wb_core.module import Context, EnvironmentProblem, ParamError

SCHEMA = "wb.lab.runtime.v1"
GAME_FOLDERS = (".minecraft", "minecraft")
GAME_PARTS = ("mods", "config", "resourcepacks", "shaderpacks", "scripts", "groovy", "options.txt")
NATIVE_SUFFIXES = (".dll", ".so", ".dylib", ".jnilib")
SYSTEMS = {"Windows": "windows", "Linux": "linux", "Darwin": "osx"}
ARCHES = {"x86": "x86", "i386": "x86", "i686": "x86", "x86_64": "x86_64", "amd64": "x86_64", "arm64": "aarch64",
          "aarch64": "aarch64"}
USER_AGENT = "workbench-lab-assemble"
TIMEOUT = 90


class Mismatch(Exception):
    """Bytes other than the SHA-1 the metadata names."""


@dataclass
class Instance:
    components: list[dict]    # {"uid", "version"} in the instance's order
    patches: dict[str, dict]  # uid -> the instance's patch
    local: dict[str, bytes]   # file name -> a library the instance carries
    texts: dict[str, bytes]   # uid -> the patch as written, for its digest


@dataclass
class Artifact:
    name: str                 # group:artifact:version[:classifier]
    path: str                 # under the cache's libraries/ (Maven layout)
    sha1: str
    url: str | None = None
    data: bytes | None = None  # a library the instance carries
    native: bool = False


# ---------------------------------------------------------------------------------------------
# the instance

def _prefix(names: list[str]) -> str:
    """An exported instance may hold its files under one top folder."""
    if "mmc-pack.json" in names or any(n.startswith("patches/") for n in names):
        return ""
    tops = {n.split("/", 1)[0] + "/" for n in names if "/" in n}
    for top in sorted(tops):
        if top + "mmc-pack.json" in names or any(n.startswith(top + "patches/") for n in names):
            return top
    return ""


def read_instance(path: Path) -> Instance:
    files: dict[str, bytes] = {}
    if path.is_file():
        try:
            with zipfile.ZipFile(path) as archive:
                names = [n for n in archive.namelist() if not n.endswith("/")]
                prefix = _prefix(names)
                for name in names:
                    relative = name[len(prefix):] if name.startswith(prefix) else None
                    if relative and (relative == "mmc-pack.json" or relative.startswith(("patches/", "libraries/"))):
                        files[relative] = archive.read(name)
        except zipfile.BadZipFile:
            raise ParamError(f"{path.name} is neither an instance folder nor an instance .zip") from None
    elif path.is_dir():
        if (path / "mmc-pack.json").is_file():
            files["mmc-pack.json"] = (path / "mmc-pack.json").read_bytes()
        for folder, suffix in (("patches", ".json"), ("libraries", ".jar")):
            for item in sorted((path / folder).glob("*" + suffix)) if (path / folder).is_dir() else []:
                files[f"{folder}/{item.name}"] = item.read_bytes()
    else:
        raise ParamError(f"no instance at {path}")
    patches, texts = {}, {}
    for name, data in files.items():
        if name.startswith("patches/") and name.endswith(".json"):
            try:
                patch = json.loads(data)
            except ValueError as error:
                raise ParamError(f"{name} is not JSON ({error})") from None
            uid = patch.get("uid") or Path(name).stem
            patches[uid], texts[uid] = patch, data
    local = {Path(n).name: d for n, d in files.items() if n.startswith("libraries/")}
    if "mmc-pack.json" in files:
        try:
            pack = json.loads(files["mmc-pack.json"])
        except ValueError as error:
            raise ParamError(f"mmc-pack.json is not JSON ({error})") from None
        components = [{"uid": c["uid"], "version": str(c.get("version") or "")} for c in pack.get("components", [])
                      if c.get("uid")]
    else:  # no pack file: the patches, the game first
        components = [{"uid": uid, "version": str(p.get("version") or "")} for uid, p in patches.items()]
        components.sort(key=lambda c: c["uid"] != "net.minecraft")
    if not components:
        raise ParamError(f"{path.name} names no components (no mmc-pack.json and no patches)")
    return Instance(components, patches, local, texts)


# ---------------------------------------------------------------------------------------------
# libraries

def maven_path(name: str) -> str:
    """group:artifact:version[:classifier][@ext] -> its path in a Maven layout."""
    coordinates, _, extension = name.partition("@")
    parts = coordinates.split(":")
    if len(parts) < 3:
        raise ValueError(f"library name {name!r} is not group:artifact:version")
    group, artifact, version = parts[:3]
    classifier = f"-{parts[3]}" if len(parts) > 3 and parts[3] else ""
    return f"{group.replace('.', '/')}/{artifact}/{version}/{artifact}-{version}{classifier}.{extension or 'jar'}"


def _arch(value: str) -> str:
    return ARCHES.get(value.lower(), value.lower())


def allowed(library: dict, system: str, arch: str, here: bool) -> bool:
    """Whether a library belongs to a runtime for ``system`` and ``arch``, by its rules. An OS version rule can only be
    judged on the machine itself (``here``); elsewhere it is taken as matching."""
    rules = library.get("rules") or []
    result = not rules
    for rule in rules:
        spec = rule.get("os") or {}
        match = True
        if "name" in spec:
            match = spec["name"] == system or (arch == "aarch64" and spec["name"] == f"{system}-arm64")
        if "arch" in spec:
            match = match and _arch(str(spec["arch"])) == arch
        if "version" in spec and here:
            match = match and bool(re.search(str(spec["version"]), platform.release()))
        if match:
            result = rule.get("action") == "allow"
    parts = str(library.get("name", "")).split(":")
    classifier = parts[3] if len(parts) > 3 else ""
    if classifier.startswith("natives-"):  # metadata may list a native per architecture under one OS rule
        if arch == "x86_64" and (classifier.endswith("-x86") or "-arm" in classifier):
            return False
        if arch == "aarch64" and not classifier.endswith("-arm64"):
            return False
    return result


def merged(components: list[dict], system: str, arch: str, here: bool) -> list[dict]:
    """The libraries of every component for the target: a later component's library replaces an earlier one of the
    same group, artifact and classifier; the class path takes the later components' libraries first."""
    selected: dict[tuple, dict] = {}
    priority: dict[tuple, int] = {}
    for index, component in enumerate(components):
        for library in list(component.get("libraries") or []) + list(component.get("+libraries") or []):
            if not library.get("name") or not allowed(library, system, arch, here):
                continue
            parts = library["name"].split(":")
            key = (parts[0], parts[1], ":".join(parts[3:]))
            prior = selected.get(key)
            if prior and prior["name"] == library["name"]:  # the same library again: what it adds is kept
                library = {**prior, **library,
                           "downloads": {**(prior.get("downloads") or {}), **(library.get("downloads") or {})}}
            selected[key], priority[key] = library, index
    return [selected[k] for k in sorted(selected, key=lambda k: priority[k], reverse=True)]


def artifacts(libraries: list[dict], instance: Instance, system: str, arch: str) -> tuple[list[Artifact], list[str]]:
    out, problems = [], []
    native_key = f"{system}-arm64" if arch == "aarch64" else system
    for library in libraries:
        name, downloads = library["name"], library.get("downloads") or {}
        if library.get("MMC-hint") == "local":
            parts = name.split(":")
            file = f"{parts[1]}-{parts[2]}" + (f"-{parts[3]}" if len(parts) > 3 else "") + ".jar"
            data = instance.local.get(file)
            if data is None:
                problems.append(f"{name} is marked local, and the instance carries no libraries/{file}")
                continue
            out.append(Artifact(name, maven_path(name), hashlib.sha1(data).hexdigest(), data=data,
                                native=":natives-" in name))
        elif "artifact" in downloads:
            item = downloads["artifact"]
            if not item.get("sha1"):
                problems.append(f"{name} names no SHA-1, so it cannot be checked")
            else:
                out.append(Artifact(name, item.get("path") or maven_path(name), item["sha1"].lower(), item.get("url"),
                                    native=":natives-" in name))
        elif not library.get("natives"):
            problems.append(f"{name} names no download with a SHA-1")
        classifier = (library.get("natives") or {}).get(native_key)
        if classifier:  # natives in a classifier of the library (the older layout)
            classifier = classifier.replace("${arch}", "32" if arch == "x86" else "64")
            item = (downloads.get("classifiers") or {}).get(classifier)
            if not item or not item.get("sha1"):
                problems.append(f"{name} names natives {classifier} without a download and its SHA-1")
            else:
                full = f"{name}:{classifier}"
                out.append(Artifact(full, item.get("path") or maven_path(full), item["sha1"].lower(), item.get("url"),
                                    native=True))
    return out, problems


# ---------------------------------------------------------------------------------------------
# fetching

def sha1_file(path: Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _write(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(f"{target.name}.{os.getpid()}-{threading.get_ident()}.part")
    part.write_bytes(data)
    os.replace(part, target)


def download(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.read()
    except (urllib.error.URLError, OSError) as error:
        raise EnvironmentProblem(f"could not download {url}: {error}") from None


class Fetcher:
    """Files by SHA-1: the cache, then candidate folders, then the network."""

    def __init__(self, library_folders: list[Path]):
        self.library_folders = library_folders
        self._by_name: dict[str, list[Path]] | None = None
        self._lock = threading.Lock()
        self.counts = {"cached": 0, "candidate": 0, "instance": 0, "downloaded": 0}

    def _index(self) -> dict[str, list[Path]]:
        with self._lock:
            if self._by_name is None:
                self._by_name = {}
                for folder in self.library_folders:
                    if folder.is_dir():
                        for path in folder.rglob("*.jar"):
                            self._by_name.setdefault(path.name, []).append(path)
            return self._by_name

    def _count(self, how: str) -> str:
        with self._lock:
            self.counts[how] += 1
        return how

    def fetch(self, target: Path, sha1: str, url: str | None, *, data: bytes | None = None,
              candidates: list[Path] | None = None) -> str:
        if target.is_file() and sha1_file(target) == sha1:
            return self._count("cached")
        for candidate in (candidates if candidates is not None else self._index().get(target.name, [])):
            if candidate.is_file() and sha1_file(candidate) == sha1:
                target.parent.mkdir(parents=True, exist_ok=True)
                part = target.with_name(f"{target.name}.{os.getpid()}-{threading.get_ident()}.part")
                shutil.copyfile(candidate, part)
                os.replace(part, target)
                return self._count("candidate")
        if data is not None:
            _write(target, data)
            return self._count("instance")
        if not url:
            raise Mismatch(f"{target.name} is in no cache or candidate folder, and its metadata names no URL")
        body = download(url)
        actual = hashlib.sha1(body).hexdigest()
        if actual != sha1:
            raise Mismatch(f"{url} gave SHA-1 {actual[:12]}, not the {sha1[:12]} its metadata names; nothing was kept")
        _write(target, body)
        return self._count("downloaded")


# ---------------------------------------------------------------------------------------------
# the command

def _cache(ctx: Context) -> Path:
    configured = str(ctx.config.get("runtime_cache") or "")
    if configured:
        return Path(configured)
    return Path(os.environ.get("WB_STATE_DIR") or "/var/lib/wb") / "cache" / "runtime"


def _component(ctx: Context, instance: Instance, cache: Path, uid: str, version: str) -> tuple[dict, str, str]:
    """A component as the instance patches it, else from the metadata (kept in the cache by uid and version)."""
    if uid in instance.patches:
        return instance.patches[uid], "instance", hashlib.sha256(instance.texts[uid]).hexdigest()
    template = str(ctx.config.get("metadata") or "")
    kept = cache / "meta" / uid / f"{version}.json"
    if kept.is_file():  # read once from the metadata, then kept by uid and version
        data = kept.read_bytes()
    elif not template:
        raise ParamError(f"the instance carries no patch for {uid} {version}, and [modules.lab] metadata (a URL "
                         "template with {uid} and {version}) is not set")
    else:
        data = download(template.replace("{uid}", uid).replace("{version}", version))
        try:
            json.loads(data)
        except ValueError as error:
            raise EnvironmentProblem(f"the metadata for {uid} {version} is not JSON ({error})") from None
        _write(kept, data)
    return json.loads(data), "metadata", hashlib.sha256(data).hexdigest()


def copy_game(source: Path, target: Path) -> dict[str, dict]:
    """The instance's own game folder (its .minecraft): the parts that make the game what it is (mods, config, packs,
    scripts, options.txt), never its worlds, logs or screenshots. The instance is only read. Returns each file copied
    by its POSIX path, with its size and SHA-256."""
    files: dict[str, bytes] = {}
    if source.is_file():
        with zipfile.ZipFile(source) as archive:
            names = [n for n in archive.namelist() if not n.endswith("/")]
            prefix = _prefix(names)
            for name in names:
                relative = name[len(prefix):] if name.startswith(prefix) else name
                top, _, rest = relative.partition("/")
                if top in GAME_FOLDERS and rest and rest.split("/", 1)[0] in GAME_PARTS:
                    files[rest] = archive.read(name)
    else:
        game = next((source / folder for folder in GAME_FOLDERS if (source / folder).is_dir()), None)
        for part in GAME_PARTS if game else ():
            path = game / part
            items = [path] if path.is_file() else sorted(p for p in path.rglob("*") if p.is_file()) if path.is_dir() else []
            for item in items:
                files[item.relative_to(game).as_posix()] = item.read_bytes()
    listed = {}
    for relative, data in sorted(files.items()):
        if ".." in relative.split("/"):
            continue
        placed = target / relative
        placed.parent.mkdir(parents=True, exist_ok=True)
        placed.write_bytes(data)
        listed[relative] = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    return listed


def _unpack_natives(paths: list[Path], target: Path) -> list[str]:
    shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True)
    unpacked = []
    for path in paths:
        with zipfile.ZipFile(path) as archive:
            for member in archive.namelist():
                if member.startswith("META-INF/") or not member.lower().endswith(NATIVE_SUFFIXES):
                    continue
                name = Path(member).name
                (target / name).write_bytes(archive.read(member))
                unpacked.append(name)
    return sorted(set(unpacked))


@dataclass
class Runtime:
    """A runtime as it was assembled, with local paths: what runtime.json and lab.build's receipt are written from."""
    system: str
    arch: str
    components: list[dict]    # {uid, version, source, sha256} in the instance's order
    patches: list[dict]       # the components themselves, in that order
    items: list[Artifact]     # the libraries in class-path order
    cache: Path
    client: Path
    client_sha1: str
    natives: Path
    native_files: list[str]
    asset_index: dict | None  # {id, path, sha1, entries, objects}
    fetched: dict

    @property
    def main_class(self) -> str | None:
        return next((c["mainClass"] for c in reversed(self.patches) if c.get("mainClass")), None)

    @property
    def tweakers(self) -> list[str]:
        return [t for c in self.patches for t in c.get("+tweakers") or []]

    def library(self, item: Artifact) -> Path:
        return self.cache / "libraries" / item.path


def host_system() -> tuple[str, str]:
    return SYSTEMS.get(platform.system(), platform.system().lower()), _arch(platform.machine())


def build_runtime(ctx: Context, source: Path, cache: Path, natives_dir: Path, *, system: str = "", arch: str = "",
                  assets: bool = True, library_folders: list[Path] | None = None,
                  asset_folders: list[Path] | None = None) -> tuple[Runtime | None, list[str], dict | None]:
    """Assembles the runtime of the launcher instance ``source`` into ``cache``, its natives unpacked into
    ``natives_dir``: (the runtime, [], counts), or (None, the problems, each already a finding, and the fetch counts
    when fetching had begun)."""
    here_system, here_arch = host_system()
    system, arch = system or here_system, arch or here_arch
    here = (system, arch) == (here_system, here_arch)
    read = read_instance(source)
    try:
        cache.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise EnvironmentProblem(f"the runtime cache {ctx.host_path(cache)} cannot be made ({error.strerror or error}): "
                                 "create it, or set [modules.lab] runtime_cache to a folder this job can write") from None
    components, recorded = [], []
    for item in read.components:
        component, origin, digest = _component(ctx, read, cache, item["uid"], item["version"])
        components.append(component)
        recorded.append({"uid": item["uid"], "version": item["version"] or str(component.get("version") or ""),
                         "source": origin, "sha256": digest})
    libraries = merged(components, system, arch, here)
    items, problems = artifacts(libraries, read, system, arch)
    game = next((c for c in components if c.get("uid") == "net.minecraft"), None)
    main_jar = ((game or {}).get("mainJar") or {})
    jar = (main_jar.get("downloads") or {}).get("artifact") or {}
    if not jar.get("sha1"):
        problems.append("no component gives the client JAR with its SHA-1 (net.minecraft's mainJar)")
    if problems:
        for problem in problems:
            ctx.finding(problem)
        return None, problems, None

    if library_folders is None:
        library_folders = [Path(p) for p in ctx.config.get("library_candidates") or []]
    if asset_folders is None:
        asset_folders = [Path(p) for p in ctx.config.get("asset_candidates") or []]
    fetcher = Fetcher(library_folders)
    failures: list[str] = []

    def get(item: Artifact) -> None:
        try:
            fetcher.fetch(cache / "libraries" / item.path, item.sha1, item.url, data=item.data)
        except Mismatch as error:
            failures.append(str(error))

    ctx.progress("libraries", msg=f"{len(items)} file(s) for {system} {arch}")
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(get, items))
    client_name = main_jar.get("name") or f"com.mojang:minecraft:{(game or {}).get('version', 'unknown')}:client"
    client = cache / "libraries" / (jar.get("path") or maven_path(client_name))
    try:
        fetcher.fetch(client, jar["sha1"].lower(), jar.get("url"))
    except Mismatch as error:
        failures.append(str(error))

    asset_index = None
    if assets and (game or {}).get("assetIndex"):
        index = game["assetIndex"]
        index_path = cache / "assets" / "indexes" / f"{index['id']}.json"
        try:
            fetcher.fetch(index_path, str(index["sha1"]).lower(), index.get("url"),
                          candidates=[f / "indexes" / index_path.name for f in asset_folders])
        except Mismatch as error:
            failures.append(str(error))
        if index_path.is_file():
            entries = json.loads(index_path.read_text(encoding="utf-8")).get("objects", {})
            objects = {o["hash"] for o in entries.values()}
            base = str(ctx.config.get("asset_base") or "").rstrip("/") + "/"
            ctx.progress("assets", msg=f"{len(objects)} object(s) of index {index['id']}")

            def get_object(digest: str) -> None:
                relative = f"{digest[:2]}/{digest}"
                try:
                    fetcher.fetch(cache / "assets" / "objects" / relative, digest, base + relative,
                                  candidates=[f / "objects" / relative for f in asset_folders])
                except Mismatch as error:
                    failures.append(str(error))

            with ThreadPoolExecutor(max_workers=16) as pool:
                list(pool.map(get_object, sorted(objects)))
            asset_index = {"id": index["id"], "path": index_path, "sha1": str(index["sha1"]).lower(),
                           "entries": len(entries), "objects": len(objects)}
    if failures:
        for failure in sorted(failures):
            ctx.finding(failure)
        return None, sorted(failures), fetcher.counts

    unpacked = _unpack_natives([cache / "libraries" / i.path for i in items if i.native], natives_dir)
    return Runtime(system, arch, recorded, components, items, cache, client, jar["sha1"].lower(), natives_dir, unpacked,
                   asset_index, fetcher.counts), [], fetcher.counts


def assemble(ctx: Context, instance: str, system: str = "", arch: str = "", assets: bool = True, game: bool = False):
    source = Path(instance)
    natives_dir = ctx.out / "natives"
    built, problems, counts = build_runtime(ctx, source, _cache(ctx), natives_dir, system=system, arch=arch,
                                            assets=assets)
    if built is None:
        return {"runtime": None, "problems": problems, **({"fetched": counts} if counts is not None else {})}
    library_records = [{"name": i.name, "path": ctx.host_path(built.library(i)), "sha1": i.sha1,
                        "native": i.native} for i in built.items]
    asset_record = None
    if built.asset_index:
        asset_record = {"root": ctx.host_path(built.cache / "assets"), "index": built.asset_index["id"],
                        "entries": built.asset_index["entries"], "objects": built.asset_index["objects"]}
    components = built.patches
    runtime = {
        "schema": SCHEMA,
        "instance": ctx.host_path(source),
        "system": built.system,
        "arch": built.arch,
        "components": built.components,
        "main_class": built.main_class,
        "tweakers": built.tweakers,
        "jvm_args": [a for c in components for a in c.get("+jvmArgs") or []],
        "game_arguments": next((c["minecraftArguments"] for c in reversed(components)
                                if c.get("minecraftArguments")), None),
        "traits": sorted({t for c in components for t in c.get("+traits") or []}),
        "libraries": library_records,
        "client": {"path": ctx.host_path(built.client), "sha1": built.client_sha1},
        "classpath": [r["path"] for r in library_records] + [ctx.host_path(built.client)],
        "natives": ctx.host_path(natives_dir),
        "native_files": built.native_files,
        "assets": asset_record,
        "fetched": built.fetched,
        "game": None,
    }
    if game:
        copied = copy_game(source, ctx.out / "game")
        inventory = json.dumps({"schema": SCHEMA, "files": copied}, indent=1) + "\n"
        (ctx.out / "game-inventory.json").write_text(inventory, encoding="utf-8")
        runtime["game"] = {"path": ctx.host_path(ctx.out / "game"), "files": len(copied)}
        ctx.output("game", ctx.out / "game")
        if not copied:
            ctx.finding("game=true, but the instance holds no game folder (.minecraft) with mods, config or packs")
    target = ctx.out / "runtime.json"
    target.write_text(json.dumps(runtime, indent=1) + "\n", encoding="utf-8")
    ctx.output("runtime.json", target)
    ctx.output("natives", natives_dir)
    ctx.summary(f"runtime for {built.system} {built.arch}: {len(built.items)} librar"
                f"{'y' if len(built.items) == 1 else 'ies'}, main class {runtime['main_class']}, "
                f"{asset_record['objects'] if asset_record else 0} asset object(s); {counts['downloaded']} downloaded, "
                f"{counts['cached']} cached, {counts['candidate']} from candidates")
    return {"runtime": ctx.host_path(target), "main_class": runtime["main_class"], "libraries": len(built.items),
            "fetched": counts, "problems": []}
