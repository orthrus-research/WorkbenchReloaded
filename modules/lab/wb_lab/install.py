"""lab.install-server: a dedicated server installed by its loader's own installer, the installer pinned by SHA-256.

The installer is checked against ``sha256`` before anything runs, copied into the job's work folder, and run with
``launch`` (by default ``java -jar <installer> --installServer <target>``) into a fresh folder, the out folder's
``server/``. What it installed is kept as ``inventory.json``, every file by size and SHA-256, beside ``install.log``
and ``receipt.json`` (the installer, the JVM, the command, the exit code and the time). The folder is then a stopped
server ``lab.server`` and ``lab.session`` take.

The Minecraft EULA is not accepted for anyone: ``eula = true`` writes ``eula.txt`` only when the request says so, as
the record of whoever made it accepting it for this folder.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path

from wb_core.module import Context, EnvironmentProblem, ParamError, sha256_file

from . import process as processes

SCHEMA = "wb.lab.install-server/1"
PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")
IN_LAUNCH = {"java", "installer", "target"}


def inventory(root: Path) -> dict[str, dict]:
    """Every file under ``root`` by its POSIX path: size and SHA-256."""
    files = {}
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        files[path.relative_to(root).as_posix()] = {"bytes": path.stat().st_size, "sha256": sha256_file(path)}
    return files


def install_server(ctx: Context, installer: str, sha256: str, jdk: str, launch: list, eula: bool, timeout: float):
    source = Path(installer)
    if not source.is_file():
        raise ParamError(f"installer: {installer} is not a file")
    unknown = sorted({m.group(1) for item in launch for m in PLACEHOLDER.finditer(str(item))} - IN_LAUNCH)
    if unknown:
        raise ParamError(f"launch: {{{unknown[0]}}} is no placeholder (known: {', '.join(sorted(IN_LAUNCH))})")
    if not launch:
        raise ParamError("launch: the installer's command line is empty")
    actual = sha256_file(source)
    if actual != sha256.lower():
        ctx.finding(f"{source.name} is SHA-256 {actual[:12]}, not the pinned {sha256[:12]}; nothing was run")
        return {"server": None, "sha256": actual}
    work_installer = ctx.work / source.name
    ctx.work.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, work_installer)
    target = ctx.out / "server"
    if target.exists() and any(target.iterdir()):
        raise ParamError(f"{ctx.host_path(target)} already holds files; install into a fresh out folder")
    target.mkdir(parents=True, exist_ok=True)
    values = {"installer": str(work_installer), "target": str(target)}
    env = {}
    if jdk:
        home = ctx.jdk(jdk)
        values["java"] = str(home / "bin" / ("java.exe" if os.name == "nt" else "java"))
        env = {"JAVA_HOME": str(home), "PATH": str(home / "bin") + os.pathsep + os.environ.get("PATH", "")}
    elif any("{java}" in str(item) for item in launch):
        raise ParamError("launch uses {java}, so it needs a jdk")
    argv = [PLACEHOLDER.sub(lambda m: values[m.group(1)], str(item)) for item in launch]
    ctx.progress("install", msg=f"{source.name} into {ctx.host_path(target)}")
    started = datetime.now(UTC)
    ran = processes.run(ctx, argv, cwd=ctx.work, timeout=timeout, env=env)
    (ctx.out / "install.log").write_text(ran.output + "\n", encoding="utf-8")
    ctx.output("install-log", ctx.out / "install.log")
    files = inventory(target)
    if eula:
        (target / "eula.txt").write_text(f"# accepted in the request of Workbench job {ctx.job}\neula=true\n",
                                         encoding="utf-8")
    receipt = {"schema": SCHEMA, "job": ctx.job, "installer": {"path": ctx.host_path(source), "sha256": actual,
                                                                "bytes": source.stat().st_size},
               "jdk": jdk or None, "argv": argv, "started": started.isoformat(), "seconds": ran.seconds,
               "exit": ran.code, "files": len(files), "eula": bool(eula)}
    (ctx.out / "inventory.json").write_text(json.dumps({"schema": SCHEMA, "files": files}, indent=1) + "\n",
                                            encoding="utf-8")
    (ctx.out / "receipt.json").write_text(json.dumps(receipt, indent=1) + "\n", encoding="utf-8")
    ctx.output("inventory", ctx.out / "inventory.json")
    ctx.output("receipt", ctx.out / "receipt.json")
    if ran.code is None:
        raise EnvironmentProblem(f"the installer was stopped at its timeout ({processes.span(timeout)})")
    if ran.code != 0:
        last = [line for line in ran.output.splitlines() if line.strip()][-1:] or ["no output"]
        ctx.finding(f"the installer exited {ran.code}: {last[0][:200]}")
        return {"server": None, "exit": ran.code}
    if not files:
        ctx.finding("the installer exited 0 but installed nothing")
        return {"server": None, "exit": 0}
    ctx.output("server", target)
    ctx.summary(f"{source.name} installed {len(files)} file(s) into {ctx.host_path(target)} in "
                f"{processes.span(ran.seconds)}" + ("" if eula else "; the EULA is not accepted (eula=true)"))
    return {"server": ctx.host_path(target), "exit": 0, "files": len(files), "sha256": actual}
