"""The nvidia-smi adapter: which query fields this nvidia-smi knows, its looping query, and its CSV, live or saved.

Field names change across drivers (clocks.gr became clocks.current.graphics, clocks_throttle_reasons became
clocks_event_reasons), and one name a binary does not know fails the whole query. So the plan asks the binary's
``--help-query-gpu`` for the names it accepts, takes the newest for each sample key, and the probe query drops any
the binary still refuses. Values come as "1845", "1845 MHz", "N/A", "[N/A]" or "[Not Supported]": anything that is
not a number in the field's unit is unknown, never zero. Timestamps are local time with no zone
("2026/11/21 21:15:05.467"): live, the sampling machine's zone places them; a saved file needs its zone given.
"""

from __future__ import annotations

import csv
import math
import re
from dataclasses import dataclass, field
from datetime import datetime

from . import gpu

NAME = EXECUTABLE = "nvidia-smi"

# sample key -> (how its value reads, the query names for it, newest first)
FIELDS = {
    "time": ("time", ("timestamp",)),
    "device": ("index", ("index",)),
    "name": ("text", ("name", "gpu_name")),
    "pstate": ("pstate", ("pstate",)),
    "graphicsClockMhz": ("MHz", ("clocks.current.graphics", "clocks.gr")),
    "smClockMhz": ("MHz", ("clocks.current.sm", "clocks.sm")),
    "memoryClockMhz": ("MHz", ("clocks.current.memory", "clocks.mem")),
    "powerW": ("W", ("power.draw", "power.draw.average", "power.draw.instant")),
    "powerLimitW": ("W", ("enforced.power.limit", "gpu.power.current_limit", "power.limit")),
    "utilisationPercent": ("%", ("utilization.gpu",)),
    "memoryUtilisationPercent": ("%", ("utilization.memory",)),
    "temperatureC": ("C", ("temperature.gpu",)),
    "memoryUsedMiB": ("MiB", ("memory.used",)),
    "memoryTotalMiB": ("MiB", ("memory.total",)),
    "throttleReasons": ("mask", ("clocks_event_reasons.active", "clocks_throttle_reasons.active")),
}
REQUIRED = ("time", "device")
DRIVER = ("kmd_version", "driver_version")  # asked once, by the probe
# The bits of the active clock event reasons (nvml.h), by the sample format's names.
BITS = {0x1: "idle", 0x2: "clock_setting", 0x4: "power_cap", 0x8: "hw_slowdown", 0x10: "sync_boost",
        0x20: "thermal", 0x40: "hw_thermal", 0x80: "hw_power_brake", 0x100: "display_clocks"}
# One "Active" / "Not Active" column per reason, which saved files sometimes carry instead of the bits.
FLAGS = {"gpu_idle": "idle", "applications_clocks_setting": "clock_setting", "sw_power_cap": "power_cap",
         "hw_slowdown": "hw_slowdown", "sync_boost": "sync_boost", "sw_thermal_slowdown": "thermal",
         "hw_thermal_slowdown": "hw_thermal", "hw_power_brake_slowdown": "hw_power_brake",
         "display_clock_setting": "display_clocks"}
UNITS = {"MHz": {"mhz": 1, "ghz": 1000, "khz": 0.001}, "W": {"w": 1, "mw": 0.001, "kw": 1000}, "%": {"%": 1},
         "C": {"c": 1}, "MiB": {"mib": 1, "kib": 1 / 1024, "gib": 1024}}
UNKNOWN = {"", "n/a", "not supported", "not available", "unsupported", "unknown", "unknown error",
           "insufficient permissions"}
_NUMBER = re.compile(r"^([-+]?\d+(?:\.\d+)?)\s*([A-Za-z%]*)$")
_TIME = re.compile(r"^(\d{4})/(\d{2})/(\d{2}) (\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?$")
_REFUSED = re.compile(r'Field "([^"]+)" is not a valid field to query')
_UNIT_SUFFIX = re.compile(r"\s*\[[^\]]*\]$")
_NAMES = {name: key for key, (_, names) in FIELDS.items() for name in names}


# -- values ---------------------------------------------------------------------------------------------
def reasons(bits: int) -> list[str]:
    """The clock event reasons a bitmask holds; a bit no table knows keeps its value as its name."""
    out = [name for bit, name in BITS.items() if bits & bit]
    rest = bits & ~sum(BITS)
    return out + [f"0x{1 << n:x}" for n in range(rest.bit_length()) if rest >> n & 1]


