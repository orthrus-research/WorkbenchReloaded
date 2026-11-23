"""The state database (spec S7): one SQLite file in WAL mode holding the core's tables and every module's
``<module>_*`` tables, each with numbered migrations.

Connections are per thread; writes are serialised by SQLite itself (``busy_timeout``).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

CORE_SCHEMA = [
    # 1: jobs, events, quotes, approvals, artifacts, cache
    """
    CREATE TABLE job (
        id TEXT PRIMARY KEY,
        command TEXT NOT NULL,
        module TEXT NOT NULL,
        command_version TEXT,
        module_version TEXT,
        params TEXT NOT NULL,
        params_hash TEXT,
        state TEXT NOT NULL,
        outcome TEXT,
        code INTEGER,
        summary TEXT,
        envelope TEXT,
        machine TEXT,
        out_dir TEXT,
        path_style TEXT NOT NULL DEFAULT 'windows',
        fresh INTEGER NOT NULL DEFAULT 0,
        cache_key TEXT,
        idempotency_key TEXT UNIQUE,
        source TEXT NOT NULL DEFAULT 'api',
        worker_pid INTEGER,
        submitted REAL NOT NULL,
        started REAL,
        settled REAL
    );
    CREATE INDEX job_state ON job(state);
    CREATE INDEX job_command ON job(command, submitted);
    CREATE TABLE job_event (
        job TEXT NOT NULL REFERENCES job(id),
        seq INTEGER NOT NULL,
        ts REAL NOT NULL,
        kind TEXT NOT NULL,
        data TEXT NOT NULL,
        PRIMARY KEY (job, seq)
    );
    CREATE TABLE quote (
        id TEXT PRIMARY KEY,
        job TEXT NOT NULL REFERENCES job(id),
        created REAL NOT NULL,
        expires REAL NOT NULL,
        data TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'open'
    );
    CREATE TABLE approval (
        job TEXT NOT NULL REFERENCES job(id),
        quote TEXT NOT NULL REFERENCES quote(id),
        via TEXT NOT NULL,
        ts REAL NOT NULL,
        PRIMARY KEY (job, quote)
    );
    CREATE TABLE artifact (
        job TEXT NOT NULL REFERENCES job(id),
        name TEXT NOT NULL,
        sha256 TEXT NOT NULL,
        bytes INTEGER NOT NULL,
        media_type TEXT,
        path TEXT NOT NULL,
        stored INTEGER NOT NULL DEFAULT 0,
        pinned INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (job, name)
    );
    CREATE INDEX artifact_sha ON artifact(sha256);
    CREATE TABLE cache (
        key TEXT PRIMARY KEY,
        job TEXT NOT NULL REFERENCES job(id),
        created REAL NOT NULL
    );
    """,
    # 2: the hashed inputs a job ran on
    "ALTER TABLE job ADD COLUMN inputs TEXT;",
    # 3: the job that submitted this one (plans, repeats): the scheduler never makes a child wait on it
    "ALTER TABLE job ADD COLUMN parent TEXT;",
    # 4: a label the submitter gives itself (WB_CALLER), shown and filtered on, never enforced
    "ALTER TABLE job ADD COLUMN caller TEXT;",
]


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._opened: list[sqlite3.Connection] = []  # every thread's connection, for close()
        self._opened_lock = threading.Lock()
        self.write_lock = threading.RLock()
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            connection.execute("CREATE TABLE IF NOT EXISTS schema_migration (module TEXT NOT NULL, "
                               "version INTEGER NOT NULL, applied REAL NOT NULL, PRIMARY KEY (module, version))")
        self.migrate("core", CORE_SCHEMA)

    def connect(self) -> sqlite3.Connection:
        connection = getattr(self._local, "connection", None)
        if connection is None:
            connection = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=30000")
            connection.execute("PRAGMA foreign_keys=ON")
            self._local.connection = connection
            with self._opened_lock:
                self._opened.append(connection)
        return _Context(connection)

    def close(self) -> None:
        """Closes every thread's connection, for when nothing uses the database any more (a thread
        that still does opens a new one)."""
        with self._opened_lock:
            opened, self._opened = self._opened, []
            self._local = threading.local()
        for connection in opened:
            connection.close()

    def migrate(self, module: str, scripts: list[str]) -> int:
        """Applies the numbered scripts not yet applied for ``module``; returns how many ran."""
        applied = 0
        with self.write_lock, self.connect() as connection:
            done = {row["version"] for row in connection.execute(
                "SELECT version FROM schema_migration WHERE module = ?", (module,))}
            for version, script in enumerate(scripts, start=1):
                if version in done:
                    continue
                connection.execute("BEGIN IMMEDIATE")
                try:
                    for statement in _statements(script):
                        connection.execute(statement)
                    connection.execute("INSERT INTO schema_migration VALUES (?, ?, ?)", (module, version, time.time()))
                    connection.execute("COMMIT")
                except Exception:
                    connection.execute("ROLLBACK")
                    raise
                applied += 1
        return applied

    def migrate_dir(self, module: str, directory: Path) -> int:
        if not directory.is_dir():
            return 0
        scripts = [p.read_text(encoding="utf-8") for p in sorted(directory.glob("*.sql"))]
        return self.migrate(module, scripts)

    def execute(self, sql: str, params=()) -> sqlite3.Cursor:
        with self.write_lock:
            return self.connect().execute(sql, params)

    def query(self, sql: str, params=()) -> list[sqlite3.Row]:
        return list(self.connect().execute(sql, params))

    def one(self, sql: str, params=()) -> sqlite3.Row | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def transaction(self):
        return _Transaction(self)


class _Context:
    """Wraps a connection so ``with db.connect() as c`` works without closing the per-thread connection."""

    def __init__(self, connection: sqlite3.Connection):
        self._connection = connection

    def __enter__(self):
        return self._connection

    def __exit__(self, *exc):
        return False

    def __getattr__(self, name):
        return getattr(self._connection, name)


class _Transaction:
    def __init__(self, database: Database):
        self.database = database

    def __enter__(self) -> sqlite3.Connection:
        self.database.write_lock.acquire()
        self.connection = self.database.connect()._connection
        self.connection.execute("BEGIN IMMEDIATE")
        return self.connection

    def __exit__(self, kind, value, traceback):
        try:
            self.connection.execute("ROLLBACK" if kind else "COMMIT")
        finally:
            self.database.write_lock.release()
        return False


def _statements(script: str) -> list[str]:
    statements, current = [], []
    for line in script.splitlines():
        current.append(line)
        candidate = "\n".join(current).strip()
        if candidate.endswith(";") and sqlite3.complete_statement(candidate):
            statements.append(candidate)
            current = []
    tail = "\n".join(current).strip()
    if tail:
        statements.append(tail)
    return [s for s in statements if s.strip(" ;\n")]


def dumps(value) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, default=str)
