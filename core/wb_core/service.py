"""The Workbench service: registry, promises (jobs), quotes and approval, scheduling and execution.

Lifecycle (spec 6.1): submit → validate → (spends: quoting → awaiting_approval → approve) → queued →
running → settled | cancelled | interrupted. Every valid POST creates a job (S5); one that cannot run
settles at once with ``usage`` or ``environment`` and the reasons.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import secrets as pysecrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__, types
from .db import Database, dumps
from .logs import REDACT, LogHub
from .manifest import Command, Module
from .module import sha256_tree
from .paths import PathError, PathMap
from .requirements import Activation, Checker, Reason
from .settings import Settings

SCHEMA = "wb/result/v1"
TERMINAL = {"settled", "cancelled", "interrupted"}
WAKE_STATES = TERMINAL | {"awaiting_approval"}
CODES = {"ok": 0, "finding": 1, "usage": 2, "environment": 3}
LOCAL_MACHINES = {"container", "host"}  # one physical machine; a rental is another
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
REPO_ROOT = Path(__file__).resolve().parents[2]


def new_id(prefix: str) -> str:
    """Time-ordered ids: 10 characters of milliseconds, 12 random, Crockford base32."""
    stamp = int(time.time() * 1000)
    head = "".join(_CROCKFORD[(stamp >> (5 * i)) & 31] for i in reversed(range(10)))
    tail = "".join(pysecrets.choice(_CROCKFORD) for _ in range(12))
    return f"{prefix}_{head}{tail}"


def iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts)) + f".{int(ts * 1000) % 1000:03d}Z"


class NotFound(Exception):
    pass


class Conflict(Exception):
    def __init__(self, message: str, envelope: dict | None = None):
        super().__init__(message)
        self.envelope = envelope


class BadRequest(Exception):
    pass


@dataclass
class Entry:
    module: Module
    command: Command
    activation: Activation


@dataclass
class Running:
    job: str
    machine: str
    cpu: float
    mem: float
    exclusive: set[str]
    quiet: bool
    process: subprocess.Popen | None = None
    host_run: str | None = None
    cancel_requested: float | None = None
    thread: threading.Thread | None = None


@dataclass
class Collected:
    measurements: list = field(default_factory=list)
    artifacts: list = field(default_factory=list)
    phases: dict = field(default_factory=dict)
    cost: dict | None = None
    machine: dict = field(default_factory=dict)
    toolchain: dict = field(default_factory=dict)
    children: list = field(default_factory=list)
    findings: list = field(default_factory=list)
    last_phase: tuple | None = None


class Workbench:
    def __init__(self, settings: Settings, modules: list[Module], *, hub: LogHub | None = None,
                 checker: Checker | None = None, host_client=None, worker_python: str | None = None):
        self.settings = settings
        self.modules = {m.name: m for m in modules}
        self.state = settings.state_dir
        self.state.mkdir(parents=True, exist_ok=True)
        core = settings.core
        self.hub = hub or LogHub(self.state, rotate_mb=core["logging"]["rotate_mb"],
                                 file_level=core["logging"]["file_level"],
                                 stdout_level=core["logging"]["stdout_level"], color=core["logging"]["color"])
        self.db = Database(self.state / "db" / "wb.sqlite")
        for module in modules:
            applied = self.db.migrate_dir(module.name, module.migrations)
            if applied:
                self.hub.info(f"applied {applied} migration(s)", module=module.name)
        self.host = host_client
        self.checker = checker or Checker(settings, host_probe=self._probe_host if host_client else None)
        self.paths = PathMap(settings.roots)
        self.host = host_client
        self.python = worker_python or sys.executable
        self.cond = threading.Condition()
        self.running: dict[str, Running] = {}
        self.quoting: set[str] = set()
        self.seq: dict[str, int] = {}
        self.stopping = threading.Event()
        self.cpu_budget = core["budget"].get("cpu_cores") or float(os.cpu_count() or 2)
        self.mem_budget = core["budget"].get("mem_gb") or _total_memory_gb()
        self.image_digest = os.environ.get("WB_IMAGE_DIGEST")
        self.install_id = _install_id(self.state)
        self.started = time.time()
        self.registry: dict[str, Entry] = {}
        self.aliases: dict[str, str] = {}
        self.reloader = None  # the daemon's settings loader, for reload_config
        self._digests: dict = {}  # input files' digests by path, size and mtime, so a cached command re-reads no data
        self._build_registry()
        self._redact_secrets()
        self.threads: list[threading.Thread] = []

    def _probe_host(self, settings) -> str | None:
        try:
            self.host.health()
            return None
        except Exception as error:  # noqa: BLE001
            return f"wb-host does not answer: {error}"

    # ------------------------------------------------------------------------------------------
    # registry
    def _build_registry(self):
        active_modules = set()
        # Two passes so `module` requirements see which modules have any active command.
        for _ in range(2):
            self.checker.active_modules = active_modules
            registry = {}
            for module in self.modules.values():
                for command in module.commands:
                    activation = self.checker.activation(module, command, volatile=False)
                    registry[command.full] = Entry(module, command, activation)
            active_modules = {e.module.name for e in registry.values() if e.activation.active}
        self.registry = registry
        self.aliases = {}
        for name, entry in registry.items():
            for alias in entry.command.aliases:
                if alias in registry or alias in self.aliases:
                    raise ValueError(f"alias {alias} of {name} names another command too")
                self.aliases[alias] = name

    # The parts of the settings the running service was built around: a change to one needs a restart.
    FIXED = ("api", "host", "state", "logging", "config", "image", "jdks")

    def reload_config(self) -> dict:
        """Re-reads the config folders. Module config, schedules, quotes and the budget apply to jobs that start from
        now on (a running job keeps the config it started with), and activation is checked again; what the service
        was built around (its API, host runner, state, logs, config layers, mounted roots and secrets) needs a
        restart, so a change there is refused and nothing changes."""
        if self.reloader is None:
            raise BadRequest("this service was started without its config loader, so it cannot reload")
        fresh = self.reloader()
        changed = [f"core.{key}" for key in self.FIXED if fresh.core.get(key) != self.settings.core.get(key)]
        if [r.__dict__ for r in fresh.roots] != [r.__dict__ for r in self.settings.roots]:
            changed.append("paths.roots")
        if fresh.secrets != self.settings.secrets:
            changed.append("secrets")
        if changed:
            raise BadRequest(f"{', '.join(changed)} changed, which needs `wb restart`; nothing was reloaded")
        with self.cond:
            self.settings = fresh
            self.cpu_budget = fresh.core["budget"].get("cpu_cores") or float(os.cpu_count() or 2)
            self.mem_budget = fresh.core["budget"].get("mem_gb") or _total_memory_gb()
            self._build_registry()
            self.cond.notify_all()
        active = sum(e.activation.active for e in self.registry.values())
        self.hub.info(f"config reloaded: {active} of {len(self.registry)} commands active")
        return {"reloaded": True, "active": active, "commands": len(self.registry)}

    def _redact_secrets(self):
        for name in self.settings.secrets:
            path = self.settings.secret_path(name)
            try:
                if path and path.is_file() and path.stat().st_size < 65536:
                    REDACT.add(path.read_text(encoding="utf-8", errors="replace").strip())
            except OSError:
                pass

    def describe_commands(self) -> list[dict]:
        out = []
        for name in sorted(self.registry):
            entry = self.registry[name]
            item = entry.command.describe()
            item["active"] = entry.activation.active
            item["reasons"] = [r.as_dict() for r in entry.activation.reasons]
            out.append(item)
        return out

    def entry(self, name: str) -> Entry:
        entry = self.registry.get(name) or self.registry.get(self.aliases.get(name, ""))
        if entry is None:
            raise NotFound(f"unknown command {name!r} (see GET /v1/commands)")
        return entry

    def schema(self) -> dict:
        envelope = json.loads((REPO_ROOT / "schema" / "envelope.schema.json").read_text(encoding="utf-8")) \
            if (REPO_ROOT / "schema" / "envelope.schema.json").exists() else {}
        return {"$schema": "https://json-schema.org/draft/2020-12/schema", "api": "wb/v1", "version": __version__,
                "envelope": envelope,
                "commands": {name: {"params": e.command.describe()["params"], "result": e.command.describe()["result"],
                                    "spends": e.command.spends, "deterministic": e.command.deterministic,
                                    "summary": e.command.summary}
                             for name, e in sorted(self.registry.items())}}

    # ------------------------------------------------------------------------------------------
    # events
    def add_event(self, job: str, kind: str, data: dict, ts: float | None = None) -> int:
        with self.cond:
            seq = self.seq.get(job)
            if seq is None:
                row = self.db.one("SELECT MAX(seq) AS s FROM job_event WHERE job = ?", (job,))
                seq = (row["s"] or 0) if row else 0
            seq += 1
            self.seq[job] = seq
            self.db.execute("INSERT INTO job_event VALUES (?, ?, ?, ?, ?)",
                            (job, seq, ts or time.time(), kind, dumps(data)))
            self.cond.notify_all()
        return seq

    def events(self, job: str, after: int = 0) -> list[dict]:
        rows = self.db.query("SELECT seq, ts, kind, data FROM job_event WHERE job = ? AND seq > ? ORDER BY seq",
                             (job, after))
        return [{"id": r["seq"], "ts": iso(r["ts"]), "kind": r["kind"], "data": json.loads(r["data"])} for r in rows]

    def _set_state(self, job: str, state: str, **columns):
        sets = ", ".join(["state = ?"] + [f"{k} = ?" for k in columns])
        with self.cond:
            self.db.execute(f"UPDATE job SET {sets} WHERE id = ?", (state, *columns.values(), job))
            self.cond.notify_all()
        self.add_event(job, "state", {"state": state})

    # ------------------------------------------------------------------------------------------
    # submission
    def submit(self, command: str, params: dict | None, *, out: str | None = None, fresh: bool = False,
               path_style: str = "windows", idempotency_key: str | None = None, source: str = "api",
               parent: str | None = None) -> dict:
        if path_style not in ("windows", "posix"):
            raise BadRequest("path_style must be windows or posix")
        if idempotency_key:
            row = self.db.one("SELECT id FROM job WHERE idempotency_key = ?", (idempotency_key,))
            if row:
                return self.envelope(row["id"])
        entry = self.entry(command)
        command = entry.command.full  # a former name resolves to its command; the job records the current one
        params = params or {}
        if not isinstance(params, dict):
            raise BadRequest("params must be an object")
        job = new_id("j")
        now = time.time()
        machine = "container" if "container" in entry.command.runs_on else entry.command.runs_on[0]
        if parent and self.db.one("SELECT id FROM job WHERE id = ?", (parent,)) is None:
            parent = None
        self.db.execute(
            "INSERT INTO job (id, command, module, command_version, module_version, params, state, machine, "
            "out_dir, path_style, fresh, idempotency_key, source, submitted, parent) VALUES "
            "(?, ?, ?, ?, ?, ?, 'new', ?, ?, ?, ?, ?, ?, ?, ?)",
            (job, command, entry.module.name, entry.command.version, entry.module.version, dumps(params), machine,
             out, path_style, int(bool(fresh)), idempotency_key, "child" if parent else source, now, parent))
        reasons = []
        if not entry.activation.active:
            self._settle(job, "environment", f"{command} is not active", reasons=entry.activation.reasons)
            return self.envelope(job)
        try:
            checked = types.validate(entry.command.params, params)
        except types.ValueError_ as error:
            reasons.append(Reason("param", error.field, error.detail, f"see `wb help {command}`"))
            self._settle(job, "usage", f"invalid parameter {error.field}: {error.detail}", reasons=reasons)
            return self.envelope(job)
        try:
            self._translate(entry.command, checked, out)
        except PathError as error:
            self._settle(job, "usage", str(error), reasons=[Reason("path", "path", str(error),
                                                                   "add a root under [paths.roots]")])
            return self.envelope(job)
        self.db.execute("UPDATE job SET params_hash = ? WHERE id = ?",
                        ("sha256:" + hashlib.sha256(dumps(checked).encode()).hexdigest(), job))
        if entry.command.spends:
            if source == "schedule":
                self._settle(job, "usage", "a schedule may not run a command that spends")
                return self.envelope(job)
            with self.cond:
                self.quoting.add(job)
        self._set_state(job, "queued")  # validated: from here the dispatcher may take it
        if entry.command.spends:
            self._start_quote(job, entry)
        return self.envelope(job)

    def _translate(self, command: Command, checked: dict, out: str | None) -> tuple[dict, str | None]:
        translated = dict(checked)
        for name, spec in command.params.items():
            if spec["type"] in ("path.in", "path.out") and checked.get(name):
                resolved = self.paths.resolve(checked[name], need_write=spec["type"] == "path.out")
                translated[name] = resolved.container
            elif spec["type"] == "list" and spec.get("items") in ("path.in", {"type": "path.in"}) and checked.get(name):
                translated[name] = [self.paths.resolve(v).container for v in checked[name]]
        out_container = self.paths.resolve(out, need_write=True).container if out else None
        return translated, out_container

    # ------------------------------------------------------------------------------------------
    # quotes and approval
    def _start_quote(self, job: str, entry: Entry):
        with self.cond:
            self.quoting.add(job)
        thread = threading.Thread(target=self._quote, args=(job, entry), name=f"quote-{job}", daemon=True)
        thread.start()

    def _quote(self, job: str, entry: Entry):
        try:
            row = self.db.one("SELECT * FROM job WHERE id = ?", (job,))
            checked = types.validate(entry.command.params, json.loads(row["params"]))
            translated, out = self._translate(entry.command, checked, row["out_dir"])
            result = self._run_worker_sync(job, entry, translated, out, mode="quote", handler=entry.command.quote)
            if result.get("outcome") != "ok" or "quote" not in result:
                self._settle(job, result.get("outcome") if result.get("outcome") in CODES else "environment",
                             result.get("summary") or "no quote could be made")
                return
            self._store_quote(job, entry, result["quote"])
        except Exception as error:  # noqa: BLE001
            self.hub.error(f"quote failed: {error}", job=job, module=entry.module.name)
            self._settle(job, "environment", f"quote failed: {error}")
        finally:
            with self.cond:
                self.quoting.discard(job)
                self.cond.notify_all()

    def _store_quote(self, job: str, entry: Entry, quote: dict, reason: str | None = None):
        ttl = self.settings.core["quotes"]["ttl"]
        quote = dict(quote or {})
        prompt = quote.pop("prompt", None)
        quote_id = new_id("q")
        now = time.time()
        quote.update(id=quote_id, expires_at=iso(now + ttl), currency=quote.get("currency", "USD"))
        if prompt is None:
            prompt = default_prompt(entry.command, quote)
        self.db.execute("UPDATE quote SET status = 'replaced' WHERE job = ? AND status = 'open'", (job,))
        self.db.execute("INSERT INTO quote VALUES (?, ?, ?, ?, ?, 'open')",
                        (quote_id, job, now, now + ttl, dumps({"quote": quote, "prompt": prompt})))
        self.add_event(job, "quote", {"quote": quote, "reason": reason})
        self._set_state(job, "awaiting_approval")
        self.hub.info(f"awaiting approval: {prompt.get('question')}", job=job, module=entry.module.name)

    def approve(self, job: str, quote_id: str, via: str = "direct") -> dict:
        if via not in ("direct", "relayed"):
            raise BadRequest("via must be direct or relayed")
        row = self._job_row(job)
        if row["state"] != "awaiting_approval":
            raise Conflict(f"job {job} is {row['state']}, not awaiting approval", self.envelope(job))
        quote = self.db.one("SELECT * FROM quote WHERE job = ? AND status = 'open' ORDER BY created DESC LIMIT 1",
                            (job,))
        if quote is None or quote["id"] != quote_id:
            raise Conflict(f"quote {quote_id} is not the job's current quote", self.envelope(job))
        if time.time() > quote["expires"]:
            self.db.execute("UPDATE quote SET status = 'expired' WHERE id = ?", (quote_id,))
            entry = self.entry(row["command"])
            self._start_quote(job, entry)
            self._set_state(job, "queued")
            raise Conflict(f"quote {quote_id} expired; a new quote is being made", self.envelope(job))
        with self.db.transaction() as connection:
            connection.execute("UPDATE quote SET status = 'approved' WHERE id = ?", (quote_id,))
            connection.execute("INSERT INTO approval VALUES (?, ?, ?, ?)", (job, quote_id, via, time.time()))
        self.add_event(job, "approval", {"quote": quote_id, "via": via})
        self.hub.info(f"approved quote {quote_id} ({via})", job=job, module=row["module"])
        self._set_state(job, "queued")
        return self.envelope(job)

    def approved_quote(self, job: str) -> dict | None:
        row = self.db.one("SELECT q.data FROM approval a JOIN quote q ON q.id = a.quote WHERE a.job = ? "
                          "AND q.status = 'approved' ORDER BY a.ts DESC LIMIT 1", (job,))
        return json.loads(row["data"])["quote"] if row else None

    # ------------------------------------------------------------------------------------------
    # cancel
    def cancel(self, job: str) -> dict:
        row = self._job_row(job)
        state = row["state"]
        if state in TERMINAL:
            return self.envelope(job)
        with self.cond:
            running = self.running.get(job)
        if running is None:
            self.db.execute("UPDATE quote SET status = 'void' WHERE job = ? AND status = 'open'", (job,))
            self._set_state(job, "cancelled", settled=time.time(), summary="cancelled before it ran")
            return self.envelope(job)
        running.cancel_requested = time.time()
        self.add_event(job, "log", {"level": "WARNING", "msg": "cancel requested"})
        if running.process is not None and running.process.poll() is None:
            (self.state / "jobs" / job / "cancel").write_text("cancel", encoding="ascii")
        elif running.host_run and self.host:
            try:
                self.host.cancel(running.host_run)
            except Exception as error:  # noqa: BLE001
                self.hub.warning(f"host cancel failed: {error}", job=job)
        return self.envelope(job)

    # ------------------------------------------------------------------------------------------
    # waiting
    def wait(self, job: str, timeout: float) -> dict:
        deadline = time.monotonic() + max(0.0, timeout)
        with self.cond:
            while True:
                row = self.db.one("SELECT state FROM job WHERE id = ?", (job,))
                if row is None:
                    raise NotFound(f"unknown job {job}")
                if row["state"] in WAKE_STATES and job not in self.quoting:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.cond.wait(min(remaining, 1.0))
        return self.envelope(job)

    def wait_events(self, job: str, after: int, timeout: float) -> list[dict]:
        deadline = time.monotonic() + timeout
        with self.cond:
            while True:
                events = self.events(job, after)
                if events:
                    return events
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return []
                self.cond.wait(min(remaining, 1.0))

    # ------------------------------------------------------------------------------------------
    # scheduling
    def start(self):
        self.recover()
        self.run_startup_hooks()
        for target, name in ((self._dispatch_loop, "dispatch"), (self._schedule_loop, "schedules"),
                             (self._expiry_loop, "quotes")):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self.threads.append(thread)

    def stop(self, timeout: float = 10):
        self.stopping.set()
        with self.cond:
            self.cond.notify_all()
            running = list(self.running.values())
        for item in running:
            self.cancel(item.job)
        end = time.time() + timeout
        for item in running:
            if item.thread:
                item.thread.join(max(0.1, end - time.time()))

    def recover(self):
        """D10: jobs left running by a stopped daemon are interrupted; their workers are killed."""
        for row in self.db.query("SELECT id, worker_pid, module FROM job WHERE state = 'running'"):
            if row["worker_pid"]:
                _kill_pid(row["worker_pid"])
            self.db.execute("UPDATE job SET state = 'interrupted', settled = ?, summary = ? WHERE id = ?",
                            (time.time(), "interrupted by a Workbench restart", row["id"]))
            self.add_event(row["id"], "state", {"state": "interrupted"})
            self.hub.warning("interrupted by a restart", job=row["id"], module=row["module"])
        for row in self.db.query("SELECT j.id, j.command FROM job j WHERE j.state = 'queued' AND j.id IN "
                                 "(SELECT job FROM quote WHERE status IN ('open', 'expired', 'replaced')) "
                                 "AND j.id NOT IN (SELECT job FROM approval)"):
            entry = self.registry.get(row["command"])
            if entry and entry.command.spends:
                self._start_quote(row["id"], entry)

    def run_startup_hooks(self):
        """Module startup hooks run before any new work, e.g. destroying rentals no running job holds."""
        for module in self.modules.values():
            if not module.startup:
                continue
            fake = Entry(module, Command(module=module.name, name="startup", summary="", handler=module.startup,
                                         version=module.version), None)
            job = f"startup-{module.name}"
            try:
                result = self._run_worker_sync(job, fake, {}, None, mode="startup", handler=module.startup,
                                               record=False)
                level = "INFO" if result.get("outcome") == "ok" else "ERROR"
                self.hub.emit(level, f"startup hook: {result.get('summary') or result.get('outcome')}",
                              module=module.name)
            except Exception as error:  # noqa: BLE001
                self.hub.error(f"startup hook failed: {error}", module=module.name)

    def _resources(self, command: Command, machine: str) -> tuple[float, float, set[str], bool]:
        res = command.resources
        cpu = float(res.get("cpu", 1)) if machine == "container" else 0.0
        mem = float(res.get("mem_gb", 0.5)) if machine == "container" else 0.0
        exclusive = {f"gpu@{machine}"} if res.get("gpu") else set()
        return cpu, mem, exclusive, bool(res.get("quiet"))

    def _ancestors(self, job: str) -> set[str]:
        out, current = set(), job
        for _ in range(32):
            row = self.db.one("SELECT parent FROM job WHERE id = ?", (current,))
            if not row or not row["parent"]:
                break
            current = row["parent"]
            out.add(current)
        return out

    def _fits(self, machine: str, cpu: float, mem: float, exclusive: set[str], quiet: bool,
              job: str | None = None) -> bool:
        waiting = self._ancestors(job) if job else set()  # a parent waiting on this child holds nothing back
        others = [r for r in self.running.values() if r.job not in waiting]
        used_cpu = sum(r.cpu for r in others)
        used_mem = sum(r.mem for r in others)
        held = set().union(*[r.exclusive for r in others]) if others else set()
        # The container runs on the host: for a measurement's quiet they are one machine.
        on_machine = [r for r in others if r.machine == machine or {r.machine, machine} <= LOCAL_MACHINES]
        if any(r.quiet for r in on_machine):
            return False
        if quiet and on_machine:
            return False
        if exclusive & held:
            return False
        if machine == "container" and others:
            if used_cpu + cpu > self.cpu_budget + 1e-9 or used_mem + mem > self.mem_budget + 1e-9:
                return False
        return True

    def _dispatch_loop(self):
        while not self.stopping.is_set():
            with self.cond:
                self.cond.wait(0.5)
                if self.stopping.is_set():
                    return
                rows = self.db.query("SELECT id, command, machine FROM job WHERE state = 'queued' ORDER BY submitted")
                blocked: set[str] = set()
                for row in rows:
                    if row["id"] in self.quoting or row["id"] in self.running:
                        continue
                    entry = self.registry.get(row["command"])
                    if entry is None:
                        continue
                    if entry.command.spends and self.approved_quote(row["id"]) is None:
                        continue
                    cpu, mem, exclusive, quiet = self._resources(entry.command, row["machine"])
                    key = row["machine"] + ("|" + ",".join(sorted(exclusive)) if exclusive else "")
                    if key in blocked:
                        continue  # first in, first out per resource
                    if not self._fits(row["machine"], cpu, mem, exclusive, quiet, row["id"]):
                        blocked.add(key)
                        continue
                    running = Running(row["id"], row["machine"], cpu, mem, exclusive, quiet)
                    self.running[row["id"]] = running
                    running.thread = threading.Thread(target=self._run_job, args=(row["id"], entry, running),
                                                      name=f"job-{row['id']}", daemon=True)
                    running.thread.start()

    def _schedule_loop(self):
        due: dict[str, float] = {}
        for name, schedule in self.settings.schedules.items():
            if schedule["enabled"]:
                due[name] = time.time() + 5
        while not self.stopping.wait(2):
            for name, schedule in self.settings.schedules.items():
                if not schedule["enabled"] or time.time() < due.get(name, 0):
                    continue
                due[name] = time.time() + schedule["every"]
                command = schedule["command"]
                entry = self.registry.get(command)
                if entry is None or entry.command.spends or not entry.command.schedulable:
                    self.hub.error(f"schedule {name}: {command} cannot be scheduled")
                    continue
                busy = self.db.one("SELECT id FROM job WHERE command = ? AND source = 'schedule' AND state IN "
                                   "('queued', 'running')", (command,))
                if busy:
                    continue  # one at a time
                envelope = self.submit(command, schedule["params"], source="schedule")
                self.hub.info(f"schedule {name}: submitted {command}", job=envelope["job"])

    def _expiry_loop(self):
        while not self.stopping.wait(5):
            now = time.time()
            for row in self.db.query("SELECT q.id, q.job FROM quote q JOIN job j ON j.id = q.job "
                                     "WHERE q.status = 'open' AND q.expires < ? AND j.state = 'awaiting_approval'",
                                     (now,)):
                self.db.execute("UPDATE quote SET status = 'expired' WHERE id = ?", (row["id"],))
                self.add_event(row["job"], "log", {"level": "INFO", "msg": f"quote {row['id']} expired"})

    # ------------------------------------------------------------------------------------------
    # running one job
    def _spec(self, job: str, entry: Entry, params: dict, out: str | None, mode: str, handler: str,
              quote: dict | None, path_style: str = "windows") -> dict:
        work = self.state / "jobs" / job
        module = entry.module
        sys_path = list(dict.fromkeys(str(m.directory.parent) for m in [module, *self.modules.values()]
                                      if (m.directory.parent / m.package).exists()))
        sys_path.append(str(Path(__file__).resolve().parents[1]))
        sys_path.append(str(REPO_ROOT / "sdk" / "python"))
        secrets = {}
        for name in self.settings.secrets:
            path = self.settings.secret_path(name)
            if path:
                secrets[name] = str(path)
        return {"job": job, "command": entry.command.full, "module": module.name, "handler": handler, "mode": mode,
                "params": params, "work": str(work / "work"), "out": out or str(work / "out"),
                "module_config": self.settings.modules.get(module.name, {}), "secrets": secrets,
                "jdks": self.checker.jdks, "quote": quote, "machine": "container", "path_style": path_style,
                "roots": [r.__dict__ for r in self.settings.roots], "sys_path": sys_path,
                "env": {"WB_URL": f"http://127.0.0.1:{self.settings.core['api']['port']}",
                        "WB_INSTALL_ID": self.install_id,
                        "WB_TOKEN": self._token(), "WB_JOB": job, "WB_STATE_DIR": str(self.state)}}

    def _token(self) -> str:
        path = Path(self.settings.core["api"]["token_file"])
        try:
            return path.read_text(encoding="ascii").strip() if path.is_file() else os.environ.get("WB_TOKEN", "")
        except OSError:
            return ""

    def _spawn(self, spec: dict) -> subprocess.Popen:
        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONPATH"] = os.pathsep.join(spec["sys_path"] + [env.get("PYTHONPATH", "")]).strip(os.pathsep)
        folder = Path(spec["work"]).parent
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "cancel").unlink(missing_ok=True)
        process = subprocess.Popen([self.python, "-m", "wb_core.worker"], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                   errors="replace", env=env, cwd=str(folder))
        process.stdin.write(json.dumps(spec) + "\n")
        process.stdin.flush()
        process.stdin.close()  # the worker never reads stdin again; cancelling uses the cancel file
        return process

    def _run_worker_sync(self, job: str, entry: Entry, params: dict, out: str | None, *, mode: str,
                         handler: str, record: bool = True) -> dict:
        Path(self.state / "jobs" / job).mkdir(parents=True, exist_ok=True)
        process = self._spawn(self._spec(job, entry, params, out, mode, handler, None))
        result: dict = {}
        stderr = threading.Thread(target=self._pump_stderr, args=(process, job, entry.module.name), daemon=True)
        stderr.start()
        for line in process.stdout:
            event = _parse(line)
            if event is None:
                continue
            kind = event.get("kind")
            if kind == "log":
                self.hub.emit(event.get("level", "INFO"), event.get("msg", ""), module=entry.module.name,
                              job=job if record else None)
            elif kind == "quote":
                result["quote"] = event.get("quote")
            elif kind == "done":
                result.update(outcome=event.get("outcome"), summary=event.get("summary"), data=event.get("data"))
        process.wait()
        stderr.join(2)
        if "outcome" not in result:
            result.update(outcome="environment", summary=f"worker exited {process.returncode} without a result")
        return result

    def _pump_stderr(self, process, job, module):
        for line in process.stderr:
            line = line.rstrip("\n")
            if line:
                self.hub.emit("DEBUG", line, module=module, job=job if not job.startswith("startup-") else None)

    def _run_job(self, job: str, entry: Entry, running: Running):
        command = entry.command
        collected = Collected()
        outcome, summary, data, reasons = "environment", None, {}, []
        requoted = False
        try:
            row = self._job_row(job)
            params = types.validate(command.params, json.loads(row["params"]))
            translated, out = self._translate(command, params, row["out_dir"])
            problems = self.checker.volatile(entry.module, command)
            if problems:
                outcome, summary, reasons = "environment", problems[0].detail, problems
                return
            inputs = self._inputs(command, params, translated)
            cache_key = self._cache_key(command, params, inputs) if command.deterministic else None
            if cache_key:
                self.db.execute("UPDATE job SET cache_key = ? WHERE id = ?", (cache_key, job))
                if not row["fresh"] and self._serve_cache(job, cache_key, out):
                    outcome = None  # settled by the cache
                    return
            self.db.execute("UPDATE job SET inputs = ? WHERE id = ?", (dumps(inputs), job))
            self._set_state(job, "running", started=time.time())
            self.hub.info(f"running {command.full}", job=job, module=entry.module.name)
            quote = self.approved_quote(job) if command.spends else None
            if running.machine == "host":
                result = self._execute_host(job, entry, params, out, quote, row["path_style"], running, collected)
            else:
                result = self._execute_local(job, entry, translated, out, quote, row["path_style"], running,
                                             collected)
            if result.get("requote"):
                requoted = True
                self._store_quote(job, entry, result["requote"], reason=result.get("reason"))
                outcome = None
                return
            outcome = result.get("outcome", "environment")
            summary = result.get("summary")
            data = result.get("data") or {}
            if outcome == "ok" and command.result:
                try:
                    types.validate({k: dict(v, optional=True) if "default" not in v else v
                                    for k, v in command.result.items()},
                                   {k: v for k, v in data.items() if k in command.result})
                except types.ValueError_ as error:
                    outcome, summary = "environment", f"handler result does not match its schema: {error}"
        except Exception as error:  # noqa: BLE001
            self.hub.error(f"job failed in the core: {error}", job=job, module=entry.module.name)
            outcome, summary = "environment", f"{type(error).__name__}: {error}"
        finally:
            with self.cond:
                self.running.pop(job, None)
                self.cond.notify_all()
            if outcome is not None and not requoted:
                if outcome == "cancelled":
                    self._finish(job, "cancelled", None, summary or "cancelled", data, collected, reasons)
                else:
                    self._finish(job, "settled", outcome, summary, data, collected, reasons)

    def _execute_local(self, job, entry, params, out, quote, path_style, running: Running, collected) -> dict:
        spec = self._spec(job, entry, params, out, "run", entry.command.handler, quote, path_style)
        process = self._spawn(spec)
        running.process = process
        self.db.execute("UPDATE job SET worker_pid = ? WHERE id = ?", (process.pid, job))
        stderr = threading.Thread(target=self._pump_stderr, args=(process, job, entry.module.name), daemon=True)
        stderr.start()
        watchdog = threading.Thread(target=self._watchdog, args=(job, entry.command, running), daemon=True)
        watchdog.start()
        result: dict = {}
        for line in process.stdout:
            event = _parse(line)
            if event is not None:
                self._absorb(job, entry, event, collected, result)
        process.wait()
        stderr.join(5)
        if not result:
            result = {"outcome": "environment", "summary": f"worker exited {process.returncode} without a result"}
        if running.cancel_requested and result.get("outcome") != "cancelled" and process.returncode != 0:
            result = {"outcome": "cancelled", "summary": "cancelled"}
        return result

    def _watchdog(self, job: str, command: Command, running: Running):
        started = time.time()
        while running.process and running.process.poll() is None:
            time.sleep(1)
            if command.timeout and time.time() - started > command.timeout and not running.cancel_requested:
                self.hub.warning(f"timeout after {command.timeout:.0f} s; cancelling", job=job)
                self.cancel(job)
            if running.cancel_requested and time.time() - running.cancel_requested > command.cancel_grace:
                self.hub.warning("cancel grace over; killing the worker", job=job)
                _kill_tree(running.process)
                return

    def _execute_host(self, job, entry, params, out, quote, path_style, running: Running, collected) -> dict:
        if self.host is None:
            return {"outcome": "environment", "summary": "no host runner is configured"}
        host_params = dict(params)
        for name, spec in entry.command.params.items():
            if spec["type"] in ("path.in", "path.out") and host_params.get(name):
                host_params[name] = self.paths.to_host(self.paths.resolve(host_params[name]).container, "windows")
        host_out = self.paths.to_host(out, "windows") if out else None
        run_id = self.host.start(entry.command.full, {"job": job, "params": host_params, "out": host_out,
                                                       "module_config": self.settings.modules.get(entry.module.name, {}),
                                                       "quote": quote})
        running.host_run = run_id
        result: dict = {}
        after = 0
        last_contact = time.time()
        timeout = self.settings.core["host"]["heartbeat_timeout"]
        while True:
            try:
                batch = self.host.events(run_id, after, wait=20)
                last_contact = time.time()
            except Exception as error:  # noqa: BLE001
                if time.time() - last_contact > timeout:
                    return {"outcome": "environment", "summary": f"host lost: {error}"}
                time.sleep(2)
                continue
            for event in batch.get("events", []):
                after = max(after, event.get("seq", after))
                self._absorb(job, entry, event, collected, result)
            if batch.get("finished") and result:
                return result
            if batch.get("finished"):
                return {"outcome": "environment", "summary": "host run ended without a result"}
            if time.time() - batch.get("heartbeat", time.time()) > timeout:
                return {"outcome": "environment", "summary": "host lost: no heartbeat"}

    def _absorb(self, job: str, entry: Entry, event: dict, collected: Collected, result: dict):
        kind = event.get("kind")
        ts = event.get("ts") or time.time()
        module = entry.module.name
        if kind == "log":
            level = event.get("level", "INFO")
            self.hub.emit(level, event.get("msg", ""), module=module, job=job, **(event.get("fields") or {}))
            if level != "DEBUG":
                self.add_event(job, "log", {"level": level, "msg": REDACT(event.get("msg", ""))}, ts)
        elif kind == "progress":
            phase = event.get("phase")
            if collected.last_phase and collected.last_phase[0] != phase:
                previous, began = collected.last_phase
                collected.phases[previous] = round(collected.phases.get(previous, 0) + ts - began, 3)
            if not collected.last_phase or collected.last_phase[0] != phase:
                collected.last_phase = (phase, ts)
            self.add_event(job, "progress", {k: event.get(k) for k in ("phase", "fraction", "msg")}, ts)
            if event.get("msg"):
                self.hub.info(f"{phase}: {event['msg']}", module=module, job=job)
        elif kind == "measurement":
            item = {k: event.get(k) for k in ("name", "value", "unit", "labels")}
            collected.measurements.append(item)
            self.add_event(job, "measurement", item, ts)
        elif kind == "artifact":
            item = {k: event.get(k) for k in ("name", "path", "sha256", "bytes", "media_type", "files")}
            collected.artifacts.append(item)
            self.add_event(job, "artifact", {k: item[k] for k in ("name", "sha256", "bytes")}, ts)
        elif kind == "finding":
            collected.findings.append(event.get("summary"))
            self.add_event(job, "finding", {"summary": event.get("summary")}, ts)
        elif kind == "cost":
            collected.cost = {k: v for k, v in event.items() if k not in ("kind", "ts")}
            self.add_event(job, "cost", collected.cost, ts)
        elif kind == "machine":
            collected.machine.update({k: v for k, v in event.items() if k not in ("kind", "ts")})
        elif kind == "toolchain":
            collected.toolchain.update({k: v for k, v in event.items() if k not in ("kind", "ts")})
        elif kind == "child":
            collected.children.append(event.get("job"))
            self.add_event(job, "child", {"job": event.get("job"), "command": event.get("command")}, ts)
        elif kind == "requote":
            result.update(requote=event.get("quote"), reason=event.get("reason"))
        elif kind == "done":
            if collected.last_phase:
                phase, began = collected.last_phase
                collected.phases[phase] = round(collected.phases.get(phase, 0) + ts - began, 3)
                collected.last_phase = None
            result.update(outcome=event.get("outcome"), summary=event.get("summary"), data=event.get("data"))

    # ------------------------------------------------------------------------------------------
    # inputs, cache, artifacts
    def _inputs(self, command: Command, params: dict, translated: dict) -> list[dict]:
        inputs = []
        for name, spec in command.params.items():
            if spec["type"] == "path.in" and translated.get(name):
                inputs.append(self._input(name, spec, params[name], Path(translated[name])))
            elif spec["type"] == "list" and spec.get("items") in ("path.in", {"type": "path.in"}) \
                    and translated.get(name):
                # Each path of a list is an input too: a cached result must not outlive a change to any of them.
                for index, (host, container) in enumerate(zip(params[name], translated[name])):
                    inputs.append(dict(self._input(name, {}, host, Path(container)), index=index))
        return inputs

    def _input(self, name: str, spec: dict, host_path: str, path: Path) -> dict:
        if not path.exists():
            raise FileNotFoundError(f"input {name} does not exist: {host_path}")
        if spec.get("digest") == "git":
            # A checkout's files, build output and caches included, can take hours to hash through a mount;
            # the command reads it through git and reports the commit or tree it read.
            return {"name": name, "host_path": host_path, "head": _git_head(path)}
        if len(self._digests) > 500_000:  # a bound, not an eviction policy: rebuilt as jobs read files
            self._digests.clear()
        digest, count = sha256_tree(path, self._digests)
        return {"name": name, "host_path": host_path, "sha256": digest, "files": count}

    def _cache_key(self, command: Command, params: dict, inputs: list[dict]) -> str:
        hashed = dict(params)
        lists: dict[str, list[str]] = {}
        for item in inputs:  # a deterministic command's inputs are hashed by content (manifest.py)
            if "index" in item:  # a list of paths: its digests, in order, wherever the files are
                lists.setdefault(item["name"], []).append("sha256:" + item["sha256"])
            else:
                hashed[item["name"]] = "sha256:" + item["sha256"]
        hashed.update(lists)
        # The module's config reaches the handler as ctx.config, so a result depends on it as on its params.
        material = {"command": command.full, "version": command.version, "params": hashed,
                    "config": self.settings.modules.get(command.module, {}),
                    "image": self.image_digest, "python": platform.python_version()}
        return "sha256:" + hashlib.sha256(dumps(material).encode()).hexdigest()

    def _serve_cache(self, job: str, key: str, out: str | None) -> bool:
        row = self.db.one("SELECT c.job FROM cache c JOIN job j ON j.id = c.job WHERE c.key = ? AND "
                          "j.state = 'settled'", (key,))
        if row is None:
            return False
        earlier = json.loads(self.db.one("SELECT envelope FROM job WHERE id = ?", (row["job"],))["envelope"])
        artifacts = []
        for artifact in self.db.query("SELECT * FROM artifact WHERE job = ?", (row["job"],)):
            source = Path(artifact["path"])
            if not source.exists():
                return False  # pruned: run again
            target = source
            if out:
                target = Path(out) / source.name
                target.parent.mkdir(parents=True, exist_ok=True)
                if source.is_dir():
                    shutil.copytree(source, target, dirs_exist_ok=True)
                else:
                    shutil.copy2(source, target)
            artifacts.append({"name": artifact["name"], "path": str(target), "sha256": artifact["sha256"],
                              "bytes": artifact["bytes"], "media_type": artifact["media_type"]})
        collected = Collected(measurements=earlier.get("measurements", []), artifacts=artifacts,
                              machine=earlier.get("machine") or {}, toolchain=earlier.get("toolchain") or {})
        self._finish(job, "settled", earlier["outcome"], earlier.get("summary"), earlier.get("data") or {},
                     collected, [], cache_hit=row["job"])
        return True

    def _store(self, job: str, artifacts: list[dict], out_requested: bool):
        for item in artifacts:
            path = Path(item["path"])
            stored = 0
            if path.is_file() and not out_requested:
                target = self.state / "store" / "sha256" / item["sha256"][:2] / item["sha256"]
                if not target.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        os.link(path, target)
                    except OSError:
                        shutil.copy2(path, target)
                stored = 1
            self.db.execute("INSERT OR REPLACE INTO artifact (job, name, sha256, bytes, media_type, path, stored) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?)", (job, item["name"], item["sha256"], item["bytes"],
                                                            item.get("media_type"), str(path), stored))

    # ------------------------------------------------------------------------------------------
    # settling and the envelope
    def _settle(self, job: str, outcome: str, summary: str | None, *, reasons=None, data=None):
        self._finish(job, "settled", outcome, summary, data or {}, Collected(), reasons or [])

    def _finish(self, job: str, state: str, outcome: str | None, summary: str | None, data: dict,
                collected: Collected, reasons: list, cache_hit: str | None = None):
        if outcome not in CODES and state == "settled":
            outcome = "environment"
        row = self._job_row(job)
        self._store(job, collected.artifacts, bool(row["out_dir"]))
        extra = {"measurements": collected.measurements, "phases": collected.phases, "cost": collected.cost,
                 "machine": collected.machine, "toolchain": collected.toolchain, "children": collected.children,
                 "findings": collected.findings, "data": data,
                 "reasons": [r.as_dict() if isinstance(r, Reason) else r for r in reasons],
                 "cache_hit": cache_hit}
        code = CODES.get(outcome) if state == "settled" else None
        with self.cond:
            self.db.execute("UPDATE job SET state = ?, outcome = ?, code = ?, summary = ?, settled = ?, "
                            "envelope = ? WHERE id = ?",
                            (state, outcome if state == "settled" else None, code, REDACT(summary or "") or None,
                             time.time(), dumps(extra), job))
            self.cond.notify_all()
        envelope = self.envelope(job)
        self.db.execute("UPDATE job SET envelope = ? WHERE id = ?", (dumps(dict(envelope, _extra=extra)), job))
        if state == "settled" and row["cache_key"] and outcome in ("ok", "finding") and not cache_hit:
            self.db.execute("INSERT OR REPLACE INTO cache VALUES (?, ?, ?)", (row["cache_key"], job, time.time()))
        self.add_event(job, "state", {"state": state, "outcome": outcome})
        level = "INFO" if outcome in ("ok", None) else "WARNING"
        self.hub.emit(level, f"{state}{' ' + outcome if outcome else ''}: {summary or ''}".rstrip(": "),
                      module=row["module"], job=job)

    def _job_row(self, job: str):
        row = self.db.one("SELECT * FROM job WHERE id = ?", (job,))
        if row is None:
            raise NotFound(f"unknown job {job}")
        return row

    def envelope(self, job: str) -> dict:
        row = self._job_row(job)
        stored = json.loads(row["envelope"]) if row["envelope"] else {}
        extra = stored.get("_extra", stored)
        style = row["path_style"]
        artifacts = self.db.query("SELECT * FROM artifact WHERE job = ? ORDER BY name", (job,))
        outputs = [{"name": a["name"], "host_path": self.paths.to_host(a["path"], style), "sha256": a["sha256"],
                    "bytes": a["bytes"], "media_type": a["media_type"],
                    "url": f"/v1/jobs/{job}/artifacts/{a['name']}"} for a in artifacts]
        approval = None
        quote = self.db.one("SELECT * FROM quote WHERE job = ? ORDER BY created DESC LIMIT 1", (job,))
        if quote is not None:
            body = json.loads(quote["data"])
            given = self.db.one("SELECT via, ts FROM approval WHERE job = ? AND quote = ?", (job, quote["id"]))
            approval = {"quote": body["quote"], "prompt": body["prompt"], "status": quote["status"],
                        "approved": {"via": given["via"], "at": iso(given["ts"])} if given else None}
        inputs = json.loads(row["inputs"]) if row["inputs"] else []
        state = row["state"]
        if job in self.quoting and state == "queued":
            phase = "quoting"
        elif job in self.running and state == "queued":
            phase = "preparing"  # dispatched: digesting its inputs or checking the cache, holding its resources
        else:
            phase = None
        return {
            "schema": SCHEMA,
            "job": job,
            "command": row["command"], "command_version": row["command_version"],
            "module": row["module"], "module_version": row["module_version"],
            "params": json.loads(row["params"]), "params_hash": row["params_hash"],
            "state": state, "phase": phase,
            "outcome": row["outcome"], "code": row["code"],
            "summary": row["summary"],
            "reasons": extra.get("reasons", []),
            "inputs": inputs,
            "outputs": outputs,
            "data": extra.get("data", {}),
            "measurements": extra.get("measurements", []),
            "findings": extra.get("findings", []),
            "machine": dict({"id": row["machine"]}, **(extra.get("machine") or {})),
            "toolchain": dict({"image": self.image_digest, "python": platform.python_version(),
                               "workbench": __version__}, **(extra.get("toolchain") or {})),
            "timings": {"submitted": iso(row["submitted"]), "started": iso(row["started"]),
                        "settled": iso(row["settled"]), "phases": extra.get("phases", {})},
            "cost": extra.get("cost"),
            "cache": {"hit": bool(extra.get("cache_hit")), "from": extra.get("cache_hit"), "key": row["cache_key"]},
            "approval": approval,
            "children": extra.get("children", []),
            "parent": row["parent"],
            "logs": {"job": f"/v1/logs?job={job}"},
        }

    def list_jobs(self, state: str | None = None, command: str | None = None, since: str | None = None,
                  limit: int = 100) -> list[dict]:
        sql, args = "SELECT id FROM job WHERE 1=1", []
        if state:
            sql += " AND state = ?"
            args.append(state)
        if command:
            sql += " AND command = ?"
            args.append(command)
        if since:
            sql += " AND id > ?"
            args.append(since)
        sql += " ORDER BY submitted DESC LIMIT ?"
        args.append(min(int(limit), 1000))
        return [self.envelope(r["id"]) for r in self.db.query(sql, args)]

    def artifact_path(self, job: str, name: str) -> Path:
        row = self.db.one("SELECT path FROM artifact WHERE job = ? AND name = ?", (job, name))
        if row is None:
            raise NotFound(f"job {job} has no artifact {name}")
        path = Path(row["path"])
        if not path.is_file():
            raise NotFound(f"artifact {name} is no longer on disk (pruned?)")
        return path

    # ------------------------------------------------------------------------------------------
    # retention (D26)
    def prune(self, dry_run: bool = True) -> dict:
        from .logs import prune as prune_logs
        days = self.settings.core["retention"]["days"]
        cutoff = time.time() - days * 86400
        work_dirs = []
        pinned = {r["job"] for r in self.db.query("SELECT job FROM artifact WHERE pinned = 1")}
        for row in self.db.query("SELECT id FROM job WHERE state IN ('settled', 'cancelled', 'interrupted') "
                                 "AND settled < ?", (cutoff,)):
            folder = self.state / "jobs" / row["id"]
            if folder.exists() and row["id"] not in pinned:
                work_dirs.append(str(folder))
                if not dry_run:
                    shutil.rmtree(folder, ignore_errors=True)
        store = []
        referenced = {r["sha256"] for r in self.db.query("SELECT sha256 FROM artifact a JOIN job j ON j.id = a.job "
                                                          "WHERE j.settled >= ? OR a.pinned = 1", (cutoff,))}
        for path in (self.state / "store").rglob("*") if (self.state / "store").exists() else []:
            if path.is_file() and path.name not in referenced and path.stat().st_mtime < cutoff:
                store.append(str(path))
                if not dry_run:
                    path.unlink()
        logs = prune_logs(self.state, days, dry_run)
        return {"dry_run": dry_run, "days": days, "job_dirs": work_dirs, "store": store, "logs": logs}


def default_prompt(command: Command, quote: dict) -> dict:
    cost = quote.get("max_cost")
    currency = quote.get("currency", "USD")
    what = quote.get("summary") or command.summary or command.full
    money = f" (at most {cost:.2f} {currency})" if isinstance(cost, (int, float)) else ""
    return {"question": f"Approve {command.full}: {what}{money}?",
            "options": [{"label": "Approve", "description": f"Run {command.full} on quote {quote.get('id')}."},
                        {"label": "Decline", "description": "Cancel the job; nothing is spent."}]}


def _parse(line: str) -> dict | None:
    line = line.strip()
    if not line:
        return None
    try:
        event = json.loads(line)
    except ValueError:
        return {"kind": "log", "level": "DEBUG", "msg": line}
    return event if isinstance(event, dict) else None


def _install_id(state: Path) -> str:
    """This installation's stable id, made once and kept in its state. Anything Workbench creates outside itself
    (a rental's label) carries it, so clean-up never touches another installation's resources."""
    path = state / "install-id"
    try:
        value = path.read_text(encoding="ascii").strip()
        if value:
            return value
    except OSError:
        pass
    value = pysecrets.token_hex(4)
    path.write_text(value, encoding="ascii")
    return value


def _total_memory_gb() -> float:
    try:
        with open("/proc/meminfo", encoding="ascii") as handle:
            for line in handle:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) / 1024 / 1024
    except OSError:
        pass
    return 16.0


def _git_head(path: Path) -> str | None:
    """The commit a checkout's HEAD names; None for a folder that is not one (or a HEAD with no commit yet)."""
    try:
        done = subprocess.run(["git", "-c", "safe.directory=*", "-C", str(path), "rev-parse", "--verify", "-q",
                               "HEAD"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    head = done.stdout.strip()
    return head if done.returncode == 0 and head else None


def _kill_pid(pid: int):
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        else:
            os.kill(pid, 9)
    except (OSError, ProcessLookupError):
        pass


def _kill_tree(process: subprocess.Popen):
    if process.poll() is None:
        _kill_pid(process.pid)
        try:
            process.kill()
        except OSError:
            pass