def moment(text: str, zone=None) -> int:
    """Epoch nanoseconds of an nvidia-smi timestamp, in ``zone`` (a tzinfo), or this machine's zone when None."""
    match = _TIME.match(text.strip())
    if not match:
        raise ValueError(f"not an nvidia-smi timestamp: {text.strip()!r}")
    year, month, day, hour, minute, second = (int(g) for g in match.groups()[:6])
    at = datetime(year, month, day, hour, minute, second, tzinfo=zone)
    return int(at.timestamp()) * 1_000_000_000 + int((match.group(7) or "").ljust(6, "0")) * 1000


def value(text: str, how: str):
    """One cell as the sample format holds it, or None when the tool does not know it."""
    text = text.strip()
    if text.startswith("[") or text.lower() in UNKNOWN:
        return None
    if how == "text":
        return text
    if how == "index":
        return int(text) if text.isdigit() else None
    if how == "pstate":
        return text.upper() if text.upper() in gpu.PSTATES else None
    if how == "mask":
        try:
            return reasons(int(text, 16) if text.lower().startswith("0x") else int(text))
        except ValueError:
            return None
    if how == "flag":
        return {"active": True, "not active": False}.get(text.lower())
    match = _NUMBER.match(text)
    scale = UNITS[how].get(match.group(2).lower(), None) if match and match.group(2) else 1
    if not match or scale is None:
        return None
    number = float(match.group(1)) * scale
    if not math.isfinite(number):
        return None
    return int(number) if number.is_integer() else round(number, 3)


def _sample(cells: dict[str, str], zone, reasons_from_flags=None) -> tuple[int, dict]:
    nanos = moment(cells["time"], zone)
    values = {key: value(text, FIELDS[key][0]) for key, text in cells.items() if key != "time"}
    if reasons_from_flags is not None and values.get("throttleReasons") is None:
        values["throttleReasons"] = reasons_from_flags
    device = values.get("device")
    if device is None:
        raise ValueError(f"not a device index: {cells.get('device', '').strip()!r}")
    return nanos, gpu.make(nanos, device, values)


# -- the live query -------------------------------------------------------------------------------------
@dataclass
class Plan:
    """The query fields chosen for one binary, and what its probe found."""
    executable: str
    keys: list[str] = field(default_factory=list)     # the sample keys asked for, in query order
    names: list[str] = field(default_factory=list)    # their query fields
    missing: list[str] = field(default_factory=list)  # sample keys this binary cannot give
    devices: list[dict] = field(default_factory=list)  # {device, name, driver} for each device the probe saw

    def describe(self) -> dict:
        return {"tool": NAME, "executable": self.executable, "fields": dict(zip(self.keys, self.names)),
                "missing": self.missing}


def advertised(help_text: str) -> set[str]:
    """The field names a binary's ``--help-query-gpu`` lists (each entry's line starts with its quoted names)."""
    return {name for line in help_text.splitlines() if line.startswith('"') for name in re.findall(r'"([^"]+)"', line)}


def _tail(text: str) -> str:
    return " | ".join(line.strip() for line in text.strip().splitlines()[-3:] if line.strip()) or "no output"


def plan(executable: str, run, device: int | None = None) -> Plan:
    """Chooses the fields to query and probes them once. ``run(args)`` returns (exit code, output). RuntimeError:
    the tool cannot be used; LookupError: it has no device ``device``."""
    code, text = run([executable, "--help-query-gpu"])
    known = advertised(text) if code == 0 else set()
    # With a help to go by, only names it lists; without one, every name in turn, as refusals tell.
    candidates = {key: [n for n in names if n in known] if known else list(names) for key, (_, names) in FIELDS.items()}
    while True:
        keys = [key for key in FIELDS if candidates[key]]
        if any(key not in keys for key in REQUIRED):
            raise RuntimeError(f"{executable} cannot report " + " and ".join(FIELDS[k][1][0] for k in REQUIRED
                                                                              if k not in keys))
        names = [candidates[key][0] for key in keys]
        code, text = run([executable, f"--query-gpu={','.join(names)}", "--format=csv,noheader,nounits"])
        refused = _REFUSED.search(text) if code != 0 else None
        if refused and refused.group(1) in names:
            candidates[keys[names.index(refused.group(1))]].pop(0)
            continue
        if code != 0:
            raise RuntimeError(f"{executable} exited {code}: {_tail(text)}")
        break
    found = Plan(executable, keys, names, [key for key in FIELDS if not candidates[key]])
    for line in text.splitlines():
        if line.strip():
            try:
                _, sample = parse(found, line)
            except ValueError:
                continue
            found.devices.append({"device": sample["device"], "name": sample.get("name"), "driver": None})
    if not found.devices:
        raise RuntimeError(f"{executable} lists no device it can query: {_tail(text)}")
    if device is not None and device not in [d["device"] for d in found.devices]:
        raise LookupError(f"no device {device}: {executable} lists "
                          + ", ".join(f"{d['device']} ({d['name']})" for d in found.devices))
    driver = next((name for name in DRIVER if name in known), None) if known else DRIVER[-1]
    if driver:
        code, text = run([executable, f"--query-gpu=index,{driver}", "--format=csv,noheader,nounits"])
        versions = dict(tuple(c.strip() for c in line.split(",", 1)) for line in text.splitlines()
                        if code == 0 and line.count(",") == 1)
        for entry in found.devices:
            entry["driver"] = versions.get(str(entry["device"])) or None
    return found


