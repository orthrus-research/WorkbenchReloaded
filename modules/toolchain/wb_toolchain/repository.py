"""toolchain: the rescue repository, a local Maven repository of coordinates their public repositories no longer serve
(build plugins, libraries, retained builds, patched JARs), each file with a receipt, served read-only to every Gradle
build Workbench runs, with coordinate substitutions for what cannot come back under its own name::

    <repository>/<group as folders>/<artifact>/<version>/<artifact>-<version>[-<classifier>].<ext>
    <repository>/.../<file>.sha256, <file>.sha1   # its digests, the hex alone, as Maven repositories publish them
    <repository>/.../<artifact>-<version>.pom     # the version's POM: the original, or a minimal one Workbench writes
    <repository>/<group as folders>/<artifact>/maven-metadata.xml (.sha256, .sha1)   # the versions held
    <repository>/receipts.json                    # {"schema": "wb.repository.v1", "files": {<path>: <receipt>}}
    <repository>/receipts.lock                    # every writer holds it

A receipt names the file's coordinates, SHA-256, SHA-1 and size, its kind (``rescued`` bytes, or a ``pom`` or
``marker`` Workbench wrote), where its bytes came from (``source``), why they are there (``reason``), when (``added``)
and the job that added them. Rescued bytes are never replaced by other bytes: another build of the same coordinates
goes under new coordinates, and a substitution sends builds there. A POM Workbench wrote is written again when asked
for other dependencies, and gives way to the original POM once that is rescued. Only coordinates with a receipt are
served: a file put into the folder by hand is not, and ``repo-verify`` names it.

``[modules.toolchain] repository`` is the folder (empty: ``<state>/toolchain/repository``, which only builds in the
container see; under a configured root, such as the out root, builds on the host can read it too).
``[modules.toolchain] substitutions`` maps vanished coordinates to the coordinates builds get instead::

    [modules.toolchain.substitutions]
    "com.example:lib:1.0" = "com.example:lib:1.0.1"                                       # a dependency
    "com.example.tool:com.example.tool.gradle.plugin:2.0" = "com.example:tool-retained:2.0"  # a plugin request

``repo-init`` makes the Gradle init script that serves both (see ``gradle.py``); ``ci.run`` asks it for the script
every run and keeps it in its Gradle home's ``init.d``, which every build there reads.
"""

from __future__ import annotations

import calendar
import fnmatch
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from wb_build.files import copy_verified, locked, write_atomic
from wb_core.module import Context, EnvironmentProblem, ParamError, sha256_file

from . import gradle, maven
from .commands import PATCH_SCHEMA
from .maven import Coordinates, CoordinatesError

