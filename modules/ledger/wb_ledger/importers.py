"""What the ledger imports from files, read into runs (``store.Record``) for ``store.record_run``: Workbench's CI
run records, result files, and logs through the parsers config declares. A file that is not what it should be is
refused with one sentence saying why (``Unreadable``); the importer reports it as a finding.

CI run records (``ledger.import-ci``): a folder of runs, each a folder holding the ``summary.json`` the actions
engine writes when a run ends. A run becomes the ledger run ``ci/<project>/<run id>``: the trees it tested are its
inputs (a repository's name, its tree), and its measurements, from source ``ci``, are each job's ``job_seconds``
(part: the job; the sum of its steps' seconds, for a job that ran steps), ``job_passed`` (1 passed, 0 failed) or
``job_skipped`` (1), and the run's ``run_seconds``. Its exit is 1 when a job failed, else 0.

A result file (``ledger.import-results``) is a JSON object of this shape; ``schema`` and ``label`` are required,
the rest optional::

    {
      "schema": "wb/ledger-result/v1",
      "label": "lab/session-01",
      "fields": {"date": "2026-11-20", "card": "RTX 3060", "exit": 0},
      "inputs": [{"name": "mod.jar", "value": "1.4.2", "sha256": "<64 hex digits>"}],
      "measurements": [{"metric": "frame_ms", "value": 6.9, "unit": "ms", "part": "1920x1080"}],
      "jobs": ["<a Workbench job id>"],
      "findings": [{"id": "steady-frame-time", "title": "...", "statement": "...", "status": "hypothesis",
                    "runs": ["<other run labels>"], "jobs": ["<job ids>"], "evidence": "...", "date": "2026-11-20"}]
    }

``label`` names the run; a label the ledger has maps to that run. ``fields`` are ledger.add-run's run fields, the
date defaulting to the day the file was last written. ``inputs`` are what the run ran on: a name, and a value, a
SHA-256 or both. ``measurements`` are ledger.add-run's ({metric, value} and optionally unit, part, source, place,
view, sample, against); their source defaults to ``result``. ``jobs`` are the Workbench jobs the run came from.
Each finding rests on the file's run and on the runs it names by label (in the ledger, or in a file imported with
it); its status is one of established, hypothesis, refuted, superseded; its jobs' evidence is pinned.

Log parsers (``ledger.parse-log``) are config, ``[modules.ledger.parsers.<name>]``: a table of metric = regex,
each regex naming its number ``(?P<value>...)`` and optionally its unit ``(?P<unit>...)`` and part
``(?P<part>...)``. Every match in the log, line by line, is a measurement from source ``<name>``, its sample the
number of the match for that metric and part::

    [modules.ledger.parsers.gradle]
    build_seconds = 'BUILD SUCCESSFUL in (?P<value>\\d+)(?P<unit>s)'
    task_ms = '> Task (?P<part>\\S+) took (?P<value>[\\d.]+) ?(?P<unit>ms)'

JSON readers (``ledger.import-json``) are config as well, ``[modules.ledger.readers.<name>]``, for the JSON a tool
writes in a shape of its own. A reader says by dotted paths where a file's runs are and what each one's label,
date, fields, inputs and measurements are. In a path, ``*`` takes every key of an object or every item of a list,
and ``[field]`` every item of a list, each named by its ``field``. The names a measurement's wildcards take make
its part, joined by ``/``; the names ``each`` takes make an entry's ``@key``. ``@file`` is the file's name without
its suffix, and ``@folder`` the name of the folder it is in::

    [modules.ledger.readers.passes]
    each = "*"                          # optional: each entry here is a run (else the whole file is one)
    require = { schema = "passes.v2" }  # optional: what the file must hold, by path, or it is refused
    label = "passes/{@key}"             # the run's label, with paths in braces
    date = "utc"                        # optional: an ISO date or time; else the day the file was written
    fields = { card = "machine.gpu" }   # optional: ledger.add-run's run fields, by path
    inputs = { commit = "commit" }      # optional: what the run ran on, by path (64 hex digits: a SHA-256 too)
    [modules.ledger.readers.passes.measurements]
    seconds = { path = "seconds", unit = "s" }
    tests = "suites.*.tests"            # part: each suite's key
    tick_p95_ms = { path = "scenarios.[name].tick.p95", unit = "ms" }

Paths other than ``require``'s start at the entry. What a measurement's path reaches that is not a number is
passed over (true and false count 1 and 0), and a field or input that is not there is left out; a label or date
path that reaches nothing refuses the file. The measurements' source is the reader's name.
"""

