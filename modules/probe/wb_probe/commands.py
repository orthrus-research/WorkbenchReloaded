"""probe: JVM runs without the game. Small Java programs compiled against an artifact and run in
throwaway JVMs, so what they prove holds for a fresh process on a known JDK.

A probe passes when its JVM exits 0 and prints a line starting with the marker (``PROBE_OK`` unless
another is named): an exit status alone cannot tell a probe that checked something from one that
returned early. Program arguments may use ``{work}`` (a fresh folder for the run), and for
``api-consumer`` also ``{phase}``, ``{state}`` (a folder the phases share) and ``{api}`` (the API JAR).
"""

from __future__ import annotations

import json
import os
import sys
import zipfile
from pathlib import Path

from wb_core.module import Context, EnvironmentProblem, ParamError

from . import jvm

_SRC = Path(__file__).resolve().parents[2] / "tools" / "src"  # the audit's reference reader
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _paths(values: list, name: str) -> list[Path]:
    paths = [Path(v) for v in values]
    for path in paths:
        if not path.exists():
            raise ParamError(f"{name}: no such file or folder: {path}")
    return paths


def _report(ctx: Context, report: dict) -> Path:
    path = ctx.out / "report.json"
    path.write_text(json.dumps(report, indent=1, default=str) + "\n", encoding="utf-8")
    ctx.output("report", path, "application/json")
    return path


def _fill(args: list, **values) -> list[str]:
    out = []
    for arg in args:
        for key, value in values.items():
            arg = arg.replace("{" + key + "}", str(value))
        out.append(arg)
    return out


def _probes(files: list[Path]) -> list[tuple[Path, str]]:
    probes = [(f, jvm.main_class(f)) for f in files]
    return [(f, main) for f, main in probes if main]


def _unchanged(before: dict[str, str], paths: list[Path]) -> None:
    after = jvm.digests(paths)
    changed = [Path(p).name for p, digest in before.items() if after.get(p) != digest]
    if changed:
        raise EnvironmentProblem(f"inputs changed while the probes ran: {', '.join(changed)}")


def _verdict(run: jvm.Run, marker: str) -> tuple[bool, str]:
    if run.timed_out:
        return False, f"timed out after {run.seconds:.0f} s"
    if run.code != 0:
        return False, f"exited {run.code}" + (f": {run.tail()}" if run.tail() else "")
    if marker and not run.has_line(marker):
        return False, f"exited 0 without a line starting {marker}"
    return True, "passed"


