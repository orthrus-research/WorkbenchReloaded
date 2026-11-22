"""The ledger's storage functions, shared with other modules (``vast`` records its requests, offers,
rentals and charges through these).

A run names what it ran on as inputs, each a name, a value and the input's SHA-256 when it is known, so the
schema names nothing of any one project: a project's jars, checkouts and versions are data of its runs.
"""

from __future__ import annotations

import datetime
import json
import math
import os
import re
import sqlite3
import statistics
import string
import time
import zlib
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

MACHINE_FIELDS = {
    "gpu": "gpu_name", "gpu_ram_mib": "gpu_ram", "gpu_mem_bw": "gpu_mem_bw", "pcie_bw": "pcie_bw", "cpu": "cpu_name",
    "cpu_cores_effective": "cpu_cores_effective", "cpu_ram_mib": "cpu_ram", "cpu_ghz": "cpu_ghz", "disk_bw": "disk_bw",
    "inet_up": "inet_up", "inet_down": "inet_down", "place": "geolocation", "host": "host_id",
}
OFFER_FIELDS = {
    "dph": "dph_total", "storage_cost": "storage_cost", "inet_up_cost": "inet_up_cost",
    "inet_down_cost": "inet_down_cost", "driver": "driver_version", "cuda": "cuda_max_good",
    "reliability": "reliability", "verified": "verified", "dlperf": "dlperf", "dph_base": "dph_base",
    "storage_hourly": "storage_total_cost", "min_bid": "min_bid", "discounted_dph": "discounted_dph_total",
    "credit_discount_max": "credit_discount_max", "rented": "rented", "duration_s": "duration", "end_date": "end_date",
    "score": "score",
}
RENTAL_FIELDS = ("offer", "label", "purpose", "created", "destroyed", "held_s", "dph_quoted", "dph_billed", "disk_gb",
                 "driver", "outcome", "failure", "to_running_s", "to_ssh_s", "upload_s", "upload_bytes", "bootstrap_s",
                 "work_s", "fetch_s", "download_bytes", "job", "quote")
RUN_FIELDS = ("date", "instance", "os", "host", "card", "driver", "options", "tour", "window", "radius", "exit",
              "evidence", "note", "job")
WHOLE_FIELDS = ("radius", "exit")
INPUT_KEYS = ("name", "value", "sha256")
# A measurement row's columns, in the order its tuples hold them.
MEASUREMENT_FIELDS = ("source", "place", "view", "sample", "metric", "part", "value", "unit", "against")
STATUSES = ("established", "hypothesis", "refuted", "superseded")
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
DIGEST = re.compile(r"(?:sha256:)?([0-9a-fA-F]{64})")


class Unknown(LookupError):
    """A run, finding, machine, rental or job the ledger (or Workbench) does not have."""


class Refused(ValueError):
    """What a record states disagrees with what the ledger holds; nothing of the record is written."""


class Median:
    def __init__(self):
        self.values = []

    def step(self, value):
        if value is not None:
            self.values.append(float(value))

    def finalize(self):
        return statistics.median(self.values) if self.values else None


def inflate(blob):
    return None if blob is None else zlib.decompress(blob).decode("utf-8", "replace")


def database_path() -> Path:
    return Path(os.environ.get("WB_STATE_DIR", "/var/lib/wb")) / "db" / "wb.sqlite"


def connect(path: Path | None = None, read_only: bool = False) -> sqlite3.Connection:
    path = Path(path or database_path())
    if read_only:
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
    else:
        # URI mode, so an imported database can be ATTACHed read-only (mode=ro).
        connection = sqlite3.connect(path.resolve().as_uri(), uri=True, timeout=30)
        connection.execute("PRAGMA busy_timeout=30000")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.create_aggregate("median", 1, Median)
    connection.create_function("inflate", 1, inflate, deterministic=True)
    return connection


def utc(moment: float) -> str:
    """A moment as call times are written: UTC, to the millisecond."""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(moment)) + f".{int(moment % 1 * 1000):03d}Z"


def now_utc() -> str:
    return utc(time.time())


def clean(value):
    return value.strip() if isinstance(value, str) else value


# ---------------------------------------------------------------------------------------------
# what a run, its inputs and its measurements must look like

def is_date(value) -> bool:
    if not isinstance(value, str) or not DATE.fullmatch(value):
        return False
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def digest_of(value) -> str | None:
    """A value that is a SHA-256 (64 hex digits) is also its input's digest, in lower case. Migration 002 moved the
    run columns of the first schema into inputs by the same rule."""
    if isinstance(value, str) and len(value) == 64 and all(c in string.hexdigits for c in value):
        return value.lower()
    return None


def run_fields(fields) -> dict:
    """A run's fields, checked: known names only (what a run ran on is an input, not a field), a YYYY-MM-DD date,
    whole numbers for radius and exit, text or numbers for the rest; unset ones left out. Raises ValueError naming
    the first problem."""
    if not isinstance(fields, dict):
        raise ValueError("fields must be an object")
    unknown = sorted(set(fields) - set(RUN_FIELDS))
    if unknown:
        raise ValueError(f"unknown run field(s) {', '.join(unknown)} (known: {', '.join(RUN_FIELDS)}); what a run "
                         "ran on goes in its inputs")
    checked = {}
    for name, value in fields.items():
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise ValueError(f"run field {name} must be text or a number")
        if name in WHOLE_FIELDS:
            if isinstance(value, float) and value.is_integer():
                value = int(value)
            if not isinstance(value, int):
                raise ValueError(f"run field {name} must be a whole number, not {value!r}")
        if name == "date" and not is_date(value):
            raise ValueError(f"run field date must be YYYY-MM-DD, not {value!r}")
        checked[name] = value
    return checked


