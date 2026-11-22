"""Compare captured frames against reference frames.

Two directories of PNG files are matched by name (by relative path with ``--recursive``). For
each pair the tool measures how many pixels changed and by how much, and a comparison profile
turns those measurements into pass or fail.

    orthrus-frames compare REFERENCE CANDIDATE --profile near
    orthrus-frames compare REFERENCE CANDIDATE --profile my-profile.json --out diff/
    orthrus-frames measure RUN_A RUN_B [RUN_C ...]

``--profile`` takes a bundled profile name (``exact``, ``near``, ``repeat``) or a JSON file.
``compare`` exits 0 when every frame passes, 1 when any fails, 2 on a usage error and 3 when
numpy or Pillow is missing.
``measure`` reports the differences between repeated captures of the same views, which is how
a profile's limits are chosen: a tolerance below the noise of two identical runs can never pass.

Requires the ``frames`` extra: ``pip install "orthrus-tools[frames]"``.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from . import cli
from .cli import ENVIRONMENT, FINDING, OK, USAGE, Output, ToolError

try:
    import numpy as np
    from PIL import Image
except ImportError:  # pragma: no cover - exercised only without the extra installed
    np = None  # type: ignore[assignment]
    Image = None  # type: ignore[assignment]

BUNDLED_PROFILES = ("exact", "near", "repeat")


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
    def from_dict(data: dict[str, Any]) -> Profile:
        profile = Profile(
            int(data["channelThreshold"]),
            float(data["maxChangedFraction"]),
            float(data["maxMeanAbsolute"]),
        )
        if not 0 <= profile.channelThreshold <= 255:
            raise ValueError("channelThreshold must be between 0 and 255")
        if not 0 <= profile.maxChangedFraction <= 1 or profile.maxMeanAbsolute < 0:
            raise ValueError("profile limits must be a fraction in [0, 1] and a non-negative mean")
        return profile

    @staticmethod
    def load(name_or_path: str | Path) -> Profile:
        """A bundled profile by name, or a profile JSON file by path."""
        if str(name_or_path) in BUNDLED_PROFILES:
            text = (
                resources.files("orthrus_tools") / "profiles" / f"{name_or_path}.json"
            ).read_text(encoding="utf-8")
        else:
            text = Path(name_or_path).read_text(encoding="utf-8")
        return Profile.from_dict(json.loads(text))


@dataclass(frozen=True)
class Measurement:
    """How two frames of the same size differ."""

    changedFraction: float
    meanAbsolute: float
    maxAbsolute: int


def _require_imaging() -> None:
    if np is None or Image is None:
        raise ToolError(
            ENVIRONMENT,
            'orthrus-frames needs numpy and Pillow: pip install "orthrus-tools[frames]"',
        )


def load_rgb(path: Path):
    _require_imaging()
    with Image.open(path) as image:
        return np.asarray(image.convert("RGB"), dtype=np.int16)


def measure_pair(reference, candidate, channel_threshold: int) -> Measurement:
    if reference.shape != candidate.shape:
        raise ValueError(
            f"sizes differ: {reference.shape[1]}x{reference.shape[0]} "
            f"and {candidate.shape[1]}x{candidate.shape[0]}"
        )
    difference = np.abs(reference - candidate)
    per_pixel = difference.max(axis=2)
    return Measurement(
        float((per_pixel > channel_threshold).mean()),
        float(difference.mean()),
        int(difference.max()),
    )


def passes(measurement: Measurement, profile: Profile) -> bool:
    return (
        measurement.changedFraction <= profile.maxChangedFraction
        and measurement.meanAbsolute <= profile.maxMeanAbsolute
    )


def frames(directory: Path, recursive: bool = False) -> dict[str, Path]:
    """PNG files keyed by their name, or by their path relative to ``directory`` (always ``/``)."""
    pattern = "**/*.png" if recursive else "*.png"
    found = {}
    for path in sorted(directory.glob(pattern)):
        if path.is_file():
            found[path.relative_to(directory).as_posix()] = path
    return found


def write_difference(reference, candidate, channel_threshold: int, path: Path) -> None:
    """Saves the reference dimmed to gray, with changed pixels in red scaled by their difference."""
    per_pixel = np.abs(reference - candidate).max(axis=2)
    gray = (reference.mean(axis=2) * 0.35).astype(np.uint8)
    out = np.stack([gray, gray, gray], axis=2)
    changed = per_pixel > channel_threshold
    count = int(changed.sum())
    out[changed] = np.stack(
        [
            np.clip(96 + per_pixel[changed] * 2, 0, 255).astype(np.uint8),
            np.zeros(count, dtype=np.uint8),
            np.zeros(count, dtype=np.uint8),
        ],
        axis=1,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(out, "RGB").save(path)


def compare(
    reference_dir: Path,
    candidate_dir: Path,
    profile: Profile,
    out_dir: Path | None,
    recursive: bool = False,
) -> dict[str, Any]:
    reference, candidate = frames(reference_dir, recursive), frames(candidate_dir, recursive)
    results: list[dict[str, Any]] = []
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
            write_difference(a, b, profile.channelThreshold, out_dir / name)
    report = {
        "reference": reference_dir.as_posix(),
        "candidate": candidate_dir.as_posix(),
        "profile": asdict(profile),
        "passed": bool(results) and all(result["passed"] for result in results),
        "frames": results,
    }
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def measure_runs(
    runs: list[Path], channel_threshold: int, recursive: bool = False
) -> dict[str, Any]:
    """Differences of every later run against the first, per frame, and the worst of each measure."""
    first = frames(runs[0], recursive)
    rows: list[dict[str, Any]] = []
    for run in runs[1:]:
        other = frames(run, recursive)
        for name in sorted(set(first) & set(other)):
            try:
                measurement = measure_pair(
                    load_rgb(first[name]), load_rgb(other[name]), channel_threshold
                )
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
    return {
        "baseline": runs[0].as_posix(),
        "channelThreshold": channel_threshold,
        "pairs": len(measured),
        "identicalPairs": sum(1 for row in measured if row["maxAbsolute"] == 0),
        "worst": worst,
        "rows": rows,
    }


def suggest_profile(report: dict[str, Any], margin: float = 1.5) -> dict[str, Any]:
    """A starting profile whose limits sit ``margin`` times above the measured run-to-run noise."""
    worst = report["worst"]
    return {
        "channelThreshold": report["channelThreshold"],
        "maxChangedFraction": min(1.0, round(worst["changedFraction"] * margin, 6)),
        "maxMeanAbsolute": round(worst["meanAbsolute"] * margin, 4),
    }


def build_parser() -> cli.Parser:
    parser = cli.parser(
        "orthrus-frames",
        (__doc__ or "").splitlines()[0],
        epilog="Bundled profiles: " + ", ".join(BUNDLED_PROFILES),
    )
    commands = cli.subcommands(parser)

    c = commands.add_parser("compare", help="pass or fail a candidate against a reference")
    c.add_argument("reference", type=Path)
    c.add_argument("candidate", type=Path)
    c.add_argument(
        "--profile",
        required=True,
        metavar="NAME_OR_FILE",
        help="bundled profile name or profile JSON path",
    )
    c.add_argument(
        "--out", type=Path, metavar="DIR", help="directory for report.json and difference images"
    )
    c.add_argument("--recursive", action="store_true", help="match frames in subdirectories too")

    m = commands.add_parser("measure", help="report differences between repeated captures")
    m.add_argument("runs", type=Path, nargs="+")
    m.add_argument(
        "--channel-threshold",
        type=int,
        default=8,
        metavar="0-255",
        help="per-channel difference that counts as a changed pixel",
    )
    m.add_argument("--recursive", action="store_true", help="match frames in subdirectories too")
    m.add_argument(
        "--suggest-profile",
        type=Path,
        metavar="OUT.json",
        help="write a profile 1.5x above the measured noise",
    )
    return parser


def run(args: argparse.Namespace, out: Output) -> int:
    if args.command == "compare":
        for directory in (args.reference, args.candidate):
            if not directory.is_dir():
                raise ToolError(USAGE, f"Not a directory: {directory}")
        try:
            profile = Profile.load(args.profile)
        except (OSError, ValueError, KeyError) as error:
            raise ToolError(USAGE, f"Cannot read profile {args.profile}: {error}") from None
        _require_imaging()
        report = compare(args.reference, args.candidate, profile, args.out, args.recursive)
        out.result = report
        for frame in report["frames"]:
            if "reason" in frame:
                out.line(f"FAIL  {frame['frame']}: {frame['reason']}")
            else:
                out.line(
                    f"{'pass' if frame['passed'] else 'FAIL'}  {frame['frame']}: "
                    f"{frame['changedFraction']:.4%} changed, mean {frame['meanAbsolute']:.3f}, "
                    f"max {frame['maxAbsolute']}"
                )
        if not report["frames"]:
            out.warn("No frames to compare.")
        out.line("PASSED" if report["passed"] else "FAILED")
        return OK if report["passed"] else FINDING

    if len(args.runs) < 2:
        raise ToolError(USAGE, "measure needs at least two runs")
    for directory in args.runs:
        if not directory.is_dir():
            raise ToolError(USAGE, f"Not a directory: {directory}")
    _require_imaging()
    report = measure_runs(args.runs, args.channel_threshold, args.recursive)
    out.result = report
    if args.suggest_profile:
        args.suggest_profile.write_text(
            json.dumps(suggest_profile(report), indent=2) + "\n", encoding="utf-8"
        )
    worst = report["worst"]
    out.line(
        f"{report['pairs']} frame pairs against {report['baseline']}; "
        f"{report['identicalPairs']} identical"
    )
    out.line(
        f"worst changed fraction {worst['changedFraction']:.4%}, "
        f"worst mean {worst['meanAbsolute']:.3f}, worst max {worst['maxAbsolute']}"
    )
    return OK


def main(argv: list[str] | None = None) -> int:
    return cli.run("frames", build_parser, run, argv)


if __name__ == "__main__":
    sys.exit(main())
