"""HTTP for the channels, standard library only.

A channel's token goes only to the URL its config names: redirects are never followed (a redirected request fails
and says where it was sent), plain http is refused except to this machine, and a loopback address skips any proxy.
Request bodies are streamed from files with their length known up front, so a large JAR is never read into memory.
"""

from __future__ import annotations

import base64
import http.client
import io
import ipaddress
import json
import re
import secrets
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

MAX_ANSWER = 32 * 2**20  # a channel answers with metadata; a larger answer is refused rather than read
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")  # file and form names: safe in headers and URL paths


class ChannelError(Exception):
    """A channel refused a request, answered with something unexpected, or could not be reached."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def loopback(host: str) -> bool:
    host = host.strip("[]").lower()
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def checked_url(value, where: str) -> str:
    """A channel URL as config gives it: https (http only to this machine), no credentials, query or fragment."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"{where} is not set")
    parts = urllib.parse.urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"{where} = {value!r} is not an http(s) URL")
    if parts.scheme == "http" and not loopback(parts.hostname):
        raise ValueError(f"{where} = {value!r} would send the channel's token in the clear: use https (plain http "
                         "only reaches a server on this machine)")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError(f"{where} = {value!r} may not carry credentials, a query or a fragment")
    return value.rstrip("/")


def basic(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # the 3xx surfaces as an HTTPError, reported with where it pointed


@dataclass
class Answer:
    status: int
    headers: dict
    body: bytes
    where: str = ""

    def json(self):
        try:
            return json.loads(self.body.decode("utf-8") or "null")
        except (ValueError, UnicodeDecodeError):
            raise ChannelError(f"{self.where} answered with something that is not JSON: {excerpt(self.body)}",
                               self.status) from None


def excerpt(raw: bytes, limit: int = 300) -> str:
    text = raw[:limit].decode("utf-8", "replace").strip()
    return (text + (" ..." if len(raw) > limit else "")) or "(no body)"


def _read(stream, where: str) -> bytes:
    data = stream.read(MAX_ANSWER + 1)
    if len(data) > MAX_ANSWER:
        raise ChannelError(f"{where} answered with more than {MAX_ANSWER >> 20} MiB; refused")
    return data


class Http:
    def __init__(self, user_agent: str, timeout: float, log=None):
        self.user_agent, self.timeout, self.log = user_agent, timeout, log

    def request(self, method: str, url: str, *, headers: dict | None = None, body=None, length: int | None = None,
                missing: bool = False) -> Answer:
        """One request; ``missing`` returns a 404 as an answer instead of raising."""
        request = urllib.request.Request(url, data=body, method=method)
        request.add_header("User-Agent", self.user_agent)
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        if length is not None:
            request.add_header("Content-Length", str(length))
        parts = urllib.parse.urlsplit(url)
        where = f"{method} {parts.scheme}://{parts.netloc}{parts.path}"
        handlers = [_NoRedirects()]
        if loopback(parts.hostname or ""):
            handlers.append(urllib.request.ProxyHandler({}))
        if self.log:
            self.log(where)
        try:
            with urllib.request.build_opener(*handlers).open(request, timeout=self.timeout) as response:
                return Answer(response.status, dict(response.headers.items()), _read(response, where), where)
        except urllib.error.HTTPError as error:
            raw = _read(error, where) if error.fp is not None else b""
            if missing and error.code == 404:
                return Answer(404, dict(error.headers.items()) if error.headers else {}, raw, where)
            if 300 <= error.code < 400:
                target = error.headers.get("Location") if error.headers else None
                raise ChannelError(f"{where} was redirected (HTTP {error.code}) to {target}; a token never follows a "
                                   "redirect, so point the channel at the final URL", error.code) from None
            raise ChannelError(f"{where} -> HTTP {error.code}: {excerpt(raw)}", error.code) from None
        except (urllib.error.URLError, OSError, http.client.HTTPException) as error:
            raise ChannelError(f"{where} failed: {getattr(error, 'reason', None) or error}") from None

    def json(self, method: str, url: str, *, headers: dict | None = None, payload=None, missing: bool = False):
        """A JSON request and its decoded answer; None for a 404 when ``missing``."""
        merged = {"Accept": "application/json", **(headers or {})}
        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            merged["Content-Type"] = "application/json"
        answer = self.request(method, url, headers=merged, body=body, missing=missing)
        return None if answer.status == 404 else answer.json()

    def upload(self, method: str, url: str, path: Path, *, headers: dict | None = None,
               content_type: str = "application/octet-stream") -> Answer:
        """A file as the whole body, streamed."""
        size = Path(path).stat().st_size
        with open(path, "rb") as handle:
            return self.request(method, url, headers={**(headers or {}), "Content-Type": content_type}, body=handle,
                                length=size)

    def form(self, method: str, url: str, form: "Form", *, headers: dict | None = None) -> Answer:
        """A multipart/form-data body, streamed."""
        reader = form.reader()
        try:
            return self.request(method, url, headers={**(headers or {}), "Content-Type": form.content_type},
                                body=reader, length=form.length)
        finally:
            reader.close()


class Form:
    """A multipart/form-data body of fields and files, with its length known before it is sent."""

    def __init__(self):
        self.boundary = "wb-release-" + secrets.token_hex(16)
        self.parts: list = []

    def field(self, name: str, value: str):
        self._head(name, None, None)  # no Content-Type: a plain form field, as a browser or curl sends one
        self.parts.append(value.encode("utf-8") + b"\r\n")

    def file(self, name: str, filename: str, path: Path, content_type: str):
        self._head(name, filename, content_type)
        self.parts += [Path(path), b"\r\n"]

    def _head(self, name: str, filename: str | None, content_type: str | None):
        for value in (name, filename):
            if value is not None and not SAFE_NAME.match(value):
                raise ValueError(f"{value!r} cannot be named in a form")
        head = f"--{self.boundary}\r\nContent-Disposition: form-data; name=\"{name}\""
        if filename is not None:
            head += f"; filename=\"{filename}\""
        if content_type:
            head += f"\r\nContent-Type: {content_type}"
        self.parts.append((head + "\r\n\r\n").encode("utf-8"))

    @property
    def content_type(self) -> str:
        return f"multipart/form-data; boundary={self.boundary}"

    def _all(self) -> list:
        return self.parts + [f"--{self.boundary}--\r\n".encode("ascii")]

    @property
    def length(self) -> int:
        return sum(len(part) if isinstance(part, bytes) else part.stat().st_size for part in self._all())

    def reader(self) -> "_Reader":
        return _Reader(self._all())


class _Reader:
    """Reads the parts one after another, opening each file only while it is being sent."""

    def __init__(self, parts: list):
        self._parts = list(parts)
        self._current = None

    def read(self, size: int = -1) -> bytes:
        out = bytearray()
        while self._parts or self._current is not None:
            if self._current is None:
                part = self._parts.pop(0)
                self._current = open(part, "rb") if isinstance(part, Path) else io.BytesIO(part)
            chunk = self._current.read(-1 if size is None or size < 0 else size - len(out))
            if not chunk:
                self._current.close()
                self._current = None
                continue
            out += chunk
            if size is not None and 0 <= size <= len(out):
                break
        return bytes(out)

    def close(self):
        if self._current is not None:
            self._current.close()
            self._current = None