from __future__ import annotations

import datetime
import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from . import store

RESULT_SCHEMA = "wb/ledger-result/v1"
RESULT_KEYS = ("schema", "label", "fields", "inputs", "measurements", "jobs", "findings")
FINDING_KEYS = ("id", "title", "statement", "status", "runs", "jobs", "evidence", "date")
FINDING_ID = re.compile(r"[a-z0-9][a-z0-9-]*")
RUN_ID = re.compile(r"(\d{4})(\d{2})(\d{2})-\d{6}(?:-\d+)?")  # the actions engine's run folders
STATUS_MEASURES = {"passed": ("job_passed", 1.0), "failed": ("job_passed", 0.0), "skipped": ("job_skipped", 1.0)}
GROUPS = ("value", "unit", "part")


class Unreadable(ValueError):
    """A file that is not what its importer reads."""


def day_of(path: Path) -> str:
    """The UTC day a file was last written."""
    return datetime.datetime.fromtimestamp(path.stat().st_mtime, datetime.timezone.utc).date().isoformat()


def _json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as error:
        raise Unreadable(f"cannot be read: {error}") from None
    except ValueError as error:
        raise Unreadable(f"is not JSON: {error}") from None


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _texts(value, where: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise Unreadable(f"{where} must be a list of text")
    return list(dict.fromkeys(value))


# ---------------------------------------------------------------------------------------------
# CI run records

def ci_run(summary: Path, project: str) -> store.Record:
    """A CI run's summary.json as the ledger run ci/<project>/<run id> (see the module docstring)."""
    data = _json(summary)
    if not isinstance(data, dict):
        raise Unreadable("summary.json is not an object")
    run_id = data.get("runId")
    if not isinstance(run_id, str) or not run_id:
        raise Unreadable("summary.json names no runId")
    jobs = data.get("jobs")
    if not isinstance(jobs, dict):
        raise Unreadable("summary.json has no jobs object")
    trees = data.get("trees") or {}
    if not isinstance(trees, dict) or not all(tree is None or isinstance(tree, str) for tree in trees.values()):
        raise Unreadable("summary.json's trees must name each repository's tree")
    rows, failed = [], False
    for name, job in jobs.items():
        status = job.get("status") if isinstance(job, dict) else None
        if status not in STATUS_MEASURES:
            raise Unreadable(f"job {name} has status {status!r}, not passed, failed or skipped")
        steps = job.get("steps") or []
        if not isinstance(steps, list) or not all(isinstance(s, dict) and _number(s.get("seconds", 0)) for s in steps):
            raise Unreadable(f"job {name}'s steps must each give their seconds")
        if steps:
            seconds = round(sum(float(step.get("seconds", 0)) for step in steps), 3)
            rows.append(("ci", None, None, None, "job_seconds", name, seconds, "s", None))
        metric, value = STATUS_MEASURES[status]
        rows.append(("ci", None, None, None, metric, name, value, None, None))
        failed = failed or status == "failed"
    if _number(data.get("seconds")):
        rows.append(("ci", None, None, None, "run_seconds", "", float(data["seconds"]), "s", None))
    named = RUN_ID.fullmatch(run_id)
    day = "-".join(named.groups()) if named else None
    fields = {"date": day if store.is_date(day) else day_of(summary), "exit": 1 if failed else 0}
    if data.get("treesChanged"):
        fields["note"] = "files changed during the run, so it vouches for no tree"
    inputs = [(repository, tree, None) for repository, tree in trees.items() if tree]
    return store.Record(label=f"ci/{project}/{run_id}", fields=fields, inputs=inputs, measurements=rows)


# ---------------------------------------------------------------------------------------------
# result files

def result_file(path: Path) -> tuple[store.Record, list[dict]]:
    """A result file's run and its findings (see the module docstring)."""
    data = _json(path)
    schema = data.get("schema") if isinstance(data, dict) else None
    if schema != RESULT_SCHEMA:
        raise Unreadable(f"is not a {RESULT_SCHEMA} file (its schema is {schema!r})")
    unknown = sorted(set(data) - set(RESULT_KEYS))
    if unknown:
        raise Unreadable(f"has unknown key(s) {', '.join(unknown)} (known: {', '.join(RESULT_KEYS)})")
    label = data.get("label")
    if not isinstance(label, str) or not label.strip():
        raise Unreadable("names no run: its label is missing or empty")
    try:
        fields = store.run_fields(data.get("fields") or {})
        inputs = store.run_inputs(data.get("inputs") or [])
        rows = store.measurement_rows(data.get("measurements") or [], source="result")
    except ValueError as error:
        raise Unreadable(str(error)) from None
    fields.setdefault("date", day_of(path))
    jobs = _texts(data.get("jobs") or [], "jobs")
    findings = data.get("findings") or []
    if not isinstance(findings, list):
        raise Unreadable("findings must be a list")
    return (store.Record(label=label, fields=fields, inputs=inputs, measurements=rows, jobs=jobs),
            [_finding(item, f"findings[{index}]") for index, item in enumerate(findings)])


def _finding(item, at: str) -> dict:
    if not isinstance(item, dict):
        raise Unreadable(f"{at} must be an object")
    unknown = sorted(set(item) - set(FINDING_KEYS))
    if unknown:
        raise Unreadable(f"{at} has unknown key(s) {', '.join(unknown)} (known: {', '.join(FINDING_KEYS)})")
    if not isinstance(item.get("id"), str) or not FINDING_ID.fullmatch(item["id"]):
        raise Unreadable(f"{at} needs an id of lower-case letters, digits and dashes")
    for key in ("title", "statement"):
        if not isinstance(item.get(key), str) or not item[key].strip():
            raise Unreadable(f"{at} ({item['id']}) needs a {key}")
    if item.get("status") not in store.STATUSES:
        raise Unreadable(f"{at} ({item['id']}) has status {item.get('status')!r}, not one of "
                         f"{', '.join(store.STATUSES)}")
    if item.get("evidence") is not None and not isinstance(item["evidence"], str):
        raise Unreadable(f"{at} ({item['id']}): evidence must be text")
    if item.get("date") is not None and not store.is_date(item["date"]):
        raise Unreadable(f"{at} ({item['id']}): date must be YYYY-MM-DD")
    return {"id": item["id"], "title": item["title"], "statement": item["statement"], "status": item["status"],
            "runs": _texts(item.get("runs") or [], f"{at}.runs"), "jobs": _texts(item.get("jobs") or [], f"{at}.jobs"),
            "evidence": item.get("evidence"), "date": item.get("date")}


# ---------------------------------------------------------------------------------------------
# logs

def parser(parsers: dict, name: str) -> dict[str, re.Pattern]:
    """The named parser of ``[modules.ledger.parsers]``, each metric's regex compiled. Raises LookupError for a name
    config does not have and ValueError for a parser config gets wrong."""
    if name not in parsers:
        raise LookupError(f"no parser {name} in [modules.ledger.parsers] (have: {', '.join(sorted(parsers)) or 'none'})")
    where = f"modules.ledger.parsers.{name}"
    table = parsers[name]
    if not isinstance(table, dict) or not table:
        raise ValueError(f"{where} must be a table of metric = regex")
    patterns = {}
    for metric, text in table.items():
        if not isinstance(text, str):
            raise ValueError(f"{where}.{metric} must be a regex (text)")
        try:
            pattern = re.compile(text)
        except re.error as error:
            raise ValueError(f"{where}.{metric} is not a regex: {error}") from None
        if "value" not in pattern.groupindex:
            raise ValueError(f"{where}.{metric} names no (?P<value>...) group")
        other = sorted(set(pattern.groupindex) - set(GROUPS))
        if other:
            raise ValueError(f"{where}.{metric} names group(s) {', '.join(other)}; a parser's groups are "
                             f"{', '.join(GROUPS)}")
        patterns[metric] = pattern
    return patterns


def parse_log(path: Path, patterns: dict[str, re.Pattern], source: str) -> tuple[list[tuple], list[str], dict]:
    """Every match of each metric's regex in the log, line by line, as a measurement row from ``source``: the n-th
    match of a metric and part is its sample n. Returns the rows, the matches whose value is not a number, and the
    rows each metric gave."""
    rows, bad, found = [], [], dict.fromkeys(patterns, 0)
    samples = Counter()
    with path.open(encoding="utf-8", errors="replace") as handle:
        for number, line in enumerate(handle, 1):
            line = line.rstrip("\r\n")
            for metric, pattern in patterns.items():
                for match in pattern.finditer(line):
                    groups = match.groupdict()
                    try:
                        value = float(groups["value"])
                    except (TypeError, ValueError):
                        value = math.nan
                    if not math.isfinite(value):
                        bad.append(f"{metric} at line {number}: {groups['value']!r} is not a number")
                        continue
                    part = groups.get("part") or ""
                    samples[metric, part] += 1
                    rows.append((source, None, None, samples[metric, part], metric, part, value, groups.get("unit"),
                                 None))
                    found[metric] += 1
    return rows, bad, found


# ---------------------------------------------------------------------------------------------
# JSON files, by a reader

READER_KEYS = ("each", "require", "label", "date", "fields", "inputs", "measurements")
MEASURE_KEYS = ("path", "unit")
NAMES = ("@key", "@file", "@folder")
PLACEHOLDER = re.compile(r"\{([^{}]+)\}")


@dataclass
class Reader:
    """A reader of ``[modules.ledger.readers]``, checked: each value a list of path steps."""
    name: str
    each: list | None
    require: dict  # path text -> (steps, expected value)
    label: str
    date: list | None
    fields: dict  # run field -> steps
    inputs: dict  # input name -> steps
    measurements: dict  # metric -> (steps, unit)


def _steps(text, where: str, wild: bool) -> list[str]:
    if not isinstance(text, str) or not text.strip():
        raise ValueError(f"{where} must be a dotted path (text)")
    steps = text.split(".")
    if any(not step for step in steps):
        raise ValueError(f"{where}: {text!r} has an empty step")
    for step in steps:
        if step in NAMES and len(steps) > 1:
            raise ValueError(f"{where}: {step} is a path of its own, not a step of {text!r}")
        if not wild and (step == "*" or step.startswith("[")):
            raise ValueError(f"{where}: {text!r} must reach one value, so it takes no * or [field]")
        if step.startswith("[") and (not step.endswith("]") or len(step) < 3):
            raise ValueError(f"{where}: {step!r} must be [field]")
    return steps


def reader(readers: dict, name: str) -> Reader:
    """The named reader of ``[modules.ledger.readers]``, checked. Raises LookupError for a name config does not
    have and ValueError for a reader config gets wrong."""
    if name not in readers:
        have = ", ".join(sorted(readers)) or "none"
        raise LookupError(f"no reader {name} in [modules.ledger.readers] (have: {have})")
    where = f"modules.ledger.readers.{name}"
    table = readers[name]
    if not isinstance(table, dict):
        raise ValueError(f"{where} must be a table")
    unknown = sorted(set(table) - set(READER_KEYS))
    if unknown:
        raise ValueError(f"{where} has unknown key(s) {', '.join(unknown)} (known: {', '.join(READER_KEYS)})")
    label = table.get("label")
    if not isinstance(label, str) or not label.strip():
        raise ValueError(f"{where}.label must name the run: text, with paths in braces")
    for path in PLACEHOLDER.findall(label):
        _steps(path, f"{where}.label", wild=False)
    each = _steps(table["each"], f"{where}.each", wild=True) if table.get("each") else None
    if each is not None and not any(step == "*" or step.startswith("[") for step in each):
        raise ValueError(f"{where}.each must take entries: a path with a * or a [field]")

    def table_of(key: str) -> dict:
        value = table.get(key) or {}
        if not isinstance(value, dict):
            raise ValueError(f"{where}.{key} must be a table")
        return value

    require = {path: (_steps(path, f"{where}.require", wild=False), value)
               for path, value in table_of("require").items()}
    fields = table_of("fields")
    wrong = sorted(set(fields) - (set(store.RUN_FIELDS) - {"date", "job"}))
    if wrong:
        raise ValueError(f"{where}.fields: {', '.join(wrong)} not a run field this sets (date has its own key; the "
                         f"rest: {', '.join(f for f in store.RUN_FIELDS if f not in ('date', 'job'))})")
    measurements = {}
    for metric, spec in table_of("measurements").items():
        at = f"{where}.measurements.{metric}"
        if isinstance(spec, dict):
            other = sorted(set(spec) - set(MEASURE_KEYS))
            if other:
                raise ValueError(f"{at} has unknown key(s) {', '.join(other)} (known: {', '.join(MEASURE_KEYS)})")
            unit = spec.get("unit")
            if unit is not None and not isinstance(unit, str):
                raise ValueError(f"{at}.unit must be text")
            measurements[metric] = (_steps(spec.get("path"), f"{at}.path", wild=True), unit)
        else:
            measurements[metric] = (_steps(spec, at, wild=True), None)
    if not measurements:
        raise ValueError(f"{where}.measurements must name at least one metric = path")
    return Reader(name=name, each=each, require=require, label=label,
                  date=_steps(table["date"], f"{where}.date", wild=False) if table.get("date") else None,
                  fields={key: _steps(path, f"{where}.fields.{key}", wild=False) for key, path in fields.items()},
                  inputs={key: _steps(path, f"{where}.inputs.{key}", wild=False)
                          for key, path in table_of("inputs").items()},
                  measurements=measurements)


def _walk(value, steps: list[str], names: tuple = ()):
    """Every (names, value) the path's steps reach from ``value``; the names are what its wildcards took."""
    if not steps:
        yield names, value
        return
    step, rest = steps[0], steps[1:]
    if step == "*":
        items = value.items() if isinstance(value, dict) else enumerate(value) if isinstance(value, list) else ()
        for key, item in items:
            yield from _walk(item, rest, names + (str(key),))
    elif step.startswith("["):
        field_name = step[1:-1]
        for index, item in enumerate(value if isinstance(value, list) else ()):
            named = item.get(field_name) if isinstance(item, dict) else None
            yield from _walk(item, rest, names + (str(named if named is not None else index),))
    elif isinstance(value, dict) and step in value:
        yield from _walk(value[step], rest, names)
    elif isinstance(value, list) and step.isdigit() and int(step) < len(value):
        yield from _walk(value[int(step)], rest, names)


def _one(entry, steps: list[str], known: dict):
    """The one value a path without wildcards reaches, or None."""
    if steps[0] in known:
        return known[steps[0]]
    found = [value for _, value in _walk(entry, steps)]
    return found[0] if found else None


def read_json(path: Path, chosen: Reader) -> list[store.Record]:
    """A JSON file's runs by a reader (see the module docstring)."""
    data = _json(path)
    for text, (steps, expected) in chosen.require.items():
        held = _one(data, steps, {})
        if held != expected:
            raise Unreadable(f"is not what reader {chosen.name} reads: its {text} is {held!r}, not {expected!r}")
    entries = list(_walk(data, chosen.each)) if chosen.each else [((), data)]
    if not entries:
        raise Unreadable(f"has no entries at {'.'.join(chosen.each)}")
    records, labels = [], set()
    for names, entry in entries:
        known = {"@key": "/".join(names), "@file": path.stem, "@folder": path.parent.name}
        where = f" (entry {known['@key']})" if chosen.each else ""

        def scalar(steps: list[str], what: str):
            value = _one(entry, steps, known)
            if value is None or isinstance(value, (dict, list)):
                raise Unreadable(f"{what}: {'.'.join(steps)} reaches no value{where}")
            return value

        label = PLACEHOLDER.sub(lambda match: str(scalar(match.group(1).split("."), "label")), chosen.label)
        if label in labels:
            raise Unreadable(f"names run {label} twice")
        labels.add(label)
        def maybe(steps: list[str], what: str):
            value = _one(entry, steps, known)
            if isinstance(value, (dict, list)):
                raise Unreadable(f"{what}: {'.'.join(steps)} reaches {type(value).__name__}, not a value{where}")
            return value

        fields = {key: maybe(steps, f"field {key}") for key, steps in chosen.fields.items()}
        if chosen.date:
            day = str(scalar(chosen.date, "date"))[:10]
            if not store.is_date(day):
                raise Unreadable(f"date: {'.'.join(chosen.date)} is not an ISO date or time{where}")
        else:
            day = day_of(path)
        inputs = []
        for key, steps in chosen.inputs.items():
            value = maybe(steps, f"input {key}")
            if value is not None:
                value = str(value)
                inputs.append({"name": key, "value": value,
                               "sha256": value if store.DIGEST.fullmatch(value) else None})
        reached = []
        for metric, (steps, unit) in chosen.measurements.items():
            for parts, value in _walk(entry, steps):
                if isinstance(value, bool):
                    value = float(value)
                if _number(value):
                    reached.append((metric, "/".join(parts), float(value), unit))
        repeated = Counter((metric, part) for metric, part, _, _ in reached)
        seen = Counter()
        rows = []
        for metric, part, value, unit in reached:  # items of one name (two scenarios called alike) are samples
            seen[metric, part] += 1
            sample = seen[metric, part] if repeated[metric, part] > 1 else None
            rows.append((chosen.name, None, None, sample, metric, part, value, unit, None))
        try:
            records.append(store.Record(label=label, fields=dict(store.run_fields(fields), date=day),
                                        inputs=store.run_inputs(inputs), measurements=rows))
        except ValueError as error:
            raise Unreadable(f"{error}{where}") from None
    return records
