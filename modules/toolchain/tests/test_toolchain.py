"""toolchain: a pinned dependency rebuilt in a clean tree and kept only at its pin (the pin cache read first), a JAR
repaired by swapping exactly the classes named, and the rescue repository: its Maven layout, digests, POMs, metadata
and receipts, its refusals, and the init script that serves it. The compile tests need a JDK 9 or newer, from
JAVA_HOME or where Workbench finds JDKs, and are skipped without one; the build that reads the init script needs a
Gradle distribution already on this machine as well (none is downloaded), and is skipped without one."""

import functools
import hashlib
import http.server
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import xml.etree.ElementTree as ElementTree
import zipfile
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path[:0] = [str(REPO / "tests"), str(REPO / "modules" / "jar" / "tests")]
from classwriter import INVOKESPECIAL, ClassWriter  # noqa: E402
from harness import Daemon  # noqa: E402

sys.path[:0] = [str(REPO / "modules" / "build"), str(REPO / "modules" / "toolchain")]
from wb_core.module import Context, EnvironmentProblem  # noqa: E402
from wb_core.requirements import find_jdks  # noqa: E402
from wb_toolchain import commands, gradle, maven, repository  # noqa: E402


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args], cwd=cwd,
                          check=True, capture_output=True, text=True).stdout.strip()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def scratch(test: unittest.TestCase, prefix: str) -> Path:
    """A temporary folder that goes when the test does."""
    folder = Path(tempfile.mkdtemp(prefix=prefix))
    test.addCleanup(shutil.rmtree, folder, ignore_errors=True)
    return folder


def _major(home: Path) -> int | None:
    release = home / "release"
    if not release.is_file():
        return None
    for line in release.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("JAVA_VERSION="):
            version = line.split("=", 1)[1].strip().strip('"')
            match = re.match(r"1\.(\d+)|(\d+)", version)
            return int(match.group(1) or match.group(2)) if match else None
    return None


def find_jdk() -> tuple[int, Path] | None:
    """The newest JDK whose javac takes --release: JAVA_HOME, or one Workbench finds."""
    homes = [Path(os.environ["JAVA_HOME"])] if os.environ.get("JAVA_HOME") else []
    homes += [Path(h) for h in find_jdks(SimpleNamespace(core={"jdks": {"root": "/opt/jdk"}})).values()]
    best = None
    for home in homes:
        major = _major(home)
        javac = home / "bin" / ("javac.exe" if os.name == "nt" else "javac")
        if major and major >= 9 and javac.is_file() and (best is None or major > best[0]):
            best = (major, home)
    return best


JDK = find_jdk()
BUILD = """import pathlib
data = pathlib.Path("a.txt").read_bytes()
pathlib.Path("out").mkdir(exist_ok=True)
pathlib.Path("out", "lib-1.0.jar").write_bytes(b"lib:" + data)
"""


