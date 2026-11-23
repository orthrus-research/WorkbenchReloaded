"""toolchain: repairing a dependency's JAR without forking it.

``patch`` rebuilds the classes named from sources with ``javac --release N`` (N defaults to the bytecode level of the
classes it replaces) against the JAR itself and any classpath given, and writes a copy of the JAR in which only those
entries changed: every other entry keeps its bytes, name, order, timestamp and attributes. The input must be the
JAR's expected bytes, a signed JAR is refused (its signature would no longer hold), and the sources must compile to
exactly the classes named. Beside ``<name>`` go ``<name>.sha256`` and the receipt ``<stem>.json`` (``wb.patch.v1``):
the input's and output's digests, each replaced entry before and after, the sources, the classpath and the javac.
With ``expect``, any other output bytes are a finding (the output is kept, to requalify).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import struct
import zipfile
from pathlib import Path

from wb_build import checkout as checkouts
from wb_build.files import copy_verified, write_atomic, write_sidecar
from wb_core.module import Context, EnvironmentProblem, ParamError, sha256_file, sha256_tree

PATCH_SCHEMA = "wb.patch.v1"
SIGNATURE = re.compile(r"(?i)META-INF/[^/]+\.(SF|RSA|DSA|EC)")
CLASS_NAME = re.compile(r"[A-Za-z_$][\w$]*(?:[./][A-Za-z_$][\w$]*)*")
FILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,199}")


def _java_runtime(home: Path) -> str | None:
    release = home / "release"
    if release.is_file():
        for line in release.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("JAVA_RUNTIME_VERSION="):
                return line.split("=", 1)[1].strip().strip('"')
    return None


def _output_name(value: str, default: str) -> str:
    name = value or default
    if not FILE_NAME.fullmatch(name):
        raise ParamError(f"name {name!r} must be a file name (letters, digits and . _ + -)")
    return name


# ---------------------------------------------------------------------------------------------
# patch

def _entry(name: str) -> str:
    """The JAR entry of a class: pkg.Name, pkg.Name$Inner and pkg/Name.class all name pkg/Name.class."""
    text = name.strip().removesuffix(".class")
    if not CLASS_NAME.fullmatch(text):
        raise ParamError(f"classes: {name!r} is not a class name (pkg.Name or pkg/Name.class)")
    return text.replace(".", "/") + ".class"


def _level(data: bytes, where: str) -> int:
    """A class file's major version (52 is Java 8)."""
    if len(data) < 8 or data[:4] != b"\xca\xfe\xba\xbe":
        raise ParamError(f"{where} is not a class file")
    return struct.unpack(">H", data[6:8])[0]


def _without_zip64(extra: bytes) -> bytes:
    """An entry's extra field without its ZIP64 record, which the writer adds again when it needs one."""
    out, index = b"", 0
    while index + 4 <= len(extra):
        kind, size = struct.unpack("<HH", extra[index:index + 4])
        if kind != 1:
            out += extra[index:index + 4 + size]
        index += 4 + size
    return out


def _copy_info(info: zipfile.ZipInfo) -> zipfile.ZipInfo:
    """A fresh entry header with everything a reader sees of the original: name, time, method and attributes."""
    copy = zipfile.ZipInfo(info.filename, info.date_time)
    copy.compress_type = info.compress_type
    copy.comment = info.comment
    copy.extra = _without_zip64(info.extra)
    copy.create_system = info.create_system
    copy.create_version = info.create_version
    copy.extract_version = info.extract_version
    copy.internal_attr = info.internal_attr
    copy.external_attr = info.external_attr
    return copy


def _swap(original: Path, compiled: dict[str, bytes], target: Path) -> list[str]:
    """Writes a copy of ``original`` to ``target`` with the compiled entries' bytes in place of theirs and everything
    else as it was; returns the entries whose bytes differ."""
    target.unlink(missing_ok=True)
    with zipfile.ZipFile(original) as source, zipfile.ZipFile(target, "w", allowZip64=True) as output:
        output.comment = source.comment
        for info in source.infolist():
            data = compiled.get(info.filename)
            output.writestr(_copy_info(info), data if data is not None else source.read(info))
    with zipfile.ZipFile(original) as source, zipfile.ZipFile(target) as output:
        if output.namelist() != source.namelist():
            raise EnvironmentProblem("the repaired JAR's entries differ from the original's")
        return [n for n in source.namelist() if source.read(n) != output.read(n)]


