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


class Third(Fetch):
    """A third-party entry: each file fetched from its Maven repository on an allowed host, checked, kept."""

    def test_fetched_checked_and_kept(self):
        from wb_ci import land
        jar = zipped()
        url = "https://repo.example.org/maven/com/example/lib/1.0/lib-1.0.jar"
        self.bodies[url] = jar
        wanted = {"module": "com.example:lib:1.0", "repository": "https://repo.example.org/maven",
                  "files": {"runtime": {"sha256": hashlib.sha256(jar).hexdigest()}}, "licence": "MIT", "mod_id": "lib"}
        with mock.patch.object(self.platform, "_open", self.serve):
            entry, line = land._third(self.context(["example.org"]), "lib", wanted)
        self.assertEqual(entry["files"], {"runtime": hashlib.sha256(jar).hexdigest()})
        self.assertEqual((entry["licence"], entry["mod_id"]), ("MIT", "lib"))
        self.assertTrue((self.store / "files" / entry["files"]["runtime"] / "lib-1.0.jar").is_file())
        self.assertIn("third lib com.example:lib:1.0 (MIT)", line)
        wanted["files"]["runtime"]["sha256"] = "0" * 64
        with mock.patch.object(self.platform, "_open", self.serve), self.assertRaises(EnvironmentProblem):
            land._third(self.context(["example.org"]), "lib", wanted)  # other bytes: refused, nothing kept
        with self.assertRaises(ParamError):
            land._third(self.context(["elsewhere.test"]), "lib", wanted)  # a host outside the list


class ThirdFromFolder(Fetch):
    """A third-party file with no Maven publication: the one JAR in <out>/third/<name>/, seeded with its source."""

    def drop(self, *names, body=b"scala provider"):
        folder = Path(self.folder.name) / "out" / "third" / "scalar"
        folder.mkdir(parents=True, exist_ok=True)
        for name in names:
            (folder / name).write_bytes(body)
        return hashlib.sha256(body).hexdigest()

    def test_the_folders_jar_is_seeded_and_landed_with_its_source(self):
        from wb_ci import land
        digest = self.drop("Scalar Legacy-1.0.1.jar")
        wanted = {"source": "https://example.org/scalar/1.0.1", "licence": "LGPL-2.1", "mod_id": "scalar"}
        entry, line = land._third(self.context([]), "scalar", wanted)
        self.assertEqual(entry, {"file": "Scalar Legacy-1.0.1.jar", "sha256": digest,
                                 "source": "https://example.org/scalar/1.0.1", "licence": "LGPL-2.1", "mod_id": "scalar"})
        self.assertTrue((self.store / "files" / digest / "Scalar Legacy-1.0.1.jar").is_file())
        seeded = json.loads((self.store / "seeded" / f"{digest}.json").read_text(encoding="utf-8"))
        self.assertEqual((seeded["as"], seeded["source"]), ("third:scalar", "https://example.org/scalar/1.0.1"))
        self.assertIn("third scalar Scalar Legacy-1.0.1.jar from https://example.org/scalar/1.0.1 (LGPL-2.1)", line)
        again, _ = land._third(self.context([]), "scalar", {**wanted, "sha256": digest})  # the SHA-256 given matches
        self.assertEqual(again["sha256"], digest)

    def test_other_bytes_two_jars_or_no_source_are_refused(self):
        from wb_ci import land
        self.drop("a.jar")
        wanted = {"source": "https://example.org/scalar", "licence": "LGPL-2.1"}
        with self.assertRaisesRegex(ParamError, "not the 000000000000 given"):
            land._third(self.context([]), "scalar", {**wanted, "sha256": "0" * 64})
        with self.assertRaisesRegex(ParamError, "--source"):
            land._third(self.context([]), "scalar", {"licence": "LGPL-2.1"})
        self.drop("b.jar")
        with self.assertRaisesRegex(ParamError, "put its one JAR in .*found 2"):
            land._third(self.context([]), "scalar", wanted)
        with self.assertRaisesRegex(ParamError, "found 0"):
            land._third(self.context([]), "nothing", wanted)


