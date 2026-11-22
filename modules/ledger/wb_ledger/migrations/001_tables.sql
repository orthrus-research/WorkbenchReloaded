-- The results ledger, ported from Quadrature's tools/results.py (schema version 3) with a ledger_ prefix.
CREATE TABLE ledger_run (
    id INTEGER PRIMARY KEY,
    label TEXT NOT NULL UNIQUE,
    date TEXT NOT NULL,
    instance TEXT, os TEXT, host TEXT, card TEXT, driver TEXT,
    client_jar TEXT, cubature TEXT, riven TEXT, options TEXT, tour TEXT, window TEXT,
    radius INTEGER, exit INTEGER, evidence TEXT, note TEXT,
    machine_id INTEGER REFERENCES ledger_machine(id),
    rental_id INTEGER REFERENCES ledger_rental(id),
    job TEXT
);
CREATE TABLE ledger_measurement (
    run_id INTEGER NOT NULL REFERENCES ledger_run(id) ON DELETE CASCADE,
    source TEXT NOT NULL, place TEXT, view TEXT, sample INTEGER,
    metric TEXT NOT NULL, part TEXT NOT NULL DEFAULT '', value REAL NOT NULL, unit TEXT, against TEXT
);
CREATE INDEX ledger_measurement_run ON ledger_measurement(run_id);
CREATE INDEX ledger_measurement_metric ON ledger_measurement(metric, part);
CREATE TABLE ledger_finding (
    id TEXT PRIMARY KEY,
    date TEXT NOT NULL, title TEXT NOT NULL, statement TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('established', 'hypothesis', 'refuted', 'superseded')),
    superseded_by TEXT REFERENCES ledger_finding(id),
    evidence TEXT
);
CREATE TABLE ledger_finding_run (
    finding_id TEXT NOT NULL REFERENCES ledger_finding(id) ON DELETE CASCADE,
    run_id INTEGER NOT NULL REFERENCES ledger_run(id) ON DELETE CASCADE,
    PRIMARY KEY (finding_id, run_id)
);
CREATE TABLE ledger_finding_job (
    finding_id TEXT NOT NULL REFERENCES ledger_finding(id) ON DELETE CASCADE,
    job TEXT NOT NULL,
    PRIMARY KEY (finding_id, job)
);
CREATE TABLE ledger_machine (
    id INTEGER PRIMARY KEY,
    provider TEXT NOT NULL, key TEXT NOT NULL, host TEXT,
    gpu TEXT, gpu_ram_mib REAL, gpu_mem_bw REAL, pcie_bw REAL, cpu TEXT, cpu_cores_effective REAL,
    cpu_ram_mib REAL, cpu_ghz REAL, disk_bw REAL, inet_up REAL, inet_down REAL, place TEXT, note TEXT,
    UNIQUE (provider, key)
);
CREATE TABLE ledger_api_call (
    id INTEGER PRIMARY KEY,
    time TEXT NOT NULL, provider TEXT NOT NULL, method TEXT NOT NULL, endpoint TEXT NOT NULL,
    request TEXT, status INTEGER, duration_ms REAL, bytes INTEGER, result_count INTEGER, purpose TEXT,
    response BLOB, job TEXT
);
CREATE INDEX ledger_api_call_time ON ledger_api_call(time);
CREATE TABLE ledger_offer (
    id INTEGER PRIMARY KEY,
    observed TEXT NOT NULL, provider TEXT NOT NULL, offer TEXT NOT NULL,
    machine_id INTEGER REFERENCES ledger_machine(id), call_id INTEGER REFERENCES ledger_api_call(id),
    dph REAL, dph_base REAL, storage_hourly REAL, min_bid REAL, discounted_dph REAL, credit_discount_max REAL,
    rented INTEGER, duration_s REAL, end_date REAL, score REAL, storage_cost REAL, inet_up_cost REAL,
    inet_down_cost REAL, driver TEXT, cuda REAL, reliability REAL, verified INTEGER, dlperf REAL, raw TEXT,
    UNIQUE (provider, offer, observed)
);
CREATE TABLE ledger_rental (
    id INTEGER PRIMARY KEY,
    provider TEXT NOT NULL, instance TEXT NOT NULL, offer TEXT,
    machine_id INTEGER REFERENCES ledger_machine(id),
    label TEXT, purpose TEXT, created TEXT, destroyed TEXT, held_s REAL, dph_quoted REAL, dph_billed REAL,
    disk_gb REAL, driver TEXT,
    outcome TEXT CHECK (outcome IN ('completed', 'partial', 'failed')),
    failure TEXT, to_running_s REAL, to_ssh_s REAL, upload_s REAL, upload_bytes REAL, bootstrap_s REAL,
    work_s REAL, fetch_s REAL, download_bytes REAL, job TEXT, quote TEXT,
    UNIQUE (provider, instance)
);
CREATE TABLE ledger_charge (
    provider TEXT NOT NULL, instance TEXT NOT NULL, day TEXT NOT NULL, kind TEXT NOT NULL,
    amount REAL NOT NULL, description TEXT,
    PRIMARY KEY (provider, instance, day, kind)
);
CREATE VIEW ledger_rental_cost AS
SELECT r.id, r.provider, r.instance, r.label, r.purpose, r.created, r.outcome, r.failure, r.job,
       r.held_s / 60.0 AS held_min, r.dph_quoted, r.dph_billed,
       r.held_s / 3600.0 * r.dph_billed AS estimated,
       (SELECT sum(c.amount) FROM ledger_charge c WHERE c.provider = r.provider AND c.instance = r.instance) AS billed,
       (SELECT count(*) FROM ledger_run WHERE ledger_run.rental_id = r.id) AS runs,
       r.to_running_s, r.to_ssh_s, r.upload_s, r.upload_bytes, r.bootstrap_s, r.work_s, r.fetch_s,
       r.driver, m.gpu, m.cpu, m.cpu_cores_effective, m.cpu_ghz, m.pcie_bw, m.gpu_mem_bw, m.inet_up, m.inet_down,
       m.place, m.key AS machine
