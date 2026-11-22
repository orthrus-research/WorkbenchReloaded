"""Evidence records, and the precondition a comparison of runs must meet before it counts as evidence.

Two sets of runs are evidence about a treatment only when they differ in that treatment alone: the same command at
the same version, the same inputs, the same toolchain and machine, the same declared facts (JVM flags, world seed,
view distances). A record keeps the facts that define one finished run as a flat map of named fields, and ``compare``
refuses a comparison in which a control field differs or a treatment field does not.

A record, ``evidence.json``:

    schema           "wb/evidence/v1"
    job, command     the run; ``cache_from`` names the job that measured it when its result came from the cache
    outcome, started, settled
    defaults         whether params.* include the defaults the command ran with
    fields           name -> value, sorted:
                       command, command.version, module.version
                       params.<name>    what the run was given, objects flattened with dots; path parameters are
                                        left to inputs.* and to ``paths``
                       inputs.<name>    an input by content ("sha256:<hex>"), a checkout by commit ("git:<commit>")
                       toolchain.<key>, machine.<key>   the envelope's toolchain and machine facts
                       data.<name>      the fields of the run's result data that were asked for
                       and what the caller declares: facts by value, files and folders by digest ("sha256:<hex>")
    declared         the fields the caller declared rather than Workbench observed
    files            each declared file or folder: its path, digest, bytes, file count and the copy kept beside the
                     record (``kept``, relative to it; null when it was too large to keep)
    paths            the run's path parameters as given, for the reader
    measurements, outputs   what the run measured and wrote; never compared

Fields compare exactly unless a kind says otherwise. Kinds are entries by field name or glob, first
``[modules.runs] kinds`` and then the ``[kinds]`` table of a comparison's kinds file; the last entry matching a field
wins, and a nested table is a dotted name:

    [kinds]
    "toolchain.*" = "ignored"                 # never compared
    "toolchain.jdk" = "exact"                 # the default, taken back here for one field
    "data.heap_mb" = { tolerance = 64 }       # numbers within 64 of each other match
    "data.warmup_s" = { tolerance = "5%" }    # within 5% of the larger magnitude
"""

from __future__ import annotations

import fnmatch
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path

from wb_core import types

SCHEMA = "wb/evidence/v1"
RECORD = "evidence.json"
MEASURED = ("ok", "finding")  # the outcomes of a run that measured something
NAME = re.compile(r"^\w[\w.\-]*$")  # a declared field: no glob characters, so patterns can name it
_MISSING = object()  # a field a record lacks: equal to no value, null included
_PERCENT = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*%\s*$")
_SPECS = {"integer": "int", "number": "float", "boolean": "bool"}


class EvidenceError(ValueError):
    """A record, a declaration or a field kind that cannot be used; the message says which and why."""


def _flat(mapping: dict, prefix: str = ""):
    for key, value in mapping.items():
        name = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict) and value:
            yield from _flat(value, name)
        else:
            yield name, value


def flatten(mapping: dict, prefix: str = "") -> dict:
    """Nested objects as dotted names (``{"jvm": {"flags": x}}`` is ``jvm.flags``); lists and scalars are values."""
    return dict(_flat(mapping, prefix))


