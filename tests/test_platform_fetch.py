"""store.platform: the archives a Set revision's platform names are fetched by SHA-256 from allowed hosts only, kept
by digest and unpacked once; other bytes, or a host outside the list, keep nothing."""

import hashlib
import io
import json
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness  # noqa: E402,F401

sys.path.insert(0, str(harness.REPO / "modules" / "ci"))
sys.path.insert(0, str(harness.REPO / "modules" / "tools"))
sys.path.insert(0, str(harness.REPO / "modules" / "build"))
from wb_core import setfile  # noqa: E402
from wb_core.module import Context, EnvironmentProblem, ParamError  # noqa: E402


def zipped() -> bytes:
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as packed:
        packed.writestr("jdk-26/bin/java.exe", b"windows java")
    return data.getvalue()


def tarred() -> bytes:
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as packed:
        body = b"linux java"
        info = tarfile.TarInfo("jdk-26/bin/java")
        info.size = len(body)
        packed.addfile(info, io.BytesIO(body))
    return data.getvalue()


class Fetch(unittest.TestCase):
    def setUp(self):
        from wb_ci import platform
        self.platform = platform
        self.folder = tempfile.TemporaryDirectory()
        base = Path(self.folder.name)
        self.store = base / "out" / "store"
        self.store.mkdir(parents=True)
        self.bodies = {"https://downloads.example.org/jdk-linux.tar.gz": tarred(),
                       "https://downloads.example.org/jdk-windows.zip": zipped(),
                       "https://dist.example.org/gradle-9.zip": zipped()}
        digest = {url: hashlib.sha256(body).hexdigest() for url, body in self.bodies.items()}
        self.set = {"schema": 1, "projects": {}, "third": {}, "holds": {}, "doctor": {}, "platform": {
            "java": 26, "jdk": {"26": {"build": "26.0.2.1+1",
                                       "linux": {"url": "https://downloads.example.org/jdk-linux.tar.gz",
                                                 "sha256": digest["https://downloads.example.org/jdk-linux.tar.gz"]},
                                       "windows": {"url": "https://downloads.example.org/jdk-windows.zip",
                                                   "sha256": digest["https://downloads.example.org/jdk-windows.zip"]}}},
            "gradle": {"version": "9.8.1", "url": "https://dist.example.org/gradle-9.zip",
                       "sha256": digest["https://dist.example.org/gradle-9.zip"]}}}
        self.revision = "a" * 40
        setfile.export(base / "out" / "sets", self.revision, setfile.dump(self.set))
        setfile.write_head(base / "out" / "sets", self.revision)

    def tearDown(self):
        self.folder.cleanup()

    def context(self, hosts):
        base = Path(self.folder.name)
        return Context({"job": "direct", "command": "ci.store-platform", "module": "ci", "work": str(base / "work"),
                        "out": str(base / "job-out"),
                        "module_config": {"store": str(self.store), "store_hosts": hosts,
                                          "pin_cache": str(base / "pins")}}, lambda *a, **k: None)

    def serve(self, url, hosts=()):
        return io.BytesIO(self.bodies[url])

    def test_fetched_once_kept_by_digest_and_unpacked(self):
        with mock.patch.object(self.platform, "_open", self.serve):
            first = self.platform.fetch(self.context(["example.org"]))
            again = self.platform.fetch(self.context(["example.org"]))
        self.assertEqual(len(first["fetched"]), 3)
        self.assertEqual((again["fetched"], len(again["held"])), ([], 3))
        self.assertEqual((self.store / "jdks" / "26.0.2.1+1" / "linux" / "jdk-26" / "bin" / "java").read_bytes(),
                         b"linux java")
        self.assertTrue((self.store / "jdks" / "26.0.2.1+1" / "windows" / "jdk-26" / "bin" / "java.exe").is_file())
        self.assertTrue((self.store / "gradle" / "9.8.1").is_dir())

    def test_a_host_outside_the_list_fetches_nothing(self):
        with mock.patch.object(self.platform, "_open", self.serve), self.assertRaises(ParamError) as raised:
            self.platform.fetch(self.context(["downloads.example.org"]))
        self.assertIn("store_hosts", str(raised.exception))
        self.assertFalse((self.store / "files").exists())

    def test_a_redirect_to_a_host_outside_the_list_is_refused(self):
        hops = self.platform._Hops(["example.org"])
        request = __import__("urllib.request").request.Request("https://dist.example.org/gradle-9.zip")
        with self.assertRaises(EnvironmentProblem) as raised:
            hops.redirect_request(request, None, 302, "Found", {}, "https://elsewhere.test/gradle-9.zip")
        self.assertIn("outside store_hosts", str(raised.exception))
        self.assertIsNotNone(hops.redirect_request(request, None, 302, "Found", {}, "https://cdn.example.org/g.zip"))

    def test_other_bytes_keep_nothing(self):
        self.bodies["https://dist.example.org/gradle-9.zip"] = b"tampered"
        with mock.patch.object(self.platform, "_open", self.serve), self.assertRaises(EnvironmentProblem):
            self.platform.fetch(self.context(["example.org"]), only=["gradle"])
        self.assertFalse((self.store / "gradle").exists())


class Plugins(unittest.TestCase):
    """The Set's packed plugins go into the Store's plugins folder once, by logic id, never replaced."""

    def test_unpacked_once_without_metadata_and_never_replaced(self):
        from wb_ci import commands
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "store"
            packed = Path(folder) / "set-plugins-1.zip"
            with zipfile.ZipFile(packed, "w") as archive:
                archive.writestr("set/base/set.base.gradle.plugin/1/set.base.gradle.plugin-1.pom", "<pom/>")
                archive.writestr("set/base/set.base.gradle.plugin/maven-metadata.xml", "<metadata/>")
            self.assertEqual(commands._store_plugins(root, packed), 1)
            self.assertEqual(commands._store_plugins(root, packed), 0)  # the same bytes: nothing to add
            self.assertFalse((root / "plugins" / "set/base/set.base.gradle.plugin/maven-metadata.xml").exists())
            with zipfile.ZipFile(packed, "w") as archive:
                archive.writestr("set/base/set.base.gradle.plugin/1/set.base.gradle.plugin-1.pom", "<other/>")
            with self.assertRaises(EnvironmentProblem):
                commands._store_plugins(root, packed)


if __name__ == "__main__":
    unittest.main()
