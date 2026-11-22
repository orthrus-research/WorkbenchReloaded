"""mixin: verifying Mixin and patch application without the game (remap-proof, apply, doctor).

``remap-proof`` and ``doctor`` read JARs' bytes (``wb_jar``); ``apply`` runs a project's harness in a fresh
JVM (``wb_probe.jvm``). Each writes ``report.json`` in the job's output folder.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from wb_core.module import Context, ParamError, sha256_file

LISTED = 20  # findings named one by one; the report holds every one


def _report(ctx: Context, report: dict) -> Path:
    path = ctx.out / "report.json"
    path.write_text(json.dumps(report, indent=1, default=str) + "\n", encoding="utf-8")
    ctx.output("report", path, "application/json")
    return path


def _files(values: list, name: str) -> list[Path]:
    paths = [Path(v) for v in values]
    for path in paths:
        if not path.exists():
            raise ParamError(f"{name}: no such file or folder: {path}")
    return paths


def _findings(ctx: Context, lines: list[str], total: int | None = None) -> None:
    for line in lines[:LISTED]:
        ctx.finding(line)
    rest = (total if total is not None else len(lines)) - min(len(lines), LISTED)
    if rest > 0:
        ctx.finding(f"and {rest} more (see report.json)")


# -- mixin.remap-proof ------------------------------------------------------------------------------
def remap_proof(ctx: Context, jar: str, mappings: list, reverse_mappings: list, owners: list, expect: dict,
                native: list, refmap=None, environment=None):
    from wb_jar.archive import Archive

    from .mappings import MappingError, load
    from .remap import prove

    path = Path(jar)
    if not path.is_file() or not zipfile.is_zipfile(path):
        raise ParamError(f"jar: not a JAR: {jar}")
    files = _files(mappings, "mappings")
    reversed_files = _files(reverse_mappings, "reverse_mappings")
    try:
        names = load(files, reversed_files)
    except MappingError as error:
        raise ParamError(f"mappings: {error}") from None
    for mixin, entries in (expect or {}).items():
        if not isinstance(entries, dict) or not all(isinstance(v, str) for v in entries.values()):
            raise ParamError(f"expect.{mixin}: expected a table of selector = mapped selector (\"\" for none)")
    with zipfile.ZipFile(path) as opened:
        report = prove(Archive(path.name, opened), names, refmap_name=refmap, environment=environment,
                       owners=list(owners), expect=expect, native=list(native))
    report["inputs"] = {ctx.host_path(p): sha256_file(p) for p in [path, *files, *reversed_files]}
    out = _report(ctx, report)
    total = sum(report["failure_counts"].values())
    _findings(ctx, [f"{f['check']}: {f['where']}: {f['detail']}" for f in report["failures"]], total)
    checked = report["checked"]
    for name, value in checked.items():
        ctx.measure(f"checked_{name}", value)
    if ctx.outcome == "ok":
        ctx.summary(f"remapped: {checked.get('refmap', 0)} refmap entries, {checked.get('members', 0)} renamed "
                    f"members, {checked.get('shadows', 0)} shadows and overwrites, {checked.get('references', 0)} "
                    f"references checked" + (f"; {len(report['unverified'])} entries no mapping names"
                                             if report["unverified"] else ""))
    return {"proved": report["proved"], "checked": checked, "failures": report["failures"][:50],
            "failure_counts": report["failure_counts"], "unverified": len(report["unverified"]),
            "refmaps": report["refmaps"], "report": ctx.host_path(out)}


# -- mixin.apply ------------------------------------------------------------------------------------
def apply(ctx: Context, tool: list, configs: list, classpath: list, production: bool, side: str, strict: bool,
          by_target: bool, removed: list, jdk: str, jvm_args: list, timeout: float, main=None, baseline=None):
    from . import apply as runner

    tools = _files(tool, "tool")
    if not tools:
        raise ParamError("tool: name the harness JAR")
    if not configs:
        raise ParamError("configs: name at least one Mixin configuration")
    entries = [Path(p) for p in classpath]
    for entry in entries:
        if not entry.exists():
            ctx.log.warning(f"class path entry {entry} does not exist; the harness skips it")
    if baseline is not None and not Path(baseline).is_file():
        raise ParamError(f"baseline: no such report: {baseline}")
    result = runner.apply(ctx, java=ctx.java(jdk), tool=tools, main=main, configs=list(configs), classpath=entries,
                          production=production, side=side, strict=strict,
                          baseline=Path(baseline) if baseline else None, by_target=by_target, removed=list(removed),
                          jvm_args=list(jvm_args), timeout=timeout)
    ctx.output("harness-report", ctx.out / "report.txt", "text/tab-separated-values")
    out = _report(ctx, dict(result, schema="wb.mixin.apply/1", configs=list(configs), production=production,
                            side=side, strict=strict))
    counts = result["counts"]
    for kind, value in counts.items():
        ctx.measure(kind.lower(), value)
    if result["exit"] == 1:
        _findings(ctx, result["failures"] or [f"the harness reported a failure (exit 1): {' | '.join(result['output'][-3:])}"])
    compared = result.get("compared")
    if compared and not compared["same"]:
        ctx.finding("the hooks differ from the baseline" + (f": {compared['differences'][0]}"
                                                            if compared["differences"] else ""))
    if ctx.outcome == "ok":
        ctx.summary(f"applied {len(configs)} configuration(s): {counts['TARGET']} targets, {counts['HOOK']} hooks"
                    + (", the same as the baseline" if compared else ""))
    return {"applied": result["applied"], "counts": counts, "failures": result["failures"][:50],
            "compared": compared, "report": ctx.host_path(out)}


# -- mixin.doctor -----------------------------------------------------------------------------------
def _line(problem: dict) -> str:
    return f"{problem['check']}: {problem['where']}: {problem['detail']}"


def doctor(ctx: Context, jars: list, classpath: list, nested: bool, loader: str, policy=None,
           compatibility_levels=None, mixin_version=None, max_class_version=None, required_fields=None,
           required_may_target_missing=None, injection_namespaces=None, remapped_packages=None, severity=None):
    from wb_jar.archive import jars as find_jars

    from . import doctor as checker
    from . import policy as policies

    roots = _files(jars, "jars")
    if not roots:
        raise ParamError("jars: name at least one JAR or folder")
    if not any(find_jars(root) for root in roots):
        raise ParamError(f"jars: no JAR files at {', '.join(jars)}")
    path_roots = _files(classpath, "classpath")
    configured = ctx.config.get("loaders") or {}
    if loader and loader not in configured:
        raise ParamError(f"loader: no {loader!r} under [modules.mixin.loaders] (known: "
                         f"{', '.join(sorted(configured)) or 'none'})")
    given = {"compatibility_levels": compatibility_levels, "mixin_version": mixin_version,
             "max_class_version": max_class_version, "required_fields": required_fields,
             "required_may_target_missing": required_may_target_missing,
             "injection_namespaces": injection_namespaces, "remapped_packages": remapped_packages, "severity": severity}
    layers = []
    try:
        if loader:
            where = f"[modules.mixin.loaders.{loader}]"
            layers.append((where, policies.check(configured[loader], where)))
        if policy:
            layers.append((Path(policy).name, policies.check(policies.read(Path(policy)), Path(policy).name)))
        given = {key: value for key, value in given.items() if value is not None}
        if given:
            layers.append(("params", policies.check(given, "params")))
    except OSError as error:
        raise ParamError(f"policy: cannot read {policy}: {error}") from None
    except ValueError as error:
        raise ParamError(str(error)) from None
    rules = policies.resolve(layers)
    ctx.progress("examine", msg=f"{len(roots)} JAR path(s) under {', '.join(rules.sources) or 'no policy'}")
    report = checker.examine(roots, path_roots, rules, nested=nested, host=ctx.host_path)
    if policy:
        report["policy"]["file"] = {"path": ctx.host_path(policy), "sha256": sha256_file(Path(policy))}
    out = _report(ctx, report)
    problems, counts = report["problems"], report["counts"]
    errors = [p for p in problems if p["severity"] == "error"]
    warnings = [p for p in problems if p["severity"] == "warning"]
    _findings(ctx, [_line(p) for p in errors])
    for problem in warnings[:LISTED]:
        ctx.log.warning(_line(problem))
    for entry in (report["classpath"] or {}).get("jars", []):
        if "error" in entry:
            ctx.log.warning(f"classpath: {entry['jar']}: {entry['error']}; targets only it holds are reported missing")
    for name in ("configs", "mixins", "errors", "warnings"):
        ctx.measure(name, counts[name])
    if not counts["configs"]:
        ctx.finding(f"no Mixin configuration in the {counts['archives']} JAR(s): nothing to check")
    if ctx.outcome == "ok":
        sources = rules.sources
        sources = ", ".join(sources[:-1]) + " and " + sources[-1] if len(sources) > 1 else "".join(sources)
        targets = (f"targets found among the class path's {report['classpath']['classes']} classes"
                   if report["classpath"] else "targets not checked (no classpath)")
        ctx.summary(f"{counts['configs']} Mixin configuration(s) in {counts['archives']} JAR(s) pass"
                    + (f" under {sources}" if sources else "") + f": {counts['mixins']} Mixins; {targets}"
                    + (f"; {len(warnings)} warning(s), first {_line(warnings[0])}" if warnings else ""))
    configs = [{key: row[key] for key in ("jar", "config", "required", "registered", "compatibility_level",
                                          "min_version", "errors", "warnings")}
               | {"mixins": sum(1 for m in row["mixins"] if m["mixin"]),
                  "checks": {k: v for k, v in row["checks"].items() if v != "ok"}}
               for row in report["configs"]]
    return {"passed": report["passed"] and bool(counts["configs"]), "counts": counts, "policy": report["policy"],
            "jars": [{key: row.get(key) for key in ("jar", "nested", "configs", "errors", "warnings")}
                     for row in report["jars"]],
            "configs": configs, "problems": problems[:50], "report": ctx.host_path(out)}
