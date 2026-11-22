"""Rule sets: what a world and the runner that plays it allow a tour or a camera path to do.

Config keeps rule sets by name, in [modules.drive.rules.<name>]; a command takes one by name (rules=NAME) and lays
its own params over it. Without a name, the built-in rules below apply: the game's own limits, nothing more.
Every key is optional:

    band = [min_y, max_y]       the height band a view must stand in (its feet); unset: not checked
    border = 29999984           half the width of the square world border, in blocks
    centre = [0, 0]             the border's centre, x and z
    dimensions = {}             dimension -> {band, border, centre}, each over the keys above; when given, every
                                view must be in one of these dimensions
    dimension = ""              the dimension of a view (and a tour) that names none
    allow = []                  the commands a view may run: command names (fill) or shell patterns over the whole
                                command (gamerule doDaylightCycle *, effect {player} *); empty: any that deny leaves
    deny = [stop, kick, ...]    commands refused anywhere, as names or patterns (checked before allow)
    placeholders = ["player"]   the {placeholders} a command may use
    settle = []                 the settle table: rows {view_distance, settle, arrive}. A captured view must
                                settle at least the settle of the first row whose view_distance reaches the
                                session's, and at least its arrive (default: its settle) when the view arrives: the
                                tour's first view, a view in another dimension than the one before, or one farther
                                than arrive_distance from it
    settle_default = 0          the settle a view gets when it declares none (the runner's own wait)
    view_distance = 0           the session's view distance in chunks, when neither the tour nor the command says
    arrive_distance = 0         blocks, along any axis; 0: the view distance in blocks (chunks x 16)
    tick_rate = 20              ticks a second, for settleTicks and durations like 100t
    min_move = []               [h, v]: a capture must be more than h blocks across (|dx| + |dz|) or more than v
                                up or down (|dy|) from the capture before it, or the runner never takes it;
                                [0, 0] refuses only a repeated position; unset: not checked
    label_pattern = "^[A-Za-z0-9_-]+$"              a view label's form
    capture_pattern = "^[A-Za-z0-9][A-Za-z0-9._-]*$" a capture name's form
    fill_limit = 32768          the most blocks one fill command may change; 0: not checked
    max_seconds = 0             the longest a tour may take (a turn's limit); 0: not checked
    view_overhead = 0           seconds each view takes beyond its settle (the teleport, the capture, filing it)
    startup = 0                 seconds a tour takes before its first view (the launch and the join)
    view_distance_keys = []     the names a tour's viewDistance table may use; empty: any
    required = [label, x, y, z, yaw, pitch]    the keys every view must have
    path = {max_speed, max_turn, max_accel, max_seconds}    a camera path's limits: blocks a second, degrees a
                                second (yaw or pitch), blocks a second squared, and its length; 0: not checked

Durations are seconds or strings: 5s, 2m, 100t (ticks at tick_rate).
"""

from __future__ import annotations

import fnmatch
import math
import re
from dataclasses import dataclass, field

DEFAULTS = {
    "band": None, "border": 29_999_984, "centre": [0, 0], "dimensions": {}, "dimension": "",
    "allow": [], "deny": ["stop", "kick", "ban", "ban-ip", "kill {player}*", "kill @a*", "kill @p*", "kill @e"],
    "placeholders": ["player"], "settle": [], "settle_default": 0, "view_distance": 0, "arrive_distance": 0,
    "tick_rate": 20, "min_move": None, "label_pattern": r"^[A-Za-z0-9_-]+$",
    "capture_pattern": r"^[A-Za-z0-9][A-Za-z0-9._-]*$", "fill_limit": 32768, "max_seconds": 0,
    "view_overhead": 0, "startup": 0, "view_distance_keys": [], "required": ["label", "x", "y", "z", "yaw", "pitch"],
    "path": {},
}
AREA_KEYS = {"band", "border", "centre"}
PATH_KEYS = ("max_speed", "max_turn", "max_accel", "max_seconds")
_TIME = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(t|ms|s|m|h)?\s*$")
_UNITS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}
BUILT_IN = "built-in"


class RuleError(ValueError):
    """A rule set (or a param laid over one) that cannot be used as given."""


def seconds(value, tick_rate: float = 20.0, where: str = "duration") -> float:
    """Seconds from a number of seconds or a string: 5s, 250ms, 2m, 1h, 100t (ticks)."""
    if isinstance(value, bool):
        raise RuleError(f"{where}: {value!r} is not a duration")
    if isinstance(value, (int, float)):
        if value < 0 or not math.isfinite(value):
            raise RuleError(f"{where}: {value!r} is not a duration of at least 0")
        return float(value)
    match = _TIME.match(str(value))
    if not match:
        raise RuleError(f"{where}: {value!r} is not a duration (5s, 2m, 100t)")
    amount, unit = float(match.group(1)), match.group(2) or "s"
    return amount / tick_rate if unit == "t" else amount * _UNITS[unit]


