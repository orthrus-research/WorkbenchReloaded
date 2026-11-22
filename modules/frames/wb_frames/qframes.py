"""Compare captured frames against reference frames.

Two directories of PNG files are matched by file name. For each pair the tool measures how
many pixels changed and by how much, and a comparison profile turns those measurements into
pass or fail.

    python tools/frames.py compare REFERENCE CANDIDATE --profile tools/comparison/exact.json
    python tools/frames.py measure RUN_A RUN_B [RUN_C ...]
    python tools/frames.py quality REFERENCE CANDIDATE [--json OUT]

``compare`` exits 0 when every frame passes, 1 when any fails, 2 on a usage error.
``measure`` reports the differences between repeated captures of the same views, which is how
a profile's limits are chosen: a tolerance below the noise of two identical runs can never pass.
``quality`` scores pictures that are meant to differ, such as frames drawn below the output's size and
scaled up, against reference frames: beside the measures above, the peak signal-to-noise ratio, the
structural similarity of the two lumas and how much edge detail the candidate keeps.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
from PIL import Image


@dataclass(frozen=True)
class Profile:
    """Limits for one comparison.

    A pixel counts as changed when any channel differs by more than ``channelThreshold``
    (0 to 255). A frame passes when the changed fraction and the mean absolute difference are
    both within their limits.
    """

    channelThreshold: int
    maxChangedFraction: float
    maxMeanAbsolute: float

    @staticmethod
    def load(path: Path) -> "Profile":
        data = json.loads(path.read_text(encoding="utf-8"))
        return Profile(
            int(data["channelThreshold"]),
            float(data["maxChangedFraction"]),
            float(data["maxMeanAbsolute"]),
        )


@dataclass(frozen=True)
class Measurement:
    """How two frames of the same size differ."""

    changedFraction: float
    meanAbsolute: float
    maxAbsolute: int


def load_rgb(path: Path) -> np.ndarray:
    with Image.open(path) as image:
        return np.asarray(image.convert("RGB"), dtype=np.int16)


def measure_pair(reference: np.ndarray, candidate: np.ndarray, channel_threshold: int) -> Measurement:
    if reference.shape != candidate.shape:
        raise ValueError(f"sizes differ: {reference.shape[1]}x{reference.shape[0]} "
                         f"and {candidate.shape[1]}x{candidate.shape[0]}")
    difference = np.abs(reference - candidate)
    per_pixel = difference.max(axis=2)
    return Measurement(
        float((per_pixel > channel_threshold).mean()),
        float(difference.mean()),
        int(difference.max()),
    )


def passes(measurement: Measurement, profile: Profile) -> bool:
    return (measurement.changedFraction <= profile.maxChangedFraction
            and measurement.meanAbsolute <= profile.maxMeanAbsolute)


def frames(directory: Path) -> dict[str, Path]:
    return {path.name: path for path in sorted(directory.glob("*.png"))}


def write_difference(reference: np.ndarray, candidate: np.ndarray, channel_threshold: int, path: Path) -> None:
    """Saves the reference dimmed to gray, with changed pixels in red scaled by their difference."""
    per_pixel = np.abs(reference - candidate).max(axis=2)
    gray = (reference.mean(axis=2) * 0.35).astype(np.uint8)
    out = np.stack([gray, gray, gray], axis=2)
    changed = per_pixel > channel_threshold
    out[changed] = np.stack([
        np.clip(96 + per_pixel[changed] * 2, 0, 255).astype(np.uint8),
        np.zeros(int(changed.sum()), dtype=np.uint8),
        np.zeros(int(changed.sum()), dtype=np.uint8),
    ], axis=1)
    Image.fromarray(out, "RGB").save(path)


def compare(reference_dir: Path, candidate_dir: Path, profile: Profile, out_dir: Path | None) -> dict:
    reference, candidate = frames(reference_dir), frames(candidate_dir)
    results = []
    for name in sorted(set(reference) | set(candidate)):
        if name not in candidate:
            results.append({"frame": name, "passed": False, "reason": "missing from candidate"})
            continue
        if name not in reference:
            results.append({"frame": name, "passed": False, "reason": "not in reference"})
            continue
        a, b = load_rgb(reference[name]), load_rgb(candidate[name])
        try:
            measurement = measure_pair(a, b, profile.channelThreshold)
        except ValueError as error:
            results.append({"frame": name, "passed": False, "reason": str(error)})
            continue
        passed = passes(measurement, profile)
        results.append({"frame": name, "passed": passed, **asdict(measurement)})
        if out_dir is not None and not passed:
            out_dir.mkdir(parents=True, exist_ok=True)
            write_difference(a, b, profile.channelThreshold, out_dir / name)
    report = {
        "reference": str(reference_dir),
        "candidate": str(candidate_dir),
        "profile": asdict(profile),
        "passed": bool(results) and all(result["passed"] for result in results),
        "frames": results,
    }
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


# A view of terrain that has settled differs between two captures of one instance by a fraction of a
# percent. A view tens of percent apart was captured facing elsewhere, whatever was drawn.
SUSPECT_FRACTION = 0.2


def suspects(run_dir: Path, other_dir: Path, channel_threshold: int = 8,
             fraction: float = SUSPECT_FRACTION) -> list[dict]:
    """The frames of a run that are too far from another capture of the same instance to be the same view."""
    run, other = frames(run_dir), frames(other_dir)
    found = []
    for name in sorted(set(run) & set(other)):
        try:
            measurement = measure_pair(load_rgb(other[name]), load_rgb(run[name]), channel_threshold)
        except ValueError as error:
            found.append({"frame": name, "reason": str(error)})
            continue
        if measurement.changedFraction >= fraction:
            found.append({"frame": name, **asdict(measurement)})
    return found


def measure_runs(runs: list[Path], channel_threshold: int) -> dict:
    """Differences of every later run against the first, per frame, and the worst of each measure."""
    first = frames(runs[0])
    rows = []
    for run in runs[1:]:
        other = frames(run)
        for name in sorted(set(first) & set(other)):
            try:
                measurement = measure_pair(load_rgb(first[name]), load_rgb(other[name]), channel_threshold)
            except ValueError as error:
                rows.append({"frame": name, "run": run.name, "error": str(error)})
                continue
            rows.append({"frame": name, "run": run.name, **asdict(measurement)})
    measured = [row for row in rows if "error" not in row]
    worst = {
        "changedFraction": max((row["changedFraction"] for row in measured), default=0.0),
        "meanAbsolute": max((row["meanAbsolute"] for row in measured), default=0.0),
        "maxAbsolute": max((row["maxAbsolute"] for row in measured), default=0),
    }
    return {"baseline": str(runs[0]), "channelThreshold": channel_threshold, "pairs": len(measured),
            "identicalPairs": sum(1 for row in measured if row["maxAbsolute"] == 0),
            "worst": worst, "rows": rows}


# Structural similarity (Wang, Bovik, Sheikh and Simoncelli, 2004): local means, variances and
# covariance of the two lumas under a Gaussian window of standard deviation 1.5, cut at 5 pixels, and
# the published constants for 8-bit values.
SSIM_SIGMA = 1.5
SSIM_RADIUS = 5
SSIM_C1 = (0.01 * 255) ** 2
SSIM_C2 = (0.03 * 255) ** 2


def luma(rgb: np.ndarray) -> np.ndarray:
    """Rec. 601 luma of an RGB image, as floats on the 0 to 255 scale."""
    return rgb[..., 0] * 0.299 + rgb[..., 1] * 0.587 + rgb[..., 2] * 0.114


def gaussian_blur(image: np.ndarray, sigma: float = SSIM_SIGMA, radius: int = SSIM_RADIUS) -> np.ndarray:
    """A separable Gaussian blur over the valid region only: the result is smaller by the radius each side."""
    offsets = np.arange(-radius, radius + 1)
    kernel = np.exp(-(offsets ** 2) / (2 * sigma * sigma))
    kernel /= kernel.sum()
    height, width = image.shape
    rows = sum(weight * image[:, radius + offset:width - radius + offset] for offset, weight in zip(offsets, kernel))
    return sum(weight * rows[radius + offset:height - radius + offset, :] for offset, weight in zip(offsets, kernel))


def ssim(reference: np.ndarray, candidate: np.ndarray) -> float:
    """Mean structural similarity of two RGB images' lumas, 1 for identical images."""
    if reference.shape != candidate.shape:
        raise ValueError("sizes differ")
    a = luma(reference.astype(np.float64))
    b = luma(candidate.astype(np.float64))
    mean_a, mean_b = gaussian_blur(a), gaussian_blur(b)
    var_a = gaussian_blur(a * a) - mean_a * mean_a
    var_b = gaussian_blur(b * b) - mean_b * mean_b
    covariance = gaussian_blur(a * b) - mean_a * mean_b
    index = ((2 * mean_a * mean_b + SSIM_C1) * (2 * covariance + SSIM_C2)) / (
        (mean_a * mean_a + mean_b * mean_b + SSIM_C1) * (var_a + var_b + SSIM_C2))
    return float(index.mean())


