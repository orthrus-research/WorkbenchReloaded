"""jar: static analysis of compiled mods, as typed commands: index, references and hook-targets.

Nothing is loaded or run: every class is read as bytes (``wb_jar.classes``). Each command writes its full
report to ``report.json`` in the job's output folder and returns the compact form as its data.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from wb_core.module import Context, ParamError

from . import diff as differ
from . import hooks as hook_check
from . import index as indexer
from . import references as finder
from .archive import jars as find_jars

LISTED = 20  # findings named one by one; the report holds every one
ROWS = 100   # rows per target and kind in a command's data; the report holds up to references.CAP
_SHA = re.compile(r"^(sha256:)?[0-9a-fA-F]{64}$")


def _root(value: str, name: str = "jars") -> Path:
    path = Path(value)
    if not path.exists():
        raise ParamError(f"{name}: no such file or folder: {value}")
    if not find_jars(path):
        raise ParamError(f"{name}: no JAR files at {value}")
    return path


def _report(ctx: Context, report: dict) -> Path:
    path = ctx.out / "report.json"
    path.write_text(json.dumps(report, indent=1, default=str) + "\n", encoding="utf-8")
    ctx.output("report", path, "application/json")
    return path


def _findings(ctx: Context, lines: list[str]) -> None:
    for line in lines[:LISTED]:
        ctx.finding(line)
    if len(lines) > LISTED:
        ctx.finding(f"and {len(lines) - LISTED} more (see report.json)")


def _settle(ctx: Context, text: str) -> None:
    """The summary when nothing was found; otherwise the findings speak and this goes to the log."""
    if ctx.outcome == "ok":
        ctx.summary(text)
    else:
        ctx.log.info(text)


# -- jar.index --------------------------------------------------------------------------------------
def index(ctx: Context, jars: str, subscribers: list, require_complete: bool, platform: list, systems=None):
    root = _root(jars)
    ctx.progress("index", msg=ctx.host_path(root))
    report = indexer.index(root, subscribers=list(subscribers), systems=systems, platform=list(platform))
    path = _report(ctx, report)
    records = report["jars"]
    patchers = {t: sorted({row["jar"] for row in rows}) for t, rows in report["targets"].items()}
    shared = {t: jars_ for t, jars_ in patchers.items() if len(jars_) > 1}
    mixins = sum(len(r["mixins"]) for r in records)
    for name, value in (("jars", len(records)), ("mixins", mixins), ("targets", len(patchers)),
                        ("shared_targets", len(shared)), ("collisions", len(report["collisions"]))):
        ctx.measure(name, value)
    problems = [f"{e['jar']}: {e['error']}" for e in report["errors"]]
    for collision in report["collisions"]:
        by = ", ".join(f"{b['mixin']} ({b['jar']})" for b in collision["by"])
        where = collision["member"] + (f" at {collision['at']}" if collision.get("at") else "")
        problems.append(f"{collision['kind']} collision on {collision['target']}.{where}: {by}")
    if require_complete:
        problems += [f"{m['jar']} requires {m['requires']}, which no indexed JAR declares" for m in report["missing"]]
    _findings(ctx, problems)
    _settle(ctx, f"{len(records)} JARs, {mixins} Mixins over {len(patchers)} target classes, "
                 f"{len(shared)} patched by more than one JAR")
    compact = [{"jar": r["jar"], "mods": r["mods"], "requires": r["requires"], "classes": r["classes"],
                "configs": [c["name"] for c in r["configs"]], "mixins": len(r["mixins"]),
                "targets": len({t for m in r["mixins"] for t in m["targets"]}), "systems": r["systems"],
                "events": sum(r["events"].values()), "threads": sorted(r["threads"]), "errors": len(r["errors"])}
               for r in records]
    return {"jars": compact, "targets": patchers, "shared": shared, "collisions": report["collisions"],
            "missing": report["missing"], "errors": report["errors"], "report": ctx.host_path(path)}


# -- jar.references ---------------------------------------------------------------------------------
def references(ctx: Context, jars: str, targets: list, kinds: list, fail_if: str, hierarchy=None):
    root = _root(jars)
    tree = _root(hierarchy, "hierarchy") if hierarchy else None
    try:
        parsed = [finder.parse_target(t) for t in targets]
    except ValueError as error:
        raise ParamError(str(error)) from None
    if not parsed:
        raise ParamError("name at least one target")
    if not kinds:
        raise ParamError("name at least one kind: subclass, override or call")
    ctx.progress("search", msg=f"{len(parsed)} target(s) in {ctx.host_path(root)}")
    report = finder.search(root, parsed, list(dict.fromkeys(kinds)), tree)
    path = _report(ctx, report)
    lines, problems = [], []
    for row in report["targets"]:
        total = sum(row["counts"].values())
        ctx.measure("references", total, target=row["target"])
        counted = ", ".join(f"{n} {kind}" for kind, n in row["counts"].items())
        lines.append(f"{row['target']}: {counted}")
        if fail_if == "found" and total:
            problems.append(f"{row['target']} is referenced: {counted}")
        if fail_if == "missing" and not total:
            problems.append(f"{row['target']}: no {' or '.join(kinds)} found")
    incomplete = [f"{e['jar']}: {e['error']}" for e in report["errors"]]
    if report["unreadable"]:
        incomplete.append(f"{len(report['unreadable'])} class(es) could not be read whole, so the search may "
                          f"miss references in them: {', '.join(report['unreadable'][:5])}")
    if fail_if == "found":
        problems += incomplete  # a gate that nothing refers to the targets cannot vouch for unread classes
    else:
        for line in incomplete:
            ctx.log.warning(line)
    _findings(ctx, problems)
    _settle(ctx, "; ".join(lines)[:500])
    data = [{key: (value[:ROWS] if isinstance(value, list) else value) for key, value in row.items()}
            for row in report["targets"]]
    return {"targets": data, "classes": report["classes"], "unreadable": len(report["unreadable"]),
            "report": ctx.host_path(path)}


# -- jar.hook-targets -------------------------------------------------------------------------------
def hook_targets(ctx: Context, jars: str, targets: list, resources: list, expect: dict):
    root = _root(jars)
    try:
        hooks = [hook_check.parse_hook(t) for t in targets]
    except ValueError as error:
        raise ParamError(str(error)) from None
    pins = {}
    for entry, digest in (expect or {}).items():
        if not isinstance(digest, str) or not _SHA.match(digest):
            raise ParamError(f"expect.{entry}: expected a SHA-256 hex digest")
        pins[str(entry)] = digest.lower().removeprefix("sha256:")
    if not hooks and not resources and not pins:
        raise ParamError("name targets, resources or expect")
    report = hook_check.check(root, hooks, list(resources), pins)
    path = _report(ctx, report)
    problems = [f"{e['jar']}: {e['error']}" for e in report["errors"]]
    for target in report["targets"]:
        if not target["found"]:
            similar = f" (it declares {', '.join(target['similar'][:5])})" if target["similar"] else ""
            problems.append(f"{target['target']} is not in {ctx.host_path(root)}{similar}")
    for resource in report["resources"]:
        if not resource["entries"]:
            problems.append(f"no entry matches {resource['pattern']}")
    for pin in report["pins"]:
        if not pin["found"]:
            problems.append(f"{pin['entry']} is not packaged")
        elif not pin["passed"]:
            got = ", ".join(sorted({f"{s['sha256'][:12]} in {s['jar']}" for s in pin["found"]}))
            problems.append(f"{pin['entry']} differs from {pin['expected'][:12]}: {got}")
    _findings(ctx, problems)
    found = sum(t["found"] for t in report["targets"])
    digested = sum(len(r["entries"]) for r in report["resources"])
    ctx.measure("targets_found", found)
    ctx.measure("resources", digested)
    _settle(ctx, f"{found} of {len(report['targets'])} targets found, {digested} resources digested"
                 + (f", {len(pins)} pinned entries match" if pins else ""))
    return {"passed": ctx.outcome == "ok",
            "targets": [{"target": t["target"], "found": t["found"], "where": sorted({w["jar"] for w in t["where"]})}
                        for t in report["targets"]],
            "resources": {r["pattern"]: len(r["entries"]) for r in report["resources"]},
            "pins": [{k: p[k] for k in ("entry", "expected", "passed")} for p in report["pins"]],
            "jars": report["jars"], "report": ctx.host_path(path)}


# -- jar.diff ---------------------------------------------------------------------------------------
def diff(ctx: Context, a: str, b: str):
    for name, value in (("a", a), ("b", b)):
        if not Path(value).is_file():
            raise ParamError(f"{name}: no such archive: {value}")
    try:
        report = differ.compare(Path(a), Path(b))
    except (OSError, ValueError) as error:  # zipfile.BadZipFile is a ValueError's sibling: an unreadable archive
        raise ParamError(f"not two readable archives: {error}") from None
    _report(ctx, report)
    if report["identical"]:
        ctx.summary(f"identical archives ({report['a']['entries']} entries, sha256 {report['a']['sha256'][:12]})")
        return _diff_data(report)
    lines = [f"only in {side}: {', '.join(names[:5])}" + (f" and {len(names) - 5} more" if len(names) > 5 else "")
             for side, names in (("a", report["onlyInA"]), ("b", report["onlyInB"])) if names]
    if report["orderDiffers"]:
        lines.append("the entries come in another order")
    for item in report["entries"]:
        lines.append(f"{item['name']}: {item['kind']}")
    _findings(ctx, lines or ["the archives differ outside their entries (comments or central directory)"])
    kinds = ", ".join(f"{count} {kind}" for kind, count in sorted(report["kinds"].items()))
    ctx.log.info(f"{len(report['entries'])} differing entries: {kinds or 'none'}")
    return _diff_data(report)


def _diff_data(report: dict) -> dict:
    return {"identical": report["identical"], "kinds": report["kinds"], "only_in_a": report["onlyInA"][:ROWS],
            "only_in_b": report["onlyInB"][:ROWS], "order_differs": report["orderDiffers"],
            "entries": [{"name": e["name"], "kind": e["kind"]} for e in report["entries"][:ROWS]]}
