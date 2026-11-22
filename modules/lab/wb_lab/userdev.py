"""lab.userdev: a development client run from a project's own build, in a fresh run folder, with evidence.

The project's dev-run task (``runClient`` by default, any JavaExec task) is run through its Gradle wrapper with an
init script Workbench writes for the run: it points the task's working folder at the job's fresh ``run/`` folder,
gives it the ``system_properties`` asked for plus ``wb.run.id`` and ``wb.run.output`` (the job's ``evidence/``
folder, where a probe in the game writes what it saw), and records the class path the client really ran on.

The files the ``inputs`` globs match are hashed before the run and again after it: a change in between is a finding,
since the run then proves nothing about either version. The run is stopped at its ``deadline``. When ``receipt``
names a file the run must write under ``evidence/``, a missing receipt, or one whose ``receipt_ok`` key is not
true, is a finding: an exit code alone never passes a run. Every file the ``captures`` globs match under the run and
evidence folders is kept in ``inventory.json`` by SHA-256, each PNG with its size read from its header, a broken PNG
being a finding.
"""

from __future__ import annotations

import fnmatch
import json
import os
import struct
import zlib
from datetime import UTC, datetime
from pathlib import Path

from wb_core.module import Context, EnvironmentProblem, ParamError, sha256_file

from . import process as processes

SCHEMA = "wb.lab.userdev/1"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
INIT_SCRIPT = r"""// Written by Workbench for one lab.userdev run: points the dev-run task at the run's folders.
import groovy.json.JsonOutput
import groovy.json.JsonSlurper

gradle.projectsEvaluated {
    def spec = new JsonSlurper().parse(new File(SPEC_PATH))
    def owner = spec.project ? gradle.rootProject.project(spec.project) : gradle.rootProject
    def task = owner.tasks.findByName(spec.task)
    if (task == null) {
        throw new GradleException("lab.userdev: the project has no task ${spec.task}")
    }
    if (!(task instanceof JavaExec)) {
        throw new GradleException("lab.userdev: ${spec.task} is a ${task.class.simpleName}, not a JavaExec task")
    }
    task.workingDir(new File(spec.run))
    spec.properties.each { key, value -> task.systemProperty(key, value) }
    task.systemProperty('wb.run.id', spec.id)
    task.systemProperty('wb.run.output', spec.evidence)
    task.doFirst {
        def entries = task.classpath.files.collect { file -> file.absolutePath }
        new File(spec.evidence, 'classpath.json').setText(JsonOutput.prettyPrint(JsonOutput.toJson(entries)), 'UTF-8')
    }
}
"""


def _matches(relative: str, pattern: str) -> bool:
    """A glob over POSIX paths; a leading ``**/`` also matches at the top of the folder."""
    return fnmatch.fnmatchcase(relative, pattern) or (pattern.startswith("**/")
                                                      and fnmatch.fnmatchcase(relative, pattern[3:]))


def _glob_files(root: Path, patterns: list[str]) -> dict[str, dict]:
    files: dict[str, dict] = {}
    if not root.is_dir():
        return files
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        relative = path.relative_to(root).as_posix()
        if any(_matches(relative, pattern) for pattern in patterns):
            files[relative] = {"bytes": path.stat().st_size, "sha256": sha256_file(path)}
    return files


def png_size(path: Path) -> tuple[int, int]:
    """A PNG's width and height from its header, its IHDR checksum checked; ValueError for a broken one."""
    data = path.read_bytes()[:33]
    if len(data) < 33 or data[:8] != PNG_SIGNATURE or data[12:16] != b"IHDR":
        raise ValueError("not a PNG")
    if zlib.crc32(data[12:29]) & 0xFFFFFFFF != struct.unpack(">I", data[29:33])[0]:
        raise ValueError("its IHDR checksum is wrong")
    width, height = struct.unpack(">II", data[16:24])
    if not width or not height:
        raise ValueError("it is 0 pixels in one direction")
    return width, height


def _dig(data, dotted: str):
    for part in dotted.split("."):
        data = data.get(part) if isinstance(data, dict) else None
    return data


