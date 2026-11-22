"""world: the command handlers. What a stopped save holds (read), and how two worlds differ: save against
save (diff), digest against digest (digest-diff), and in their settled light (light-diff). And a world as its
generator makes it, through the generator's sampler (sampler.py): its windows against golden digests (golden), and
a layer of a window as a picture (preview)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from wb_core.module import Context, EnvironmentProblem, ParamError

from . import digest as digests
from . import golden as goldens
from . import sampler as samplers
from .diff import diff_worlds
from .light import light_diff as compare_light
from .save import Save, Selection, level_facts, level_time
from .survey import Survey
from .units import Namer, many, view


def read(ctx: Context, world: str, dimension: str, regions: list, block_entities: list, nbt_limit: int,
         digest: bool, dump: list, quiet: float, box: list | None = None) -> dict:
    selection = Selection.parse(box, regions)
    dumped = _cubes(dump)
    if dumped and not digest:
        raise ParamError("dump names cubes for the digest to keep whole: pass digest=true as well")
    save = _save(world, dimension)
    _stopped(save, quiet)
    _known(selection, save)
    survey = Survey(selection, set(block_entities), nbt_limit)
    builder = digests.DigestBuilder(selection, level_time(save.level), dumped) if digest else None
    namer = Namer(save.registry)
    for unit in save.units(selection, _progress(ctx)):
        ctx.check_cancel()
        seen = view(unit, namer, save.problems)
        survey.add(seen)
        if builder:
            builder.add(seen)
    tallies = survey.report()
    _write(ctx, "report", {"schema": "wb/world-read/v1", "world": ctx.host_path(world), "dimension": dimension,
                           **save.describe(), "selection": selection.describe(), **tallies,
                           "problems": save.problems})
    if builder:
        _write(ctx, "digest", builder.document())
    blocks = sum(survey.blocks.values())
    ctx.measure("units", sum(survey.units.values()))
    _settle(ctx, f"read {_units(survey.units)}: {many(len(survey.blocks), 'block state')} over "
                 f"{many(blocks, 'block')}, {many(sum(survey.block_entities.values()), 'block entity')}, "
                 f"{many(sum(survey.entities.values()), 'entity')}", [("the save", save)])
    return {"units": dict(survey.units), "blocks": blocks, "states": len(survey.blocks),
            "top": [[name, count] for name, count in survey.blocks.most_common(10)], "problems": save.problems[:20]}


def diff(ctx: Context, a: str, b: str, dimension: str, regions: list, ignore: list, quiet: float,
         box: list | None = None) -> dict:
    selection = Selection.parse(box, regions)
    saves = _pair(a, b, dimension, quiet, selection)
    found = diff_worlds(*saves, selection, tuple(ignore), ctx.check_cancel)
    if not (found["compared"] or found["onlyInA"] or found["onlyInB"]):
        raise ParamError("neither world keeps anything in the selection: there is nothing to compare")
    _write(ctx, "report", {"schema": "wb/world-diff/v1", "a": _side(ctx, a, saves[0]), "b": _side(ctx, b, saves[1]),
                           "dimension": dimension, "selection": selection.describe(), **found})
    lines = []
    if found["level"]:
        lines.append(f"the worlds differ in {', '.join(row['field'] for row in found['level'])}")
    if found["differing"]:
        fields = ", ".join(list(found["fields"])[:3])
        lines.append(f"{len(found['differing'])} of the {many(found['compared'], 'unit')} compared differ, most "
                     f"often in {fields}")
    lines += [f"{many(len(found[key]), 'unit')} saved only in {side}"
              for key, side in (("onlyInA", "a"), ("onlyInB", "b")) if found[key]]
    if found["undecodable"]:
        lines.append(f"{many(len(found['undecodable']), 'unit')} with sections that cannot be decoded, the first: "
                     f"{found['undecodable'][0]}")
    ctx.measure("differing_units", len(found["differing"]))
    _settle(ctx, f"the worlds agree in all {many(found['compared'], 'unit')} compared",
            [("a", saves[0]), ("b", saves[1])], lines)
    return {"compared": found["compared"], "identical": found["identical"], "differing": len(found["differing"]),
            "only_in_a": len(found["onlyInA"]), "only_in_b": len(found["onlyInB"]), "fields": found["fields"],
            "positions": found["differing"][:20]}


def digest_diff(ctx: Context, a: str, b: str, ignore_tick: bool) -> dict:
    loaded = digests.load(Path(a), "a"), digests.load(Path(b), "b")
    found = digests.compare(*loaded, ignore_tick)
    _write(ctx, "report", {"schema": "wb/world-digest-diff/v1", "a": _digest_side(ctx, a, loaded[0]),
                           "b": _digest_side(ctx, b, loaded[1]), "ignoreTick": ignore_tick, **found})
    counts = {key: 1 if key == "tick" else len(value) for key, value in found["differences"].items()}
    ctx.measure("differences", sum(counts.values()))
    first = loaded[0]
    agreed = (f"the digests agree: {many(len(first['cubes']), 'cube')}, {many(len(first['columns']), 'column')} and "
              f"{many(len(first['entities']), 'entity')} at tick {first['tick']}")
    _settle(ctx, agreed, [], digests.findings(found["differences"]))
    return {"agree": found["agree"], "differences": counts}


def light_diff(ctx: Context, a: str, b: str, dimension: str, regions: list, quiet: float,
               box: list | None = None) -> dict:
    selection = Selection.parse(box, regions)
    saves = _pair(a, b, dimension, quiet, selection)
    found = compare_light(*saves, selection, ctx.check_cancel)
    if not found["compared"]:
        raise ParamError(f"no {found['layout']} in the selection has every neighbour saved in both worlds: there "
                         "is nothing to compare")
    _write(ctx, "report", {"schema": "wb/world-light-diff/v1", "worlds": [ctx.host_path(a), ctx.host_path(b)],
                           "dimension": dimension, "selection": selection.describe(), **found,
                           "problems": {"a": saves[0].problems, "b": saves[1].problems}})
    lines = []
    if found["differing"]:
        lines.append(f"light differs in {found['differing']} of the {many(found['sections'], 'section')} interior "
                     f"to both worlds ({found['differingSameBlocks']} over the same blocks)")
    ctx.measure("differing_sections", found["differing"])
    _settle(ctx, f"light agrees in all {many(found['sections'], 'section')} of the "
                 f"{many(found['compared'], found['layout'])} interior to both worlds",
            [("a", saves[0]), ("b", saves[1])], lines)
    return {"compared": found["compared"], "sections": found["sections"], "differing": found["differing"],
            "same_blocks": found["differingSameBlocks"]}


def golden(ctx: Context, golden: str, sampler: list, cwd: str, threads: list, shuffle: bool, update: bool) -> dict:
    loaded = goldens.load(Path(golden))
    command = samplers.command(ctx, sampler)
    if not 1 <= len(threads) <= 2 or min(threads) < 0:
        raise ParamError("threads takes one or two counts of 0 or more, the first run's and the verifying run's "
                         "(0: the machine's cores)")
    counts = [count or samplers.cores() for count in threads]
    if update:
        (ctx.out / "golden.json").unlink(missing_ok=True)  # so an update refused leaves none from an earlier job
    first = samplers.run(ctx, command, Path(cwd), loaded.windows, number=1, threads=counts[0], order="forward",
                         label=f"{many(counts[0], 'thread')}, the file's order")
    second = None
    if len(counts) == 2:
        second = samplers.run(ctx, command, Path(cwd), goldens.verifying_order(loaded.windows, shuffle), number=2,
                              threads=counts[1], order="reverse",
                              label=f"{many(counts[1], 'thread')}, {'shuffled' if shuffle else 'reverse'} order")
    runs = [run for run in (first, second) if run]
    for run in runs:
        ctx.measure("sampler_seconds", round(run.seconds, 3), "s", run=run.number, threads=run.threads,
                    order=run.order)
        for name, seconds in run.timings.items():
            ctx.measure("window_seconds", seconds, "s", run=run.number, window=name)
    found = goldens.compare(loaded, first, second)
    lines = goldens.findings(found, runs, update)
    layers = sum(len(window.layers) for window in loaded.windows)
    agreeing = f"run 2 ({second.label}) agrees with run 1 ({first.label})" if second else \
        f"one run ({first.label}): determinism not checked"
    written, diff = None, None
    if update and found["nondeterministic"]:
        lines.append("golden.json was not written: a golden holds only digests both runs agree on")
    elif update:
        document, diff = goldens.updated(loaded, first)
        written = _write(ctx, "golden", document)
        ctx.summary(f"wrote golden.json, {many(layers, 'layer digest')}: {goldens.update_summary(diff)}; {agreeing}")
    elif not lines:
        ctx.summary(f"all {many(layers, 'layer')} of {many(len(loaded.windows), 'window')} match the golden; "
                    f"{agreeing}")
    for line in lines:
        ctx.finding(line)
    ctx.measure("changed_layers", len(found["changed"]))
    ctx.measure("nondeterministic_layers", len(found["nondeterministic"]))
    _write(ctx, "report", {"schema": "wb/world-golden-report/v1", "golden": ctx.host_path(golden),
                           "sampler": list(sampler), "cwd": ctx.host_path(cwd), "update": update,
                           "runs": [run.describe() for run in runs], "windows": found["windows"],
                           "changed": found["changed"], "new": found["new"],
                           "nondeterministic": found["nondeterministic"],
                           "updated": {"golden": ctx.host_path(written), **diff} if written else None})
    return {"windows": len(loaded.windows), "layers": layers,
            "verified": second is not None and not found["nondeterministic"], "changed": found["changed"],
            "new": found["new"], "nondeterministic": found["nondeterministic"],
            "runs": [{"threads": run.threads, "order": run.order, "seconds": round(run.seconds, 3)} for run in runs],
            "updated": ctx.host_path(written) if written else None}


def preview(ctx: Context, sampler: list, cwd: str, layer: str, window: str, options: dict, palette: dict,
            scale: int, threads: int, golden: str | None = None, columns: list | None = None,
            seed: int | None = None, step: int | None = None) -> dict:
    try:
        from . import paint
    except ImportError as error:  # NumPy or Pillow missing
        raise EnvironmentProblem(f"world.preview needs NumPy and Pillow: {error}") from None
    target, colours = _preview_window(golden, window, columns, seed, step, options, layer)
    try:
        stops, named = paint.colours({**colours, **palette})
    except ValueError as error:
        raise ParamError(str(error)) from None
    command = samplers.command(ctx, sampler)
    count = threads or samplers.cores()
    run = samplers.run(ctx, command, Path(cwd), [target], number=1, threads=count, order="forward",
                       label=many(count, "thread"))
    ctx.measure("sampler_seconds", round(run.seconds, 3), "s", run=1, threads=count, order="forward")
    sampled = run.digests[target.name][layer]
    raster = sampled["raster"]
    picture, facts = paint.paint(raster, stops, named, scale)
    path = ctx.out / f"{target.name}.{layer}.png"
    picture.save(path, format="PNG", optimize=True)
    ctx.output("png", path, "image/png")
    unlisted = facts["unlisted"]
    more = f" and {len(unlisted) - 3} more" if len(unlisted) > 3 else ""
    note = f"; not in the palette: {', '.join(map(str, unlisted[:3]))}{more}" if unlisted and (colours or palette) \
        else ""
    ctx.summary(f"{target.name} {layer}: {raster['width']} x {raster['height']} values, "
                f"{many(facts['values'], 'distinct value')}, painted to {path.name}{note}")
    return {"width": raster["width"], "height": raster["height"], "values": facts["values"],
            "unlisted": unlisted[:20], "png": ctx.host_path(path), "sha256": sampled["sha256"]}


def _preview_window(golden: str | None, name: str, columns: list | None, seed: int | None, step: int | None,
                    options: dict, layer: str) -> tuple[samplers.Window, dict]:
    """The window a preview samples, the golden file's window with what the params give in its place, and the
    golden file's palette for the layer."""
    spec: dict = {"name": name or "preview"}
    colours: dict = {}
    if golden:
        loaded = goldens.load(Path(golden))
        found = next((window for window in loaded.windows if window.name == name), None)
        if found is None:
            names = ", ".join(window.name for window in loaded.windows)
            raise ParamError(f"window: name one of the golden file's windows ({names})")
        spec = {key: value for key, value in found.entry().items() if key != "raster"}
        colours = loaded.palettes.get(layer, {})
    elif columns is None or seed is None:
        raise ParamError("give the window: columns and a seed, or a golden file and the name of a window in it")
    if columns is not None:
        spec.pop("box", None)
        spec["columns"] = columns
    spec.update({key: value for key, value in (("seed", seed), ("step", step)) if value is not None})
    spec.update(layers=[layer], options={**spec.get("options", {}), **options})
    try:
        target = samplers.parse_window(spec, {}, "the window")
    except ValueError as error:
        raise ParamError(str(error)) from None
    target.raster = (layer,)
    return target, colours


