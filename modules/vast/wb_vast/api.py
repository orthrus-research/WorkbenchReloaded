"""The Vast.ai REST client (ported from vast_probe.call). Every request, failed ones included, is recorded in
the ledger with its time, purpose and compressed response; the key travels only in the header."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

DEFAULT_API = "https://console.vast.ai/api/v0"


class VastError(Exception):
    def __init__(self, message: str, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status, self.body = status, body


class QuotaExceeded(VastError):
    """Vast's daily search quota or rate limit: nothing is wrong with the request; retry after ``retry_after`` s."""

    def __init__(self, message: str, status: int, body: str, retry_after: float):
        super().__init__(message, status, body)
        self.retry_after = retry_after


class Vast:
    def __init__(self, key: str, base: str = DEFAULT_API, recorder=None, job: str | None = None):
        self.key, self.base, self.recorder, self.job = key, base.rstrip("/"), recorder, job
        self.purpose = None

    def call(self, method: str, path: str, body=None, purpose: str | None = None, timeout: float = 60):
        request = urllib.request.Request(self.base + path, method=method,
                                         data=None if body is None else json.dumps(body).encode(),
                                         headers={"Authorization": "Bearer " + self.key,
                                                  "Content-Type": "application/json", "Accept": "application/json"})
        started = time.time()
        status, raw = None, b""
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status, raw = response.status, response.read()
            return json.loads(raw or b"{}")
        except urllib.error.HTTPError as error:
            status, raw = error.code, error.read()
            if error.code == 429:
                try:
                    detail = json.loads(raw or b"{}")
                except ValueError:
                    detail = {}
                wait = float(detail.get("retry_after") or 0)
                when = time.strftime("%H:%M UTC", time.gmtime(time.time() + wait)) if wait else "later"
                raise QuotaExceeded(f"Vast refused the request: {detail.get('msg') or 'rate limited'} "
                                    f"(limit {detail.get('limit', '?')}); retry after {when}", 429,
                                    raw.decode("utf-8", "replace"), wait) from None
            raise VastError(f"Vast {method} {path.split('?')[0]} -> HTTP {error.code}: "
                            f"{raw[:300].decode('utf-8', 'replace')}", error.code, raw.decode("utf-8", "replace")) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            raise VastError(f"Vast {method} {path.split('?')[0]} failed: {error}") from None
        finally:
            if self.recorder is not None:
                try:
                    endpoint, _, query = path.partition("?")
                    self.recorder(method, endpoint, body if body is not None else (query or None), status,
                                  (time.time() - started) * 1000, raw, purpose or self.purpose, self.job)
                except Exception:  # noqa: BLE001 - recording never breaks a request
                    pass


def list_instances(vast: "Vast", purpose: str) -> list[dict]:
    """This account's instances. Vast retired GET /api/v0/instances/ (HTTP 410, 'Use /api/v1/instances/ instead');
    the v1 listing pages with next_token."""
    base = vast.base.replace("/api/v0", "/api/v1")
    saved, items, token = vast.base, [], None
    try:
        vast.base = base
        for _ in range(50):
            page = vast.call("GET", "/instances/" + (f"?next_token={token}" if token else ""), purpose=purpose)
            items += page.get("instances") or []
            token = page.get("next_token")
            if not token or not page.get("instances"):
                break
    finally:
        vast.base = saved
    return items


def numeric_driver(version: str) -> int:
    """580.95.05 -> 580095005, the form Vast's search compares (as the Vast CLI encodes it)."""
    parts = (str(version).split(".") + ["0", "0"])[:3]
    return int("".join(p.zfill(3) for p in parts))


def branch_range(branch: str) -> tuple[int, int]:
    """R580 -> [580000000, 581000000)."""
    major = int(branch.upper().removeprefix("R"))
    return major * 1_000_000, (major + 1) * 1_000_000
