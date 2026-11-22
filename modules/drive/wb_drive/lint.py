"""The tour lint: every way a tour would waste a session, found before the session plays it (rules in RULES)."""

from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass, field

from .rules import Rules, command_name, command_text, matches
from .tour import Finding, Tour, View, parse

RULES = {
    "format": "the document reads as a tour",
    "required": "every view has the keys the rule set requires",
    "type": "every key holds what the format says",
    "unknown-key": "no view has a key the format does not know",
    "label": "labels match the label pattern and no two views share one",
    "rotation": "pitch lies within -90..90",
    "dimension": "every view is in a dimension the rule set knows",
    "band": "every view stands inside its dimension's height band",
    "border": "every view stands inside its dimension's world border",
    "command": "commands are single lines the rule set allows, with known placeholders",
    "fill": "no fill changes more blocks than the limit",
    "view-distance": "the tour's view distances use names the rule set knows",
    "settle": "every captured view settles long enough for the session's view distance",
    "move": "every capture is far enough from the one before for the runner to take it",
    "capture": "capture names are safe file names, unique, with one extension and one way of numbering",
    "duration": "the tour fits its time limit",
}
PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
EXTENSION = re.compile(r"\.([A-Za-z]{2,4})$")
DIGITS = re.compile(r"\d+")


@dataclass
class Linted:
    tour: Tour
    rules: Rules
    findings: list[Finding]
    checked: dict[str, str]                   # rule -> "checked", or why it was not
    view_distance: int | None                 # the session's, as the lint took it
    seconds: float                            # about how long the tour takes
    rows: list[dict] = field(default_factory=list)

    @property
    def captures(self) -> int:
        return sum(1 for view in self.tour.views if view.captured)

    def broken(self) -> list[str]:
        return sorted({finding.rule for finding in self.findings}, key=list(RULES).index)


def lint(document, rules: Rules, view_distance: int = 0) -> Linted:
    tour = parse(document, rules.tick_rate, rules.required)
    findings = list(tour.findings)
    checked = {rule: "checked" for rule in RULES}
    views = tour.views
    _labels(views, rules, findings)
    for view in views:
        if view.pitch is not None and not -90 <= view.pitch <= 90:
            findings.append(Finding("rotation", f"has pitch {view.pitch:g}, outside -90..90", view.index, view.label))
    _places(views, rules, findings, checked)
    _commands(views, rules, findings, checked)
    if rules.view_distance_keys:
        for key in tour.view_distance:
            if key not in rules.view_distance_keys:
                named = f"names the view distance {key!r}" if key else "gives a bare view distance"
                findings.append(Finding("view-distance", f"the tour {named}; the rule set knows "
                                                         f"{', '.join(rules.view_distance_keys)}"))
    else:
        checked["view-distance"] = "the rule set names no view distances"
    distance = view_distance or max(tour.view_distance.values(), default=0) or rules.view_distance or None
    arrivals = _arrivals(views, rules, distance)
    rows = []
    for view in views:
        settle = view.settle if view.settle is not None else rules.settle_default
        rows.append({"view": view.index, "label": view.label, "pos": list(view.pos) if view.pos else None,
                     "yaw": view.yaw, "pitch": view.pitch, "dimension": view.dimension, "capture": view.capture,
                     "captured": view.captured, "settle": round(settle, 3), "arrives": view.index in arrivals,
                     "commands": len(view.commands)})
    _settles(views, rules, distance, arrivals, rows, findings, checked)
    _moves(views, rules, findings, checked)
    _captures(views, rules, findings)
    total = rules.startup + sum(row["settle"] + rules.view_overhead for row in rows)
    if rules.max_seconds:
        if total > rules.max_seconds:
            findings.append(Finding("duration", f"the tour takes about {total:,.0f} s (settles, {rules.view_overhead:g}"
                                                f" s a view and {rules.startup:g} s to start); its limit is "
                                                f"{rules.max_seconds:,.0f} s"))
    else:
        checked["duration"] = "no time limit set"
    if tour.shape == "none":
        checked = {rule: ("checked" if rule == "format" else "the document is not a tour") for rule in RULES}
    return Linted(tour, rules, findings, checked, distance, round(total, 3), rows)