def _settle(ctx: Context, agreed: str, saves: list[tuple[str, Save]], lines: list[str] | None = None) -> None:
    """A finding per difference and per save read with problems; the summary when there is none."""
    lines = list(lines or [])
    lines += [f"{many(len(save.problems), 'problem')} reading {side}, the first: {save.problems[0]}"
              for side, save in saves if save.problems]
    for line in lines:
        ctx.finding(line)
    if not lines:
        ctx.summary(agreed)


def _pair(a: str, b: str, dimension: str, quiet: float, selection: Selection) -> tuple[Save, Save]:
    saves = _save(a, dimension, "a"), _save(b, dimension, "b")
    for side, save in zip("ab", saves):
        _stopped(save, quiet, side)
    _known(selection, *saves)
    return saves


def _save(path: str, dimension: str, side: str = "") -> Save:
    try:
        return Save(Path(path), dimension)
    except ParamError as error:
        raise ParamError(f"{side}: {error}" if side else str(error)) from None


def _stopped(save: Save, quiet: float, side: str = "") -> None:
    try:
        save.check_stopped(quiet)
    except EnvironmentProblem as error:
        raise EnvironmentProblem(f"{side}: {error}" if side else str(error)) from None


def _known(selection: Selection, *saves: Save) -> None:
    missing = set.intersection(*(save.unmatched(selection) for save in saves))
    if missing:
        raise ParamError(f"no region file is named {', '.join(sorted(missing))}")


