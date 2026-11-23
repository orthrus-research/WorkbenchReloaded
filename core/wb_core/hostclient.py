"""The container's client for the Windows host runner (``wb-host``, spec 12)."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from .settings import Settings


class HostError(Exception):
    """The host runner answered with an error; its own words are in the message."""


class HostClient:
    def __init__(self, url: str, token: str | None):
        self.url, self.token = url.rstrip("/"), token

    @classmethod
    def from_settings(cls, settings: Settings) -> "HostClient | None":
        host = settings.core["host"]
        if not host["enabled"]:
            return None
        path = settings.secret_path(host["token_secret"])
        token = path.read_text(encoding="ascii").strip() if path and path.is_file() else None
        return cls(host["url"], token)

    def _call(self, method: str, path: str, body=None, timeout: float = 30):
        request = urllib.request.Request(self.url + path, method=method,
                                         data=None if body is None else json.dumps(body).encode())
        request.add_header("Content-Type", "application/json")
        request.add_header("Host", urllib.parse.urlsplit(self.url).netloc)
        if self.token:
            request.add_header("Authorization", "Bearer " + self.token)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            raw = error.read() or b""
            try:
                said = json.loads(raw).get("error")
            except (ValueError, AttributeError):
                said = raw.decode("utf-8", "replace")[:300]
            hint = " (the host runner may be on older code: restart it, or run wb deploy)" if error.code == 404 else ""
            raise HostError(f"the host runner refused {method} {path.split('?')[0]}: HTTP {error.code}: "
                            f"{said or error.reason}{hint}") from None

    def health(self) -> dict:
        return self._call("GET", "/v1/health", timeout=3)

    def start(self, routine: str, body: dict) -> str:
        return self._call("POST", f"/v1/routines/{routine}", body)["run"]

    def events(self, run: str, after: int, wait: float = 20) -> dict:
        return self._call("GET", f"/v1/runs/{run}/events?after={after}&wait={int(wait)}", timeout=wait + 15)

    def cancel(self, run: str) -> dict:
        return self._call("POST", f"/v1/runs/{run}/cancel", {})
