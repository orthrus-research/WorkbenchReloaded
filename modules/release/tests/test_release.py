"""release: the publication allow-list, the plan a publish is quoted as, its approval and re-quote, and uploads to
fake GitHub, Modrinth, CurseForge, Maven, Central Portal and S3 services checked request by request. Every channel
points at a fake service on 127.0.0.1 and every token is a made-up one: nothing here reaches a real publishing
service. The central tests sign with a throwaway key made in a temporary gpg home; without a usable gpg on PATH
they are skipped."""

import calendar
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO / "tests"))
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import fake_channels as fakes  # noqa: E402
from harness import Daemon  # noqa: E402

from wb import ApprovalRequired  # noqa: E402
from wb_core.module import EnvironmentProblem, ParamError  # noqa: E402
from wb_release import central, channels, commands, mirror, transport  # noqa: E402
from wb_release import rules as rulebook  # noqa: E402

TOKENS = {"gh": "fake-github-token", "mr": "fake-modrinth-token", "cf": "fake-curseforge-token",
          "cf_core": "fake-core-api-key", "maven": "deployer:fake-maven-password",
          "central": "portal-user:fake-portal-password", "r2_key_id": "fakeaccesskeyid0000",
          "r2_secret": "fake-secret-access-key", "r2_wrong": "not-the-secret-access-key",
          "signing_pass": "fake-signing-passphrase"}
D = C = GH = MR = CF = MV = CE = BK = PB = NOTES = None
SERVERS: list = []
GPG_HOME: Path | None = None
KEY: str | None = None        # the throwaway signing key's fingerprint, when gpg can make one
NO_KEY = "no gpg on PATH"
LICENCE = {"name": "Example Licence 1.0", "url": "https://licence.example.invalid/1.0"}


def digest(path: Path, algo: str = "sha256") -> str:
    return hashlib.new(algo, Path(path).read_bytes()).hexdigest()


# ---------------------------------------------------------------------------------------------
# files to publish

def class_file(major: int) -> bytes:
    return b"\xca\xfe\xba\xbe" + (0).to_bytes(2, "big") + major.to_bytes(2, "big") + b"\x00\x01"


