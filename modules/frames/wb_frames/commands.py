"""frames: typed commands over Quadrature's frames.py (kept as qframes.py, unchanged)."""

from __future__ import annotations

import json
from pathlib import Path

from wb_core.module import Context, EnvironmentProblem, ParamError

PROFILES = Path(__file__).resolve().parent / "profiles"


def _q():
    try:
        from . import qframes
    except ImportError as error:  # numpy or Pillow missing
        raise EnvironmentProblem(f"frames needs NumPy and Pillow: {error}") from None
    return qframes


def _dir(value: str, name: str) -> Path:
    path = Path(value)
    if not path.is_dir():
        raise ParamError(f"{name} is not a folder: {value}")
    return path


def _profile(q, value: str):
    path = PROFILES / f"{value}.json" if (PROFILES / f"{value}.json").is_file() else Path(value)
    if not path.is_file():
        raise ParamError(f"no profile {value} (exact, near, repeat, or a JSON path)")
    return q.Profile.load(path), path.stem


def compare(ctx: Context, reference: str, candidate: str, profile: str):
    q = _q()
    loaded, name = _profile(q, profile)
    report = q.compare(_dir(reference, "reference"), _dir(candidate, "candidate"), loaded, ctx.out)
    ctx.output("report", ctx.out / "report.json", "application/json")
    failed = [f["frame"] for f in report["frames"] if not f.get("passed")]
    for frame in report["frames"]:
        if "changedFraction" in frame:
            ctx.measure("changed_fraction", frame["changedFraction"], frame=frame["frame"])
    if not report["frames"]:
        raise ParamError("no frames to compare: the folders share no PNG names")
    if failed:
        ctx.finding(f"{len(failed)} of {len(report['frames'])} frames over the {name} profile: {', '.join(failed[:5])}")
    else:
        ctx.summary(f"all {len(report['frames'])} frames within the {name} profile")
    return {"passed": report["passed"], "frames": len(report["frames"]), "failed": failed, "profile": name}


def measure(ctx: Context, runs: list, channel_threshold: int):
    if len(runs) < 2:
        raise ParamError("measure needs at least two runs")
    q = _q()
    report = q.measure_runs([_dir(r, "run") for r in runs], channel_threshold)
    path = ctx.out / "measure.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    ctx.output("measure", path, "application/json")
    worst = report["worst"]
    ctx.measure("worst_changed_fraction", worst["changedFraction"])
    ctx.measure("worst_mean_absolute", worst["meanAbsolute"])
    ctx.summary(f"{report['pairs']} pairs, {report['identicalPairs']} identical; worst {worst['changedFraction']:.4%} changed")
    return report


def check(ctx: Context, run: str, other: str, channel_threshold: int, fraction: float):
    q = _q()
    found = q.suspects(_dir(run, "run"), _dir(other, "other"), channel_threshold, fraction)
    if found:
        ctx.finding(f"{len(found)} frame(s) captured facing elsewhere: {', '.join(f['frame'] for f in found[:5])}")
    else:
        ctx.summary("every frame matches the other capture's view")
    return {"suspects": found}


def quality(ctx: Context, reference: str, candidate: str, channel_threshold: int):
    q = _q()
    report = q.quality(_dir(reference, "reference"), _dir(candidate, "candidate"), channel_threshold)
    if not report["frames"]:
        raise ParamError("no frames to score")
    path = ctx.out / "quality.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    ctx.output("quality", path, "application/json")
    mean = report["mean"]
    for key in ("psnr", "ssim", "edgeDetail"):
        if mean.get(key) is not None:
            ctx.measure(key, mean[key], unit="dB" if key == "psnr" else None)
    ctx.summary(f"PSNR {mean['psnr']:.2f} dB, SSIM {mean['ssim']:.4f} over {len(report['frames'])} frames"
                if mean.get("ssim") is not None else "scored")
    return {"mean": mean, "frames": len(report["frames"])}
