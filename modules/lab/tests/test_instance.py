"""lab.build with small fixtures and no network: a fake installer and launcher instance, members in a Store, the JDK
folder the Store keeps. Members are hashed again before they are placed, the receipt names every file by SHA-256 under
{instance}, {cache} and {jdk:N} with the command lines, a second instance reuses the template, and a launch file whose
runtime files are missing is refused with the fix."""

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path[:0] = [str(REPO / "core"), str(REPO / "sdk" / "python"), str(REPO / "modules" / "lab")]

from wb_core.module import Context, EnvironmentProblem, ParamError  # noqa: E402
from wb_lab import assemble, instance  # noqa: E402

VERSION = "0.7.14-alpha"
BUILD = "26.0.2.1+1"
UNIVERSAL = b"the universal jar"
INSTALLER = r'''
import pathlib, sys
args = sys.argv
target = pathlib.Path(args[args.index("--dir") + 1])
version = args[args.index("--version") + 1]
folder = target / "libraries" / "com" / "cleanroommc" / "cleanroom" / version
folder.mkdir(parents=True)
(folder / f"cleanroom-{version}-universal.jar").write_bytes(UNIVERSAL)
(folder / "win_args.txt").write_text("-cp libraries/x.jar\n")
(folder / "unix_args.txt").write_text("-cp libraries/x.jar\n")
(target / "user_jvm_args.txt").write_text("# the installer's own\n")
print("Installed")
'''.replace("UNIVERSAL", repr(UNIVERSAL))


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def jar(*members: tuple[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in members:
            archive.writestr(name, data)
    return buffer.getvalue()


CLIENT = jar(("net/minecraft/client/Main.class", b"client"))
LIBRARY = jar(("org/example/Lib.class", b"lib"))
NATIVE = jar(("game.dll", b"a native"), ("libgame.so", b"a native"))
OBJECT = b"a texture"
INDEX = json.dumps({"objects": {"textures/a.png": {"hash": sha1(OBJECT), "size": len(OBJECT)}}}).encode()


# A base as a base project's [launch.standalone] carries it (Cubature's standalone world), the installer this Python.
STANDALONE = {
    "summary": "a local creative server on 127.0.0.1 and one offline client",
    "runtime": "cleanroom",
    "player": "CubicLab",
    "installer": [sys.executable, "{installer}", "--dir", "{target}", "--version", "{version}"],
    "memory": "8G",
    "jvm_profile": {"name": "zgc-fixed", "collector": "ZGC", "initialHeapMiB": 8192, "maxHeapMiB": 8192,
                    "arguments": ["-Xms8192M", "-Xmx8G", "-XX:+UseZGC"]},
    "jvm_args_file": "# Owned lab profile: same heap and collector on server and client.\n",
    "files": {
        "both": {"config/forge_early.cfg": "general {\n    B:DISABLE_PATCH_MOD_CHECK=true\n}\n",
                 "config/cubicchunks.cfg": "general {\n    I:verticalCubeLoadDistance=12\n}\n"},
        "server": {"server.properties": "server-ip=127.0.0.1\nserver-port={port}\nlevel-name=world\n"
                                        "level-type=DEFAULT\nlevel-seed={seed}\nview-distance=12\n",
                   "ops.json": '[{"uuid": "{player_uuid}", "name": "{player}", "level": 4}]\n'},
        "client": {"options.txt": "renderDistance:12\nmaxFps:120\n"},
    },
    "views": {"horizontal": ["server/server.properties:view-distance"],
              "vertical": ["server/config/cubicchunks.cfg:I:verticalCubeLoadDistance",
                           "client/config/cubicchunks.cfg:I:verticalCubeLoadDistance"],
              "client_render": ["client/options.txt:renderDistance"]},
    "eula": "eula=true\n",
    "server": ["{java}", "{jvm}", "{profile}", "-Djava.io.tmpdir={game}/java-tmp", "-Dfile.encoding=UTF-8",
               "-Djava.awt.headless=true", "@user_jvm_args.txt",
               "@{game}/libraries/com/cleanroommc/cleanroom/{version}/{args_file}", "nogui"],
    "client": ["{java}", "{jvm}", "{profile}", "-Djava.io.tmpdir={game}/java-tmp", "-Dfile.encoding=UTF-8",
               "--enable-native-access=ALL-UNNAMED", "-Djava.library.path={game}/natives", "-cp", "{classpath}",
               "{main_class}", "{tweakers}", "--username", "{player}", "--uuid", "{player_uuid}",
               "--accessToken", "0", "--userType", "legacy", "--versionType", "custom",
               "--version", "Cleanroom-{version}", "--gameDir", "{game}", "--assetsDir", "{assets}",
               "--assetIndex", "{asset_index}", "--width", "1280", "--height", "720",
               "--server", "127.0.0.1", "--port", "{port}"],
}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="wb-build-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.store = self.root / "store"
        self.cache = self.root / "cache"
        self.system, _ = assemble.host_system()
        # the JDK as store.platform unpacks it
        home = self.store / "jdks" / BUILD / self.system / f"jdk-{BUILD}"
        (home / "bin").mkdir(parents=True)
        (home / "bin" / ("java.exe" if self.system == "windows" else "java")).write_bytes(b"not a real java")
        (home / "release").write_text(f'JAVA_RUNTIME_VERSION="{BUILD}"\n', encoding="utf-8")
        # the runtime files
        native_name = f"org.lwjgl:lwjgl:3.0:natives-{self.system}"
        game = {"uid": "net.minecraft", "version": "1.12.2",
                "mainJar": {"name": "com.mojang:minecraft:1.12.2:client",
                            "downloads": {"artifact": {"sha1": sha1(CLIENT), "url": "https://example.org/client.jar"}}},
                "assetIndex": {"id": "1.12", "sha1": sha1(INDEX), "url": "https://example.org/1.12.json"},
                "libraries": [{"name": "org.example:lib:1.0",
                               "downloads": {"artifact": {"sha1": sha1(LIBRARY), "url": "https://example.org/lib.jar"}}}]}
        loader = {"uid": "net.minecraftforge", "version": VERSION, "mainClass": "top.example.Boot",
                  "+tweakers": ["net.example.Tweaker"], "libraries": [{"name": native_name, "MMC-hint": "local"}]}
        pack = {"formatVersion": 1, "components": [{"uid": "net.minecraft", "version": "1.12.2"},
                                                   {"uid": "net.minecraftforge", "version": VERSION}]}
        prism = jar(("mmc-pack.json", json.dumps(pack).encode()),
                    ("patches/net.minecraft.json", json.dumps(game).encode()),
                    ("patches/net.minecraftforge.json", json.dumps(loader).encode()),
                    (f"libraries/lwjgl-3.0-natives-{self.system}.jar", NATIVE))
        # what the cache already holds, so nothing is downloaded
        for relative, data in ((assemble.maven_path("com.mojang:minecraft:1.12.2:client"), CLIENT),
                               (assemble.maven_path("org.example:lib:1.0"), LIBRARY)):
            (self.cache / "libraries" / relative).parent.mkdir(parents=True, exist_ok=True)
            (self.cache / "libraries" / relative).write_bytes(data)
        (self.cache / "assets" / "indexes").mkdir(parents=True)
        (self.cache / "assets" / "indexes" / "1.12.json").write_bytes(INDEX)
        (self.cache / "assets" / "objects" / sha1(OBJECT)[:2]).mkdir(parents=True)
        (self.cache / "assets" / "objects" / sha1(OBJECT)[:2] / sha1(OBJECT)).write_bytes(OBJECT)
        self.runtime = {}
        for part, name, data in (("installer", f"cleanroom-{VERSION}-installer.jar", INSTALLER.encode()),
                                 ("universal", f"cleanroom-{VERSION}-universal.jar", UNIVERSAL),
                                 ("prism", f"cleanroom-{VERSION}.zip", prism)):
            digest = sha256(data)
            if part != "universal":  # the universal comes from the installer
                self.keep(name, data)
            self.runtime[part] = {"url": f"https://example.org/{VERSION}/{name}", "file": name, "sha256": digest}
        # members
        self.mod = jar(("mcmod.info", b'[{"modid": "cube"}]'))
        self.client_mod = jar(("mcmod.info", b'[{"modid": "draw"}]'))
        members = []
        for spec, file, data, side, place in (("cube:mod@both", "cube-1.jar", self.mod, "both", None),
                                              ("draw:mod@client", "draw-1.jar", self.client_mod, "client",
                                               "+Draw.jar")):
            self.keep(file, data)
            members.append({"member": spec, "name": spec.split(":")[0], "side": side, "kind": "line",
                            "sha256": sha256(data), "file": file, "path": f"files/{sha256(data)}/{file}",
                            "mod_ids": [], **({"place": place} if place else {})})
        self.launch_doc = {"schema": 1, "project": "game", "launch": "default", "commit": "a" * 40, "tree": "b" * 40,
                           "set": "c" * 40, "store": str(self.store),
                           "runtime": {"kind": "cleanroom", "version": VERSION, "java": 26, "jdk": BUILD,
                                       "files": self.runtime},
                           "members": members, "jvm": {"client": ["-Ddraw.mode=own"], "server": []},
                           "collect": [], "failures": [], "params": {"seed": 8675309},
                           # the base as lab.resolve records it: the base project's table, with the fake installer
                           "base": {"name": "test:standalone", "project": "test", "commit": "d" * 40,
                                    "chosen": "line", "source": "project.toml",
                                    "definition": json.loads(json.dumps(STANDALONE)), "overlay": None}}
        self.launch = self.write_launch(self.launch_doc)
        self.config = {"instances": str(self.root / "instances"), "runtime_cache": str(self.cache),
                       "runtime_hosts": ["example.com"], "metadata": ""}
        self.lab = {"trusted": ["game"]}  # [lab] of the machine: the launch's project may run its hook

    def keep(self, name: str, data: bytes) -> Path:
        path = self.store / "files" / sha256(data) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def write_launch(self, doc: dict, name: str = "default.json") -> Path:
        path = self.root / "launch" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
        return path

    def run_build(self, name: str = "one", launch: Path | None = None, **params):
        ctx = Context({"job": "direct", "command": "lab.build", "module": "lab", "work": str(self.root / "work"),
                       "out": str(self.root / "out" / name), "module_config": self.config, "lab": self.lab},
                      lambda event: None)
        result = instance.build(ctx, str(launch or self.launch), name, **params)
        return ctx, result

    def receipt(self, name: str = "one") -> dict:
        return json.loads((self.root / "instances" / name / "instance.json").read_text(encoding="utf-8"))


class Build(Fixture):
    # -- content ------------------------------------------------------------------------------------------------------

    def test_members_are_placed_by_side_under_their_place_or_file(self):
        ctx, result = self.run_build(eula=True)
        self.assertEqual(ctx.outcome, "ok", ctx.findings)
        folder = Path(result["instance"])
        self.assertEqual(sorted(p.name for p in (folder / "server" / "mods").iterdir()), ["cube-1.jar"])
        self.assertEqual(sorted(p.name for p in (folder / "client" / "mods").iterdir()), ["+Draw.jar", "cube-1.jar"])
        self.assertEqual((folder / "client" / "mods" / "+Draw.jar").read_bytes(), self.client_mod)
        properties = (folder / "server" / "server.properties").read_text(encoding="utf-8")
        self.assertIn(f"server-port={instance.DEFAULT_PORT}", properties)  # a session writes its own
        self.assertIn("level-seed=8675309", properties)
        self.assertEqual((folder / "server" / "eula.txt").read_text(encoding="utf-8"), "eula=true\n")
        self.assertIn("-XX:+UseZGC", (folder / "server" / "user_jvm_args.txt").read_text(encoding="utf-8"))
        self.assertIn("bb7aa29d-9f86-369b-96db-56e37e2d2a30", (folder / "server" / "ops.json").read_text())
        self.assertEqual((folder / "client" / "natives" / "game.dll").read_bytes(), b"a native")
        self.assertEqual((folder / "launch.json").read_bytes(), self.launch.read_bytes())

    def test_a_member_whose_store_file_changed_is_refused_before_anything_is_placed(self):
        (self.store / self.launch_doc["members"][1]["path"]).write_bytes(b"other bytes")
        with self.assertRaises(ParamError) as raised:
            self.run_build()
        self.assertIn("draw:mod@client", str(raised.exception))
        self.assertIn("not the launch's", str(raised.exception))
        self.assertFalse((self.root / "instances" / "one").exists())
        self.assertEqual(list((self.root / "instances" / ".store" / "staging").iterdir()), [])

    def test_two_members_placed_under_one_name_are_refused(self):
        doc = json.loads(json.dumps(self.launch_doc))
        doc["members"][1]["place"] = "cube-1.jar"
        with self.assertRaisesRegex(ParamError, "would both be client/mods/cube-1.jar"):
            self.run_build(launch=self.write_launch(doc, "clash.json"))

    # -- the receipt ----------------------------------------------------------------------------------------------------

    def test_the_receipt_names_every_file_by_sha256_under_its_root(self):
        self.run_build()
        receipt = self.receipt()
        text = json.dumps(receipt)
        self.assertNotIn(str(self.root), text)  # no absolute path anywhere
        self.assertNotIn(str(self.root).replace("\\", "/"), text)
        self.assertEqual(receipt["schema"], instance.SCHEMA)
        self.assertEqual(receipt["launch"]["sha256"], sha256(self.launch.read_bytes()))
        self.assertEqual(receipt["name"], "one")
        files = receipt["files"]
        self.assertTrue(all(k.startswith(("{instance}/", "{cache}/", "{jdk:26}/")) for k in files), sorted(files))
        self.assertEqual(files["{instance}/server/mods/cube-1.jar"]["sha256"], sha256(self.mod))
        self.assertEqual(files["{instance}/client/mods/+Draw.jar"]["sha256"], sha256(self.client_mod))
        client_jar = "{cache}/libraries/" + assemble.maven_path("com.mojang:minecraft:1.12.2:client")
        self.assertEqual((files[client_jar]["sha256"], files[client_jar]["sha1"]), (sha256(CLIENT), sha1(CLIENT)))
        self.assertIn("{jdk:26}/release", files)
        universal = f"{{instance}}/server/libraries/com/cleanroommc/cleanroom/{VERSION}/cleanroom-{VERSION}-universal.jar"
        self.assertEqual(files[universal]["sha256"], sha256(UNIVERSAL))
        self.assertNotIn("{instance}/instance.json", files)
        self.assertEqual(receipt["members"][1]["placed"], ["{instance}/client/mods/+Draw.jar"])
        self.assertEqual(receipt["runtime"]["files"]["universal"]["sha256"], sha256(UNIVERSAL))
        # every runtime file is kept in the Store, the universal from the installer's output
        self.assertTrue((self.store / "files" / sha256(UNIVERSAL) / f"cleanroom-{VERSION}-universal.jar").is_file())

    def test_the_command_lines_use_the_placeholders(self):
        self.run_build()
        receipt = self.receipt()
        java = "{jdk:26}/bin/" + ("java.exe" if self.system == "windows" else "java")
        server, client = receipt["server"], receipt["client"]
        self.assertEqual((server["cwd"], client["cwd"]), ("{instance}/server", "{instance}/client"))
        self.assertEqual(server["command"][:5], [java, "-Xms8192M", "-Xmx8G", "-XX:+UseZGC",
                                                 "-Djava.io.tmpdir={instance}/server/java-tmp"])
        self.assertIn(f"@{{instance}}/server/libraries/com/cleanroommc/cleanroom/{VERSION}/"
                      + ("win_args.txt" if self.system == "windows" else "unix_args.txt"), server["command"])
        command = client["command"]
        self.assertEqual(command[:2], [java, "-Ddraw.mode=own"])  # the launch's JVM arguments come first
        self.assertEqual((client["jvm_at"], server["jvm_at"]), (2, 1))  # where a session's own go
        classpath = command[command.index("-cp") + 1].split(";" if self.system == "windows" else ":")
        self.assertEqual(classpath, ["{cache}/libraries/" + assemble.maven_path("com.mojang:minecraft:1.12.2:client"),
                                     "{cache}/libraries/" + assemble.maven_path(f"org.lwjgl:lwjgl:3.0:natives-{self.system}"),
                                     "{cache}/libraries/" + assemble.maven_path("org.example:lib:1.0")])
        self.assertEqual(command[command.index("-cp") + 2:command.index("-cp") + 5],
                         ["top.example.Boot", "--tweakClass", "net.example.Tweaker"])
        self.assertIn("-Djava.library.path={instance}/client/natives", command)
        self.assertEqual(command[command.index("--assetsDir") + 1], "{cache}/assets")
        self.assertEqual(command[-2:], ["--port", "{port}"])  # the session's port, never the template's
        self.assertEqual(receipt["session"]["placeholders"], ["port"])
        roots = {"instance": "/i", "cache": "/c", "jdk:26": "/j"}
        self.assertEqual(instance.expand(command, roots)[0], "/j/bin/" + Path(java).name)

    # -- templates ------------------------------------------------------------------------------------------------------

    def test_a_second_instance_reuses_the_template_by_hard_links(self):
        _, first = self.run_build("one")
        _, second = self.run_build("two")
        self.assertEqual((first["built"], second["built"]), (True, False))
        self.assertEqual(first["template"], second["template"])
        templates = [p for p in (self.root / "instances" / ".store" / "templates").iterdir()]
        self.assertEqual(len(templates), 1)
        one, two = self.root / "instances" / "one", self.root / "instances" / "two"
        self.assertTrue(os.path.samefile(one / "server" / "mods" / "cube-1.jar", two / "server" / "mods" / "cube-1.jar"))
        self.assertFalse(os.path.samefile(one / "server" / "server.properties", two / "server" / "server.properties"))
        self.assertEqual(self.receipt("two")["name"], "two")
        _, again = self.run_build("one")
        self.assertTrue(again["reused"])

    def test_other_params_are_another_template_and_are_not_laid_over_an_instance(self):
        _, first = self.run_build("one")
        with self.assertRaisesRegex(ParamError, "fresh=true replaces it"):
            self.run_build("one", params={"seed": 42})
        _, replaced = self.run_build("one", params={"seed": 42}, fresh=True)
        self.assertNotEqual(first["template"], replaced["template"])
        self.assertIn("level-seed=42", (self.root / "instances" / "one" / "server" / "server.properties").read_text())

    def test_the_eula_comes_from_the_machine(self):
        _, plain = self.run_build("plain")
        self.assertFalse((self.root / "instances" / "plain" / "server" / "eula.txt").exists())
        self.lab["eula"] = True
        _, accepted = self.run_build("accepted")
        self.assertTrue((self.root / "instances" / "accepted" / "server" / "eula.txt").is_file())
        self.assertNotEqual(plain["template"], accepted["template"])

    def test_a_configure_hook_of_a_project_not_trusted_is_refused(self):
        hook = self.root / "configure.py"
        hook.write_text("raise SystemExit('must never load')\n", encoding="utf-8")
        self.lab["trusted"] = ["other"]
        with self.assertRaises(ParamError) as raised:
            self.run_build("hooked", configure=str(hook))
        self.assertIn("not in [lab] trusted", str(raised.exception))
        self.assertIn("game", str(raised.exception))
        self.assertFalse((self.root / "instances").exists())  # refused before anything was made

    def test_a_folder_lab_build_did_not_make_is_left_alone(self):
        (self.root / "instances" / "mine").mkdir(parents=True)
        with self.assertRaisesRegex(ParamError, "left alone"):
            self.run_build("mine")

    def test_a_configure_hook_runs_after_the_base_and_is_recorded(self):
        hook = self.root / "configure.py"
        hook.write_text("def configure(instance, launch, params, placed):\n"
                        "    path = instance / 'server' / 'server.properties'\n"
                        "    path.write_text(path.read_text().replace('level-type=DEFAULT', 'level-type=game'))\n"
                        "    return {'scale': params.get('scale', 1), 'placed': sorted(placed)}\n", encoding="utf-8")
        _, plain = self.run_build("plain")
        _, hooked = self.run_build("hooked", configure=str(hook), params={"scale": 2})
        self.assertNotEqual(plain["template"], hooked["template"])
        receipt = self.receipt("hooked")
        self.assertEqual(receipt["configure"]["record"], {"scale": 2, "placed": ["cube:mod@both", "draw:mod@client"]})
        self.assertEqual(receipt["configure"]["sha256"], sha256(hook.read_bytes()))
        self.assertIn("level-type=game", (self.root / "instances" / "hooked" / "server" / "server.properties").read_text())

    # -- runtime files --------------------------------------------------------------------------------------------------

    def test_a_launch_without_its_runtime_files_is_refused_with_the_fix(self):
        doc = json.loads(json.dumps(self.launch_doc))
        del doc["runtime"]["files"]
        with self.assertRaises(ParamError) as raised:
            self.run_build(launch=self.write_launch(doc, "old.json"))
        self.assertIn(f"wb land platform.cleanroom={VERSION}", str(raised.exception))
        self.assertIn("wb run lab.resolve", str(raised.exception))
        self.assertFalse((self.root / "instances").exists())

    def test_a_runtime_file_comes_from_a_candidate_folder_into_the_store(self):
        kept = self.store / "files" / self.runtime["prism"]["sha256"] / self.runtime["prism"]["file"]
        candidates = self.root / "downloads"
        candidates.mkdir()
        shutil.move(kept, candidates / kept.name)
        self.config["runtime_candidates"] = [str(candidates)]
        ctx, _ = self.run_build()
        self.assertEqual(ctx.outcome, "ok", ctx.findings)
        self.assertTrue(kept.is_file())

    def test_a_runtime_file_on_a_host_not_allowed_is_refused(self):
        kept = self.store / "files" / self.runtime["prism"]["sha256"] / self.runtime["prism"]["file"]
        kept.unlink()
        with self.assertRaisesRegex(ParamError, "outside the allowed hosts"):
            self.run_build()

    def test_a_universal_other_than_the_launchs_is_refused(self):
        doc = json.loads(json.dumps(self.launch_doc))
        doc["runtime"]["files"]["universal"]["sha256"] = "0" * 64
        with self.assertRaisesRegex(EnvironmentProblem, "not the launch's 000000000000"):
            self.run_build(launch=self.write_launch(doc, "universal.json"))

    def test_a_jdk_the_store_lacks_names_the_fetch(self):
        doc = json.loads(json.dumps(self.launch_doc))
        doc["runtime"]["jdk"] = "26.0.9+9"
        with self.assertRaisesRegex(EnvironmentProblem, "wb run store.platform"):
            self.run_build(launch=self.write_launch(doc, "jdk.json"))


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c",
                           "core.autocrlf=false", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


class ProjectBase(Fixture):
    """A launch whose base is a project's [launch.<name>] with an overlay folder, and whose product names its own
    configure script: both exported from the repositories by the ids the launch file names."""

    def setUp(self):
        super().setUp()
        self.repo = Path(tempfile.mkdtemp(prefix="wb-base-repo-"))
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)
        git(self.repo, "init", "-q", "-b", "main")
        overlay = self.repo / "launch" / "standalone"
        for relative, text in (("both/config/cubicchunks.cfg", "general {\n    I:defaultMinHeight=-128\n}\n"),
                               ("server/server.properties",
                                "server-port={port}\nlevel-seed={seed}\nview-distance=12\n"),
                               ("server/ops.json", '[{"uuid": "{player_uuid}", "name": "{player}"}]\n'),
                               ("client/options.txt", "renderDistance:12\n")):
            (overlay / relative).parent.mkdir(parents=True, exist_ok=True)
            (overlay / relative).write_bytes(text.encode("utf-8"))
        (overlay / "client" / "icon.bin").write_bytes(b"\xff\xfe\x00raw")
        (self.repo / "launch" / "configure.py").write_bytes(
            b"def configure(instance, launch, params, placed):\n"
            b"    path = instance / 'server' / 'server.properties'\n"
            b"    path.write_text(path.read_text() + 'level-type=game\\n')\n"
            b"    return {'scale': params['scale']}\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "standalone")
        self.commit = git(self.repo, "rev-parse", "HEAD")
        definition = {k: v for k, v in json.loads(json.dumps(STANDALONE)).items()
                      if k != "files"}
        definition.update(overlay="standalone", player="Lab", ready="Ready now")
        self.launch_doc["base"] = {"name": "game:standalone", "project": "game", "commit": self.commit,
                                   "source": "project.toml", "definition": definition,
                                   "overlay": {"path": "launch/standalone",
                                               "tree": git(self.repo, "rev-parse", "HEAD:launch/standalone")}}
        self.launch_doc["configure"] = {"project": "game", "commit": self.commit, "path": "launch/configure.py",
                                        "blob": git(self.repo, "rev-parse", "HEAD:launch/configure.py")}
        self.launch_doc["params"] = {"seed": 8675309, "scale": 1.0}
        self.launch_doc["param_types"] = {"seed": {"type": "int"}, "scale": {"type": "float", "min": 0.5, "max": 8}}
        self.launch = self.write_launch(self.launch_doc, "based.json")
        patch = mock.patch.object(instance.Machine, "repository", lambda machine, project: self.repo)
        patch.start()
        self.addCleanup(patch.stop)

    def test_the_base_and_its_overlay_come_from_the_launch_file(self):
        ctx, result = self.run_build()
        self.assertEqual(ctx.outcome, "ok", ctx.findings)
        folder = Path(result["instance"])
        server = (folder / "server" / "server.properties").read_bytes()
        newline = os.linesep.encode()
        self.assertEqual(server, b"server-port=25565" + newline + b"level-seed=8675309" + newline + b"view-distance=12"
                         + newline + b"level-type=game" + newline)  # filled, the platform's line ends, then configured
        self.assertIn(instance.offline_uuid("Lab"), (folder / "server" / "ops.json").read_text())
        for side in ("server", "client"):  # both/ goes to each side
            self.assertTrue((folder / side / "config" / "cubicchunks.cfg").is_file())
        self.assertFalse((folder / "server" / "options.txt").exists())
        self.assertEqual((folder / "client" / "icon.bin").read_bytes(), b"\xff\xfe\x00raw")  # not text: as it is
        receipt = self.receipt()
        self.assertEqual(receipt["base"], "game:standalone")
        self.assertEqual(receipt["base_source"], {"name": "game:standalone", "project": "game", "commit": self.commit,
                                                  "source": "project.toml",
                                                  "overlay": self.launch_doc["base"]["overlay"]["tree"]})
        self.assertEqual((receipt["session"]["views"], receipt["session"]["ready"]),
                         (self.launch_doc["base"]["definition"]["views"], "Ready now"))
        self.assertEqual(receipt["player"]["name"], "Lab")
        self.assertEqual(receipt["configure"]["path"], "launch/configure.py")
        self.assertEqual(receipt["configure"]["record"], {"scale": 1.0})

    def test_an_inheriting_launch_lays_each_launchs_overlay_and_trusts_the_scripts_project(self):
        for name, text in (("inherited", "from=host\nhost=yes\n"), ("own", "from=game\n")):
            (self.repo / "launch" / name / "server").mkdir(parents=True)
            (self.repo / "launch" / name / "server" / "extra.txt").write_text(text, encoding="utf-8")
        (self.repo / "launch" / "inherited" / "server" / "host.txt").write_text("host\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "launch overlays")
        doc = json.loads(json.dumps(self.launch_doc))
        doc["overlays"] = [{"project": project, "commit": self.commit, "path": f"launch/{name}",
                            "tree": git(self.repo, "rev-parse", f"HEAD:launch/{name}")}
                           for project, name in (("host", "inherited"), ("game", "own"))]
        doc["overlay"] = doc["overlays"][1]
        doc["configure"]["project"] = "host"  # the base launch's script, inherited
        doc["inherits"] = [{"name": "host:default", "project": "host", "launch": "default", "commit": self.commit,
                            "tree": "d" * 40, "chosen": "branch", "ref": "refs/heads/main"}]
        launch = self.write_launch(doc, "inherited.json")
        with self.assertRaisesRegex(ParamError, r"configure script launch/configure.py is host's code"):
            self.run_build(launch=launch)
        self.lab["trusted"] = ["host"]
        ctx, result = self.run_build(launch=launch)
        self.assertEqual(ctx.outcome, "ok", ctx.findings)
        server = Path(result["instance"]) / "server"
        self.assertEqual((server / "extra.txt").read_text(encoding="utf-8").splitlines(), ["from=game"])
        self.assertTrue((server / "host.txt").is_file())
        self.assertTrue((server / "ops.json").is_file())  # the base table's overlay is still laid first

    def test_a_bundle_carries_the_store_and_the_trees_by_id(self):
        carried = self.root / "bundle"
        shutil.copytree(self.store, carried / "store")
        sources = carried / "sources"
        overlay = self.launch_doc["base"]["overlay"]["tree"]
        shutil.copytree(self.repo / "launch" / "standalone", sources / overlay)
        script = self.launch_doc["configure"]["blob"]
        (sources / script).mkdir()
        shutil.copyfile(self.repo / "launch" / "configure.py", sources / script / "configure.py")
        doc = json.loads(json.dumps(self.launch_doc))
        doc["store"] = str(self.root / "a-store-this-machine-does-not-have")
        launch = self.write_launch(doc, "carried.json")

        def no_repository(machine, project):
            raise AssertionError(f"a bundle's build read {project}'s repository")

        with mock.patch.object(instance.Machine, "repository", no_repository):
            ctx, result = self.run_build(launch=launch, store=str(carried / "store"), sources=str(sources))
            self.assertEqual(ctx.outcome, "ok", ctx.findings)
            folder = Path(result["instance"])
            self.assertIn(b"level-type=game", (folder / "server" / "server.properties").read_bytes())
            receipt = self.receipt()
            self.assertEqual(receipt["launch"]["sha256"], hashlib.sha256(launch.read_bytes()).hexdigest())
            shutil.rmtree(sources / script)
            with self.assertRaisesRegex(ParamError, rf"sources: .* has no {script[:12]}/configure.py"):
                self.run_build("two", launch=launch, store=str(carried / "store"), sources=str(sources))

    def test_the_launchs_configure_script_needs_a_trusted_project(self):
        self.lab["trusted"] = []
        with self.assertRaisesRegex(ParamError, r"configure script launch/configure.py is game's code"):
            self.run_build()
        self.assertFalse((self.root / "instances").exists())

    def test_params_are_checked_against_the_launchs_types(self):
        with self.assertRaisesRegex(ParamError, r"the launch declares no param seeds \(it declares: scale, seed\)"):
            self.run_build(params={"seeds": 1})
        with self.assertRaisesRegex(ParamError, r"param seed is int, not 'x'"):
            self.run_build(params={"seed": "x"})
        with self.assertRaisesRegex(ParamError, r"param scale is 0.5..8, not 9.0"):
            self.run_build(params={"scale": 9})
        ctx, _ = self.run_build(params={"scale": 2})  # an int is a float too
        self.assertEqual(self.receipt()["configure"]["record"], {"scale": 2.0})

    def test_a_launch_file_without_a_base_is_refused_with_the_fix(self):
        doc = json.loads(json.dumps(self.launch_doc))
        for key in ("base", "configure", "param_types"):
            del doc[key]
        with self.assertRaisesRegex(ParamError, r"names no base .* resolve it again"):
            self.run_build(name="old", launch=self.write_launch(doc, "old.json"))
        self.assertFalse((self.root / "instances" / "old").exists())

if __name__ == "__main__":
    unittest.main()
