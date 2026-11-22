"""render: shader pack checks, shader load verdicts from game logs, and GL qualification of a card."""

from __future__ import annotations

import gzip
import hashlib
import json
import zipfile
from pathlib import Path

from wb_core.module import Context, ParamError, sha256_file

from . import gl, glslang, verdict
from . import hazards as lint
from . import profiles as profile_files
from .pack import EXTENSIONS, Flat, Pack

LIMIT = 25  # findings of one kind shown one by one; the rest are counted in one more


def _table(path: str, what: str) -> dict:
    try:
        return profile_files.read_table(Path(path))
    except (OSError, ValueError) as error:
        raise ParamError(f"{what}: {error}") from None


def _write_json(ctx: Context, name: str, data) -> Path:
    path = ctx.out / name
    path.write_text(json.dumps(data, indent=2, default=str) + "\n", encoding="utf-8")
    ctx.output(path.stem, path, "application/json")
    return path


def _findings(ctx: Context, lines: list[str], label: str):
    for line in lines[:LIMIT]:
        ctx.finding(line)
    if len(lines) > LIMIT:
        ctx.finding(f"{len(lines) - LIMIT} more {label} (see the report)")


# ----------------------------------------------------------------------------------------------
# render.validate
def _shaders_root(ctx: Context, pack: str) -> Path:
    path = Path(pack)
    if path.is_file() and path.suffix.lower() == ".zip":
        target = ctx.work / "pack"
        try:
            with zipfile.ZipFile(path) as archive:
                for member in archive.infolist():
                    name = member.filename.replace("\\", "/")
                    if member.is_dir() or name.startswith("/") or ".." in name.split("/") or ":" in name:
                        continue
                    out = target / name
                    out.parent.mkdir(parents=True, exist_ok=True)
                    out.write_bytes(archive.read(member))
        except zipfile.BadZipFile as error:
            raise ParamError(f"{path.name} is not a readable zip: {error}") from None
        path = target
    if not path.is_dir():
        raise ParamError(f"{pack} is not a shader pack folder or .zip")
    if (path / "shaders").is_dir():
        return path / "shaders"
    folders = [p for p in path.iterdir() if p.is_dir()]
    if len(folders) == 1 and (folders[0] / "shaders").is_dir():
        return folders[0] / "shaders"
    return path


def _located(found: list[dict], flat: Flat) -> list[dict]:
    for message in found:
        if message.get("line"):
            message["file"], message["source_line"] = flat.where(message["line"])
    return found


def _first_error(found: list[dict]) -> str:
    error = next((m for m in found if m["level"] == "error"), None)
    if error is None:
        return "no message"
    where = f"{error['file']}:{error['source_line']}: " if error.get("file") and error.get("source_line") else ""
    return (where + error["text"] + (f" {error['detail']}" if error.get("detail") else ""))[:300]


