"""content: the command handlers. A pack's GroovyScript folder read statically (scripts), a recipe dump read as
a graph (recipe-graph), and placement data read as point statistics (placement). Each writes report.json in
the job's output folder; a defect is a finding."""

from __future__ import annotations

import json
from pathlib import Path

from wb_core.module import Context, ParamError

LISTED = 20  # findings named one by one; the report holds every one


def _write(ctx: Context, name: str, document: dict) -> Path:
    path = ctx.out / f"{name}.json"
    path.write_text(json.dumps(document, indent=1, default=str) + "\n", encoding="utf-8")
    ctx.output(name, path, "application/json")
    return path


def _findings(ctx: Context, lines: list[str]) -> None:
    for line in lines[:LISTED]:
        ctx.finding(line)
    if len(lines) > LISTED:
        ctx.finding(f"and {len(lines) - LISTED} more (see report.json)")


def _many(count: int, noun: str) -> str:
    return f"1 {noun}" if count == 1 else f"{count} {noun}s"


# -- content.scripts ------------------------------------------------------------------------------------------
def _groovy_root(path: Path) -> Path:
    if not path.is_dir():
        raise ParamError(f"groovy: not a folder: {path}")
    for candidate in (path, path / "groovy"):
        if (candidate / "runConfig.json").is_file():
            return candidate
    if path.name.casefold() != "groovy" and (path / "groovy").is_dir():
        return path / "groovy"
    return path


def _rules(ctx: Context, ruleset: str, rules: str | None):
    from .rules import Rules, parse, read_file
    chosen = Rules()
    if ruleset:
        sets = ctx.config.get("rulesets") or {}
        if ruleset not in sets:
            raise ParamError(f"ruleset: no rule set {ruleset} under [modules.content.rulesets] "
                             f"(have: {', '.join(sorted(sets)) or 'none'})")
        try:
            chosen = parse(sets[ruleset], f"rulesets.{ruleset}")
        except ValueError as error:
            raise ParamError(str(error)) from None
    if rules:
        try:
            chosen = chosen.over(parse(read_file(Path(rules)), Path(rules).name))
        except (OSError, ValueError) as error:  # tomllib's and json's errors are ValueErrors
            raise ParamError(f"rules: {error}") from None
    return chosen


def scripts(ctx: Context, groovy: str, packmode: str, side: str, file_system: str, mod_ids: list, platform: list,
            scan_classes: str, ruleset: str, mods: str | None = None, log: str | None = None,
            rules: str | None = None) -> dict:
    from . import mods as modlist
    from .scripts import Analysis

    root = _groovy_root(Path(groovy))
    chosen = _rules(ctx, ruleset, rules)
    mod_list = None
    if mods or mod_ids:
        ctx.progress("mods")
        try:
            mod_list = modlist.read(Path(mods) if mods else None, list(mod_ids), scan_classes, ctx.check_cancel)
        except (OSError, ValueError) as error:
            raise ParamError(f"mods: {error}") from None
    if log and not Path(log).is_file():
        raise ParamError(f"log: not a file: {log}")
    ctx.progress("scripts")
    analysis = Analysis(root, packmode=packmode, side=side, file_system=file_system, mods=mod_list,
                        platform=list(platform), rules=chosen, cancel=ctx.check_cancel)
    try:
        found = analysis.run(Path(log) if log else None)
    except ValueError as error:
        raise ParamError(str(error)) from None
    report = {"schema": "wb/content-scripts/v1", "groovy": ctx.host_path(root), **found}
    out = _write(ctx, "report", report)
    _findings(ctx, analysis.findings)
    if found.get("runConfig") is None:
        return {"files": 0, "order": [], "cycles": [], "duplicates": [], "recipes": {}, "missing": [], "log": None,
                "report": ctx.host_path(out)}
    order = found["order"]
    running = [row for row in order if row["runs"]]
    recipes = found["recipes"]
    ctx.measure("scripts", len(analysis.inventory))
    ctx.measure("running", len(running))
    ctx.measure("recipe_additions", recipes["additions"])
    ctx.measure("recipe_removals", recipes["removals"])
    if ctx.outcome == "ok":
        where = analysis.label(analysis.selected)
        checked = "" if found["references"]["checked"] else "; references not checked (no mods or mod_ids)"
        ctx.summary(f"{_many(len(analysis.inventory), 'script')}: {len(running)} run in {where} on "
                    f"{file_system}; {_many(recipes['additions'], 'recipe addition')} and "
                    f"{_many(recipes['removals'], 'removal')} across {_many(len(recipes['byMod']), 'mod')}; no "
                    f"import cycle, duplicate id or missing reference{checked}")
    references = found["references"]
    return {"files": len(analysis.inventory), "order": [f"{r['loader']}:{r['file']}" for r in running][:200],
            "cycles": [c["cycle"] for c in found["imports"]["cycles"]][:50],
            "duplicates": [{"kind": d["kind"], "id": d["id"], "places": len(d["places"])}
                           for d in found["duplicates"]][:50],
            "recipes": {"additions": recipes["additions"], "removals": recipes["removals"],
                        "byMod": recipes["byMod"]},
            "missing": [m["mod"] for m in references["missing"]],
            "log": {"errors": len(found["log"]["errors"]), "warnings": len(found["log"]["warnings"])}
            if found["log"] else None,
            "report": ctx.host_path(out)}


