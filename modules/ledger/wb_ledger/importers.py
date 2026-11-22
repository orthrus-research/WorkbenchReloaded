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
"""

from __future__ import annotations

import datetime
import json
import math
import re
from collections import Counter
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
