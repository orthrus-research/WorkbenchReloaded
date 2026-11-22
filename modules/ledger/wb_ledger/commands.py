"""The ledger's commands: queries over runs, findings, costs and calls; recording runs, findings and machines; and
importing results databases, CI run records, result files and logs (importers.py says what each reads)."""

import os
import time
from pathlib import Path

from wb_core.module import Context, EnvironmentProblem, ParamError

from . import importers, store

LISTED = 20  # refusals reported as findings one by one; the rest are counted


def _rows(cursor, limit=None):
    rows = cursor.fetchmany(limit) if limit else cursor.fetchall()
    return [dict(r) for r in rows]


def _inputs(connection, where: str, params) -> dict:
    """Each run's inputs, {name, value, sha256} each, for the runs ``where`` selects (``r`` is the run)."""
    found = {}
    for row in connection.execute(f"SELECT i.run_id, i.name, i.value, i.sha256 FROM ledger_run_input i "
                                  f"JOIN ledger_run r ON r.id = i.run_id WHERE {where} ORDER BY i.run_id, i.name",
                                  params):
        found.setdefault(row["run_id"], []).append({"name": row["name"], "value": row["value"],
                                                    "sha256": row["sha256"]})
    return found


def _refusals(ctx: Context, lines: list[str]):
    for line in lines[:LISTED]:
        ctx.finding(line)
    if len(lines) > LISTED:
        ctx.finding(f"{len(lines) - LISTED} more refused (see the result's refused)")


def query(ctx: Context, sql: str, limit: int):
    connection = store.connect(read_only=True)
    try:
        cursor = connection.execute(sql)
    except Exception as error:  # noqa: BLE001 - a bad query is the caller's
        raise ParamError(f"query failed: {error}") from None
    columns = [d[0] for d in cursor.description or []]
    rows = [[r[c] if not isinstance(r[c], bytes) else f"<{len(r[c])} bytes>" for c in columns]
            for r in cursor.fetchmany(limit)]
    ctx.summary(f"{len(rows)} row(s)")
    return {"columns": columns, "rows": rows}


def runs(ctx: Context, like: str):
    connection = store.connect(read_only=True)
    rows = _rows(connection.execute("SELECT id, label, date, host, card, driver, exit, job FROM ledger_run "
                                    "WHERE label LIKE ? ORDER BY date, id", (like,)))
    inputs = _inputs(connection, "r.label LIKE ?", (like,))
    for row in rows:
        row["inputs"] = inputs.get(row["id"], [])
    ctx.summary(f"{len(rows)} run(s)")
    return {"runs": rows}


def findings(ctx: Context, status: str):
    connection = store.connect(read_only=True)
    sql = "SELECT * FROM ledger_finding" + ("" if status == "any" else " WHERE status = ?") + " ORDER BY date"
    rows = _rows(connection.execute(sql, () if status == "any" else (status,)))
    ctx.summary(f"{len(rows)} finding(s)")
    return {"findings": rows}


def show(ctx: Context, ref: str):
    connection = store.connect(read_only=True)
    finding = connection.execute("SELECT * FROM ledger_finding WHERE id = ?", (ref,)).fetchone()
    if finding:
        runs = _rows(connection.execute("SELECT r.id, r.label FROM ledger_finding_run f JOIN ledger_run r "
                                        "ON r.id = f.run_id WHERE f.finding_id = ?", (ref,)))
        jobs = [r["job"] for r in connection.execute("SELECT job FROM ledger_finding_job WHERE finding_id = ?", (ref,))]
        ctx.summary(f"finding {ref}: {finding['status']}")
        return {"finding": dict(finding), "runs": runs, "jobs": jobs}
    run = connection.execute("SELECT * FROM ledger_run WHERE label = ? OR CAST(id AS TEXT) = ? "
                             "ORDER BY label = ? DESC", (ref, ref, ref)).fetchone()
    if run is None:
        raise ParamError(f"no run or finding {ref}")
    inputs = _inputs(connection, "r.id = ?", (run["id"],)).get(run["id"], [])
    jobs = [r["job"] for r in connection.execute("SELECT job FROM ledger_run_job WHERE run_id = ? ORDER BY job",
                                                 (run["id"],))]
    measurements = _rows(connection.execute("SELECT source, place, view, sample, metric, part, value, unit, against "
                                            "FROM ledger_measurement WHERE run_id = ?", (run["id"],)))
    ctx.summary(f"run {run['label']}: {len(inputs)} input(s), {len(measurements)} measurement(s)")
    return {"run": dict(run), "inputs": inputs, "jobs": jobs, "measurements": measurements}