def run_inputs(items, where: str = "inputs") -> list[tuple]:
    """A run's inputs, checked, as (name, value, sha256): each {name, value, sha256} with a value, a SHA-256 or both,
    names unique. A value is kept as text, a digest as lower-case hex (a ``sha256:`` prefix dropped). Raises
    ValueError naming the first problem."""
    if not isinstance(items, list):
        raise ValueError(f"{where} must be a list of {{name, value, sha256}}")
    checked, seen = [], set()
    for index, item in enumerate(items):
        at = f"{where}[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{at} must be an object: {{name, value, sha256}}")
        unknown = sorted(set(item) - set(INPUT_KEYS))
        if unknown:
            raise ValueError(f"{at} has unknown key(s) {', '.join(unknown)} (known: {', '.join(INPUT_KEYS)})")
        name, value, digest = (item.get(key) for key in INPUT_KEYS)
        if not isinstance(name, str) or not name or name != name.strip():
            raise ValueError(f"{at} needs a name: text without surrounding spaces")
        if name in seen:
            raise ValueError(f"{at}: input {name} is named twice")
        seen.add(name)
        if value is not None:
            if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                raise ValueError(f"{at}: the value of input {name} must be text or a number")
            value = value if isinstance(value, str) else str(value)
        if digest is not None:
            match = DIGEST.fullmatch(digest) if isinstance(digest, str) else None
            if match is None:
                raise ValueError(f"{at}: the sha256 of input {name} must be 64 hex digits")
            digest = match.group(1).lower()
        if value is None and digest is None:
            raise ValueError(f"{at}: input {name} needs a value, a sha256, or both")
        checked.append((name, value, digest))
    return checked


def measurement_rows(items, source: str, where: str = "measurements") -> list[tuple]:
    """Measurements, checked, as rows in MEASUREMENT_FIELDS order: each {metric, value, ...} of those keys, its value
    a finite number (or numeric text), its sample a whole number, the rest text; ``source`` is the source of those
    that name none. Raises ValueError naming the first problem."""
    if not isinstance(items, list):
        raise ValueError(f"{where} must be a list of {{metric, value, ...}}")
    rows = []
    for index, item in enumerate(items):
        at = f"{where}[{index}]"
        if not isinstance(item, dict) or "metric" not in item or "value" not in item:
            raise ValueError(f"{at} needs metric and value")
        unknown = sorted(set(item) - set(MEASUREMENT_FIELDS))
        if unknown:
            raise ValueError(f"{at} has unknown key(s) {', '.join(unknown)} (known: {', '.join(MEASUREMENT_FIELDS)})")
        try:
            if isinstance(item["value"], bool):
                raise ValueError
            value = float(item["value"])
        except (TypeError, ValueError):
            raise ValueError(f"{at}: value {item['value']!r} is not a number") from None
        if not math.isfinite(value):
            raise ValueError(f"{at}: value {item['value']!r} is not a finite number")
        sample = item.get("sample")
        if isinstance(sample, float) and sample.is_integer():
            sample = int(sample)
        if sample is not None and (isinstance(sample, bool) or not isinstance(sample, int)):
            raise ValueError(f"{at}: sample must be a whole number")
        text = {}
        for key in ("source", "place", "view", "metric", "part", "unit", "against"):
            if item.get(key) is not None and not isinstance(item[key], str):
                raise ValueError(f"{at}: {key} must be text")
            text[key] = item.get(key)
        if not text["metric"]:
            raise ValueError(f"{at} needs a metric")
        rows.append((text["source"] or source, text["place"], text["view"], sample, text["metric"], text["part"] or "",
                     value, text["unit"], text["against"]))
    return rows


# ---------------------------------------------------------------------------------------------
# machines, offers, calls, rentals and charges

def upsert_machine(connection, provider: str, key: str, specs: dict) -> int:
    specs = {name: clean(value) for name, value in specs.items() if name in MACHINE_FIELDS or name == "note"}
    specs = {name: value for name, value in specs.items() if value is not None}
    row = connection.execute("SELECT id FROM ledger_machine WHERE provider = ? AND key = ?", (provider, key)).fetchone()
    if row is None:
        names = ["provider", "key", *specs]
        return connection.execute(f"INSERT INTO ledger_machine ({', '.join(names)}) VALUES "
                                  f"({', '.join('?' * len(names))})", (provider, key, *specs.values())).lastrowid
    if specs:
        connection.execute(f"UPDATE ledger_machine SET {', '.join(f'{n} = ?' for n in specs)} WHERE id = ?",
                           (*specs.values(), row["id"]))
    return row["id"]


def machine_of_offer(connection, provider: str, offer: dict) -> int | None:
    if offer.get("machine_id") is None:
        return None
    return upsert_machine(connection, provider, str(offer["machine_id"]),
                          {column: offer.get(field) for column, field in MACHINE_FIELDS.items()})


def record_offer(connection, provider: str, observed: str, offer: dict, call_id: int | None = None):
    machine = machine_of_offer(connection, provider, offer)
    values = {column: clean(offer.get(field)) for column, field in OFFER_FIELDS.items()}
    for flag in ("verified", "rented"):
        if values[flag] is not None:
            values[flag] = int(bool(values[flag]))
    connection.execute(f"INSERT OR REPLACE INTO ledger_offer (observed, provider, offer, machine_id, call_id, "
                       f"{', '.join(values)}, raw) VALUES (?, ?, ?, ?, ?, {', '.join('?' * len(values))}, ?)",
                       (observed, provider, str(offer["id"]), machine, call_id, *values.values(),
                        None if call_id else json.dumps(offer)))