@dataclass(frozen=True)
class Area:
    band: tuple[float, float] | None
    border: float
    centre: tuple[float, float]

    def describe(self) -> dict:
        return {"band": list(self.band) if self.band else None, "border": self.border, "centre": list(self.centre)}


@dataclass(frozen=True)
class SettleRow:
    view_distance: int
    settle: float
    arrive: float


@dataclass(frozen=True)
class PathLimits:
    max_speed: float = 0.0
    max_turn: float = 0.0
    max_accel: float = 0.0
    max_seconds: float = 0.0

    def describe(self) -> dict:
        return {key: getattr(self, key) for key in PATH_KEYS}


@dataclass
class Rules:
    name: str
    area: Area
    dimensions: dict[str, Area]
    dimension: str
    allow: list[str]
    deny: list[str]
    placeholders: list[str]
    settle: list[SettleRow]
    settle_default: float
    view_distance: int
    arrive_distance: float
    tick_rate: float
    min_move: tuple[float, float] | None
    label_pattern: re.Pattern
    capture_pattern: re.Pattern
    fill_limit: int
    max_seconds: float
    view_overhead: float
    startup: float
    view_distance_keys: list[str]
    required: list[str]
    path: PathLimits = field(default_factory=PathLimits)

    def area_of(self, dimension: str | None) -> Area | None:
        """The area a view in this dimension must stand in; None for a dimension the rule set does not know."""
        if not self.dimensions:
            return self.area
        return self.dimensions.get(dimension or self.dimension)

    def settle_row(self, view_distance: int) -> tuple[SettleRow | None, bool]:
        """The settle table's row for a view distance, and whether the table reaches it (when it does not, its
        last row is the least a view needs)."""
        for row in self.settle:
            if row.view_distance >= view_distance:
                return row, True
        return (self.settle[-1], False) if self.settle else (None, False)

    def describe(self) -> dict:
        return {"name": self.name, **self.area.describe(),
                "dimensions": {k: v.describe() for k, v in self.dimensions.items()}, "dimension": self.dimension,
                "allow": self.allow, "deny": self.deny, "placeholders": self.placeholders,
                "settle": [row.__dict__ for row in self.settle], "settle_default": self.settle_default,
                "view_distance": self.view_distance, "arrive_distance": self.arrive_distance,
                "tick_rate": self.tick_rate, "min_move": list(self.min_move) if self.min_move else None,
                "label_pattern": self.label_pattern.pattern, "capture_pattern": self.capture_pattern.pattern,
                "fill_limit": self.fill_limit, "max_seconds": self.max_seconds, "view_overhead": self.view_overhead,
                "startup": self.startup, "view_distance_keys": self.view_distance_keys, "required": self.required,
                "path": self.path.describe()}


def resolve(configured: dict | None, name: str = "", overrides: dict | None = None) -> Rules:
    """The rule set named (from [modules.drive.rules]), or the built-in one, with overrides laid over it (keys as
    in a rule set; None leaves a key as it is)."""
    values = dict(DEFAULTS)
    configured = configured or {}
    where = name or BUILT_IN
    if name:
        if name not in configured:
            known = ", ".join(sorted(configured)) or "none"
            raise RuleError(f"no rule set {name!r} in [modules.drive.rules] (known: {known})")
        table = configured[name]
        if not isinstance(table, dict):
            raise RuleError(f"rule set {name}: must be a table")
        unknown = set(table) - set(DEFAULTS)
        if unknown:
            raise RuleError(f"rule set {name}: unknown keys {', '.join(sorted(unknown))} (keys: wb_drive/rules.py)")
        values.update(table)
    for key, value in (overrides or {}).items():
        if value is not None:
            if key not in DEFAULTS:
                raise RuleError(f"no rule {key}")
            values[key] = value
    return _build(values, where)


