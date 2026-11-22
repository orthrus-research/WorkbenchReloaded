"""The ``/v1`` HTTP API (spec 7): JSON on localhost, a bearer token, a Host header check, no CORS.

Standard library only (``http.server``, threaded). Long-poll with ``?wait=N``; events as server-sent
events, or as JSON batches with ``?format=json&after=N&wait=N`` for clients that cannot stream.
"""

from __future__ import annotations

import hmac
import sys
import json
import re
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__, types
from .service import TERMINAL, BadRequest, Conflict, NotFound, Workbench

_ROUTES = []


def route(method: str, pattern: str):
    compiled = re.compile("^" + pattern + "$")

    def register(function):
        _ROUTES.append((method, compiled, function))
        return function
    return register


class ApiError(Exception):
    def __init__(self, status: int, message: str, body: dict | None = None):
        super().__init__(message)
        self.status, self.body = status, body or {}


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, workbench: Workbench, token: str | None, *, host_check: bool = True,
                 extra_hosts: set[str] | None = None, max_wait: float = 60):
        self.workbench, self.token, self.host_check = workbench, token, host_check
        self.max_wait = max_wait
        super().__init__(address, Handler)
        port = self.server_address[1]
        self.allowed_hosts = set()  # replaced below; set first so handle_error can run early
        self.allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"} | (extra_hosts or set())


def _handle_error(self, request, client_address):
    """A client closing a kept-alive connection is normal (Java's and Node's clients do); anything else is logged."""
    kind = sys.exc_info()[0]
    if kind and issubclass(kind, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError, TimeoutError)):
        return
    self.workbench.hub.error(f"connection from {client_address[0]} failed: {sys.exc_info()[1]!r}")


Server.handle_error = _handle_error


