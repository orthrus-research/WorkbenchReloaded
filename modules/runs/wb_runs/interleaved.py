"""Treatments interleaved within one session, paired window by window.

A session that alternates conditions in fixed turns can lock onto its workload's own rhythm (a tour that holds each
place for a fixed time), so one condition keeps landing on the cheap stretches. Here a sample's condition comes from
a hash of its index instead, and the analysis pairs the conditions inside each window of consecutive samples, so a
window's conditions saw the same part of the workload.

The assignment any driver can follow: condition = conditions[splitmix64(seed + index) % len(conditions)], with
splitmix64 the standard 64-bit finaliser (see ``assign``).
"""

from __future__ import annotations

import csv
import json
import random
import statistics
from pathlib import Path

MASK = (1 << 64) - 1


def splitmix64(value: int) -> int:
    value = (value + 0x9E3779B97F4A7C15) & MASK
    value = ((value ^ (value >> 30)) * 0xBF58476D1CE4E5B9) & MASK
    value = ((value ^ (value >> 27)) * 0x94D049BB133111EB) & MASK
    return value ^ (value >> 31)


def assign(index: int, conditions: list[str], seed: int = 0) -> str:
    """The condition of sample ``index``: the same on every machine and in every language that implements it."""
    return conditions[splitmix64((seed + index) & MASK) % len(conditions)]


def read_samples(path: Path) -> list[dict]:
    """Samples from a CSV with a header or from JSON lines: each has an index, a condition and metric columns."""
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".jsonl", ".json"):
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    return list(csv.DictReader(text.splitlines()))


def _number(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def windows(samples: list[dict], index: str, size: int) -> list[list[dict]]:
    ordered = sorted(samples, key=lambda s: int(float(s[index])))
    return [ordered[i:i + size] for i in range(0, len(ordered) - size + 1, size)]


def _bootstrap(differences: list[float], rounds: int, seed: int) -> tuple[float, float]:
    """A 95% interval of the median window difference, by resampling windows."""
    rng = random.Random(seed)
    medians = sorted(statistics.median(rng.choices(differences, k=len(differences))) for _ in range(rounds))
    return medians[int(0.025 * (rounds - 1))], medians[int(0.975 * (rounds - 1))]


def analyse(samples: list[dict], *, index: str, condition: str, metrics: list[str], baseline: str,
            treatments: list[str], size: int, min_share: float, rounds: int = 2000, seed: int = 0) -> dict:
    """Per window and metric: each condition's median and its difference from the baseline's; overall: the median of
    the window differences, how many windows each treatment led, and a bootstrap interval. A window where a
    condition holds less than ``min_share`` of the samples is unbalanced and left out of the pairing."""
    conditions = [baseline, *treatments]
    report = {"windows": [], "metrics": {}, "unbalanced": [], "samples": len(samples), "size": size}
    paired: dict[str, dict[str, list[float]]] = {m: {t: [] for t in treatments} for m in metrics}
    for number, window in enumerate(windows(samples, index, size)):
        shares = {c: sum(1 for s in window if s[condition] == c) / len(window) for c in conditions}
        entry = {"window": number, "from": window[0][index], "to": window[-1][index], "shares": shares}
        if min(shares.values()) < min_share:
            report["unbalanced"].append(entry)
            continue
        entry["medians"] = {}
        for metric in metrics:
            values = {c: [v for s in window if s[condition] == c and (v := _number(s.get(metric))) is not None]
                      for c in conditions}
            medians = {c: statistics.median(v) for c, v in values.items() if v}
            entry["medians"][metric] = medians
            if baseline in medians:
                for t in treatments:
                    if t in medians:
                        paired[metric][t].append(medians[t] - medians[baseline])
        report["windows"].append(entry)
    for metric in metrics:
        report["metrics"][metric] = {}
        for t, differences in paired[metric].items():
            if not differences:
                continue
            low, high = _bootstrap(differences, rounds, seed) if len(differences) > 1 else (differences[0],) * 2
            report["metrics"][metric][t] = {
                "windows": len(differences), "medianDifference": statistics.median(differences),
                "interval95": [low, high], "treatmentLower": sum(1 for d in differences if d < 0),
                "treatmentHigher": sum(1 for d in differences if d > 0)}
    return report