def record_call(connection, provider: str, method: str, endpoint: str, request, status, duration_ms: float,
                body: bytes, purpose: str | None = None, sent: str | None = None, job: str | None = None) -> int:
    """A request and its compressed response; offers it listed become observations at the time it was sent.
    The credential travels only in the request header and is never recorded."""
    sent = sent or now_utc()
    try:
        data = json.loads(body or b"null")
    except ValueError:
        data = None
    listed = data.get("offers") if isinstance(data, dict) else None
    instances = data.get("instances") if isinstance(data, dict) else None
    count = len(listed) if isinstance(listed, list) else len(instances) if isinstance(instances, list) else None
    request_text = request if isinstance(request, str) or request is None else json.dumps(request)
    with connection:
        call_id = connection.execute(
            "INSERT INTO ledger_api_call (time, provider, method, endpoint, request, status, duration_ms, bytes, "
            "result_count, purpose, response, job) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (sent, provider, method, endpoint, request_text, status, round(duration_ms, 1), len(body or b""), count,
             purpose, zlib.compress(body or b"", 9), job)).lastrowid
        if isinstance(listed, list):
            seen = set()
            for offer in listed:
                if isinstance(offer, dict) and "id" in offer and offer["id"] not in seen:
                    seen.add(offer["id"])
                    record_offer(connection, provider, sent, offer, call_id)
    return call_id


def prune_responses(connection, before: str, execute: bool) -> dict:
    """The stored response bodies of the calls sent before ``before`` (UTC, as call times are written): how many and
    their compressed bytes. ``execute`` drops them; the calls stay, and so do the offers they listed."""
    with connection:
        if execute:
            connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT count(*) AS calls, coalesce(sum(length(response)), 0) AS bytes "
                                 "FROM ledger_api_call WHERE time < ? AND response IS NOT NULL", (before,)).fetchone()
        if execute:
            connection.execute("UPDATE ledger_api_call SET response = NULL WHERE time < ? AND response IS NOT NULL",
                               (before,))
    return {"calls": row["calls"], "bytes": row["bytes"], "before": before}


def add_rental(connection, record: dict) -> int:
    provider, instance = record["provider"], str(record["instance"])
    offer = record.get("offer")
    with connection:
        machine = None
        fields = {name: record.get(name) for name in RENTAL_FIELDS}
        if isinstance(offer, dict):
            record_offer(connection, provider, record.get("observed") or record.get("created"), offer)
            machine = machine_of_offer(connection, provider, offer)
            fields["offer"] = str(offer["id"])
            fields["dph_quoted"] = fields["dph_quoted"] or offer.get("dph_total")
            fields["driver"] = fields["driver"] or offer.get("driver_version")
        row = connection.execute("SELECT id FROM ledger_rental WHERE provider = ? AND instance = ?",
                                 (provider, instance)).fetchone()
        if row is None:
            names = ["provider", "instance", "machine_id", *fields]
            return connection.execute(f"INSERT INTO ledger_rental ({', '.join(names)}) VALUES "
                                      f"({', '.join('?' * len(names))})", (provider, instance, machine,
                                                                           *fields.values())).lastrowid
        connection.execute(f"UPDATE ledger_rental SET machine_id = ?, {', '.join(f'{n} = ?' for n in fields)} "
                           "WHERE id = ?", (machine, *fields.values(), row["id"]))
        return row["id"]


def add_charges(connection, provider: str, results: list[dict]) -> int:
    count = 0
    with connection:
        for entry in results:
            source = str(entry.get("source") or "")
            if not source.startswith("instance-"):
                continue
            instance = source.removeprefix("instance-")
            day = datetime.datetime.fromtimestamp(entry["start"], datetime.timezone.utc).date().isoformat()
            for item in entry.get("items") or [{"type": entry.get("type"), "amount": entry["amount"]}]:
                connection.execute("INSERT OR REPLACE INTO ledger_charge VALUES (?, ?, ?, ?, ?, ?)",
                                   (provider, instance, day, item["type"], float(item["amount"]),
                                    item.get("description")))
                count += 1
    return count


# ---------------------------------------------------------------------------------------------
# runs and findings

def add_run(connection, label: str, fields: dict, measurements: list[tuple] = (), inputs: list[tuple] = ()) -> int:
    """Records a run, or sets the fields given of the run of that label; its measurements are added, and its inputs
    set (an input named again takes the new value). A new run's date is today unless the fields give one."""
    values = {name: fields[name] for name in RUN_FIELDS if fields.get(name) is not None}
    with connection:
        row = connection.execute("SELECT id FROM ledger_run WHERE label = ?", (label,)).fetchone()
        if row:
            run_id = row["id"]
            if values:
                connection.execute(f"UPDATE ledger_run SET {', '.join(f'{quoted(n)} = ?' for n in values)} "
                                   "WHERE id = ?", (*values.values(), run_id))
        else:
            values.setdefault("date", datetime.date.today().isoformat())
            names = ("label", *values)
            run_id = connection.execute(f"INSERT INTO ledger_run ({listed(names)}) VALUES ({marks(names)})",
                                        (label, *values.values())).lastrowid
        connection.executemany("INSERT INTO ledger_run_input (run_id, name, value, sha256) VALUES (?, ?, ?, ?) "
                               "ON CONFLICT (run_id, name) DO UPDATE SET value = excluded.value, "
                               "sha256 = excluded.sha256", [(run_id, *item) for item in inputs])
        connection.executemany(f"INSERT INTO ledger_measurement (run_id, {listed(MEASUREMENT_FIELDS)}) "
                               f"VALUES (?, {marks(MEASUREMENT_FIELDS)})", [(run_id, *row) for row in measurements])
    return run_id