def validate(ctx: Context, pack: str, version: str, required: list, hazards: str, reviewed: list,
             profiles: str | None = None):
    if version and version != "any":
        try:
            Pack.normal_version(version)
        except ValueError as error:
            raise ParamError(f"version: {error}") from None
    root = _shaders_root(ctx, pack)
    model = Pack(root)
    if not model.stages:
        raise ParamError(f"{pack} has no shader stages (.vsh, .fsh, .gsh, .csh, .tcs, .tes) in its shaders folder")
    try:
        chosen = profile_files.resolve(model, profile_files.parse(_table(profiles, "profiles") if profiles else None))
    except ValueError as error:
        raise ParamError(f"profiles: {error}") from None

    ctx.progress("contract")
    problems = model.contract(version=version, required=required,
                              environments=[model.environment(p.values, p.defines) for p in chosen])
    found = [] if hazards == "off" else lint.lint({rel: "\n".join(model.lines(rel)) for rel in model.sources},
                                                  reviewed)

    tool = glslang.executable()
    validator = glslang.version(ctx, tool)
    stages, links = [], []
    flat_root = ctx.out / "flat"
    for index, profile in enumerate(chosen):
        ctx.check_cancel()
        ctx.progress("compile", index / len(chosen), f"profile {profile.name}")
        for name, program in model.programs.items():
            if not model.selected(name, profile.only, profile.exclude):
                continue
            compiled: dict[str, tuple[bool, Path]] = {}
            for kind, stage in program.stages.items():
                flat = model.flatten(stage, profile.values, profile.defines)
                target = flat_root / profile.name / (name + EXTENSIONS[kind])
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(flat.text, encoding="utf-8", newline="\n")
                ok, log = glslang.compile_stage(ctx, tool, target)
                stages.append({"profile": profile.name, "program": name, "stage": stage, "kind": kind, "compiled": ok,
                               "flat": target.relative_to(ctx.out).as_posix(),
                               "sha256": hashlib.sha256(flat.text.encode("utf-8")).hexdigest(),
                               "messages": _located(glslang.messages(log), flat)})
                compiled[kind] = (ok, target)
            graphics = [kind for kind in ("vert", "tesc", "tese", "geom", "frag") if kind in compiled]
            if not {"vert", "frag"} <= set(graphics):
                continue
            if all(compiled[kind][0] for kind in graphics):
                ok, log = glslang.link(ctx, tool, [compiled[kind][1] for kind in graphics])
                links.append({"profile": profile.name, "program": name, "linked": ok, "messages": glslang.messages(log)})
            else:
                links.append({"profile": profile.name, "program": name, "linked": None,
                              "skipped": "a stage does not compile"})
    if not stages:
        raise ParamError("profiles: every program is excluded, so nothing was compiled")

    # One finding per cause: the stages (or programs) and profiles that fail the same way are named together.
    failures: dict[tuple[str, str], dict[str, list[str]]] = {}
    for kind, entries, subject, failed in (("compile", stages, "stage", lambda e: not e["compiled"]),
                                           ("link", links, "program", lambda e: e["linked"] is False)):
        for entry in entries:
            if failed(entry):
                item = failures.setdefault((kind, _first_error(entry["messages"])), {"what": [], "profiles": []})
                item["what"] = list(dict.fromkeys(item["what"] + [entry[subject]]))
                item["profiles"] = list(dict.fromkeys(item["profiles"] + [entry["profile"]]))
    lines = []
    for (kind, why), item in failures.items():
        what = item["what"]
        named = ", ".join(what[:3]) + (f" and {len(what) - 3} more" if len(what) > 3 else "")
        subject = named if kind == "compile" else ("program " if len(what) == 1 else "programs ") + named
        verb = ("does not " if len(what) == 1 else "do not ") + kind
        under = f" (profile {', '.join(item['profiles'])})" if len(chosen) > 1 else ""
        lines.append(f"{subject} {verb}{under}: {why}")
    _findings(ctx, [f"contract: {problem}" for problem in problems], "contract problems")
    _findings(ctx, lines, "compile or link failures")
    errors = [h for h in found if h.level == "error"]
    review = [h for h in found if h.level == "review" and not h.reviewed]
    _findings(ctx, [f"driver hazard: {h}" for h in errors], "driver hazards")
    if hazards == "review":
        _findings(ctx, [f"driver hazard to review: {h}" for h in review], "shapes to review")

    per_profile = []
    for profile in chosen:
        mine = [s for s in stages if s["profile"] == profile.name]
        linked = [l for l in links if l["profile"] == profile.name]
        per_profile.append({"profile": profile.name, "stages": len(mine), "compiled": sum(s["compiled"] for s in mine),
                            "programs": len(linked), "linked": sum(l["linked"] is True for l in linked),
                            "failed": [s["stage"] for s in mine if not s["compiled"]]
                            + [l["program"] for l in linked if l["linked"] is False]})
    report = {"pack": Path(pack).name, "validator": validator,
              "programs": {n: p.stages for n, p in model.programs.items()},
              "options": {n: {"kind": o.kind, "default": o.default, "allowed": o.allowed, "file": o.file, "line": o.line}
                          for n, o in model.options.items()},
              "profiles": [p.as_dict() for p in chosen], "contract": [p.as_dict() for p in problems],
              "hazards": [h.as_dict() for h in found], "stages": stages, "links": links}
    _write_json(ctx, "report.json", report)
    ctx.output("flat", flat_root)
    ctx.measure("stages_compiled", sum(s["compiled"] for s in stages))
    ctx.measure("compile_failures", sum(not s["compiled"] for s in stages))
    ctx.measure("link_failures", sum(l["linked"] is False for l in links))
    ctx.measure("contract_problems", len(problems))
    ctx.measure("driver_hazards", len(errors))
    if not ctx.findings:
        ctx.summary(f"{len(model.stages)} stages in {len(model.programs)} programs compile and link under "
                    f"{len(chosen)} profile(s); the pack contract holds"
                    + (f"; {len(review)} loop shape(s) listed for review" if review else ""))
    return {"passed": not ctx.findings, "stages": len(model.stages), "programs": len(model.programs),
            "options": len(model.options), "profiles": per_profile, "contract": [str(p) for p in problems][:100],
            "hazards": [h.as_dict() for h in found][:100], "validator": validator}


