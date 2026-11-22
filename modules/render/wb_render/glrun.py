"""render.gl-run: a project's GPU test task run on the host under the GPU lease, its results kept and its context
checked against a gate.

The task (a Gradle test task that opens a hidden GL context, ``glTest`` by default) runs through the checkout's own
wrapper. The JUnit XML results the ``junit`` globs match and that the run wrote are read: every failing or erroring
test is named in a finding, and the counts become measurements. The run's capability report, the newest file the
``report`` glob matches that the run wrote, is read as a ``wb/gl-probe/v1`` report (or a glxinfo dump; the keys are
in ``wb_render/gl.py``) and judged against ``gate`` (``[modules.render.gates]``), as ``render.gl-gate`` judges one: a
missing report, or a check it fails, is a finding. Results and report are kept under the out folder.
"""

from __future__ import annotations

import os
import shutil
import xml.etree.ElementTree as ElementTree
from pathlib import Path

from wb_core.module import Context, ParamError

from . import gl


def _stamps(root: Path, patterns: list[str]) -> dict[Path, int]:
    """Each matching file's modification time, to tell afterwards which files a run wrote."""
    return {p: p.stat().st_mtime_ns for pattern in patterns for p in root.glob(pattern) if p.is_file()}


def _written(root: Path, patterns: list[str], before: dict[Path, int]) -> list[Path]:
    """The matching files that are new since ``before``, or changed."""
    return sorted(p for p, stamp in _stamps(root, patterns).items() if before.get(p) != stamp)


def junit_results(paths: list[Path]) -> tuple[dict, list[str]]:
    """Totals over JUnit XML files and the failing tests as ``Class.test: message``."""
    totals = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    failing: list[str] = []
    for path in paths:
        try:
            root = ElementTree.parse(path).getroot()
        except ElementTree.ParseError as error:
            failing.append(f"{path.name}: not JUnit XML ({error})")
            continue
        suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
        for suite in suites:
            for key in totals:
                totals[key] += int(suite.get(key) or 0)
            for case in suite.findall("testcase"):
                for kind in ("failure", "error"):
                    problem = case.find(kind)
                    if problem is not None:
                        message = (problem.get("message") or problem.text or kind).strip().splitlines()[0][:160]
                        failing.append(f"{case.get('classname', '')}.{case.get('name', '')}: {message}")
    return totals, failing


def gl_run(ctx: Context, checkout: str, task: str, project: str, gradle: list, args: list, junit: list, report: str,
           gate: str, deadline: float, jdk: str):
    from wb_lab import process as processes  # the lab's process runner: deadlines and cancelling that kill the tree

    root = Path(checkout)
    if not root.is_dir():
        raise ParamError(f"checkout: {checkout} is not a folder")
    gate_table = None
    if gate:
        configured = ctx.config.get("gates") or {}
        if gate not in configured:
            raise ParamError(f"no gate {gate!r} under [modules.render.gates] (known: "
                             f"{', '.join(sorted(configured)) or 'none'})")
        try:
            gate_table = gl.check_gate(dict(configured[gate]), f"[modules.render.gates.{gate}]")
        except ValueError as error:
            raise ParamError(str(error)) from None
    if gradle:
        wrapper = [str(item).replace("{checkout}", str(root)) for item in gradle]
    elif os.name == "nt":
        wrapper = [str(root / "gradlew.bat")]
    else:
        wrapper = ["sh", str(root / "gradlew")]
    env = {}
    if jdk:
        home = ctx.jdk(jdk)
        env = {"JAVA_HOME": str(home), "PATH": str(home / "bin") + os.pathsep + os.environ.get("PATH", "")}
    argv = [*wrapper, "--console=plain", *[str(a) for a in args], f"{':' + project + ':' if project else ''}{task}"]
    ctx.progress("run", msg=f"{task} in {ctx.host_path(root)}")
    before_results, before_reports = _stamps(root, junit), _stamps(root, [report] if report else [])
    ran = processes.run(ctx, argv, cwd=root, timeout=deadline, env=env)
    (ctx.out / "run.log").write_text(ran.output + "\n", encoding="utf-8")
    ctx.output("run-log", ctx.out / "run.log")
    results = _written(root, junit, before_results)
    totals, failing = junit_results(results)
    kept = ctx.out / "results"
    for path in results:
        target = kept / path.relative_to(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    for key, value in totals.items():
        ctx.measure(f"tests_{key}" if key != "tests" else "tests", value)
    problems = []
    if ran.code is None:
        problems.append(f"{task} did not end within {processes.span(deadline)} and was stopped")
    problems += [f"failed: {item}" for item in failing]
    if ran.code not in (0, None) and not failing:
        last = [line for line in ran.output.splitlines() if line.strip()][-1:] or ["no output"]
        problems.append(f"{task} exited {ran.code}: {last[0][:200]}")
    card, checks = None, []
    reports = _written(root, [report], before_reports) if report else []
    if reports:
        newest = max(reports, key=lambda p: p.stat().st_mtime)
        shutil.copy2(newest, ctx.out / ("capabilities" + newest.suffix))
        ctx.output("capabilities", ctx.out / ("capabilities" + newest.suffix))
        try:
            probe = gl.read_report(newest)
        except (OSError, ValueError) as error:
            problems.append(f"the capability report {newest.name} cannot be read: {error}")
        else:
            card = {key: probe.get(key) for key in ("vendor", "renderer", "version", "profile")}
            if gate_table is not None:
                checks = gl.judge(probe, gate_table)
                problems += [f"gate {gate}: not {c['check']} (measured {c.get('measured')})" for c in checks
                             if not c["passed"]]
    elif gate_table is not None:
        problems.append(f"the run wrote no capability report matching {report!r}, so gate {gate} cannot judge it")
    for problem in problems:
        ctx.finding(problem)
    if not problems:
        ctx.summary(f"{task}: {totals['tests']} test(s), {totals['skipped']} skipped, none failed"
                    + (f"; {card['renderer']} {card['version']} passes gate {gate}" if card and gate_table else ""))
    return {"exit": ran.code, **totals, "card": card, "checks": checks, "problems": problems}
