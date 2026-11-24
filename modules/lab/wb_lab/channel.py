"""A session's client channel: how a command reaches a fixture mod inside a running client.

Every session has a folder ``<session>/channel``, which a launch's client JVM arguments name as ``{channel}`` (lab.run
fills it in, as it fills ``{port}``), so the product's own test-only fixture mod knows where to read. The protocol is
files, one request at a time:

- a request is a JSON object ``{"id": ..., "action": ..., ...}`` written whole to ``channel/request.json`` (written
  beside it and renamed, so the fixture never reads half of one); its id is letters, digits, ``-`` and ``_``;
- the fixture takes it (it moves or removes ``request.json``), acts, and writes its answer to
  ``channel/responses/<id>.json``, a JSON object with ``ok`` and, as the fixture chooses, ``result`` or ``error``;
- a new request is written only once ``request.json`` is gone.

Lines of an open session's ``commands.txt`` that start with ``client `` are requests (``client status``,
``client capture {"name": "a.png"}``, or ``client {"action": "status"}``): lab.run writes each in turn and records the
answers in session.json. ``send`` does the same for one request from any program (lab.send, a driver). Workbench reads
no action: what they mean is the fixture's.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from datetime import UTC, datetime
from pathlib import Path

FOLDER = "channel"
REQUEST = "request.json"
RESPONSES = "responses"
ID = re.compile(r"^[A-Za-z0-9_-]+$")
ACTION = re.compile(r"^[A-Za-z0-9_.:-]+$")
TIMEOUT = 120.0  # how long the relay waits for one answer before it gives up on it


class ChannelError(Exception):
    """A request that cannot be written, or an answer that did not come."""


def new_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(4)


def parse(text: str) -> dict:
    """A request from a ``client`` line's rest: ``<action> [<JSON object>]`` or a JSON object with its action."""
    text = text.strip()
    if text.startswith("{"):
        try:
            request = json.loads(text)
        except ValueError as error:
            raise ChannelError(f"client request {text!r} is not a JSON object: {error}") from None
    else:
        action, _, rest = text.partition(" ")
        try:
            values = json.loads(rest) if rest.strip() else {}
        except ValueError as error:
            raise ChannelError(f"client {action}: {rest.strip()!r} is not a JSON object: {error}") from None
        if not isinstance(values, dict):
            raise ChannelError(f"client {action}: its values are a JSON object, not {rest.strip()!r}")
        request = {**values, "action": action}
    if not isinstance(request, dict) or not ACTION.match(str(request.get("action", ""))):
        raise ChannelError(f"client request {text!r} names no action (client <action> [<JSON object>])")
    if "id" in request and not ID.match(str(request["id"])):
        raise ChannelError(f"client request id {request['id']!r} is not letters, digits, - and _")
    return request


def post(folder: Path, request: dict) -> dict:
    """Writes one request (given an id when it has none) to the channel; refused while another is unread."""
    request = {"id": new_id(), **request} if "id" not in request else dict(request)
    target = folder / REQUEST
    if target.exists():
        raise ChannelError(f"{target} is still unread: one request at a time")
    (folder / RESPONSES).mkdir(parents=True, exist_ok=True)
    temporary = folder / f".{request['id']}.tmp"
    temporary.write_text(json.dumps(request, indent=1) + "\n", encoding="utf-8")
    os.replace(temporary, target)
    return request


def answer(folder: Path, ident: str) -> dict | None:
    """The fixture's answer to request ``ident``, or None while there is none (or it is still being written)."""
    path = folder / RESPONSES / f"{ident}.json"
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else {"ok": False, "error": f"the answer is not a JSON object: {value!r}"}


def send(folder: Path, action: str, values: dict | None = None, *, timeout: float = 45.0, poll: float = 0.1,
         cancelled=None) -> dict:
    """Sends one request and waits for its answer: ``{"request": ..., "answer": ..., "file": ...}``."""
    request = parse(json.dumps({**(values or {}), "action": action}))
    deadline = time.monotonic() + timeout
    while (folder / REQUEST).exists():  # another request is being read; wait for its turn
        if time.monotonic() > deadline:
            raise ChannelError(f"{folder / REQUEST} stayed unread for {timeout:g} s")
        time.sleep(poll)
    request = post(folder, request)
    while (found := answer(folder, request["id"])) is None:
        if cancelled is not None and cancelled.is_set():
            raise ChannelError(f"cancelled while waiting for the answer to {request['action']} ({request['id']})")
        if time.monotonic() > deadline:
            raise ChannelError(f"no answer to {request['action']} ({request['id']}) within {timeout:g} s: is the "
                               f"fixture reading {folder}?")
        time.sleep(poll)
    return {"request": request, "answer": found, "file": str(folder / RESPONSES / f"{request['id']}.json")}


class Relay:
    """The requests of a session's commands file, written one at a time; ``pump`` is called as the session waits."""

    def __init__(self, folder: Path, log=None, timeout: float = TIMEOUT):
        self.folder, self.log, self.timeout = folder, log, timeout
        self.queue: list[dict] = []
        self.current: dict | None = None
        self.records: list[dict] = []

    def add(self, text: str) -> dict:
        """Queues the request of one ``client`` line; a line that is no request is recorded as refused."""
        try:
            request = parse(text)
        except ChannelError as error:
            record = {"line": text, "state": "refused", "error": str(error)}
            self.records.append(record)
            if self.log:
                self.log(f"client channel: {error}")
            return record
        request = {"id": new_id(), **request} if "id" not in request else request
        self.queue.append(request)
        return request

    def pump(self, post_next: bool = True) -> list[dict]:
        """Collects an answer that came and, unless ``post_next`` is false (the session is ending), writes the next
        request when the channel is free; returns what ended."""
        ended = []
        if self.current is not None:
            request, sent = self.current["request"], self.current["at"]
            found = answer(self.folder, request["id"])
            if found is not None:
                ended.append(self._end(request, "answered", sent, found))
            elif time.monotonic() - sent > self.timeout:
                unread = self.folder / REQUEST
                if unread.is_file() and _id_of(unread) == request["id"]:
                    unread.unlink(missing_ok=True)  # our own request, never read: take it back
                ended.append(self._end(request, "timed_out", sent, None))
        if post_next and self.current is None and self.queue and not (self.folder / REQUEST).exists():
            request = post(self.folder, self.queue.pop(0))
            self.current = {"request": request, "at": time.monotonic(), "sent": _now()}
            if self.log:
                self.log(f"client channel: sent {request['action']} ({request['id']})")
        return ended

    def _end(self, request: dict, state: str, sent: float, found: dict | None) -> dict:
        record = {"id": request["id"], "action": request["action"], "state": state, "sent": self.current["sent"],
                  "seconds": round(time.monotonic() - sent, 3), "request": request,
                  **({"ok": bool(found.get("ok")), "error": found.get("error"),
                      "answer": f"{FOLDER}/{RESPONSES}/{request['id']}.json"} if found is not None else {})}
        self.records.append(record)
        self.current = None
        if self.log:
            self.log(f"client channel: {request['action']} ({request['id']}) "
                     + (f"answered, ok={record['ok']}" + (f": {record['error']}" if record.get("error") else "")
                        if found is not None else f"not answered within {self.timeout:g} s"))
        return record

    def pending(self) -> bool:
        return self.current is not None or bool(self.queue)


def _id_of(path: Path) -> str | None:
    try:
        return str(json.loads(path.read_text(encoding="utf-8")).get("id"))
    except (OSError, ValueError, AttributeError):
        return None


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
