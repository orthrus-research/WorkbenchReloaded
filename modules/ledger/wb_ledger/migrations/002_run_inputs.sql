-- Runs name what they ran on as inputs: a name, a value, and the input's SHA-256 when it is known. The first schema
-- gave three projects' inputs run columns of their own (client_jar, cubature, riven); each becomes an input of the
-- column's name, every value moved, a value of 64 hex digits being also its SHA-256 (store.digest_of is the same
-- rule), and the columns are dropped. DROP COLUMN rewrites the table in place: a rebuild (create, copy, drop, rename)
-- would cascade-delete every run's measurements and finding links, as this script runs in one transaction, where
-- foreign keys cannot be switched off.
CREATE TABLE ledger_run_input (
    run_id INTEGER NOT NULL REFERENCES ledger_run(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    value TEXT,
    sha256 TEXT CHECK (sha256 IS NULL OR (length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*')),
    CHECK (value IS NOT NULL OR sha256 IS NOT NULL),
    PRIMARY KEY (run_id, name)
);
INSERT INTO ledger_run_input (run_id, name, value, sha256)
SELECT id, 'client_jar', client_jar,
       CASE WHEN length(client_jar) = 64 AND lower(client_jar) NOT GLOB '*[^0-9a-f]*' THEN lower(client_jar) END
FROM ledger_run WHERE client_jar IS NOT NULL;
INSERT INTO ledger_run_input (run_id, name, value, sha256)
SELECT id, 'cubature', cubature,
       CASE WHEN length(cubature) = 64 AND lower(cubature) NOT GLOB '*[^0-9a-f]*' THEN lower(cubature) END
FROM ledger_run WHERE cubature IS NOT NULL;
INSERT INTO ledger_run_input (run_id, name, value, sha256)
SELECT id, 'riven', riven,
       CASE WHEN length(riven) = 64 AND lower(riven) NOT GLOB '*[^0-9a-f]*' THEN lower(riven) END
FROM ledger_run WHERE riven IS NOT NULL;
ALTER TABLE ledger_run DROP COLUMN client_jar;
ALTER TABLE ledger_run DROP COLUMN cubature;
ALTER TABLE ledger_run DROP COLUMN riven;
-- The Workbench jobs a run's results came from (ledger.link-run, the importers).
CREATE TABLE ledger_run_job (
    run_id INTEGER NOT NULL REFERENCES ledger_run(id) ON DELETE CASCADE,
    job TEXT NOT NULL,
    PRIMARY KEY (run_id, job)
);
-- A run's places are every place its measurements name; the first view counted one benchmark source's only.
DROP VIEW ledger_run_cost;
CREATE VIEW ledger_run_cost AS
SELECT run.id, run.label, run.date, rc.provider, rc.instance, rc.gpu, rc.cpu_cores_effective, rc.place,
       coalesce(rc.billed, rc.estimated) / max(rc.runs, 1) AS cost,
       (SELECT count(DISTINCT x.place) FROM ledger_measurement x WHERE x.run_id = run.id) AS places_benchmarked
FROM ledger_run run JOIN ledger_rental_cost rc ON rc.id = run.rental_id;