def command(found: Plan, period_ms: int, device: int | None = None) -> list[str]:
    """The looping query: one line per device every ``period_ms``."""
    args = [found.executable, f"--query-gpu={','.join(found.names)}", "--format=csv,noheader,nounits",
            "-lms", str(int(period_ms))]
    return args + ([f"--id={device}"] if device is not None else [])


def parse(found: Plan, line: str, zone=None) -> tuple[int, dict]:
    """One line of the looping query as (epoch nanoseconds, sample); ValueError when it is not one."""
    cells = next(csv.reader([line]), [])
    if len(cells) != len(found.keys):
        raise ValueError(f"{len(cells)} fields where the query asks for {len(found.keys)}")
    return _sample(dict(zip(found.keys, cells)), zone)


# -- saved files ----------------------------------------------------------------------------------------
def _name(cell: str) -> str:
    """A header cell's field name: "clocks.current.sm [MHz]" is clocks.current.sm."""
    return _UNIT_SUFFIX.sub("", cell.strip())


def _flag(name: str) -> str | None:
    head, _, reason = name.partition(".")
    return FLAGS.get(reason) if head in ("clocks_event_reasons", "clocks_throttle_reasons") else None


def _header(text: str, columns) -> list[str]:
    if columns:
        return [_name(c) for c in columns]
    first = next((line for line in text.splitlines() if line.strip()), "")
    return [_name(c) for c in next(csv.reader([first]), [])]


def recognises(text: str, columns=()) -> bool:
    """Whether a saved file is nvidia-smi's CSV: its header (or the columns given) names a timestamp and more."""
    names = _header(text, columns)
    return "timestamp" in names and any(n in _NAMES or _flag(n) for n in names if n != "timestamp")


def read_csv(text: str, columns=(), zone=None) -> tuple[list[tuple[int, dict]], list[str]]:
    """Samples from a saved ``--query-gpu`` CSV, with or without units, and notes on what was assumed or left out.
    A file without a header line needs ``columns``, its query fields in order. ValueError when nothing is a sample."""
    header = _header(text, columns)
    if "timestamp" not in header:
        raise ValueError("the CSV names no timestamp field: a file without a header line needs columns")
    chosen: dict[str, int] = {}
    for index, name in enumerate(header):
        key = _NAMES.get(name)
        if key and (key not in chosen or FIELDS[key][1].index(name) < FIELDS[key][1].index(header[chosen[key]])):
            chosen[key] = index
    flags = [(index, _flag(name)) for index, name in enumerate(header) if _flag(name)]
    ignored = [name for index, name in enumerate(header) if index not in chosen.values() and not _flag(name)]
    lines = [line for line in text.splitlines() if line.strip()]
    rows = list(csv.reader(lines))
    pairs, bad, first, names = [], 0, None, {}
    for number, row in enumerate(rows, 1):
        if [_name(c) for c in row] == header:
            continue  # the header, or a header again where two files were joined
        try:
            if len(row) != len(header):
                raise ValueError(f"{len(row)} fields where the header names {len(header)}")
            cells = {key: row[index] for key, index in chosen.items()}
            if "device" not in cells:  # no index column: devices by name, in the order they first appear
                named = row[chosen["name"]].strip() if "name" in chosen else ""
                cells["device"] = str(names.setdefault(named, len(names)))
            states = [value(row[index], "flag") for index, _ in flags]
            from_flags = [reason for (_, reason), on in zip(flags, states) if on] \
                if flags and None not in states else None
            pairs.append(_sample(cells, zone, from_flags))
        except ValueError as error:
            bad += 1
            first = first or f"line {number}: {error}"
    if not pairs:
        raise ValueError(f"no samples in the CSV ({first})" if first else "no samples in the CSV")
    notes = []
    if bad:
        notes.append(f"{bad} line{'s are' if bad != 1 else ' is'} not a sample, left out ({first})")
    if "device" not in chosen:
        notes.append("the CSV has no index column: devices are told apart by name" if len(names) > 1 else
                     "the CSV has no index column: its rows are taken as device 0")
    if flags and "throttleReasons" not in chosen and len(flags) < len(FLAGS):
        notes.append("throttle reasons come from flag columns for " + ", ".join(r for _, r in flags) + " only")
    if ignored:
        notes.append(f"columns not read: {', '.join(ignored)}")
    return pairs, notes