def jar_bytes(classes: dict | None = None, manifest: dict | None = None, entries: dict | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as jar:
        lines = ["Manifest-Version: 1.0", *(f"{k}: {v}" for k, v in (manifest or {}).items())]
        jar.writestr("META-INF/MANIFEST.MF", "\r\n".join(lines) + "\r\n\r\n")
        for name, major in (classes or {}).items():
            jar.writestr(name, class_file(major))
        for name, data in (entries or {}).items():
            jar.writestr(name, data)
    return buffer.getvalue()


def write_jar(path: Path, **parts) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(jar_bytes(**parts))
    return path


def mod_jar(folder: Path, version: str, *, major: int = 52, implementation: str | None = None,
            extra: dict | None = None) -> Path:
    """A production JAR as the test rules want it: Java 8 classes, its title and version in the manifest."""
    return write_jar(folder / f"examplemod-{version}.jar",
                     classes={"org/example/examplemod/ExampleMod.class": major,
                              "org/example/examplemod/Config.class": major},
                     manifest={"Implementation-Title": "examplemod",
                               "Implementation-Version": implementation or version},
                     entries={"mcmod.info": json.dumps([{"modid": "examplemod", "version": version}]),
                              **(extra or {})})


def sources_jar(folder: Path, version: str) -> Path:
    return write_jar(folder / f"examplemod-{version}-sources.jar",
                     entries={"org/example/examplemod/ExampleMod.java": "class ExampleMod {}"})


def javadoc_jar(folder: Path, version: str) -> Path:
    return write_jar(folder / f"examplemod-{version}-javadoc.jar", entries={"index.html": "<html></html>"})


def write_pom(path: Path, version: str, *, artifact: str = "examplemod", dependencies=(),
              group: str = "org.example") -> Path:
    deps = "".join(f"<dependency><groupId>{g}</groupId><artifactId>{a}</artifactId><version>{v}</version>"
                   f"<scope>{s}</scope></dependency>" for g, a, v, s in dependencies)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('<?xml version="1.0" encoding="UTF-8"?>\n<project xmlns="http://maven.apache.org/POM/4.0.0">'
                    f"<modelVersion>4.0.0</modelVersion><groupId>{group}</groupId><artifactId>{artifact}"
                    f"</artifactId><version>{version}</version><dependencies>{deps}</dependencies></project>\n",
                    encoding="utf-8")
    return path


def write_module(path: Path, version: str, jar: tuple, sources: tuple | None = None, *, jvm: int = 8,
                 dependencies=()) -> Path:
    """A Gradle module file; ``jar`` and ``sources`` are (name, size, sha256) as it lists them."""
    def entry(name, size, sha256):
        return {"name": name, "url": name, "size": size, "sha256": sha256}

    deps = [{"group": g, "module": m, "version": {"requires": v}} for g, m, v in dependencies]
    variants = [{"name": name, "attributes": {"org.gradle.category": "library", "org.gradle.jvm.version": jvm},
                 "dependencies": deps, "files": [entry(*jar)]} for name in ("apiElements", "runtimeElements")]
    if sources:
        variants.append({"name": "sourcesElements", "attributes": {"org.gradle.category": "documentation"},
                         "files": [entry(*sources)]})
    path.write_text(json.dumps({"formatVersion": "1.1", "variants": variants,
                                "component": {"group": "org.example", "module": "examplemod", "version": version}}),
                    encoding="utf-8")
    return path


def listed(path: Path) -> tuple:
    return path.name, path.stat().st_size, digest(path)


# ---------------------------------------------------------------------------------------------
# one Workbench and the four fake services for the whole suite

def config(secrets: Path, store: Path) -> str:
    return f"""
        [secrets.release_gh]
        host = '{secrets / "gh"}'
        [secrets.release_mr]
        host = '{secrets / "mr"}'
        [secrets.release_cf]
        host = '{secrets / "cf"}'
        [secrets.release_cf_core]
        host = '{secrets / "cf_core"}'
        [secrets.release_maven]
        host = '{secrets / "maven"}'
        [secrets.release_central]
        host = '{secrets / "central"}'
        [secrets.release_signing_pass]
        host = '{secrets / "signing_pass"}'
        [secrets.release_signing_key]
        host = '{secrets / "signing_key"}'
        [secrets.release_r2_key_id]
        host = '{secrets / "r2_key_id"}'
        [secrets.release_r2_secret]
        host = '{secrets / "r2_secret"}'
        [secrets.release_r2_wrong]
        host = '{secrets / "r2_wrong"}'

        [modules.release]
        user_agent = "wb-release-tests"
        timeout = "30s"
        store = '{store}'

        [modules.release.rules.mod]
        java = 8
        production = ["examplemod-{{version}}.jar"]
        dependencies = ["org.example:*"]
        manifest = {{ Implementation-Title = "examplemod" }}

        [modules.release.channels.gh]
        kind = "github"
        base_url = "{GH.url}"
        upload_url = "{GH.url}/uploads"
        project = "example/examplemod"
        secret = "release_gh"
        rules = "mod"
        [modules.release.channels.gh.defaults]
        tag = "v{{version}}"

        [modules.release.channels.mr]
        kind = "modrinth"
        base_url = "{MR.url}"
        project = "AbCdEf12"
        secret = "release_mr"
        rules = "mod"
        page_url = "{MR.url}/mod/examplemod/version/{{id}}"
        [modules.release.channels.mr.defaults]
        release_type = "beta"
        game_versions = ["1.12.2"]
        loaders = ["forge"]

        [modules.release.channels.cf]
        kind = "curseforge"
        base_url = "{CF.url}"
        project = "123456"
        secret = "release_cf"
        status_url = "{CF.url}"
        status_secret = "release_cf_core"
        rules = "mod"
        page_url = "{CF.url}/projects/examplemod/files/{{id}}"
        [modules.release.channels.cf.defaults]
        game_versions = ["1.12.2", "Java 8"]
        loaders = ["Forge"]
        dependencies = ["required:examplelib"]

        [modules.release.channels.maven]
        kind = "maven"
        base_url = "{MV.url}/releases"
        project = "org.example:examplemod"
        secret = "release_maven"
        rules = "mod"

        [modules.release.rules.lib]
        java = 8
        production = ["examplemod-{{version}}.jar"]
        dependencies = ["org.example:*"]
        licence = {{ name = "{LICENCE['name']}", url = "{LICENCE['url']}" }}

        [modules.release.channels.central]
        kind = "central"
        base_url = "{CE.url}"
        project = "cc.orthrus:examplemod"
        secret = "release_central"
        rules = "lib"
        signing = {{ key = "{KEY or '0' * 40}", secret = "release_signing_pass", keyring = "release_signing_key" }}
        poll_interval = "20ms"
        {POM}

        [modules.release.channels.central_auto]
        kind = "central"
        base_url = "{CE.url}"
        project = "cc.orthrus:examplemod"
        secret = "release_central"
        rules = "lib"
        publishing_type = "AUTOMATIC"
        javadoc = "placeholder"
        checksums = ["md5", "sha1"]
        signing = {{ key = "{KEY or '0' * 40}", secret = "release_signing_pass", keyring = "release_signing_key" }}
        poll_interval = "20ms"
        {POM}

        [modules.release.channels.central_unlicensed]
        kind = "central"
        base_url = "{CE.url}"
        project = "cc.orthrus:examplemod"
        secret = "release_central"
        rules = "mod"
        javadoc = "placeholder"
        signing = {{ key = "{KEY or '0' * 40}", secret = "release_signing_pass", keyring = "release_signing_key" }}
        {POM}

        [modules.release.channels.mirror]
        kind = "mirror"
        base_url = "{BK.url}"
        bucket = "orthrus-maven"
        public_url = "{PB.url}"
        group = "cc.orthrus"
        project = "cc.orthrus:examplemod"
        key_id_secret = "release_r2_key_id"
        secret = "release_r2_secret"
        rules = "lib"

        [modules.release.channels.mirror_other_group]
        kind = "mirror"
        base_url = "{BK.url}"
        bucket = "orthrus-maven"
        public_url = "{PB.url}"
        group = "cc.orthrus"
        project = "org.example:examplemod"
        key_id_secret = "release_r2_key_id"
        secret = "release_r2_secret"

        [modules.release.channels.mirror_wrong_key]
        kind = "mirror"
        base_url = "{BK.url}"
        bucket = "orthrus-maven"
        public_url = "{PB.url}"
        group = "cc.orthrus"
        project = "cc.orthrus:examplemod"
        key_id_secret = "release_r2_key_id"
        secret = "release_r2_wrong"
        rules = "lib"

        [modules.release.channels.insecure]
        kind = "github"
        base_url = "http://example.invalid"
        project = "example/examplemod"
        secret = "release_gh"

        [modules.release.channels.unmounted]
        kind = "github"
        base_url = "{GH.url}"
        project = "example/examplemod"
        secret = "release_nowhere"
    """


POM = ('pom = { name = "Example Mod", description = "An example mod.", url = "https://examplemod.example.invalid", '
       'developers = [{ id = "example", name = "Example Developer", url = "https://example.invalid/dev" }], '
       'scm = { url = "https://git.example.invalid/examplemod", '
       'connection = "scm:git:https://git.example.invalid/examplemod.git", '
       'developer_connection = "scm:git:ssh://git.example.invalid/examplemod.git" } }')


def gpg(*args: str, input: str | None = None) -> subprocess.CompletedProcess:
    """gpg in the tests' own throwaway home, never the user's."""
    return subprocess.run([shutil.which("gpg"), "--homedir", GPG_HOME.as_posix(), "--batch", "--no-tty", *args],
                          input=input, capture_output=True, text=True, timeout=120)


def make_key(home: Path) -> tuple[str | None, str]:
    """A throwaway ed25519 signing key in ``home``, and its armored secret key; (None, why) without a usable gpg."""
    global GPG_HOME
    if not shutil.which("gpg"):
        return None, "no gpg on PATH"
    GPG_HOME = home
    home.mkdir(mode=0o700)
    (home / "private-keys-v1.d").mkdir(mode=0o700)
    try:
        gpg("--pinentry-mode", "loopback", "--passphrase", TOKENS["signing_pass"], "--quick-gen-key",
            "Release Tests <release-tests@example.invalid>", "ed25519", "sign", "never")
        listed = gpg("--with-colons", "--list-secret-keys").stdout
        fingerprint = next(line.split(":")[9] for line in listed.splitlines() if line.startswith("fpr:"))
        exported = gpg("--pinentry-mode", "loopback", "--passphrase-fd", "0", "--armor", "--export-secret-keys",
                       fingerprint, input=TOKENS["signing_pass"] + "\n").stdout
    except (OSError, subprocess.SubprocessError, StopIteration):
        return None, "the gpg on PATH cannot make a key in a temporary home"
    if "PRIVATE KEY BLOCK" not in exported:
        return None, "the gpg on PATH cannot export a key from a temporary home"
    return fingerprint, exported


def setUpModule():
    global D, C, GH, MR, CF, MV, CE, BK, PB, NOTES, KEY, NO_KEY
    GH, MR = fakes.GitHub(TOKENS["gh"]), fakes.Modrinth(TOKENS["mr"])
    CF, MV = fakes.CurseForge(TOKENS["cf"], TOKENS["cf_core"]), fakes.Maven("deployer", "fake-maven-password")
    CE = fakes.Central(*TOKENS["central"].split(":"))
    BK = fakes.Bucket(TOKENS["r2_key_id"], TOKENS["r2_secret"], "orthrus-maven")
    PB = fakes.Public(BK)
    SERVERS.extend(fakes.serve(service) for service in (GH, MR, CF, MV, CE, BK, PB))
    root = Path(tempfile.mkdtemp(prefix="wb-release-"))
    (root / "secrets").mkdir()
    KEY, armored = make_key(root / "gnupg")
    if KEY is None:
        NO_KEY, armored = armored, "no key"
    for name, value in {**TOKENS, "signing_key": armored}.items():
        (root / "secrets" / name).write_text(value, encoding="ascii")
    D = Daemon(config(root / "secrets", root / "data" / "set-out" / "store"), root=root).start()
    C = D.client
    NOTES = D.data / "notes.md"
    NOTES.write_bytes("# 1.x\n\n- Fixed the recipe loop.\n- Lighter on memory.\n".encode("utf-8"))


def tearDownModule():
    D.stop()
    for server in SERVERS:
        server.shutdown()
        server.server_close()
    if GPG_HOME is not None and shutil.which("gpgconf"):
        subprocess.run([shutil.which("gpgconf"), "--homedir", GPG_HOME.as_posix(), "--kill", "gpg-agent"],
                       capture_output=True, timeout=30, check=False)


def submit(channel: str, version: str, files: list, **params):
    return C.submit("release.publish", {"channel": channel, "version": version, "files": [str(f) for f in files],
                                        **params})


def quoted(test: unittest.TestCase, job) -> ApprovalRequired:
    with test.assertRaises(ApprovalRequired) as raised:
        job.result(timeout=60)
    return raised.exception


def publish(test: unittest.TestCase, channel: str, version: str, files: list, **params):
    """Submit, approve the plan quoted, and wait for the publish; returns (quote, result)."""
    job = submit(channel, version, files, **params)
    quote = quoted(test, job).quote
    job.approve(quote["id"])
    result = job.result(timeout=120)
    test.assertEqual(result.outcome, "ok", result.summary)
    return quote, result


def status(channel: str, version: str, files: list = (), **params):
    return C.submit("release.status", {"channel": channel, "version": version, "files": [str(f) for f in files],
                                       **params}).result(timeout=60)


def receipt_file(result) -> dict:
    output = next(o for o in result["outputs"] if o["name"] == "receipt")
    return json.loads(Path(output["host_path"]).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------------------------

class Units(unittest.TestCase):
    def test_channel_urls_keep_a_token_off_plain_http(self):
        self.assertEqual(transport.checked_url("https://api.example.com/", "u"), "https://api.example.com")
        self.assertEqual(transport.checked_url("http://127.0.0.1:8080", "u"), "http://127.0.0.1:8080")
        self.assertEqual(transport.checked_url("http://[::1]:8080/x", "u"), "http://[::1]:8080/x")
        for bad in ("http://example.invalid", "ftp://example.invalid", "https://user:secret@example.invalid",
                    "https://example.invalid/?page=1", "example.invalid"):
            with self.subTest(bad), self.assertRaises(ValueError):
                transport.checked_url(bad, "u")

    def test_a_form_streams_its_parts_exactly(self):
        folder = Path(tempfile.mkdtemp(prefix="wb-release-form-"))
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        data = os.urandom(300_000) + b"\r\n\r\n--tail\r\n"
        (folder / "x.jar").write_bytes(data)
        form = transport.Form()
        form.field("data", '{"a": 1}')
        form.file("file0", "x.jar", folder / "x.jar", "application/java-archive")
        reader = form.reader()
        chunks = []
        while chunk := reader.read(8192):
            chunks.append(chunk)
        reader.close()
        body = b"".join(chunks)
        self.assertEqual(len(body), form.length)
        parts = fakes.multipart(form.content_type, body)
        self.assertEqual(parts["data"].body, b'{"a": 1}')
        self.assertIsNone(parts["data"].content_type)
        self.assertEqual((parts["file0"].filename, parts["file0"].body), ("x.jar", data))
        with self.assertRaises(ValueError):
            form.file("file1", 'bad"name.jar', folder / "x.jar", "application/java-archive")

    def test_versions_and_classifiers_are_read_from_names(self):
        self.assertTrue(rulebook.carries("examplemod-1.2.3", "1.2.3"))
        self.assertTrue(rulebook.carries("examplemod-1.12.2-1.2.3", "1.2.3"))
        self.assertFalse(rulebook.carries("examplemod-1.2.3", "1.2"))
        self.assertFalse(rulebook.carries("examplemod-11.2", "1.2"))
        self.assertEqual(rulebook.classifiers("examplemod-1.0-dev", "1.0"), ["dev"])
        self.assertEqual(rulebook.classifiers("examplemod-1.0", "1.0"), [])
        self.assertEqual(rulebook.classifiers("examplemod-1.0-dev-shadow", None), ["dev", "shadow"])
        self.assertEqual([rulebook.java_level(v) for v in ("1.8", "8", "17", "x")], [8, 8, 17, None])

    def test_maven_metadata_keeps_the_earlier_versions(self):
        root = ElementTree.fromstring(channels.metadata_xml("org.example", "examplemod", ["1.0.0", "1.1.0"], "1.2.0"))
        self.assertEqual([v.text for v in root.findall("versioning/versions/version")], ["1.0.0", "1.1.0", "1.2.0"])
        self.assertEqual((root.findtext("versioning/latest"), root.findtext("versioning/release")), ("1.2.0", "1.2.0"))

    def test_channel_config_and_settings_are_checked(self):
        good = {"kind": "github", "base_url": "https://api.example.com", "project": "example/examplemod",
                "secret": "s"}
        for change in ({"defaults": {"loaders": ["forge"]}}, {"colour": "red"}, {"project": "examplemod"},
                       {"kind": "ftp"}, {"base_url": "http://example.invalid"}):
            with self.subTest(change), self.assertRaises(EnvironmentProblem):
                channels.load({"channels": {"a": {**good, **change}}}, "a")
        with self.assertRaises(ParamError):
            channels.load({"channels": {}}, "a")
        github = channels.load({"channels": {"a": good}}, "a")
        modrinth = channels.load({"channels": {"m": {**good, "kind": "modrinth", "project": "AbCdEf12"}}}, "m")
        self.assertEqual(channels.resolve(github, {}, "1.0")["tag"], "1.0")
        for chan, given in ((github, {"game_versions": ["1.12.2"]}), (github, {"tag": "{nope}"}),
                            (github, {"visibility": "unlisted"}), (modrinth, {"game_versions": ["1.12.2"]}),
                            (modrinth, {"game_versions": ["1.12.2"], "loaders": ["forge"],
                                        "dependencies": ["needed:examplelib"]})):
            with self.subTest(given), self.assertRaises(ParamError):
                channels.resolve(chan, given, "1.0")

    def test_plan_changes_are_named(self):
        before = {"version": "1.0", "visibility": "public", "notes": None,
                  "files": [{"name": "a.jar", "sha256": "0" * 64, "bytes": 1}]}
        after = {"version": "1.0", "visibility": "draft", "notes": {"sha256": "1" * 64},
                 "files": [{"name": "a.jar", "sha256": "1" * 64, "bytes": 2}, {"name": "b.jar", "sha256": "2" * 64}]}
        changes = commands._changes(before, after)
        self.assertTrue(changes[0].startswith("a.jar changed: sha256 000000000000 -> 111111111111, 1 -> 2 bytes"))
        self.assertIn("b.jar was added", changes)
        self.assertIn("notes were added", changes)
        self.assertIn("visibility: 'public' -> 'draft'", changes)
        self.assertEqual(commands._changes(before, json.loads(json.dumps(before))), [])

    def test_versions_must_agree_when_none_is_given(self):
        folder = Path(tempfile.mkdtemp(prefix="wb-release-rules-"))
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        jar = mod_jar(folder, "1.0.0", implementation="1.0.1")
        item = rulebook.Item(jar, jar.name, digest(jar), jar.stat().st_size)
        report = rulebook.inspect([item], rulebook.load({}, None, given=False), None)
        self.assertEqual(len(report.violations), 1, report.violations)
        self.assertIn("versions disagree", report.violations[0])
        self.assertIn("Implementation-Version says 1.0.1", report.violations[0])
        sources = sources_jar(folder, "1.0.0")
        alone = rulebook.inspect([rulebook.Item(sources, sources.name, digest(sources), 1)],
                                 rulebook.load({}, None, given=False), "1.0.0")
        self.assertEqual(alone.violations, ["no production JAR among the files (production: *.jar)"])


class Check(unittest.TestCase):
    def check(self, files: list, **params):
        return C.submit("release.check", {"files": [str(f) for f in files], **params}).result(timeout=60)

    def test_a_clean_publication_passes(self):
        folder = D.data / "check-clean"
        jar, sources = mod_jar(folder, "1.0.0"), sources_jar(folder, "1.0.0")
        pom = write_pom(folder / "examplemod-1.0.0.pom", "1.0.0",
                        dependencies=[("org.example", "examplelib", "2.0", "compile")])
        module = write_module(folder / "examplemod-1.0.0.module", "1.0.0", listed(jar), listed(sources),
                              dependencies=[("org.example", "examplelib", "2.0")])
        result = self.check([jar, sources, pom, module], version="1.0.0", rules="mod")
        self.assertEqual(result.outcome, "ok", result.summary)
        self.assertEqual(result.data.violations, [])
        self.assertEqual(result.data.primary, jar.name)
        roles = {f["name"]: f["role"] for f in result.data.files}
        self.assertEqual(roles, {jar.name: "production", sources.name: "extra", pom.name: "extra",
                                 module.name: "extra"})
        self.assertEqual(result.data.files[0]["java"], 8)
        self.assertIn("Java 8", result.summary)

    def test_each_violation_is_a_finding(self):
        folder = D.data / "check-violations"
        nested = jar_bytes(classes={"org/example/lib/Lib.class": 55})
        jar = write_jar(folder / "examplemod-1.0.0.jar",
                        classes={"org/example/A.class": 52, "org/example/B.class": 61},
                        manifest={"Implementation-Version": "0.9.0"},
                        entries={"META-INF/jarjar/lib.jar": nested,
                                 "META-INF/versions/17/org/example/C.class": class_file(61)})
        dev = write_jar(folder / "examplemod-1.0.0-dev.jar", classes={"org/example/A.class": 52})
        api = write_jar(folder / "examplemod-1.0.0-api.jar", entries={"org/example/A.java": "class A {}"})
        pom = write_pom(folder / "examplemod-1.0.0.pom", "1.0.0", dependencies=[("com.other", "otherlib", "1.0",
                                                                                  "compile")])
        module = write_module(folder / "examplemod-1.0.0.module", "1.0.0", (jar.name, 10, "f" * 64), jvm=17)
        stray = folder / "readme.txt"
        stray.write_text("not for publishing", encoding="utf-8")
        result = self.check([jar, dev, api, pom, module, stray], version="1.0.0", java=8,
                            production=["examplemod-*.jar"], dependencies=["org.example:*"])
        self.assertEqual((result.outcome, result.code), ("finding", 1), result.summary)
        findings = result["findings"]
        expected = [
            "examplemod-1.0.0.jar: 1 class(es) need Java 17 (class version 61), above the target Java 8: "
            "org/example/B.class",
            "examplemod-1.0.0.jar!/META-INF/jarjar/lib.jar: 1 class(es) need Java 11",
            "examplemod-1.0.0.jar manifest Implementation-Version says version 0.9.0, not 1.0.0",
            "examplemod-1.0.0-dev.jar is a development JAR (classifier dev), not a production JAR",
            "examplemod-1.0.0-api.jar holds only sources (1 .java files)",
            "examplemod-1.0.0.pom (compile) declares com.other:otherlib:1.0, which is not on the dependency "
            "allow-list (org.example:*)",
            "examplemod-1.0.0.module variant apiElements: org.gradle.jvm.version is 17, but the target is Java 8",
            "examplemod-1.0.0.module lists examplemod-1.0.0.jar with SHA-256 ffffffffffff",
            "readme.txt is neither a production JAR (examplemod-*.jar) nor an allowed extra",
        ]
        for text in expected:
            self.assertTrue(any(text in f for f in findings), f"no finding with {text!r} in {findings}")
        self.assertEqual(len(findings), len(result.data.violations))
        self.assertFalse(any("META-INF/versions/17" in f for f in findings), findings)  # Java 17 classes for 17+

    def test_a_maven_channel_checks_its_layout_and_pom(self):
        folder = D.data / "check-maven"
        jar = mod_jar(folder, "1.0.0")
        pom = write_pom(folder / "pom-default.xml", "1.0.0", artifact="otherthing")
        result = self.check([jar, pom], version="1.0.0", channel="maven")
        self.assertEqual(result.outcome, "finding", result.summary)
        self.assertTrue(any("is the POM of org.example:otherthing:1.0.0, not of org.example:examplemod:1.0.0" in f
                            for f in result["findings"]), result["findings"])


class Publish(unittest.TestCase):
    def test_commands_and_the_approval_they_need(self):
        names = {c["name"]: c for c in C.commands()}
        for name in ("release.check", "release.publish", "release.status"):
            self.assertTrue(names[name]["active"], names[name]["reasons"])
        self.assertTrue(names["release.publish"]["spends"])
        self.assertFalse(names["release.check"]["spends"] or names["release.status"]["spends"])
        folder = D.data / "scheduled"
        envelope = D.workbench.submit("release.publish", {"channel": "gh", "version": "9.0.0",
                                                          "files": [str(mod_jar(folder, "9.0.0"))]}, source="schedule")
        self.assertEqual(envelope["outcome"], "usage")
        self.assertIn("schedule may not run", envelope["summary"])

    def test_github_waits_for_a_yes_then_uploads_exactly_the_plan(self):
        folder = D.data / "gh-1.0.0"
        jar, sources = mod_jar(folder, "1.0.0"), sources_jar(folder, "1.0.0")
        before = len(GH.requests)
        job = submit("gh", "1.0.0", [jar, sources], notes=str(NOTES), commit="0123abcd")
        approval = quoted(self, job)
        quote, plan = approval.quote, approval.quote["plan"]
        self.assertEqual(len(GH.requests), before, "the quote reads local files only")
        self.assertEqual(quote["max_cost"], 0.0)
        self.assertEqual({k: plan[k] for k in ("channel", "kind", "endpoint", "project", "version", "tag", "name",
                                               "visibility", "release_type", "commit", "upload_endpoint")},
                         {"channel": "gh", "kind": "github", "endpoint": GH.url, "project": "example/examplemod",
                          "version": "1.0.0", "tag": "v1.0.0", "name": "1.0.0", "visibility": "public",
                          "release_type": "release", "commit": "0123abcd", "upload_endpoint": GH.url + "/uploads"})
        files = {f["name"]: f for f in plan["files"]}
        self.assertEqual((files[jar.name]["sha256"], files[jar.name]["bytes"]), (digest(jar), jar.stat().st_size))
        self.assertEqual((files[jar.name]["role"], files[jar.name]["primary"]), ("production", True))
        self.assertEqual((files[sources.name]["role"], files[sources.name]["primary"]), ("extra", False))
        self.assertEqual(plan["notes"]["sha256"], digest(NOTES))
        self.assertEqual((plan["rules"]["name"], plan["rules"]["java"]), ("mod", 8))
        for text in ("1.0.0", "github channel gh", jar.name, digest(jar)[:12], digest(NOTES)[:12]):
            self.assertIn(text, approval.prompt["question"])

        job.approve(quote["id"], via="relayed")
        result = job.result(timeout=120)
        self.assertEqual(result.outcome, "ok", result.summary)
        sent = GH.requests[before:]
        self.assertEqual([(r.method, r.path) for r in sent],
                         [("GET", "/repos/example/examplemod/releases"),
                          ("POST", "/repos/example/examplemod/releases"),
                          ("POST", f"/uploads/repos/example/examplemod/releases/{GH.releases[0]['id']}/assets"),
                          ("POST", f"/uploads/repos/example/examplemod/releases/{GH.releases[0]['id']}/assets")])
        self.assertTrue(all(r.headers["authorization"] == "Bearer " + TOKENS["gh"] for r in sent))
        self.assertTrue(all(r.headers["user-agent"] == "wb-release-tests" for r in sent))
        created = json.loads(sent[1].body)
        self.assertEqual(created, {"tag_name": "v1.0.0", "name": "1.0.0", "body": NOTES.read_bytes().decode("utf-8"),
                                   "draft": False, "prerelease": False, "target_commitish": "0123abcd"})
        self.assertEqual([(r.query["name"], r.body) for r in sent[2:]],
                         [(jar.name, jar.read_bytes()), (sources.name, sources.read_bytes())])
        self.assertEqual(sent[2].headers["content-type"], "application/java-archive")
        receipt = result["data"]["receipt"]
        self.assertEqual(receipt_file(result), receipt)
        outputs = {o["name"]: Path(o["host_path"]).name for o in result["outputs"]}
        self.assertEqual(outputs, {"receipt": "receipt-gh-1.0.0.json"})
        self.assertTrue(receipt["complete"])
        self.assertEqual((receipt["quote"], receipt["plan_sha256"]), (quote["id"], quote["plan_sha256"]))
        self.assertEqual(receipt["release"]["url"], GH.releases[0]["html_url"])
        self.assertEqual([(f["name"], f["sha256"], f["verified"], f["verified_by"]) for f in receipt["files"]],
                         [(jar.name, digest(jar), True, "sha256"), (sources.name, digest(sources), True, "sha256")])
        self.assertEqual(result["approval"]["approved"]["via"], "relayed")

        held = status("gh", "1.0.0", [jar, sources])
        self.assertEqual(held.outcome, "ok", held.summary)
        self.assertTrue(held.data.exists)
        self.assertEqual([f["local"]["same"] for f in held.data.files], [True, True])
        self.assertFalse(status("gh", "0.0.1").data.exists)
        changed = mod_jar(D.data / "gh-1.0.0-other", "1.0.0", extra={"extra.txt": "another build"})
        other = status("gh", "1.0.0", [changed])
        self.assertEqual(other.outcome, "finding", other.summary)
        self.assertIn("holds other bytes", other["findings"][0])

    def test_a_file_changed_after_the_yes_is_quoted_again(self):
        folder = D.data / "gh-2.0.0"
        jar = mod_jar(folder, "2.0.0")
        job = submit("gh", "2.0.0", [jar])
        first = quoted(self, job).quote
        mod_jar(folder, "2.0.0", extra={"assets/examplemod/lang/en_us.lang": "item.example=Example"})  # rebuilt
        sent = len(GH.requests)
        job.approve(first["id"])
        second = quoted(self, job).quote
        self.assertEqual(len(GH.requests), sent, "a changed plan stops the run before it asks the channel anything")
        self.assertNotEqual(second["id"], first["id"])
        self.assertNotEqual(second["plan"]["files"][0]["sha256"], first["plan"]["files"][0]["sha256"])
        self.assertEqual(second["plan"]["files"][0]["sha256"], digest(jar))
        events = C.request("GET", f"/v1/jobs/{job.id}/events?format=json&after=0")["events"]
        reasons = [e["data"].get("reason") for e in events if e["kind"] == "quote"]
        self.assertEqual(reasons[0], None)
        self.assertIn(f"{jar.name} changed: sha256 {first['plan']['files'][0]['sha256'][:12]}", reasons[-1])
        job.approve(second["id"])
        result = job.result(timeout=120)
        self.assertEqual(result.outcome, "ok", result.summary)
        uploads = GH.sent("POST", "/uploads/")
        self.assertEqual(uploads[-1].body, jar.read_bytes())  # the bytes of the second yes

    def test_a_version_the_channel_holds_is_never_published_over(self):
        folder = D.data / "gh-3.0.0"
        jar = mod_jar(folder, "3.0.0")
        publish(self, "gh", "3.0.0", [jar])
        posts = len(GH.sent("POST"))
        job = submit("gh", "3.0.0", [jar])
        job.approve(quoted(self, job).quote["id"])
        result = job.result(timeout=120)
        self.assertEqual(result.outcome, "finding", result.summary)
        self.assertFalse(result.data.published)
        self.assertIn("already has a release tagged v3.0.0", result.summary)
        self.assertEqual(len(GH.sent("POST")), posts)

    def test_a_stopped_publish_leaves_a_receipt_of_what_it_did(self):
        folder = D.data / "gh-5.0.0"
        jar, sources = mod_jar(folder, "5.0.0"), sources_jar(folder, "5.0.0")
        GH.fail_asset = sources.name
        self.addCleanup(setattr, GH, "fail_asset", None)
        job = submit("gh", "5.0.0", [jar, sources])
        job.approve(quoted(self, job).quote["id"])
        result = job.result(timeout=120)
        self.assertEqual(result.outcome, "environment", result.summary)
        self.assertIn("publishing to gh stopped", result.summary)
        self.assertIn(f"{GH.releases[0]['html_url']} exists on github, with 1 of 2 file(s) uploaded", result.summary)
        receipt = receipt_file(result)
        self.assertFalse(receipt["complete"])
        self.assertEqual(receipt["release"]["id"], GH.releases[0]["id"])
        self.assertEqual([f["name"] for f in receipt["files"]], [jar.name])
        self.assertIn("HTTP 502", receipt["error"])
        self.assertIn("[redacted]", receipt["error"])  # the channel echoed the token; the receipt does not keep it
        self.assertNotIn(TOKENS["gh"], json.dumps(receipt) + result.summary)

    def test_modrinth_gets_one_version_with_every_file(self):
        folder = D.data / "mr-1.1.0"
        jar, sources = mod_jar(folder, "1.1.0"), sources_jar(folder, "1.1.0")
        quote, result = publish(self, "mr", "1.1.0", [jar, sources], notes=str(NOTES),
                                dependencies=["required:examplelib"])
        plan = quote["plan"]
        self.assertEqual((plan["release_type"], plan["game_versions"], plan["loaders"]),
                         ("beta", ["1.12.2"], ["forge"]))
        self.assertEqual(plan["dependencies"], [{"type": "required", "project": "examplelib"}])
        post = MR.sent("POST", "^/v2/version$")[-1]
        self.assertEqual(post.headers["authorization"], TOKENS["mr"])
        parts = fakes.multipart(post.headers["content-type"], post.body)
        data = json.loads(parts["data"].body)
        self.assertEqual({k: data[k] for k in ("version_number", "version_type", "loaders", "game_versions", "status",
                                               "project_id", "file_parts", "primary_file", "changelog",
                                               "dependencies", "name", "featured")},
                         {"version_number": "1.1.0", "version_type": "beta", "loaders": ["forge"],
                          "game_versions": ["1.12.2"], "status": "listed", "project_id": "AbCdEf12",
                          "file_parts": ["file0", "file1"], "primary_file": "file0",
                          "changelog": NOTES.read_bytes().decode("utf-8"), "name": "1.1.0", "featured": False,
                          "dependencies": [{"project_id": "examplelib", "dependency_type": "required"}]})
        self.assertEqual([(parts[p].filename, parts[p].body) for p in ("file0", "file1")],
                         [(jar.name, jar.read_bytes()), (sources.name, sources.read_bytes())])
        receipt = result["data"]["receipt"]
        version_id = MR.versions[0]["id"]
        self.assertEqual(receipt["release"], {"id": version_id, "version_number": "1.1.0",
                                              "url": f"{MR.url}/mod/examplemod/version/{version_id}"})
        self.assertEqual([(f["verified"], f["verified_by"]) for f in receipt["files"]], [(True, "sha512")] * 2)

        held = status("mr", "1.1.0", [jar, sources])
        self.assertEqual(held.outcome, "ok", held.summary)
        self.assertEqual([(f["local"]["same"], f["local"]["by"]) for f in held.data.files], [(True, "sha512")] * 2)

    def test_curseforge_resolves_its_ids_and_attaches_the_rest_to_the_main_file(self):
        folder = D.data / "cf-1.2.0"
        jar, sources = mod_jar(folder, "1.2.0"), sources_jar(folder, "1.2.0")
        before = len(CF.requests)
        quote, result = publish(self, "cf", "1.2.0", [jar, sources], notes=str(NOTES))
        self.assertEqual(quote["plan"]["precheck"], "files of the same name, on the Core API")
        sent = CF.requests[before:]
        self.assertEqual(sent[0].path, "/v1/mods/123456/files")  # asked what the project holds first
        self.assertEqual(sent[0].headers["x-api-key"], TOKENS["cf_core"])
        uploads = [r for r in sent if r.method == "POST"]
        self.assertEqual(len(uploads), 2)
        self.assertTrue(all(r.headers["x-api-token"] == TOKENS["cf"] for r in uploads))
        main = fakes.multipart(uploads[0].headers["content-type"], uploads[0].body)
        child = fakes.multipart(uploads[1].headers["content-type"], uploads[1].body)
        metadata = json.loads(main["metadata"].body)
        self.assertEqual(metadata, {"changelog": NOTES.read_bytes().decode("utf-8"), "changelogType": "markdown",
                                    "displayName": "1.2.0", "gameVersions": [6756, 4458, 7498],
                                    "releaseType": "release",
                                    "relations": {"projects": [{"slug": "examplelib", "type": "requiredDependency"}]}})
        main_id = CF.files[-2]["id"]
        self.assertEqual(json.loads(child["metadata"].body)["parentFileID"], main_id)
        self.assertNotIn("gameVersions", json.loads(child["metadata"].body))
        self.assertEqual((main["file"].filename, main["file"].body), (jar.name, jar.read_bytes()))
        self.assertEqual((child["file"].filename, child["file"].body), (sources.name, sources.read_bytes()))
        receipt = result["data"]["receipt"]
        self.assertEqual(receipt["release"]["url"], f"{CF.url}/projects/examplemod/files/{main_id}")
        self.assertEqual(receipt["release"]["game_version_ids"], [6756, 4458, 7498])

        held = status("cf", "1.2.0", [jar])
        self.assertEqual(held.outcome, "ok", held.summary)
        self.assertEqual([(f["name"], f["local"]["same"], f["local"]["by"]) for f in held.data.files
                          if "local" in f], [(jar.name, True, "sha1")])
        posts = len(CF.sent("POST"))
        again = submit("cf", "1.2.0", [jar, sources], notes=str(NOTES))
        again.approve(quoted(self, again).quote["id"])
        refused = again.result(timeout=120)
        self.assertEqual(refused.outcome, "finding", refused.summary)
        self.assertIn(f"already holds {jar.name}", refused.summary)
        self.assertEqual(len(CF.sent("POST")), posts)

    def test_maven_puts_every_file_its_checksums_and_the_metadata(self):
        folder = D.data / "maven-1.3.0"
        jar, sources = mod_jar(folder, "1.3.0"), sources_jar(folder, "1.3.0")
        pom = write_pom(folder / "pom-default.xml", "1.3.0",
                        dependencies=[("org.example", "examplelib", "2.0", "runtime")])
        module = write_module(folder / "module.json", "1.3.0", listed(jar), listed(sources))
        quote, result = publish(self, "maven", "1.3.0", [jar, sources, pom, module])
        plan = quote["plan"]
        self.assertEqual([f["name"] for f in plan["files"]],
                         [jar.name, sources.name, "examplemod-1.3.0.pom", "examplemod-1.3.0.module"])
        self.assertEqual(plan["maven"], {"folder": "org/example/examplemod/1.3.0",
                                         "checksums": ["md5", "sha1", "sha256", "sha512"], "metadata": True})
        base = "/releases/org/example/examplemod/1.3.0/"
        for local, published in ((jar, jar.name), (sources, sources.name), (pom, "examplemod-1.3.0.pom"),
                                 (module, "examplemod-1.3.0.module")):
            self.assertEqual(MV.files[base + published], local.read_bytes())
            for algo in ("md5", "sha1", "sha256", "sha512"):
                self.assertEqual(MV.files[f"{base}{published}.{algo}"].decode(), digest(local, algo))
        metadata = MV.files["/releases/org/example/examplemod/maven-metadata.xml"]
        root = ElementTree.fromstring(metadata)
        self.assertEqual([v.text for v in root.findall("versioning/versions/version")], ["1.3.0"])
        self.assertEqual(MV.files["/releases/org/example/examplemod/maven-metadata.xml.sha1"].decode(),
                         hashlib.sha1(metadata).hexdigest())
        puts = MV.sent("PUT")
        self.assertTrue(puts and all(r.headers["authorization"] == MV.auth for r in puts))
        self.assertEqual(result["data"]["receipt"]["release"]["coordinates"], "org.example:examplemod:1.3.0")

        held = status("maven", "1.3.0", [jar, pom])
        self.assertEqual(held.outcome, "ok", held.summary)
        self.assertTrue(held.data.release["in_metadata"])
        self.assertEqual([(f["name"], f["local"]["same"], f["local"]["by"]) for f in held.data.files],
                         [(jar.name, True, "sha256"), ("examplemod-1.3.0.pom", True, "sha256")])
        self.assertFalse(status("maven", "0.0.1").data.exists)

    def test_refusals_come_before_any_request(self):
        folder = D.data / "refusals"
        good = mod_jar(folder, "4.0.0")
        newer = mod_jar(folder / "java17", "4.0.0", major=61)
        before = [len(s.requests) for s in (GH, MR, CF, MV)]
        cases = [
            ("gh", [newer], {}, "usage", "2 class(es) need Java 17 (class version 61), above the target Java 8"),
            ("nowhere", [good], {}, "usage", "no channel 'nowhere'"),
            ("gh", [good], {"game_versions": ["1.12.2"]}, "usage", "a github channel takes no game_versions"),
            ("mr", [good], {"loaders": []}, "usage", "a modrinth channel needs game_versions and loaders"),
            ("insecure", [good], {}, "environment", "token in the clear"),
            ("unmounted", [good], {}, "environment", "secret release_nowhere is not mounted"),
            ("mr", [good], {"dependencies": ["needed:examplelib"]}, "usage", "is not type:project"),
        ]
        for channel, files, params, outcome, text in cases:
            with self.subTest(channel=channel, params=params):
                result = submit(channel, "4.0.0", files, **params).result(timeout=60)
                self.assertEqual((result.state, result.outcome), ("settled", outcome), result.summary)
                self.assertIn(text, result.summary)
        self.assertEqual([len(s.requests) for s in (GH, MR, CF, MV)], before)


# ---------------------------------------------------------------------------------------------
# Maven Central and the mirror

def central_channel(**change) -> channels.Channel:
    pom = {"name": "Example Mod", "description": "An example mod.", "url": "https://examplemod.example.invalid",
           "developers": [{"id": "example", "name": "Example Developer", "email": "dev@example.invalid"}],
           "scm": {"url": "https://git.example.invalid/examplemod",
                   "connection": "scm:git:https://git.example.invalid/examplemod.git",
                   "developer_connection": "scm:git:ssh://git.example.invalid/examplemod.git"}}
    raw = {"kind": "central", "base_url": "https://central.example.invalid", "project": "cc.orthrus:examplemod",
           "secret": "s", "signing": {"key": "ab" * 20, "secret": "p"}, "pom": pom, **change}
    return channels.load({"channels": {"c": raw}}, "c")


def mirror_channel(**change) -> channels.Channel:
    raw = {"kind": "mirror", "base_url": "https://account.r2.example.invalid", "bucket": "orthrus-maven",
           "public_url": "https://maven.example.invalid", "group": "cc.orthrus", "project": "cc.orthrus:examplemod",
           "secret": "s", "key_id_secret": "k", **change}
    return channels.load({"channels": {"m": raw}}, "m")


POM_TAGS = {"m": "http://maven.apache.org/POM/4.0.0"}


class MavenRoutes(unittest.TestCase):
    """The parts of the central and mirror channels that need no service."""

    def test_signature_version_4_matches_the_aws_example(self):
        # The GET Object example of the AWS Signature Version 4 documentation, with its published signature.
        now = calendar.timegm(time.strptime("20130524T000000Z", "%Y%m%dT%H%M%SZ"))
        signed = mirror.sign("GET", "https://examplebucket.s3.amazonaws.com/test.txt", key_id="AKIAIOSFODNN7EXAMPLE",
                             secret="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", region="us-east-1",
                             payload_sha256=mirror.EMPTY, headers={"Range": "bytes=0-9"}, now=now)
        self.assertEqual(signed["Authorization"],
                         "AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request, "
                         "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date, "
                         "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41")
        self.assertEqual(signed["x-amz-date"], "20130524T000000Z")

    def test_metadata_is_merged_and_names_the_newest(self):
        self.assertEqual([mirror.compare_versions(a, b) for a, b in
                          (("1.0", "1.0.0"), ("1.0-beta", "1.0"), ("1.10", "1.9"), ("2.0", "10.0"))], [0, -1, 1, -1])
        first, versions = mirror.merged_metadata(None, "cc.orthrus", "examplemod", "1.2.0")
        self.assertEqual(versions, ["1.2.0"])
        body, versions = mirror.merged_metadata(first, "cc.orthrus", "examplemod", "1.1.0")
        root = ElementTree.fromstring(body)
        self.assertEqual(versions, ["1.2.0", "1.1.0"])
        self.assertEqual((root.findtext("versioning/latest"), root.findtext("versioning/release")), ("1.2.0", "1.2.0"))
        with self.assertRaises(transport.ChannelError):
            mirror.merged_metadata(first, "cc.orthrus", "othermod", "1.0.0")

    def test_the_pom_carries_the_channel_metadata_and_the_licence(self):
        chan = central_channel()
        folder = Path(tempfile.mkdtemp(prefix="wb-release-pom-"))
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        given = write_pom(folder / "pom.xml", "1.0.0", group="cc.orthrus",
                          dependencies=[("org.example", "examplelib", "2.0", "compile")])
        root = ElementTree.fromstring(central.pom(chan, "1.0.0", LICENCE, given.read_bytes()))
        self.assertEqual(root.tag, "{http://maven.apache.org/POM/4.0.0}project")
        text = {path: root.findtext(path, namespaces=POM_TAGS) for path in (
            "m:groupId", "m:artifactId", "m:version", "m:name", "m:description", "m:url",
            "m:licenses/m:license/m:name", "m:licenses/m:license/m:url", "m:developers/m:developer/m:id",
            "m:developers/m:developer/m:email", "m:scm/m:connection", "m:scm/m:developerConnection", "m:scm/m:url",
            "m:dependencies/m:dependency/m:artifactId")}
        self.assertEqual(text, {
            "m:groupId": "cc.orthrus", "m:artifactId": "examplemod", "m:version": "1.0.0", "m:name": "Example Mod",
            "m:description": "An example mod.", "m:url": "https://examplemod.example.invalid",
            "m:licenses/m:license/m:name": LICENCE["name"], "m:licenses/m:license/m:url": LICENCE["url"],
            "m:developers/m:developer/m:id": "example", "m:developers/m:developer/m:email": "dev@example.invalid",
            "m:scm/m:connection": "scm:git:https://git.example.invalid/examplemod.git",
            "m:scm/m:developerConnection": "scm:git:ssh://git.example.invalid/examplemod.git",
            "m:scm/m:url": "https://git.example.invalid/examplemod", "m:dependencies/m:dependency/m:artifactId":
            "examplelib"})
        alone = rulebook.read_pom(central.pom(chan, "1.0.0", LICENCE, None))
        self.assertEqual((alone["group"], alone["artifact"], alone["version"], alone["dependencies"]),
                         ("cc.orthrus", "examplemod", "1.0.0", []))
        placeholder = central.placeholder_javadoc(chan, "1.0.0")
        self.assertEqual(placeholder, central.placeholder_javadoc(chan, "1.0.0"), "the same bytes each time")
        with zipfile.ZipFile(io.BytesIO(placeholder)) as jar:
            self.assertEqual(sorted(jar.namelist()), ["META-INF/MANIFEST.MF", "README.md"])
            self.assertIn("placeholder", jar.read("README.md").decode())

    def test_central_and_mirror_config_is_checked(self):
        self.assertEqual(central_channel(signing={"key": "ab" * 20, "secret": "p"}).settings["signing"]["key"],
                         "AB" * 20)
        self.assertEqual(central_channel().settings["publishing_type"], "USER_MANAGED")
        self.assertEqual(central_channel().settings["javadoc"], "built")
        for change in ({"signing": {"key": "ABCD1234", "secret": "p"}}, {"checksums": ["sha256"]},
                       {"publishing_type": "LATER"}, {"pom": {"name": "x"}}, {"project": "examplemod"}):
            with self.subTest(change), self.assertRaises(EnvironmentProblem):
                central_channel(**change)
        self.assertEqual(mirror_channel().settings["region"], "auto")
        self.assertEqual(mirror_channel(project="cc.orthrus.tools:examplemod").project, "cc.orthrus.tools:examplemod")
        for change, text in (({"project": "org.example:examplemod"}, "publishes only cc.orthrus"),
                             ({"project": "cc.orthrusx:examplemod"}, "publishes only cc.orthrus"),
                             ({"bucket": "Orthrus_Maven"}, "not a bucket name"),
                             ({"public_url": "http://maven.example.invalid"}, "in the clear")):
            with self.subTest(change), self.assertRaises(EnvironmentProblem) as raised:
                mirror_channel(**change)
            self.assertIn(text, str(raised.exception))


def bundle_of(deployment: str) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(CE.deployments[deployment]["bundle"]))


class Central(unittest.TestCase):
    FOLDER = "cc/orthrus/examplemod"

    def setUp(self):
        if KEY is None:
            self.skipTest(NO_KEY)

    def test_the_bundle_is_signed_checked_and_waits_for_a_person(self):
        folder = D.data / "central-1.0.0"
        jar, sources, javadoc = mod_jar(folder, "1.0.0"), sources_jar(folder, "1.0.0"), javadoc_jar(folder, "1.0.0")
        pom = write_pom(folder / "pom-default.xml", "1.0.0", group="cc.orthrus",
                        dependencies=[("org.example", "examplelib", "2.0", "compile")])
        before = len(CE.requests)
        job = submit("central", "1.0.0", [jar, sources, javadoc, pom])
        approval = quoted(self, job)
        plan = approval.quote["plan"]
        self.assertEqual(len(CE.requests), before, "the quote reads local files only")
        self.assertEqual([f["name"] for f in plan["files"]],
                         [jar.name, sources.name, javadoc.name, "examplemod-1.0.0.pom"])
        self.assertIn("written by Workbench from", plan["files"][3]["path"])
        self.assertEqual(plan["central"]["publishing_type"], "USER_MANAGED")
        self.assertEqual((plan["central"]["signing_key"], plan["central"]["javadoc"]), (KEY, "built"))
        self.assertEqual(plan["precheck"], "whether Central has published this version")
        self.assertIn(f"signed by {KEY}", approval.prompt["question"])
        job.approve(approval.quote["id"])
        result = job.result(timeout=120)
        self.assertEqual(result.outcome, "ok", result.summary)
        self.assertIn("waits for a person to press Publish", result.summary)
        sent = CE.requests[before:]
        self.assertEqual([(r.method, r.path) for r in sent],
                         [("GET", "/api/v1/publisher/published"), ("POST", "/api/v1/publisher/upload")]
                         + [("POST", "/api/v1/publisher/status")] * 3)
        self.assertEqual(sent[0].query, {"namespace": "cc.orthrus", "name": "examplemod", "version": "1.0.0"})
        self.assertEqual(sent[1].query, {"name": "cc.orthrus:examplemod:1.0.0", "publishingType": "USER_MANAGED"})
        self.assertFalse([r for r in sent if "/deployment/" in r.path], "a person presses Publish, not Workbench")
        receipt = result["data"]["receipt"]
        deployment = receipt["release"]["deployment"]
        self.assertEqual(CE.deployments[deployment]["errors"], [])
        self.assertEqual(receipt["release"]["state"], "VALIDATED")
        self.assertEqual(receipt["release"]["bundle"]["sha256"], hashlib.sha256(CE.deployments[deployment]["bundle"])
                         .hexdigest())
        self.assertTrue(receipt["complete"])
        base = f"{self.FOLDER}/1.0.0/"
        with bundle_of(deployment) as bundle:
            names = set(bundle.namelist())
            published = [jar.name, sources.name, javadoc.name, "examplemod-1.0.0.pom"]
            self.assertEqual(names, {base + n + suffix for n in published
                                     for suffix in ("", ".md5", ".sha1", ".sha256", ".sha512", ".asc")})
            for local in (jar, sources, javadoc):
                self.assertEqual(bundle.read(base + local.name), local.read_bytes())
            written = bundle.read(base + "examplemod-1.0.0.pom")
            self.assertEqual(hashlib.sha256(written).hexdigest(), plan["files"][3]["sha256"], "the approved POM")
            root = ElementTree.fromstring(written)
            self.assertEqual(root.findtext("m:licenses/m:license/m:name", namespaces=POM_TAGS), LICENCE["name"])
            self.assertEqual(root.findtext("m:dependencies/m:dependency/m:artifactId", namespaces=POM_TAGS),
                             "examplelib")
            checks = Path(tempfile.mkdtemp(prefix="wb-release-asc-"))
            self.addCleanup(shutil.rmtree, checks, ignore_errors=True)
            for name in published:
                (checks / name).write_bytes(bundle.read(base + name))
                (checks / f"{name}.asc").write_bytes(bundle.read(base + name + ".asc"))
                verified = gpg("--status-fd", "1", "--verify", (checks / f"{name}.asc").as_posix(),
                               (checks / name).as_posix())
                self.assertIn(f"VALIDSIG {KEY}", verified.stdout, verified.stderr)
        self.assertFalse(status("central", "1.0.0").data.exists, "validated is not published")

    def test_automatic_with_a_placeholder_javadoc_then_never_again(self):
        folder = D.data / "central-1.1.0"
        jar, sources = mod_jar(folder, "1.1.0"), sources_jar(folder, "1.1.0")
        job = submit("central_auto", "1.1.0", [jar, sources])
        approval = quoted(self, job)
        plan = approval.quote["plan"]
        names = [f["name"] for f in plan["files"]]
        self.assertEqual(names, [jar.name, sources.name, "examplemod-1.1.0-javadoc.jar", "examplemod-1.1.0.pom"])
        self.assertIn('javadoc = "placeholder"', plan["files"][2]["path"])
        self.assertIn("with a placeholder javadoc JAR", approval.prompt["question"])
        job.approve(approval.quote["id"])
        result = job.result(timeout=120)
        self.assertEqual(result.outcome, "ok", result.summary)
        receipt = result["data"]["receipt"]
        self.assertEqual(receipt["release"]["state"], "PUBLISHED")
        self.assertIn("published 1.1.0", result.summary)
        with bundle_of(receipt["release"]["deployment"]) as bundle:
            entries = bundle.namelist()
            readme = zipfile.ZipFile(io.BytesIO(bundle.read(f"{self.FOLDER}/1.1.0/examplemod-1.1.0-javadoc.jar")))
            self.assertIn("No Javadoc is published", readme.read("README.md").decode())
        self.assertFalse([e for e in entries if e.endswith((".sha256", ".sha512"))], "this channel asks md5, sha1")
        self.assertTrue(status("central", "1.1.0").data.exists)
        uploads = len(CE.sent("POST", "/upload"))
        again = submit("central_auto", "1.1.0", [jar, sources])
        again.approve(quoted(self, again).quote["id"])
        refused = again.result(timeout=120)
        self.assertEqual(refused.outcome, "finding", refused.summary)
        self.assertIn("Maven Central already has cc.orthrus:examplemod:1.1.0", refused.summary)
        self.assertEqual(len(CE.sent("POST", "/upload")), uploads)

    def test_a_failed_deployment_leaves_its_id_in_the_receipt(self):
        folder = D.data / "central-1.2.0"
        files = [mod_jar(folder, "1.2.0"), sources_jar(folder, "1.2.0"), javadoc_jar(folder, "1.2.0")]
        CE.refuse = ["Invalid signature for file examplemod-1.2.0.jar"]
        job = submit("central", "1.2.0", files)
        job.approve(quoted(self, job).quote["id"])
        result = job.result(timeout=120)
        self.assertEqual(result.outcome, "environment", result.summary)
        self.assertIn("Central failed deployment", result.summary)
        receipt = receipt_file(result)
        self.assertFalse(receipt["complete"])
        self.assertEqual(receipt["release"]["state"], "FAILED")
        self.assertIn(receipt["release"]["deployment"], CE.deployments)
        self.assertIn("Invalid signature", receipt["error"])
        self.assertNotIn(TOKENS["signing_pass"], json.dumps(receipt) + result.summary)

    def test_refusals_come_before_any_request(self):
        folder = D.data / "central-refusals"
        jar, sources, javadoc = mod_jar(folder, "2.0.0"), sources_jar(folder, "2.0.0"), javadoc_jar(folder, "2.0.0")
        other = write_pom(folder / "pom-default.xml", "2.0.0", group="org.example")
        before = len(CE.requests)
        cases = [("central", [jar, sources], "Central needs the javadoc JAR examplemod-2.0.0-javadoc.jar"),
                 ("central", [jar, javadoc], "Central needs the sources JAR"),
                 ("central", [jar, sources, javadoc, other], "is the POM of org.example:examplemod:2.0.0"),
                 ("central_unlicensed", [jar, sources], "Central needs the POM's licence")]
        for channel, files, text in cases:
            with self.subTest(channel=channel, files=[f.name for f in files]):
                result = submit(channel, "2.0.0", files).result(timeout=60)
                self.assertEqual(result.outcome, "usage", result.summary)
                self.assertIn(text, result.summary)
        self.assertEqual(len(CE.requests), before)
        checked = C.submit("release.check", {"files": [str(jar), str(sources)], "version": "2.0.0",
                                             "channel": "central"}).result(timeout=60)
        self.assertEqual(checked.outcome, "finding", checked.summary)
        self.assertIn("javadoc JAR", checked["findings"][0])


class Mirror(unittest.TestCase):
    ROOT = "cc/orthrus/examplemod"

    def files(self, version: str) -> list[Path]:
        folder = D.data / f"mirror-{version}"
        return [mod_jar(folder, version), sources_jar(folder, version),
                write_pom(folder / "pom-default.xml", version, group="cc.orthrus")]

    def test_every_path_is_written_once_signed_and_read_back(self):
        jar, sources, pom = self.files("1.0.0")
        before, public = len(BK.requests), len(PB.requests)
        quote, result = publish(self, "mirror", "1.0.0", [jar, sources, pom])
        self.assertEqual(quote["plan"]["mirror"], {"bucket": "orthrus-maven", "public_url": PB.url,
                                                   "group": "cc.orthrus", "region": "auto"})
        self.assertEqual(quote["plan"]["maven"]["metadata"], True)
        sent = BK.requests[before:]
        self.assertTrue(all(r.headers["authorization"].startswith("AWS4-HMAC-SHA256 Credential="
                                                                  f"{TOKENS['r2_key_id']}/") for r in sent))
        puts = [r for r in sent if r.method == "PUT"]
        base = f"/orthrus-maven/{self.ROOT}/1.0.0/"
        names = [jar.name, sources.name, "examplemod-1.0.0.pom"]
        expected = [base + n + suffix for n in names for suffix in ("", ".md5", ".sha1", ".sha256", ".sha512")]
        expected += [f"/orthrus-maven/{self.ROOT}/maven-metadata.xml" + s
                     for s in ("", ".md5", ".sha1", ".sha256", ".sha512")]
        self.assertEqual([r.path for r in puts], expected)
        self.assertTrue(all(r.headers.get("if-none-match") == "*" for r in puts[:15]), "a path is written once")
        for local, name in ((jar, jar.name), (sources, sources.name), (pom, "examplemod-1.0.0.pom")):
            held = BK.objects[f"{self.ROOT}/1.0.0/{name}"]
            self.assertEqual((held["body"], held["meta"]["sha256"]), (local.read_bytes(), digest(local)))
            for algo in ("md5", "sha1", "sha256", "sha512"):
                self.assertEqual(BK.objects[f"{self.ROOT}/1.0.0/{name}.{algo}"]["body"].decode(), digest(local, algo))
        metadata = ElementTree.fromstring(BK.objects[f"{self.ROOT}/maven-metadata.xml"]["body"])
        self.assertIn("1.0.0", [v.text for v in metadata.findall("versioning/versions/version")])
        reads = PB.requests[public:]
        self.assertEqual(len(reads), 20, "every path read back from the public base URL")
        self.assertTrue(all("wb-verify" in r.query for r in reads))
        receipt = result["data"]["receipt"]
        self.assertEqual([(f["name"], f["verified"], f["verified_by"], f["kept"]) for f in receipt["files"]],
                         [(n, True, "sha256", False) for n in names])
        self.assertEqual(receipt["release"]["verified"], 20)
        self.assertEqual(receipt["files"][0]["url"], f"{PB.url}/{self.ROOT}/1.0.0/{jar.name}")

        held = status("mirror", "1.0.0", [jar, sources])
        self.assertEqual(held.outcome, "ok", held.summary)
        self.assertEqual([f["local"]["same"] for f in held.data.files], [True, True])
        writes = len(BK.sent("PUT"))
        again = submit("mirror", "1.0.0", [jar, sources, pom])
        again.approve(quoted(self, again).quote["id"])
        refused = again.result(timeout=120)
        self.assertEqual(refused.outcome, "finding", refused.summary)
        self.assertIn("with these exact bytes; nothing to publish", refused.summary)
        self.assertEqual(len(BK.sent("PUT")), writes)

    def test_the_same_bytes_are_kept_and_other_bytes_refused(self):
        jar, sources, pom = self.files("2.0.0")
        BK.put(f"{self.ROOT}/2.0.0/{jar.name}", jar.read_bytes())  # left by an earlier, stopped publish
        quote, result = publish(self, "mirror", "2.0.0", [jar, sources, pom])
        self.assertFalse(BK.sent("PUT", f"/2.0.0/{jar.name}$"), "the same bytes are not written again")
        receipt = result["data"]["receipt"]
        self.assertEqual([f["kept"] for f in receipt["files"]], [True, False, False])
        self.assertIn(f"{self.ROOT}/2.0.0/{jar.name}", receipt["release"]["kept"])

        jar, sources, pom = self.files("2.1.0")
        BK.put(f"{self.ROOT}/2.1.0/{sources.name}", b"another build")
        writes = len(BK.sent("PUT"))
        job = submit("mirror", "2.1.0", [jar, sources, pom])
        job.approve(quoted(self, job).quote["id"])
        refused = job.result(timeout=120)
        self.assertEqual(refused.outcome, "finding", refused.summary)
        self.assertIn(f"{sources.name} with other bytes", refused.summary)

        jar, sources, pom = self.files("2.2.0")
        BK.put(f"{self.ROOT}/2.2.0/{jar.name}.sha1", b"0" * 40)
        job = submit("mirror", "2.2.0", [jar, sources, pom])
        job.approve(quoted(self, job).quote["id"])
        stopped = job.result(timeout=120)
        self.assertEqual(stopped.outcome, "environment", stopped.summary)
        self.assertIn(f"{jar.name}.sha1 already holds other bytes", stopped.summary)
        self.assertIn("nothing was uploaded", stopped.summary)
        self.assertEqual(len(BK.sent("PUT")), writes)

    def test_metadata_keeps_what_another_writer_listed(self):
        key = f"{self.ROOT}/maven-metadata.xml"
        body, _ = mirror.merged_metadata(BK.objects.get(key, {}).get("body"), "cc.orthrus", "examplemod", "99.0.0")
        BK.put(key, body)
        jar, sources, pom = self.files("3.0.0")
        publish(self, "mirror", "3.0.0", [jar, sources, pom])
        root = ElementTree.fromstring(BK.objects[key]["body"])
        listed = [v.text for v in root.findall("versioning/versions/version")]
        self.assertEqual(listed[-2:], ["99.0.0", "3.0.0"])
        self.assertEqual(root.findtext("versioning/release"), "99.0.0")
        put = BK.sent("PUT", "/maven-metadata.xml$")[-1]
        self.assertEqual(put.headers["if-match"].strip('"'), fakes.sha("md5", body), "written only over what it read")

    def test_bytes_served_otherwise_are_a_finding(self):
        jar, sources, pom = self.files("4.0.0")
        PB.override[f"{self.ROOT}/4.0.0/{jar.name}"] = b"a stale cache"
        self.addCleanup(PB.override.clear)
        job = submit("mirror", "4.0.0", [jar, sources, pom])
        job.approve(quoted(self, job).quote["id"])
        result = job.result(timeout=120)
        self.assertEqual(result.outcome, "finding", result.summary)
        self.assertIn(f"{jar.name} serves other bytes", " ".join(result["findings"]))
        receipt = receipt_file(result)
        self.assertEqual(receipt["files"][0]["verified"], False)
        self.assertTrue(receipt["complete"])

    def test_refusals_before_any_write(self):
        jar, sources, pom = self.files("5.0.0")
        writes = len(BK.sent("PUT"))
        before = len(BK.requests)
        result = submit("mirror_other_group", "5.0.0", [jar]).result(timeout=60)
        self.assertEqual(result.outcome, "environment", result.summary)
        self.assertIn("publishes only cc.orthrus", result.summary)
        self.assertEqual(len(BK.requests), before)
        job = submit("mirror_wrong_key", "5.0.0", [jar, sources, pom])
        job.approve(quoted(self, job).quote["id"])
        refused = job.result(timeout=120)
        self.assertEqual(refused.outcome, "environment", refused.summary)
        self.assertIn("HTTP 403", refused.summary)
        self.assertIn("nothing was uploaded", refused.summary)
        self.assertEqual(len(BK.sent("PUT")), writes)


class FromSet(unittest.TestCase):
    """release.publish set=<project>@<revision>: the Store's bytes the set names, under the line's names."""

    COMMIT = "c0ffee" + "0" * 34

    @classmethod
    def setUpClass(cls):
        cls.store = D.data / "set-out" / "store"
        sets = cls.store.parent / "sets"
        built = D.data / "set-built"
        cls.jar, cls.sources = mod_jar(built, "7.0.0"), sources_jar(built, "7.0.0")
        artifacts = {}
        for key, path in (("sources", cls.sources), ("mod", cls.jar)):  # the line lists sources first
            sha = digest(path)
            kept = cls.store / "files" / sha / path.name
            kept.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, kept)
            artifacts[key] = {"sha256": sha, "file": path.name}
        cls.revision = "ab" * 20
        (sets / cls.revision).mkdir(parents=True, exist_ok=True)
        (sets / cls.revision / "set.json").write_text(json.dumps(
            {"schema": 1, "projects": {"examplemod": {"branch": "main", "commit": cls.COMMIT,
                                                      "artifacts": artifacts}}}), encoding="utf-8")
        (sets / "HEAD").write_text(cls.revision, encoding="ascii")

    def submit(self, version: str, **params):
        return C.submit("release.publish", {"channel": "gh", "version": version, **params})

    def test_the_set_names_the_bytes_and_the_commit(self):
        job = self.submit("7.0.0", set=f"examplemod@{self.revision[:10]}")
        approval = quoted(self, job)
        plan = approval.quote["plan"]
        self.assertEqual([(f["name"], f["sha256"], f["primary"]) for f in plan["files"]],
                         [(self.jar.name, digest(self.jar), True), (self.sources.name, digest(self.sources), False)])
        self.assertEqual(plan["set"], {"revision": self.revision, "project": "examplemod", "commit": self.COMMIT,
                                       "artifacts": ["mod", "sources"]})
        self.assertEqual(plan["commit"], self.COMMIT, "the tag is made at the commit the line names")
        self.assertIn(f"the bytes set {self.revision[:12]} names for examplemod", approval.prompt["question"])
        job.approve(approval.quote["id"])
        result = job.result(timeout=120)
        self.assertEqual(result.outcome, "ok", result.summary)
        uploads = GH.sent("POST", "/uploads/")
        self.assertEqual([(u.query["name"], u.body) for u in uploads[-2:]],
                         [(self.jar.name, self.jar.read_bytes()), (self.sources.name, self.sources.read_bytes())])
        created = json.loads([r for r in GH.sent("POST") if r.path == "/repos/example/examplemod/releases"][-1].body)
        self.assertEqual(created["target_commitish"], self.COMMIT)

    def test_head_a_chosen_artifact_and_a_given_commit(self):
        job = self.submit("7.0.0", set="examplemod@head", artifacts=["mod"], commit="feedbeef")
        plan = quoted(self, job).quote["plan"]
        job.cancel()
        self.assertEqual([f["name"] for f in plan["files"]], [self.jar.name])
        self.assertEqual((plan["set"]["revision"], plan["commit"]), (self.revision, "feedbeef"))

    def test_refusals(self):
        cases = [({"set": f"examplemod@{self.revision}", "files": [str(self.jar)]}, "not both"),
                 ({"artifacts": ["mod"], "files": [str(self.jar)]}, "give set="),
                 ({"set": "other@head"}, "has no line for other"),
                 ({"set": "examplemod@head", "artifacts": ["lab"]}, "has no artifact lab"),
                 ({"set": "examplemod@1234567"}, "no export")]
        for params, text in cases:
            with self.subTest(params=params):
                result = self.submit("7.0.2", **params).result(timeout=60)
                self.assertEqual(result.outcome, "usage", result.summary)
                self.assertIn(text, result.summary)

    def test_a_store_copy_that_changed_is_refused(self):
        sha = digest(self.jar)
        kept = self.store / "files" / sha / self.jar.name
        original = kept.read_bytes()
        try:
            os.chmod(kept, 0o644)
            kept.write_bytes(original + b"tampered")
            result = self.submit("7.0.3", set="examplemod@head").result(timeout=60)
            self.assertEqual(result.outcome, "environment", result.summary)
            self.assertIn("no longer hashes", result.summary)
        finally:
            kept.write_bytes(original)


if __name__ == "__main__":
    unittest.main()
