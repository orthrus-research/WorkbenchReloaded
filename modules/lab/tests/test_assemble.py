"""lab.assemble against a fake metadata and download server: components from the instance and the metadata, library
rules per system, a later component replacing an earlier library, both natives layouts, local libraries, assets,
the cache and candidate folders, and bytes other than the named SHA-1."""

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
from wb_lab import assemble  # noqa: E402


def jar(*members: tuple[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in members:
            archive.writestr(name, data)
    return buffer.getvalue()


def sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


FILES: dict[str, bytes] = {}
HITS: dict[str, int] = {}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - the http.server name
        HITS[self.path] = HITS.get(self.path, 0) + 1
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


LIB_A1 = jar(("a/One.class", b"one"))
LIB_A2 = jar(("a/Two.class", b"two"))
OSX_ONLY = jar(("o/Mac.class", b"mac"))
LWJGL_LINUX = jar(("liblwjgl.so", b"linux native"), ("META-INF/MANIFEST.MF", b"x"))
LWJGL_WINDOWS = jar(("lwjgl.dll", b"windows native"))
OLD = jar(("o/Old.class", b"old"))
OLD_LINUX = jar(("liboldnative.so", b"old linux native"), ("META-INF/x.so", b"not this"))
OLD_WINDOWS = jar(("oldnative.dll", b"old windows native"))
EMPTY = jar(("META-INF/MANIFEST.MF", b"empty"))
CLIENT = jar(("net/minecraft/client/Main.class", b"client"))
OBJECT_1, OBJECT_2 = b"a texture", b"a sound"
INDEX = json.dumps({"objects": {"textures/a.png": {"hash": sha1(OBJECT_1), "size": len(OBJECT_1)},
                                "sounds/b.ogg": {"hash": sha1(OBJECT_2), "size": len(OBJECT_2)},
                                "sounds/again.ogg": {"hash": sha1(OBJECT_2), "size": len(OBJECT_2)}}}).encode()


def download(name: str, data: bytes, base: str) -> dict:
    path = assemble.maven_path(name)
    FILES["/maven/" + path] = data
    return {"sha1": sha1(data), "size": len(data), "url": f"{base}/maven/{path}"}


def fixture(base: str, folder: Path) -> Path:
    """The served files and an instance .zip: the game from the metadata, LWJGL and a loader patched in."""
    FILES.clear()
    game = {"uid": "net.minecraft", "version": "1.12.2", "mainClass": "net.minecraft.client.main.Main",
            "minecraftArguments": "--username ${auth_player_name}",
            "mainJar": {"name": "com.mojang:minecraft:1.12.2:client",
                        "downloads": {"artifact": download("com.mojang:minecraft:1.12.2:client", CLIENT, base)}},
            "assetIndex": {"id": "1.12", "sha1": sha1(INDEX), "url": f"{base}/indexes/1.12.json"},
            "libraries": [
                {"name": "org.example:a:1.0", "downloads": {"artifact": download("org.example:a:1.0", LIB_A1, base)}},
                {"name": "org.example:osxonly:1.0", "rules": [{"action": "allow", "os": {"name": "osx"}}],
                 "downloads": {"artifact": download("org.example:osxonly:1.0", OSX_ONLY, base)}},
                {"name": "org.example:oldnative:1.0", "natives": {"linux": "natives-linux",
                                                                   "windows": "natives-windows"},
                 "downloads": {"artifact": download("org.example:oldnative:1.0", OLD, base),
                               "classifiers": {
                                   "natives-linux": download("org.example:oldnative:1.0:natives-linux", OLD_LINUX,
                                                             base),
                                   "natives-windows": download("org.example:oldnative:1.0:natives-windows",
                                                               OLD_WINDOWS, base)}}}]}
    FILES["/meta/net.minecraft/1.12.2.json"] = json.dumps(game).encode()
    FILES["/indexes/1.12.json"] = INDEX
    FILES["/objects/" + sha1(OBJECT_1)[:2] + "/" + sha1(OBJECT_1)] = OBJECT_1
    FILES["/objects/" + sha1(OBJECT_2)[:2] + "/" + sha1(OBJECT_2)] = OBJECT_2
    lwjgl = {"uid": "org.lwjgl", "version": "3.0", "libraries": [
        {"name": "org.lwjgl:lwjgl:3.0:natives-linux", "rules": [{"action": "allow", "os": {"name": "linux"}}],
         "downloads": {"artifact": download("org.lwjgl:lwjgl:3.0:natives-linux", LWJGL_LINUX, base)}},
        {"name": "org.lwjgl:lwjgl:3.0:natives-windows", "rules": [{"action": "allow", "os": {"name": "windows"}}],
         "downloads": {"artifact": download("org.lwjgl:lwjgl:3.0:natives-windows", LWJGL_WINDOWS, base)}}]}
    loader = {"uid": "net.loader", "version": "7.0", "mainClass": "net.loader.Boot", "+tweakers": ["net.loader.Tweak"],
              "+jvmArgs": ["-Dloader=1"], "libraries": [
                  {"name": "org.example:a:2.0", "downloads": {"artifact": download("org.example:a:2.0", LIB_A2,
                                                                                   base)}},
                  {"name": "com.example:empty:999", "MMC-hint": "local"}]}
    pack = {"formatVersion": 1, "components": [{"uid": "net.minecraft", "version": "1.12.2"},
                                               {"uid": "org.lwjgl", "version": "3.0"},
                                               {"uid": "net.loader", "version": "7.0"}]}
    instance = folder / "instance.zip"
    instance.write_bytes(jar(("mmc-pack.json", json.dumps(pack).encode()),
                             ("patches/org.lwjgl.json", json.dumps(lwjgl).encode()),
                             ("patches/net.loader.json", json.dumps(loader).encode()),
                             ("libraries/empty-999.jar", EMPTY),
                             ("instance.cfg", b"name=test")))
    return instance


class Assemble(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="wb-assemble-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.instance = fixture(self.base, self.root)
        self.cache = self.root / "cache"
        self.config = {"metadata": self.base + "/meta/{uid}/{version}.json", "asset_base": self.base + "/objects/",
                       "runtime_cache": str(self.cache)}
        HITS.clear()

    def run_assemble(self, name: str, **params):
        out = self.root / name
        ctx = Context({"job": "direct", "command": "lab.assemble", "module": "lab", "work": str(self.root / "work"),
                       "out": str(out), "module_config": self.config}, lambda event: None)
        result = assemble.assemble(ctx, str(params.pop("instance", self.instance)), **params)
        runtime = json.loads((out / "runtime.json").read_text(encoding="utf-8")) if result.get("runtime") else None
        return ctx, result, runtime, out

    def test_a_runtime_for_linux(self):
        ctx, result, runtime, out = self.run_assemble("linux", system="linux", arch="x86_64")
        self.assertEqual(ctx.outcome, "ok", result)
        self.assertEqual((runtime["main_class"], runtime["tweakers"], runtime["jvm_args"]),
                         ("net.loader.Boot", ["net.loader.Tweak"], ["-Dloader=1"]))
        self.assertEqual(runtime["game_arguments"], "--username ${auth_player_name}")
        names = [library["name"] for library in runtime["libraries"]]
        self.assertEqual(names[:2], ["org.example:a:2.0", "com.example:empty:999"])  # the loader's first
        self.assertNotIn("org.example:a:1.0", names)  # replaced by the later component's
        self.assertNotIn("org.example:osxonly:1.0", names)
        self.assertIn("org.lwjgl:lwjgl:3.0:natives-linux", names)
        self.assertNotIn("org.lwjgl:lwjgl:3.0:natives-windows", names)
        self.assertIn("org.example:oldnative:1.0:natives-linux", names)
        self.assertEqual(runtime["native_files"], ["liblwjgl.so", "liboldnative.so"])
        self.assertEqual((out / "natives" / "liblwjgl.so").read_bytes(), b"linux native")
        self.assertEqual(runtime["classpath"][-1], runtime["client"]["path"])
        self.assertEqual([c["source"] for c in runtime["components"]], ["metadata", "instance", "instance"])
        self.assertEqual((runtime["assets"]["entries"], runtime["assets"]["objects"]), (3, 2))  # two share one file
        for library in runtime["libraries"]:  # every file in the cache is the SHA-1 it is listed with
            self.assertEqual(assemble.sha1_file(Path(library["path"])), library["sha1"], library["name"])
        empty = next(lib for lib in runtime["libraries"] if lib["name"] == "com.example:empty:999")
        self.assertEqual(empty["sha1"], sha1(EMPTY))  # the instance's own library, by the bytes it carries
        self.assertEqual(result["fetched"]["instance"], 1)

    def test_a_second_runtime_downloads_nothing_again(self):
        self.run_assemble("first", system="linux", arch="x86_64")
        before = dict(HITS)
        ctx, result, runtime, _ = self.run_assemble("second", system="linux", arch="x86_64")
        self.assertEqual(ctx.outcome, "ok", result)
        self.assertEqual(HITS, before)  # neither the metadata nor a file was asked for again
        self.assertEqual(result["fetched"]["downloaded"], 0)

    def test_each_system_takes_its_own_libraries_and_natives(self):
        _, _, windows, out = self.run_assemble("windows", system="windows", arch="x86_64")
        names = [library["name"] for library in windows["libraries"]]
        self.assertIn("org.lwjgl:lwjgl:3.0:natives-windows", names)
        self.assertIn("org.example:oldnative:1.0:natives-windows", names)
        self.assertEqual(windows["native_files"], ["lwjgl.dll", "oldnative.dll"])
        _, _, osx, _ = self.run_assemble("osx", system="osx", arch="x86_64")
        names = [library["name"] for library in osx["libraries"]]
        self.assertIn("org.example:osxonly:1.0", names)
        self.assertFalse(any(":natives-" in name for name in names))

    def test_a_candidate_folder_is_used_before_downloading(self):
        candidates = self.root / "gradle-cache" / "deep" / "folder"
        candidates.mkdir(parents=True)
        (candidates / "a-2.0.jar").write_bytes(LIB_A2)
        (candidates / "client-wrong.jar").write_bytes(b"x")
        self.config["library_candidates"] = [str(self.root / "gradle-cache")]
        ctx, result, runtime, _ = self.run_assemble("candidates", system="linux", arch="x86_64", assets=False)
        self.assertEqual(ctx.outcome, "ok", result)
        self.assertEqual(result["fetched"]["candidate"], 1)
        self.assertNotIn("/maven/" + assemble.maven_path("org.example:a:2.0"), HITS)
        self.assertIsNone(runtime["assets"])

    def test_bytes_other_than_the_named_sha1_are_never_kept(self):
        FILES["/maven/" + assemble.maven_path("org.example:a:2.0")] = b"tampered"
        ctx, result, runtime, out = self.run_assemble("tampered", system="linux", arch="x86_64")
        self.assertEqual(ctx.outcome, "finding")
        self.assertIsNone(runtime)
        self.assertTrue(any("not the" in problem and "its metadata names" in problem for problem in result["problems"]))
        self.assertFalse((self.cache / "libraries" / assemble.maven_path("org.example:a:2.0")).exists())
        self.assertFalse((out / "runtime.json").exists())

    def test_a_component_with_no_patch_needs_the_metadata(self):
        self.config["metadata"] = ""
        with self.assertRaises(ParamError) as raised:
            self.run_assemble("nometa", system="linux", arch="x86_64")
        self.assertIn("[modules.lab] metadata", str(raised.exception))

    def test_an_instance_folder_gives_what_its_zip_gives(self):
        folder = self.root / "unzipped"
        with zipfile.ZipFile(self.instance) as archive:
            archive.extractall(folder)
        _, _, from_zip, _ = self.run_assemble("zip", system="linux", arch="x86_64", assets=False)
        _, _, from_folder, _ = self.run_assemble("folder", instance=folder, system="linux", arch="x86_64",
                                                 assets=False)
        self.assertEqual([lib["name"] for lib in from_folder["libraries"]],
                         [lib["name"] for lib in from_zip["libraries"]])

    def test_rules_follow_the_architecture(self):
        library = {"name": "org.lwjgl:lwjgl:3.0:natives-windows-arm64",
                   "rules": [{"action": "allow", "os": {"name": "windows-arm64"}}]}
        self.assertTrue(assemble.allowed(library, "windows", "aarch64", here=False))
        self.assertFalse(assemble.allowed(library, "windows", "x86_64", here=False))
        x86 = {"name": "org.example:legacy:1.0", "rules": [{"action": "allow"},
                                                           {"action": "disallow", "os": {"arch": "x86"}}]}
        self.assertTrue(assemble.allowed(x86, "windows", "x86_64", here=False))  # x86 rules mean 32-bit only
        self.assertFalse(assemble.allowed(x86, "windows", "x86", here=False))


if __name__ == "__main__":
    unittest.main()