def _plain(value):
    """The one form equal values share: an integral float is its integer (12.0 is 12, while true is not 1)."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    return value


def canonical(value) -> str:
    return json.dumps(_plain(value), sort_keys=True, separators=(",", ":"), default=str)


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


# -- field kinds ----------------------------------------------------------------------------------
@dataclass(frozen=True)
class Kind:
    """How a field compares: ``exact``, ``ignored``, or ``tolerance``: numbers within ``tolerance`` of each other,
    or within that percentage of the larger magnitude when ``relative``. Anything else under a tolerance is exact."""
    name: str = "exact"
    tolerance: float = 0.0
    relative: bool = False

    def __str__(self) -> str:
        if self.name != "tolerance":
            return self.name
        return f"within {self.tolerance:g}%" if self.relative else f"within {self.tolerance:g}"

    def same(self, a, b) -> bool:
        if a is _MISSING or b is _MISSING:
            return a is b
        if self.name == "tolerance" and _number(a) and _number(b):
            limit = self.tolerance / 100 * max(abs(a), abs(b)) if self.relative else self.tolerance
            gap = abs(a - b)
            return gap <= limit or math.isclose(gap, limit, rel_tol=1e-9, abs_tol=1e-12)
        return canonical(a) == canonical(b)


EXACT, IGNORED = Kind("exact"), Kind("ignored")


def parse_kind(name: str, spec) -> Kind:
    if spec == "exact":
        return EXACT
    if spec == "ignored":
        return IGNORED
    if isinstance(spec, dict) and set(spec) == {"tolerance"}:
        value = spec["tolerance"]
        if _number(value) and value >= 0:
            return Kind("tolerance", float(value))
        match = _PERCENT.match(value) if isinstance(value, str) else None
        if match:
            return Kind("tolerance", float(match.group(1)), relative=True)
        raise EvidenceError(f'{name}: a tolerance is a number of at least 0 or a percentage such as "2%", '
                            f"not {value!r}")
    raise EvidenceError(f'{name}: a kind is "exact", "ignored", {{ tolerance = 0.5 }} or {{ tolerance = "2%" }}, '
                        f"not {spec!r}")


def kind_entries(table, prefix: str = "") -> list[tuple[str, Kind]]:
    """A kinds table as (pattern, kind) entries in order. A nested table is a dotted name, so an unquoted
    ``machine.gpu = "ignored"`` says what the quoted ``"machine.gpu" = "ignored"`` says."""
    if not isinstance(table, dict):
        raise EvidenceError(f"{prefix or 'kinds'}: expected a table of field names or globs")
    entries = []
    for key, spec in table.items():
        name = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(spec, dict) and "tolerance" not in spec:
            entries += kind_entries(spec, name)
        else:
            entries.append((name, parse_kind(name, spec)))
    return entries


class Kinds:
    """Field kinds by pattern: the last entry matching a field wins, and a field no entry matches is exact."""

    def __init__(self, entries=()):
        self.entries = list(entries)

    def of(self, name: str) -> Kind:
        kind = EXACT
        for pattern, entry in self.entries:
            if fnmatch.fnmatchcase(name, pattern):
                kind = entry
        return kind


def controlled(name: str, patterns) -> bool:
    """Whether control ``patterns`` name a field: globs, where ``!glob`` takes fields back out and the last match wins."""
    chosen = False
    for pattern in patterns:
        negated = pattern.startswith("!")
        if fnmatch.fnmatchcase(name, pattern[1:] if negated else pattern):
            chosen = not negated
    return chosen


# -- records --------------------------------------------------------------------------------------
def _typed(schema: dict, value):
    """A submitted value as its command reads it: the CLI sends numbers and booleans as text, durations as 5m."""
    spec = {"type": "duration"} if schema.get("format") == "duration" else \
        {"type": _SPECS.get(schema.get("type"), "any")}
    try:
        return types.coerce("value", spec, value)
    except types.ValueError_:
        return value


def declared_names(fields: dict | None, files: dict | None) -> list[str]:
    """The field names a caller declares, checked: usable in patterns, and each declared once."""
    names = [name for name, _ in _flat(fields or {})] + list(files or {})
    for name in names:
        if not NAME.match(name):
            raise EvidenceError(f"declared field {name!r}: use letters, digits, _, - and dots")
    twice = sorted({name for name in names if names.count(name) > 1})
    if twice:
        raise EvidenceError(f"declared more than once (as facts, files or both): {', '.join(twice)}")
    return names


def require_measured(envelope: dict):
    """Refuses a run that has not settled, or settled without measuring anything (usage, environment)."""
    job = envelope.get("job")
    if envelope.get("state") != "settled":
        raise EvidenceError(f"{job} has not settled ({envelope.get('state')}): a record is of a finished run")
    if envelope.get("outcome") not in MEASURED:
        raise EvidenceError(f"{job} settled {envelope.get('outcome')}: it measured nothing")


def build(envelope: dict, schema: dict | None = None, *, declared: dict | None = None, files: dict | None = None,
          data=()) -> dict:
    """The record of a settled run, from its envelope.

    ``schema`` is the params schema of the run's command (``GET /v1/commands/NAME``), for the defaults the run had;
    without it params are recorded as submitted. ``declared`` holds facts the caller declares, ``files`` the declared
    files' digests (name -> {"sha256", ...}) and ``data`` globs of the run's result-data fields to keep."""
    require_measured(envelope)
    job = envelope.get("job")
    properties = (schema or {}).get("properties") or {}
    submitted = envelope.get("params") or {}
    inputs = {item["name"]: item for item in envelope.get("inputs") or []}
    values = {}
    for name, prop in properties.items():
        if submitted.get(name) is not None:
            values[name] = _typed(prop, submitted[name])
        elif prop.get("default") is not None:
            values[name] = _typed(prop, prop["default"])
    values.update({k: v for k, v in submitted.items() if k not in properties and v is not None})
    # A path is where an input was, not what it held: inputs.* carries its digest, and paths keeps it for the reader.
    paths = {name: values.pop(name) for name in list(values)
             if name in inputs or (properties.get(name) or {}).get("format") in ("path.in", "path.out")}
    fields = {"command": envelope.get("command"), "command.version": envelope.get("command_version"),
              "module.version": envelope.get("module_version")}
    fields.update(flatten(values, "params"))
    for name, item in inputs.items():
        fields[f"inputs.{name}"] = (f"sha256:{item['sha256']}" if item.get("sha256")
                                    else f"git:{item['head']}" if item.get("head") else None)
    fields.update(flatten(envelope.get("toolchain") or {}, "toolchain"))
    fields.update(flatten(envelope.get("machine") or {}, "machine"))
    result = flatten(envelope.get("data") or {})
    for pattern in data:
        chosen = {name: value for name, value in result.items() if fnmatch.fnmatchcase(name, pattern)}
        if not chosen:
            raise EvidenceError(f"data {pattern} names no field of {job}'s result data "
                                f"(it has {', '.join(sorted(result)) or 'none'})")
        fields.update({f"data.{name}": value for name, value in chosen.items()})
    names = declared_names(declared, files)
    for name, value in [*flatten(declared or {}).items(),
                        *((name, "sha256:" + info["sha256"]) for name, info in (files or {}).items())]:
        if name in fields:
            raise EvidenceError(f"declared field {name} is one the run's envelope records already")
        fields[name] = value
    timings, cache = envelope.get("timings") or {}, envelope.get("cache") or {}
    return {"schema": SCHEMA, "job": job, "command": envelope.get("command"), "outcome": envelope.get("outcome"),
            "started": timings.get("started"), "settled": timings.get("settled"),
            "cache_from": cache.get("from") if cache.get("hit") else None, "defaults": bool(properties),
            "fields": dict(sorted(fields.items())), "declared": sorted(names), "files": dict(files or {}),
            "paths": paths, "measurements": envelope.get("measurements") or [],
            "outputs": [{key: item.get(key) for key in ("name", "sha256", "bytes", "host_path")}
                        for item in envelope.get("outputs") or []]}


def check_record(record, where: str) -> dict:
    """A record read back, refused when it is no record or the record of a run that measured nothing."""
    if not isinstance(record, dict) or record.get("schema") != SCHEMA:
        raise EvidenceError(f"{where} is not an evidence record ({SCHEMA})")
    if not record.get("job") or not isinstance(record.get("fields"), dict):
        raise EvidenceError(f"{where} names no job or holds no fields")
    if record.get("outcome") not in MEASURED:
        raise EvidenceError(f"{where} is the record of a run that settled {record.get('outcome')}: "
                            "it measured nothing")
    return record


def write(record: dict, folder: Path) -> Path:
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / RECORD
    path.write_text(json.dumps(record, indent=1, default=str) + "\n", encoding="utf-8")
    return path


def _natural(path: Path):
    return [int(token) if token.isdigit() else token for token in re.split(r"(\d+)", path.parent.name)]


def find(path: Path) -> list[Path]:
    """The records at ``path``: a record file, a folder holding evidence.json, or a folder of such folders, in
    natural order (2 before 10)."""
    path = Path(path)
    if path.is_file():
        return [path]
    if (path / RECORD).is_file():
        return [path / RECORD]
    return sorted((p for p in path.glob(f"*/{RECORD}") if p.is_file()), key=_natural)


# -- the precondition -----------------------------------------------------------------------------
def _show(value) -> str:
    if value is _MISSING:
        return "absent"
    text = json.dumps(value, default=str)
    return text if len(text) <= 60 else text[:57] + "..."


def _span(labels: list[str]) -> str:
    """Run labels with consecutive runs of a side joined: A1, A2, A3, B2 is A1-A3, B2."""
    groups: list[list] = []
    for label in labels:
        side, number = label[0], int(label[1:])
        if groups and groups[-1][0] == side and groups[-1][2] == number - 1:
            groups[-1][2] = number
        else:
            groups.append([side, number, number])
    return ", ".join(f"{s}{a}" if a == b else f"{s}{a}-{s}{b}" for s, a, b in groups)


def _groups(values: list[tuple[str, object]]) -> list[dict]:
    """A field's values across runs, one entry per distinct value, in order of first appearance."""
    groups: dict[str, dict] = {}
    for label, value in values:
        key = "\0absent" if value is _MISSING else canonical(value)
        if key not in groups:
            groups[key] = {"missing": True, "runs": []} if value is _MISSING else {"value": value, "runs": []}
        groups[key]["runs"].append(label)
    return list(groups.values())


