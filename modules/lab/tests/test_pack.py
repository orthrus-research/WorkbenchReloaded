"""lab.pack against a fake download server: files per side by env, optional ones when asked, overrides laid over,
the cache reused, a host not allowed, a path that leaves the folder, and bytes other than the SHA-512."""

import hashlib
import io
import json
import shutil
import sys
import tempfile
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path[:0] = [str(REPO / "core"), str(REPO / "sdk" / "python"), str(REPO / "modules" / "lab")]

from wb_core.module import Context, ParamError  # noqa: E402
from wb_lab import pack  # noqa: E402

FILES: dict[str, bytes] = {}
HITS: list[str] = []


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        HITS.append(self.path)
        body = FILES.get(self.path)
        if body is None:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


BOTH, CLIENT_ONLY, SERVER_ONLY = b"a mod on both sides", b"a client tweak", b"a server tool"


def mrpack(base: str, files: list[dict], extra: dict | None = None) -> bytes:
    index = {"formatVersion": 1, "game": "minecraft", "versionId": "1.2.0", "name": "Test Pack",
             "dependencies": {"minecraft": "1.12.2", "forge": "14.23.5.2860"}, "files": files}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("modrinth.index.json", json.dumps(index))
        archive.writestr("overrides/config/common.cfg", "both=true\n")
        archive.writestr("client-overrides/options.txt", "fov:90\n")
        archive.writestr("server-overrides/server.properties", "motd=pack\n")
        for name, data in (extra or {}).items():
            archive.writestr(name, data)
    return buffer.getvalue()


def entry(base: str, path: str, data: bytes, env: dict | None = None, served: bytes | None = None) -> dict:
    url = f"/files/{hashlib.sha1(data).hexdigest()}"
    FILES[url] = data if served is None else served
    item = {"path": path, "hashes": {"sha1": hashlib.sha1(data).hexdigest(), "sha512": hashlib.sha512(data).hexdigest()},
            "downloads": [base + url], "fileSize": len(data)}
    if env:
        item["env"] = env
    return item


class Packs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="wb-pack-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        FILES.clear()
        HITS.clear()
        self.config = {"pack_cache": str(self.root / "cache"), "pack_hosts": ["127.0.0.1"]}
        self.files = [entry(self.base, "mods/both.jar", BOTH),
                      entry(self.base, "mods/tweak.jar", CLIENT_ONLY, {"client": "optional", "server": "unsupported"}),
                      entry(self.base, "mods/tool.jar", SERVER_ONLY, {"client": "unsupported", "server": "required"})]

    def run_pack(self, name: str, data: bytes, side: str = "client", optional: bool = False):
        source = self.root / f"{name}.mrpack"
        source.write_bytes(data)
        ctx = Context({"job": "direct", "command": "lab.pack", "module": "lab", "work": str(self.root / name / "w"),
                       "out": str(self.root / name / "out"), "module_config": self.config}, lambda e: None)
        result = pack.pack(ctx, str(source), side, optional)
        return ctx, result, self.root / name / "out" / "pack"

    def test_each_side_takes_its_files_and_overrides(self):
        ctx, result, folder = self.run_pack("client", mrpack(self.base, self.files))
        self.assertEqual(ctx.outcome, "ok", ctx.findings)
        self.assertEqual(sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file()),
                         ["config/common.cfg", "mods/both.jar", "options.txt"])  # the optional tweak left out
        _, _, with_optional = self.run_pack("optional", mrpack(self.base, self.files), optional=True)
        self.assertTrue((with_optional / "mods" / "tweak.jar").is_file())
        _, _, server = self.run_pack("server", mrpack(self.base, self.files), side="server")
        self.assertEqual(sorted(p.relative_to(server).as_posix() for p in server.rglob("*") if p.is_file()),
                         ["config/common.cfg", "mods/both.jar", "mods/tool.jar", "server.properties"])
        receipt = json.loads((self.root / "client" / "out" / "receipt.json").read_text(encoding="utf-8"))
        self.assertEqual(receipt["dependencies"]["minecraft"], "1.12.2")
        self.assertEqual(receipt["skipped"], ["mods/tool.jar", "mods/tweak.jar"])

    def test_a_file_held_in_the_cache_is_not_downloaded_again(self):
        self.run_pack("first", mrpack(self.base, self.files))
        HITS.clear()
        ctx, result, _ = self.run_pack("again", mrpack(self.base, self.files))
        self.assertEqual(ctx.outcome, "ok", ctx.findings)
        self.assertEqual(HITS, [])

    def test_other_bytes_and_hosts_not_allowed_are_findings(self):
        files = [entry(self.base, "mods/both.jar", BOTH, served=b"tampered")]
        ctx, result, folder = self.run_pack("tampered", mrpack(self.base, files))
        self.assertTrue(any("other bytes than the SHA-512" in f for f in ctx.findings), ctx.findings)
        self.assertFalse((folder / "mods" / "both.jar").exists())
        self.config["pack_hosts"] = ["cdn.modrinth.com"]
        ctx, _, _ = self.run_pack("hosts", mrpack(self.base, [entry(self.base, "mods/both.jar", BOTH)]))
        self.assertTrue(any("no download from a host pack_hosts allows" in f for f in ctx.findings), ctx.findings)

    def test_a_path_that_leaves_the_pack_is_refused(self):
        drive = "C" + ":/x.jar"  # a path with a drive letter
        for bad in ("../outside.jar", "/etc/x.jar", drive):
            with self.subTest(path=bad):
                with self.assertRaises(ParamError):
                    self.run_pack("escape", mrpack(self.base, [entry(self.base, bad, BOTH)]))


if __name__ == "__main__":
    unittest.main()