class Handler(BaseHTTPRequestHandler):
    server: Server
    protocol_version = "HTTP/1.1"
    server_version = f"wb/{__version__}"

    def log_message(self, fmt, *args):  # routed to the hub instead of stderr
        self.server.workbench.hub.debug(f"{self.address_string()} {fmt % args}", module="core")

    # -- plumbing --------------------------------------------------------------------------------
    def _send(self, status: int, body: dict | None = None, headers: dict | None = None):
        payload = json.dumps(body if body is not None else {}, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > 4 * 1024 * 1024:
            raise ApiError(413, "request body too large")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            body = json.loads(raw)
        except ValueError:
            raise ApiError(400, "request body is not JSON") from None
        if not isinstance(body, dict):
            raise ApiError(400, "request body must be a JSON object")
        return body

    def _authorise(self):
        if self.server.host_check:
            host = (self.headers.get("Host") or "").lower()
            if host not in self.server.allowed_hosts:
                raise ApiError(421, f"Host {host!r} is not served here (local only)")
        if self.headers.get("Origin"):
            raise ApiError(403, "browser requests are refused")
        if self.server.token:
            given = self.headers.get("Authorization", "")
            expected = "Bearer " + self.server.token
            if not hmac.compare_digest(given.encode(), expected.encode()):
                raise ApiError(401, "missing or wrong bearer token (see ~/.config/wb/run/token)")

    def _dispatch(self, method: str):
        parsed = urllib.parse.urlsplit(self.path)
        query = {k: v[-1] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        try:
            self._authorise()
            for verb, pattern, function in _ROUTES:
                match = pattern.match(parsed.path)
                if match and verb == method:
                    return function(self, query, *match.groups())
            raise ApiError(404, f"no route {method} {parsed.path}")
        except ApiError as error:
            self._send(error.status, dict(error.body, error=str(error)))
        except NotFound as error:
            self._send(404, {"error": str(error)})
        except Conflict as error:
            self._send(409, {"error": str(error), "envelope": error.envelope})
        except BadRequest as error:
            self._send(400, {"error": str(error)})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as error:  # noqa: BLE001
            self.server.workbench.hub.error(f"API error on {method} {parsed.path}: {error!r}")
            try:
                self._send(500, {"error": f"internal error: {type(error).__name__}"})
            except OSError:
                pass

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_OPTIONS(self):
        self._send(405, {"error": "no CORS here"})


def _wait(server: Server, query: dict) -> float:
    try:
        return min(float(query.get("wait", 0)), server.max_wait)
    except ValueError:
        raise ApiError(400, "wait must be a number of seconds") from None


# -- routes --------------------------------------------------------------------------------------
@route("GET", r"/v1/health")
def health(h: Handler, query):
    wb = h.server.workbench
    host = None
    if wb.host is not None:
        try:
            host = wb.host.health()
        except Exception as error:  # noqa: BLE001
            host = {"error": str(error)}
    active = sum(1 for e in wb.registry.values() if e.activation.active)
    h._send(200, {"ok": True, "version": __version__, "image": wb.image_digest, "install": wb.install_id, "uptime_s": round(time.time() - wb.started),
                  "modules": sorted(wb.modules), "commands": {"active": active, "total": len(wb.registry)},
                  "running": len(wb.running), "host": host})


@route("GET", r"/v1/commands")
def commands(h: Handler, query):
    items = h.server.workbench.describe_commands()
    if query.get("module"):
        items = [c for c in items if c["module"] == query["module"]]
    h._send(200, {"commands": items})


@route("GET", r"/v1/commands/([\w.\-]+)")
def command(h: Handler, query, name):
    wb = h.server.workbench
    entry = wb.entry(name)
    item = entry.command.describe()
    item["active"] = entry.activation.active
    item["reasons"] = [r.as_dict() for r in entry.activation.reasons]
    item["warnings"] = [r.as_dict() for r in entry.activation.warnings]
    h._send(200, item)


@route("POST", r"/v1/jobs")
def submit(h: Handler, query):
    body = h._body()
    if not body.get("command"):
        raise ApiError(400, "command is required")
    unknown = set(body) - {"command", "params", "out", "fresh", "path_style"}
    if unknown:
        raise ApiError(400, f"unknown fields {sorted(unknown)}")
    envelope = h.server.workbench.submit(body["command"], body.get("params") or {}, out=body.get("out"),
                                         fresh=bool(body.get("fresh")),
                                         path_style=body.get("path_style", "windows"),
                                         idempotency_key=h.headers.get("Idempotency-Key"),
                                         parent=h.headers.get("X-Wb-Parent"))
    h._send(202, envelope, {"Location": f"/v1/jobs/{envelope['job']}"})


@route("GET", r"/v1/jobs")
def jobs(h: Handler, query):
    h._send(200, {"jobs": h.server.workbench.list_jobs(query.get("state"), query.get("command"),
                                                       query.get("since"), int(query.get("limit", 100)))})


@route("GET", r"/v1/jobs/(j_\w+)")
def job(h: Handler, query, job_id):
    wait = _wait(h.server, query)
    wb = h.server.workbench
    h._send(200, wb.wait(job_id, wait) if wait else wb.envelope(job_id))


@route("POST", r"/v1/jobs/(j_\w+)/approve")
def approve(h: Handler, query, job_id):
    body = h._body()
    if not body.get("quote"):
        raise ApiError(400, "quote is required: approve one exact quote")
    h._send(200, h.server.workbench.approve(job_id, body["quote"], body.get("via", "direct")))


@route("POST", r"/v1/jobs/(j_\w+)/cancel")
def cancel(h: Handler, query, job_id):
    h._send(200, h.server.workbench.cancel(job_id))


@route("GET", r"/v1/jobs/(j_\w+)/events")
def events(h: Handler, query, job_id):
    wb = h.server.workbench
    wb.envelope(job_id)  # 404 for an unknown job
    after = int(query.get("after") or h.headers.get("Last-Event-ID") or 0)
    if query.get("format") == "json":
        wait = _wait(h.server, query)
        batch = wb.wait_events(job_id, after, wait) if wait else wb.events(job_id, after)
        state = wb.envelope(job_id)["state"]
        h._send(200, {"events": batch, "state": state, "finished": state in TERMINAL})
        return
    h.send_response(200)
    h.send_header("Content-Type", "text/event-stream")
    h.send_header("Cache-Control", "no-store")
    h.send_header("Connection", "close")
    h.end_headers()
    h.close_connection = True
    last_beat = time.monotonic()
    while True:
        batch = wb.wait_events(job_id, after, 15)
        for event in batch:
            after = event["id"]
            payload = json.dumps(dict(event["data"], ts=event["ts"]), default=str)
            h.wfile.write(f"id: {event['id']}\nevent: {event['kind']}\ndata: {payload}\n\n".encode())
        if not batch and time.monotonic() - last_beat > 14:
            h.wfile.write(b": keep-alive\n\n")
            last_beat = time.monotonic()
        h.wfile.flush()
        if wb.envelope(job_id)["state"] in TERMINAL and not wb.events(job_id, after):
            return


@route("GET", r"/v1/jobs/(j_\w+)/artifacts/([^/]+)")
def artifact(h: Handler, query, job_id, name):
    path = h.server.workbench.artifact_path(job_id, urllib.parse.unquote(name))
    size = path.stat().st_size
    h.send_response(200)
    h.send_header("Content-Type", "application/octet-stream")
    h.send_header("Content-Length", str(size))
    h.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
    h.end_headers()
    with open(path, "rb") as handle:
        while block := handle.read(1 << 20):
            h.wfile.write(block)


@route("GET", r"/v1/config")
def config(h: Handler, query):
    settings = h.server.workbench.settings
    body = {"values": settings.config.values, "folder": str(settings.config.folder) if settings.config.folder else None}
    if query.get("origin"):
        body["origins"] = settings.config.origins
    h._send(200, body)


@route("GET", r"/v1/logs")
def logs(h: Handler, query):
    limit = max(1, min(int(query.get("limit", 200)), 5000))
    h._send(200, {"lines": h.server.workbench.hub.tail(query.get("module"), query.get("job"), limit)})


@route("GET", r"/v1/schema")
def schema(h: Handler, query):
    h._send(200, h.server.workbench.schema())


@route("POST", r"/v1/config/reload")
def config_reload(h: Handler, query):
    h._send(200, h.server.workbench.reload_config())


@route("POST", r"/v1/prune")
def prune(h: Handler, query):
    body = h._body()
    h._send(200, h.server.workbench.prune(dry_run=body.get("dry_run", True) is not False))


def serve(workbench: Workbench, listen: str, port: int, token: str | None, *, host_check: bool = True,
          max_wait: float = 60) -> Server:
    server = Server((listen, port), workbench, token, host_check=host_check, max_wait=max_wait)
    thread = threading.Thread(target=server.serve_forever, name="api", daemon=True)
    thread.start()
    return server


__all__ = ["serve", "Server", "types"]