def costs(ctx: Context, limit: int):
    connection = store.connect(read_only=True)
    rows = _rows(connection.execute("SELECT * FROM ledger_rental_cost ORDER BY created DESC LIMIT ?", (limit,)))
    billed = sum(r["billed"] or 0 for r in rows)
    ctx.summary(f"{len(rows)} rental(s), ${billed:.3f} billed")
    return {"rentals": rows, "total_billed": billed}


def calls(ctx: Context, since: str, limit: int):
    connection = store.connect(read_only=True)
    rows = _rows(connection.execute("SELECT id, time, provider, method, endpoint, status, duration_ms, bytes, "
                                    "result_count, purpose, job FROM ledger_api_call WHERE time >= ? "
                                    "ORDER BY id DESC LIMIT ?", (since, limit)))
    ctx.summary(f"{len(rows)} call(s)")
    return {"calls": rows}


def add_run(ctx: Context, label: str, fields: dict, inputs: list, measurements: list):
    try:
        checked = store.run_fields(fields)
        given = store.run_inputs(inputs)
        rows = store.measurement_rows(measurements, source="wb")
    except ValueError as error:
        raise ParamError(str(error)) from None
    run_id = store.add_run(store.connect(), label, dict(checked, job=checked.get("job") or ctx.job), rows, given)
    ctx.summary(f"run {label} recorded as {run_id} with {len(given)} input(s) and {len(rows)} measurement(s)")
    return {"run": run_id}


def add_finding(ctx: Context, id: str, title: str, statement: str, status: str, runs: list, jobs: list,
                evidence=None):
    try:
        store.add_finding(store.connect(), id, title, statement, status, [str(r) for r in runs],
                          [str(j) for j in jobs], evidence)
    except (LookupError, ValueError) as error:
        raise ParamError(str(error)) from None
    ctx.summary(f"finding {id} ({status}) on {len(runs)} run(s), {len(jobs)} job(s)")
    return {"finding": id}


def link_run(ctx: Context, run: str, jobs: list, machine=None, rental=None):
    if not jobs and not machine and not rental:
        raise ParamError("name the jobs, the machine or the rental to link the run to")
    try:
        linked = store.link_run(store.connect(), run, [str(j) for j in jobs], machine, rental)
    except (LookupError, ValueError) as error:
        raise ParamError(str(error)) from None
    parts = []
    if jobs:
        parts.append(f"{len(linked['linked'])} job(s) linked" + (f", {len(jobs) - len(linked['linked'])} linked "
                                                                 "already" if len(linked["linked"]) < len(jobs) else ""))
    parts += [f"on machine {machine}"] if machine else []
    parts += [f"in rental {rental}"] if rental else []
    ctx.summary(f"run {linked['label']}: " + ", ".join(parts))
    return linked


def add_machine(ctx: Context, provider: str, key: str, **specs):
    connection = store.connect()
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        known = connection.execute("SELECT 1 FROM ledger_machine WHERE provider = ? AND key = ?",
                                   (provider, key)).fetchone()
        machine = store.upsert_machine(connection, provider, key, specs)
    given = {name: value for name, value in specs.items() if value is not None}
    ctx.summary(f"machine {provider}:{key} {'updated' if known else 'recorded'} as {machine}"
                + (f" ({', '.join(sorted(given))})" if given else ""))
    return {"machine": machine, "added": known is None, "specs": given}


def supersede(ctx: Context, finding: str, by: str):
    try:
        changed = store.supersede(store.connect(), finding, by)
    except store.Refused as error:
        ctx.finding(f"{error}; nothing changed")
        return {"finding": finding, "by": by, "changed": False}
    except (LookupError, ValueError) as error:
        raise ParamError(str(error)) from None
    ctx.summary(f"finding {finding} is superseded by {by}" + ("" if changed else " already"))
    return {"finding": finding, "by": by, "changed": changed}


def prune(ctx: Context, days: int, execute: bool):
    before = store.utc(time.time() - days * 86400)
    found = store.prune_responses(store.connect(read_only=not execute), before, execute)
    size = f"{found['bytes'] / 1048576:.1f} MiB"
    if execute:
        ctx.summary(f"dropped the stored responses of {found['calls']} call(s) sent before {before} ({size}); the "
                    "calls and the offers they listed stay, and SQLite reuses the space (the file does not shrink)")
    else:
        ctx.summary(f"{found['calls']} call(s) sent before {before} keep their responses ({size}); nothing dropped "
                    "(run with execute=true)")
    return dict(found, dropped=execute)


def import_db(ctx: Context, source: str):
    try:
        counts = store.import_results(store.connect(), source)
    except store.ImportConflict as error:
        raise ParamError(str(error)) from None
    ctx.summary(_imported(counts))
    return {"imported": counts}


