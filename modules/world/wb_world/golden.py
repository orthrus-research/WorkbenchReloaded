"""Worldgen goldens: digests of fixed windows of a generated world, kept in a file beside the generator's code, so
that any change in what the generator makes shows without starting the game. world.golden has the generator's own
sampler (sampler.py) sample the windows twice, the second time in another order and on another thread count, and
compares the runs with each other and with the file.

A golden file, ``wb/world-golden/v1``:

    {
      "format": "wb/world-golden/v1",
      "comment": "...",                             for people; a key not named here is kept as it is
      "seed": 4242,                                 the seed, layers, step and options of every window that
      "layers": ["height", "biome"],                gives none of its own (a window's options are laid over
      "step": 1,                                    the file's)
      "options": {"sea_level": 63},
      "palettes": {"biome": {"ocean": "#1f4e9c"}},  colours by layer, for world.preview
      "windows": [
        {"name": "origin", "columns": [-2048, -2048, 2047, 2047], "step": 32,
         "digests": {"height": {"sha256": "<64 hex>", "summary": {"min": 52, "max": 140}},
                     "biome": {"sha256": "<64 hex>", "summary": {"ocean": 7383, "plains": 9001}}}},
        {"name": "spawn", "seed": 17, "box": [1400, 0, -1500, 1527, 255, -1373], "layers": ["blocks"],
         "comment": "..."}
      ]
    }

A window is as the windows file gives it (sampler.py), but for raster, with "digests": what the sampler wrote for
each layer when the file was last updated. Only format and windows are required, a window's seed and layers being
its own or the file's; a layer without a digest is new. A changed digest is a regression, or an intended change
whose digests belong in the same commit: world.golden with update=true writes the file anew into its output folder,
the digests replaced by those sampled now and all else kept, to copy over this one.
"""

from __future__ import annotations

import copy
import json
import random
from dataclasses import dataclass
from pathlib import Path

from wb_core.module import ParamError

from .sampler import SHA256, Run, Window, parse_window
from .units import many

FORMAT = "wb/world-golden/v1"
DEFAULTS = ("seed", "layers", "step", "options")
SHUFFLE_SEED = 0x5EED  # the verifying run's shuffle is the same every time
_NONE = object()


@dataclass
class Golden:
    """A golden file, read and checked."""

    document: dict                          # the file as read, for writing it anew
    windows: list[Window]
    recorded: dict[str, dict[str, dict]]    # window, layer: sha256 and summary, as last updated
    palettes: dict[str, dict]               # layer: colours by value


def load(path: Path) -> Golden:
    try:
        document = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ParamError(f"golden: {path.name} is not a JSON file ({error})") from None
    if not isinstance(document, dict) or document.get("format") != FORMAT:
        raise ParamError(f"golden: {path.name} is not a golden file: it lacks \"format\": \"{FORMAT}\"")
    try:
        return _parse(document)
    except ValueError as error:
        raise ParamError(f"golden: {path.name}: {error}") from None


def _parse(document: dict) -> Golden:
    entries = document.get("windows")
    if not isinstance(entries, list) or not entries:
        raise ValueError("windows must be a list of at least one window")
    defaults = {key: document[key] for key in DEFAULTS if key in document}
    windows: list[Window] = []
    recorded: dict[str, dict[str, dict]] = {}
    for index, entry in enumerate(entries, 1):
        window = parse_window(entry, defaults, f"window {index}", frozenset({"digests", "comment"}))
        if window.name in recorded:
            raise ValueError(f"two windows are named {window.name}")
        windows.append(window)
        recorded[window.name] = _digests(entry.get("digests", {}), window.name)
    palettes = document.get("palettes", {})
    if not isinstance(palettes, dict) or not all(isinstance(palette, dict) for palette in palettes.values()):
        raise ValueError("palettes must be an object of palettes by layer, each an object of colours by value")
    return Golden(document, windows, recorded, palettes)


def _digests(value, name: str) -> dict[str, dict]:
    if not isinstance(value, dict):
        raise ValueError(f"window {name}: digests must be an object, by layer")
    out = {}
    for layer, digest in value.items():
        sha = digest.get("sha256") if isinstance(digest, dict) else None
        if not isinstance(sha, str) or not SHA256.fullmatch(sha.lower()):
            raise ValueError(f"window {name}: the digest of {layer} has no sha256 of 64 hex digits")
        summary = digest.get("summary")
        out[layer] = {"sha256": sha.lower(), **({"summary": summary} if isinstance(summary, dict) else {})}
    return out


def verifying_order(windows: list[Window], shuffle: bool) -> list[Window]:
    """The verifying run's windows: reversed, or shuffled by a fixed seed; never in the first run's order."""
    if not shuffle:
        return windows[::-1]
    order = list(windows)
    random.Random(SHUFFLE_SEED).shuffle(order)
    return windows[::-1] if order == windows else order