def psnr(reference: np.ndarray, candidate: np.ndarray) -> float:
    """Peak signal-to-noise ratio over all channels, in decibels; infinite for identical images."""
    error = np.mean((reference.astype(np.float64) - candidate.astype(np.float64)) ** 2)
    return float("inf") if error == 0 else float(10 * np.log10(255 * 255 / error))


def edge_detail(reference: np.ndarray, candidate: np.ndarray) -> float:
    """The candidate's mean luma gradient against the reference's: under 1 is softer, over 1 sharper or noisier."""
    def gradient(image: np.ndarray) -> float:
        y = luma(image.astype(np.float64))
        return float(np.abs(np.diff(y, axis=1)).mean() + np.abs(np.diff(y, axis=0)).mean())
    base = gradient(reference)
    return float("nan") if base == 0 else gradient(candidate) / base


# Global shift by cross-correlation of the two lumas' gradients under a Hann window, refined below a pixel by
# evaluating the correlation around its peak on a finer grid with a matrix DFT (Guizar-Sicairos, Thurman and Fienup,
# 2008). Gradients weigh edges, where a shift shows, and a symmetric blur such as an upscale's moves no peak.
SHIFT_UPSAMPLE = 20


def gradients(image: np.ndarray, window: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """The luma's central differences across and down, under a window."""
    y = luma(image.astype(np.float64))
    across = np.zeros_like(y)
    down = np.zeros_like(y)
    across[:, 1:-1] = (y[:, 2:] - y[:, :-2]) / 2
    down[1:-1, :] = (y[2:, :] - y[:-2, :]) / 2
    return across * window, down * window


def shift(reference: np.ndarray, candidate: np.ndarray, upsample: int = SHIFT_UPSAMPLE) -> tuple[float, float]:
    """How far the candidate's picture lies from the reference's, in pixels, x right and y down: the candidate is the
    reference moved by the returned (dx, dy)."""
    if reference.shape != candidate.shape:
        raise ValueError("sizes differ")
    height, width = reference.shape[:2]
    window = np.outer(np.hanning(height), np.hanning(width))
    ax, ay = gradients(reference, window)
    bx, by = gradients(candidate, window)
    cross = np.fft.fft2(ax) * np.conj(np.fft.fft2(bx)) + np.fft.fft2(ay) * np.conj(np.fft.fft2(by))
    correlation = np.fft.ifft2(cross).real
    peak_y, peak_x = np.unravel_index(int(np.argmax(correlation)), correlation.shape)
    peak_y = peak_y - height if peak_y > height // 2 else peak_y
    peak_x = peak_x - width if peak_x > width // 2 else peak_x
    # The correlation on a grid 1/upsample apart, a pixel and a half each way of the whole-pixel peak.
    steps = np.arange(-1.5, 1.5 + 1e-9, 1.0 / upsample)
    ys, xs = peak_y + steps, peak_x + steps
    ky = np.fft.fftfreq(height) * height
    kx = np.fft.fftfreq(width) * width
    rows = np.exp(2j * np.pi * np.outer(ys, ky) / height)
    columns = np.exp(2j * np.pi * np.outer(kx, xs) / width)
    fine = (rows @ cross @ columns).real
    fy, fx = np.unravel_index(int(np.argmax(fine)), fine.shape)
    # The correlation peaks where the reference must move back onto the candidate: the shift is its negative.
    return float(-xs[fx]), float(-ys[fy])


def quality(reference_dir: Path, candidate_dir: Path, channel_threshold: int = 8) -> dict:
    """Every frame the two directories share, scored by each measure, and the mean of each over them."""
    reference, candidate = frames(reference_dir), frames(candidate_dir)
    rows = []
    for name in sorted(set(reference) & set(candidate)):
        a, b = load_rgb(reference[name]), load_rgb(candidate[name])
        try:
            measurement = measure_pair(a, b, channel_threshold)
        except ValueError as error:
            rows.append({"frame": name, "error": str(error)})
            continue
        rows.append({"frame": name, **asdict(measurement), "psnr": psnr(a, b), "ssim": ssim(a, b),
                     "edgeDetail": edge_detail(a, b)})
    scored = [row for row in rows if "error" not in row]
    mean = {}
    for key in ["changedFraction", "meanAbsolute", "psnr", "ssim", "edgeDetail"]:
        values = [row[key] for row in scored if np.isfinite(row[key])]
        mean[key] = float(np.mean(values)) if values else None
    return {"reference": str(reference_dir), "candidate": str(candidate_dir), "channelThreshold": channel_threshold,
            "frames": rows, "mean": mean, "missing": sorted(set(reference) ^ set(candidate))}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)

    c = commands.add_parser("compare", help="pass or fail a candidate against a reference")
    c.add_argument("reference", type=Path)
    c.add_argument("candidate", type=Path)
    c.add_argument("--profile", type=Path, required=True, help="comparison profile JSON")
    c.add_argument("--out", type=Path, help="directory for report.json and difference images of failed frames")

    m = commands.add_parser("measure", help="report differences between repeated captures")
    m.add_argument("runs", type=Path, nargs="+")
    m.add_argument("--channel-threshold", type=int, default=8)
    m.add_argument("--json", action="store_true", help="print the full report as JSON")

    k = commands.add_parser("check", help="name the frames of a run that another capture of the same "
                                          "instance shows were captured facing elsewhere")
    k.add_argument("run", type=Path)
    k.add_argument("other", type=Path, help="another capture of the same instance, such as the settling run")
    k.add_argument("--channel-threshold", type=int, default=8)
    k.add_argument("--fraction", type=float, default=SUSPECT_FRACTION,
                   help="changed fraction from which a frame is not the same view")

    q = commands.add_parser("quality", help="score frames meant to differ from a reference: PSNR, SSIM, edge detail")
    q.add_argument("reference", type=Path)
    q.add_argument("candidate", type=Path)
    q.add_argument("--channel-threshold", type=int, default=8)
    q.add_argument("--json", type=Path, help="write the full report here")

    args = parser.parse_args(argv)
    if args.command == "quality":
        for directory in (args.reference, args.candidate):
            if not directory.is_dir():
                print(f"Not a directory: {directory}", file=sys.stderr)
                return 2
        report = quality(args.reference, args.candidate, args.channel_threshold)
        for row in report["frames"]:
            if "error" in row:
                print(f"ERROR  {row['frame']}: {row['error']}")
            else:
                print(f"{row['frame']}: PSNR {row['psnr']:.2f} dB, SSIM {row['ssim']:.4f}, edge detail "
                      f"{row['edgeDetail']:.3f}, {row['changedFraction']:.2%} changed, mean {row['meanAbsolute']:.3f}")
        mean = report["mean"]
        if mean["ssim"] is not None:
            print(f"MEAN  PSNR {mean['psnr']:.2f} dB, SSIM {mean['ssim']:.4f}, edge detail {mean['edgeDetail']:.3f}, "
                  f"{mean['changedFraction']:.2%} changed, mean {mean['meanAbsolute']:.3f} over {len(report['frames'])} frames")
        if args.json:
            args.json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        return 0 if report["frames"] else 2
    if args.command == "check":
        for directory in (args.run, args.other):
            if not directory.is_dir():
                print(f"Not a directory: {directory}", file=sys.stderr)
                return 2
        found = suspects(args.run, args.other, args.channel_threshold, args.fraction)
        for frame in found:
            if "reason" in frame:
                print(f"SUSPECT  {frame['frame']}: {frame['reason']}")
            else:
                print(f"SUSPECT  {frame['frame']}: {frame['changedFraction']:.4%} changed against the other capture")
        print("SUSPECT" if found else "CLEAR")
        return 1 if found else 0
    if args.command == "compare":
        for directory in (args.reference, args.candidate):
            if not directory.is_dir():
                print(f"Not a directory: {directory}", file=sys.stderr)
                return 2
        report = compare(args.reference, args.candidate, Profile.load(args.profile), args.out)
        for frame in report["frames"]:
            if "reason" in frame:
                print(f"FAIL  {frame['frame']}: {frame['reason']}")
            else:
                apart = "" if frame["changedFraction"] < SUSPECT_FRACTION else \
                    "; not the same view: check both captures against a third"
                print(f"{'pass' if frame['passed'] else 'FAIL'}  {frame['frame']}: "
                      f"{frame['changedFraction']:.4%} changed, mean {frame['meanAbsolute']:.3f}, "
                      f"max {frame['maxAbsolute']}{apart}")
        if not report["frames"]:
            print("No frames to compare.")
        print("PASSED" if report["passed"] else "FAILED")
        return 0 if report["passed"] else 1

    if len(args.runs) < 2:
        print("measure needs at least two runs", file=sys.stderr)
        return 2
    report = measure_runs(args.runs, args.channel_threshold)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        worst = report["worst"]
        print(f"{report['pairs']} frame pairs against {report['baseline']}; "
              f"{report['identicalPairs']} identical")
        print(f"worst changed fraction {worst['changedFraction']:.4%}, "
              f"worst mean {worst['meanAbsolute']:.3f}, worst max {worst['maxAbsolute']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
