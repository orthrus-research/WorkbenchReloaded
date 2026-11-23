"""store.platform: the platform a Set revision names, fetched into the Store by URL and SHA-256.

Each JDK build the revision's platform names (for Linux and Windows) and its Gradle distribution are downloaded once,
checked against their SHA-256, kept as files/<sha256>/<name>, and unpacked into jdks/<build>/<os>/ and
gradle/<version>/. A URL whose host is not in [modules.ci] store_hosts is refused before anything is read. Archives
are never swept: a worktree on an older revision keeps running on exactly its builds."""

from __future__ import annotations

import hashlib
import os
import shutil
import tarfile
import tempfile
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from wb_core import artifacts as store
from wb_core import setfile
from wb_core.module import Context, EnvironmentProblem, ParamError

from . import commands


def _wanted(platform: dict, only: list) -> list[dict]:
    items = []
    for major, jdk in sorted((platform.get("jdk") or {}).items()):
        for system in ("linux", "windows"):
            archive = jdk.get(system) or {}
            if archive and (not only or major in only or f"{major}:{system}" in only):
                items.append({"what": f"jdk {major} {system}", "url": archive["url"], "sha256": archive["sha256"],
                              "into": Path("jdks") / jdk["build"] / system})
    gradle = platform.get("gradle") or {}
    if gradle and (not only or "gradle" in only):
        items.append({"what": f"gradle {gradle['version']}", "url": gradle["url"], "sha256": gradle["sha256"],
                      "into": Path("gradle") / gradle["version"]})
    return items


def _allowed(url: str, hosts: list[str]) -> bool:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return urllib.parse.urlsplit(url).scheme == "https" and any(host == h.lower() or host.endswith("." + h.lower())
                                                                for h in hosts)


class _Hops(urllib.request.HTTPRedirectHandler):
    """Follows a redirect only to an allowed host: the list governs every hop, not just the first URL."""

    def __init__(self, hosts: list[str]):
        self.hosts = hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _allowed(newurl, self.hosts):
            raise EnvironmentProblem(f"{req.full_url} redirects to {newurl}, a host outside store_hosts")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(url: str, hosts: list[str]):
    return urllib.request.build_opener(_Hops(hosts)).open(url, timeout=60)


def _download(ctx: Context, url: str, digest: str, target: Path, hosts: list[str]):
    hasher = hashlib.sha256()
    with _open(url, hosts) as response, open(target, "wb") as handle:
        while True:
            block = response.read(1 << 20)
            if not block:
                break
            hasher.update(block)
            handle.write(block)
            if ctx.cancelled.is_set():
                raise EnvironmentProblem("cancelled while downloading")
    if hasher.hexdigest() != digest:
        raise EnvironmentProblem(f"{url} is {hasher.hexdigest()}, not the {digest} the Set names; nothing kept")


def _unpack(archive: Path, folder: Path):
    """Unpacks into folder through a sibling .part folder and a rename; a member that would land outside is refused."""
    if folder.is_dir():
        return
    part = folder.with_name(folder.name + ".part")
    shutil.rmtree(part, ignore_errors=True)
    part.mkdir(parents=True)
    root = part.resolve()
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as packed:
            for name in packed.namelist():
                if not (root / name).resolve().is_relative_to(root):
                    raise EnvironmentProblem(f"{archive.name}: {name} would be written outside its folder")
            packed.extractall(part)
            for info in packed.infolist():  # keep the modes the zip records (bin/gradle's execute bit)
                mode = (info.external_attr >> 16) & 0o777
                if mode and not info.is_dir():
                    os.chmod(part / info.filename, mode)
    else:
        with tarfile.open(archive) as packed:
            packed.extractall(part, filter="data")
    part.replace(folder)


def fetch(ctx: Context, revision: str = "", only: list | None = None, unpack: bool = True):
    root = commands._store_root(ctx)
    sets = root.parent / "sets"
    revision = revision or setfile.read_head(sets) or ""
    if not revision:
        raise ParamError("no Set revision: name one, or run wb work sync so the head is exported")
    try:
        platform = setfile.read_export(sets, revision).get("platform") or {}
    except setfile.SetError as error:
        raise ParamError(str(error)) from None
    hosts = [str(h) for h in ctx.config.get("store_hosts") or []]
    items = _wanted(platform, [str(o) for o in only or []])
    refused = [i["url"] for i in items if not _allowed(i["url"], hosts)]
    if refused:
        raise ParamError(f"{len(refused)} URL(s) are on hosts outside [modules.ci] store_hosts "
                         f"({', '.join(hosts) or 'none listed'}), so nothing was fetched; first: {refused[0]}")
    fetched, kept = [], []
    pins = commands._folder(ctx, "pin_cache", commands._state() / "cache" / "pins")
    for item in items:
        name = Path(urllib.parse.urlsplit(item["url"]).path).name
        path = root / "files" / item["sha256"] / name
        if not path.is_file():
            ctx.progress("fetch", msg=f"{item['what']} ({name})")
            with tempfile.TemporaryDirectory(dir=root) as scratch:
                downloaded = Path(scratch) / name
                _download(ctx, item["url"], item["sha256"], downloaded, hosts)
                store.put(root, downloaded, item["sha256"], pins)
            fetched.append(item["what"])
        else:
            kept.append(item["what"])
        if unpack:
            ctx.progress("unpack", msg=item["what"])
            _unpack(store.path_of(root, item["sha256"], name), root / item["into"])
    ctx.summary(f"platform of set {revision[:12]}: {len(fetched)} fetched, {len(kept)} already held"
                + (f" ({', '.join(fetched)})" if fetched else ""))
    return {"revision": revision, "fetched": fetched, "held": kept,
            "folders": [str(root / i["into"]) for i in items] if unpack else []}
