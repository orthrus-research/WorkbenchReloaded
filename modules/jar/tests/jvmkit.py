"""JDKs, javac and JARs for the tests of the jar, mixin and probe modules.

``find_jdks`` looks wherever a machine keeps JDKs: Workbench's own lookup (``wb_core.requirements``:
``WB_JDKS``, the image's ``/opt/jdk``, ``~/.jdks``), then ``JAVA_HOME`` and ``JAVA_HOME_<n>_X64``, then the
folders on ``PATH`` that hold ``javac``. ``export`` hands what it found to the Workbench a test starts.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import zipfile
from pathlib import Path

EXE = ".exe" if os.name == "nt" else ""

# Declarations with the names and retention of the Mixin, Forge and loader annotations the commands
# read, so javac writes class files shaped as real mods' are. Only their shape matters.
STUBS = {
    "org/spongepowered/asm/mixin/Mixin.java": """
        package org.spongepowered.asm.mixin;
        import java.lang.annotation.*;
        @Retention(RetentionPolicy.CLASS) @Target(ElementType.TYPE)
        public @interface Mixin { Class<?>[] value() default {}; String[] targets() default {};
            int priority() default 1000; boolean remap() default true; }
    """,
    "org/spongepowered/asm/mixin/Overwrite.java": """
        package org.spongepowered.asm.mixin;
        import java.lang.annotation.*;
        @Retention(RetentionPolicy.CLASS) @Target(ElementType.METHOD)
        public @interface Overwrite {}
    """,
    "org/spongepowered/asm/mixin/Shadow.java": """
        package org.spongepowered.asm.mixin;
        import java.lang.annotation.*;
        @Retention(RetentionPolicy.CLASS) @Target({ElementType.METHOD, ElementType.FIELD})
        public @interface Shadow { String prefix() default "shadow$"; boolean remap() default true; }
    """,
    "org/spongepowered/asm/mixin/injection/At.java": """
        package org.spongepowered.asm.mixin.injection;
        import java.lang.annotation.*;
        @Retention(RetentionPolicy.RUNTIME)
        public @interface At { String value(); String target() default ""; int ordinal() default -1; }
    """,
    "org/spongepowered/asm/mixin/injection/Inject.java": """
        package org.spongepowered.asm.mixin.injection;
        import java.lang.annotation.*;
        @Retention(RetentionPolicy.RUNTIME) @Target(ElementType.METHOD)
        public @interface Inject { String[] method(); At[] at(); boolean cancellable() default false; }
    """,
    "org/spongepowered/asm/mixin/injection/Redirect.java": """
        package org.spongepowered.asm.mixin.injection;
        import java.lang.annotation.*;
        @Retention(RetentionPolicy.RUNTIME) @Target(ElementType.METHOD)
        public @interface Redirect { String[] method(); At at(); }
    """,
    "org/spongepowered/asm/mixin/injection/callback/CallbackInfo.java": """
        package org.spongepowered.asm.mixin.injection.callback;
        public class CallbackInfo {}
    """,
    "net/minecraftforge/fml/common/Mod.java": """
        package net.minecraftforge.fml.common;
        import java.lang.annotation.*;
        @Retention(RetentionPolicy.RUNTIME) @Target(ElementType.TYPE)
        public @interface Mod { String modid(); String dependencies() default ""; }
    """,
    "net/minecraftforge/fml/common/eventhandler/SubscribeEvent.java": """
        package net.minecraftforge.fml.common.eventhandler;
        import java.lang.annotation.*;
        @Retention(RetentionPolicy.RUNTIME) @Target(ElementType.METHOD)
        public @interface SubscribeEvent {}
    """,
}


def _major(home: Path) -> str | None:
    release = home / "release"
    if not release.is_file():
        return None
    for line in release.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("JAVA_VERSION="):
            parts = line.split("=", 1)[1].strip().strip('"').split(".")
            match = re.match(r"\d+", parts[1] if parts[0] == "1" and len(parts) > 1 else parts[0])
            return match.group(0) if match else None
    return None


def find_jdks() -> dict[str, str]:
    """JDK homes (with javac) by major version: "8", "17", "26"."""
    candidates: list = []
    try:
        from types import SimpleNamespace

        from wb_core.requirements import find_jdks as workbench_jdks
        candidates += list(workbench_jdks(SimpleNamespace(core={"jdks": {"root": "/opt/jdk"}})).values())
    except ImportError:  # outside a Workbench checkout the other places still count
        candidates += list(json.loads(os.environ.get("WB_JDKS") or "{}").values())
    candidates += [v for k, v in sorted(os.environ.items()) if k == "JAVA_HOME" or re.fullmatch(r"JAVA_HOME_\d+_X64", k)]
    candidates += [Path(f).parent for f in os.environ.get("PATH", "").split(os.pathsep)
                   if f and (Path(f) / f"javac{EXE}").is_file()]
    found: dict[str, str] = {}
    for home in map(Path, candidates):
        major = _major(home) if (home / "bin" / f"javac{EXE}").is_file() else None
        if major:
            found.setdefault(major, str(home))
    return dict(sorted(found.items(), key=lambda item: int(item[0])))


def export(jdks: dict[str, str]) -> None:
    """Makes ``jdks`` the JDKs of any Workbench started after this (its lookup reads WB_JDKS)."""
    merged = json.loads(os.environ.get("WB_JDKS") or "{}")
    merged.update(jdks)
    os.environ["WB_JDKS"] = json.dumps(merged)


def newest(jdks: dict[str, str]) -> str | None:
    return jdks[max(jdks, key=int)] if jdks else None


def write_sources(folder: Path, sources: dict[str, str]) -> list[Path]:
    files = []
    for name, text in sources.items():
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(line[8:] if line.startswith(" " * 8) else line for line in text.splitlines()),
                        encoding="utf-8")
        files.append(path)
    return files


def compile_java(home: str, sources: dict[str, str], out: Path, *, classpath=(), release: int | None = None) -> Path:
    """Compiles ``{relative path: source}`` into the folder ``out``; fails the test on an error."""
    files = write_sources(out.parent / (out.name + "-src"), sources)
    out.mkdir(parents=True, exist_ok=True)
    args = [str(Path(home) / "bin" / f"javac{EXE}"), "-encoding", "UTF-8", "-nowarn", "-Xlint:-options", "-d", str(out)]
    if release:
        args += ["--release", str(release)]
    if classpath:
        args += ["-cp", os.pathsep.join(str(p) for p in classpath)]
    done = subprocess.run(args + [str(f) for f in files], capture_output=True, text=True)
    if done.returncode:
        raise AssertionError("javac failed:\n" + done.stdout + done.stderr)
    return out


def make_jar(path: Path, *, folders=(), entries: dict | None = None, manifest: dict | None = None,
             skip=()) -> Path:
    """A JAR of the files under ``folders`` plus ``entries`` (name -> bytes or text)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        if manifest is not None:
            archive.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\r\n"
                             + "".join(f"{k}: {v}\r\n" for k, v in manifest.items()) + "\r\n")
        for folder in folders:
            for file in sorted(Path(folder).rglob("*")):
                name = file.relative_to(folder).as_posix()
                if file.is_file() and not any(name.startswith(s) for s in skip):
                    archive.write(file, name)
        for name, data in (entries or {}).items():
            archive.writestr(name, data)
    return path