def compare(golden: Golden, first: Run, second: Run | None) -> dict:
    """Every window's layers: the digest sampled, the golden's and, when they differ, the verifying run's; with the
    layers changed, new and not deterministic as window/layer."""
    windows: dict[str, dict[str, dict]] = {}
    found: dict[str, list[str]] = {"changed": [], "new": [], "nondeterministic": []}
    for window in golden.windows:
        rows = windows[window.name] = {}
        for layer in window.layers:
            sampled, recorded = first.digests[window.name][layer], golden.recorded[window.name].get(layer)
            if recorded is None:
                status = "new"
            else:
                status = "same" if recorded["sha256"] == sampled["sha256"] else "changed"
            row = {"status": status, "sha256": sampled["sha256"], "summary": sampled.get("summary"), "golden": recorded}
            if status == "changed":
                row["changes"] = summary_changes(recorded.get("summary"), sampled.get("summary"))
            if status != "same":
                found[status].append(f"{window.name}/{layer}")
            if second is not None:
                other = second.digests[window.name][layer]
                row["deterministic"] = other["sha256"] == sampled["sha256"]
                if not row["deterministic"]:
                    row["verify"] = {"sha256": other["sha256"], "summary": other.get("summary")}
                    row["verifyChanges"] = summary_changes(sampled.get("summary"), other.get("summary"), "vs")
                    found["nondeterministic"].append(f"{window.name}/{layer}")
            rows[layer] = row
    return {"windows": windows, **found}


def findings(found: dict, runs: list[Run], update: bool) -> list[str]:
    """A line per window that is not deterministic and, checking, per window that differs from the golden; and one
    for the layers the golden has no digest of yet."""
    lines = []
    for name, rows in found["windows"].items():
        unstable = {layer: row for layer, row in rows.items() if row.get("deterministic") is False}
        if unstable:
            lines.append(f"{name} is not deterministic: {_layers(unstable, 'verifyChanges')} "
                         f"{_verb(unstable)} between run 1 ({runs[0].label}) and run 2 ({runs[1].label})")
    if update:
        return lines
    for name, rows in found["windows"].items():
        changed = {layer: row for layer, row in rows.items() if row["status"] == "changed"}
        if changed:
            lines.append(f"{name}: {_layers(changed, 'changes')} {_verb(changed)} from the golden")
    new = found["new"]
    if new:
        lines.append(f"{many(len(new), 'layer')} {'has' if len(new) == 1 else 'have'} no golden digest yet "
                     f"({_listed(new)}): record {'it' if len(new) == 1 else 'them'} with update=true")
    return lines


def updated(golden: Golden, run: Run) -> tuple[dict, dict]:
    """The golden file anew, every digest the one sampled now, and what that changes against the file."""
    document = copy.deepcopy(golden.document)
    diff: dict = {"changed": [], "new": [], "dropped": [], "unchanged": 0}
    for entry, window in zip(document["windows"], golden.windows):
        recorded, digests = golden.recorded[window.name], {}
        for layer in window.layers:
            sampled = run.digests[window.name][layer]
            digests[layer] = {key: sampled[key] for key in ("sha256", "summary") if key in sampled}
            old = recorded.get(layer)
            if old is None:
                diff["new"].append(f"{window.name}/{layer}")
            elif old["sha256"] != sampled["sha256"]:
                diff["changed"].append(f"{window.name}/{layer}")
            else:
                diff["unchanged"] += 1
        diff["dropped"] += [f"{window.name}/{layer}" for layer in recorded if layer not in window.layers]
        entry["digests"] = digests
    return document, diff


def update_summary(diff: dict) -> str:
    """2 changed (east/height, east/biome), 1 new, 2 as recorded"""
    parts = [f"{len(diff['changed'])} changed ({_listed(diff['changed'])})" if diff["changed"] else "",
             f"{len(diff['new'])} new" if diff["new"] else "",
             f"{diff['unchanged']} as recorded" if diff["unchanged"] else "",
             f"{len(diff['dropped'])} no longer sampled, left out" if diff["dropped"] else ""]
    return ", ".join(part for part in parts if part)


def summary_changes(old: dict | None, new: dict | None, joiner: str = "->") -> list[str]:
    """What differs between two summaries, key by key ("max 140 -> 152"); nested objects by dotted keys."""
    a, b = _flat(old or {}), _flat(new or {})
    return [f"{key} {_shown(a.get(key, _NONE))} {joiner} {_shown(b.get(key, _NONE))}"
            for key in sorted(a.keys() | b.keys()) if a.get(key, _NONE) != b.get(key, _NONE)]


def _flat(summary: dict, prefix: str = "") -> dict:
    out = {}
    for key, value in summary.items():
        if isinstance(value, dict):
            out.update(_flat(value, f"{prefix}{key}."))
        else:
            out[f"{prefix}{key}"] = value
    return out


def _shown(value) -> str:
    return "none" if value is _NONE else json.dumps(value) if not isinstance(value, str) else value


def _layers(rows: dict[str, dict], key: str) -> str:
    """height (max 140 -> 152, min 52 -> 53) and biome"""
    parts = []
    for layer, row in rows.items():
        changes = row.get(key) or []
        shown = ", ".join(changes[:4]) + (f" and {len(changes) - 4} more" if len(changes) > 4 else "")
        parts.append(f"{layer} ({shown})" if changes else layer)
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def _verb(rows: dict) -> str:
    return "differs" if len(rows) == 1 else "differ"


def _listed(names: list[str], limit: int = 6) -> str:
    return ", ".join(names[:limit]) + (f" and {len(names) - limit} more" if len(names) > limit else "")