FROM ledger_rental r LEFT JOIN ledger_machine m ON m.id = r.machine_id;
CREATE VIEW ledger_run_cost AS
SELECT run.id, run.label, run.date, rc.provider, rc.instance, rc.gpu, rc.cpu_cores_effective, rc.place,
       coalesce(rc.billed, rc.estimated) / max(rc.runs, 1) AS cost,
       (SELECT count(DISTINCT x.place) FROM ledger_measurement x WHERE x.run_id = run.id AND x.source = 'tier-bench')
           AS places_benchmarked
FROM ledger_run run JOIN ledger_rental_cost rc ON rc.id = run.rental_id;
CREATE VIEW ledger_offer_price AS
SELECT o.id, o.observed, CAST(strftime('%w', o.observed) AS INTEGER) AS weekday,
       CAST(strftime('%H', o.observed) AS INTEGER) AS hour, o.provider, o.offer, o.call_id,
       m.key AS machine, m.host, m.gpu, m.gpu_ram_mib, m.cpu_cores_effective, m.cpu_ghz, m.pcie_bw, m.inet_down,
       m.place, o.dph, o.dph_base, o.storage_hourly, o.min_bid, o.discounted_dph, o.rented, o.reliability, o.driver,
       o.verified, o.duration_s
FROM ledger_offer o LEFT JOIN ledger_machine m ON m.id = o.machine_id;
CREATE VIEW ledger_price_change AS
WITH s AS (
    SELECT *, lag(dph_base) OVER w AS base_before, lag(min_bid) OVER w AS bid_before, lag(observed) OVER w AS since
    FROM ledger_offer_price WHERE machine IS NOT NULL
    WINDOW w AS (PARTITION BY provider, machine, offer ORDER BY observed))
SELECT provider, machine, offer, host, gpu, place, since, observed, base_before, dph_base, bid_before, min_bid,
       dph_base - base_before AS base_change, min_bid - bid_before AS bid_change
FROM s
WHERE (base_before IS NOT NULL AND abs(dph_base - base_before) > 1e-9)
   OR (bid_before IS NOT NULL AND abs(min_bid - bid_before) > 1e-9);