def userdev(ctx: Context, checkout: str, task: str, project: str, gradle: list, args: list, system_properties: dict,
            inputs: list, receipt: str, receipt_ok: str, captures: list, deadline: float, jdk: str):
    root = Path(checkout)
    if not root.is_dir():
        raise ParamError(f"checkout: {checkout} is not a folder")
    for name, values in (("inputs", inputs), ("captures", captures)):
        for pattern in values:
            if pattern.startswith(("/", "\\")) or ".." in pattern.replace("\\", "/").split("/"):
                raise ParamError(f"{name}: {pattern!r} must be a glob relative to its folder")
    if gradle:
        wrapper = [str(item).replace("{checkout}", str(root)) for item in gradle]
    elif os.name == "nt":
        wrapper = [str(root / "gradlew.bat")]
    else:  # through sh: a checked-out wrapper may lack its executable bit
        wrapper = ["sh", str(root / "gradlew")]
    run, evidence = ctx.out / "run", ctx.out / "evidence"
    for folder in (run, evidence):
        if folder.exists() and any(folder.iterdir()):
            raise ParamError(f"{ctx.host_path(folder)} already holds files; a run starts from a fresh out folder")
        folder.mkdir(parents=True, exist_ok=True)
    spec = {"task": task, "project": project, "run": str(run), "evidence": str(evidence), "id": ctx.job,
            "properties": {str(k): str(v) for k, v in system_properties.items()}}
    ctx.work.mkdir(parents=True, exist_ok=True)
    (ctx.work / "userdev.json").write_text(json.dumps(spec), encoding="utf-8")
    init = ctx.work / "userdev.init.gradle"
    init.write_text(INIT_SCRIPT.replace("SPEC_PATH", json.dumps(str(ctx.work / "userdev.json"))), encoding="utf-8")
    env = {}
    if jdk:
        home = ctx.jdk(jdk)
        env = {"JAVA_HOME": str(home), "PATH": str(home / "bin") + os.pathsep + os.environ.get("PATH", "")}
    before = _glob_files(root, inputs)
    argv = [*wrapper, "--init-script", str(init), "--console=plain", *[str(a) for a in args],
            f"{':' + project + ':' if project else ''}{task}"]
    ctx.progress("run", msg=f"{task} in {ctx.host_path(root)}")
    started = datetime.now(UTC)
    ran = processes.run(ctx, argv, cwd=root, timeout=deadline, env=env)
    (ctx.out / "run.log").write_text(ran.output + "\n", encoding="utf-8")
    ctx.output("run-log", ctx.out / "run.log")
    after = _glob_files(root, inputs)
    changed = sorted(set(before) ^ set(after) | {k for k in before if k in after and before[k] != after[k]})
    classpath_file = evidence / "classpath.json"
    classpath = []
    if classpath_file.is_file():
        for entry in json.loads(classpath_file.read_text(encoding="utf-8")):
            path = Path(entry)
            classpath.append({"path": ctx.host_path(path), "sha256": sha256_file(path) if path.is_file() else None,
                              "kind": "file" if path.is_file() else "folder" if path.is_dir() else "missing"})
    kept, broken = {}, []
    for label, folder in (("run", run), ("evidence", evidence)):
        for relative, record in _glob_files(folder, captures).items():
            if relative.lower().endswith(".png"):
                try:
                    record["width"], record["height"] = png_size(folder / relative)
                except ValueError as error:
                    broken.append(f"{label}/{relative}: {error}")
            kept[f"{label}/{relative}"] = record
    receipt_record = None
    problems = []
    if ran.code is None:
        problems.append(f"the run did not end within its deadline ({processes.span(deadline)}) and was stopped")
    elif ran.code != 0:
        last = [line for line in ran.output.splitlines() if line.strip()][-1:] or ["no output"]
        problems.append(f"{task} exited {ran.code}: {last[0][:200]}")
    if changed:
        problems.append(f"{len(changed)} input file(s) changed during the run, such as {changed[0]}: the run proves "
                        "nothing about either version")
    if receipt:
        path = evidence / receipt
        if not path.is_file():
            problems.append(f"the run wrote no receipt evidence/{receipt}")
        else:
            try:
                receipt_record = json.loads(path.read_text(encoding="utf-8"))
            except ValueError as error:
                problems.append(f"evidence/{receipt} is not JSON ({error})")
            else:
                if _dig(receipt_record, receipt_ok) is not True:
                    problems.append(f"evidence/{receipt} does not say {receipt_ok} = true")
    problems += [f"broken capture {item}" for item in broken]
    record = {"schema": SCHEMA, "job": ctx.job, "checkout": ctx.host_path(root), "task": task, "argv": argv,
              "started": started.isoformat(), "seconds": ran.seconds, "exit": ran.code,
              "inputs": {"files": len(before), "changed": changed}, "classpath": classpath,
              "captures": kept, "receipt": receipt_record, "problems": problems}
    (ctx.out / "inventory.json").write_text(json.dumps({"schema": SCHEMA, "captures": kept}, indent=1) + "\n",
                                            encoding="utf-8")
    (ctx.out / "receipt.json").write_text(json.dumps(record, indent=1) + "\n", encoding="utf-8")
    for name in ("inventory", "receipt"):
        ctx.output(name, ctx.out / f"{name}.json")
    ctx.output("evidence", evidence)
    if not classpath and ran.code == 0:
        raise EnvironmentProblem("the run recorded no class path, so the init script did not reach the task")
    for problem in problems:
        ctx.finding(problem)
    if not problems:
        ctx.summary(f"{task} ran in {processes.span(ran.seconds)} on {len(classpath)} class path entries; "
                    f"{len(kept)} capture(s) kept" + (f", receipt {receipt} passed" if receipt else ""))
    return {"exit": ran.code, "captures": len(kept), "problems": problems}