def add_finding(connection, finding_id: str, title: str, statement: str, status: str, runs: list[str],
                jobs: list[str] = (), evidence: str | None = None, superseded_by: str | None = None):
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    with connection:
        connection.execute("INSERT OR REPLACE INTO ledger_finding VALUES (?, ?, ?, ?, ?, ?, ?)",
                           (finding_id, datetime.date.today().isoformat(), title, statement, status, superseded_by,
                            evidence))
        for label in runs:
            row = connection.execute("SELECT id FROM ledger_run WHERE label = ? OR id = ?", (label, label)).fetchone()
            if row is None:
                raise Unknown(f"no run {label}")
            connection.execute("INSERT OR IGNORE INTO ledger_finding_run VALUES (?, ?)", (finding_id, row["id"]))
        for job in jobs:
            connection.execute("INSERT OR IGNORE INTO ledger_finding_job VALUES (?, ?)", (finding_id, job))
            connection.execute("UPDATE artifact SET pinned = 1 WHERE job = ?", (job,))  # D26: evidence is kept


def find_run(connection, ref: str) -> sqlite3.Row | None:
    """The run of a label, else of an id; None when there is neither."""
    return connection.execute("SELECT id, label FROM ledger_run WHERE label = ? OR CAST(id AS TEXT) = ? "
                              "ORDER BY label = ? DESC LIMIT 1", (ref, ref, ref)).fetchone()


def find_ref(connection, table: str, ref: str | None) -> int | None:
    """The id of a machine named PROVIDER:KEY or a rental named PROVIDER:INSTANCE; None for none named."""
    if not ref:
        return None
    provider, colon, key = ref.partition(":")
    if not colon or not provider or not key:
        raise ValueError(f"{table} {ref!r} is not PROVIDER:{'KEY' if table == 'machine' else 'INSTANCE'}")
    column = "key" if table == "machine" else "instance"
    row = connection.execute(f"SELECT id FROM ledger_{table} WHERE provider = ? AND {column} = ?",
                             (provider, key)).fetchone()
    if row is None:
        raise Unknown(f"no {table} {ref} in the ledger")
    return row["id"]


def link_run(connection, ref: str, jobs: list[str], machine: str | None = None, rental: str | None = None) -> dict:
    """Links a run to the Workbench jobs its results came from, and sets the machine or rental it ran on (one not
    named is kept). Raises Unknown, changing nothing, for a run, machine, rental or job there is none of."""
    jobs = list(dict.fromkeys(jobs))
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        run = find_run(connection, ref)
        if run is None:
            raise Unknown(f"no run {ref}")
        machine_id, rental_id = find_ref(connection, "machine", machine), find_ref(connection, "rental", rental)
        known = {row["id"] for row in connection.execute(f"SELECT id FROM job WHERE id IN ({marks(jobs)})", jobs)
                 } if jobs else set()
        missing = [job for job in jobs if job not in known]
        if missing:
            raise Unknown(f"no job {', '.join(missing)} in this Workbench")
        linked = [job for job in jobs if keyed(connection, "ledger_run_job", {"run_id": run["id"], "job": job})
                  == "inserted"]
        if machine_id is not None or rental_id is not None:
            connection.execute("UPDATE ledger_run SET machine_id = coalesce(?, machine_id), "
                               "rental_id = coalesce(?, rental_id) WHERE id = ?", (machine_id, rental_id, run["id"]))
        row = connection.execute("SELECT machine_id, rental_id FROM ledger_run WHERE id = ?", (run["id"],)).fetchone()
        every = [r["job"] for r in connection.execute("SELECT job FROM ledger_run_job WHERE run_id = ? ORDER BY job",
                                                       (run["id"],))]
    return {"run": run["id"], "label": run["label"], "linked": linked, "jobs": every,
            "machine_id": row["machine_id"], "rental_id": row["rental_id"]}


def supersede(connection, finding_id: str, by: str) -> bool:
    """Marks a finding superseded by another; False when it already is. Raises ValueError for a finding superseding
    itself, Unknown for one there is none of, and Refused when another finding supersedes it already or it
    supersedes the other, directly or through others."""
    if finding_id == by:
        raise ValueError(f"finding {finding_id} cannot supersede itself")
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        rows = {row["id"]: row for row in connection.execute(
            "SELECT id, status, superseded_by FROM ledger_finding WHERE id IN (?, ?)", (finding_id, by))}
        for name in (finding_id, by):
            if name not in rows:
                raise Unknown(f"no finding {name}")
        held = rows[finding_id]["superseded_by"]
        if held == by and rows[finding_id]["status"] == "superseded":
            return False
        if held not in (None, by):
            raise Refused(f"finding {finding_id} is superseded by {held} already")
        step, seen = rows[by]["superseded_by"], {by}
        while step is not None and step not in seen:
            if step == finding_id:
                raise Refused(f"finding {by} is superseded by {finding_id} (directly or through others), so it "
                              f"cannot supersede {finding_id}")
            seen.add(step)
            row = connection.execute("SELECT superseded_by FROM ledger_finding WHERE id = ?", (step,)).fetchone()
            step = row["superseded_by"] if row else None
        connection.execute("UPDATE ledger_finding SET status = 'superseded', superseded_by = ? WHERE id = ?",
                           (by, finding_id))
    return True


# ---------------------------------------------------------------------------------------------
# recording what files state: CI run records, result files, parsed logs

@dataclass
class Record:
    """One run as a file states it, for record_run."""
    label: str
    fields: dict = field(default_factory=dict)  # as run_fields checks them
    inputs: list = field(default_factory=list)  # (name, value, sha256), as run_inputs makes them
    measurements: list = field(default_factory=list)  # rows, as measurement_rows makes them
    jobs: list = field(default_factory=list)  # the Workbench jobs its results came from