def _number(value, where: str, low: float | None = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise RuleError(f"{where}: {value!r} is not a number")
    if low is not None and value < low:
        raise RuleError(f"{where}: {value} is below {low:g}")
    return float(value)


def _strings(value, where: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise RuleError(f"{where}: must be a list of strings")
    return list(value)


def _pair(value, where: str, low: float | None = None) -> tuple[float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise RuleError(f"{where}: must be two numbers")
    return _number(value[0], where, low), _number(value[1], where, low)


def _area(table: dict, base: Area | None, where: str) -> Area:
    band = table.get("band", base.band if base else None)
    if band is not None:
        band = _pair(band, f"{where}.band")
        if band[0] > band[1]:
            raise RuleError(f"{where}.band: {band[0]:g} is above {band[1]:g}")
    border = _number(table.get("border", base.border if base else DEFAULTS["border"]), f"{where}.border")
    centre = _pair(table.get("centre", list(base.centre) if base else DEFAULTS["centre"]), f"{where}.centre")
    return Area(band, border, centre)


def _pattern(value, where: str) -> re.Pattern:
    try:
        return re.compile(str(value))
    except re.error as error:
        raise RuleError(f"{where}: {error}") from None


def _build(values: dict, name: str) -> Rules:
    tick_rate = _number(values["tick_rate"], f"{name}.tick_rate")
    if tick_rate <= 0:
        raise RuleError(f"{name}.tick_rate: must be above 0")
    area = _area(values, None, name)
    dimensions = values["dimensions"]
    if not isinstance(dimensions, dict) or not all(isinstance(v, dict) for v in dimensions.values()):
        raise RuleError(f"{name}.dimensions: must be a table of tables")
    for key, table in dimensions.items():
        unknown = set(table) - AREA_KEYS
        if unknown:
            raise RuleError(f"{name}.dimensions.{key}: unknown keys {', '.join(sorted(unknown))}")
    rows = values["settle"]
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise RuleError(f"{name}.settle: must be a list of {{view_distance, settle, arrive}} rows")
    table = []
    for index, row in enumerate(rows):
        where = f"{name}.settle[{index}]"
        unknown = set(row) - {"view_distance", "settle", "arrive"}
        if unknown or "view_distance" not in row or "settle" not in row:
            raise RuleError(f"{where}: a row is {{view_distance, settle, arrive}} (arrive optional)")
        settle = seconds(row["settle"], tick_rate, f"{where}.settle")
        arrive = seconds(row.get("arrive", row["settle"]), tick_rate, f"{where}.arrive")
        table.append(SettleRow(int(_number(row["view_distance"], f"{where}.view_distance")), settle,
                               max(settle, arrive)))
    table.sort(key=lambda row: row.view_distance)
    move = values["min_move"]
    path = values["path"] or {}
    if not isinstance(path, dict) or set(path) - set(PATH_KEYS):
        raise RuleError(f"{name}.path: a table of {', '.join(PATH_KEYS)}")
    limits = PathLimits(**{key: (seconds(path[key], tick_rate, f"{name}.path.{key}") if key == "max_seconds"
                                 else _number(path[key], f"{name}.path.{key}")) for key in path})
    view_distance = _number(values["view_distance"], f"{name}.view_distance")
    fill_limit = _number(values["fill_limit"], f"{name}.fill_limit")
    if not view_distance.is_integer() or not fill_limit.is_integer():
        raise RuleError(f"{name}: view_distance and fill_limit are whole numbers")
    dimension = values["dimension"]
    if isinstance(dimension, bool) or not isinstance(dimension, (str, int)):
        raise RuleError(f"{name}.dimension: a name or a number")
    return Rules(
        name=name, area=area,
        dimensions={str(key): _area(table, area, f"{name}.dimensions.{key}") for key, table in dimensions.items()},
        dimension=str(dimension), allow=_strings(values["allow"], f"{name}.allow"),
        deny=_strings(values["deny"], f"{name}.deny"),
        placeholders=_strings(values["placeholders"], f"{name}.placeholders"), settle=table,
        settle_default=seconds(values["settle_default"], tick_rate, f"{name}.settle_default"),
        view_distance=int(view_distance),
        arrive_distance=_number(values["arrive_distance"], f"{name}.arrive_distance"), tick_rate=tick_rate,
        min_move=_pair(move, f"{name}.min_move", 0.0) if move is not None else None,
        label_pattern=_pattern(values["label_pattern"], f"{name}.label_pattern"),
        capture_pattern=_pattern(values["capture_pattern"], f"{name}.capture_pattern"),
        fill_limit=int(fill_limit), max_seconds=seconds(values["max_seconds"], tick_rate, f"{name}.max_seconds"),
        view_overhead=seconds(values["view_overhead"], tick_rate, f"{name}.view_overhead"),
        startup=seconds(values["startup"], tick_rate, f"{name}.startup"),
        view_distance_keys=_strings(values["view_distance_keys"], f"{name}.view_distance_keys"),
        required=_strings(values["required"], f"{name}.required"), path=limits)


# -- commands -------------------------------------------------------------------------------------------------------
def command_text(command: str) -> str:
    """A console command as the game reads it: without a leading slash, its words single-spaced."""
    return " ".join(command.strip().removeprefix("/").split())


def command_name(command: str) -> str:
    text = command_text(command)
    return text.split(" ", 1)[0].lower() if text else ""


def matches(command: str, entry: str) -> bool:
    """Whether a command is the one an allow or deny entry names: a bare name matches the command's first word, any
    other entry is a shell pattern over the whole command (case does not count)."""
    entry = " ".join(entry.split())
    if not any(ch in entry for ch in "*?[ "):
        return command_name(command) == entry.lower().removeprefix("/")
    return fnmatch.fnmatchcase(command_text(command).lower(), entry.lower().removeprefix("/"))