def patch(ctx: Context, jar: str, sha256: str, sources: str, classes: list, classpath: list, jdk: str,
          options: list, release=None, name: str = "", expect=None):
    original = Path(jar)
    if not original.is_file():
        raise ParamError(f"{ctx.host_path(original)} is not a file")
    target_name = _output_name(name, f"{original.stem}-patched{original.suffix or '.jar'}")
    digest = sha256_file(original)
    record = {"input": {"name": original.name, "sha256": digest, "bytes": original.stat().st_size}}
    if digest != sha256:
        ctx.finding(f"{original.name} is {digest[:12]}, not the expected {sha256[:12]}; nothing was patched")
        return {**record, "sha256": None, "replaced": []}
    entries = list(dict.fromkeys(_entry(c) for c in classes))
    if not entries:
        raise ParamError("classes: name the classes to replace")
    try:
        with zipfile.ZipFile(original) as source:
            names = source.namelist()
            signed = [n for n in names if SIGNATURE.fullmatch(n)]
            if signed:
                ctx.finding(f"{original.name} is signed ({signed[0]}): replacing entries would break its signature")
                return {**record, "sha256": None, "replaced": []}
            if len(set(names)) != len(names):
                raise ParamError(f"{original.name} holds an entry twice; it cannot be patched entry by entry")
            missing = [e for e in entries if e not in names]
            if missing:
                raise ParamError(f"{original.name} has no {missing[0]}; patch replaces entries, it adds none")
            before = {e: source.read(e) for e in entries}
    except zipfile.BadZipFile as error:
        raise ParamError(f"{original.name} is not a JAR: {error}") from None
    levels = {e: _level(data, e) for e, data in before.items()}
    if release is None:
        if len(set(levels.values())) != 1:
            raise ParamError("the classes named have different bytecode levels; give release=")
        release = next(iter(levels.values())) - 44
    elif any(level != release + 44 for level in levels.values()):
        ctx.log.warning(f"release {release} differs from the replaced classes' own level "
                        f"(Java {', '.join(str(v - 44) for v in sorted(set(levels.values())))})")
    java_sources = sorted(p for p in Path(sources).rglob("*.java") if p.is_file())
    if not java_sources:
        raise ParamError(f"{ctx.host_path(sources)} holds no .java sources")
    home = ctx.jdk(jdk)
    javac = home / "bin" / ("javac.exe" if os.name == "nt" else "javac")
    if not javac.is_file():
        raise EnvironmentProblem(f"JDK {jdk} has no javac ({javac})")
    compiled_dir = ctx.work / "classes"
    shutil.rmtree(compiled_dir, ignore_errors=True)
    compiled_dir.mkdir(parents=True)
    argfile = ctx.work / "sources.txt"
    argfile.write_text("".join(f'"{p.as_posix()}"\n' for p in java_sources), encoding="utf-8")
    path = os.pathsep.join([str(original), *(str(p) for p in classpath)])
    ctx.progress("javac", msg=f"{len(java_sources)} source(s), --release {release}")
    done = ctx.run([javac, "--release", str(release), "-proc:none", "-implicit:none", "-encoding", "UTF-8",
                    "-Xlint:-options", "-sourcepath", str(sources), "-classpath", path, "-d", str(compiled_dir),
                    *[str(o) for o in options], f"@{argfile}"], phase="javac")
    if done.returncode != 0:
        tail = [line for line in done.stdout.splitlines() if line.strip()][-3:]
        ctx.finding(f"javac exited {done.returncode}: {' | '.join(tail)}")
        return {**record, "sha256": None, "replaced": []}
    compiled = {p.relative_to(compiled_dir).as_posix(): p.read_bytes() for p in compiled_dir.rglob("*.class")}
    extra = sorted(set(compiled) - set(entries))
    if extra:
        raise ParamError(f"the sources also compile {', '.join(extra[:3])}: name them in classes to replace them "
                         "too, or leave their sources out")
    absent = [e for e in entries if e not in compiled]
    if absent:
        raise ParamError(f"the sources compile no {absent[0]}")
    for entry, data in compiled.items():
        if _level(data, entry) != release + 44:
            raise EnvironmentProblem(f"javac wrote {entry} at level {_level(data, entry)}, not {release + 44}")
    temporary = ctx.work / target_name
    target = ctx.out / target_name
    try:
        changed = _swap(original, compiled, temporary)
        if not set(changed) <= set(entries):
            raise EnvironmentProblem(f"entries not named changed: {', '.join(sorted(set(changed) - set(entries)))}")
        same = [e for e in entries if e not in changed]
        if same:
            ctx.log.warning(f"rebuilt as they were (the sources change nothing in them): {', '.join(same)}")
        made = sha256_file(temporary)
        if target.exists() and (not target.is_file() or sha256_file(target) != made):
            ctx.finding(f"{target_name} already exists with other bytes in the out folder; nothing was written")
            return {**record, "sha256": made, "replaced": []}
        copy_verified(temporary, target, made)
    finally:
        temporary.unlink(missing_ok=True)
    write_sidecar(target, made)
    receipt = {
        "schema": PATCH_SCHEMA, **record,
        "output": {"name": target_name, "sha256": made, "bytes": target.stat().st_size},
        "replaced": [{"entry": e, "before": hashlib.sha256(before[e]).hexdigest(),
                      "after": hashlib.sha256(compiled[e]).hexdigest()} for e in entries],
        "release": release, "javac": _java_runtime(home), "options": [str(o) for o in options],
        "sources": [{"path": p.relative_to(sources).as_posix(), "sha256": sha256_file(p)} for p in java_sources],
        "classpath": [{"name": Path(p).name, "sha256": sha256_tree(Path(p))[0]} for p in classpath],
        "expected": expect, "qualified": None if expect is None else made == expect,
    }
    receipt_path = ctx.out / (Path(target_name).stem + ".json")
    write_atomic(receipt_path, (json.dumps(receipt, indent=2) + "\n").encode("utf-8"))
    ctx.output(target_name, target)
    ctx.output(receipt_path.name, receipt_path)
    if expect is not None and made != expect:
        ctx.finding(f"{target_name} is {made[:12]}, not the qualified {expect[:12]}: kept, to requalify")
    else:
        ctx.summary(f"{target_name}: {len(entries)} class(es) replaced at release {release}, SHA-256 {made[:12]}"
                    + (" (qualified)" if expect else ""))
    return {**record, "output": receipt["output"], "sha256": made, "replaced": receipt["replaced"],
            "release": release, "qualified": receipt["qualified"], "receipt": ctx.host_path(receipt_path)}