@dataclass
class Recorded:
    run: int
    added: bool  # whether record_run added the run, rather than mapping it to the ledger's
    counts: Counter  # rows by (table, outcome): inserted, matched, and measurements replaced


def record_run(connection, record: Record, replace: bool = False) -> Recorded:
    """Maps a run to the ledger's run of its label, or adds it, and records what it states, so that recording it
    again changes nothing: an input is added when the run has none of its name and matched when it has this one;
    the measurements of a source are added when the run has none from that source and matched when it holds
    exactly these; a job link is added once. A run the ledger has keeps its fields.

    Raises Refused, with nothing of the record written, when the ledger's run has another value of one of its
    inputs or other measurements from one of its sources; ``replace`` replaces such measurements instead. Runs in
    the caller's transaction."""
    counts = Counter()
    connection.execute("SAVEPOINT record_run")
    try:
        run_id, added = _map_run(connection, record, counts)
        _map_inputs(connection, record, run_id, counts)
        _map_measurements(connection, record, run_id, replace, counts)
        for job in dict.fromkeys(record.jobs):
            counts["run_job", keyed(connection, "ledger_run_job", {"run_id": run_id, "job": job})] += 1
    except (Refused, sqlite3.IntegrityError) as error:
        connection.execute("ROLLBACK TO record_run")
        connection.execute("RELEASE record_run")
        if isinstance(error, Refused):
            raise
        raise Refused(f"run {record.label} cannot be recorded: {error}") from None
    connection.execute("RELEASE record_run")
    return Recorded(run_id, added, counts)


def _map_run(connection, record: Record, counts: Counter) -> tuple[int, bool]:
    row = connection.execute("SELECT id FROM ledger_run WHERE label = ?", (record.label,)).fetchone()
    if row is not None:
        counts["run", "matched"] += 1
        return row["id"], False
    values = {name: record.fields[name] for name in RUN_FIELDS if record.fields.get(name) is not None}
    values.setdefault("date", datetime.date.today().isoformat())
    names = ("label", *values)
    run_id = connection.execute(f"INSERT INTO ledger_run ({listed(names)}) VALUES ({marks(names)})",
                                (record.label, *values.values())).lastrowid
    counts["run", "inserted"] += 1
    return run_id, True


def _map_inputs(connection, record: Record, run_id: int, counts: Counter):
    for name, value, digest in record.inputs:
        held = connection.execute("SELECT value, sha256 FROM ledger_run_input WHERE run_id = ? AND name = ?",
                                  (run_id, name)).fetchone()
        if held is None:
            connection.execute("INSERT INTO ledger_run_input (run_id, name, value, sha256) VALUES (?, ?, ?, ?)",
                               (run_id, name, value, digest))
            counts["run_input", "inserted"] += 1
        elif (held["value"], held["sha256"]) == (value, digest):
            counts["run_input", "matched"] += 1
        else:
            raise Refused(f"the ledger's run {record.label} ran on {name} {_stated(held['value'], held['sha256'])}, "
                          f"not {_stated(value, digest)}")


def _map_measurements(connection, record: Record, run_id: int, replace: bool, counts: Counter):
    sources: dict[str, list] = {}
    for row in record.measurements:
        sources.setdefault(row[0], []).append(tuple(row))
    for source, rows in sources.items():
        held = [tuple(row) for row in connection.execute(
            f"SELECT {listed(MEASUREMENT_FIELDS)} FROM ledger_measurement WHERE run_id = ? AND source = ?",
            (run_id, source))]
        if held and Counter(held) == Counter(rows):
            counts["measurement", "matched"] += len(rows)
            continue
        if held and not replace:
            raise Refused(f"the ledger's run {record.label} holds other measurements from {source} ({len(held)} "
                          f"there, {len(rows)} here)")
        if held:
            connection.execute("DELETE FROM ledger_measurement WHERE run_id = ? AND source = ?", (run_id, source))
            counts["measurement", "replaced"] += len(held)
        connection.executemany(f"INSERT INTO ledger_measurement (run_id, {listed(MEASUREMENT_FIELDS)}) "
                               f"VALUES (?, {marks(MEASUREMENT_FIELDS)})", [(run_id, *row) for row in rows])
        counts["measurement", "inserted"] += len(rows)


def _stated(value, digest) -> str:
    return (repr(value) if value is not None else "(no value)") + (f" sha256 {digest[:12]}" if digest else "")


def record_findings(connection, findings: list[dict], run_id: int) -> Counter:
    """Maps each finding to the ledger's finding of its id, or adds it, and links it to the run, to the other runs
    it names by label and to its jobs, whose evidence is pinned. A finding the ledger has is kept as it is, its
    links added. Raises Refused, with none of them written, when one rests on a run the ledger does not have."""
    counts = Counter()
    connection.execute("SAVEPOINT record_findings")
    try:
        for finding in findings:
            runs = [run_id]
            for label in finding.get("runs", ()):
                row = connection.execute("SELECT id FROM ledger_run WHERE label = ?", (label,)).fetchone()
                if row is None:
                    raise Refused(f"finding {finding['id']} rests on run {label}, which the ledger does not have")
                runs.append(row["id"])
            if connection.execute("SELECT 1 FROM ledger_finding WHERE id = ?", (finding["id"],)).fetchone():
                counts["finding", "matched"] += 1
            else:
                connection.execute("INSERT INTO ledger_finding (id, date, title, statement, status, evidence) "
                                   "VALUES (?, ?, ?, ?, ?, ?)",
                                   (finding["id"], finding.get("date") or datetime.date.today().isoformat(),
                                    finding["title"], finding["statement"], finding["status"], finding.get("evidence")))
                counts["finding", "inserted"] += 1
            for run in dict.fromkeys(runs):
                counts["finding_run", keyed(connection, "ledger_finding_run",
                                            {"finding_id": finding["id"], "run_id": run})] += 1
            for job in dict.fromkeys(finding.get("jobs", ())):
                counts["finding_job", keyed(connection, "ledger_finding_job",
                                            {"finding_id": finding["id"], "job": job})] += 1
                connection.execute("UPDATE artifact SET pinned = 1 WHERE job = ?", (job,))  # D26: evidence is kept
    except (Refused, sqlite3.IntegrityError) as error:
        connection.execute("ROLLBACK TO record_findings")
        connection.execute("RELEASE record_findings")
        if isinstance(error, Refused):
            raise
        raise Refused(f"a finding cannot be recorded: {error}") from None
    connection.execute("RELEASE record_findings")
    return counts