class Rebuild(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(tempfile.mkdtemp(prefix="wb-rebuild-"))
        cls.cache = root / "pins"
        cls.d = Daemon(f"""
            [modules.toolchain]
            pin_cache = '{cls.cache.as_posix()}'
            """, root=root).start()
        cls.c = cls.d.client
        cls.repo = cls.d.data / "dep"
        cls.repo.mkdir()
        git(cls.repo, "init", "-q", "-b", "main")
        (cls.repo / "build.py").write_text(BUILD, encoding="utf-8")
        (cls.repo / ".gitignore").write_text("out/\n", encoding="utf-8")
        (cls.repo / "a.txt").write_bytes(b"one")
        git(cls.repo, "add", "-A")
        git(cls.repo, "commit", "-q", "-m", "one")
        cls.first = git(cls.repo, "rev-parse", "HEAD")
        (cls.repo / "a.txt").write_bytes(b"two")
        git(cls.repo, "commit", "-q", "-am", "two")
        cls.second = git(cls.repo, "rev-parse", "HEAD")
        cls.pin = sha(b"lib:one")
        cls.runs = 0

    @classmethod
    def tearDownClass(cls):
        cls.d.stop()

    def rebuild(self, **params):
        Rebuild.runs += 1
        out = self.d.out / f"run-{Rebuild.runs}"
        base = {"repo": str(self.repo), "commit": self.first, "steps": ["{python} build.py"],
                "artifact": "out/lib-*.jar", "name": "lib.jar", "sha256": self.pin}
        result = self.c.submit("toolchain.rebuild", {**base, **params}, out=str(out)).result(timeout=300)
        return result, out

    def clear_cache(self):
        for path in self.cache.glob("*") if self.cache.is_dir() else []:
            path.unlink()

    def test_rebuilds_at_the_pin_and_fills_the_cache(self):
        self.clear_cache()
        built, out = self.rebuild()
        self.assertEqual(built.outcome, "ok", built.summary)
        self.assertEqual((built.data.commit, built.data.cached), (self.first, False))
        self.assertEqual((out / "lib.jar").read_bytes(), b"lib:one")
        self.assertEqual((out / "lib.jar.sha256").read_text(encoding="utf-8"), f"{self.pin}  lib.jar\n")
        self.assertEqual((self.cache / self.pin).read_bytes(), b"lib:one")
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")  # the checkout was only read
        self.assertFalse((self.repo / "out").exists())
        failing = ['{python} -c "raise SystemExit(7)"']
        cached, out = self.rebuild(steps=failing)  # the cache has the pin: nothing is built
        self.assertEqual((cached.outcome, cached.data.cached), ("ok", True), cached.summary)
        self.assertEqual((out / "lib.jar").read_bytes(), b"lib:one")
        forced, out = self.rebuild(steps=failing, cached=False)
        self.assertEqual(forced.outcome, "finding", forced.summary)
        self.assertIn("exited 7", forced.summary)
        self.assertFalse((out / "lib.jar").exists())

    def test_a_build_that_is_not_the_pin_keeps_nothing(self):
        result, out = self.rebuild(commit=self.second, cached=False)
        self.assertEqual(result.outcome, "finding", result.summary)
        self.assertIn("not the pinned", result.summary)
        self.assertEqual(result.data.sha256, sha(b"lib:two"))
        self.assertFalse((out / "lib.jar").exists())
        self.assertFalse((self.cache / sha(b"lib:two")).exists())

    def test_a_damaged_cache_entry_is_built_again(self):
        self.cache.mkdir(parents=True, exist_ok=True)
        (self.cache / self.pin).write_bytes(b"damaged")
        result, out = self.rebuild()
        self.assertEqual((result.outcome, result.data.cached), ("ok", False), result.summary)
        self.assertEqual((self.cache / self.pin).read_bytes(), b"lib:one")

    def test_recipes_are_checked(self):
        unnamed, _ = self.rebuild(name="", cached=False)
        self.assertEqual(unnamed.outcome, "usage", unnamed.summary)  # a wildcard file name needs name=
        escape, _ = self.rebuild(artifact="../x.jar", cached=False)
        self.assertEqual(escape.outcome, "usage", escape.summary)
        unknown, _ = self.rebuild(commit="f" * 40, cached=False)
        self.assertEqual(unknown.outcome, "usage", unknown.summary)
        nothing, _ = self.rebuild(artifact="out/none-*.jar", cached=False)
        self.assertEqual(nothing.outcome, "finding", nothing.summary)
        self.assertIn("not one", nothing.summary)

    def test_the_pin_cache_folder(self):
        root = scratch(self, "wb-pin-cache-")
        ctx = Context({"job": "direct", "command": "toolchain.rebuild", "module": "toolchain",
                       "work": str(root / "work"), "out": str(root / "out"),
                       "module_config": {"pin_cache": str(root / "configured")}}, lambda event: None)
        saved = {k: os.environ.pop(k, None) for k in ("WB_PIN_CACHE", "WB_STATE_DIR")}
        try:
            self.assertEqual(commands.pin_cache(ctx), root / "configured")
            os.environ["WB_PIN_CACHE"] = str(root / "from-env")
            self.assertEqual(commands.pin_cache(ctx), root / "from-env")  # a job's own cache comes first
            del os.environ["WB_PIN_CACHE"]
            ctx.config = {"pin_cache": ""}
            os.environ["WB_STATE_DIR"] = str(root / "state")
            self.assertEqual(commands.pin_cache(ctx), root / "state" / "cache" / "pins")
        finally:
            for key, value in saved.items():
                os.environ.pop(key, None)
                if value is not None:
                    os.environ[key] = value


GREETER = """package demo;
public class Greeter {
  public static String greet() { return "%s"; }
  public static void main(String[] args) { System.out.println(greet()); }
}
"""
OTHER = "package demo;\npublic class Other { public static int n() { return 1; } }\n"
STAMP = (2020, 5, 17, 10, 30, 0)


def javac(home: Path, sources: Path, out: Path) -> None:
    files = [str(p) for p in sources.rglob("*.java")]
    compiler = home / "bin" / ("javac.exe" if os.name == "nt" else "javac")
    for release in ("8", "17"):
        done = subprocess.run([str(compiler), "--release", release, "-Xlint:-options", "-d", str(out), *files],
                              capture_output=True, text=True)
        if done.returncode == 0:
            return
    raise AssertionError(done.stdout + done.stderr)


def write_jar(path: Path, entries: list[tuple[str, bytes, int]]) -> None:
    with zipfile.ZipFile(path, "w") as bundle:
        for name, data, method in entries:
            info = zipfile.ZipInfo(name, STAMP)
            info.compress_type = method
            info.external_attr = (0o40755 if name.endswith("/") else 0o100644) << 16
            bundle.writestr(info, data)


def sources(folder: Path, files: dict[str, str]) -> Path:
    for relative, text in files.items():
        path = folder / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return folder


class Patch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.saved_jdks = os.environ.get("WB_JDKS")
        if JDK:
            os.environ["WB_JDKS"] = json.dumps({str(JDK[0]): str(JDK[1])})  # the daemon offers it to jobs
        cls.d = Daemon().start()
        cls.c = cls.d.client
        data = cls.d.data
        if JDK:
            original = sources(data / "original", {"demo/Greeter.java": GREETER % "hello", "demo/Other.java": OTHER})
            javac(JDK[1], original, data / "classes")
            greeter = (data / "classes" / "demo" / "Greeter.class").read_bytes()
            other = (data / "classes" / "demo" / "Other.class").read_bytes()
        else:
            greeter = other = b"\xca\xfe\xba\xbe\x00\x00\x00\x34 not compiled"
        cls.entries = [("META-INF/", b"", zipfile.ZIP_STORED),
                       ("META-INF/MANIFEST.MF", b"Manifest-Version: 1.0\r\n\r\n", zipfile.ZIP_DEFLATED),
                       ("demo/", b"", zipfile.ZIP_STORED),
                       ("demo/Greeter.class", greeter, zipfile.ZIP_DEFLATED),
                       ("demo/Other.class", other, zipfile.ZIP_DEFLATED),
                       ("assets/readme.txt", b"unchanged", zipfile.ZIP_STORED)]
        cls.jar = data / "dep-1.0.jar"
        write_jar(cls.jar, cls.entries)
        cls.digest = sha(cls.jar.read_bytes())
        cls.fix = sources(data / "fix", {"demo/Greeter.java": GREETER % "patched"})
        cls.runs = 0

    @classmethod
    def tearDownClass(cls):
        cls.d.stop()
        os.environ.pop("WB_JDKS", None)
        if cls.saved_jdks is not None:
            os.environ["WB_JDKS"] = cls.saved_jdks

    def patch(self, **params):
        Patch.runs += 1
        out = self.d.out / f"patch-{Patch.runs}"
        base = {"jar": str(self.jar), "sha256": self.digest, "sources": str(self.fix), "classes": ["demo.Greeter"],
                "jdk": str(JDK[0]) if JDK else "26"}
        return self.c.submit("toolchain.patch", {**base, **params}, out=str(out)).result(timeout=300), out

    def test_other_bytes_are_refused(self):
        result, out = self.patch(sha256="0" * 64)
        self.assertEqual(result.outcome, "finding", result.summary)
        self.assertIn("not the expected", result.summary)
        self.assertFalse((out / "dep-1.0-patched.jar").exists())

    def test_a_signed_jar_is_refused(self):
        signed = self.d.data / "signed.jar"
        write_jar(signed, self.entries + [("META-INF/SIGNER.SF", b"Signature-Version: 1.0\r\n", zipfile.ZIP_DEFLATED)])
        result, _ = self.patch(jar=str(signed), sha256=sha(signed.read_bytes()))
        self.assertEqual(result.outcome, "finding", result.summary)
        self.assertIn("signed", result.summary)

    @unittest.skipUnless(JDK, "no JDK 9 or newer here")
    def test_swaps_only_the_named_class(self):
        result, out = self.patch()
        self.assertEqual(result.outcome, "ok", result.summary)
        patched = out / "dep-1.0-patched.jar"
        self.assertEqual(sha(patched.read_bytes()), result.data.sha256)
        with zipfile.ZipFile(self.jar) as before, zipfile.ZipFile(patched) as after:
            self.assertEqual(after.namelist(), before.namelist())
            changed = [n for n in before.namelist() if before.read(n) != after.read(n)]
            self.assertEqual(changed, ["demo/Greeter.class"])
            for old, new in zip(before.infolist(), after.infolist(), strict=True):
                self.assertEqual((new.date_time, new.compress_type, new.external_attr),
                                 (old.date_time, old.compress_type, old.external_attr), new.filename)
            level = after.read("demo/Greeter.class")[6:8]
            self.assertEqual(level, before.read("demo/Greeter.class")[6:8])  # the original's bytecode level
        receipt = json.loads((out / "dep-1.0-patched.json").read_text(encoding="utf-8"))
        self.assertEqual((receipt["input"]["sha256"], receipt["output"]["sha256"]), (self.digest, result.data.sha256))
        self.assertEqual([r["entry"] for r in receipt["replaced"]], ["demo/Greeter.class"])
        java = JDK[1] / "bin" / ("java.exe" if os.name == "nt" else "java")
        said = subprocess.run([str(java), "-cp", str(patched), "demo.Greeter"], capture_output=True, text=True)
        self.assertEqual(said.stdout.strip(), "patched", said.stderr)
        qualified, _ = self.patch(expect=result.data.sha256)  # the same inputs make the same bytes
        self.assertEqual((qualified.outcome, qualified.data.qualified), ("ok", True), qualified.summary)
        other, out = self.patch(expect="0" * 64)
        self.assertEqual(other.outcome, "finding", other.summary)
        self.assertTrue((out / "dep-1.0-patched.jar").exists())  # kept, to requalify

    @unittest.skipUnless(JDK, "no JDK 9 or newer here")
    def test_sources_must_compile_to_exactly_the_classes_named(self):
        extra = sources(self.d.data / "extra", {"demo/Greeter.java": GREETER % "patched",
                                                "demo/Helper.java": "package demo;\nclass Helper {}\n"})
        more, _ = self.patch(sources=str(extra))
        self.assertEqual(more.outcome, "usage", more.summary)
        self.assertIn("demo/Helper.class", more.summary)
        absent, _ = self.patch(classes=["demo.Missing"])
        self.assertEqual(absent.outcome, "usage", absent.summary)
        broken = sources(self.d.data / "broken", {"demo/Greeter.java": "package demo;\npublic class Greeter {"})
        failed, _ = self.patch(sources=str(broken))
        self.assertEqual(failed.outcome, "finding", failed.summary)
        self.assertIn("javac exited", failed.summary)


# ---------------------------------------------------------------------------------------------
# the rescue repository

POM_NS = {"m": "http://maven.apache.org/POM/4.0.0"}
REASON = "vanished from its public repository; these bytes are the release asset"
ORIGINAL_POM = b"""<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <parent><groupId>com.example</groupId><artifactId>parent</artifactId><version>1.0</version></parent>
  <artifactId>withpom</artifactId>
  <dependencies>
    <dependency><groupId>com.example</groupId><artifactId>helper</artifactId><version>2.0</version></dependency>
  </dependencies>
</project>
"""


def sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def pom_fields(path: Path) -> dict:
    root = ElementTree.fromstring(path.read_bytes())
    dependencies = [":".join(d.findtext(f"m:{tag}", namespaces=POM_NS) for tag in ("groupId", "artifactId", "version"))
                    for d in root.findall("m:dependencies/m:dependency", POM_NS)]
    fields = [root.findtext(f"m:{tag}", namespaces=POM_NS) for tag in ("groupId", "artifactId", "version")]
    return {"coordinates": ":".join(fields), "packaging": root.findtext("m:packaging", namespaces=POM_NS),
            "dependencies": dependencies}


def metadata_fields(path: Path) -> dict:
    root = ElementTree.fromstring(path.read_bytes())
    return {"latest": root.findtext("versioning/latest"), "release": root.findtext("versioning/release"),
            "versions": [v.text for v in root.findall("versioning/versions/version")]}


def direct_context(root: Path, config: dict, roots: list | None = None) -> Context:
    """A Context for calling the repository's functions directly."""
    return Context({"job": "direct", "command": "toolchain.repo-init", "module": "toolchain",
                    "work": str(root / "work"), "out": str(root / "out"), "module_config": config,
                    "roots": roots or []}, lambda event: None)


def library_jar(name: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr(zipfile.ZipInfo(f"{name}.txt", STAMP), name)
    return buffer.getvalue()


def plugin_jar(*ids: str) -> bytes:
    """A JAR implementing the Gradle plugins ``ids`` with one class whose apply does nothing."""
    writer = ClassWriter("demo/RescuedPlugin", interfaces=["org/gradle/api/Plugin"])
    writer.method("<init>", "()V", code=b"\x2a" + writer.invoke(INVOKESPECIAL, "java/lang/Object", "<init>", "()V")
                  + b"\xb1")
    writer.method("apply", "(Ljava/lang/Object;)V", code=b"\xb1")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr(zipfile.ZipInfo("demo/RescuedPlugin.class", STAMP), writer.build())
        for plugin in ids:
            bundle.writestr(zipfile.ZipInfo(f"META-INF/gradle-plugins/{plugin}.properties", STAMP),
                            "implementation-class=demo.RescuedPlugin\n")
    return buffer.getvalue()


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def find_gradle(scratch_home: Path) -> tuple[Path, Path] | None:
    """A Gradle launcher already on this machine (none is downloaded) and the JDK it starts on: GRADLE_HOME, the
    distributions the wrapper unpacked, newest first, or gradle on PATH. Each is tried with a Gradle home of its own
    under ``scratch_home``, so the user's own is never touched."""
    if not JDK:
        return None
    homes = [Path(os.environ["GRADLE_HOME"])] if os.environ.get("GRADLE_HOME") else []
    dists = Path(os.environ.get("GRADLE_USER_HOME") or Path.home() / ".gradle") / "wrapper" / "dists"
    unpacked = [p for p in dists.glob("gradle-*/*/gradle-*") if p.is_dir()]
    homes += sorted(unpacked, key=lambda p: functools.cmp_to_key(maven.compare)(p.name.removeprefix("gradle-")),
                    reverse=True)
    launchers = [h / "bin" / ("gradle.bat" if os.name == "nt" else "gradle") for h in homes]
    launchers += [Path(found)] if (found := shutil.which("gradle")) else []
    for index, launcher in enumerate(launchers):
        if not launcher.is_file():
            continue
        try:
            done = subprocess.run([str(launcher), "--version", "--offline", "-g", str(scratch_home / f"probe-{index}")],
                                  capture_output=True, text=True, timeout=300,
                                  env={**os.environ, "JAVA_HOME": str(JDK[1])})
        except (OSError, subprocess.TimeoutExpired):
            continue
        if done.returncode == 0:
            return launcher, JDK[1]
    return None


PROBE_SETTINGS = """
rootProject.name = 'probe'
include 'app'
dependencyResolutionManagement {
    repositories { mavenCentral() }
}
"""
PROBE_ROOT = """
configurations { rescued }
dependencies { rescued 'com.example:vanished-lib:1.0' }
tasks.register('probe') {
    def files = configurations.rescued
    doLast { println 'ROOT ' + files.files.collect { it.name }.sort().join(',') }
}
"""
PROBE_APP = """
buildscript {
    repositories { mavenCentral() }
    dependencies { classpath 'com.example:vanished-lib:1.0' }
}
plugins {
    id 'com.example.rescued' version '1.0'
    id 'com.example.renamed' version '3.0'
}
repositories { mavenCentral() }
configurations { rescued; unheld }
dependencies {
    rescued 'com.example:vanished-lib:1.0'
    unheld 'com.example:unheld:1.0'
}
tasks.register('probe') {
    def files = configurations.rescued
    def unheld = configurations.unheld.incoming.artifactView { lenient = true }.files
    def classpath = buildscript.configurations.classpath
    def names = repositories.collect { it.name }
    def applied = [plugins.hasPlugin('com.example.rescued'), plugins.hasPlugin('com.example.renamed')]
    doLast {
        println 'APP ' + files.files.collect { it.name }.sort().join(',')
        println 'UNHELD ' + unheld.files.size()
        println 'CLASSPATH ' + classpath.files.collect { it.name }.sort().join(',')
        println 'REPOSITORIES ' + names.join(',')
        println 'PLUGINS ' + applied.join(',')
    }
}
"""


class Repository(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(tempfile.mkdtemp(prefix="wb-repository-"))
        cls.store = root / "out" / "repository"  # under the out root: builds on the host can read it
        cls.d = Daemon(f"""
            [modules.toolchain]
            repository = '{cls.store.as_posix()}'

            [modules.toolchain.substitutions]
            "com.example:vanished-lib:1.0" = "com.example:retained-lib:1.0-r1"
            "com.example.renamed:com.example.renamed.gradle.plugin:3.0" = "com.example:rescued-plugin:1.0"
            """, root=root).start()
        cls.c = cls.d.client

    @classmethod
    def tearDownClass(cls):
        cls.d.stop()

    def file(self, name: str, data: bytes) -> tuple[Path, str]:
        path = self.d.data / name
        path.write_bytes(data)
        return path, sha(data)

    def add(self, coordinates: str, data: bytes | None = None, *, name: str = "", **params):
        if data is not None:
            path, digest = self.file(name or coordinates.replace(":", "_").replace("@", "."), data)
            params = {"file": str(path), "sha256": digest, **params}
        params.setdefault("reason", REASON)
        return self.c.submit("toolchain.repo-add", {"coordinates": coordinates, **params}).result(timeout=120)

    def receipts(self) -> dict:
        return json.loads(text(self.store / "receipts.json"))["files"]

    def test_a_rescued_jar_gets_the_maven_layout_its_digests_a_pom_and_a_receipt(self):
        data = library_jar("layout")
        result = self.add("com.example:layout:1.0", data, dependencies=["com.example:helper:2.0"])
        self.assertEqual((result.outcome, result.data.state), ("ok", "new"), result.summary)
        folder = self.store / "com" / "example" / "layout"
        jar = folder / "1.0" / "layout-1.0.jar"
        self.assertEqual(jar.read_bytes(), data)
        self.assertEqual(text(folder / "1.0" / "layout-1.0.jar.sha256"), sha(data))  # the hex alone, as Maven has it
        self.assertEqual(text(folder / "1.0" / "layout-1.0.jar.sha1"), sha1(data))
        pom = folder / "1.0" / "layout-1.0.pom"
        self.assertEqual(pom_fields(pom), {"coordinates": "com.example:layout:1.0", "packaging": "jar",
                                           "dependencies": ["com.example:helper:2.0"]})
        self.assertEqual(text(folder / "1.0" / "layout-1.0.pom.sha256"), sha(pom.read_bytes()))
        metadata = folder / "maven-metadata.xml"
        self.assertEqual(metadata_fields(metadata), {"latest": "1.0", "release": "1.0", "versions": ["1.0"]})
        self.assertEqual(text(folder / "maven-metadata.xml.sha1"), sha1(metadata.read_bytes()))
        self.assertEqual(sorted(result.data.written), ["com/example/layout/1.0/layout-1.0.jar",
                                                       "com/example/layout/1.0/layout-1.0.pom"])
        receipts = self.receipts()
        receipt = receipts["com/example/layout/1.0/layout-1.0.jar"]
        self.assertEqual({k: receipt[k] for k in ("coordinates", "sha256", "sha1", "bytes", "kind", "reason", "job")},
                         {"coordinates": "com.example:layout:1.0", "sha256": sha(data), "sha1": sha1(data),
                          "bytes": len(data), "kind": "rescued", "reason": REASON, "job": result.job})
        self.assertEqual(Path(receipt["source"]), self.d.data / "com.example_layout_1.0")
        time.strptime(receipt["added"], "%Y-%m-%dT%H:%M:%SZ")  # UTC, to the second
        self.assertEqual((receipts["com/example/layout/1.0/layout-1.0.pom"]["kind"],
                          receipts["com/example/layout/1.0/layout-1.0.pom"]["dependencies"]),
                         ("pom", ["com.example:helper:2.0"]))
        again = self.add("com.example:layout:1.0", data, dependencies=["com.example:helper:2.0"])
        self.assertEqual((again.outcome, again.data.state, again.data.written), ("ok", "held", []), again.summary)
        self.assertEqual(self.receipts(), receipts)  # the same bytes again change nothing

    def test_versions_follow_gradles_order(self):
        for version in ("1.10", "1.9", "2.0-SNAPSHOT", "1.10-rc1"):
            result = self.add(f"com.example:ordered:{version}", library_jar(f"ordered-{version}"))
            self.assertEqual(result.outcome, "ok", result.summary)
        self.assertEqual(metadata_fields(self.store / "com" / "example" / "ordered" / "maven-metadata.xml"),
                         {"latest": "2.0-SNAPSHOT", "release": "1.10", "versions": ["1.9", "1.10-rc1", "1.10",
                                                                                    "2.0-SNAPSHOT"]})
        documented = ["1.a", "1.0-dev", "1.0-alpha", "1.0-zeta", "1.0-rc", "1.0-SNAPSHOT", "1.0-final", "1.0-ga",
                      "1.0-release", "1.0-sp", "1.0", "1.0.0", "1.1", "1.9", "1.10"]
        self.assertEqual(maven.ordered(reversed(documented)), documented)

    def test_other_bytes_are_refused_and_rescued_bytes_never_replaced(self):
        data = library_jar("refused")
        wrong = self.add("com.example:refused:1.0", data, sha256="0" * 64)
        self.assertEqual(wrong.outcome, "finding", wrong.summary)
        self.assertIn("not the expected", wrong.summary)
        self.assertFalse((self.store / "com" / "example" / "refused").exists())
        self.assertEqual(self.add("com.example:refused:1.0", data).outcome, "ok")
        other = self.add("com.example:refused:1.0", library_jar("refused, rebuilt"), name="refused-rebuilt")
        self.assertEqual((other.outcome, other.data.written), ("finding", []), other.summary)
        self.assertIn("never replaced", other.summary)
        self.assertEqual((self.store / "com/example/refused/1.0/refused-1.0.jar").read_bytes(), data)
        self.assertEqual(self.receipts()["com/example/refused/1.0/refused-1.0.jar"]["sha256"], sha(data))

    def test_a_url_is_fetched_and_named_in_the_receipt(self):
        served = scratch(self, "wb-served-")
        data = library_jar("fetched")
        (served / "fetched-1.0.jar").write_bytes(data)
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(served)))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_address[1]}"
        fetched = self.add("com.example:fetched:1.0", url=f"{base}/fetched-1.0.jar", sha256=sha(data))
        self.assertEqual(fetched.outcome, "ok", fetched.summary)
        self.assertEqual((self.store / "com/example/fetched/1.0/fetched-1.0.jar").read_bytes(), data)
        self.assertEqual(self.receipts()["com/example/fetched/1.0/fetched-1.0.jar"]["source"], f"{base}/fetched-1.0.jar")
        gone = self.add("com.example:gone:1.0", url=f"{base}/gone-1.0.jar", sha256=sha(data))
        self.assertEqual(gone.outcome, "environment", gone.summary)
        self.assertIn("404", gone.summary)

    def test_an_original_pom_takes_the_place_of_the_one_workbench_wrote(self):
        self.assertEqual(self.add("com.example:withpom:1.0", library_jar("withpom")).outcome, "ok")
        pom = self.store / "com/example/withpom/1.0/withpom-1.0.pom"
        written = sha(pom.read_bytes())
        original = self.add("com.example:withpom:1.0@pom", ORIGINAL_POM)
        self.assertEqual((original.outcome, original.data.state), ("ok", "replace"), original.summary)
        self.assertEqual(pom.read_bytes(), ORIGINAL_POM)
        receipt = self.receipts()["com/example/withpom/1.0/withpom-1.0.pom"]
        self.assertEqual((receipt["kind"], receipt["replaces"]), ("rescued", written))
        other = self.add("com.example:withpom:1.0@pom", ORIGINAL_POM.replace(b"</project>", b"<!-- -->\n</project>"),
                         name="other.pom")
        self.assertEqual(other.outcome, "finding", other.summary)  # a rescued POM is never replaced either
        stranger = self.add("com.example:another:1.0@pom", ORIGINAL_POM, name="another.pom")
        self.assertEqual(stranger.outcome, "finding", stranger.summary)
        self.assertIn("declares com.example:withpom:1.0", stranger.summary)
        sources = self.add("com.example:withpom:1.0:sources", library_jar("withpom-sources"))
        self.assertEqual(sources.outcome, "ok", sources.summary)
        self.assertTrue((self.store / "com/example/withpom/1.0/withpom-1.0-sources.jar").is_file())
        self.assertEqual(pom.read_bytes(), ORIGINAL_POM)  # the original stays the version's POM
        deps = self.add("com.example:withpom:1.0:javadoc", library_jar("withpom-javadoc"),
                        dependencies=["com.example:helper:2.0"])
        self.assertEqual(deps.outcome, "finding", deps.summary)
        self.assertIn("original POM", deps.summary)
        self.assertFalse((self.store / "com/example/withpom/1.0/withpom-1.0-javadoc.jar").exists())

    def test_a_plugin_gets_a_marker_for_each_id(self):
        result = self.add("com.example:tool:2.0", plugin_jar("com.example.tool"), plugins=["com.example.tool"])
        self.assertEqual(result.outcome, "ok", result.summary)
        self.assertIn("the marker of com.example.tool", result.summary)
        marker = self.store / "com/example/tool/com.example.tool.gradle.plugin/2.0/com.example.tool.gradle.plugin-2.0.pom"
        self.assertEqual(pom_fields(marker), {"coordinates": "com.example.tool:com.example.tool.gradle.plugin:2.0",
                                              "packaging": "pom", "dependencies": ["com.example:tool:2.0"]})
        self.assertEqual(metadata_fields(marker.parents[1] / "maven-metadata.xml")["versions"], ["2.0"])
        receipt = self.receipts()[marker.relative_to(self.store).as_posix()]
        self.assertEqual((receipt["kind"], receipt["plugin"]), ("marker", "com.example.tool"))

    def test_coordinates_and_sources_are_checked(self):
        data = library_jar("checked")
        for bad in ("com.example:checked", "com..example:checked:1.0", "com/example:checked:1.0",
                    "com.example:checked:1..0", "com.example:checked:1.0:sources@pom", "com.example:checked:1.0@"):
            result = self.add(bad, data, name="checked")
            self.assertEqual(result.outcome, "usage", f"{bad}: {result.summary}")
        _, digest = self.file("checked", data)
        neither = self.add("com.example:checked:1.0", sha256=digest)
        self.assertEqual(neither.outcome, "usage", neither.summary)
        both = self.add("com.example:checked:1.0", data, name="checked", url="https://example.invalid/checked.jar")
        self.assertEqual(both.outcome, "usage", both.summary)
        for params in ({"reason": "  "}, {"dependencies": ["com.example:helper"]}, {"plugins": ["not an id"]}):
            result = self.add("com.example:checked:1.0", data, name="checked", **params)
            self.assertEqual(result.outcome, "usage", f"{params}: {result.summary}")
        zipped = self.add("com.example:checked:1.0@zip", data, name="checked", plugins=["com.example.checked"])
        self.assertEqual(zipped.outcome, "usage", zipped.summary)
        self.assertFalse((self.store / "com" / "example" / "checked").exists())

    def test_the_listing_names_digests_receipts_and_substitutions(self):
        data = library_jar("listed")
        self.assertEqual(self.add("com.example:listed:1.0", data).outcome, "ok")
        listed = self.c.submit("toolchain.repo-list", {"match": "com.example:listed:*"}).result(timeout=60)
        self.assertEqual(listed.outcome, "ok", listed.summary)
        [entry] = listed.data.coordinates
        self.assertEqual(entry["coordinates"], "com.example:listed:1.0")
        files = {f["path"]: f for f in entry["files"]}
        jar = files["com/example/listed/1.0/listed-1.0.jar"]
        self.assertEqual((jar["sha256"], jar["kind"], jar["reason"]), (sha(data), "rescued", REASON))
        self.assertEqual(files["com/example/listed/1.0/listed-1.0.pom"]["kind"], "pom")
        swaps = {s["from"]: s for s in listed.data.substitutions}
        self.assertEqual(swaps["com.example:vanished-lib:1.0"]["to"], "com.example:retained-lib:1.0-r1")
        self.assertEqual(swaps["com.example.renamed:com.example.renamed.gradle.plugin:3.0"]["plugin"],
                         "com.example.renamed")

    def test_init_writes_the_script_into_a_gradle_home(self):
        self.assertEqual(self.add("com.example:served:1.0", library_jar("served")).outcome, "ok")
        home = self.d.out / "local-gradle-home"
        result = self.c.submit("toolchain.repo-init", {"gradle_home": str(home)}).result(timeout=60)
        self.assertEqual(result.outcome, "ok", result.summary)
        script = text(home / "init.d" / "workbench-repository.gradle")
        self.assertEqual((script, result.data.sha256), (result.data.script, sha(script.encode("utf-8"))))
        self.assertIn(f"new File('{self.store.as_posix()}')", script)  # as the host names it
        self.assertIn("    ['com.example', 'served', '1.0']", script)
        self.assertIn("def wbModules = [\n    ['com.example:vanished-lib:1.0', 'com.example:retained-lib:1.0-r1']\n]",
                      script)
        self.assertIn("def wbPlugins = [\n    ['com.example.renamed', '3.0', 'module', 'com.example:rescued-plugin:1.0']"
                      "\n]", script)
        inside = self.c.submit("toolchain.repo-init", {"machine": "container"}).result(timeout=60)
        self.assertEqual(inside.outcome, "ok", inside.summary)
        self.assertIsNone(inside.data.written)

    def test_verify_names_damage_strangers_and_missing_digests(self):
        data = library_jar("verified")
        self.assertEqual(self.add("com.example:verified:1.0", data).outcome, "ok")
        clean = self.c.submit("toolchain.repo-verify", {}).result(timeout=120)
        self.assertEqual(clean.outcome, "ok", clean.summary)
        jar = self.store / "com/example/verified/1.0/verified-1.0.jar"
        stranger = self.store / "com/example/stray/1.0/stray-1.0.jar"
        stranger.parent.mkdir(parents=True, exist_ok=True)
        stranger.write_bytes(b"put here by hand")
        jar.write_bytes(b"damaged")
        (self.store / "com/example/verified/1.0/verified-1.0.pom.sha1").unlink()
        try:
            damaged = self.c.submit("toolchain.repo-verify", {}).result(timeout=120)
            self.assertEqual(damaged.outcome, "finding", damaged.summary)
            problems = {p["path"]: p["problem"] for p in damaged.data.problems}
            self.assertIn("not its receipt's", problems["com/example/verified/1.0/verified-1.0.jar"])
            self.assertIn("no receipt", problems["com/example/stray/1.0/stray-1.0.jar"])
            self.assertIn("does not say", problems["com/example/verified/1.0/verified-1.0.pom.sha1"])
            self.assertEqual(len(problems), 3, problems)
            restored = self.add("com.example:verified:1.0", data)  # the receipt's bytes again: restored
            self.assertEqual((restored.outcome, restored.data.state), ("ok", "restore"), restored.summary)
            self.assertEqual(jar.read_bytes(), data)
        finally:
            shutil.rmtree(stranger.parents[1], ignore_errors=True)
        again = self.c.submit("toolchain.repo-verify", {}).result(timeout=120)
        self.assertEqual(again.outcome, "ok", again.summary)

    def test_a_gradle_build_gets_the_held_coordinates_and_the_substitutions(self):
        found = find_gradle(self.d.root / "gradle-probes")
        if not found:
            self.skipTest("no Gradle distribution on this machine (none is downloaded), or no JDK it starts on")
        launcher, jdk = found
        for coordinates, data, params in (
                ("com.example:rescued-plugin:1.0", plugin_jar("com.example.rescued", "com.example.renamed"),
                 {"plugins": ["com.example.rescued"]}),
                ("com.example:retained-lib:1.0-r1", library_jar("retained"), {"dependencies": ["com.example:helper:2.0"]}),
                ("com.example:helper:2.0", library_jar("helper"), {})):
            result = self.add(coordinates, data, **params)
            self.assertEqual(result.outcome, "ok", result.summary)
        stray = self.store / "com/example/unheld/1.0/unheld-1.0.jar"  # on disk, but without a receipt
        stray.parent.mkdir(parents=True, exist_ok=True)
        stray.write_bytes(library_jar("unheld"))
        stray.with_name("unheld-1.0.pom").write_bytes(maven.pom(maven.parse("com.example:unheld:1.0@pom"), "jar", [], "x"))
        try:
            home = self.d.out / "probe-gradle-home"
            init = self.c.submit("toolchain.repo-init", {"gradle_home": str(home)}).result(timeout=60)
            self.assertEqual(init.outcome, "ok", init.summary)
            build = self.d.out / "probe-build"
            (build / "app").mkdir(parents=True, exist_ok=True)
            for name, content in (("settings.gradle", PROBE_SETTINGS), ("build.gradle", PROBE_ROOT),
                                  ("app/build.gradle", PROBE_APP)):
                (build / name).write_text(content, encoding="utf-8")
            done = subprocess.run([str(launcher), "-p", str(build), "-g", str(home), "--offline", "--no-daemon",
                                   "--console=plain", "--warning-mode=all", "probe"], capture_output=True, text=True,
                                  timeout=900, env={**os.environ, "JAVA_HOME": str(jdk)})
        finally:
            shutil.rmtree(stray.parents[1], ignore_errors=True)
        said = done.stdout + done.stderr
        self.assertEqual(done.returncode, 0, said[-4000:])
        lines = dict(line.split(" ", 1) for line in done.stdout.splitlines()
                     if line.split(" ", 1)[0] in ("ROOT", "APP", "UNHELD", "CLASSPATH", "REPOSITORIES", "PLUGINS"))
        # A project that resolves from the settings' repositories gets it there, one with its own among them, first.
        self.assertEqual(lines["ROOT"], "helper-2.0.jar,retained-lib-1.0-r1.jar")
        self.assertEqual(lines["APP"], "helper-2.0.jar,retained-lib-1.0-r1.jar")
        self.assertEqual(lines["REPOSITORIES"], f"{gradle.NAME},MavenRepo")
        self.assertEqual(lines["UNHELD"], "0")  # asked only for what it holds
        self.assertEqual(lines["CLASSPATH"], "helper-2.0.jar,rescued-plugin-1.0.jar,retained-lib-1.0-r1.jar")
        self.assertEqual(lines["PLUGINS"], "true,true")  # by its marker, and a vanished request by substitution
        self.assertNotIn("workbench-repository.gradle", said)  # no warning or error names the script

    def test_where_the_repository_is_and_who_can_read_it(self):
        root = scratch(self, "wb-repository-folder-")
        roots = [{"name": "out", "host": str(root / "out"), "container": str(root / "out"), "mode": "rw"},
                 {"name": "data", "host": str(root / "data"), "container": str(root / "data"), "mode": "ro"}]
        saved = os.environ.get("WB_STATE_DIR")
        os.environ["WB_STATE_DIR"] = str(root / "state")
        try:
            default = direct_context(root, {}, roots)
            store = root / "state" / "toolchain" / "repository"
            self.assertEqual(repository.folder(default), store)
            configured = direct_context(root, {"repository": (root / "out" / "repo").as_posix()}, roots)
            self.assertEqual(repository.folder(configured, write=True), root / "out" / "repo")
            shared = direct_context(root, {"repository": str(root / "data" / "repo")}, roots)
            self.assertEqual(repository.folder(shared), root / "data" / "repo")  # read-only serves builds...
            with self.assertRaises(EnvironmentProblem):
                repository.folder(shared, write=True)  # ...but takes nothing new
            with self.assertRaises(EnvironmentProblem):
                repository.folder(direct_context(root, {"repository": "relative/repo"}, roots))
            receipt = {"coordinates": "com.example:lib:1.0", "sha256": "0" * 64, "sha1": "0" * 40, "bytes": 1,
                       "kind": "rescued", "source": "s", "reason": "r", "added": "2026-01-01T00:00:00Z", "job": "j"}
            store.mkdir(parents=True)
            (store / "receipts.json").write_text(json.dumps({"schema": repository.SCHEMA, "files": {
                "com/example/lib/1.0/lib-1.0.jar": receipt}}), encoding="utf-8")
            hidden = repository.init(default, "host")  # the state folder is under no root: the host cannot read it
            self.assertEqual((default.outcome, hidden["script"]), ("finding", ""))
            self.assertIn("under no configured root", default.findings[0])
            inside = repository.init(direct_context(root, {}, roots), "container")
            self.assertIn(f"new File('{store.as_posix()}')", inside["script"])
            empty = direct_context(root, {"repository": (root / "out" / "empty").as_posix()}, roots)
            home = root / "out" / "home"
            (home / "init.d").mkdir(parents=True)
            (home / "init.d" / repository.SCRIPT).write_text(gradle.HEADER + ": stale\n", encoding="utf-8")
            nothing = repository.init(empty, "host", gradle_home=str(home))
            self.assertEqual((empty.outcome, nothing["script"]), ("ok", ""))
            self.assertFalse((home / "init.d" / repository.SCRIPT).exists())  # nothing to serve: no stale script
        finally:
            os.environ.pop("WB_STATE_DIR", None)
            if saved is not None:
                os.environ["WB_STATE_DIR"] = saved

    def test_substitutions_are_checked(self):
        root = scratch(self, "wb-substitutions-")

        def check(table):
            return repository.substitutions(direct_context(root, {"substitutions": table}))
        moved = check({"com.example.tool:com.example.tool.gradle.plugin:1.0":
                       "com.example.tool:com.example.tool.gradle.plugin:1.0.1", "com.example:a:1": "com.example:b:2"})
        self.assertEqual(gradle.rules(moved), ([("com.example.tool", "1.0", "version", "1.0.1")],
                                               [("com.example:a:1", "com.example:b:2")]))
        for bad in ({"com.example:lib": "com.example:lib:2"}, {"com.example:lib:1@zip": "com.example:lib:2"},
                    {"com.example:lib:1": "com.example:lib:1"},
                    {"com.example:a:1": "com.example:b:1", "com.example:b:1": "com.example:c:1"},
                    {"com.example.p:com.example.p.gradle.plugin:1": "com.example.q:com.example.q.gradle.plugin:1"}):
            with self.assertRaises(EnvironmentProblem, msg=str(bad)):
                check(bad)

    def test_the_script_text(self):
        script = gradle.init_script("X:\\rescue\\o'neil\\repo", [("com.example", "lib", "1.0")], [])  # scrub: allow
        self.assertTrue(script.startswith(gradle.HEADER))
        self.assertIn("new File('X:/rescue/o\\'neil/repo')", script)  # scrub: allow
        self.assertIn("def wbHeld = [\n    ['com.example', 'lib', '1.0']\n]", script)
        self.assertIn("def wbModules = []", script)
        self.assertIn("content.includeVersion(held[0], held[1], held[2])", script)
        accented = gradle.init_script("/srv/zo\u00eb/repo", [("g", "a", "1")], [])
        self.assertTrue(accented.isascii())
        self.assertIn("new File('/srv/zo\\u00eb/repo')", accented)
        self.assertEqual(gradle.init_script("/repo", [], []), "")  # nothing to serve