def import_ci(ctx: Context, project: str, runs=None):
    """One ledger run per CI run of ``project`` (importers.py says what each holds), mapped to the run of its
    label when the ledger has it, and linked to the ci.run job whose output the run folder is."""
    state_dir = Path(os.environ.get("WB_STATE_DIR") or "/var/lib/wb")
    folder = Path(runs) if runs else state_dir / "ci" / "records" / project
    if not folder.is_dir():
        raise ParamError(f"no CI run records at {ctx.host_path(folder)}" + ("" if runs else (
            "; ci.run keeps a project's records there unless [modules.ci] records names another folder, which runs "
            "can name")))
    readings, refused, unfinished = [], [], []
    for run_folder in sorted(p for p in folder.iterdir() if p.is_dir()):
        summary = run_folder / "summary.json"
        if not summary.is_file():
            if importers.RUN_ID.fullmatch(run_folder.name):
                unfinished.append(run_folder.name)
            continue
        try:
            record = importers.ci_run(summary, project)
        except importers.Unreadable as error:
            refused.append({"run": run_folder.name, "why": str(error)})
            continue
        record.fields.update(evidence=ctx.host_path(run_folder), job=ctx.job)
        readings.append((run_folder, record))
    connection = store.connect()
    state = store.Import(connection)
    with connection:  # every file read first: the write lock is held for the writes alone
        connection.execute("BEGIN IMMEDIATE")
        for run_folder, record in readings:
            record.jobs = store.jobs_of_output(connection, "ci.run", "run", run_folder)
            try:
                state.merge(store.record_run(connection, record).counts)
            except store.Refused as error:
                refused.append({"run": run_folder.name, "why": str(error)})
    refused.sort(key=lambda item: item["run"])
    counts = state.counts()
    _refusals(ctx, [f"CI run {item['run']}: {item['why']}" for item in refused])
    parts = [_imported(counts)]
    if unfinished:
        parts.append(f"{len(unfinished)} without a summary.json yet (running, or cut short)")
    if refused:
        parts.append(f"{len(refused)} refused, first {refused[0]['run']}: {refused[0]['why']}")
    ctx.summary(f"CI runs of {project}: " + "; ".join(parts))
    return {"imported": counts, "refused": refused, "unfinished": unfinished}


def _files(root: Path, pattern: str) -> list[Path]:
    """The file, or the files of that name pattern at any depth under the folder."""
    return [root] if root.is_file() else sorted(path for path in root.rglob(pattern) if path.is_file())


def _record_runs(ctx: Context, connection, state, readings: list, refused: list) -> list:
    """Each (file, record, extra) reading's run recorded, in the caller's transaction; a run the ledger refuses is
    listed in ``refused`` with its file and adds nothing. Returns (file, extra, run id) for each recorded run."""
    recorded = []
    for path, record, extra in readings:
        try:
            done = store.record_run(connection, record)
        except store.Refused as error:
            refused.append({"file": ctx.host_path(path), "why": str(error)})
            continue
        state.merge(done.counts)
        recorded.append((path, extra, done.run))
    return recorded


def import_results(ctx: Context, folder: str, pattern: str):
    """Every result file (importers.py gives the shape) under ``folder``: its run mapped to the run of its label
    when the ledger has it, then its findings, once every file's run is recorded, so that a finding may rest on the
    run of another file. A refused file adds nothing, and a file whose findings are refused adds its run alone."""
    root = Path(folder)
    files = _files(root, pattern)
    readings, refused = [], []
    for path in files:
        try:
            record, found = importers.result_file(path)
        except importers.Unreadable as error:
            refused.append({"file": ctx.host_path(path), "why": str(error)})
            continue
        record.fields.setdefault("job", ctx.job)
        readings.append((path, record, found))
    connection = store.connect()
    state = store.Import(connection)
    with connection:  # every file read first: the write lock is held for the writes alone
        connection.execute("BEGIN IMMEDIATE")
        recorded = _record_runs(ctx, connection, state, readings, refused)
        for path, found, run_id in recorded:
            if found:
                try:
                    state.merge(store.record_findings(connection, found, run_id))
                except store.Refused as error:
                    refused.append({"file": ctx.host_path(path),
                                    "why": f"{error}; its run is recorded, its findings are not"})
    refused.sort(key=lambda item: item["file"])
    counts = state.counts()
    _refusals(ctx, [f"{item['file']}: {item['why']}" for item in refused])
    if not files:
        ctx.summary(f"no {pattern} under {ctx.host_path(root)}")
    else:
        ctx.summary(f"{len(files)} result file(s): {_imported(counts)}"
                    + (f"; {len(refused)} refused, first {refused[0]['file']}: {refused[0]['why']}" if refused else ""))
    return {"imported": counts, "refused": refused, "files": len(files)}