SCHEMA = "wb.repository.v1"
RECEIPTS = "receipts.json"
LOCK = "receipts.lock"
SCRIPT = "workbench-repository.gradle"
RESCUED, POM, MARKER = "rescued", "pom", "marker"
SIDECARS = (".sha256", ".sha1")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _epoch(stamp: str) -> float:
    return float(calendar.timegm(time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")))


def _sha1_file(path: Path) -> str:
    digest = hashlib.sha1()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


# ---------------------------------------------------------------------------------------------
# where it is, what it holds, what the config asks

def folder(ctx: Context, write: bool = False) -> Path:
    """The repository's folder as this machine sees it: ``[modules.toolchain] repository`` (a host path under a
    configured root, or a folder only the container has), else ``<state>/toolchain/repository``. ``write``: one this
    job may write (a root mounted read-only serves builds, but takes nothing new)."""
    value = str(ctx.config.get("repository") or "")
    if not value:
        return Path(os.environ.get("WB_STATE_DIR") or "/var/lib/wb") / "toolchain" / "repository"
    from wb_core.paths import PathError, PathMap
    from wb_core.settings import Root
    roots = [Root(**r) for r in ctx.spec.get("roots", [])]
    if not roots or ctx.machine == "host":
        return Path(value)
    try:
        return Path(PathMap(roots).resolve(value, need_write=write).container)
    except PathError as error:
        if value.startswith("/") and "read-only" not in str(error):
            return Path(value)  # a folder only the container has
        raise EnvironmentProblem(f"[modules.toolchain] repository = {value!r}: {error}") from None


def _host_location(ctx: Context, root: Path) -> str | None:
    """The repository as builds on the host name it; None when it is under no configured root (the host cannot see
    it)."""
    from wb_core.paths import PathError, PathMap
    from wb_core.settings import Root
    roots = [Root(**r) for r in ctx.spec.get("roots", [])]
    if not roots or ctx.machine == "host":
        return str(root)
    try:
        PathMap(roots).resolve(str(root))
    except PathError:
        return None
    return ctx.host_path(root)


def _receipts(root: Path) -> dict:
    path = root / RECEIPTS
    if not path.is_file():
        return {"schema": SCHEMA, "files": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise EnvironmentProblem(f"{path} cannot be read as JSON ({error}); the repository's receipts are lost "
                                 "until it is repaired") from None
    if not isinstance(data, dict) or data.get("schema") != SCHEMA or not isinstance(data.get("files"), dict):
        raise EnvironmentProblem(f"{path} is not a {SCHEMA} receipts file: the folder is another kind of repository")
    return data


def _held(receipts: dict) -> list[tuple[str, str, str]]:
    """The versions the repository holds, (group, artifact, version): every one with a receipt."""
    found = set()
    for receipt in receipts["files"].values():
        coordinates = maven.parse(receipt["coordinates"])
        found.add((coordinates.group, coordinates.artifact, coordinates.version))
    return sorted(found)


def substitutions(ctx: Context) -> list[tuple[Coordinates, Coordinates]]:
    """``[modules.toolchain] substitutions``, checked: plain ``group:artifact:version`` on both sides, nothing
    substituted by itself or by coordinates that are themselves substituted, no plugin made into another."""
    table = ctx.config.get("substitutions") or {}
    if not isinstance(table, dict):
        raise EnvironmentProblem("[modules.toolchain] substitutions must be a table of coordinates by coordinates")
    pairs = []
    for source, target in table.items():
        try:
            pair = (maven.parse(source, plain=True), maven.parse(str(target), plain=True))
        except CoordinatesError as error:
            raise EnvironmentProblem(f"[modules.toolchain] substitutions: {error}") from None
        if pair[0] == pair[1]:
            raise EnvironmentProblem(f"[modules.toolchain] substitutions: {source} is substituted by itself")
        pairs.append(pair)
    sources = {s for s, _ in pairs}
    for source, target in pairs:
        if target in sources:
            raise EnvironmentProblem(f"[modules.toolchain] substitutions: {source} becomes {target}, which is "
                                     "substituted too; name the coordinates builds finally get")
    try:
        gradle.rules(pairs)
    except CoordinatesError as error:
        raise EnvironmentProblem(f"[modules.toolchain] substitutions: {error}") from None
    return sorted(pairs)


# ---------------------------------------------------------------------------------------------
# writing

class Refused(Exception):
    """A request the repository's state refuses: a finding, and nothing written."""


def _parse(value: str, what: str) -> Coordinates:
    try:
        return maven.parse(value)
    except CoordinatesError as error:
        raise ParamError(f"{what}: {error}") from None


def _decide(root: Path, files: dict, relative: str, digest: str, rescued: bool) -> str:
    """What placing ``digest`` at ``relative`` means: new, held (already these bytes), restore (the receipt's bytes,
    damaged or gone on disk), replace (a file Workbench wrote) or adopt (these bytes, there without a receipt)."""
    receipt = files.get(relative)
    path = root / relative
    if receipt is not None:
        if receipt["sha256"] == digest:
            return "held" if path.is_file() and sha256_file(path) == digest else "restore"
        if receipt["kind"] == RESCUED or not (rescued or receipt["kind"] in (POM, MARKER)):
            raise Refused(f"{receipt['coordinates']} is already held as other bytes ({receipt['sha256'][:12]}, not "
                          f"{digest[:12]}): rescued bytes are never replaced; add these under new coordinates and "
                          "substitute them")
        return "replace"
    if path.exists():
        if path.is_file() and sha256_file(path) == digest:
            return "adopt"
        raise Refused(f"{relative} is in the repository without a receipt, as other bytes; Workbench does not write "
                      "over it")
    return "new"


def _sidecars(path: Path, sha256: str, sha1: str) -> None:
    for suffix, digest in zip(SIDECARS, (sha256, sha1)):
        side = path.with_name(path.name + suffix)
        text = maven.digest_text(digest)
        if not side.is_file() or side.read_bytes() != text:
            write_atomic(side, text)


def _write_bytes(root: Path, relative: str, data: bytes) -> None:
    sha256, sha1 = maven.digests(data)
    path = root / relative
    if not path.is_file() or path.read_bytes() != data:
        write_atomic(path, data)
    _sidecars(path, sha256, sha1)


def _metadata(root: Path, files: dict, group: str, artifact: str) -> None:
    versions, stamps = set(), []
    for receipt in files.values():
        coordinates = maven.parse(receipt["coordinates"])
        if (coordinates.group, coordinates.artifact) == (group, artifact):
            versions.add(coordinates.version)
            stamps.append(_epoch(receipt["added"]))
    if versions:
        _write_bytes(root, maven.metadata_path(group, artifact), maven.metadata(group, artifact, versions, max(stamps)))


def _fetch(ctx: Context, url: str, target: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "Workbench toolchain.repo-add"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response, open(target, "wb") as out:
            while block := response.read(1 << 20):
                ctx.check_cancel()
                out.write(block)
    except urllib.error.HTTPError as error:
        raise EnvironmentProblem(f"{url} answered HTTP {error.code} ({error.reason})") from None
    except (urllib.error.URLError, OSError) as error:
        raise EnvironmentProblem(f"{url} could not be fetched: {getattr(error, 'reason', error)}") from None


def _patch_record(path: Path, digest: str) -> dict:
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ParamError(f"patch: {path.name} is not a JSON receipt ({error})") from None
    if not isinstance(receipt, dict) or receipt.get("schema") != PATCH_SCHEMA:
        raise ParamError(f"patch: {path.name} is not a toolchain.patch receipt ({PATCH_SCHEMA})")
    made = (receipt.get("output") or {}).get("sha256")
    if made != digest:
        raise Refused(f"the patch receipt {path.name} is for SHA-256 {str(made)[:12]}, not these bytes "
                      f"({digest[:12]})")
    return {"receipt": sha256_file(path), "input": receipt.get("input"), "replaced": receipt.get("replaced"),
            "release": receipt.get("release"), "javac": receipt.get("javac")}


def _version_pom(ctx: Context, files: dict, coordinates: Coordinates, dependencies: list[Coordinates]):
    """The POM Workbench writes for the version of ``coordinates``: (relative path, bytes, receipt), or None when the
    version keeps its original POM."""
    relative = coordinates.pom.path
    existing = files.get(relative)
    if existing is not None and existing["kind"] == RESCUED:
        if dependencies:
            raise Refused(f"{coordinates.module} has its original POM, rescued; dependencies apply only to a POM "
                          "Workbench writes")
        return None
    main = coordinates if not coordinates.classifier else next(
        (c for c in (maven.parse(r["coordinates"]) for r in files.values() if r["kind"] == RESCUED)
         if c.module == coordinates.module and not c.classifier and not c.is_pom), None)
    packaging = main.ext if main else (existing or {}).get("packaging", "jar")
    if not dependencies and existing is not None:
        dependencies = [maven.parse(d) for d in existing.get("dependencies") or []]
    data = maven.pom(coordinates.pom, packaging, dependencies,
                     "Written by Workbench (toolchain.repo-add): the original POM is not in the repository.")
    sha256, sha1 = maven.digests(data)
    receipt = {"coordinates": str(coordinates.pom), "sha256": sha256, "sha1": sha1, "bytes": len(data), "kind": POM,
               "source": "workbench", "reason": f"the minimal POM of {coordinates.module}: the original is not here",
               "added": _now(), "job": ctx.job, "packaging": packaging,
               "dependencies": [str(d) for d in dependencies]}
    return relative, data, receipt


def _marker(ctx: Context, files: dict, plugin: str, coordinates: Coordinates):
    """The marker POM of ``plugin`` pointing at ``coordinates``: (relative path, bytes, receipt), or None when the
    marker there is an original, rescued."""
    marker = maven.marker(plugin, coordinates.version)
    existing = files.get(marker.path)
    if existing is not None and existing["kind"] == RESCUED:
        return None
    implementation = Coordinates(coordinates.group, coordinates.artifact, coordinates.version, coordinates.classifier)
    data = maven.pom(marker, "pom", [implementation],
                     f"Written by Workbench (toolchain.repo-add): the marker of Gradle plugin {plugin}.")
    sha256, sha1 = maven.digests(data)
    receipt = {"coordinates": str(marker), "sha256": sha256, "sha1": sha1, "bytes": len(data), "kind": MARKER,
               "source": "workbench", "reason": f"the marker of plugin {plugin}, implemented by {implementation}",
               "added": _now(), "job": ctx.job, "plugin": plugin}
    return marker.path, data, receipt


def add(ctx: Context, coordinates: str, sha256: str, reason: str, dependencies: list, plugins: list, file=None,
        url=None, patch=None):
    wanted = _parse(coordinates, "coordinates")
    needs = [_parse(d, "dependencies") for d in dependencies]
    try:
        ids = list(dict.fromkeys(maven.plugin_id(p) for p in plugins))
    except CoordinatesError as error:
        raise ParamError(f"plugins: {error}") from None
    if wanted.is_pom and (needs or ids):
        raise ParamError("an original POM brings its own dependencies; dependencies and plugins are for a file whose "
                         "POM Workbench writes")
    if ids and (wanted.ext != "jar" or wanted.plugin):
        raise ParamError("plugins: only a JAR implements a Gradle plugin")
    if bool(file) == bool(url):
        raise ParamError("give the file to rescue or its url, one of the two")
    root = folder(ctx, write=True)
    result = {"coordinates": str(wanted), "repository": ctx.host_path(root), "sha256": None, "state": "refused",
              "written": []}
    if file:
        source_path, source = Path(file), ctx.host_path(file)
        if not source_path.is_file():
            raise ParamError(f"{source} is not a file")
    else:
        source_path, source = ctx.work / "download", url
        ctx.progress("download", msg=url)
        _fetch(ctx, url, source_path)
    digest = sha256_file(source_path)
    result["sha256"] = digest
    try:
        if digest != sha256:
            raise Refused(f"{Path(source).name if file else url} is SHA-256 {digest[:12]}, not the expected "
                          f"{sha256[:12]}; nothing was added")
        if wanted.is_pom:
            try:
                declared = maven.pom_coordinates(source_path.read_bytes())
            except CoordinatesError as error:
                raise Refused(f"{source}: {error}") from None
            if declared != (wanted.group, wanted.artifact, wanted.version):
                raise Refused(f"the POM declares {':'.join(str(p) for p in declared)}, not {wanted.module}")
        record = _patch_record(Path(patch), digest) if patch else None
        root.mkdir(parents=True, exist_ok=True)
        with locked(root / LOCK, ctx):
            receipts = _receipts(root)
            files = receipts["files"]
            state = _decide(root, files, wanted.path, digest, True)
            generated = []
            if not wanted.is_pom:
                generated += [g for g in [_version_pom(ctx, files, wanted, needs)] if g]
            generated += [g for g in (_marker(ctx, files, p, wanted) for p in ids) if g]
            plan = [(g, _decide(root, files, g[0], g[2]["sha256"], False)) for g in generated]
            ctx.progress("write", msg=str(wanted))
            target = root / wanted.path
            if state in ("new", "restore", "replace"):
                copy_verified(source_path, target, digest)
                result["written"].append(wanted.path)
            _sidecars(target, digest, _sha1_file(target))
            if state != "held" and state != "restore":
                receipt = {"coordinates": str(wanted), "sha256": digest, "sha1": _sha1_file(target),
                           "bytes": target.stat().st_size, "kind": RESCUED, "source": source,
                           "reason": reason.strip(), "added": _now(), "job": ctx.job}
                if record:
                    receipt["patch"] = record
                if state == "replace":
                    receipt["replaces"] = files[wanted.path]["sha256"]
                files[wanted.path] = receipt
            for (relative, data, receipt), how in plan:
                _write_bytes(root, relative, data)  # held: its digest files made good, if need be
                if how == "held":
                    continue
                result["written"].append(relative)
                if how == "replace":
                    receipt["replaces"] = files[relative]["sha256"]
                if how != "restore":
                    files[relative] = receipt
            for group, artifact in sorted({(wanted.group, wanted.artifact)} | {
                    (maven.parse(g[2]["coordinates"]).group, maven.parse(g[2]["coordinates"]).artifact)
                    for g in generated}):
                _metadata(root, files, group, artifact)
            receipts["files"] = dict(sorted(files.items()))
            write_atomic(root / RECEIPTS, (json.dumps(receipts, indent=2) + "\n").encode("utf-8"))
    except Refused as refusal:
        ctx.finding(str(refusal))
        return result
    ctx.output(target.name, target)
    result.update(state=state, path=ctx.host_path(target))
    extras = [("a minimal POM" if receipt["kind"] == POM else f"the marker of {receipt['plugin']}")
              for (_, _, receipt), how in plan if how != "held"]
    said = {"new": "rescued", "adopt": "rescued (its bytes were already there)", "restore": "restored to its receipt",
            "replace": "rescued in place of the POM Workbench wrote", "held": "already held as these bytes"}[state]
    ctx.summary(f"{wanted} {said}, SHA-256 {digest[:12]}" + (f", with {' and '.join(extras)}" if extras else ""))
    return result


# ---------------------------------------------------------------------------------------------
# reading

def _sort_key(coordinates: Coordinates):
    import functools
    return (coordinates.group, coordinates.artifact, functools.cmp_to_key(maven.compare)(coordinates.version),
            coordinates.classifier, coordinates.ext)


def listing(ctx: Context, match: str = ""):
    root = folder(ctx)
    receipts = _receipts(root)
    entries = sorted(((maven.parse(r["coordinates"]), path, r) for path, r in receipts["files"].items()),
                     key=lambda e: _sort_key(e[0]))
    modules: dict[str, list] = {}
    for coordinates, path, receipt in entries:
        if match and not fnmatch.fnmatchcase(coordinates.module, match) and not fnmatch.fnmatchcase(str(coordinates),
                                                                                                   match):
            continue
        modules.setdefault(coordinates.module, []).append({"path": path, **receipt})
    held = {":".join(h) for h in _held(receipts)}
    swaps = [{"from": str(s), "to": str(t), "plugin": s.plugin, "held": t.module in held}
             for s, t in substitutions(ctx)]
    files = sum(len(v) for v in modules.values())
    ctx.summary(f"{len(modules)} version(s), {files} file(s) in {ctx.host_path(root)}"
                + (f" matching {match}" if match else "") + f"; {len(swaps)} substitution(s)")
    return {"repository": ctx.host_path(root), "coordinates": [{"coordinates": k, "files": v} for k, v in
                                                               modules.items()], "substitutions": swaps}


def _sidecar_digest(path: Path) -> str | None:
    try:
        words = path.read_text(encoding="ascii", errors="replace").split()
    except OSError:
        return None
    return words[0].lower() if words else None


def verify(ctx: Context):
    root = folder(ctx)
    swaps = substitutions(ctx)
    if not root.is_dir():
        ctx.summary(f"{ctx.host_path(root)} does not exist yet: the repository holds nothing")
        return {"repository": ctx.host_path(root), "files": 0, "problems": []}
    receipts = _receipts(root)
    files = receipts["files"]
    parsed = {relative: maven.parse(receipt["coordinates"]) for relative, receipt in files.items()}
    problems: list[dict] = []
    known = {RECEIPTS, LOCK}
    for index, (relative, receipt) in enumerate(sorted(files.items())):
        ctx.progress("verify", index / max(1, len(files)), relative)
        known |= {relative, *(relative + s for s in SIDECARS)}
        path = root / relative
        if parsed[relative].path != relative:
            problems.append({"path": relative, "problem": f"its receipt names {receipt['coordinates']}, whose path "
                                                          "is another"})
        if not path.is_file():
            problems.append({"path": relative, "problem": "is gone"})
            continue
        actual = sha256_file(path)
        if actual != receipt["sha256"] or path.stat().st_size != receipt["bytes"]:
            problems.append({"path": relative, "problem": f"is SHA-256 {actual[:12]}, not its receipt's "
                                                          f"{receipt['sha256'][:12]}"})
            continue
        for suffix, digest in zip(SIDECARS, (receipt["sha256"], receipt["sha1"])):
            if _sidecar_digest(root / (relative + suffix)) != digest:
                problems.append({"path": relative + suffix, "problem": f"does not say {digest[:12]}"})
        if _sha1_file(path) != receipt["sha1"]:
            problems.append({"path": relative, "problem": "its SHA-1 is not its receipt's"})
    held = _held(receipts)
    poms = {c.module for c in parsed.values() if c.is_pom}
    for group, artifact, version in held:
        if f"{group}:{artifact}:{version}" not in poms:
            problems.append({"path": Coordinates(group, artifact, version).pom.path,
                             "problem": f"{group}:{artifact}:{version} has no POM"})
    for group, artifact in sorted({(g, a) for g, a, _ in held}):
        relative = maven.metadata_path(group, artifact)
        known |= {relative, *(relative + s for s in SIDECARS)}
        versions = [v for g, a, v in held if (g, a) == (group, artifact)]
        stamps = [_epoch(files[p]["added"]) for p, c in parsed.items() if (c.group, c.artifact) == (group, artifact)]
        expected = maven.metadata(group, artifact, versions, max(stamps))
        path = root / relative
        if not path.is_file() or path.read_bytes() != expected:
            problems.append({"path": relative, "problem": "does not list the versions held"})
        elif any(_sidecar_digest(root / (relative + s)) != d for s, d in zip(SIDECARS, maven.digests(expected))):
            problems.append({"path": relative, "problem": "its digest files do not match it"})
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        relative = path.relative_to(root).as_posix()
        if relative not in known:
            problems.append({"path": relative, "problem": "has no receipt: builds are not served it"})
    for problem in problems:
        ctx.finding(f"{problem['path']} {problem['problem']}")
    if not problems:
        ctx.summary(f"{len(files)} file(s) at {len(held)} version(s) match their receipts")
    held_names = {":".join(h) for h in held}
    return {"repository": ctx.host_path(root), "files": len(files), "versions": len(held), "problems": problems,
            "substitutions": [{"from": str(s), "to": str(t), "held": t.module in held_names} for s, t in swaps]}


# ---------------------------------------------------------------------------------------------
# serving

def init(ctx: Context, machine: str, gradle_home=None):
    root = folder(ctx)
    held = _held(_receipts(root))
    swaps = substitutions(ctx)
    location = str(root)
    if machine == "host":
        location = _host_location(ctx, root)
        if location is None and held:
            ctx.finding(f"the repository {root} is under no configured root, so builds on the host cannot read it: "
                        "point [modules.toolchain] repository under one (the out root)")
            return {"script": "", "sha256": None, "repository": None, "machine": machine, "written": None}
    script = gradle.init_script(location or str(root), held, swaps)
    target = Path(gradle_home) / "init.d" / SCRIPT if gradle_home else None
    data = script.encode("utf-8")
    if script:
        write_atomic(ctx.out / SCRIPT, data)
        ctx.output(SCRIPT, ctx.out / SCRIPT)
        if target is not None:
            write_atomic(target, data)
        served = ([f"{len(held)} version(s) from {location}"] if held else []) \
            + ([f"{len(swaps)} substitution(s)"] if swaps else [])
        ctx.summary("serves " + " and ".join(served)
                    + (f"; written to {ctx.host_path(target)}" if target is not None else ""))
    else:
        if target is not None and target.is_file() and target.read_text(
                encoding="utf-8", errors="replace").startswith(gradle.HEADER):
            target.unlink()
        ctx.summary("nothing to serve: the repository holds nothing and no substitutions are configured")
    return {"script": script, "sha256": hashlib.sha256(data).hexdigest() if script else None,
            "repository": location, "machine": machine, "held": [":".join(h) for h in held],
            "substitutions": {str(s): str(t) for s, t in swaps},
            "written": ctx.host_path(target) if target is not None and script else None}