class PlatformValues(Fetch):
    """A platform move is one typed value: each file it names is downloaded from its publisher, kept in the Store and
    named by the SHA-256 of those bytes, checked against the publisher's checksum file where there is one."""

    def setUp(self):
        super().setUp()
        temurin = "https://github.com/adoptium/temurin26-binaries/releases/download/jdk-26.0.3%2B4/OpenJDK26U-jdk_x64"
        self.jdk = {"linux": f"{temurin}_linux_hotspot_26.0.3_4.tar.gz",
                    "windows": f"{temurin}_windows_hotspot_26.0.3_4.zip"}
        self.bodies[self.jdk["linux"]] = tarred()
        self.bodies[self.jdk["windows"]] = zipped()
        for url in self.jdk.values():
            self.bodies[url + ".sha256.txt"] = (hashlib.sha256(self.bodies[url]).hexdigest() + "  x\n").encode()
        cleanroom = "https://github.com/CleanroomMC/Cleanroom/releases/download/0.7.15-alpha/cleanroom-0.7.15-alpha"
        self.cleanroom = [f"{cleanroom}-installer.jar", f"{cleanroom}-universal.jar", f"{cleanroom}.zip"]
        for index, url in enumerate(self.cleanroom):
            self.bodies[url] = f"cleanroom part {index}".encode()

    def serve(self, url, hosts=()):
        if url not in self.bodies:
            raise OSError(f"404 {url}")
        return io.BytesIO(self.bodies[url])

    def resolve(self, key, value):
        from wb_ci import platform
        with mock.patch.object(platform, "_open", self.serve):
            return platform.resolve(self.context(["github.com", "services.gradle.org"]), key, value, self.set["platform"])

    def test_a_jdk_build_names_both_archives_by_their_bytes(self):
        changed, line = self.resolve("jdk.26", "26.0.3+4")
        entry = changed["jdk"]["26"]
        self.assertEqual((entry["build"], entry["linux"]["url"]), ("26.0.3+4", self.jdk["linux"]))
        self.assertEqual(entry["windows"]["sha256"], hashlib.sha256(zipped()).hexdigest())
        self.assertTrue((self.store / "files" / entry["linux"]["sha256"]).is_dir())
        self.assertEqual(self.set["platform"]["jdk"]["26"]["build"], "26.0.2.1+1")  # the given platform is not changed
        self.assertIn("platform.jdk.26 = 26.0.3+4", line)

    def test_bytes_the_publishers_checksum_disagrees_with_are_refused(self):
        self.bodies[self.jdk["linux"] + ".sha256.txt"] = b"0" * 64
        with self.assertRaisesRegex(EnvironmentProblem, "its publisher states"):
            self.resolve("jdk.26", "26.0.3+4")

    def test_cleanroom_names_its_installer_universal_and_prism_zip(self):
        changed, _ = self.resolve("cleanroom", "0.7.15-alpha")
        entry = changed["cleanroom"]
        self.assertEqual(entry["version"], "0.7.15-alpha")
        self.assertEqual(entry["installer"]["file"], "cleanroom-0.7.15-alpha-installer.jar")
        self.assertEqual(entry["prism"]["sha256"], hashlib.sha256(b"cleanroom part 2").hexdigest())

    def test_plain_values_are_typed_and_unknown_keys_refused(self):
        changed, _ = self.resolve("java", "27")
        self.assertEqual(changed["java"], 27)
        with self.assertRaisesRegex(ParamError, "is int"):
            self.resolve("java", "twenty-seven")
        with self.assertRaisesRegex(ParamError, "not a key a landing sets"):
            self.resolve("color", "blue")
        with self.assertRaisesRegex(ParamError, "written like 26.0.2.1"):
            self.resolve("jdk.26", "latest")


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