def keyed(connection, table: str, values: dict) -> str:
    """Adds a row named by its key alone (a link) unless the ledger has it already; whether it was inserted or
    matched."""
    if connection.execute(f"SELECT 1 FROM {table} WHERE {matching(values)}", tuple(values.values())).fetchone():
        return "matched"
    connection.execute(f"INSERT INTO {table} ({listed(values)}) VALUES ({marks(values)})", tuple(values.values()))
    return "inserted"


def jobs_of_output(connection, command: str, name: str, path) -> list[str]:
    """The jobs of ``command`` whose output ``name`` is the file or folder at ``path``: the Workbench jobs a record
    came from."""
    return [row["job"] for row in connection.execute(
        "SELECT a.job FROM artifact a JOIN job j ON j.id = a.job WHERE a.name = ? AND a.path = ? AND j.command = ? "
        "ORDER BY j.submitted, a.job", (name, str(path), command))]


# ---------------------------------------------------------------------------------------------
# importing a results database

TABLE_MAP = {"run": "ledger_run", "run_input": "ledger_run_input", "measurement": "ledger_measurement",
             "finding": "ledger_finding", "finding_run": "ledger_finding_run", "machine": "ledger_machine",
             "api_call": "ledger_api_call", "offer": "ledger_offer", "rental": "ledger_rental",
             "charge": "ledger_charge"}
SOURCE_TABLES = {new: old for old, new in TABLE_MAP.items()}
# Each table after the tables its rows reference, so a reference's map is complete before it is translated.
IMPORT_ORDER = ("machine", "api_call", "offer", "rental", "run", "run_input", "measurement", "finding", "finding_run",
                "charge")
# What became of a source row, exactly one each: inserted under its own id; remapped to a new id, its own being
# another row's; matched to the ledger row that already is it; or repaired: matched to a verbatim copy an
# earlier import made, whose references now name the rows this import maps theirs to.
OUTCOMES = ("inserted", "remapped", "matched", "repaired")


class ImportConflict(ValueError):
    """A source row the ledger can neither match nor take, or a reference the source cannot resolve. The import
    stops, and the ledger is left as it was."""


@dataclass
class Table:
    """A source table and its ledger table, as the ledger's schema describes it."""
    old: str
    new: str
    columns: tuple[str, ...]  # the columns both have
    key: tuple[str, ...]  # the ledger's primary key
    natural: tuple[str, ...]  # the columns that say two rows are the same thing
    references: dict[str, str]  # a column naming another row -> the source table that row is in
    renumbered: bool  # an INTEGER PRIMARY KEY: a row whose id is taken can be given a new one
    inputs: tuple[str, ...] = ()  # source run columns the ledger has none of: each becomes an input of its run

    @property
    def id(self) -> str:
        return self.key[0]


@dataclass
class Import:
    """Where an import put the source's rows: ``maps[old][source id]`` is the ledger id each maps to."""
    connection: sqlite3.Connection
    maps: dict = field(default_factory=dict)
    added: dict = field(default_factory=dict)  # old -> the source ids of the rows this import added
    tally: dict = field(default_factory=dict)  # old -> {outcome: rows}

    def translate(self, table: Table, row: dict) -> dict:
        """The row as the ledger must hold it: each reference replaced by the ledger id its row maps to."""
        values = dict(row)
        for column, target in table.references.items():
            if values[column] is not None:
                try:
                    values[column] = self.maps[target][values[column]]
                except KeyError:
                    raise ImportConflict(f"{label(table, row)}: {column} names {target} {values[column]!r}, "
                                         "which the source does not have") from None
        return values

    def settle(self, table: Table, row: dict, ledger_id, outcome: str):
        self.maps[table.old][row[table.id]] = ledger_id
        if outcome in ("inserted", "remapped"):
            self.added.setdefault(table.old, set()).add(row[table.id])
        self.tally[table.old][outcome] += 1

    def merge(self, counts: Counter):
        """Adds the rows a record_run or record_findings wrote, by (table, outcome), to the tally."""
        for (table, outcome), rows in counts.items():
            tally = self.tally.setdefault(table, dict.fromkeys(OUTCOMES, 0))
            tally[outcome] = tally.get(outcome, 0) + rows

    def counts(self) -> dict:
        """The rows added to each table under its name, as before, and per outcome the source rows it had."""
        counts = {old: tally["inserted"] + tally["remapped"] for old, tally in self.tally.items()}
        counts.update({outcome: {old: tally[outcome] for old, tally in self.tally.items()} for outcome in OUTCOMES})
        return counts