def _labels(views: list[View], rules: Rules, findings: list[Finding]) -> None:
    first: dict[str, int] = {}
    for view in views:
        if view.label is None:
            continue
        if not rules.label_pattern.search(view.label):
            findings.append(Finding("label", f"has a label that does not match {rules.label_pattern.pattern}",
                                    view.index, view.label))
        if view.label in first:
            findings.append(Finding("label", f"repeats the label of view {first[view.label]}", view.index,
                                    view.label))
        else:
            first[view.label] = view.index


def _places(views: list[View], rules: Rules, findings: list[Finding], checked: dict) -> None:
    if not any(rules.area_of(d) and rules.area_of(d).band for d in [None, *rules.dimensions]):
        checked["band"] = "no height band set"
    if not rules.dimensions:
        checked["dimension"] = "the rule set names no dimensions"
    for view in views:
        if view.pos is None:
            continue
        area = rules.area_of(view.dimension)
        if area is None:
            named = (f"is in dimension {view.dimension}" if view.dimension or rules.dimension
                     else "names no dimension, and the rule set has no default one")
            findings.append(Finding("dimension", f"{named}; the rule set knows "
                                                 f"{', '.join(sorted(rules.dimensions))}", view.index, view.label))
            continue
        x, y, z = view.pos
        if area.band and not area.band[0] <= y <= area.band[1]:
            side = "below" if y < area.band[0] else "above"
            findings.append(Finding("band", f"stands at y {y:g}, {side} the height band {area.band[0]:g}.."
                                            f"{area.band[1]:g}", view.index, view.label))
        cx, cz = area.centre
        if abs(x - cx) > area.border or abs(z - cz) > area.border:
            findings.append(Finding("border", f"stands at x {x:g}, z {z:g}, outside the border ({area.border:g} "
                                              f"blocks either way of {cx:g}, {cz:g})", view.index, view.label))


def _fill_volume(words: list[str]) -> int | None:
    """The blocks a fill command changes, when its corners say: absolute corners, or corners relative to the same
    place (~a and ~b); None when they do not."""
    if len(words) < 8:
        return None
    volume = 1
    for a, b in zip(words[1:4], words[4:7]):
        relative = a.startswith("~"), b.startswith("~")
        if relative[0] != relative[1] or a.startswith("^") or b.startswith("^"):
            return None
        try:
            low, high = (float(v[1:] or 0) if r else float(v) for v, r in ((a, relative[0]), (b, relative[1])))
        except ValueError:
            return None
        volume *= abs(math.floor(high) - math.floor(low)) + 1
    return volume


def _commands(views: list[View], rules: Rules, findings: list[Finding], checked: dict) -> None:
    if not rules.fill_limit:
        checked["fill"] = "no fill limit set"
    known = set(rules.placeholders)
    for view in views:
        for number, command in enumerate(view.commands):
            where = f"command {number + 1}"
            if "\n" in command or "\r" in command:
                findings.append(Finding("command", f"has {where} over more than one line", view.index, view.label))
                continue
            text = command_text(command)
            if not text:
                findings.append(Finding("command", f"has {where} empty", view.index, view.label))
                continue
            shown = text if len(text) <= 60 else text[:57] + "..."
            denied = next((entry for entry in rules.deny if matches(text, entry)), None)
            if denied is not None:
                findings.append(Finding("command", f"runs {shown!r}, which the rule set refuses ({denied})",
                                        view.index, view.label))
            elif rules.allow and not any(matches(text, entry) for entry in rules.allow):
                findings.append(Finding("command", f"runs {shown!r}; the rule set allows no {command_name(text)} "
                                                   "command like it", view.index, view.label))
            for name in PLACEHOLDER.findall(text):
                if name not in known:
                    findings.append(Finding("command", f"has the unknown placeholder {{{name}}} in {where} (known: "
                                                       f"{', '.join('{' + k + '}' for k in sorted(known)) or 'none'})",
                                            view.index, view.label))
            if rules.fill_limit and command_name(text) == "fill":
                volume = _fill_volume(text.split(" "))
                if volume is not None and volume > rules.fill_limit:
                    findings.append(Finding("fill", f"fills {volume:,} blocks in {where}; one fill may change at "
                                                    f"most {rules.fill_limit:,}", view.index, view.label))


def _arrivals(views: list[View], rules: Rules, distance: int | None) -> set[int]:
    """The views that arrive somewhere not yet loaded: the first, one in another dimension than the view before,
    and one farther than the arrive distance from it."""
    radius = rules.arrive_distance or (distance * 16 if distance else 0)
    out, previous = set(), None
    for view in views:
        if view.pos is None:
            continue
        if previous is None or (view.dimension or rules.dimension) != (previous.dimension or rules.dimension) or (
                radius and max(abs(a - b) for a, b in zip(view.pos, previous.pos)) > radius):
            out.add(view.index)
        previous = view
    return out


