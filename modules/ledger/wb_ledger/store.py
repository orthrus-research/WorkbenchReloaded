"""The ledger's storage functions, shared with other modules (``vast`` records its requests, offers,
rentals and charges through these). Ported from Quadrature's tools/results.py, behaviour unchanged.
"""

from __future__ import annotations

import datetime
import json
import os
import sqlite3
import statistics
import time
import zlib
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
RUN_FIELDS = ("date", "instance", "os", "host", "card", "driver", "client_jar", "cubature", "riven", "options", "tour",
              "window", "radius", "exit", "evidence", "note", "job")
STATUSES = ("established", "hypothesis", "refuted", "superseded")


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


def now_utc() -> str:
    moment = time.time()
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(moment)) + f".{int(moment % 1 * 1000):03d}Z"


def clean(value):
    return value.strip() if isinstance(value, str) else value


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


def add_run(connection, label: str, fields: dict, measurements: list[tuple] = ()) -> int:
    values = {name: fields.get(name) for name in RUN_FIELDS if fields.get(name) is not None}
    values.setdefault("date", datetime.date.today().isoformat())
    with connection:
        row = connection.execute("SELECT id FROM ledger_run WHERE label = ?", (label,)).fetchone()
        if row:
            run_id = row["id"]
            if values:
                connection.execute(f"UPDATE ledger_run SET {', '.join(f'{n} = ?' for n in values)} WHERE id = ?",
                                   (*values.values(), run_id))
        else:
            names = ["label", *values]
            run_id = connection.execute(f"INSERT INTO ledger_run ({', '.join(names)}) VALUES "
                                        f"({', '.join('?' * len(names))})", (label, *values.values())).lastrowid
        for row in measurements:
            connection.execute("INSERT INTO ledger_measurement (run_id, source, place, view, sample, metric, part, "
                               "value, unit, against) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (run_id, *row))
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
                raise KeyError(f"no run {label}")
            connection.execute("INSERT OR IGNORE INTO ledger_finding_run VALUES (?, ?)", (finding_id, row["id"]))
        for job in jobs:
            connection.execute("INSERT OR IGNORE INTO ledger_finding_job VALUES (?, ?)", (finding_id, job))
            connection.execute("UPDATE artifact SET pinned = 1 WHERE job = ?", (job,))  # D26: evidence is kept


TABLE_MAP = {"run": "ledger_run", "measurement": "ledger_measurement", "finding": "ledger_finding",
             "finding_run": "ledger_finding_run", "machine": "ledger_machine", "api_call": "ledger_api_call",
             "offer": "ledger_offer", "rental": "ledger_rental", "charge": "ledger_charge"}
SOURCE_TABLES = {new: old for old, new in TABLE_MAP.items()}
# Each table after the tables its rows reference, so a reference's map is complete before it is translated.
IMPORT_ORDER = ("machine", "api_call", "offer", "rental", "run", "measurement", "finding", "finding_run", "charge")
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
    repaired. Importing twice changes nothing.

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
    columns = tuple(c["name"] for c in connection.execute(f"PRAGMA src.table_info({old})") if c["name"] in ours)
    key = tuple(column["name"] for column in sorted(info, key=lambda c: c["pk"]) if column["pk"])
    renumbered = len(key) == 1 and next(c["type"] for c in info if c["name"] == key[0]).upper() == "INTEGER"
    natural = natural_key(connection, new, columns, key, renumbered)
    missing = [column for column in dict.fromkeys(key + natural) if column not in columns]
    if missing:
        raise ImportConflict(f"the source's {old} has no {', '.join(missing)}, which names its rows")
    references = {fk["from"]: SOURCE_TABLES[fk["table"]]
                  for fk in connection.execute(f"PRAGMA main.foreign_key_list({new})") if fk["from"] in columns}
    return Table(old, new, columns, key, natural, references, renumbered)


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
    state.tally[table.old] = dict.fromkeys(OUTCOMES, 0)
    if len(table.key) == 1:
        copy_identified(state, table)
    elif table.natural:
        copy_keyed(state, table)
    else:
        copy_keyless(state, table)


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
    """Rows named by a composite key (a finding's runs, charges): translated, then inserted or ignored as
    INSERT OR IGNORE would, except that a row the ledger refuses for anything but its key stops the import."""
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