def import_results(connection, source: Path) -> dict:
    """Copies a results database into the ledger tables. Every source row maps to a ledger row: the one with the
    same natural key when there is one, else a new row, under the source's id when that id is free and a new id
    when it is not. References are translated through those maps, so each names the row it named in the source,
    and a verbatim copy an earlier import made, whose reference names a row this import maps elsewhere, is
    repaired. A source of an older schema, whose runs name what they ran on in columns of their own, gives its runs
    those values as inputs. Importing twice changes nothing.

    ``counts[old]`` is the rows added to each table; ``counts[outcome][old]`` splits its source rows by
    ``OUTCOMES``."""
    connection.execute("ATTACH DATABASE ? AS src", (f"{Path(source).resolve().as_uri()}?mode=ro",))
    try:
        with connection:
            # One write transaction from the first read, so an id found free is still free when it is used.
            connection.execute("BEGIN IMMEDIATE")
            # References are checked at commit: a finding names the finding that superseded it, maybe copied later.
            connection.execute("PRAGMA defer_foreign_keys = ON")
            state = Import(connection)
            for old in IMPORT_ORDER:
                if in_source(connection, old):
                    copy_table(state, describe(connection, old))
            return state.counts()
    finally:
        connection.execute("DETACH DATABASE src")


def in_source(connection, old: str) -> bool:
    return connection.execute("SELECT 1 FROM src.sqlite_master WHERE type = 'table' AND name = ?",
                              (old,)).fetchone() is not None


def describe(connection, old: str) -> Table:
    """Reads the ledger table's keys and references from its schema, so a migration that adds one is followed."""
    new = TABLE_MAP[old]
    info = connection.execute(f"PRAGMA main.table_info({new})").fetchall()
    ours = {column["name"] for column in info}
    theirs = [c["name"] for c in connection.execute(f"PRAGMA src.table_info({old})")]
    columns = tuple(column for column in theirs if column in ours)
    key = tuple(column["name"] for column in sorted(info, key=lambda c: c["pk"]) if column["pk"])
    renumbered = len(key) == 1 and next(c["type"] for c in info if c["name"] == key[0]).upper() == "INTEGER"
    natural = natural_key(connection, new, columns, key, renumbered)
    missing = [column for column in dict.fromkeys(key + natural) if column not in columns]
    if missing:
        raise ImportConflict(f"the source's {old} has no {', '.join(missing)}, which names its rows")
    references = {fk["from"]: SOURCE_TABLES[fk["table"]]
                  for fk in connection.execute(f"PRAGMA main.foreign_key_list({new})") if fk["from"] in columns}
    inputs = tuple(column for column in theirs if column not in ours) if old == "run" else ()
    return Table(old, new, columns, key, natural, references, renumbered, inputs)


def natural_key(connection, new: str, columns: tuple, key: tuple, renumbered: bool) -> tuple[str, ...]:
    """The columns that say a source row is already in the ledger: the table's UNIQUE columns; without them its
    primary key, unless that is a bare row number, which says nothing about the row: then every other column."""
    unique = [index["name"] for index in connection.execute(f"PRAGMA main.index_list({new})")
              if index["unique"] and index["origin"] != "pk"]
    if len(unique) > 1:
        raise ImportConflict(f"{new} has {len(unique)} unique keys, and an import matches rows on one")
    if unique:
        return tuple(column["name"] for column in connection.execute(f"PRAGMA main.index_info({unique[0]})"))
    return tuple(column for column in columns if column not in key) if renumbered else key


def copy_table(state: Import, table: Table):
    state.tally.setdefault(table.old, dict.fromkeys(OUTCOMES, 0))
    if len(table.key) == 1:
        copy_identified(state, table)
    elif table.natural:
        copy_keyed(state, table)
    else:
        copy_keyless(state, table)
    if table.inputs:
        copy_inputs(state, table)


def copy_identified(state: Import, table: Table):
    """Rows with an id of their own. Those whose id another row holds are given new ids only after every row whose
    id is free has taken its own, so one taken id does not push on the ids of the rows after it."""
    rows = source_rows(state.connection, table)
    # A TEXT id is the row's name, kept whatever happens, and a row may name one copied after it.
    state.maps[table.old] = {} if table.renumbered else {row[table.id]: row[table.id] for row in rows}
    taken = [row for row in rows if not place(state, table, row)]
    for row in taken:
        renumber(state, table, row)


def place(state: Import, table: Table, row: dict) -> bool:
    """Maps a source row to the ledger row that already is it, or inserts it under its own id when that is free;
    False when another row holds its id."""
    values = state.translate(table, row)
    held = held_row(state.connection, table, row[table.id])
    if held == row:  # an earlier import copied it as it was; its references may name the wrong rows
        moved = repair(state.connection, table, values, held)
        state.settle(table, row, row[table.id], "repaired" if moved else "matched")
        return True
    found = same_row(state.connection, table, values)
    if found is not None:
        state.settle(table, row, found, "matched")
        return True
    if held is None:
        insert(state.connection, table, values, table.columns)
        state.settle(table, row, row[table.id], "inserted")
        return True
    return False


def renumber(state: Import, table: Table, row: dict):
    """A row whose id another row holds: matched when a row added before it is the same (a table without a UNIQUE
    key can repeat a row), else inserted under a new id."""
    values = state.translate(table, row)
    found = same_row(state.connection, table, values)
    if found is not None:
        state.settle(table, row, found, "matched")
    elif not table.renumbered:
        raise ImportConflict(f"{label(table, row)} can be neither matched nor inserted: another row holds its id, "
                             "and only a numeric id can be replaced")
    else:
        columns = tuple(column for column in table.columns if column != table.id)
        state.settle(table, row, insert(state.connection, table, values, columns), "remapped")