# -- probe.jdk-matrix -------------------------------------------------------------------------------
def jdk_matrix(ctx: Context, sources: list, classpath: list, release: int, jdks: list, compiler: str,
               marker: str, args: list, jvm_args: list, timeout: float):
    files = jvm.sources(_paths(sources, "sources"))
    probes = _probes(files)
    if not probes:
        raise ParamError("sources: no source declares a main method")
    entries = _paths(classpath, "classpath")
    have = jvm.available(ctx)
    wanted = list(dict.fromkeys(str(j) for j in jdks)) or [k for k in have if int(k) >= release]
    if not wanted:
        raise EnvironmentProblem(f"no JDK here runs release {release} (have: {', '.join(have) or 'none'})")
    homes = {key: ctx.jdk(key) for key in wanted}
    compiler_key = compiler or (max(have, key=int) if have else wanted[-1])
    compiler_home = ctx.jdk(compiler_key)
    before = jvm.digests(files + entries)
    logs, classes = ctx.out / "logs", ctx.work / "classes"
    ctx.progress("compile", msg=f"{len(files)} source(s), --release {release}, JDK {compiler_key}")
    compiled = jvm.javac(ctx, compiler_home, release, files, entries, classes, logs / "compile.log")
    report = {"schema": "wb.probe.jdk-matrix/1", "release": release, "marker": marker,
              "compiler": {"jdk": compiler_key, "runtime": jvm.runtime(compiler_home)},
              "classpath": [ctx.host_path(p) for p in entries], "inputs": {ctx.host_path(p): d for p, d in before.items()},
              "compile": {"exit": compiled.code, "log": "logs/compile.log"}, "runs": []}
    if compiled.code != 0:
        ctx.finding(f"the probes do not compile against the class path with --release {release}: "
                    + (compiled.tail(2) or f"javac exited {compiled.code}"))
        path = _report(ctx, report)
        return {"passed": False, "compiled": False, "jdks": {}, "runs": [], "report": ctx.host_path(path)}
    path_list = os.pathsep.join(str(p) for p in [classes, *entries])
    total = len(homes) * len(probes)
    for key, home in homes.items():
        for source, main in probes:
            ctx.check_cancel()
            ctx.progress("run", len(report["runs"]) / total, f"{main} on JDK {key}")
            work = ctx.work / "runs" / f"{key}-{main}"
            work.mkdir(parents=True, exist_ok=True)
            log = f"logs/{key}-{main}.log"
            done = jvm.run(ctx, [jvm.tool(home, "java"), *jvm_args, "-cp", path_list, main,
                                 *_fill(args, work=work)], timeout=timeout, cwd=work, log=ctx.out / log)
            passed, why = _verdict(done, marker)
            report["runs"].append({"jdk": key, "runtime": jvm.runtime(home), "probe": main, "source": source.name,
                                   "exit": done.code, "passed": passed, "why": why, "seconds": done.seconds,
                                   "log": log})
            ctx.measure("probe_seconds", done.seconds, unit="s", jdk=key, probe=main)
            if not passed:
                ctx.finding(f"JDK {key}: {main} {why}")
    _unchanged(before, files + entries)
    per_jdk = {key: all(r["passed"] for r in report["runs"] if r["jdk"] == key) for key in homes}
    report["jdks"] = per_jdk
    path = _report(ctx, report)
    if ctx.outcome == "ok":
        ctx.summary(f"{len(probes)} probe(s) passed on JDK {', '.join(homes)} (--release {release})")
    return {"passed": ctx.outcome == "ok", "compiled": True, "jdks": per_jdk,
            "runs": [{k: r[k] for k in ("jdk", "probe", "exit", "passed", "why")} for r in report["runs"]],
            "report": ctx.host_path(path)}


# -- probe.api-consumer -----------------------------------------------------------------------------
def _forbidden(api: Path, forbid: list[str]) -> list[str]:
    """Classes of the API JAR that refer to a forbidden package, as the audit reads references."""
    from orthrus_tools import classfile
    from orthrus_tools.jar_audit import MAX_CLASS_BYTES, denied, internal, references
    prefixes = [internal(p) for p in forbid if internal(p)]
    found = []
    with zipfile.ZipFile(api) as archive:
        for info in archive.infolist():
            if not info.filename.endswith(".class") or info.file_size > MAX_CLASS_BYTES:
                continue
            parsed = classfile.parse(archive.read(info))
            structural, _ = references(parsed)
            for reference in sorted(structural):
                package = denied(reference, prefixes)
                if package and not denied(parsed.this_class, [package]):
                    found.append(f"{parsed.this_class.replace('/', '.')} refers to {reference.replace('/', '.')}")
    return found


def _missing(ctx: Context, home: Path, api: Path, libraries: list[Path], release: int, log: Path,
             timeout: float) -> list[str]:
    """What jdeps finds the API JAR depends on that neither it, the libraries nor the JDK provide."""
    args = [jvm.tool(home, "jdeps"), "--missing-deps", "--multi-release", str(release) if release >= 9 else "base"]
    if libraries:
        args += ["-cp", os.pathsep.join(str(p) for p in libraries)]
    done = jvm.run(ctx, [*args, api], timeout=timeout, log=log)
    if done.code != 0:
        raise EnvironmentProblem(f"jdeps exited {done.code}: {done.tail()}")
    return [" ".join(line.replace("not found", "").split()) for line in done.output.splitlines()
            if line.startswith((" ", "\t")) and "->" in line and line.rstrip().endswith("not found")]