# ---------------------------------------------------------------------------------------------
# rebuild

def gradle_home(ctx: Context, gradle_home=None, write: bool = False, auto_detect: bool = False,
                auto_download: bool = False):
    """Write (or, by default, show) the block of a Gradle home's gradle.properties that lists this machine's JDKs:
    wb_toolchain.gradle_home."""
    from . import gradle_home as homes
    folder = homes.home_folder(gradle_home)
    jdks = homes.jdk_homes(dict(getattr(ctx, "_jdks", {}) or {}))
    if not jdks:
        raise EnvironmentProblem("this machine names no JDKs (on a host: [runner] jdks)")
    target = folder / "gradle.properties"
    existing = target.read_text(encoding="utf-8") if target.is_file() else ""
    text = homes.merged(existing, homes.block(jdks, auto_detect, auto_download))
    preview = ctx.out / "gradle.properties"
    preview.write_text(text, encoding="utf-8")
    ctx.output("gradle.properties", preview, "text/plain")
    if write and text != existing:
        folder.mkdir(parents=True, exist_ok=True)
        write_atomic(target, text.encode("utf-8"))
    verb = "lists" if write else "would list (a dry run: write=true writes it)"
    ctx.summary(f"{target} {verb} {len(jdks)} JDK(s), auto-detect {'on' if auto_detect else 'off'}")
    return {"gradle_home": str(folder), "jdks": jdks, "written": bool(write and text != existing)}
