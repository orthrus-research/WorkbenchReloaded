"""drive: the command handlers. A tour checked before a session plays it (tour-lint), a camera path made from
keyframes (path) and checked against the frames captured along it (path-check), and blocks laid out on a platform
for review, as a tour (gallery)."""

from __future__ import annotations

import json
import math
from pathlib import Path

from wb_core.module import Context, ParamError

from . import frames as frame_checks
from . import gallery as galleries
from . import lint
from . import path as paths
from . import rules as rule_sets
from . import tour as tours

LIMIT = 25  # findings shown one by one; the rest are counted in one more


def _many(count: int, noun: str) -> str:
    return f"{count:,} {noun}" if count == 1 else f"{count:,} {noun}s"


def _write(ctx: Context, name: str, document) -> Path:
    path = ctx.out / f"{name}.json"
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    ctx.output(name, path, "application/json")
    return path


def _findings(ctx: Context, findings: list[tours.Finding]) -> None:
    for finding in findings[:LIMIT]:
        ctx.finding(finding.text())
    if len(findings) > LIMIT:
        ctx.finding(f"{len(findings) - LIMIT} more findings (see report.json)")


def _rules(ctx: Context, name: str, overrides: dict | None = None) -> rule_sets.Rules:
    try:
        return rule_sets.resolve(ctx.config.get("rules") or {}, name, overrides)
    except rule_sets.RuleError as error:
        raise ParamError(f"rules: {error}") from None


def _load(path: str, what: str):
    try:
        return tours.load(Path(path))
    except (OSError, ValueError) as error:
        raise ParamError(f"{what}: {error}") from None


def _tour_summary(linted: lint.Linted) -> dict:
    return {"rules": linted.rules.name, "findings": [f.as_dict() for f in linted.findings],
            "checked": {rule: state for rule, state in linted.checked.items()}, "seconds": linted.seconds}


# -- drive.tour-lint -------------------------------------------------------------------------------------------------
def tour_lint(ctx: Context, tour: str, rules: str, view_distance: int, band: list | None = None,
              border: float | None = None, allow: list | None = None, settle: list | None = None,
              max_seconds: float | None = None) -> dict:
    chosen = _rules(ctx, rules, {"band": band, "border": border, "allow": allow, "settle": settle,
                                 "max_seconds": max_seconds})
    linted = lint.lint(_load(tour, "tour"), chosen, view_distance)
    if linted.tour.shape == "none":
        raise ParamError(f"tour: {linted.findings[0].message}")
    views, findings = len(linted.tour.views), linted.findings
    _write(ctx, "report", {
        "schema": "wb/drive-tour-lint/v1", "tour": ctx.host_path(tour), "shape": linted.tour.shape,
        "name": linted.tour.name, "views": views, "captures": linted.captures, "viewDistance": linted.view_distance,
        "seconds": linted.seconds, "rules": chosen.describe(),
        "checked": {rule: {"rule": lint.RULES[rule], "state": state} for rule, state in linted.checked.items()},
        "findings": [f.as_dict() for f in findings], "rows": linted.rows})
    ctx.measure("views", views)
    ctx.measure("findings", len(findings))
    ctx.measure("tour_seconds", linted.seconds, unit="s")
    _findings(ctx, findings)
    if not findings:
        skipped = [rule for rule, state in linted.checked.items() if state != "checked"]
        ctx.summary(f"{_many(views, 'view')} ({linted.captures} captured) keep every rule of the {chosen.name} rules, "
                    f"about {linted.seconds:,.0f} s" + (f"; not checked: {', '.join(skipped)}" if skipped else ""))
    return {"views": views, "captures": linted.captures, "findings": len(findings), "rules": linted.broken(),
            "seconds": linted.seconds}


def _settles(chosen: rule_sets.Rules, settle: float) -> tuple[int | None, int | None]:
    """The settle in ticks of a view, and of a view that arrives: the command's, or what the rule set's settle table
    asks at its view distance if that is more; None leaves it to the runner."""
    view = arrive = settle
    if chosen.settle and chosen.view_distance:
        least, _ = chosen.settle_row(chosen.view_distance)
        view, arrive = max(settle, least.settle), max(settle, least.arrive)
    return tuple(round(s * chosen.tick_rate) if s else None for s in (view, arrive))


# -- drive.path ------------------------------------------------------------------------------------------------------
def _far(a, b, least: tuple[float, float]) -> bool:
    return abs(a[0] - b[0]) + abs(a[2] - b[2]) > least[0] or abs(a[1] - b[1]) > least[1]