def held_row(connection, table: Table, row_id) -> dict | None:
    held = connection.execute(f"SELECT {listed(table.columns)} FROM {table.new} WHERE {quoted(table.id)} = ?",
                              (row_id,)).fetchone()
    return None if held is None else dict(zip(table.columns, held))


def repair(connection, table: Table, values: dict, copy: dict) -> bool:
    """Points a verbatim copy's references at the rows this import maps theirs to; whether any moved. The copy
    holds the source's ids, so a reference moves exactly when its translation differs."""
    moved = {column: values[column] for column in table.references if values[column] != copy[column]}
    if moved:
        connection.execute(f"UPDATE {table.new} SET {', '.join(f'{quoted(c)} = ?' for c in moved)} "
                           f"WHERE {quoted(table.id)} = ?", (*moved.values(), values[table.id]))
    return bool(moved)


def same_row(connection, table: Table, values: dict):
    """The id of the ledger row with this row's natural key. Without a UNIQUE key several rows can have it: the
    one under the row's own id comes first, so an import repeated maps each row where it mapped it before."""
    found = connection.execute(f"SELECT {quoted(table.id)} FROM {table.new} WHERE {matching(table.natural)} "
                               f"ORDER BY {quoted(table.id)} = ? DESC, {quoted(table.id)} LIMIT 1",
                               (*(values[column] for column in table.natural), values[table.id])).fetchone()
    return None if found is None else found[0]


def copy_keyed(state: Import, table: Table):
    """Rows named by a composite key (a finding's runs, a run's inputs, charges): translated, then inserted or
    ignored as INSERT OR IGNORE would, except that a row the ledger refuses for anything but its key stops the
    import."""
    for row in source_rows(state.connection, table):
        values = state.translate(table, row)
        found = state.connection.execute(f"SELECT 1 FROM {table.new} WHERE {matching(table.natural)}",
                                         [values[column] for column in table.natural]).fetchone()
        if found is None:
            insert(state.connection, table, values, table.columns)
        state.tally[table.old]["matched" if found else "inserted"] += 1


def copy_keyless(state: Import, table: Table):
    """Rows without a key (measurements) cannot be told from rows already there, so they come only with the rows
    they belong to, and only when this import added those; the others count as matched, there with their run."""
    rows = source_rows(state.connection, table)
    copied = [state.translate(table, row) for row in rows
              if all(row[column] in state.added.get(target, ()) for column, target in table.references.items())]
    try:
        state.connection.executemany(f"INSERT INTO {table.new} ({listed(table.columns)}) "
                                     f"VALUES ({marks(table.columns)})",
                                     [[values[column] for column in table.columns] for values in copied])
    except sqlite3.IntegrityError as error:
        raise ImportConflict(f"a {table.old} row can be neither matched nor inserted: {error}") from None
    state.tally[table.old].update(inserted=len(copied), matched=len(rows) - len(copied))


def copy_inputs(state: Import, table: Table):
    """The source run columns the ledger has none of (an older schema gave some inputs columns of their own) become
    inputs of their runs, named by the column, as migration 002 moved the ledger's own: every value, one of 64 hex
    digits being also the input's SHA-256. An input its run has already is matched."""
    tally = state.tally.setdefault("run_input", dict.fromkeys(OUTCOMES, 0))
    cursor = state.connection.execute(f"SELECT {listed((table.id, *table.inputs))} FROM src.{table.old} "
                                      f"ORDER BY {quoted(table.id)}")
    for row in cursor.fetchall():
        run_id = state.maps[table.old][row[0]]
        for name, value in zip(table.inputs, tuple(row)[1:]):
            if value is None:
                continue
            if state.connection.execute("SELECT 1 FROM ledger_run_input WHERE run_id = ? AND name = ?",
                                        (run_id, name)).fetchone():
                tally["matched"] += 1
                continue
            value = value if isinstance(value, str) else str(value)
            try:
                state.connection.execute("INSERT INTO ledger_run_input (run_id, name, value, sha256) "
                                         "VALUES (?, ?, ?, ?)", (run_id, name, value, digest_of(value)))
            except sqlite3.IntegrityError as error:
                raise ImportConflict(f"run {row[0]!r}: its {name} cannot be an input: {error}") from None
            tally["inserted"] += 1


def source_rows(connection, table: Table) -> list[dict]:
    """The source table's rows in key order, so new ids follow the source's order."""
    order = f" ORDER BY {listed(table.key)}" if table.key else ""
    cursor = connection.execute(f"SELECT {listed(table.columns)} FROM src.{table.old}{order}")
    return [dict(zip(table.columns, row)) for row in cursor.fetchall()]


def insert(connection, table: Table, values: dict, columns: tuple) -> int:
    """Inserts the row's ``columns`` and returns its rowid; a row the ledger refuses stops the import."""
    try:
        return connection.execute(f"INSERT INTO {table.new} ({listed(columns)}) VALUES ({marks(columns)})",
                                  [values[column] for column in columns]).lastrowid
    except sqlite3.IntegrityError as error:
        raise ImportConflict(f"{label(table, values)} can be neither matched nor inserted: {error}") from None


def label(table: Table, row: dict) -> str:
    """How an error names a source row: by its key, or by the rows it belongs to."""
    if table.key:
        return f"{table.old} " + ", ".join(repr(row[column]) for column in table.key)
    return f"a {table.old} row of " + ", ".join(f"{column} {row[column]!r}" for column in table.references)


def quoted(column: str) -> str:
    return f'"{column}"'


def listed(columns) -> str:
    return ", ".join(map(quoted, columns))


def marks(columns) -> str:
    return ", ".join("?" * len(columns))


def matching(columns) -> str:
    return " AND ".join(f"{quoted(column)} IS ?" for column in columns)  # IS: a NULL matches a NULL