def _cubes(values: list[int]) -> set[tuple[int, int, int]]:
    if len(values) % 3:
        raise ParamError("dump takes cube coordinates in threes: x,y,z[,x,y,z...]")
    return {tuple(values[i:i + 3]) for i in range(0, len(values), 3)}


def _side(ctx: Context, path: str, save: Save) -> dict:
    return {"world": ctx.host_path(path), "level": level_facts(save.level), "problems": save.problems}


def _digest_side(ctx: Context, path: str, loaded: dict) -> dict:
    return {"digest": ctx.host_path(path), "writer": loaded["writer"], "tick": loaded["tick"],
            "cubes": len(loaded["cubes"]), "columns": len(loaded["columns"]), "entities": len(loaded["entities"]),
            "dumps": len(loaded["dumps"])}


def _progress(ctx: Context) -> Callable[[int, int, str], None]:
    return lambda done, total, name: ctx.progress("reading", done / total if total else None, name)


def _units(counts: dict) -> str:
    return " and ".join(many(counts[kind], kind) for kind in ("chunk", "cube", "column") if counts.get(kind)) \
        or "nothing"


def _write(ctx: Context, name: str, document: dict) -> Path:
    path = ctx.out / f"{name}.json"
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    ctx.output(name, path, "application/json")
    return path