def api_consumer(ctx: Context, api: str, consumer: list, libraries: list, release: int, jdk: str, phases: list,
                 args: list, marker: str, jvm_args: list, forbid: list, links: bool, timeout: float, main=None):
    api_jar = Path(api)
    if not api_jar.is_file() or not zipfile.is_zipfile(api_jar):
        raise ParamError(f"api: not a JAR: {api}")
    libs = _paths(libraries, "libraries")
    files = jvm.sources(_paths(consumer, "consumer"))
    mains = [m for _, m in _probes(files)]
    if main is None:
        if len(mains) != 1:
            raise ParamError(f"consumer: several sources declare main ({', '.join(mains)}); name one as main"
                             if mains else "consumer: no source declares a main method")
        main = mains[0]
    if not phases:
        raise ParamError("phases: name at least one")
    home = ctx.jdk(jdk)
    before = jvm.digests([api_jar, *libs, *files])
    logs = ctx.out / "logs"
    report = {"schema": "wb.probe.api-consumer/1", "api": ctx.host_path(api_jar), "release": release,
              "jdk": {"key": jdk, "runtime": jvm.runtime(home)}, "libraries": [ctx.host_path(p) for p in libs],
              "inputs": {ctx.host_path(p): d for p, d in before.items()}, "main": main, "marker": marker}
    report["forbidden"] = _forbidden(api_jar, list(forbid)) if forbid else []
    for line in report["forbidden"][:20]:
        ctx.finding(f"the API JAR links a forbidden package: {line}")
    report["missing"] = _missing(ctx, home, api_jar, libs, release, logs / "jdeps.log", timeout) if links else None
    for line in (report["missing"] or [])[:20]:
        ctx.finding(f"the API JAR depends on a class nothing provides: {line}")
    classes = ctx.work / "consumer"
    ctx.progress("compile", msg=f"{main} against the API JAR alone, --release {release}")
    compiled = jvm.javac(ctx, home, release, files, [api_jar, *libs], classes, logs / "compile.log")
    report["compile"] = {"exit": compiled.code, "log": "logs/compile.log"}
    report["phases"] = []
    if compiled.code != 0:
        ctx.finding("the consumer does not compile against the API JAR alone: "
                    + (compiled.tail(2) or f"javac exited {compiled.code}"))
    else:
        state = ctx.work / "state"
        state.mkdir(parents=True, exist_ok=True)
        path_list = os.pathsep.join(str(p) for p in [classes, api_jar, *libs])
        for index, phase in enumerate(phases, 1):
            ctx.check_cancel()
            ctx.progress("run", (index - 1) / len(phases), f"phase {phase} in a fresh JVM")
            work = ctx.work / "phases" / f"{index}-{phase}"
            work.mkdir(parents=True, exist_ok=True)
            log = f"logs/{index}-{phase}.log"
            done = jvm.run(ctx, [jvm.tool(home, "java"), *jvm_args, "-cp", path_list, main,
                                 *_fill(args, phase=phase, state=state, api=api_jar, work=work)],
                           timeout=timeout, cwd=work, log=ctx.out / log)
            passed, why = _verdict(done, marker)
            report["phases"].append({"phase": phase, "exit": done.code, "passed": passed, "why": why,
                                     "seconds": done.seconds, "log": log})
            if not passed:
                ctx.finding(f"phase {phase}: {main} {why}")
                break  # a later phase builds on this one
    _unchanged(before, [api_jar, *libs, *files])
    path = _report(ctx, report)
    if ctx.outcome == "ok":
        ctx.summary(f"{main} compiled against the API JAR alone (--release {release}) and passed "
                    f"{len(phases)} phase(s) in fresh JVMs on JDK {jdk}")
    return {"passed": ctx.outcome == "ok", "compiled": compiled.code == 0,
            "phases": [{k: p[k] for k in ("phase", "exit", "passed", "why")} for p in report["phases"]],
            "missing": report["missing"] or [], "forbidden": report["forbidden"], "report": ctx.host_path(path)}
