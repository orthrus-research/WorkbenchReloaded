"""assets: generated content JSON, reference blockstates for resource packs, and texture readability metrics."""

from __future__ import annotations

import json
import shutil
import tomllib
from pathlib import Path

from wb_core.module import Context, EnvironmentProblem, ParamError

from . import blockstates as blocks_mod
from . import content, metrics

LIMIT = 25  # findings of one kind shown one by one; the rest are counted in one more


def _table(path: str, what: str) -> dict:
    file = Path(path)
    try:
        text = file.read_text(encoding="utf-8-sig")
        data = json.loads(text) if file.suffix.lower() == ".json" else tomllib.loads(text)
    except (OSError, ValueError, tomllib.TOMLDecodeError) as error:
        raise ParamError(f"{what}: {file.name}: {error}") from None
    if not isinstance(data, dict):
        raise ParamError(f"{what}: {file.name} must hold a table")
    return data


def _write_json(ctx: Context, name: str, data) -> Path:
    path = ctx.out / name
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    ctx.output(path.stem, path, "application/json")
    return path


def _findings(ctx: Context, lines: list[str], label: str):
    for line in lines[:LIMIT]:
        ctx.finding(line)
    if len(lines) > LIMIT:
        ctx.finding(f"{len(lines) - LIMIT} more {label} (see the report)")


# ----------------------------------------------------------------------------------------------
# assets.generate
def generate(ctx: Context, manifest: str, check: bool, resources: str | None = None):
    try:
        loaded = content.load(_table(manifest, "manifest"))
        files = content.render(loaded)
    except content.ManifestError as error:
        raise ParamError(f"manifest: {error}") from None
    counts = {"files": len(files), "blocks": len(loaded.blocks), "items": len(loaded.items)}
    if check:
        if not resources:
            raise ParamError("check needs resources: the folder that holds the assets/ to compare with")
        root = Path(resources)
        if not root.is_dir():
            raise ParamError(f"resources: {resources} is not a folder")
        found = content.compare(loaded, files, root)
        _write_json(ctx, "check.json", {"namespace": loaded.namespace, **counts, **found})
        _findings(ctx, [f"out of date: {p}" for p in found["stale"]], "out-of-date files")
        _findings(ctx, [f"missing: {p}" for p in found["missing"]], "missing files")
        _findings(ctx, [f"not made by the manifest: {p}" for p in found["orphaned"]], "files the manifest does not make")
        if not ctx.findings:
            ctx.summary(f"the {len(files)} files of {loaded.namespace} match the manifest "
                        f"({len(loaded.blocks)} blocks, {len(loaded.items)} items)")
        return {"namespace": loaded.namespace, **counts, "passed": not ctx.findings, **found}
    if resources:
        raise ParamError("resources is read in a check; to write into a resources folder, give it as the job's --out")
    done = content.write(loaded, files, ctx.out)
    ctx.output("assets", ctx.out / loaded.base)
    ctx.measure("written", len(done["written"]))
    ctx.measure("removed", len(done["removed"]))
    ctx.summary(f"{loaded.namespace}: wrote {len(done['written'])} and removed {len(done['removed'])} of "
                f"{len(files)} files ({len(loaded.blocks)} blocks, {len(loaded.items)} items)")
    return {"namespace": loaded.namespace, **counts, "written": done["written"], "removed": done["removed"],
            "unchanged": len(done["unchanged"])}


