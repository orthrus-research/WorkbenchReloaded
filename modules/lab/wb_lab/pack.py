"""lab.pack: a release-exact modpack from its Modrinth pack file (.mrpack), every file checked by its SHA-512.

A pack file holds ``modrinth.index.json``, naming each file the pack needs by its path, download URLs, sizes and
hashes, which sides need it (``env``) and the game and loader versions it targets, beside ``overrides/`` (and
``client-overrides/`` or ``server-overrides/``) laid over the result. Each file for the chosen side (``optional``
ones only when asked) is taken from the pack cache by its SHA-512, else downloaded from the first of its URLs whose
host ``[modules.lab] pack_hosts`` allows; bytes other than the SHA-512 are never kept. A path that would leave the
pack folder is refused. The folder (``pack/`` in the out folder) and ``receipt.json`` (each file, its hash and where
it came from, the overrides, the targeted versions) are what a session or a launcher takes.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

from wb_core.module import Context, EnvironmentProblem, ParamError

from .assemble import download

SCHEMA = "wb.lab.pack/1"
INDEX = "modrinth.index.json"


def safe(path: str) -> str:
    """A pack path, refused when it is absolute or climbs out of the pack folder."""
    pure = PurePosixPath(path.replace("\\", "/"))
    if not path or pure.is_absolute() or ".." in pure.parts or ":" in pure.parts[0]:
        raise ParamError(f"the pack names a path that leaves its folder: {path!r}")
    return pure.as_posix()


def _sha512(data: bytes) -> str:
    return hashlib.sha512(data).hexdigest()


def _cache(ctx: Context) -> Path:
    configured = str(ctx.config.get("pack_cache") or "")
    return Path(configured) if configured else Path(os.environ.get("WB_STATE_DIR") or "/var/lib/wb") / "cache" / "packs"


def pack(ctx: Context, pack: str, side: str, optional: bool):
    source = Path(pack)
    try:
        archive = zipfile.ZipFile(source)
    except (OSError, zipfile.BadZipFile) as error:
        raise ParamError(f"pack: {pack} is not a pack file ({error})") from None
    with archive:
        try:
            index = json.loads(archive.read(INDEX))
        except KeyError:
            raise ParamError(f"{source.name} holds no {INDEX}") from None
        except ValueError as error:
            raise ParamError(f"{INDEX} is not JSON ({error})") from None
        if index.get("game") != "minecraft" or index.get("formatVersion") != 1:
            raise ParamError(f"{INDEX} is not a format-1 Minecraft pack index")
        hosts = [h.lower() for h in ctx.config.get("pack_hosts") or []]
        cache = _cache(ctx)
        target = ctx.out / "pack"
        if target.exists() and any(target.iterdir()):
            raise ParamError(f"{ctx.host_path(target)} already holds files; a pack goes into a fresh out folder")
        wanted, skipped = [], []
        for item in index.get("files") or []:
            need = (item.get("env") or {}).get(side, "required")
            if need == "unsupported" or (need == "optional" and not optional):
                skipped.append(item.get("path"))
                continue
            wanted.append(item)
        problems: list[str] = []
        records: list[dict] = []
        lock = threading.Lock()

        def fetch(item: dict) -> None:
            path = safe(str(item.get("path") or ""))
            digest = str((item.get("hashes") or {}).get("sha512") or "").lower()
            if len(digest) != 128:
                with lock:
                    problems.append(f"{path} names no SHA-512, so it cannot be checked")
                return
            kept, origin = cache / digest, "cache"
            if not (kept.is_file() and _sha512(kept.read_bytes()) == digest):
                urls = [u for u in item.get("downloads") or [] if (urlparse(u).hostname or "").lower() in hosts]
                if not urls:
                    with lock:
                        problems.append(f"{path}: no download from a host pack_hosts allows "
                                        f"({', '.join(urlparse(u).hostname or u for u in item.get('downloads') or []) or 'none named'})")
                    return
                data, origin = None, urls[0]
                for url in urls:
                    try:
                        data = download(url)
                        origin = url
                        break
                    except EnvironmentProblem as error:
                        last = str(error)
                if data is None:
                    with lock:
                        problems.append(f"{path}: {last}")
                    return
                if _sha512(data) != digest:
                    with lock:
                        problems.append(f"{origin} gave other bytes than the SHA-512 the pack names for {path}; "
                                        "nothing was kept")
                    return
                kept.parent.mkdir(parents=True, exist_ok=True)
                part = kept.with_name(f"{digest}.{os.getpid()}-{threading.get_ident()}.part")
                part.write_bytes(data)
                os.replace(part, kept)
            placed = target / path
            placed.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(kept, placed)
            with lock:
                records.append({"path": path, "sha512": digest, "bytes": placed.stat().st_size, "from": origin})

        ctx.progress("files", msg=f"{len(wanted)} file(s) for the {side}")
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(fetch, wanted))
        overrides = 0
        for folder in ("overrides/", f"{side}-overrides/"):
            for name in archive.namelist():
                if name.startswith(folder) and not name.endswith("/"):
                    relative = safe(name[len(folder):])
                    placed = target / relative
                    placed.parent.mkdir(parents=True, exist_ok=True)
                    placed.write_bytes(archive.read(name))
                    overrides += 1
    if problems:
        for problem in sorted(problems):
            ctx.finding(problem)
        return {"pack": None, "problems": sorted(problems)}
    receipt = {"schema": SCHEMA, "pack": ctx.host_path(source), "name": index.get("name"),
               "version": index.get("versionId"), "dependencies": index.get("dependencies") or {}, "side": side,
               "files": sorted(records, key=lambda r: r["path"]), "skipped": sorted(p for p in skipped if p),
               "overrides": overrides}
    (ctx.out / "receipt.json").write_text(json.dumps(receipt, indent=1) + "\n", encoding="utf-8")
    ctx.output("receipt", ctx.out / "receipt.json")
    ctx.output("pack", target)
    downloaded = sum(1 for r in records if r["from"] != "cache")
    ctx.summary(f"{index.get('name')} {index.get('versionId')} for the {side}: {len(records)} file(s) "
                f"({downloaded} downloaded), {overrides} override(s), {len(skipped)} left out")
    return {"pack": ctx.host_path(target), "files": len(records), "problems": []}