def _settles(views: list[View], rules: Rules, distance: int | None, arrivals: set[int], rows: list[dict],
             findings: list[Finding], checked: dict) -> None:
    if not rules.settle:
        checked["settle"] = "no settle table set"
        return
    if not distance:
        checked["settle"] = "neither the tour, the command nor the rule set gives a view distance"
        return
    row, reached = rules.settle_row(distance)
    if not reached:
        findings.append(Finding("settle", f"the settle table stops at view distance {row.view_distance}; the "
                                          f"session's is {distance}, so its last row is taken as the least"))
    for view, report in zip(views, rows):
        if not view.captured or view.pos is None:
            continue
        arrives = view.index in arrivals
        need = row.arrive if arrives else row.settle
        report["needs"] = need
        have = report["settle"]
        if have + 1e-9 < need:
            declared = "settles" if view.settle is not None else "declares no settle, so gets the default"
            which = "a view that arrives" if arrives else "a view"
            findings.append(Finding("settle", f"{declared} {have:g} s; at view distance {distance}, {which} needs "
                                              f"{need:g} s", view.index, view.label))


def _moves(views: list[View], rules: Rules, findings: list[Finding], checked: dict) -> None:
    if rules.min_move is None:
        checked["move"] = "no least move set"
        return
    across, upright = rules.min_move
    last = None
    for view in views:
        if not view.captured or view.pos is None:
            continue
        if last is not None:
            dx, dy, dz = (a - b for a, b in zip(view.pos, last.pos))
            moved, climbed = abs(dx) + abs(dz), abs(dy)
            if not (moved > across or climbed > upright):
                findings.append(Finding("move", f"is {moved:g} blocks across and {climbed:g} up or down from the "
                                                f"capture before (view {last.index}); the runner takes a capture "
                                                f"only past {across:g} across or {upright:g} up or down",
                                        view.index, view.label))
        last = view


def _captures(views: list[View], rules: Rules, findings: list[Finding]) -> None:
    named = [(view, view.capture) for view in views if view.captured and view.capture]
    seen: dict[str, View] = {}
    for view, name in named:
        stem = name.split(".", 1)[0].upper()
        if name == view.label and not rules.label_pattern.search(name):
            pass  # the label's own finding says it
        elif not rules.capture_pattern.search(name):
            findings.append(Finding("capture", f"captures {name!r}, which does not match "
                                               f"{rules.capture_pattern.pattern}", view.index, view.label))
        elif stem in RESERVED or name.endswith("."):
            findings.append(Finding("capture", f"captures {name!r}, a name Windows cannot keep", view.index,
                                    view.label))
        key = name.lower()
        if key in seen:
            other = seen[key]
            if name == view.label == other.label == other.capture:
                continue  # the repeated label is the finding already
            same = "the same name" if other.capture == name else f"{other.capture!r}, the same name but for case"
            findings.append(Finding("capture", f"captures {name!r}; view {other.index} captures {same}, so one "
                                               "would replace the other", view.index, view.label))
        else:
            seen[key] = view
    extensions = defaultdict(list)
    for view, name in named:
        match = EXTENSION.search(name)
        extensions[match.group(1).lower() if match else ""].append(name)
    if len(extensions) > 1:
        mixed = ", ".join(f"{('.' + key) if key else 'none'} ({len(names)})"
                          for key, names in sorted(extensions.items()))
        findings.append(Finding("capture", f"capture names mix extensions: {mixed}"))
    groups = defaultdict(list)
    for view, name in named:
        groups[DIGITS.sub("#", name)].append(name)
    for skeleton, names in groups.items():
        if len(names) < 2 or "#" not in skeleton:
            continue
        widths = [[len(run) for run in DIGITS.findall(name)] for name in names]
        for position in range(len(widths[0])):
            sizes = {w[position] for w in widths}
            if len(sizes) > 1:
                short = min(names, key=lambda n: len(DIGITS.findall(n)[position]))
                long = max(names, key=lambda n: len(DIGITS.findall(n)[position]))
                findings.append(Finding("capture", f"capture names number without a fixed width ({short!r}, "
                                                   f"{long!r}), so their names sort out of capture order"))
                break
