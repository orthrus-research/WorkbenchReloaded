"""Throwaway JVMs: javac, java and jdeps runs with a hard timeout, for probes and harnesses.

Every run is a fresh process. Cancelling the job kills it, and so does its timeout, which holds even when
the process prints nothing. Its output (stdout and stderr together) goes to a log file and, line by line,
to the job log.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from wb_core.module import Cancelled, Context, EnvironmentProblem, ParamError

EXE = ".exe" if os.name == "nt" else ""
_PACKAGE = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.M)
_MAIN = re.compile(r"\bvoid\s+main\s*\(")
_COMMENTS = re.compile(r"/\*.*?\*/|//[^\n]*", re.S)


@dataclass
class Run:
    args: list[str]
    code: int | None
    """The exit status; None when the run was killed at its timeout."""
    output: str
    seconds: float

    @property
    def timed_out(self) -> bool:
        return self.code is None

    def has_line(self, marker: str) -> bool:
        """Whether a line of the output starts with ``marker`` (leading blanks ignored)."""
        return any(line.strip().startswith(marker) for line in self.output.splitlines())

    def tail(self, lines: int = 3) -> str:
        return " | ".join(line.strip() for line in self.output.strip().splitlines()[-lines:] if line.strip())


def run(ctx: Context, args: list, *, timeout: float, cwd=None, env: dict | None = None,
        log: Path | None = None) -> Run:
    args = [str(a) for a in args]
    ctx.log.debug("$ " + " ".join(args))
    started = time.monotonic()
    try:
        process = subprocess.Popen(args, cwd=cwd, env=dict(os.environ, **(env or {})), stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True,
                                   encoding="utf-8", errors="replace")
    except OSError as error:
        raise EnvironmentProblem(f"cannot start {Path(args[0]).name}: {error}") from None
    lines: list[str] = []

    def pump():
        for line in process.stdout:
            lines.append(line.rstrip("\n"))
            ctx.log.debug(lines[-1])

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    code: int | None = None
    while True:
        try:
            code = process.wait(timeout=0.25)
            break
        except subprocess.TimeoutExpired:
            if ctx.cancelled.is_set():
                process.kill()
                process.wait()
                raise Cancelled() from None
            if time.monotonic() - started > timeout:
                process.kill()
                process.wait()
                break
    reader.join(5)
    result = Run(args, code, "\n".join(lines), round(time.monotonic() - started, 3))
    if log is not None:
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(result.output + "\n", encoding="utf-8")
    return result


def tool(home: Path, name: str) -> Path:
    path = Path(home) / "bin" / f"{name}{EXE}"
    if not path.is_file():
        raise EnvironmentProblem(f"{name} is not in {home}: a JDK, not a JRE, is needed")
    return path


def release_info(home: Path) -> dict[str, str]:
    info = {}
    release = Path(home) / "release"
    if release.is_file():
        for line in release.read_text(encoding="utf-8", errors="replace").splitlines():
            key, sep, value = line.partition("=")
            if sep:
                info[key.strip()] = value.strip().strip('"')
    return info


def major(home: Path) -> int | None:
    version = release_info(home).get("JAVA_VERSION", "")
    parts = version.split(".")
    match = re.match(r"\d+", parts[1] if parts[0] == "1" and len(parts) > 1 else parts[0])
    return int(match.group(0)) if match else None


def runtime(home: Path) -> str:
    info = release_info(home)
    return info.get("JAVA_RUNTIME_VERSION") or info.get("JAVA_VERSION") or "unknown"


def available(ctx: Context) -> dict[str, Path]:
    """The JDKs Workbench has, by major version (keys like "8", "26"; early-access builds excluded)."""
    found: dict[str, Path] = {}
    for key, home in sorted((getattr(ctx, "_jdks", {}) or {}).items()):
        if key.isdigit():
            found.setdefault(key, Path(home))
    return dict(sorted(found.items(), key=lambda item: int(item[0])))


def sources(paths: list[Path]) -> list[Path]:
    """Java source files: the files given, and every .java file under the folders given."""
    out: list[Path] = []
    for path in paths:
        if path.is_dir():
            out += sorted(path.rglob("*.java"))
        elif path.is_file() and path.suffix == ".java":
            out.append(path)
        else:
            raise ParamError(f"not a Java source file or a folder of them: {path}")
    return list(dict.fromkeys(out))


def main_class(source: Path) -> str | None:
    """The class a source file declares a main method in, by its package and file name."""
    text = _COMMENTS.sub(" ", source.read_text(encoding="utf-8", errors="replace"))
    if not _MAIN.search(text):
        return None
    package = _PACKAGE.search(text)
    return f"{package.group(1)}.{source.stem}" if package else source.stem


def javac(ctx: Context, home: Path, release: int, files: list[Path], classpath: list[Path], out: Path,
          log: Path, timeout: float = 600) -> Run:
    """Compiles ``files`` with ``--release``; the arguments go through an argument file."""
    if (major(home) or 9) < 9:
        raise EnvironmentProblem(f"javac --release needs JDK 9 or later; {home} is JDK {major(home)}")
    out.mkdir(parents=True, exist_ok=True)
    lines = ["-encoding", "UTF-8", "-Xlint:-options", "--release", str(release), "-d", _quote(out)]
    if classpath:
        lines += ["-cp", _quote(os.pathsep.join(Path(p).as_posix() for p in classpath))]
    lines += [_quote(f) for f in files]
    argfile = out.parent / f"{out.name}.args"
    argfile.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return run(ctx, [tool(home, "javac"), f"@{argfile}"], timeout=timeout, log=log)


def _quote(path) -> str:
    return '"' + (Path(path).as_posix() if not isinstance(path, str) else path) + '"'


def digests(paths: list[Path]) -> dict[str, str]:
    """SHA-256 of each file, and of each folder's files by relative path, so a later change shows."""
    out = {}
    for path in paths:
        path = Path(path)
        files = sorted(p for p in path.rglob("*") if p.is_file()) if path.is_dir() else [path]
        digest = hashlib.sha256()
        for file in files:
            digest.update(file.relative_to(path).as_posix().encode() if path.is_dir() else b"")
            with open(file, "rb") as handle:
                digest.update(hashlib.file_digest(handle, "sha256").digest())
        out[str(path)] = digest.hexdigest()
    return out