def path_tour(route: paths.Route, every: int, name: str, chosen: rule_sets.Rules, settle: float) -> list[dict]:
    """A view every so many frames and at the last: none that repeats the pose before it (a hold), and, when the rule
    set has a least move, none closer than that to the view before (the last stays, in place of views too close to
    it)."""
    last = len(route.poses) - 1
    width = max(4, len(str(last)))
    frames = list(range(0, last + 1, every))
    if frames[-1] != last:
        frames.append(last)
    least, picked = chosen.min_move, []
    for frame in frames:
        pose = route.poses[frame]
        if picked:
            before = route.poses[picked[-1]]
            if all(abs(a - b) <= 1e-6 for a, b in zip(pose[:5], before[:5])):
                continue
            if least and not _far(pose, before, least):
                if frame != last:
                    continue
                while len(picked) > 1 and not _far(pose, route.poses[picked[-1]], least):
                    picked.pop()
                if not _far(pose, route.poses[picked[-1]], least):
                    continue
        picked.append(frame)
    view_ticks, arrive_ticks = _settles(chosen, settle)
    return [tours.view(f"{name}-{frame:0{width}d}", route.poses[frame][:3], route.poses[frame][3],
                       route.poses[frame][4], settle_ticks=arrive_ticks if n == 0 else view_ticks)
            for n, frame in enumerate(picked)]


def path(ctx: Context, keyframes: str, fps: float, interpolation: str, pacing: str, speed: float, turn_speed: float,
         ramp: float, output: str, every: int, settle: float, name: str, rules: str, max_speed: float | None = None,
         max_turn: float | None = None, max_accel: float | None = None, max_seconds: float | None = None) -> dict:
    chosen = _rules(ctx, rules)
    try:
        keys = paths.keyframes(_load(keyframes, "keyframes"))
    except paths.PathError as error:
        raise ParamError(f"keyframes: {error}") from None
    route = paths.build(keys, fps, interpolation, pacing, speed, turn_speed, ramp)
    limits = chosen.path.describe()
    for key, value in (("max_speed", max_speed), ("max_turn", max_turn), ("max_accel", max_accel),
                       ("max_seconds", max_seconds)):
        if value is not None:
            limits[key] = value
    findings, peaks = paths.check(route, limits)
    report = {"schema": "wb/drive-path/v1", "keyframes": ctx.host_path(keyframes), "fps": fps,
              "interpolation": interpolation, "pacing": pacing, "speed": speed, "turnSpeed": turn_speed,
              "ramp": ramp, "frames": len(route.poses), "seconds": round(route.seconds, 4),
              "length": round(route.length, 3), "limits": limits, "peaks": peaks,
              "segments": [span.describe(fps) for span in route.spans]}
    if output in ("route", "both"):
        _write(ctx, "route", route.document())
    if output in ("tour", "both"):
        document = tours.document(path_tour(route, every, name, chosen, settle))
        _write(ctx, "tour", document)
        linted = lint.lint(document, chosen)
        findings += linted.findings
        report["tour"] = {"views": len(document["views"]), **_tour_summary(linted)}
    report["findings"] = [f.as_dict() for f in findings]
    _write(ctx, "report", report)
    ctx.measure("frames", len(route.poses))
    ctx.measure("path_seconds", round(route.seconds, 4), unit="s")
    ctx.measure("peak_speed", peaks["speed"], unit="blocks/s")
    ctx.measure("peak_turn", peaks["turn"], unit="deg/s")
    _findings(ctx, findings)
    if not findings:
        checked = [key.removeprefix("max_") for key, value in limits.items() if value]
        ctx.summary(f"{_many(len(route.poses), 'frame')} over {route.seconds:,.2f} s at {fps:g} a second, "
                    f"{route.length:,.1f} blocks in {_many(len(route.spans), 'segment')}; peaks "
                    f"{peaks['speed']:.2f} blocks/s and {peaks['turn']:.1f} degrees/s"
                    + (f", within the {', '.join(checked)} limits" if checked else ", no limits set"))
    return {"frames": len(route.poses), "seconds": round(route.seconds, 4), "length": round(route.length, 3),
            "segments": len(route.spans), "max_speed": peaks["speed"], "max_turn": peaks["turn"],
            "max_accel": peaks["accel"]}


# -- drive.path-check ------------------------------------------------------------------------------------------------
def path_check(ctx: Context, frames: str, naming: str, pattern: str, offset: int, fps: float, spike: float,
               floor: float, tolerance: float, continuity: bool, scale: int, route: str | None = None) -> dict:
    folder = Path(frames)
    if not folder.is_dir():
        raise ParamError(f"frames: {frames} is not a folder")
    read = None
    if route:
        try:
            read = paths.read(_load(route, "route"))
        except paths.PathError as error:
            raise ParamError(f"route: {error}") from None
    def step(phase: str, done: int, total: int) -> None:
        ctx.check_cancel()
        if done == 1 or done % 50 == 0 or done == total:
            ctx.progress(phase, done / total, f"{done} of {total} frames")

    try:
        checked = frame_checks.check(folder, read, naming, pattern, offset, fps, spike, floor, tolerance, continuity,
                                     scale, step)
    except ValueError as error:
        raise ParamError(str(error)) from None
    placed = sum(1 for f in checked.frames if f.frame is not None)
    _write(ctx, "report", {
        "schema": "wb/drive-path-check/v1", "frames": ctx.host_path(frames),
        "route": ctx.host_path(route) if route else None, "naming": pattern or naming, "mapping": checked.mapping,
        "captured": len(checked.frames), "placed": placed, "expected": checked.expected,
        "missing": checked.missing, "repeats": checked.duplicates, "spikes": checked.spikes,
        "continuity": checked.continuity, "pace": checked.pace, "segments": checked.segments,
        "findings": [f.as_dict() for f in checked.findings],
        "rows": [frame.describe(read) for frame in checked.frames]})
    ctx.measure("frames", len(checked.frames))
    ctx.measure("missing", len(checked.missing))
    ctx.measure("repeats", checked.duplicates)
    if checked.continuity == "checked":
        ctx.measure("spikes", checked.spikes)
    _findings(ctx, checked.findings)
    if not checked.findings:
        against = f"the path's {checked.expected}" if read else "each other"
        motion = "no jumps between frames" if checked.continuity == "checked" else \
            f"continuity not checked ({checked.continuity})"
        ctx.summary(f"{_many(len(checked.frames), 'frame')} agree with {against}: none missing, none repeated while "
                    f"the camera moves, {motion}")
    return {"frames": len(checked.frames), "expected": checked.expected, "missing": len(checked.missing),
            "repeats": checked.duplicates, "spikes": checked.spikes, "segments": len(checked.segments),
            "continuity": checked.continuity == "checked"}


