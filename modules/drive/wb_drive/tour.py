"""Tours: what a session is made to see, view by view (format wb/tour/v1).

A tour is a JSON (or TOML) document: a list of views, or a table of

    views           the views, in order (required)
    viewDistance    the session's view distance: chunks, or a table of named distances in chunks
    dimension       the dimension of every view that names none
    settleTicks | settleSeconds    the settle of every view that declares none
    format          "wb/tour/v1"; name, a free-form name; keys starting x- are extensions, ignored

A view is a table of

    label           letters, digits, - and _: names the view and, unless capture says otherwise, its capture
    x, y, z         where the player stands (its feet), in blocks
    yaw, pitch      where it looks, in degrees: yaw 0 faces south (+z), 90 west (-x), 180 north, -90 east;
                    pitch 90 looks straight down, -90 straight up
    dimension       optional: the dimension, as the game names it (a name or a number)
    commands        optional: console commands run in order before the teleport to the view; {player} stands
                    for the player's name
    settleTicks | settleSeconds    optional: how long the player holds still at the view before its capture
    capture         optional: true (the default), one capture named by the label; a string, the capture's name;
                    false, no capture (a view that only moves the player or runs commands)
    x-...           extensions, ignored

Tours this module writes take the smallest form that says everything: a table of views, with viewDistance only
when one is declared, and every default written into the views, so a runner that knows only views and
viewDistance plays them.
"""

from __future__ import annotations

import json
import math
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

FORMAT = "wb/tour/v1"
TOUR_KEYS = {"views", "viewDistance", "dimension", "settleTicks", "settleSeconds", "format", "name"}
VIEW_KEYS = {"label", "x", "y", "z", "yaw", "pitch", "dimension", "commands", "settleTicks", "settleSeconds",
             "capture"}
REQUIRED = ("label", "x", "y", "z", "yaw", "pitch")
EYE = 1.62  # a standing (or spectating) player's eye above its feet, in blocks


@dataclass(frozen=True)
class Finding:
    """One violation: the rule it breaks, the view it is about (None for the tour as a whole), and what is wrong,
    said of the view ("settles 5 s; ...")."""
    rule: str
    message: str
    view: int | None = None
    label: str | None = None

    def text(self) -> str:
        if self.view is None:
            return f"{self.rule}: {self.message}"
        return f"{self.rule}: view {self.view}{f' ({self.label})' if self.label else ''} {self.message}"

    def as_dict(self) -> dict:
        return {"rule": self.rule, "view": self.view, "label": self.label, "message": self.message,
                "text": self.text()}


@dataclass
class View:
    index: int
    label: str | None = None
    pos: tuple[float, float, float] | None = None
    yaw: float | None = None
    pitch: float | None = None
    dimension: str | None = None
    commands: list[str] = field(default_factory=list)
    settle: float | None = None      # seconds; None: declares none (nor does the tour)
    capture: str | None = None       # the capture's name; None: no capture
    captured: bool = True


@dataclass
class Tour:
    shape: str                                   # list, table or none (not a tour at all)
    views: list[View]
    view_distance: dict[str, int]                # declared distances by name; a bare number is named ""
    name: str | None
    findings: list[Finding]                      # where the document does not read as the format


def load(path: Path):
    """A JSON or TOML document (by suffix: .toml is TOML, anything else JSON)."""
    path = Path(path)
    text = path.read_text(encoding="utf-8-sig")
    try:
        return tomllib.loads(text) if path.suffix.lower() == ".toml" else json.loads(text)
    except (ValueError, tomllib.TOMLDecodeError) as error:
        raise ValueError(f"{path.name}: {error}") from None


def number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _extension(key) -> bool:
    return isinstance(key, str) and key.startswith("x-")


def _settle(table: dict, tick_rate: float, findings: list[Finding], view: int | None = None,
            label: str | None = None) -> float | None:
    """The settle a table (a view, or the tour) declares, in seconds; None when it declares none."""
    who = "the tour " if view is None else ""
    ticks, seconds = table.get("settleTicks"), table.get("settleSeconds")
    if ticks is not None and seconds is not None:
        findings.append(Finding("type", f"{who}declares both settleTicks and settleSeconds", view, label))
        return None
    for key, value, scale in (("settleTicks", ticks, 1 / tick_rate), ("settleSeconds", seconds, 1.0)):
        if value is None:
            continue
        if not number(value) or value < 0:
            findings.append(Finding("type", f"{who}has {key} {value!r}, not a number of at least 0", view, label))
            return None
        return float(value) * scale
    return None