def _text(groups: list[dict]) -> str:
    return "; ".join(f"{_show(_MISSING if g.get('missing') else g['value'])} on {_span(g['runs'])}" for g in groups)


def _names(names: list[str], most: int = 5) -> str:
    return ", ".join(names[:most]) + (f" and {len(names) - most} more" if len(names) > most else "")


def compare(a: list[dict], b: list[dict], treatment=(), control=("*",), kinds: Kinds | None = None) -> dict:
    """Whether runs ``a`` and ``b`` (records) differ in the treatment alone.

    Every field a ``treatment`` pattern names must hold one value within each side, and each pattern must name a
    field whose value differs between the sides (compared exactly, or by its tolerance): a name, that field; a glob,
    one of its fields at least. Every other field the ``control`` patterns name must hold, in every run, the value it
    has in the first run of A, as its kind compares. A run given twice, or a cached result beside the run it came
    from, is one run counted twice.

    Returns ``refused`` and the ``refusals`` in words, the treatment's first; every field whose values differ, with
    its ``role`` (treatment, control, ignored, or uncontrolled when no control pattern names it), kind and values by
    run; the ``treatment`` values of each side; how many ``controls`` were checked; and the ``runs`` by label."""
    if not a or not b:
        raise EvidenceError("a comparison needs at least one run on each side")
    kinds = kinds or Kinds()
    runs = [(f"A{i}", record) for i, record in enumerate(a, 1)] + [(f"B{i}", record) for i, record in enumerate(b, 1)]
    refusals, differences, treated = [], [], {}
    seen: dict[str, str] = {}
    for label, record in runs:
        identity = record.get("cache_from") or record["job"]
        if identity in seen:
            refusals.append(f"{label} is the run {seen[identity]} already counts ({identity}): a run counts once")
        else:
            seen[identity] = label
    names = sorted({name for _, record in runs for name in record["fields"]})
    values = {name: [(label, record["fields"].get(name, _MISSING)) for label, record in runs] for name in names}
    # The treatment: one value within each side, and a value that differs between the sides.
    states: dict[str, tuple[Kind, bool, bool]] = {}  # name -> (kind, varies within a side, differs between them)
    for name in names:
        if not any(fnmatch.fnmatchcase(name, pattern) for pattern in treatment):
            continue
        kind = kinds.of(name)
        kind = kind if kind.name == "tolerance" else EXACT  # a declared treatment is compared even where ignored
        sides = {"a": values[name][:len(a)], "b": values[name][len(a):]}
        varies = False
        for side, items in sides.items():
            if not all(kind.same(items[0][1], value) for _, value in items):
                refusals.append(f"treatment {name} varies within {side.upper()}: {_text(_groups(items))}")
                varies = True
        differs = not varies and not kind.same(sides["a"][0][1], sides["b"][0][1])
        if differs:
            treated[name] = {side: items[0][1] for side, items in sides.items() if items[0][1] is not _MISSING}
        states[name] = (kind, varies, differs)
    unmoved = []
    for pattern in treatment:
        named = [name for name in states if fnmatch.fnmatchcase(name, pattern)]
        if not named:
            refusals.append(f"treatment {pattern} names no field of these runs")
        elif not any(states[name][1] or states[name][2] for name in named):
            unmoved.append(pattern)
            same = _show(values[pattern][0][1]) if named == [pattern] else f"none of {_names(named)} differs"
            refusals.append(f"treatment {pattern} is the same on both sides: {same}")
    controls = 0
    for name in names:
        items = values[name]
        if name in states:
            kind, varies, differs = states[name]
            role = "treatment"
            refused = varies or (not differs and any(fnmatch.fnmatchcase(name, p) for p in unmoved))
        else:
            kind = kinds.of(name)
            if kind.name == "ignored":
                role, refused = "ignored", False
            elif controlled(name, control):
                role, controls = "control", controls + 1
                refused = not all(kind.same(items[0][1], value) for _, value in items)
                if refused:
                    refusals.append(f"{name} differs: {_text(_groups(items))}")
            else:
                role, refused = "uncontrolled", False
        if not all(EXACT.same(items[0][1], value) for _, value in items):
            differences.append({"field": name, "role": role, "kind": str(kind), "refused": refused,
                                "values": _groups(items)})
    return {"refused": bool(refusals), "refusals": refusals, "treatment": treated, "controls": controls,
            "differences": differences, "runs": {label: record["job"] for label, record in runs}}