# ----------------------------------------------------------------------------------------------
# assets.blockstates
def blockstates(ctx: Context, client_jar: str, blocks: list, namespace: str, description: str,
                catalog: str | None = None, mod: str | None = None):
    try:
        types: dict[str, dict] = blocks_mod.catalog_types(_table(catalog, "catalog")) if catalog else {}
        for text in blocks:
            spec = blocks_mod.reference(text)
            if spec["name"] in types:
                raise ParamError(f"blocks: {spec['name']} is named twice")
            types[spec["name"]] = {"about": {}, "roles": {}, "blocks": [spec]}
    except blocks_mod.BlockError as error:
        raise ParamError(str(error)) from None
    if not types and not mod:
        raise ParamError("name reference blocks (blocks=oak_stairs,...), a catalog, or a mod whose blocks to classify")
    try:
        resources = blocks_mod.Resources(Path(client_jar), Path(mod) if mod else None)
    except blocks_mod.BlockError as error:
        raise ParamError(str(error)) from None
    data: dict = {"namespace": namespace}
    if types:
        try:
            files, listing = blocks_mod.pack_files(types, resources, namespace, description)
            problems = blocks_mod.verify(files, resources, namespace, listing)
        except blocks_mod.BlockError as error:
            raise ParamError(str(error)) from None
        target = ctx.out / "pack"
        if target.exists():
            shutil.rmtree(target)  # the pack is this command's own: what an earlier run left does not linger
        for name, text in files.items():
            path = target / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="\n")
        ctx.output("pack", target)
        _findings(ctx, [f"pack check: {p}" for p in problems], "pack problems")
        listed = [b for t in listing["types"].values() for b in t["blocks"]]
        slots = sum(len(b["slots"]) for b in listed)
        data.update(blocks=len(listed), slots=slots, files=len(files), problems=problems,
                    made=[{"block": b["block"], "reference": b["reference"], "format": b["format"],
                           "slots": sorted(b["slots"])} for b in listed])
        ctx.measure("slots", slots)
    if mod:
        try:
            sorted_blocks = blocks_mod.classify(resources, types)
        except blocks_mod.BlockError as error:
            raise ParamError(str(error)) from None
        _write_json(ctx, "classify.json", sorted_blocks)
        _findings(ctx, [f"{name} uses {', '.join(record['missing'])}, which the mod does not have"
                        for name, record in sorted_blocks["blocks"].items() if record.get("missing")],
                  "blocks with missing textures")
        data.update(classified=len(sorted_blocks["blocks"]), counts=sorted_blocks["counts"],
                    unmatched=sorted_blocks["unmatched"], shapes=len(sorted_blocks["shapes"]))
    if not ctx.findings:
        parts = []
        if types:
            parts.append(f"{data['blocks']} reference block(s) copied into {namespace} with {data['slots']} texture "
                         f"slot(s); the pack is complete but for their textures")
        if mod:
            typed = data["classified"] - len(data["unmatched"])
            parts.append(f"{data['classified']} mod block(s) in {data['shapes']} shape(s), {typed} of a known type")
        ctx.summary("; ".join(parts))
    return data


# ----------------------------------------------------------------------------------------------
# assets.texture-metrics
def texture_metrics(ctx: Context, textures: str, kind: str, alternate_suffix: str, limits: str | None = None):
    try:
        faces = metrics.Faces()
    except ImportError as error:
        raise EnvironmentProblem(f"texture-metrics needs NumPy and Pillow: {error}") from None
    try:
        rules = metrics.check_limits(_table(limits, "limits")) if limits else {}
    except ValueError as error:
        raise ParamError(f"limits: {error}") from None
    kinds = rules.get("kinds", {})
    if kind and kinds and kind not in kinds:
        raise ParamError(f"kind {kind!r} has no limits (the limits name {', '.join(sorted(kinds))})")
    root = Path(textures)
    if root.is_file():
        paths, base = [root], root.parent
    elif root.is_dir():
        paths, base = sorted(root.rglob("*.png")), root
    else:
        raise ParamError(f"textures: {textures} is neither a PNG nor a folder")
    if not paths:
        raise ParamError(f"textures: {textures} holds no PNG")
    step_gap, accent = float(rules.get("step_gap", 0.135)), float(rules.get("accent_above_mean", 0.15))
    report, failures, unreadable = [], [], []
    for path in paths:
        relative = path.relative_to(base).as_posix()
        if alternate_suffix and path.stem.endswith(alternate_suffix):
            main = path.with_name(path.stem[: -len(alternate_suffix)] + path.suffix)
            if main.is_file():
                continue  # judged with its main face
        try:
            face = faces.load(path)
            measured = faces.measure(face, step_gap, accent)
            alternate = path.with_name(path.stem + alternate_suffix + path.suffix) if alternate_suffix else None
            if alternate is not None and alternate.is_file():
                measured.update(faces.pair(face, faces.load(alternate)))
                measured["alternate"] = alternate.relative_to(base).as_posix()
        except (OSError, ValueError) as error:
            unreadable.append(f"{relative}: {error}")
            continue
        face_kind = metrics.kind_of(relative, rules.get("assign", {}), kind)
        judged = metrics.judge(measured, kinds.get(face_kind, {})) if face_kind else {}
        failed = [f"{rule} {r['value']} (limit {r['limit']})" for rule, r in judged.items() if not r["passed"]]
        if failed:
            failures.append(f"{relative} ({face_kind}): " + "; ".join(failed))
        report.append({"texture": relative, "kind": face_kind or None, "metrics": measured, "rules": judged})
    _write_json(ctx, "metrics.json", {"limits": rules, "textures": report, "unreadable": unreadable})
    _findings(ctx, [f"cannot measure {line}" for line in unreadable], "unreadable textures")
    _findings(ctx, failures, "textures over their limits")
    judged_count = sum(1 for entry in report if entry["rules"])
    if not ctx.findings:
        ctx.summary(f"{len(report)} texture(s) measured" + (f", {judged_count} within their kind's limits"
                                                            if judged_count else "; no limits applied"))
    return {"measured": len(report), "judged": judged_count, "failed": len(failures), "unreadable": len(unreadable),
            "textures": [{"texture": e["texture"], "kind": e["kind"],
                          **{k: e["metrics"][k] for k in ("mean", "spread", "edge_density", "quadrant")}}
                         for e in report][:200]}
