"""Run-level statistics for comparing timings. Never treat correlated samples as independent runs.

Ticks or frames inside one run are strongly correlated, so pooling them overstates confidence.
The unit of evidence is the run: pair each baseline run with a candidate run under the same
conditions and bootstrap the median of the paired changes.

    from orthrus_tools.stats import paired_interval
    paired_interval([(baseline_ms, candidate_ms), ...])["bootstrap95PercentInterval"]

``orthrus-stats PAIRS.json`` reads ``[[before, after], ...]`` and prints the interval; ``--json``
prints the full result.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import sys
from pathlib import Path
from typing import Any

from . import cli
from .cli import OK, USAGE, Output, ToolError

MINIMUM_PAIRS_FOR_REVIEW = 5


def nearest_rank(values: list[float], probability: float) -> float:
    if not values or not 0 < probability <= 1:
        raise ValueError("nearest_rank needs values and a probability in (0, 1]")
    return sorted(values)[max(0, math.ceil(len(values) * probability) - 1)]


def _positive_finite(value: object) -> bool:
    return (
        type(value) in (int, float)
        and math.isfinite(value)  # type: ignore[arg-type]
        and value > 0  # type: ignore[operator]
    )


def paired_interval(
    pairs: list[tuple[float, float]], seed: int = 71423, draws: int = 10000
) -> dict[str, Any]:
    """Percentile bootstrap of the median paired percent change, ``after/before - 1``.

    The caller supplies matched, independent runs from one scenario and metric. A fixed seed
    keeps the result reproducible.
    """
    if not pairs or draws < 1000:
        raise ValueError("need paired runs and at least 1000 resamples")
    for before, after in pairs:
        if not (_positive_finite(before) and _positive_finite(after)):
            raise ValueError("paired costs must be finite and positive")
    changes = [100 * (after / before - 1) for before, after in pairs]
    rng = random.Random(seed)
    n = len(changes)
    medians = [statistics.median(rng.choices(changes, k=n)) for _ in range(draws)]
    return {
        "pairs": n,
        "pairedPercentChanges": changes,
        "medianPercentChange": statistics.median(changes),
        "bootstrap95PercentInterval": [nearest_rank(medians, 0.025), nearest_rank(medians, 0.975)],
        "enoughRepetitionsForReview": n >= MINIMUM_PAIRS_FOR_REVIEW,
        "seed": seed,
        "resamples": draws,
        "scope": (
            "Percentile bootstrap of the median paired run change. Descriptive uncertainty with "
            "few runs; no pooled-sample independence, causal proof or automatic acceptance. "
            f"At least {MINIMUM_PAIRS_FOR_REVIEW} matched run pairs for review; keep per-run tails "
            "and maxima."
        ),
    }


def window_quality(count: int, dropped: int, minimum: int = 3000) -> dict[str, Any]:
    """Whether a sampling window holds enough observations to say anything about its p99 tail."""
    if type(count) is not int or type(dropped) is not int or count < 0 or dropped < 0:
        raise ValueError("counts must be non-negative integers")
    return {
        "observations": count,
        "dropped": dropped,
        "p99UpperTailObservations": count // 100,
        "sufficientForTailReview": count >= minimum and dropped == 0,
        "minimumObservations": minimum,
        "scope": (
            "Sampling gate only. Temporal dependence and independent run repetition still "
            "matter; brief event windows are event measurements, not stable p99 estimates."
        ),
    }


def build_parser() -> cli.Parser:
    parser = cli.parser("orthrus-stats", (__doc__ or "").splitlines()[0])
    parser.add_argument("pairs", type=Path, help="JSON file of [[before, after], ...] run costs")
    parser.add_argument("--seed", type=int, default=71423, metavar="N", help="bootstrap seed")
    parser.add_argument("--draws", type=int, default=10000, metavar="N", help="bootstrap resamples")
    return parser


def run(args: argparse.Namespace, out: Output) -> int:
    try:
        data = json.loads(args.pairs.read_text(encoding="utf-8"))
        pairs = [(pair[0], pair[1]) for pair in data]
    except (OSError, ValueError, TypeError, IndexError, KeyError) as error:
        raise ToolError(USAGE, f"cannot read pairs from {args.pairs}: {error}") from None
    try:
        result = paired_interval(pairs, args.seed, args.draws)
    except ValueError as error:
        raise ToolError(USAGE, str(error)) from None
    out.result = result
    low, high = result["bootstrap95PercentInterval"]
    out.line(
        f"{result['pairs']} pairs: median change {result['medianPercentChange']:+.2f}%, "
        f"95% interval [{low:+.2f}%, {high:+.2f}%]"
    )
    if not result["enoughRepetitionsForReview"]:
        out.warn(f"fewer than {MINIMUM_PAIRS_FOR_REVIEW} pairs: not enough for review")
    return OK


def main(argv: list[str] | None = None) -> int:
    return cli.run("stats", build_parser, run, argv)


if __name__ == "__main__":
    sys.exit(main())
