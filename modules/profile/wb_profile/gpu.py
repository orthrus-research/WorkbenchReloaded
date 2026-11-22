"""The GPU sample format, ``wb.profile.gpu-samples/1``: JSON lines, one device at one instant on each line.

    {"time": "2026-11-21T20:15:05.467Z", "device": 0, "name": "Example GPU", "pstate": "P0",
     "graphicsClockMhz": 1845, "smClockMhz": 1845, "memoryClockMhz": 7500, "powerW": 168.2, "powerLimitW": 170,
     "utilisationPercent": 98, "memoryUtilisationPercent": 41, "temperatureC": 71, "memoryUsedMiB": 5120,
     "memoryTotalMiB": 12288, "throttleReasons": ["power_cap"]}

``time`` (ISO-8601 with a zone) and ``device`` (the tool's index) are always there. Every other key may be missing
or null, which means unknown: never zero, and for ``throttleReasons`` never "nothing holds the clocks down" (that is
``[]``). The samples are whole-device: every process on the device counts, the desktop included.

Vendors' tools are adapters (``nvidia``: nvidia-smi) that write this format, live or from a saved file; the sampler
and the report only ever read samples. A new tool is one more module listed in ``adapters()``.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass

from .windows import iso, timestamp

SCHEMA = "wb.profile.gpu-samples/1"


@dataclass(frozen=True)
class Metric:
    key: str    # the sample's key
    stem: str   # its expectations are min_<stem> and max_<stem>
    unit: str
    what: str


METRICS = (
    Metric("graphicsClockMhz", "graphics_clock_mhz", "MHz", "graphics clock"),
    Metric("smClockMhz", "sm_clock_mhz", "MHz", "SM clock"),
    Metric("memoryClockMhz", "memory_clock_mhz", "MHz", "memory clock"),
    Metric("powerW", "power_w", "W", "power draw"),
    Metric("powerLimitW", "power_limit_w", "W", "power limit"),
    Metric("utilisationPercent", "utilisation_percent", "%", "utilisation"),
    Metric("memoryUtilisationPercent", "memory_utilisation_percent", "%", "memory utilisation"),
    Metric("temperatureC", "temperature_c", "°C", "temperature"),
    Metric("memoryUsedMiB", "memory_used_mib", "MiB", "memory used"),
    Metric("memoryTotalMiB", "memory_total_mib", "MiB", "memory size"),
)
BY_KEY = {m.key: m for m in METRICS}
BY_STEM = {m.stem: m for m in METRICS}
PSTATES = tuple(f"P{n}" for n in range(16))  # P0 is the highest performance state

# What can hold a device's clocks down, by kind: idle is no limit (there is nothing to run); a setting was chosen;
# a throttle holds the clocks below what the work asks for, and makes throttling episodes. A name an adapter
# cannot map (a bit no table knows) is of kind "unknown".
REASONS = {
    "idle": "idle",
    "clock_setting": "setting",    # application or locked clocks
    "display_clocks": "setting",   # the display clock setting
    "sync_boost": "setting",       # held to the slowest device of a sync-boost group
    "power_cap": "throttle",       # the power limit
    "thermal": "throttle",         # software thermal capping
    "hw_slowdown": "throttle",     # hardware slowdown: clocks halved or more
    "hw_thermal": "throttle",      # hardware slowdown for temperature
    "hw_power_brake": "throttle",  # hardware slowdown for an external power brake (the power supply)
}


def kind(reason: str) -> str:
    return REASONS.get(reason, "unknown")


def adapters() -> dict:
    """The vendors' tools, by the name the ``tool`` parameter gives them."""
    from . import nvidia  # adapters import this module for the format
    return {nvidia.NAME: nvidia}


def stamp(nanos: int) -> str:
    """A sample's time: ISO-8601 UTC to the millisecond."""
    return iso(nanos)[:23] + "Z"


def make(nanos: int, device: int, values: dict) -> dict:
    """A sample in the format, in a fixed key order, with unknown values left out."""
    out = {"time": stamp(nanos), "device": device}
    if values.get("name"):
        out["name"] = values["name"]
    if values.get("pstate") is not None:
        out["pstate"] = values["pstate"]
    for metric in METRICS:
        if values.get(metric.key) is not None:
            out[metric.key] = values[metric.key]
    if values.get("throttleReasons") is not None:
        out["throttleReasons"] = list(values["throttleReasons"])
    return out


def dumps(sample: dict) -> str:
    return json.dumps(sample, separators=(",", ":"), ensure_ascii=False)


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def check(record) -> tuple[int, dict]:
    """A line's record as (epoch nanoseconds, sample); ValueError when it is not a sample."""
    if not isinstance(record, dict):
        raise ValueError("not an object")
    nanos = timestamp(record.get("time"))
    device = record.get("device")
    if not isinstance(device, int) or isinstance(device, bool) or device < 0:
        raise ValueError(f"device is not an index: {device!r}")
    values = {}
    name = record.get("name")
    if name is not None and not isinstance(name, str):
        raise ValueError("name is not text")
    values["name"] = name
    pstate = record.get("pstate")
    if pstate is not None and pstate not in PSTATES:
        raise ValueError(f"pstate is not P0 to P15: {pstate!r}")
    values["pstate"] = pstate
    for metric in METRICS:
        value = record.get(metric.key)
        if value is not None and not _number(value):
            raise ValueError(f"{metric.key} is not a number: {value!r}")
        values[metric.key] = value
    reasons = record.get("throttleReasons")
    if reasons is not None and (not isinstance(reasons, list) or not all(isinstance(r, str) for r in reasons)):
        raise ValueError("throttleReasons is not a list of names")
    values["throttleReasons"] = reasons
    return nanos, make(nanos, device, values)


def read(text: str) -> tuple[list[tuple[int, dict]], list[str]]:
    """Samples from JSON lines as (epoch nanoseconds, sample), and notes on lines left out (a line cut short by a
    stopped sampler, say). ValueError when no line is a sample."""
    pairs, bad, first = [], 0, None
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            pairs.append(check(json.loads(line)))
        except ValueError as error:
            bad += 1
            first = first or f"line {number}: {error}"
    if not pairs:
        raise ValueError(f"no GPU samples ({first})" if first else "no GPU samples: the file is empty")
    notes = [f"{bad} line{'s are' if bad != 1 else ' is'} not a sample, left out ({first})"] if bad else []
    return pairs, notes


def load(text: str, columns=(), zone=None) -> tuple[list[tuple[int, dict]], list[str], str]:
    """Samples from a file: this format's JSON lines, or a CSV an adapter recognises (``columns`` names the fields
    of one without a header line, ``zone`` places its times). Returns the samples, notes, and the format read.
    ValueError when the file is neither."""
    body = text.lstrip("﻿ \t\r\n")
    if body.startswith("{"):
        return (*read(body), SCHEMA)
    for name, adapter in adapters().items():
        if adapter.recognises(body, columns):
            return (*adapter.read_csv(body, columns, zone), name)
    raise ValueError("neither GPU samples (JSON lines) nor the CSV of a GPU tool (with a header line, or give "
                     "columns)")