def import_json(ctx: Context, reader: str, folder: str, pattern: str):
    """Every JSON file under ``folder`` read into its runs by a reader of [modules.ledger.readers] (importers.py
    gives a reader's shape), each run mapped to the run of its label when the ledger has it. A file the reader
    refuses adds nothing; a run the ledger refuses adds nothing of itself."""
    try:
        chosen = importers.reader(ctx.config.get("readers") or {}, reader)
    except LookupError as error:
        raise ParamError(str(error)) from None
    except ValueError as error:
        raise EnvironmentProblem(str(error)) from None
    root = Path(folder)
    files = _files(root, pattern)
    readings, refused = [], []
    for path in files:
        try:
            records = importers.read_json(path, chosen)
        except importers.Unreadable as error:
            refused.append({"file": ctx.host_path(path), "why": str(error)})
            continue
        for record in records:
            record.fields.setdefault("job", ctx.job)
            readings.append((path, record, None))
    connection = store.connect()
    state = store.Import(connection)
    with connection:  # every file read first: the write lock is held for the writes alone
        connection.execute("BEGIN IMMEDIATE")
        recorded = _record_runs(ctx, connection, state, readings, refused)
    refused.sort(key=lambda item: item["file"])
    counts = state.counts()
    _refusals(ctx, [f"{item['file']}: {item['why']}" for item in refused])
    if not files:
        ctx.summary(f"no {pattern} under {ctx.host_path(root)}")
    else:
        ctx.summary(f"reader {reader}: {len(files)} file(s), {len(recorded)} run(s): {_imported(counts)}"
                    + (f"; {len(refused)} refused, first {refused[0]['file']}: {refused[0]['why']}" if refused else ""))
    return {"imported": counts, "refused": refused, "files": len(files), "runs": len(recorded)}


def parse_log(ctx: Context, parser: str, log: str, run: str, replace: bool):
    """A log's measurements, by a parser of [modules.ledger.parsers], into a run: the run of that label (or id),
    or a new run of that label dated the day the log was last written."""
    try:
        patterns = importers.parser(ctx.config.get("parsers") or {}, parser)
    except LookupError as error:
        raise ParamError(str(error)) from None
    except ValueError as error:
        raise EnvironmentProblem(str(error)) from None
    path = Path(log)
    if not path.is_file():
        raise ParamError(f"{ctx.host_path(path)} is not a file")
    rows, bad, found = importers.parse_log(path, patterns, parser)
    missing = [metric for metric, n in found.items() if not n]
    result = {"run": None, "label": run, "added": False, "measurements": len(rows), "metrics": found,
              "missing": missing, "recorded": None}
    if bad:
        _refusals(ctx, [f"{path.name}: {line}" for line in bad])
        ctx.summary(f"parser {parser}: {len(bad)} match(es) in {path.name} are not numbers, first {bad[0]}; "
                    "nothing recorded")
        return result
    if not rows:
        ctx.finding(f"parser {parser} matched none of {', '.join(patterns)} in {path.name}; nothing recorded")
        return result
    connection = store.connect()
    label = run
    try:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            target = store.find_run(connection, run)
            label = target["label"] if target else run
            done = store.record_run(connection, store.Record(label=label, measurements=rows,
                                                             fields={"date": importers.day_of(path), "job": ctx.job}),
                                    replace=replace)
    except store.Refused as error:
        ctx.finding(f"{error}; nothing recorded (replace=true replaces them)")
        return dict(result, label=label)
    replaced = done.counts["measurement", "replaced"]
    recorded = "replaced" if replaced else "matched" if done.counts["measurement", "matched"] else "inserted"
    told = {"inserted": "recorded", "matched": "recorded already",
            "replaced": f"recorded in place of {replaced} earlier"}[recorded]
    ctx.summary(f"run {label}{' (new)' if done.added else ''}: {len(rows)} measurement(s) of "
                f"{len(patterns) - len(missing)} metric(s) from {path.name} {told}"
                + (f"; no match for {', '.join(missing)}" if missing else ""))
    return dict(result, run=done.run, label=label, added=done.added, recorded=recorded)


def _imported(counts: dict) -> str:
    """Per table, the rows inserted, remapped (given new ids, theirs being taken) and repaired; the rows matched
    as one number, since after the first import that is nearly every row."""
    parts = [f"{outcome} " + ", ".join(f"{table} {n}" for table, n in counts[outcome].items() if n)
             for outcome in ("inserted", "remapped", "repaired") if any(counts[outcome].values())]
    matched = sum(counts["matched"].values())
    return "; ".join(parts or ["nothing new"]) + (f"; {matched} matched" if matched else "")