class GradleHome(unittest.TestCase):
    """toolchain.gradle-home: a Gradle home that lists the machine's JDKs, other lines kept."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.home = Path(self.folder.name) / "gradle-home"

    def tearDown(self):
        self.folder.cleanup()

    def ctx(self, jdks):
        out = Path(self.folder.name) / "out"
        out.mkdir(exist_ok=True)
        notes = {}
        return SimpleNamespace(_jdks=jdks, out=out, output=lambda *a, **k: None,
                               summary=lambda text: notes.setdefault("summary", text)), notes

    def test_the_block_is_written_once_and_replaced_in_place(self):
        from wb_toolchain import commands, gradle_home
        self.home.mkdir()
        own = "org.gradle.jvmargs=-Xmx4g\n"
        (self.home / "gradle.properties").write_text(own, encoding="utf-8")
        ctx, notes = self.ctx({"8": "/jdk8", "26": "/jdk26", "name": "ignored"})
        dry = commands.gradle_home(ctx, gradle_home=str(self.home))
        self.assertFalse(dry["written"])
        self.assertIn("dry run", notes["summary"])
        self.assertEqual((self.home / "gradle.properties").read_text(encoding="utf-8"), own)
        done = commands.gradle_home(ctx, gradle_home=str(self.home), write=True)
        self.assertTrue(done["written"])
        text = (self.home / "gradle.properties").read_text(encoding="utf-8")
        self.assertTrue(text.startswith(own))  # the user's own line stays
        self.assertIn("org.gradle.java.installations.paths=/jdk8,/jdk26", text)  # majors in order
        self.assertIn("auto-detect=false", text)
        ctx, _ = self.ctx({"26": "/jdk26", "27": "/jdk27"})
        commands.gradle_home(ctx, gradle_home=str(self.home), write=True)
        again = (self.home / "gradle.properties").read_text(encoding="utf-8")
        self.assertEqual(again.count(gradle_home.BEGIN), 1)  # replaced, not appended
        self.assertIn("paths=/jdk26,/jdk27", again)
        if os.name == "nt":  # a backslash escapes in a properties file: Windows paths are written with slashes
            drive = "X" + ":"  # synthetic: built in parts, as the other path tests do
            self.assertIn(f"paths={drive}/jdk26", gradle_home.block([drive + "\\jdk26"], False, False))
        self.assertNotIn("jdk8", again)
        self.assertFalse(commands.gradle_home(ctx, gradle_home=str(self.home), write=True)["written"])  # no change

    def test_a_machine_without_jdks_is_refused(self):
        from wb_core.module import EnvironmentProblem
        from wb_toolchain import commands
        ctx, _ = self.ctx({})
        with self.assertRaises(EnvironmentProblem):
            commands.gradle_home(ctx, gradle_home=str(self.home))


if __name__ == "__main__":
    unittest.main()
