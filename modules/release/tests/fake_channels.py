"""Fake publishing services for the release tests: GitHub releases, Modrinth, CurseForge (its upload API and its Core
API) and a Maven repository. Each is an http.server on 127.0.0.1 that keeps what it was sent and records every
request, so a test can check exactly what a publish sent, and that nothing was sent before a yes."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import threading
import urllib.parse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


@dataclass
class Request:
    method: str
    path: str
    query: dict
    headers: dict
    body: bytes


@dataclass
class Part:
    name: str
    filename: str | None
    content_type: str | None
    body: bytes


def multipart(content_type: str, body: bytes) -> dict[str, Part]:
    """The parts of a multipart/form-data body, by name, their bytes exactly as sent."""
    boundary = re.search(r'boundary="?([^";]+)"?', content_type).group(1).encode()
    parts = {}
    chunks = body.split(b"--" + boundary)
    assert chunks[-1] in (b"--\r\n", b"--"), "the body does not end with the closing boundary"
    for chunk in chunks[1:-1]:
        assert chunk.startswith(b"\r\n") and chunk.endswith(b"\r\n"), "a part is not framed by CRLF"
        head, _, content = chunk[2:-2].partition(b"\r\n\r\n")
        headers = {}
        for line in head.decode("utf-8").split("\r\n"):
            key, _, value = line.partition(":")
            headers[key.strip().lower()] = value.strip()
        disposition = headers.get("content-disposition", "")
        name = re.search(r'(?:^|;)\s*name="([^"]*)"', disposition).group(1)
        filename = re.search(r'filename="([^"]*)"', disposition)
        parts[name] = Part(name, filename.group(1) if filename else None, headers.get("content-type"), content)
    return parts


def answer(status: int, value=None, headers: dict | None = None) -> tuple:
    body = b"" if value is None else value if isinstance(value, bytes) else json.dumps(value).encode()
    return status, {"Content-Type": "application/json", **(headers or {})}, body


class Service:
    def __init__(self):
        self.lock = threading.Lock()
        self.requests: list[Request] = []
        self.url = ""

    def handle(self, request: Request) -> tuple:  # pragma: no cover - each service has its own
        return answer(404, {"error": "not found"})

    def sent(self, method: str | None = None, pattern: str = "") -> list[Request]:
        return [r for r in self.requests if (method is None or r.method == method) and re.search(pattern, r.path)]


def serve(service: Service) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _dispatch(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            parts = urllib.parse.urlsplit(self.path)
            request = Request(self.command, parts.path, dict(urllib.parse.parse_qsl(parts.query)),
                              {k.lower(): v for k, v in self.headers.items()}, body)
            with service.lock:
                service.requests.append(request)
                status, headers, payload = service.handle(request)
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)

        do_GET = do_POST = do_PUT = do_HEAD = do_DELETE = _dispatch

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    service.url = f"http://127.0.0.1:{server.server_address[1]}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def sha(algo: str, data: bytes) -> str:
    return hashlib.new(algo, data).hexdigest()


class GitHub(Service):
    """Releases of one repository; uploads under /uploads, as a separate uploads host would take them."""

    def __init__(self, token: str, project: str = "example/examplemod"):
        super().__init__()
        self.token, self.project = token, project
        self.releases: list[dict] = []
        self.assets: dict[int, bytes] = {}
        self.next_id = 100
        self.fail_asset: str | None = None  # an upload of this name fails, echoing the request's credentials

    def handle(self, r: Request) -> tuple:
        if r.headers.get("authorization") != f"Bearer {self.token}":
            return answer(401, {"message": "Bad credentials"})
        base = f"/repos/{self.project}/releases"
        if r.method == "GET" and r.path == base:
            page, per = int(r.query.get("page", 1)), int(r.query.get("per_page", 30))
            return answer(200, self.releases[(page - 1) * per: page * per])
        if r.method == "POST" and r.path == base:
            payload = json.loads(r.body)
            if any(release["tag_name"] == payload["tag_name"] for release in self.releases):
                return answer(422, {"message": "Validation Failed", "errors": [{"code": "already_exists"}]})
            self.next_id += 1
            release = {"id": self.next_id, "tag_name": payload["tag_name"], "name": payload.get("name"),
                       "body": payload.get("body"), "draft": payload.get("draft", False),
                       "prerelease": payload.get("prerelease", False),
                       "target_commitish": payload.get("target_commitish", "main"),
                       "html_url": f"{self.url}/{self.project}/releases/tag/{payload['tag_name']}", "assets": []}
            self.releases.insert(0, release)
            return answer(201, release)
        match = re.fullmatch(rf"/uploads{base}/(\d+)/assets", r.path)
        if r.method == "POST" and match:
            release = next((x for x in self.releases if x["id"] == int(match.group(1))), None)
            if release is None:
                return answer(404, {"message": "Not Found"})
            if r.query["name"] == self.fail_asset:
                return answer(502, {"message": "upstream failed",
                                    "request": {"authorization": r.headers["authorization"]}})
            self.next_id += 1
            asset = {"id": self.next_id, "name": r.query["name"], "size": len(r.body),
                     "digest": "sha256:" + sha("sha256", r.body), "content_type": r.headers.get("content-type"),
                     "state": "uploaded",
                     "browser_download_url": f"{self.url}/{self.project}/releases/download/{release['tag_name']}/"
                                             f"{r.query['name']}"}
            release["assets"].append(asset)
            self.assets[asset["id"]] = r.body
            return answer(201, asset)
        return answer(404, {"message": "Not Found"})


class Modrinth(Service):
    def __init__(self, token: str, project: str = "AbCdEf12"):
        super().__init__()
        self.token, self.project = token, project
        self.versions: list[dict] = []
        self.files: dict[str, bytes] = {}

    def handle(self, r: Request) -> tuple:
        if r.method == "GET" and r.path == f"/v2/project/{self.project}/version":
            if r.headers.get("authorization") not in (None, self.token):
                return answer(401, {"error": "unauthorized"})
            return answer(200, self.versions)
        if r.method == "POST" and r.path == "/v2/version":
            if r.headers.get("authorization") != self.token:
                return answer(401, {"error": "unauthorized"})
            parts = multipart(r.headers["content-type"], r.body)
            data = json.loads(parts["data"].body)
            for key in ("name", "version_number", "game_versions", "version_type", "loaders", "featured",
                        "project_id", "file_parts", "dependencies"):
                if key not in data:
                    return answer(400, {"error": "invalid_input", "description": f"missing {key}"})
            if data["project_id"] != self.project or not data["loaders"] or not data["game_versions"]:
                return answer(400, {"error": "invalid_input"})
            version_id = f"v{len(self.versions) + 1:07d}"
            files = []
            for part_name in data["file_parts"]:
                part = parts[part_name]
                self.files[part.filename] = part.body
                files.append({"hashes": {"sha1": sha("sha1", part.body), "sha512": sha("sha512", part.body)},
                              "url": f"{self.url}/data/{self.project}/versions/{version_id}/{part.filename}",
                              "filename": part.filename, "primary": part_name == data.get("primary_file"),
                              "size": len(part.body)})
            version = {"id": version_id, "project_id": self.project, "files": files, "status": data.get("status"),
                       **{k: data.get(k) for k in ("name", "version_number", "changelog", "dependencies",
                                                   "game_versions", "version_type", "loaders", "featured")}}
            self.versions.insert(0, version)
            return answer(200, version)
        return answer(404, {"error": "not_found"})


class CurseForge(Service):
    """The upload API (X-Api-Token) and the Core API (x-api-key) of one project."""

    TYPES = [{"id": 628, "name": "Minecraft 1.12", "slug": "minecraft-1-12"},
             {"id": 2, "name": "Java", "slug": "java"},
             {"id": 68441, "name": "Modloader", "slug": "modloader"},
             {"id": 1, "name": "Bukkit 1.12", "slug": "bukkit-1-12"}]
    VERSIONS = [{"id": 6756, "gameVersionTypeID": 628, "name": "1.12.2", "slug": "1-12-2"},
                {"id": 6588, "gameVersionTypeID": 1, "name": "1.12.2", "slug": "1-12-2"},  # Bukkit's, never chosen
                {"id": 4458, "gameVersionTypeID": 2, "name": "Java 8", "slug": "java-8"},
                {"id": 7498, "gameVersionTypeID": 68441, "name": "Forge", "slug": "forge"}]

    def __init__(self, token: str, core_key: str, project: str = "123456"):
        super().__init__()
        self.token, self.core_key, self.project = token, core_key, project
        self.files: list[dict] = []
        self.next_id = 4000000

    def handle(self, r: Request) -> tuple:
        if r.path.startswith("/v1/"):
            if r.headers.get("x-api-key") != self.core_key:
                return answer(403, {"error": "forbidden"})
            if r.method == "GET" and r.path == f"/v1/mods/{self.project}/files":
                index, size = int(r.query.get("index", 0)), int(r.query.get("pageSize", 50))
                listed = [{"id": f["id"], "displayName": f["displayName"], "fileName": f["fileName"],
                           "releaseType": {"release": 1, "beta": 2, "alpha": 3}[f["metadata"]["releaseType"]],
                           "fileStatus": 4, "fileLength": len(f["data"]),
                           "hashes": [{"value": sha("sha1", f["data"]), "algo": 1},
                                      {"value": sha("md5", f["data"]), "algo": 2}],
                           "downloadUrl": f"{self.url}/files/{f['id']}/{f['fileName']}"} for f in self.files]
                return answer(200, {"data": listed[index:index + size],
                                    "pagination": {"index": index, "pageSize": size,
                                                   "resultCount": len(listed[index:index + size]),
                                                   "totalCount": len(listed)}})
            return answer(404, {"error": "not found"})
        if r.headers.get("x-api-token") != self.token:
            return answer(401, {"errorCode": 401, "errorMessage": "invalid token"})
        if r.method == "GET" and r.path == "/api/game/version-types":
            return answer(200, self.TYPES)
        if r.method == "GET" and r.path == "/api/game/versions":
            return answer(200, self.VERSIONS)
        if r.method == "POST" and r.path == f"/api/projects/{self.project}/upload-file":
            parts = multipart(r.headers["content-type"], r.body)
            metadata = json.loads(parts["metadata"].body)
            known = {v["id"] for v in self.VERSIONS}
            parent = metadata.get("parentFileID")
            if metadata.get("releaseType") not in ("alpha", "beta", "release") or "changelog" not in metadata:
                return answer(400, {"errorCode": 1001, "errorMessage": "invalid metadata"})
            if parent is not None and ("gameVersions" in metadata or parent not in {f["id"] for f in self.files}):
                return answer(400, {"errorCode": 1002, "errorMessage": "a child file takes no game versions"})
            if parent is None and not (metadata.get("gameVersions") and set(metadata["gameVersions"]) <= known):
                return answer(400, {"errorCode": 1003, "errorMessage": "unknown game versions"})
            self.next_id += 1
            file = parts["file"]
            self.files.append({"id": self.next_id, "fileName": file.filename, "data": file.body,
                               "displayName": metadata.get("displayName") or file.filename, "metadata": metadata,
                               "content_type": file.content_type})
            return answer(200, {"id": self.next_id})
        return answer(404, {"errorCode": 404})


class Maven(Service):
    """A repository under /releases that takes PUTs with basic auth and refuses to replace a released file."""

    def __init__(self, user: str, password: str):
        super().__init__()
        self.auth = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
        self.files: dict[str, bytes] = {}

    def handle(self, r: Request) -> tuple:
        if not r.path.startswith("/releases/"):
            return answer(404)
        if r.method == "PUT":
            if r.headers.get("authorization") != self.auth:
                return answer(401, headers={"WWW-Authenticate": 'Basic realm="releases"'})
            if r.path in self.files and "maven-metadata.xml" not in r.path:
                return answer(409, {"error": "a released file is never replaced"})
            self.files[r.path] = r.body
            return answer(201)
        if r.method in ("GET", "HEAD"):
            if r.path in self.files:
                return 200, {"Content-Type": "application/octet-stream"}, self.files[r.path]
            return answer(404)
        return answer(405)
