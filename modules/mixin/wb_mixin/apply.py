"""Apply Mixin configurations offline through a project's own harness, and report what it found.

Workbench does not apply Mixins itself: a harness JAR does, on the Mixin build the project ships against,
and Workbench runs it in a fresh JVM and reads its report. The harness follows this contract:

    <main> apply --out REPORT --config A.json[,B.json] [--production] [--server] [--strict] ENTRY...
    <main> compare [--by-target] [--removed HANDLER@MIXIN[,...]] BEFORE AFTER

- ``apply`` applies the configurations (resource names, in registration order) over the class path
  entries: the Mixins with their configurations and refmaps, the target classes and every class those
  extend or implement. ``--production`` resolves every reference through its configuration's refmap over
  production-named classes; ``--server`` applies the server side; ``--strict`` also fails an injector
  that hooks nothing. It writes ``REPORT``.
- ``compare`` compares two reports: per calling method, or per target class with ``--by-target`` (a
  development run against a production run). Handlers named with ``--removed`` were removed on purpose.
- Exit status: 0 clean, 1 Mixins failed to apply or the reports differ, 2 wrong usage.
- A report is tab-separated text, one row per line, its first field the row's kind: ``TARGET``,
  ``MERGED``, ``HOOK``, ``FAIL``, ``MISSING`` or ``NOTE``. ``FAIL`` and ``MISSING`` rows are failures.
"""

from __future__ import annotations

import os
import zipfile
from collections import Counter
from pathlib import Path

from wb_core.module import Context, EnvironmentProblem, ParamError
from wb_probe import jvm

KINDS = ("TARGET", "MERGED", "HOOK", "FAIL", "MISSING", "NOTE")
FAILED = ("FAIL", "MISSING")


def main_of(tool: Path) -> str | None:
    """The ``Main-Class`` of a JAR's manifest."""
    try:
        with zipfile.ZipFile(tool) as archive:
            text = archive.read("META-INF/MANIFEST.MF").decode("utf-8", "replace")
    except (KeyError, OSError, zipfile.BadZipFile):
        return None
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip() == "Main-Class":
            return value.strip()
    return None


def read_report(path: Path) -> list[list[str]]:
    if not path.is_file():
        return []
    return [line.split("\t") for line in path.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()]


def harness(java: Path, tool: list[Path], main: str, jvm_args: list) -> list:
    return [java, *jvm_args, "-cp", os.pathsep.join(str(p) for p in tool), main]


def apply(ctx: Context, *, java: Path, tool: list[Path], main: str | None, configs: list[str], classpath: list[Path],
          production: bool, side: str, strict: bool, baseline: Path | None, by_target: bool, removed: list[str],
          jvm_args: list, timeout: float) -> dict:
    main = main or main_of(tool[0])
    if not main:
        raise ParamError(f"{tool[0].name} names no Main-Class; give the harness's main class as main")
    report_path = ctx.out / "report.txt"
    report_path.unlink(missing_ok=True)  # an output folder used before must not lend this run its report
    command = harness(java, tool, main, jvm_args) + ["apply", "--out", report_path, "--config", ",".join(configs)]
    command += (["--production"] if production else []) + (["--server"] if side == "server" else [])
    command += (["--strict"] if strict else []) + classpath
    ctx.progress("apply", msg=f"{len(configs)} configuration(s) over {len(classpath)} class path entries")
    done = jvm.run(ctx, command, timeout=timeout, log=ctx.out / "apply.log")
    if done.timed_out:
        raise EnvironmentProblem(f"the harness did not finish in {timeout:.0f} s")
    if done.code == 2:
        raise ParamError(f"the harness refused the request: {done.tail()}")
    if done.code not in (0, 1):
        raise EnvironmentProblem(f"the harness exited {done.code}: {done.tail()}")
    if not report_path.is_file():
        raise EnvironmentProblem(f"the harness wrote no report (exit {done.code}): {done.tail()}")
    rows = read_report(report_path)
    counts = Counter(row[0] for row in rows)
    failures = [row for row in rows if row[0] in FAILED]
    result = {"applied": done.code == 0, "exit": done.code, "counts": {k: counts.get(k, 0) for k in KINDS},
              "failures": ["\t".join(row) for row in failures], "notes": ["\t".join(r) for r in rows if r[0] == "NOTE"]}
    if baseline is not None:
        compare = harness(java, tool, main, jvm_args) + ["compare"] + (["--by-target"] if by_target else [])
        compare += (["--removed", ",".join(removed)] if removed else []) + [baseline, report_path]
        compared = jvm.run(ctx, compare, timeout=timeout, log=ctx.out / "compare.log")
        if compared.timed_out or compared.code not in (0, 1, 2):
            raise EnvironmentProblem(f"the harness's compare exited {compared.code}: {compared.tail()}")
        if compared.code == 2:
            raise ParamError(f"the harness refused the comparison: {compared.tail()}")
        result["compared"] = {"same": compared.code == 0, "by_target": by_target,
                              "differences": [line for line in compared.output.splitlines() if line.strip()][:200]}
    result["output"] = done.output.splitlines()[-20:]
    return result