# ----------------------------------------------------------------------------------------------
# render.load-verdict
def load_verdict(ctx: Context, log: str, pack: str, profile: str, dimensions: list, min_captures: int, loader: str,
                 patterns: str | None = None, screenshots: str | None = None):
    overlay = _table(patterns, "patterns") if patterns else None
    try:
        rules = verdict.rules(loader, ctx.config.get("loaders") or {}, overlay)
    except ValueError as error:
        raise ParamError(str(error)) from None
    path = Path(log)
    if not path.is_file():
        raise ParamError(f"{log} is not a log file")
    try:
        result = verdict.evaluate(verdict.read_lines(path), rules, pack=pack, profile=profile, dimensions=dimensions,
                                  min_captures=min_captures)
    except (OSError, EOFError, gzip.BadGzipFile) as error:
        raise ParamError(f"cannot read {path.name}: {error}") from None
    if screenshots:
        folder = Path(screenshots)
        if not folder.is_dir():
            raise ParamError(f"screenshots: {screenshots} is not a folder")
        result["reasons"] += verdict.check_captures(result, folder)
        result["passed"] = not result["reasons"]
    result["log"] = {"name": path.name, "sha256": sha256_file(path)}
    result["loader"] = loader
    _write_json(ctx, "verdict.json", result)
    for reason in result["reasons"]:
        ctx.finding(reason)
    ctx.measure("fatal_lines", len(result["fatal"]))
    ctx.measure("captures", len(result["captures"]))
    if result["passed"]:
        first = next(item for item in result["active"] if not pack or verdict._pack_name(item["pack"]) == verdict._pack_name(pack))
        ctx.summary(f"{first['pack']} loaded (active at line {first['line']}; {len(result['loads'])} completed load(s); "
                    f"{len(result['captures'])} capture(s)); no fallback, load, compile or option errors")
    return {"passed": result["passed"], "reasons": result["reasons"], "active": [a["pack"] for a in result["active"]],
            "loads": len(result["loads"]), "captures": len(result["captures"]), "fatal": len(result["fatal"]),
            "other_errors": len(result["other_errors"])}


# ----------------------------------------------------------------------------------------------
# render.gl-gate
def gl_gate(ctx: Context, report: str, gate: str, gate_file: str | None = None, reference: str | None = None,
            min_version: str | None = None, profile: str | None = None, vendors: list | None = None,
            min_memory_mib: int | None = None, extensions: list | None = None, os_specific: list | None = None):
    merged: dict = {}
    try:
        if gate:
            configured = ctx.config.get("gates") or {}
            if gate not in configured:
                raise ParamError(f"no gate {gate!r} under [modules.render.gates] (known: "
                                 f"{', '.join(sorted(configured)) or 'none'})")
            merged.update(gl.check_gate(dict(configured[gate]), f"[modules.render.gates.{gate}]"))
        if gate_file:
            merged.update(gl.check_gate(_table(gate_file, "gate_file"), Path(gate_file).name))
        given = {"min_version": min_version, "profile": profile, "vendors": vendors, "min_memory_mib": min_memory_mib,
                 "extensions": extensions, "os_specific": os_specific}
        merged.update({key: value for key, value in given.items() if value is not None})
        gl.check_gate(merged, "the gate")
    except ValueError as error:
        raise ParamError(str(error)) from None
    if not any(merged.get(k) for k in ("min_version", "vendors", "min_memory_mib", "extensions")) \
            and merged.get("profile", "any") == "any":
        raise ParamError("the gate checks nothing: name a gate, give gate_file, or pass min_version, profile, vendors, "
                         "min_memory_mib or extensions")
    try:
        probe = gl.read_report(Path(report))
        other = gl.read_report(Path(reference)) if reference else None
    except (OSError, ValueError) as error:
        raise ParamError(str(error)) from None
    checks = gl.judge(probe, merged)
    failed = [check for check in checks if not check["passed"]]
    card = {key: probe.get(key) for key in ("machine", "os", "vendor", "renderer", "version", "profile", "core_version",
                                            "memory_mib", "driver") if probe.get(key) is not None}
    result = {"accepted": not failed, "card": card, "gate": merged, "checks": checks,
              "extensions": len(probe["extensions"])}
    if other is not None:
        result["reference"] = gl.compare(probe, other, merged)
    _write_json(ctx, "gate.json", result)
    for check in failed:
        measured = check["measured"]
        ctx.finding(f"refused: {check['check']} (the card has {measured if measured not in (None, []) else 'none'})")
    if probe.get("memory_mib") is not None:
        ctx.measure("memory", probe["memory_mib"], unit="MiB")
    if not failed:
        ctx.summary(f"accepted: {probe.get('renderer') or 'the card'}, {probe['version']}; {len(checks)} check(s) pass")
    return result
