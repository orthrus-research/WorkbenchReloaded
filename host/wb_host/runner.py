"""wb-host (spec 12): runs declared host routines natively on Windows for the container, one worker process
per run, holding the GPU lease for routines that need the GPU.

A host routine is a command whose ``runs_on`` includes ``host``. The container sends it with Windows paths;
the runner never runs anything a manifest does not declare.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from wb_core import types
from wb_core.manifest import Command, Module
from wb_core.requirements import find_jdks
from wb_core.service import REPO_ROOT, new_id

from . import lease as leases


@dataclass
class Run:
    id: str
    command: Command
    module: Module
    body: dict
    events: list = field(default_factory=list)
    finished: bool = False
    cancelled: threading.Event = field(default_factory=threading.Event)
    process: subprocess.Popen | None = None
    started: float = field(default_factory=time.time)


class Runner:
    def __init__(self, settings, modules: list[Module], *, log=None):
        self.settings = settings
        self.modules = {m.name: m for m in modules}
        self.routines = {c.full: (m, c) for m in modules for c in m.commands if "host" in c.runs_on}
        self.runs: dict[str, Run] = {}
        self.cond = threading.Condition()
        self.log = log or (lambda text: print(text, flush=True))
        runner = settings.runner
        base = REPO_ROOT
        lab = Path(os.path.expanduser(runner["lab_home"]))
        self.lab_home = lab if lab.is_absolute() else (base / lab).resolve()
        self.lease_file = Path(runner.get("lease_file") or self.lab_home / "lease" / "gpu.lock")
        coordination = runner.get("coordination_dir")
        self.coordination = Path(coordination) if coordination else self.lab_home / "coordination"
        self.lane = runner.get("lane") or "workbench"
        self.token_env = [leases.TOKEN_ENV, *runner.get("token_env", [])]
        self.lane_env = [leases.LANE_ENV, *runner.get("lane_env", [])]
        self.jdks = {**find_jdks(settings), **{str(k): str(v) for k, v in (runner.get("jdks") or {}).items()}}
        self.java_home = str(runner.get("java_home") or "")

    # -- runs ------------------------------------------------------------------------------------
    def start(self, name: str, body: dict) -> str:
        if name not in self.routines:
            raise KeyError(f"{name} is not a declared host routine")
        module, command = self.routines[name]
        types.validate(command.params, body.get("params") or {})
        run = Run(new_id("r"), command, module, body)
        with self.cond:
            self.runs[run.id] = run
        threading.Thread(target=self._run, args=(run,), name=f"run-{run.id}", daemon=True).start()
        return run.id

    def _emit(self, run: Run, event: dict):
        with self.cond:
            event = dict(event, seq=len(run.events) + 1)
            run.events.append(event)
            self.cond.notify_all()

    def _run(self, run: Run):
        token = None
        held = None
        try:
            if run.command.resources.get("gpu"):
                self._emit(run, {"kind": "progress", "ts": time.time(), "phase": "lease",
                                 "msg": "waiting for the GPU lease"})
                lease, lane, token = leases.acquire(
                    self.lease_file, self.coordination, note=f"workbench {run.command.full}", job=run.body.get("job", ""),
                    cancelled=run.cancelled, lane_name=self.lane,
                    report=lambda why: self._emit(run, {"kind": "log", "ts": time.time(), "level": "INFO",
                                                        "msg": f"lease: {why}"}))
                held = (lease, lane)
                self._emit(run, {"kind": "log", "ts": time.time(), "level": "INFO", "msg": "GPU lease taken"})
            self._spawn(run, token)
        except InterruptedError:
            self._emit(run, {"kind": "done", "ts": time.time(), "outcome": "cancelled", "summary": "cancelled", "data": {}})
        except Exception as error:  # noqa: BLE001
            self._emit(run, {"kind": "done", "ts": time.time(), "outcome": "environment",
                             "summary": f"host runner: {type(error).__name__}: {error}", "data": {}})
        finally:
            if held:
                leases.release(held[0], held[1], self.coordination)
            with self.cond:
                run.finished = True
                self.cond.notify_all()

    def _spawn(self, run: Run, token: str | None):
        work = self.lab_home / "runs" / run.id
        work.mkdir(parents=True, exist_ok=True)
        (work / "cancel").unlink(missing_ok=True)
        out = run.body.get("out") or str(work / "out")
        sys_path = list(dict.fromkeys(str(m.directory.parent) for m in self.modules.values()))
        sys_path += [str(REPO_ROOT / "core"), str(REPO_ROOT / "sdk" / "python"), str(REPO_ROOT / "host")]
        secrets = {name: os.path.expanduser(entry["host"]) for name, entry in self.settings.secrets.items()}
        spec = {"job": run.body.get("job") or run.id, "command": run.command.full, "module": run.module.name,
                "handler": run.command.handler, "mode": "run",
                "params": types.validate(run.command.params, run.body.get("params") or {}),
                "work": str(work / "work"), "out": out, "module_config": run.body.get("module_config") or {},
                "secrets": secrets, "jdks": self.jdks, "quote": run.body.get("quote"), "machine": "host",
                "path_style": "windows", "roots": [], "sys_path": sys_path,
                "env": {"WB_LAB_HOME": str(self.lab_home), "WB_HOST_RUN": run.id,
                        "WB_LEASE_FILE": str(self.lease_file), "WB_COORDINATION_DIR": str(self.coordination)}}
        env = dict(os.environ, PYTHONUNBUFFERED="1",
                   PYTHONPATH=os.pathsep.join(sys_path + [os.environ.get("PYTHONPATH", "")]).strip(os.pathsep))
        env.update({f"JAVA_HOME_{key}_X64": home for key, home in self.jdks.items() if key.isdigit()})
        if self.java_home in self.jdks:
            env["JAVA_HOME"] = self.jdks[self.java_home]
        if token:
            env.update({name: token for name in self.token_env})
            env.update({name: self.lane for name in self.lane_env})
        process = subprocess.Popen([sys.executable, "-m", "wb_core.worker"], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                   errors="replace", env=env, cwd=str(work))
        run.process = process
        process.stdin.write(json.dumps(spec) + "\n")
        process.stdin.flush()
        process.stdin.close()  # cancelling uses the cancel file (see wb_core.worker)

        def pump_stderr():
            for line in process.stderr:
                if line.strip():
                    self._emit(run, {"kind": "log", "ts": time.time(), "level": "DEBUG", "msg": line.rstrip()})
        threading.Thread(target=pump_stderr, daemon=True).start()
        got_done = False
        for line in process.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            got_done = got_done or event.get("kind") in ("done", "requote")
            self._emit(run, event)
        process.wait()
        if not got_done:
            self._emit(run, {"kind": "done", "ts": time.time(), "outcome": "environment",
                             "summary": f"host worker exited {process.returncode} without a result", "data": {}})

    def cancel(self, run_id: str):
        run = self.runs[run_id]
        run.cancelled.set()
        if run.process and run.process.poll() is None:
            (self.lab_home / "runs" / run_id / "cancel").write_text("cancel", encoding="ascii")

    def events(self, run_id: str, after: int, wait: float) -> dict:
        run = self.runs[run_id]
        deadline = time.monotonic() + wait
        with self.cond:
            while len(run.events) <= after and not run.finished:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.cond.wait(min(remaining, 1.0))
            batch = run.events[after:]
        return {"events": batch, "finished": run.finished and len(run.events) <= after + len(batch),
                "heartbeat": time.time()}

    def health(self) -> dict:
        gpu = None
        if shutil.which("nvidia-smi"):
            done = subprocess.run(["nvidia-smi", "--query-gpu=name,driver_version,memory.total", "--format=csv,noheader"],
                                  capture_output=True, text=True, timeout=10)
            if done.returncode == 0 and done.stdout.strip():
                name, driver, memory = [p.strip() for p in done.stdout.strip().splitlines()[0].split(",")]
                gpu = {"name": name, "driver": driver, "memory": memory}
        holder = leases.owner(self.lease_file) if leases.occupied(self.lease_file) else None
        return {"ok": True, "gpu": gpu, "lab_home": str(self.lab_home), "lease_file": str(self.lease_file),
                "lease": {"held": holder is not None, "by": (holder or {}).get("lane") or (holder or {}).get("name")},
                "routines": sorted(self.routines), "running": sum(1 for r in self.runs.values() if not r.finished)}


class _Handler(BaseHTTPRequestHandler):
    server: "HostServer"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, status, body):
        raw = json.dumps(body, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _check(self) -> bool:
        host = (self.headers.get("Host") or "").lower()
        if host not in self.server.allowed:
            self._send(421, {"error": f"Host {host!r} is not served here"})
            return False
        expected = "Bearer " + self.server.token
        if not hmac.compare_digest((self.headers.get("Authorization") or "").encode(), expected.encode()):
            self._send(401, {"error": "missing or wrong token"})
            return False
        return True

    def do_GET(self):
        if not self._check():
            return
        parsed = urllib.parse.urlsplit(self.path)
        query = {k: v[-1] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        runner = self.server.runner
        if parsed.path == "/v1/health":
            return self._send(200, runner.health())
        match = re.match(r"^/v1/runs/(r_\w+)/events$", parsed.path)
        if match and match.group(1) in runner.runs:
            return self._send(200, runner.events(match.group(1), int(query.get("after", 0)),
                                                 min(float(query.get("wait", 0)), 30)))
        self._send(404, {"error": "no such route or run"})

    def do_POST(self):
        if not self._check():
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        except ValueError:
            return self._send(400, {"error": "body is not JSON"})
        runner = self.server.runner
        match = re.match(r"^/v1/routines/([\w.\-]+)$", self.path)
        if match:
            try:
                return self._send(202, {"run": runner.start(match.group(1), body)})
            except KeyError as error:
                return self._send(404, {"error": str(error)})
            except types.ValueError_ as error:
                return self._send(400, {"error": str(error)})
        match = re.match(r"^/v1/runs/(r_\w+)/cancel$", self.path)
        if match and match.group(1) in runner.runs:
            runner.cancel(match.group(1))
            return self._send(200, {"cancelled": True})
        self._send(404, {"error": "no such route or run"})


class HostServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        if not issubclass(sys.exc_info()[0] or Exception, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)):
            super().handle_error(request, client_address)

    def __init__(self, runner: Runner, token: str, port: int):
        self.runner, self.token = runner, token
        super().__init__(("127.0.0.1", port), _Handler)
        port = self.server_address[1]
        self.allowed = {f"127.0.0.1:{port}", f"localhost:{port}", f"host.docker.internal:{port}"}