# -- content.recipe-graph -------------------------------------------------------------------------------------
def recipe_graph(ctx: Context, recipes: str, obtainable: list, ignore: list, types: list) -> dict:
    from . import recipes as graph

    path = Path(recipes)
    if not path.is_file():
        raise ParamError(f"recipes: not a file: {recipes}")
    try:
        dump = graph.read(path, ignore=list(ignore), types=list(types))
    except (OSError, ValueError, EOFError) as error:
        raise ParamError(f"recipes: {error}") from None
    if not dump.recipes:
        raise ParamError(f"recipes: {path.name} holds no recipe" + (f" ({len(dump.unreadable)} unreadable records)"
                                                                     if dump.unreadable else ""))
    found = graph.analyse(dump, obtainable=list(obtainable), cancel=ctx.check_cancel)
    out = _write(ctx, "report", {"schema": "wb/content-recipe-graph/v1", "recipes": ctx.host_path(path),
                                 "obtainable": list(obtainable), "ignore": list(ignore), "types": list(types),
                                 **found.report})
    _findings(ctx, found.findings)
    report = found.report
    ctx.measure("recipes", report["counts"]["active"])
    ctx.measure("items", report["counts"]["items"])
    if ctx.outcome == "ok":
        ctx.summary(f"{_many(report['counts']['active'], 'recipe')} ({report['counts']['removed']} removed) over "
                    f"{_many(report['counts']['items'], 'item')}: {_many(len(report['deadEnds']), 'dead end')}, "
                    f"{_many(len(report['cycles']), 'cycle')}, nothing needed that only removed recipes made"
                    + ("" if obtainable else " (pass obtainable= to tell world resources from defects)"))
    return {"recipes": report["counts"]["active"], "items": report["counts"]["items"],
            "dead_ends": [d["id"] for d in report["deadEnds"]][:100],
            "cycles": [c["items"] for c in report["cycles"]][:50],
            "removed_only": [r["id"] for r in report["removedOnly"]][:100],
            "report": ctx.host_path(out)}


# -- content.placement ----------------------------------------------------------------------------------------
def placement(ctx: Context, points: str, x: str, z: str, kind: str, quadrat: float, edge: str, kinds: list,
              expect: dict, min_points: int, area: list | None = None) -> dict:
    from . import placement as stats

    path = Path(points)
    if not path.is_file():
        raise ParamError(f"points: not a file: {points}")
    if area is not None and len(area) != 4:
        raise ParamError("area takes four numbers: x1,z1,x2,z2")
    try:
        ranges = stats.ranges(expect)
        data = stats.read(path, x=x, z=z, kind=kind, kinds=list(kinds))
        found = stats.analyse(data, area=area, quadrat=quadrat, edge=edge, ranges=ranges, min_points=min_points)
    except (OSError, ValueError) as error:
        raise ParamError(str(error)) from None
    out = _write(ctx, "report", {"schema": "wb/content-placement/v1", "points": ctx.host_path(path), **found.report})
    _findings(ctx, found.findings)
    report = found.report
    ctx.measure("points", report["points"])
    if ctx.outcome == "ok":
        ctx.summary(found.summary)
    return {"points": report["points"],
            "kinds": {name: {k: row.get(k) for k in ("points", "per_hectare", "clark_evans", "dispersion", "pattern")}
                      for name, row in report["kinds"].items()},
            "report": ctx.host_path(out)}
