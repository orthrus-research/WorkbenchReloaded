"""lab.pack: a release-exact modpack from its pack file, every file checked by the hash the pack names.

A **Modrinth pack** (``.mrpack``) holds ``modrinth.index.json``, naming each file by its path, download URLs, sizes and
hashes, which sides need it (``env``) and the game and loader versions it targets, beside ``overrides/`` (and
``client-overrides/`` or ``server-overrides/``). Each file for the chosen side (``optional`` ones only when asked) is
checked by its SHA-512 and downloaded only from a host ``[modules.lab] pack_hosts`` allows.

A **CurseForge pack** (a .zip holding ``manifest.json``) names each mod by project and file id; the CurseForge API
(``curseforge_api``, with the machine's secret ``curseforge_api_key``) gives each one's file name, download URL and
SHA-1, and it is downloaded only from a host ``curseforge_hosts`` allows. Such a pack says nothing about sides, so
every required file comes, and an author who does not allow other tools to download a file leaves a finding naming
it, to be downloaded by hand.

A file is taken from the pack cache by its hash before anything is downloaded, and bytes other than the hash are
never kept; a path that would leave the pack folder is refused. The folder (``pack/`` in the out folder) and
``receipt.json`` (each file, its hash and where it came from, the overrides, the versions targeted) are the result.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

from wb_core.module import Context, EnvironmentProblem, ParamError

from .assemble import USER_AGENT, download

SCHEMA = "wb.lab.pack/1"
INDEX = "modrinth.index.json"
MANIFEST = "manifest.json"
LENGTHS = {"sha512": 128, "sha1": 40}


@dataclass
class Item:
    path: str
    algo: str            # sha512 or sha1
    digest: str
    urls: list[str]
    hosts: list[str]     # the hosts it may come from


def safe(path: str) -> str:
    """A pack path, refused when it is absolute or climbs out of the pack folder."""
    pure = PurePosixPath(path.replace("\\", "/"))
    if not path or pure.is_absolute() or ".." in pure.parts or ":" in pure.parts[0]:
        raise ParamError(f"the pack names a path that leaves its folder: {path!r}")
    return pure.as_posix()


def _cache(ctx: Context) -> Path:
    configured = str(ctx.config.get("pack_cache") or "")
    return Path(configured) if configured else Path(os.environ.get("WB_STATE_DIR") or "/var/lib/wb") / "cache" / "packs"


def _kept(cache: Path, item: Item) -> Path:
    """Where the cache keeps a file: by SHA-512 at its top, by SHA-1 under sha1/."""
    return cache / item.digest if item.algo == "sha512" else cache / item.algo / item.digest


def _modrinth(ctx: Context, archive: zipfile.ZipFile, side: str, optional: bool):
    try:
        index = json.loads(archive.read(INDEX))
    except ValueError as error:
        raise ParamError(f"{INDEX} is not JSON ({error})") from None
    if index.get("game") != "minecraft" or index.get("formatVersion") != 1:
        raise ParamError(f"{INDEX} is not a format-1 Minecraft pack index")
    hosts = [h.lower() for h in ctx.config.get("pack_hosts") or []]
    wanted, skipped = [], []
    for entry in index.get("files") or []:
        need = (entry.get("env") or {}).get(side, "required")
        if need == "unsupported" or (need == "optional" and not optional):
            skipped.append(entry.get("path"))
            continue
        wanted.append(Item(str(entry.get("path") or ""), "sha512",
                           str((entry.get("hashes") or {}).get("sha512") or "").lower(),
                           list(entry.get("downloads") or []), hosts))
    meta = {"format": "modrinth", "name": index.get("name"), "version": index.get("versionId"),
            "dependencies": index.get("dependencies") or {}}
    return wanted, skipped, meta, ["overrides/", f"{side}-overrides/"], "pack_hosts"


def _api_get(url: str, key: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "x-api-key": key,
                                                   "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            raise EnvironmentProblem(f"the CurseForge API refused the key ({error.code})") from None
        raise EnvironmentProblem(f"the CurseForge API answered {error.code} for {url}") from None
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise EnvironmentProblem(f"could not ask the CurseForge API ({error})") from None


def _curseforge(ctx: Context, archive: zipfile.ZipFile, optional: bool, problems: list[str]):
    try:
        manifest = json.loads(archive.read(MANIFEST))
    except ValueError as error:
        raise ParamError(f"{MANIFEST} is not JSON ({error})") from None
    if manifest.get("manifestType") != "minecraftModpack":
        raise ParamError(f"{MANIFEST} is not a CurseForge modpack manifest")
    key = ctx.secret("curseforge_api_key")
    api = str(ctx.config.get("curseforge_api") or "").rstrip("/")
    hosts = [h.lower() for h in ctx.config.get("curseforge_hosts") or []]
    wanted, skipped = [], []
    for entry in manifest.get("files") or []:
        project, file = entry.get("projectID"), entry.get("fileID")
        if not entry.get("required", True) and not optional:
            skipped.append(f"{project}/{file}")
            continue
        data = _api_get(f"{api}/v1/mods/{project}/files/{file}", key).get("data") or {}
        name, url = data.get("fileName"), data.get("downloadUrl")
        sha1 = next((h.get("value") for h in data.get("hashes") or [] if h.get("algo") == 1), "")
        if not url:
            problems.append(f"project {project} file {file} ({name or 'unnamed'}): its author does not allow other "
                            "tools to download it; download it by hand")
            continue
        wanted.append(Item(f"mods/{name}", "sha1", str(sha1).lower(), [url], hosts))
    minecraft = manifest.get("minecraft") or {}
    loaders = {loader.get("id"): bool(loader.get("primary")) for loader in minecraft.get("modLoaders") or []}
    meta = {"format": "curseforge", "name": manifest.get("name"), "version": manifest.get("version"),
            "dependencies": {"minecraft": minecraft.get("version"), "loaders": loaders}}
    overrides = str(manifest.get("overrides") or "overrides").strip("/") + "/"
    return wanted, skipped, meta, [overrides], "curseforge_hosts"


def pack(ctx: Context, pack: str, side: str, optional: bool):
    source = Path(pack)
    try:
        archive = zipfile.ZipFile(source)
    except (OSError, zipfile.BadZipFile) as error:
        raise ParamError(f"pack: {pack} is not a pack file ({error})") from None
    problems: list[str] = []
    records: list[dict] = []
    lock = threading.Lock()
    with archive:
        names = set(archive.namelist())
        if INDEX in names:
            wanted, skipped, meta, override_folders, hosts_key = _modrinth(ctx, archive, side, optional)
        elif MANIFEST in names:
            wanted, skipped, meta, override_folders, hosts_key = _curseforge(ctx, archive, optional, problems)
        else:
            raise ParamError(f"{source.name} holds neither {INDEX} (Modrinth) nor {MANIFEST} (CurseForge)")
        cache = _cache(ctx)
        target = ctx.out / "pack"
        if target.exists() and any(target.iterdir()):
            raise ParamError(f"{ctx.host_path(target)} already holds files; a pack goes into a fresh out folder")

        def fetch(item: Item) -> None:
            path = safe(item.path)
            label = item.algo.upper().replace("SHA", "SHA-")
            if len(item.digest) != LENGTHS[item.algo]:
                with lock:
                    problems.append(f"{path} names no {label}, so it cannot be checked")
                return
            kept, origin = _kept(cache, item), "cache"
            if not (kept.is_file() and hashlib.new(item.algo, kept.read_bytes()).hexdigest() == item.digest):
                urls = [u for u in item.urls if (urlparse(u).hostname or "").lower() in item.hosts]
                if not urls:
                    with lock:
                        problems.append(f"{path}: no download from a host {hosts_key} allows "
                                        f"({', '.join(urlparse(u).hostname or u for u in item.urls) or 'none named'})")
                    return
                data, last = None, ""
                for url in urls:
                    try:
                        data, origin = download(url), url
                        break
                    except EnvironmentProblem as error:
                        last = str(error)
                if data is None:
                    with lock:
                        problems.append(f"{path}: {last}")
                    return
                if hashlib.new(item.algo, data).hexdigest() != item.digest:
                    with lock:
                        problems.append(f"{origin} gave other bytes than the {label} the pack names for {path}; "
                                        "nothing was kept")
                    return
                kept.parent.mkdir(parents=True, exist_ok=True)
                part = kept.with_name(f"{item.digest}.{os.getpid()}-{threading.get_ident()}.part")
                part.write_bytes(data)
                os.replace(part, kept)
            placed = target / path
            placed.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(kept, placed)
            with lock:
                records.append({"path": path, item.algo: item.digest, "bytes": placed.stat().st_size, "from": origin})

        ctx.progress("files", msg=f"{len(wanted)} file(s) for the {side}")
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(fetch, wanted))
        overrides = 0
        for folder in override_folders:
            for name in sorted(names):
                if name.startswith(folder) and not name.endswith("/"):
                    placed = target / safe(name[len(folder):])
                    placed.parent.mkdir(parents=True, exist_ok=True)
                    placed.write_bytes(archive.read(name))
                    overrides += 1
    if problems:
        for problem in sorted(problems):
            ctx.finding(problem)
        return {"pack": None, "problems": sorted(problems)}
    receipt = {"schema": SCHEMA, "pack": ctx.host_path(source), **meta, "side": side,
               "files": sorted(records, key=lambda r: r["path"]), "skipped": sorted(p for p in skipped if p),
               "overrides": overrides}
    (ctx.out / "receipt.json").write_text(json.dumps(receipt, indent=1) + "\n", encoding="utf-8")
    ctx.output("receipt", ctx.out / "receipt.json")
    ctx.output("pack", target)
    downloaded = sum(1 for r in records if r["from"] != "cache")
    ctx.summary(f"{meta['name']} {meta['version']} for the {side}: {len(records)} file(s) ({downloaded} downloaded), "
                f"{overrides} override(s), {len(skipped)} left out")
    return {"pack": ctx.host_path(target), "files": len(records), "problems": []}
