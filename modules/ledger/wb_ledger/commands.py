from wb_core.module import Context, ParamError

from . import store


def _rows(cursor, limit=None):
    rows = cursor.fetchmany(limit) if limit else cursor.fetchall()
    return [dict(r) for r in rows]


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
    run = connection.execute("SELECT * FROM ledger_run WHERE label = ? OR CAST(id AS TEXT) = ?", (ref, ref)).fetchone()
    if run is None:
        raise ParamError(f"no run or finding {ref}")
    measurements = _rows(connection.execute("SELECT source, place, view, sample, metric, part, value, unit, against "
                                            "FROM ledger_measurement WHERE run_id = ?", (run["id"],)))
    ctx.summary(f"run {run['label']}: {len(measurements)} measurement(s)")
    return {"run": dict(run), "measurements": measurements}


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


def add_run(ctx: Context, label: str, fields: dict, measurements: list):
    rows = []
    for item in measurements:
        if not isinstance(item, dict) or "metric" not in item or "value" not in item:
            raise ParamError("each measurement needs metric and value")
        rows.append((item.get("source", "wb"), item.get("place"), item.get("view"), item.get("sample"),
                     item["metric"], item.get("part", ""), float(item["value"]), item.get("unit"), item.get("against")))
    run_id = store.add_run(store.connect(), label, dict(fields, job=fields.get("job") or ctx.job), rows)
    ctx.summary(f"run {label} recorded as {run_id} with {len(rows)} measurement(s)")
    return {"run": run_id}


def add_finding(ctx: Context, id: str, title: str, statement: str, status: str, runs: list, jobs: list,
                evidence=None):
    try:
        store.add_finding(store.connect(), id, title, statement, status, [str(r) for r in runs],
                          [str(j) for j in jobs], evidence)
    except (KeyError, ValueError) as error:
        raise ParamError(str(error)) from None
    ctx.summary(f"finding {id} ({status}) on {len(runs)} run(s), {len(jobs)} job(s)")
    return {"finding": id}


def import_db(ctx: Context, source: str):
    try:
        counts = store.import_results(store.connect(), source)
    except store.ImportConflict as error:
        raise ParamError(str(error)) from None
    ctx.summary(_imported(counts))
    return {"imported": counts}


def _imported(counts: dict) -> str:
    """Per table, the rows inserted, remapped (given new ids, theirs being taken) and repaired; the rows matched
    as one number, since after the first import that is nearly every row."""
    parts = [f"{outcome} " + ", ".join(f"{table} {n}" for table, n in counts[outcome].items() if n)
             for outcome in ("inserted", "remapped", "repaired") if any(counts[outcome].values())]
    matched = sum(counts["matched"].values())
    return "; ".join(parts or ["nothing new"]) + (f"; {matched} matched" if matched else "")
