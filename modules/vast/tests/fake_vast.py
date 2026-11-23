"""A fake Vast.ai API and a fake SSH transport, so the rental flow can be tested without money or a machine."""

from __future__ import annotations

import io
import json
import os
import re
import tarfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.offers = {"777": {"id": 777, "gpu_name": "RTX 3060", "num_gpus": 1, "gpu_ram": 12288, "dph_total": 0.082,
                               "driver_version": "580.95.05", "cpu_cores_effective": 12, "cpu_name": "i7-8700",
                               "reliability": 0.99, "geolocation": "Japan", "machine_id": 41213, "host_id": 9,
                               "inet_down_cost": 0.002, "storage_cost": 0.1}}
        self.instances: dict[str, dict] = {}
        self.next_id = 61700000
        self.destroyed: list[str] = []
        self.billed_override: float | None = None
        self.listing_gone = False  # answer the listing with HTTP 410, as a retired endpoint does
        self.calls: list[tuple] = []


STATE = State()


def storage_dph(offer: dict, gb: float) -> float:
    """What a disk adds per hour: Vast lists storage per GB-month."""
    return float(offer.get("storage_cost") or 0) * gb / (30 * 24)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, body, status=200):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}") if length else {}

    def _auth(self):
        return self.headers.get("Authorization") == "Bearer fake-key"

    def do_POST(self):
        if not self._auth():
            return self._send({"error": "auth"}, 401)
        body = self._body()
        STATE.calls.append(("POST", self.path, body))
        if self.path == "/bundles/":
            card = body.get("gpu_name", {}).get("eq")
            cap = body.get("dph_total", {}).get("lte", 99)
            storage = float(body.get("allocated_storage", 5.0))  # Vast prices a small default disk unless asked
            offers = [dict(o, dph_total=o["dph_total"] + storage_dph(o, storage)) for o in STATE.offers.values()
                      if o["gpu_name"] == card]
            return self._send({"offers": [o for o in offers if o["dph_total"] <= cap]})
        if re.match(r"^/instances/\d+/ssh$", self.path):
            return self._send({"success": True})
        self._send({"error": "no route"}, 404)

    def do_PUT(self):
        body = self._body()
        STATE.calls.append(("PUT", self.path, body))
        match = re.match(r"^/asks/(\d+)/$", self.path)
        if match:
            offer = STATE.offers.get(match.group(1))
            if offer is None:
                return self._send({"error": "gone"}, 410)
            with STATE.lock:
                STATE.next_id += 1
                instance = str(STATE.next_id)
                STATE.instances[instance] = {"id": int(instance), "label": body.get("label"), "actual_status": "running",
                                             "ssh_host": "ssh9.vast.ai", "ssh_port": 12345, "public_ipaddr": "203.0.113.7",
                                             "ports": {"22/tcp": [{"HostPort": "40022"}]},
                                             "dph_total": STATE.billed_override or
                                             offer["dph_total"] + storage_dph(offer, float(body.get("disk") or 0))}
            return self._send({"success": True, "new_contract": int(instance)})
        if self.path.startswith("/instances/request_logs/"):
            return self._send({"result_url": ""})
        self._send({"error": "no route"}, 404)

    def do_GET(self):
        STATE.calls.append(("GET", self.path, None))
        if (self.path == "/instances/" or self.path.startswith("/instances/?")) and STATE.listing_gone:
            return self._send({"success": False, "error": "deprecated_endpoint"}, 410)
        if self.path == "/instances/" or self.path.startswith("/instances/?"):  # the v1 listing, paged
            return self._send({"success": True, "instances": list(STATE.instances.values()), "next_token": None})
        match = re.match(r"^/instances/(\d+)/$", self.path)
        if match:
            return self._send({"instances": STATE.instances.get(match.group(1), {})})
        if self.path.startswith("/charges/"):
            return self._send({"results": [{"source": "instance-61700001", "start": 1795000000, "amount": 0.03,
                                            "items": [{"type": "gpu", "amount": 0.03}]}]})
        self._send({"error": "no route"}, 404)

    def do_DELETE(self):
        STATE.calls.append(("DELETE", self.path, None))
        match = re.match(r"^/instances/(\d+)/$", self.path)
        if match:
            STATE.instances.pop(match.group(1), None)
            STATE.destroyed.append(match.group(1))
            return self._send({"success": True})
        self._send({"error": "no route"}, 404)


def serve() -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


RESULTS_ENV = "WB_VAST_FAKE_RESULTS"  # {member: text} as JSON: what every fetched .tar holds


class FakeTransport:
    """Plays a machine: bootstrap prints BOOT ready, the run prints RUN lines, results.tar comes back. It runs in the
    rental job's worker, so a test gives the fetched archives' members through the environment (RESULTS_ENV);
    without them a line of text stands in for each result."""

    def __init__(self, ctx, host, port):
        self.ctx, self.host, self.port = ctx, host, port
        self.log = ctx.work / "fake-remote.log"

    def _note(self, text):
        with open(self.log, "a", encoding="utf-8") as handle:
            handle.write(text + "\n")

    def run(self, command, timeout=600, check=True):
        self._note(f"run {command}")
        if "bootstrap.sh" in command:
            return "BOOT packages\nBOOT ready: Python 3.14.4, openjdk 26, vglrun yes\n"
        if command.startswith("tail"):
            return "RUN tour 1 forests.json as settle\nRUN tour 1 exit 0\nRUN done; results.tar 1K\n"
        return ""

    def put(self, files, destination, timeout=3600):
        for path in files:
            self._note(f"put {Path(path).name} -> {destination}")

    def get(self, remote, local, timeout=1800):
        local.mkdir(parents=True, exist_ok=True)
        members = json.loads(os.environ.get(RESULTS_ENV) or "{}")
        got = []
        for path in remote:
            target = local / Path(path).name
            if path.endswith(".tar") and members:
                with tarfile.open(target, "w") as archive:
                    for name, text in sorted(members.items()):
                        data = text.encode("utf-8")
                        info = tarfile.TarInfo(name)
                        info.size = len(data)
                        archive.addfile(info, io.BytesIO(data))
            else:
                target.write_text(f"fake {path}\n", encoding="utf-8")
            got.append(target)
        return got


def make(ctx, host, port):
    return FakeTransport(ctx, host, port)
