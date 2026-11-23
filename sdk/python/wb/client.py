"""The Workbench client: submit a command, get a promise (a job), wait for its envelope.

    import wb
    job = wb.run("frames.compare", ref="ref/", cand="cand/", profile="near")
    result = job.result()          # waits; raises wb.ApprovalRequired for a command that spends

Standard library only. The endpoint and token come from WB_URL and WB_TOKEN (or WB_TOKEN_FILE), else
from the run folder ``wb up`` writes: ``~/.config/wb/run/endpoint.json`` and ``~/.config/wb/run/token``.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import config as wbconfig

SCHEMA = "wb/result/v1"
TERMINAL = {"settled", "cancelled", "interrupted"}
OUTCOME_CODES = {"ok": 0, "finding": 1, "usage": 2, "environment": 3}
EXIT_NOT_SETTLED, EXIT_PENDING, EXIT_AWAITING_APPROVAL = 4, 5, 6


class WbError(Exception):
    """The API refused a request or could not be reached."""

    def __init__(self, message: str, status: int | None = None, body: dict | None = None):
        super().__init__(message)
        self.status, self.body = status, body or {}


class ApprovalRequired(Exception):
    """A command that spends money is waiting for an explicit yes on one quote.

    ``prompt`` is a ready-made question with options, for a person or a program to show the user.
    Approve with ``job.approve(quote_id)`` (or ``wb approve JOB --quote Q``) only after that yes.
    """

    def __init__(self, job: "Job", envelope: "Envelope"):
        approval = envelope.get("approval") or {}
        self.job, self.envelope = job, envelope
        self.quote = approval.get("quote") or {}
        self.prompt = approval.get("prompt") or {}
        super().__init__(self.prompt.get("question") or f"job {job.id} is awaiting approval")


class Envelope(dict):
    """A ``wb/result/v1`` envelope. Keys read as attributes: ``env.outcome``, ``env.data``."""

    def __getattr__(self, name):
        try:
            value = self[name]
        except KeyError:
            raise AttributeError(name) from None
        return _wrap(value)

    @property
    def ok(self) -> bool:
        return self.get("outcome") == "ok"

    @property
    def settled(self) -> bool:
        return self.get("state") == "settled"

    @property
    def exit_code(self) -> int:
        state = self.get("state")
        if state == "settled":
            return int(self.get("code", 3))
        if state in ("cancelled", "interrupted"):
            return EXIT_NOT_SETTLED
        if state == "awaiting_approval":
            return EXIT_AWAITING_APPROVAL
        return EXIT_PENDING


class _Attr(dict):
    def __getattr__(self, name):
        try:
            return _wrap(self[name])
        except KeyError:
            raise AttributeError(name) from None


def _wrap(value):
    if isinstance(value, dict) and not isinstance(value, _Attr):
        return _Attr(value)
    return value


@dataclass
class Endpoint:
    url: str
    token: str | None


def run_folder() -> Path:
    return wbconfig.default_folder() / "run"


def discover() -> Endpoint:
    url = os.environ.get("WB_URL")
    token = os.environ.get("WB_TOKEN")
    token_file = os.environ.get("WB_TOKEN_FILE")
    folder = run_folder()
    if not url:
        endpoint = folder / "endpoint.json"
        if endpoint.is_file():
            url = json.loads(endpoint.read_text(encoding="utf-8")).get("url")
    if not token:
        path = Path(token_file) if token_file else folder / "token"
        if path.is_file():
            token = path.read_text(encoding="ascii").strip()
    return Endpoint(url=url or "http://127.0.0.1:7377", token=token)


class Client:
    def __init__(self, url: str | None = None, token: str | None = None, timeout: float = 90):
        found = discover() if url is None or token is None else None
        self.url = (url or found.url).rstrip("/")
        self.token = token if token is not None else found.token
        self.timeout = timeout
        self.path_style = "windows" if sys.platform == "win32" else "posix"

    # -- transport -------------------------------------------------------------------------------
    def request(self, method: str, path: str, body=None, headers: dict | None = None,
                timeout: float | None = None) -> dict:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.url + path, data=data, method=method)
        request.add_header("Accept", "application/json")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        if self.token:
            request.add_header("Authorization", "Bearer " + self.token)
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            raw = error.read()
            try:
                payload = json.loads(raw or b"{}")
            except ValueError:
                payload = {"error": raw.decode("utf-8", "replace")}
            raise WbError(payload.get("error") or f"HTTP {error.code}", error.code, payload) from None
        except (urllib.error.URLError, ConnectionError, TimeoutError) as error:
            raise WbError(f"Workbench is not reachable at {self.url}: {error}") from None
        return json.loads(raw or b"{}")

    # -- API -------------------------------------------------------------------------------------
    def health(self) -> dict:
        return self.request("GET", "/v1/health")

    def commands(self) -> list[dict]:
        return self.request("GET", "/v1/commands")["commands"]

    def command(self, name: str) -> dict:
        return self.request("GET", "/v1/commands/" + urllib.parse.quote(name))

    def submit(self, command: str, params: dict | None = None, *, out: str | None = None,
               fresh: bool = False, idempotency_key: str | None = None) -> "Job":
        body = {"command": command, "params": params or {}, "fresh": fresh, "path_style": self.path_style}
        if out:
            body["out"] = str(out)
        key = idempotency_key or uuid.uuid4().hex
        headers = {"Idempotency-Key": key}
        if os.environ.get("WB_JOB"):  # submitted from inside a job: the scheduler must not make it wait on us
            headers["X-Wb-Parent"] = os.environ["WB_JOB"]
        if os.environ.get("WB_CALLER"):
            headers["X-Wb-Caller"] = os.environ["WB_CALLER"]  # who submitted it, as the caller names itself
        if not os.environ.get("WB_JOB") and not os.environ.get("WB_DEV"):
            commit = _cli_commit()
            if commit:
                headers["X-Wb-Commit"] = commit  # the daemon refuses a CLI on other code (wb deploy brings them together)
        envelope = Envelope(self.request("POST", "/v1/jobs", body, headers))
        return Job(self, envelope["job"], envelope)

    def call_inline(self, job_id: str, command: str, params: dict | None = None, timeout: float = 600) -> dict:
        """Runs an inline command for the running job ``job_id`` in that job's reservation, and returns
        {command, outcome, summary, data}. Only commands marked inline in their manifest can be called this way."""
        return self.request("POST", f"/v1/jobs/{job_id}/call", {"command": command, "params": params or {}},
                            timeout=timeout)

    def job(self, job_id: str) -> "Job":
        return Job(self, job_id, None)

    def jobs(self, **filters) -> list[Envelope]:
        query = urllib.parse.urlencode({k: v for k, v in filters.items() if v})
        return [Envelope(e) for e in self.request("GET", "/v1/jobs" + (f"?{query}" if query else ""))["jobs"]]

    def config(self, origin: bool = False) -> dict:
        return self.request("GET", "/v1/config" + ("?origin=1" if origin else ""))

    def reload_config(self) -> dict:
        """Has the daemon re-read its config folders (module config, schedules, budget); see Workbench.reload_config."""
        return self.request("POST", "/v1/config/reload", {})

    def schema(self) -> dict:
        return self.request("GET", "/v1/schema")

    def events(self, job_id: str, after: int = 0):
        """Yields stored and live events of a job until it reaches a terminal state."""
        request = urllib.request.Request(f"{self.url}/v1/jobs/{job_id}/events")
        request.add_header("Accept", "text/event-stream")
        if self.token:
            request.add_header("Authorization", "Bearer " + self.token)
        if after:
            request.add_header("Last-Event-ID", str(after))
        with urllib.request.urlopen(request, timeout=3600) as stream:
            event = {}
            for raw in stream:
                line = raw.decode("utf-8").rstrip("\n")
                if not line:
                    if event.get("data") is not None:
                        yield {"id": int(event.get("id", 0)), "kind": event.get("event", "message"),
                               "data": json.loads(event["data"])}
                    event = {}
                    continue
                if line.startswith(":"):
                    continue
                key, _, value = line.partition(":")
                event[key] = value[1:] if value.startswith(" ") else value

    def logs(self, module: str | None = None, job: str | None = None, limit: int = 200) -> list[dict]:
        query = urllib.parse.urlencode({k: v for k, v in {"module": module, "job": job, "limit": limit}.items() if v})
        return self.request("GET", "/v1/logs?" + query)["lines"]


class Job:
    """A promise of one result. ``result()`` waits for it and returns the settled envelope."""

    def __init__(self, client: Client, job_id: str, envelope: dict | None):
        self.client, self.id = client, job_id
        self.envelope = Envelope(envelope) if envelope else None

    def __repr__(self):
        state = self.envelope.get("state") if self.envelope else "?"
        return f"<wb.Job {self.id} {state}>"

    def refresh(self, wait: float = 0) -> Envelope:
        query = f"?wait={int(wait)}" if wait else ""
        self.envelope = Envelope(self.client.request("GET", f"/v1/jobs/{self.id}{query}",
                                                     timeout=max(self.client.timeout, wait + 30)))
        return self.envelope

    def wait(self, timeout: float | None = None, *, stop_at_approval: bool = True) -> Envelope:
        deadline = None if timeout is None else time.monotonic() + timeout
        envelope = self.envelope or self.refresh()
        while envelope.get("state") not in TERMINAL:
            if stop_at_approval and envelope.get("state") == "awaiting_approval":
                return envelope
            remaining = 60 if deadline is None else min(60, deadline - time.monotonic())
            if remaining <= 0:
                return envelope
            envelope = self.refresh(wait=max(1, remaining))
        return envelope

    def result(self, timeout: float | None = None) -> Envelope:
        envelope = self.wait(timeout)
        if envelope.get("state") == "awaiting_approval":
            raise ApprovalRequired(self, envelope)
        return envelope

    def approve(self, quote: str, via: str = "direct") -> Envelope:
        self.envelope = Envelope(self.client.request("POST", f"/v1/jobs/{self.id}/approve",
                                                     {"quote": quote, "via": via}))
        return self.envelope

    def cancel(self) -> Envelope:
        self.envelope = Envelope(self.client.request("POST", f"/v1/jobs/{self.id}/cancel", {}))
        return self.envelope

    def events(self):
        return self.client.events(self.id)


_COMMIT: list = []


def _cli_commit() -> str | None:
    """The commit of the checkout this CLI runs from (None in an image or a folder without git), once per process."""
    if not _COMMIT:
        try:
            from wb_core.identity import source_commit
            from pathlib import Path as _Path
            root = _Path(__file__).resolve().parents[3]
            _COMMIT.append(source_commit(root) if (root / ".git").exists() else None)
        except Exception:  # noqa: BLE001 - no identity, no handshake
            _COMMIT.append(None)
    return _COMMIT[0]


_default: Client | None = None


def connect(url: str | None = None, token: str | None = None) -> Client:
    global _default
    _default = Client(url, token)
    return _default


def _client() -> Client:
    return _default or connect()


def run(command: str, *, out: str | None = None, fresh: bool = False, **params) -> Job:
    """Submit ``command`` with keyword parameters; returns the job (a promise)."""
    return _client().submit(command, params, out=out, fresh=fresh)