def parse(document, tick_rate: float = 20.0, required=REQUIRED) -> Tour:
    """Reads a tour document into views, noting every way it falls short of the format as a finding."""
    findings: list[Finding] = []
    if isinstance(document, list):
        shape, raw_views, top = "list", document, {}
    elif isinstance(document, dict) and isinstance(document.get("views"), list):
        shape, raw_views, top = "table", document["views"], document
    else:
        found = "a table without a list of views" if isinstance(document, dict) else type(document).__name__
        findings.append(Finding("format", f"a tour is a list of views or a table with views, not {found}"))
        return Tour("none", [], {}, None, findings)
    for key in sorted(set(top) - TOUR_KEYS, key=str):
        if not _extension(key):
            findings.append(Finding("format", f"the tour has the unknown key {key!r} (known: "
                                              f"{', '.join(sorted(TOUR_KEYS))}; x-... for extensions)"))
    if "format" in top and top["format"] != FORMAT:
        findings.append(Finding("format", f"the tour says format {top['format']!r}; this reads {FORMAT}"))
    distances: dict[str, int] = {}
    declared = top.get("viewDistance")
    if isinstance(declared, int) and not isinstance(declared, bool) and declared >= 0:
        distances[""] = declared
    elif isinstance(declared, dict) and all(isinstance(v, int) and not isinstance(v, bool) and v >= 0
                                            for v in declared.values()):
        distances = {str(k): v for k, v in declared.items()}
    elif declared is not None:
        findings.append(Finding("format", f"viewDistance is {declared!r}: chunks, or a table of named chunks"))
    default_dimension = top.get("dimension")
    if default_dimension is not None and (isinstance(default_dimension, bool)
                                          or not isinstance(default_dimension, (str, int))):
        findings.append(Finding("format", f"the tour's dimension {default_dimension!r} is not a name or a number"))
        default_dimension = None
    default_settle = _settle(top, tick_rate, findings)
    views = [_view(index, raw, tick_rate, required, default_dimension, default_settle, findings)
             for index, raw in enumerate(raw_views)]
    name = top.get("name") if isinstance(top.get("name"), str) else None
    return Tour(shape, views, distances, name, findings)


def _view(index: int, raw, tick_rate: float, required, default_dimension, default_settle,
          findings: list[Finding]) -> View:
    view = View(index)
    if not isinstance(raw, dict):
        findings.append(Finding("format", f"is {type(raw).__name__}, not a table", index))
        view.captured = False
        return view
    label = raw.get("label")
    view.label = label if isinstance(label, str) else None
    for key in sorted(set(raw) - VIEW_KEYS, key=str):
        if not _extension(key):
            findings.append(Finding("unknown-key", f"has the unknown key {key!r}", index, view.label))
    for key in required:
        if key not in raw:
            findings.append(Finding("required", f"has no {key}", index, view.label))
    if label is not None and not isinstance(label, str):
        findings.append(Finding("type", f"has the label {label!r}, not a string", index))
    values = {}
    for key in ("x", "y", "z", "yaw", "pitch"):
        value = raw.get(key)
        if value is None:
            continue
        if number(value):
            values[key] = float(value)
        else:
            findings.append(Finding("type", f"has {key} {value!r}, not a number", index, view.label))
    if all(k in values for k in ("x", "y", "z")):
        view.pos = (values["x"], values["y"], values["z"])
    view.yaw, view.pitch = values.get("yaw"), values.get("pitch")
    dimension = raw.get("dimension", default_dimension)
    if dimension is not None:
        if isinstance(dimension, bool) or not isinstance(dimension, (str, int)):
            findings.append(Finding("type", f"has the dimension {dimension!r}, not a name or a number", index,
                                    view.label))
        else:
            view.dimension = str(dimension)
    commands = raw.get("commands", [])
    if isinstance(commands, list) and all(isinstance(c, str) for c in commands):
        view.commands = list(commands)
    else:
        findings.append(Finding("type", "has commands that are not a list of strings", index, view.label))
    settle = _settle(raw, tick_rate, findings, index, view.label)
    view.settle = settle if settle is not None or "settleTicks" in raw or "settleSeconds" in raw else default_settle
    capture = raw.get("capture", True)
    if capture is True:
        view.capture = view.label
    elif capture is False:
        view.capture, view.captured = None, False
    elif isinstance(capture, str):
        view.capture = capture
    else:
        findings.append(Finding("type", f"has capture {capture!r}: true, false or a name", index, view.label))
        view.capture = view.label
    return view


# -- writing ------------------------------------------------------------------------------------------------------
def normal_yaw(yaw: float) -> float:
    """The same heading in (-180, 180]."""
    turned = math.fmod(yaw + 180.0, 360.0)
    turned = turned + 360.0 if turned < 0 else turned
    return 180.0 if turned == 0 else turned - 180.0


def tidy(value: float, places: int = 3):
    """A number for a file: rounded, and an integer when it is one."""
    rounded = round(float(value), places)
    return int(rounded) if rounded.is_integer() else rounded


def view(label: str, pos, yaw: float, pitch: float, *, commands=(), settle_ticks: int | None = None,
         capture=None, dimension=None, extra: dict | None = None) -> dict:
    """One view as a tour file holds it; yaw is written in (-180, 180]."""
    out = {"label": label, "x": tidy(pos[0]), "y": tidy(pos[1]), "z": tidy(pos[2]),
           "yaw": tidy(normal_yaw(yaw), 2), "pitch": tidy(pitch, 2)}
    if dimension is not None:
        out["dimension"] = dimension
    if commands:
        out["commands"] = list(commands)
    if settle_ticks is not None:
        out["settleTicks"] = int(settle_ticks)
    if capture is not None and capture is not True:
        out["capture"] = capture
    for key, value in (extra or {}).items():
        out[key if key.startswith("x-") else "x-" + key] = value
    return out


def document(views: list[dict], view_distance=None) -> dict:
    return {"viewDistance": view_distance, "views": views} if view_distance else {"views": views}


# -- looking ------------------------------------------------------------------------------------------------------
def look(dx: float, dy: float, dz: float) -> tuple[float, float]:
    """The yaw and pitch of a camera looking along (dx, dy, dz)."""
    return math.degrees(math.atan2(-dx, dz)), math.degrees(math.atan2(-dy, math.hypot(dx, dz)))


def forward(yaw: float, pitch: float) -> tuple[float, float, float]:
    """The unit vector a camera at yaw and pitch looks along."""
    y, p = math.radians(yaw), math.radians(pitch)
    return -math.sin(y) * math.cos(p), -math.sin(p), math.cos(y) * math.cos(p)