# -- drive.gallery ---------------------------------------------------------------------------------------------------
def gallery(ctx: Context, blocks: list, origin: list, facing: str, columns: int, spacing: int, row_spacing: int,
            distance: float, pitch: float, fov: float, window: str, min_gap_px: int, min_block_px: int,
            platform: str, clear: int, syntax: str, setup: list, settle: float, overview: bool, name: str,
            rules: str, catalog: str | None = None) -> dict:
    chosen = _rules(ctx, rules)
    if len(origin) != 3:
        raise ParamError("origin: x,y,z, three whole numbers")
    width, height = (int(v) for v in window.lower().split("x"))
    if not width or not height:
        raise ParamError(f"window: {window} has no area")
    if not row_spacing:
        row_spacing = math.ceil(distance) + 2
        if chosen.min_move:  # far enough apart for the runner to take every row's capture
            row_spacing = max(row_spacing, math.floor(chosen.min_move[0]) + 1)
    try:
        listed = [galleries.parse_block(text) for text in blocks]
        if catalog:
            listed += galleries.catalog(_load(catalog, "catalog"))
        listed, findings = galleries.unique(listed)
        plan = galleries.layout(listed, tuple(origin), facing, columns, spacing, row_spacing, distance, pitch, fov,
                                width / height, name)
        built = galleries.commands(plan, galleries.parse_block(platform) if platform else None, clear, syntax,
                                   chosen.fill_limit)
    except galleries.GalleryError as error:
        raise ParamError(str(error)) from None
    findings += galleries.spacing_check(plan, fov, width, height, min_gap_px, min_block_px)
    view_ticks, arrive_ticks = _settles(chosen, settle)
    above = plan.overview
    views = [tours.view(f"{name}-arrive", above.pos, above.yaw, above.pitch, commands=setup,
                        settle_ticks=arrive_ticks, capture=False)]
    for k, camera in enumerate(plan.cameras):
        views.append(tours.view(camera.label, camera.pos, camera.yaw, camera.pitch,
                                commands=built if k == 0 else (), settle_ticks=view_ticks,
                                extra={"blocks": [p.block.label for p in plan.rows[k]]}))
    if overview:
        views.append(tours.view(above.label, above.pos, above.yaw, above.pitch, settle_ticks=view_ticks))
    document = tours.document(views)
    _write(ctx, "tour", document)
    linted = lint.lint(document, chosen, chosen.view_distance)
    findings += linted.findings
    x1, z1, x2, z2 = plan.footprint
    _write(ctx, "report", {
        "schema": "wb/drive-gallery/v1", "origin": list(plan.origin), "facing": facing, "columns": columns,
        "spacing": spacing, "rowSpacing": row_spacing, "distance": distance, "pitch": pitch, "fov": fov,
        "window": [width, height], "syntax": syntax,
        "platform": {"block": platform or None, "from": [x1, plan.origin[1] - 1, z1],
                     "to": [x2, plan.origin[1] - 1, z2], "clear": clear},
        "legend": [{"row": p.row, "column": p.column, "label": p.block.label, "block": p.block.spec(syntax),
                    "x": p.pos[0], "y": p.pos[1], "z": p.pos[2]} for p in plan.placed],
        "rows": plan.stats, "commands": len(built), "views": len(views), "tour": _tour_summary(linted),
        "findings": [f.as_dict() for f in findings]})
    ctx.measure("blocks", len(plan.placed))
    ctx.measure("rows", len(plan.rows))
    ctx.measure("commands", len(built))
    _findings(ctx, findings)
    if not findings:
        ctx.summary(f"{_many(len(plan.placed), 'block')} in {_many(len(plan.rows), 'row')}: {_many(len(views), 'view')}"
                    f" and {_many(len(built), 'command')}; every row reads in its view, and the tour keeps the "
                    f"{chosen.name} rules")
    return {"blocks": len(plan.placed), "rows": len(plan.rows), "views": len(views), "commands": len(built),
            "findings": len(findings)}
