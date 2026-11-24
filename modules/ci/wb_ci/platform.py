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


# --- typed platform values: one value lands a platform move ---------------------------------------------------------
#
# A landing names a platform key and one value: platform.jdk.<major>=<build>, platform.gradle=<version>,
# platform.cleanroom=<version>, or a plain value (platform.java, platform.cleanroomgradle, platform.cleanroom.lwjgl).
# Each file a value names is downloaded from its publisher on an allowed host and kept in the Store; its SHA-256 is the
# one computed from those bytes, checked against the publisher's own checksum file where it publishes one.

PLAIN = {"java": int, "cleanroomgradle": str, "cleanroom.lwjgl": str}


def _temurin(major: int, build: str) -> dict:
    """The Temurin archives of a JDK build for Linux and Windows, and their checksum files."""
    import re
    import urllib.parse
    if major == 8:
        if not re.fullmatch(r"8u\d+-b\d+", build):
            raise ParamError(f"a Java 8 build is written like 8u512-b09, not {build!r}")
        tag, version = f"jdk{build}", build.replace("-", "")
    else:
        if not re.fullmatch(rf"{major}(\.\d+)*\+\d+", build):
            raise ParamError(f"a Java {major} build is written like {major}.0.2.1+1, not {build!r}")
        tag, version = f"jdk-{build}", build.replace("+", "_")
    base = (f"https://github.com/adoptium/temurin{major}-binaries/releases/download/{urllib.parse.quote(tag)}/"
            f"OpenJDK{major}U-jdk_x64")
    return {"linux": f"{base}_linux_hotspot_{version}.tar.gz", "windows": f"{base}_windows_hotspot_{version}.zip"}


def _checksum(url: str, hosts: list[str]) -> str | None:
    """The SHA-256 a publisher states beside a file, when it states one (the first 64-hex token)."""
    import re
    try:
        with _open(url, hosts) as response:
            found = re.search(rb"\b[0-9a-fA-F]{64}\b", response.read(4096))
    except Exception:
        return None
    return found.group(0).decode().lower() if found else None


def keep(ctx: Context, url: str, stated: str | None = None) -> tuple[str, str]:
    """Downloads a file from an allowed host into the Store and returns (sha256, file name); bytes that differ from a
    checksum the publisher states are refused and nothing is kept."""
    hosts = [str(h) for h in ctx.config.get("store_hosts") or []]
    if not _allowed(url, hosts):
        raise ParamError(f"{url} is on a host outside [modules.ci] store_hosts")
    root = commands._store_root(ctx)
    name = urllib.parse.unquote(urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1])
    with tempfile.TemporaryDirectory(dir=root) as scratch:
        target = Path(scratch) / name
        hasher = hashlib.sha256()
        with _open(url, hosts) as response, open(target, "wb") as handle:
            while block := response.read(1 << 20):
                hasher.update(block)
                handle.write(block)
                if ctx.cancelled.is_set():
                    raise EnvironmentProblem("cancelled while downloading")
        digest = hasher.hexdigest()
        if stated and stated != digest:
            raise EnvironmentProblem(f"{url} is {digest}, but its publisher states {stated}; nothing kept")
        store.put(root, target, digest, commands._folder(ctx, "pin_cache", commands._state() / "cache" / "pins"))
    return digest, name


def resolve(ctx: Context, key: str, value: str, platform: dict) -> tuple[dict, str]:
    """The platform with ``key`` set from ``value``, every file it names kept in the Store, and the landing's line."""
    import json
    hosts = [str(h) for h in ctx.config.get("store_hosts") or []]
    changed = json.loads(json.dumps(platform or {}))
    if key in PLAIN:
        try:
            typed = PLAIN[key](value)
        except ValueError:
            raise ParamError(f"platform.{key} is {PLAIN[key].__name__}, not {value!r}") from None
        table, _, leaf = key.rpartition(".")
        (changed.setdefault(table, {}) if table else changed)[leaf] = typed
        return changed, f"platform.{key} = {typed}"
    if key.startswith("jdk."):
        major = key[4:]
        if not major.isdigit():
            raise ParamError(f"platform.{key}: name a JDK by its major, platform.jdk.26=<build>")
        entry = {"build": value}
        for system, url in _temurin(int(major), value).items():
            digest, _ = keep(ctx, url, _checksum(url + ".sha256.txt", hosts))
            entry[system] = {"url": url, "sha256": digest}
        changed.setdefault("jdk", {})[major] = entry
        return changed, f"platform.jdk.{major} = {value} (linux {entry['linux']['sha256'][:12]}, windows " \
                        f"{entry['windows']['sha256'][:12]})"
    if key == "gradle":
        url = f"https://services.gradle.org/distributions/gradle-{value}-bin.zip"
        digest, _ = keep(ctx, url, _checksum(url + ".sha256", hosts))
        changed["gradle"] = {"version": value, "url": url, "sha256": digest}
        return changed, f"platform.gradle = {value} ({digest[:12]})"
    if key == "cleanroom":
        base = f"https://github.com/CleanroomMC/Cleanroom/releases/download/{value}/cleanroom-{value}"
        entry = dict(changed.get("cleanroom") or {})
        entry["version"] = value
        for part, url in (("installer", f"{base}-installer.jar"), ("universal", f"{base}-universal.jar"),
                          ("prism", f"{base}.zip")):
            digest, name = keep(ctx, url)
            entry[part] = {"url": url, "file": name, "sha256": digest}
        changed["cleanroom"] = entry
        return changed, (f"platform.cleanroom = {value} (installer {entry['installer']['sha256'][:12]}, universal "
                         f"{entry['universal']['sha256'][:12]}, prism {entry['prism']['sha256'][:12]})")
    raise ParamError(f"platform.{key} is not a key a landing sets (jdk.<major>, gradle, cleanroom, "
                     f"{', '.join(sorted(PLAIN))})")
