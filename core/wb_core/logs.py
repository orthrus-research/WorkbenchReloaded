"""Logging (D12, S20): Minecraft-style files with a JSONL sidecar, one folder per module.

    logs/latest.log  logs/latest.jsonl  logs/2026-11-21-1.log.gz  logs/2026-11-21-1.jsonl.gz
    logs/modules/<module>/latest.log ...
    jobs/<id>/job.log

``latest.*`` rolls at startup, at UTC midnight and at a size limit. Module code runs in worker
subprocesses and sends its records to the daemon as events, so only the daemon writes these files:
modules log side by side without interleaving mid-line. A filter masks every secret before writing.
"""

from __future__ import annotations

import datetime
import gzip
import json
import logging
import os
import shutil
import sys
import threading
import time
from pathlib import Path

LEVELS = {"DEBUG": logging.DEBUG, "INFO": logging.INFO, "WARNING": logging.WARNING, "ERROR": logging.ERROR}


class Redactor:
    def __init__(self):
        self.values: set[str] = set()
        self.lock = threading.Lock()

    def add(self, value: str | None):
        if value and len(value) >= 6:
            with self.lock:
                self.values.add(value)

    def __call__(self, text: str) -> str:
        if not self.values or not text:
            return text
        for value in self.values:
            if value in text:
                text = text.replace(value, "[redacted]")
        return text


REDACT = Redactor()


def _utc_date(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts, datetime.UTC).strftime("%Y-%m-%d")


def _iso(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts, datetime.UTC).strftime("%Y-%m-%dT%H:%M:%S.") + \
        f"{int(ts * 1000) % 1000:03d}Z"


def text_line(record: dict) -> str:
    job = record.get("job") or "-"
    return f"{_iso(record['ts'])} {record['level']:<5} [{record.get('module') or 'core'}] {job} {record['msg']}"


class RollingPair:
    """``latest.log`` + ``latest.jsonl`` in one folder, rolled together to ``<date>-<n>.*.gz``."""

    def __init__(self, folder: Path, rotate_bytes: int):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.rotate_bytes = rotate_bytes
        self.lock = threading.Lock()
        self.text = self.json = None
        self.opened_date = None
        self.closed = False
        self.roll()  # at startup
        self._open()

    def _open(self):
        self.text = open(self.folder / "latest.log", "a", encoding="utf-8")
        self.json = open(self.folder / "latest.jsonl", "a", encoding="utf-8")
        self.opened_date = _utc_date(time.time())

    def _close(self):
        for handle in (self.text, self.json):
            if handle:
                handle.close()
        self.text = self.json = None

    def roll(self):
        latest = self.folder / "latest.log"
        if not latest.exists() or latest.stat().st_size == 0:
            return
        date = _utc_date(latest.stat().st_mtime)
        n = 1
        while (self.folder / f"{date}-{n}.log.gz").exists():
            n += 1
        for suffix in ("log", "jsonl"):
            source = self.folder / f"latest.{suffix}"
            if source.exists():
                with open(source, "rb") as raw, gzip.open(self.folder / f"{date}-{n}.{suffix}.gz", "wb") as packed:
                    shutil.copyfileobj(raw, packed)
                source.unlink()

    def write(self, record: dict):
        with self.lock:
            if self.text is None and self.closed:
                return  # a late record after shutdown
            if self.opened_date != _utc_date(record["ts"]) or \
                    (self.text and self.text.tell() > self.rotate_bytes):
                self._close()
                self.roll()
                self._open()
            self.text.write(text_line(record) + "\n")
            self.json.write(json.dumps(record, separators=(",", ":"), default=str) + "\n")
            self.text.flush()
            self.json.flush()

    def close(self):
        with self.lock:
            self.closed = True
            self._close()


class LogHub:
    """Routes structured records to the root pair, the module's pair, the job's log and stdout."""

    def __init__(self, state_dir: Path, *, rotate_mb: int = 64, file_level: str = "DEBUG",
                 stdout_level: str = "INFO", color: bool = True, stream=None):
        self.base = Path(state_dir) / "logs"
        self.rotate = rotate_mb * 1024 * 1024
        self.file_level = LEVELS[file_level]
        self.stdout_level = LEVELS[stdout_level]
        self.stream = stream if stream is not None else sys.stdout
        self.color = color and hasattr(self.stream, "isatty") and self.stream.isatty()
        self.root = RollingPair(self.base, self.rotate)
        self.modules: dict[str, RollingPair] = {}
        self.jobs_dir = Path(state_dir) / "jobs"
        self.lock = threading.Lock()
        self.listeners: list = []

    def module_pair(self, module: str) -> RollingPair:
        with self.lock:
            pair = self.modules.get(module)
            if pair is None:
                folder = self.base / ("host" if module == "host" else f"modules/{module}")
                pair = self.modules[module] = RollingPair(folder, self.rotate)
            return pair

    def emit(self, level: str, msg: str, *, module: str | None = None, job: str | None = None,
             event: str | None = None, ts: float | None = None, **fields):
        record = {"ts": ts or time.time(), "level": level.upper(), "module": module or "core", "job": job,
                  "event": event, "msg": REDACT(str(msg))}
        if fields:
            record["fields"] = json.loads(REDACT(json.dumps(fields, default=str)))
        numeric = LEVELS.get(record["level"], logging.INFO)
        if numeric >= self.file_level:
            self.root.write(record)
            if module and module != "core":
                self.module_pair(module).write(record)
            if job:
                folder = self.jobs_dir / job
                folder.mkdir(parents=True, exist_ok=True)
                with open(folder / "job.log", "a", encoding="utf-8") as handle:
                    handle.write(text_line(record) + "\n")
        if numeric >= self.stdout_level:
            line = text_line(record)
            if self.color:
                colour = {"DEBUG": "2", "INFO": "0", "WARNING": "33", "ERROR": "31"}.get(record["level"], "0")
                line = f"\033[{colour}m{line}\033[0m"
            try:
                self.stream.write(line + "\n")
                self.stream.flush()
            except (OSError, ValueError):
                pass
        for listener in list(self.listeners):
            try:
                listener(record)
            except Exception:  # noqa: BLE001 - a listener never breaks logging
                pass
        return record

    def info(self, msg, **kw):
        return self.emit("INFO", msg, **kw)

    def debug(self, msg, **kw):
        return self.emit("DEBUG", msg, **kw)

    def warning(self, msg, **kw):
        return self.emit("WARNING", msg, **kw)

    def error(self, msg, **kw):
        return self.emit("ERROR", msg, **kw)

    def tail(self, module: str | None = None, job: str | None = None, limit: int = 200) -> list[dict]:
        if job:
            path = self.jobs_dir / job / "job.log"
            lines = path.read_text(encoding="utf-8").splitlines()[-limit:] if path.exists() else []
            return [{"line": line} for line in lines]
        folder = self.base if not module else (self.base / ("host" if module == "host" else f"modules/{module}"))
        path = folder / "latest.jsonl"
        if not path.exists():
            return []
        with open(path, encoding="utf-8") as handle:
            lines = handle.readlines()[-limit:]
        return [json.loads(line) for line in lines if line.strip()]

    def close(self):
        self.root.close()
        for pair in self.modules.values():
            pair.close()


def prune(state_dir: Path, days: int, dry_run: bool = True) -> list[str]:
    """Rolled log archives older than ``days``; deleted unless ``dry_run``."""
    cutoff = time.time() - days * 86400
    found = []
    for path in (Path(state_dir) / "logs").rglob("*.gz"):
        if path.stat().st_mtime < cutoff:
            found.append(str(path))
            if not dry_run:
                os.unlink(path)
    return found
