"""lab.resolve: a launch resolved once into launch.json. Members come by SHA-256 from the set's lines, holds and third
entries and, for the product itself, from the Store by its tree; a member's own [launch.member] adds its switches; a
launch that would load two builds of one mod id, or a member the set or Store lacks, is refused with what to do."""

import io
import json
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

from wb_core import artifacts  # noqa: E402
from wb_core.module import Context, ParamError  # noqa: E402
from wb_lab import resolve as launch  # noqa: E402

COMMIT = "a" * 40
TREE = "b" * 40


def jar_bytes(*mod_ids: str, extra: bytes = b"") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        if mod_ids:
            archive.writestr("mcmod.info", json.dumps([{"modid": m, "name": m} for m in mod_ids]))
        archive.writestr("payload.bin", extra or "-".join(mod_ids).encode())
    return buffer.getvalue()


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args], cwd=cwd,
                          check=True, capture_output=True, text=True).stdout.strip()


class Base(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.base = Path(self.folder.name)
        self.store = self.base / "store"
        self.digests = {}
        for name, data in {"game-1.0.jar": jar_bytes("game"), "cube-1.jar": jar_bytes("cubic"),
                           "lib-1.jar": jar_bytes("lib"), "lib-adapter.jar": jar_bytes("lib_adapter"),
                           "lib-adapter-held.jar": jar_bytes("lib_adapter", extra=b"held"),
                           "draw-1.jar": jar_bytes("draw"), "ui-3.jar": jar_bytes("ui"),
                           "+helper-0.2.jar": jar_bytes()}.items():
            source = self.base / name
            source.write_bytes(data)
            digest = artifacts.sha256(source)
            artifacts.put(self.store, source, digest)
            self.digests[name] = digest
        d = self.digests
        artifacts.record(self.store, "game", TREE, COMMIT, {"game-1.0.jar": {
            "sha256": d["game-1.0.jar"], "job": "build", "run": "r", "from": "build/libs/game-1.0.jar"}})
        self.set_doc = {
            "schema": 1,
            "projects": {
                "cube": {"commit": "c" * 40, "artifacts": {"mod": {"sha256": d["cube-1.jar"], "file": "cube-1.jar"}}},
                "lib": {"commit": "d" * 40, "artifacts": {
                    "mod": {"sha256": d["lib-1.jar"], "file": "lib-1.jar"},
                    "adapter": {"sha256": d["lib-adapter.jar"], "file": "lib-adapter.jar"}}},
                "draw": {"commit": "e" * 40, "artifacts": {"mod": {"sha256": d["draw-1.jar"], "file": "draw-1.jar"}}},
            },
            "third": {
                "ui": {"module": "org.example:ui:3", "files": {"runtime": d["ui-3.jar"]}, "licence": "LGPL-3.0",
                       "mod_id": "ui"},
                "helper": {"module": "org.example:helper:0.2", "files": {"runtime": d["+helper-0.2.jar"]},
                           "licence": "MIT", "mod_id": "helper"},
            },
            "holds": {"lib:adapter": {"commit": "f" * 40, "reason": "qualified copy", "artifacts": {
                "adapter": {"sha256": d["lib-adapter-held.jar"], "file": "lib-adapter-held.jar"}}}},
            "platform": {"java": 26, "jdk": {"26": {"build": "26.0.2.1+1"}}, "cleanroom": {"version": "0.7.14-alpha"}},
        }
        self.table = {"members": ["game:mod@both", "cube:mod@both", "lib:mod@both", "lib:adapter@both",
                                  "draw:mod@client", "ui@both", "helper@server"],
                      "params": {"seed": 8675309}}

    def tearDown(self):
        self.folder.cleanup()

    def run_resolve(self, table=None, set_doc=None, index="found", extras=None, objects=None, warn=None):
        index = artifacts.find(self.store, "game", TREE) if index == "found" else index
        return launch.resolve(product="game", commit=COMMIT, tree=TREE, launch="default",
                              table=launch.definition({"launch": {"default": table or self.table}}, "default"),
                              set_doc=set_doc or self.set_doc, revision="9" * 40, store=self.store,
                              product_artifacts={"mod": {"check": "build", "file": "build/libs/game-*.jar"}},
                              product_index=index, extras=extras, objects=objects, warn=warn)


class Members(Base):
    def test_each_member_kind_by_sha256_with_its_mod_ids(self):
        doc = self.run_resolve()
        by = {m["member"]: m for m in doc["members"]}
        d = self.digests
        self.assertEqual([m["member"] for m in doc["members"]], self.table["members"])
        self.assertEqual((by["game:mod@both"]["kind"], by["game:mod@both"]["sha256"], by["game:mod@both"]["tree"]),
                         ("product", d["game-1.0.jar"], TREE))
        self.assertEqual((by["cube:mod@both"]["kind"], by["cube:mod@both"]["commit"]), ("line", "c" * 40))
        self.assertEqual(by["draw:mod@client"]["side"], "client")
        self.assertEqual((by["ui@both"]["kind"], by["ui@both"]["artifact"], by["ui@both"]["licence"]),
                         ("third", "runtime", "LGPL-3.0"))
        self.assertEqual(by["helper@server"]["file"], "+helper-0.2.jar")  # a third's file keeps the Store's name
        self.assertEqual(by["helper@server"]["mod_ids"], ["helper"])      # declared, though its JAR has no mcmod.info
        self.assertEqual(by["cube:mod@both"]["mod_ids"], ["cubic"])
        self.assertEqual(by["cube:mod@both"]["path"], f"files/{d['cube-1.jar']}/cube-1.jar")
        self.assertEqual(doc["runtime"], {"kind": "cleanroom", "version": "0.7.14-alpha", "java": 26,
                                          "jdk": "26.0.2.1+1"})
        self.assertEqual((doc["set"], doc["params"], doc["schema"]), ("9" * 40, {"seed": 8675309}, 1))

    def test_the_platforms_runtime_files_go_into_the_launch(self):
        base = "https://example.org/cleanroom/0.7.14-alpha/cleanroom-0.7.14-alpha"
        files = {"installer": {"url": base + "-installer.jar", "file": "cleanroom-0.7.14-alpha-installer.jar",
                               "sha256": "1" * 64},
                 "universal": {"url": base + "-universal.jar", "file": "cleanroom-0.7.14-alpha-universal.jar",
                               "sha256": "2" * 64},
                 "prism": {"url": base + ".zip", "file": "cleanroom-0.7.14-alpha.zip", "sha256": "3" * 64}}
        set_doc = {**self.set_doc, "platform": {**self.set_doc["platform"],
                                                "cleanroom": {"version": "0.7.14-alpha", **files}}}
        doc = self.run_resolve(set_doc=set_doc)
        self.assertEqual(doc["runtime"]["files"], files)
        self.assertNotIn("files", self.run_resolve()["runtime"])  # a platform without them: none in the launch

    def test_place_names_the_file_a_member_is_placed_under(self):
        table = {**self.table, "place": {"helper@server": "+Helper-0.2.jar"}}
        by = {m["member"]: m for m in self.run_resolve(table=table)["members"]}
        self.assertEqual((by["helper@server"]["place"], by["helper@server"]["file"]),
                         ("+Helper-0.2.jar", "+helper-0.2.jar"))
        self.assertNotIn("place", by["cube:mod@both"])

    def test_a_third_file_without_a_coordinate_is_its_runtime_file(self):
        doc = json.loads(json.dumps(self.set_doc))
        doc["third"]["helper"] = {"file": "+helper-0.2.jar", "sha256": self.digests["+helper-0.2.jar"],
                                  "source": "https://example.org/helper", "licence": "MIT", "mod_id": "helper"}
        member = {m["member"]: m for m in self.run_resolve(set_doc=doc)["members"]}["helper@server"]
        self.assertEqual((member["kind"], member["artifact"], member["file"], member["source"], member["mod_ids"]),
                         ("third", "runtime", "+helper-0.2.jar", "https://example.org/helper", ["helper"]))
        with self.assertRaisesRegex(launch.LaunchError, r"third entry helper has no dev file \(it has: runtime\)"):
            self.run_resolve(table={"members": ["helper:dev@both"]}, set_doc=doc)

    def test_a_hold_wins_over_the_line(self):
        member = {m["member"]: m for m in self.run_resolve()["members"]}["lib:adapter@both"]
        self.assertEqual((member["kind"], member["sha256"], member["commit"], member["reason"]),
                         ("hold", self.digests["lib-adapter-held.jar"], "f" * 40, "qualified copy"))

    def test_two_builds_of_one_mod_id_are_refused(self):
        doc = json.loads(json.dumps(self.set_doc))
        doc["holds"] = {}  # the line's adapter and a second copy under another name
        doc["projects"]["cube"]["artifacts"]["adapter"] = doc["projects"]["lib"]["artifacts"]["adapter"]
        table = {"members": ["lib:adapter@both", "cube:adapter@both"]}
        with self.assertRaisesRegex(launch.LaunchError, r"two builds of one mod id: lib_adapter from "
                                                        r"lib:adapter@both, cube:adapter@both"):
            self.run_resolve(table=table, set_doc=doc)

    def test_a_product_tree_ci_has_not_built_is_refused_with_the_fix(self):
        with self.assertRaisesRegex(launch.LaunchError, r"CI has not built game tree bbbbbbbbbbbb.*run wb check") as raised:
            self.run_resolve(index=None)
        self.assertEqual(raised.exception.missing[0], "game")  # what wb play builds first

    def test_the_tour_rules_and_drivers_are_carried_into_the_launch_file(self):
        table = {"members": ["game:mod@both"], "tour": {"captured": "view captured", "client_jvm": ["-Dgame.tour=1"]},
                 "drivers": {"pipeline": {"command": ["{python}", "{root}/tools/pipeline.py"]}}}
        doc = self.run_resolve(table=table)
        self.assertEqual(doc["tour"]["captured"], "view captured")
        self.assertEqual(doc["drivers"]["pipeline"]["command"][0], "{python}")

    def test_a_priming_start_is_carried_into_the_launch_file(self):
        self.assertNotIn("prime", self.run_resolve(table={"members": ["game:mod@both"]}))
        doc = self.run_resolve(table={"members": ["game:mod@both"], "prime": True})
        self.assertEqual(doc["prime"], {"commands": [], "seconds": 0})
        doc = self.run_resolve(table={"members": ["game:mod@both"],
                                      "prime": {"commands": ["forceload add 0 0"], "seconds": 30, "timeout": 600}})
        self.assertEqual(doc["prime"], {"commands": ["forceload add 0 0"], "seconds": 30, "timeout": 600})
        self.assertNotIn("prime", self.run_resolve(table={"members": ["game:mod@both"], "prime": False}))
        for prime, pattern in (("yes", "prime is true, false or a table"), ({"ticks": 5}, "unknown key prime.ticks"),
                               ({"commands": "stop"}, "a list of console lines"), ({"seconds": -1}, "0 or more")):
            with self.subTest(prime=prime), self.assertRaisesRegex(launch.LaunchError, pattern):
                launch.definition({"launch": {"default": {"members": ["a:mod@both"], "prime": prime}}}, "default")

    def test_a_file_the_store_lacks_names_the_check_that_makes_it(self):
        doc = json.loads(json.dumps(self.set_doc))
        doc["projects"]["cube"]["artifacts"]["mod"]["sha256"] = "0" * 64
        with self.assertRaisesRegex(launch.LaunchError, r"cube:mod@both: the Store has no cube-1.jar.*wb check in "
                                                        r"cube at cccccccccccc") as raised:
            self.run_resolve(set_doc=doc)
        self.assertEqual(raised.exception.missing, ("cube", "c" * 40))

    def test_an_unknown_member_or_artifact_is_refused(self):
        with self.assertRaisesRegex(launch.LaunchError, r"names no project or third entry nope"):
            self.run_resolve(table={"members": ["nope:mod@both"]})
        with self.assertRaisesRegex(launch.LaunchError, r"cube's line names no artifact sources \(it has: mod\)"):
            self.run_resolve(table={"members": ["cube:sources@both"]})
        with self.assertRaisesRegex(launch.LaunchError, r"a project member names its artifact \(cube:mod\)"):
            self.run_resolve(table={"members": ["cube@both"]})


class Definition(unittest.TestCase):
    def test_the_table_is_checked(self):
        for table, pattern in [({"members": ["a:mod@both"], "extra": 1}, "unknown key launch.default.extra"),
                               ({"members": []}, "lists the launch's members"),
                               ({"members": ["a:mod@everywhere"]}, "is not <project>:<artifact>@<side>"),
                               ({"members": ["a:mod@both", "a:mod@both"]}, "names a member twice"),
                               ({"members": ["a:mod@both"], "runtime": "forge"}, "runtime is one of cleanroom"),
                               ({"members": ["a:mod@both"], "jvm": {"client": "-Dx"}}, "list of JVM arguments"),
                               ({"members": ["a:mod@both"], "place": {"b:mod@both": "b.jar"}}, "which is not a member"),
                               ({"members": ["a:mod@both"], "place": {"a:mod@both": "mods/a.jar"}}, "is a JAR file name"),
                               ({"members": ["a:mod@both"], "place": {"a:mod@both": "a\\b.jar"}}, "is a JAR file name")]:
            with self.subTest(pattern=pattern), self.assertRaisesRegex(launch.LaunchError, pattern):
                launch.definition({"launch": {"default": table}}, "default")
        with self.assertRaisesRegex(launch.LaunchError, r"no \[launch.capture\] \(defined: default\)"):
            launch.definition({"launch": {"default": {"members": ["a:mod@both"]}, "member": {}}}, "capture")


class Extras(Base):
    def test_a_members_own_switches_join_the_products_per_side(self):
        tomls = {"draw": {"launch": {"member": {"jvm": {"client": ["-Ddraw.distant=own"]},
                                                "collect": [["client/draw.txt", "draw.txt"]],
                                                "failures": ["draw refused: .*"]}}},
                 "lib": {"launch": {"member": {"jvm": {"both": ["-Dlib.strict=true"]}}}}}
        seen = []

        def extras(project, commit):
            seen.append((project, commit))
            return tomls.get(project)

        table = {**self.table, "jvm": {"client": ["-Dgame.tour=true"]}}
        doc = self.run_resolve(table=table, extras=extras)
        self.assertEqual(doc["jvm"], {"client": ["-Dlib.strict=true", "-Ddraw.distant=own", "-Dgame.tour=true"],
                                      "server": ["-Dlib.strict=true"]})
        self.assertEqual(doc["collect"], [["client/draw.txt", "draw.txt"]])
        self.assertEqual(doc["failures"], ["draw refused: .*"])
        self.assertIn(("lib", "f" * 40), seen)      # a hold's member reads its project.toml at the hold's commit
        self.assertNotIn("game", [p for p, _ in seen])  # the product's own table is the definition itself

    def test_an_unknown_member_key_is_refused(self):
        with self.assertRaisesRegex(launch.LaunchError, r"unknown key launch.member.mods"):
            self.run_resolve(extras=lambda project, commit: {"launch": {"member": {"mods": []}}})


class Keys(unittest.TestCase):
    def test_the_new_keys_are_checked_and_unknown_ones_refused(self):
        member = {"members": ["a:mod@both"]}
        for extra, pattern in [({"base": "cubature"}, r"base is '<project>:<name>'"),
                               ({"overlay": "../up"}, r"overlay is a folder under launch/"),
                               ({"configure": "tools/configure.py"}, r"configure is a Python file under launch/"),
                               ({"configure": "launch/../x.py"}, r"configure is a Python file under launch/"),
                               ({"params": {"seed": [1]}}, r"a default is a bool, int, float or string"),
                               ({"params": {"scale": {"type": "real"}}}, r"type is one of int, float, bool, string"),
                               ({"params": {"scale": {"type": "float", "default": "x"}}}, r"param scale is float"),
                               ({"params": {"scale": {"type": "float", "default": 9, "max": 8}}}, r"\.\.8, not 9"),
                               ({"params": {"mode": {"type": "string", "choices": ["A"], "default": "B"}}},
                                r"param mode is one of A, not 'B'"),
                               ({"params": {"mode": {"type": "string", "units": "m"}}}, r"unknown key units"),
                               ({"tour": {"captured": 1}}, r"tour.captured is str"),
                               ({"tour": {"capture": "x"}}, r"unknown key tour.capture"),
                               ({"tour": {"setup": "time set 6000"}}, r"tour.setup is a list of strings"),
                               ({"drivers": {"pipeline": {"run": ["x"]}}}, r"unknown key drivers.pipeline.run"),
                               ({"drivers": {"pipeline": {"command": []}}}, r"command is the driver's command line")]:
            with self.subTest(extra=extra), self.assertRaisesRegex(launch.LaunchError, pattern):
                launch.definition({"launch": {"default": {**member, **extra}}}, "default")
        table = {**member, "base": "cube:standalone", "overlay": "default", "configure": "launch/configure.py",
                 "params": {"seed": 1, "scale": {"type": "float", "default": 1, "min": 0.5, "max": 8},
                            "threads": {"type": "int", "min": -1, "max": 16}},
                 "tour": {"captured": "view captured", "spectator": True, "pause": 5},
                 "drivers": {"pipeline": {"command": ["{python}", "{root}/tools/pipeline.py"], "cwd": "{root}"}}}
        self.assertIs(launch.definition({"launch": {"default": table}}, "default"), table)
        self.assertEqual(launch.params_of(table, "launch.default"),
                         ({"seed": 1, "scale": 1.0}, {"seed": {"type": "int"},
                                                      "scale": {"type": "float", "min": 0.5, "max": 8},
                                                      "threads": {"type": "int", "min": -1, "max": 16}}))

    def test_a_base_is_no_launch_and_its_keys_are_checked(self):
        standalone = {"installer": ["{java}"], "server": ["{java}"], "client": ["{java}"], "overlay": "standalone"}
        with self.assertRaisesRegex(launch.LaunchError,
                                    r"\[launch.standalone\] is a base.*base = '<project>:standalone'"):
            launch.definition({"launch": {"standalone": standalone}}, "standalone")
        self.assertIs(launch.base_definition(standalone, "base"), standalone)
        for extra, pattern in [({"members": ["a:mod@both"]}, "it is a launch, not a base"),
                               ({"mods": []}, "unknown key mods"),
                               ({"server": "java"}, "server is a command line"),
                               ({"views": {"vertical": ["cubicchunks.cfg:I:x"]}}, "views is a table"),
                               ({"jvm_profile": {"heap": 8}}, "jvm_profile is a table"),
                               ({"files": {"world": {}}}, "files is a table of both, server and client"),
                               ({"overlay": "/abs"}, "overlay is a folder under launch/")]:
            with self.subTest(extra=extra), self.assertRaisesRegex(launch.LaunchError, pattern):
                launch.base_definition({**standalone, **extra}, "base")

    def test_the_builders_base_is_a_base_and_the_tour_keys_are_lab_runs(self):
        from test_instance import STANDALONE
        from wb_lab import run
        launch.base_definition(STANDALONE, "test:standalone")
        self.assertEqual(set(launch.TOUR_KEYS), set(run.TOUR_DEFAULTS))


class Bases(Base):
    """The base comes from the base project's project.toml at the commit the set names for it, with its overlay's
    tree id; the product's launch/ tree, overlay and configure script at its own commit."""

    def objects(self, found):
        seen = []

        def objects(project, commit, path):
            seen.append((project, commit, path))
            return found.get((project, path))
        return objects, seen

    def test_the_base_is_read_at_the_line_with_its_overlay_tree(self):
        standalone = {"installer": ["{java}"], "server": ["{java}"], "client": ["{java}"], "player": "Lab",
                      "overlay": "standalone", "views": {"horizontal": ["server/server.properties:view-distance"]}}
        read = []

        def extras(project, commit):
            read.append((project, commit))
            return {"launch": {"standalone": standalone, "member": {"failures": ["cube refused: .*"]}}} \
                if project == "cube" else None

        objects, seen = self.objects({("cube", "launch/standalone"): ("tree", "1" * 40),
                                      ("game", "launch"): ("tree", "2" * 40),
                                      ("game", "launch/configure.py"): ("blob", "3" * 40)})
        table = {**self.table, "base": "cube:standalone", "configure": "launch/configure.py"}
        doc = self.run_resolve(table=table, extras=extras, objects=objects)
        self.assertEqual(doc["base"], {"name": "cube:standalone", "project": "cube", "commit": "c" * 40,
                                       "chosen": "line", "source": "project.toml", "definition": standalone,
                                       "overlay": {"path": "launch/standalone", "tree": "1" * 40}})
        self.assertIn(("cube", "c" * 40, "launch/standalone"), seen)
        self.assertEqual(doc["definition"], {"commit": COMMIT, "launch_tree": "2" * 40})
        self.assertEqual(doc["configure"], {"project": "game", "commit": COMMIT, "path": "launch/configure.py",
                                            "blob": "3" * 40})
        self.assertEqual(doc["failures"], ["cube refused: .*"])  # the base project's [launch.member] still joins

    def test_a_hold_for_the_product_moves_the_base_with_it(self):
        set_doc = json.loads(json.dumps(self.set_doc))
        set_doc["holds"]["cube@game"] = {"commit": "7" * 40, "reason": "an older world"}
        standalone = {"installer": ["x"], "server": ["x"], "client": ["x"]}
        doc = self.run_resolve(table={**self.table, "base": "cube:standalone"}, set_doc=set_doc,
                               extras=lambda project, commit: {"launch": {"standalone": standalone}},
                               objects=lambda *a: None)
        self.assertEqual((doc["base"]["commit"], doc["base"]["overlay"]), ("7" * 40, None))

    def test_a_base_project_without_the_table_is_refused(self):
        for name in ("cube:standalone", "cube:other"):
            with self.subTest(base=name), self.assertRaisesRegex(
                    launch.LaunchError, rf"cube at cccccccccccc has no \[launch.{name.split(':')[1]}\], so there is no "
                                        rf"base {name}"):
                self.run_resolve(table={**self.table, "base": name}, extras=lambda p, c: None,
                                 objects=lambda *a: None)

    def test_a_missing_overlay_or_script_is_refused(self):
        standalone = {"installer": ["x"], "server": ["x"], "client": ["x"], "overlay": "standalone"}
        with self.assertRaisesRegex(launch.LaunchError, r"cube has no folder launch/standalone at cccccccccccc"):
            self.run_resolve(table={**self.table, "base": "cube:standalone"},
                             extras=lambda p, c: {"launch": {"standalone": standalone}}, objects=lambda *a: None)
        with self.assertRaisesRegex(launch.LaunchError, r"game has no file launch/configure.py"):
            self.run_resolve(table={**self.table, "configure": "launch/configure.py"}, objects=lambda *a: None)
        with self.assertRaisesRegex(launch.LaunchError, r"\[launch.nope\]\): unknown key mods"):
            self.run_resolve(table={**self.table, "base": "cube:nope"},
                             extras=lambda p, c: {"launch": {"nope": {**standalone, "mods": []}}},
                             objects=lambda *a: ("tree", "1" * 40))


class FakeWorkspace:
    def __init__(self, store, home, projects):
        self._store, self._home, self._projects = store, home, projects

    def store_root(self):
        return self._store

    def set_home(self):
        return self._home

    def projects(self):
        return self._projects


class Command(Base):
    def setUp(self):
        super().setUp()
        self.product = self.base / "game"
        self.product.mkdir()
        git(self.product, "init", "-q", "-b", "main")
        (self.product / "project.toml").write_text(
            'schema = 1\n\n[project]\nname = "game"\n\n[artifacts.mod]\ncheck = "build"\nfile = "build/libs/game-*.jar"\n'
            '\n[checks.build]\nsteps = [["{gradle}", "build"]]\n\n[launch.default]\n'
            'members = ["game:mod@both", "cube:mod@both", "ui@both"]\n', encoding="utf-8")
        git(self.product, "add", "-A")
        git(self.product, "commit", "-q", "-m", "one")
        commit, tree = git(self.product, "rev-parse", "HEAD"), git(self.product, "rev-parse", "HEAD^{tree}")
        artifacts.record(self.store, "game", tree, commit, {"game-1.0.jar": {
            "sha256": self.digests["game-1.0.jar"], "job": "build", "run": "r", "from": "build/libs/game-1.0.jar"}})
        self.home = self.base / "set"
        self.home.mkdir()
        git(self.home, "init", "-q", "-b", "main")
        (self.home / "set.json").write_text(json.dumps(self.set_doc), encoding="utf-8")
        git(self.home, "add", "-A")
        git(self.home, "commit", "-q", "-m", "set")
        self.revision = git(self.home, "rev-parse", "HEAD")
        cube = self.base / "cube"
        cube.mkdir()
        git(cube, "init", "-q", "-b", "main")
        (cube / "README").write_text("cube", encoding="utf-8")
        git(cube, "add", "-A")
        git(cube, "commit", "-q", "-m", "cube")
        self.set_doc["projects"]["cube"]["commit"] = git(cube, "rev-parse", "HEAD")
        (self.home / "set.json").write_text(json.dumps(self.set_doc), encoding="utf-8")
        git(self.home, "commit", "-q", "-am", "cube line")
        self.revision = git(self.home, "rev-parse", "HEAD")
        project = type("P", (), {"repo": cube})
        self.workspace = FakeWorkspace(self.store, self.home, {"cube": project})
        spec = {"job": "j", "command": "lab.resolve", "module": "lab", "params": {}, "module_config": {},
                "machine": "host", "work": str(self.base / "job" / "work"), "out": str(self.base / "job" / "out")}
        self.ctx = Context(spec, lambda event: None)
        self.patches = [mock.patch("wb.workspace.Workspace", lambda: self.workspace),
                        mock.patch("wb.setwork.resolve", lambda workspace, folder, say=None: self.revision)]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in self.patches:
            patch.stop()
        super().tearDown()

    def test_writes_launch_json_in_the_worktree_and_the_job(self):
        result = launch.resolve_command(self.ctx, str(self.product))
        written = self.product / ".local" / "launch" / "default.json"
        doc = json.loads(written.read_text(encoding="utf-8"))
        self.assertTrue(Path(result["launch"]).samefile(written))
        self.assertEqual((doc["project"], doc["set"], doc["commit"]),
                         ("game", self.revision, git(self.product, "rev-parse", "HEAD")))
        self.assertEqual([m["member"] for m in doc["members"]], ["game:mod@both", "cube:mod@both", "ui@both"])
        self.assertEqual(json.loads((self.ctx.out / "launch.json").read_text(encoding="utf-8")), doc)

    def test_a_worktree_with_changes_is_refused(self):
        (self.product / "project.toml").write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(ParamError, "uncommitted changes"):
            launch.resolve_command(self.ctx, str(self.product))

    def test_an_unknown_launch_name_is_refused(self):
        with self.assertRaisesRegex(ParamError, r"no \[launch.capture\]"):
            launch.resolve_command(self.ctx, str(self.product), launch="capture")

    def test_the_base_and_the_products_launch_folder_are_read_from_the_repositories(self):
        cube = self.workspace.projects()["cube"].repo
        (cube / "project.toml").write_text(
            'schema = 1\n\n[project]\nname = "cube"\n\n[launch.standalone]\ninstaller = ["{java}"]\n'
            'server = ["{java}"]\nclient = ["{java}"]\noverlay = "standalone"\n', encoding="utf-8")
        (cube / "launch" / "standalone" / "server").mkdir(parents=True)
        (cube / "launch" / "standalone" / "server" / "server.properties").write_text("level-seed={seed}\n",
                                                                                     encoding="utf-8")
        git(cube, "add", "-A")
        git(cube, "commit", "-q", "-m", "standalone")
        self.set_doc["projects"]["cube"]["commit"] = git(cube, "rev-parse", "HEAD")
        (self.home / "set.json").write_text(json.dumps(self.set_doc), encoding="utf-8")
        git(self.home, "commit", "-q", "-am", "cube's standalone")
        self.revision = git(self.home, "rev-parse", "HEAD")
        text = (self.product / "project.toml").read_text(encoding="utf-8")
        (self.product / "project.toml").write_text(
            text + 'base = "cube:standalone"\nconfigure = "launch/configure.py"\n', encoding="utf-8")
        (self.product / "launch").mkdir()
        (self.product / "launch" / "configure.py").write_text("def configure(**_):\n    pass\n", encoding="utf-8")
        git(self.product, "add", "-A")
        git(self.product, "commit", "-q", "-m", "launch folder")
        commit, tree = git(self.product, "rev-parse", "HEAD"), git(self.product, "rev-parse", "HEAD^{tree}")
        artifacts.record(self.store, "game", tree, commit, {"game-1.0.jar": {
            "sha256": self.digests["game-1.0.jar"], "job": "build", "run": "r", "from": "build/libs/game-1.0.jar"}})
        result = launch.resolve_command(self.ctx, str(self.product))
        doc = json.loads(Path(result["launch"]).read_text(encoding="utf-8"))
        line = self.set_doc["projects"]["cube"]["commit"]
        self.assertEqual((doc["base"]["commit"], doc["base"]["source"]), (line, "project.toml"))
        self.assertEqual(doc["base"]["overlay"]["tree"], git(cube, "rev-parse", f"{line}:launch/standalone"))
        self.assertEqual(doc["definition"], {"commit": commit, "launch_tree": git(self.product, "rev-parse",
                                                                                   "HEAD:launch")})
        self.assertEqual(doc["configure"]["blob"], git(self.product, "rev-parse", "HEAD:launch/configure.py"))
        self.assertEqual(result["base"], {"name": "cube:standalone", "commit": line, "source": "project.toml"})


class Inheritance(Base):
    """A launch whose base is another product's launch inherits it, resolved at that project's commit (its line, or
    its branch when it has none) as that product resolves it, then adds its own members; one build per mod id holds
    across the union, a cycle and a chain too deep are refused, and launch.json names every launch passed through."""

    HOST_COMMIT = "1" * 40
    HOST_TREE = "4" * 40

    def setUp(self):
        super().setUp()
        for name, data in {"host-1.jar": jar_bytes("host"), "host-fork.jar": jar_bytes("host", extra=b"fork"),
                           "solo-1.jar": jar_bytes("solo")}.items():
            source = self.base / name
            source.write_bytes(data)
            self.digests[name] = artifacts.put(self.store, source).parent.name
        artifacts.record(self.store, "host", self.HOST_TREE, self.HOST_COMMIT, {"host-1.jar": {
            "sha256": self.digests["host-1.jar"], "job": "build", "run": "r", "from": "build/libs/host-1.jar"}})
        self.set_doc["third"]["hostfork"] = {"module": "org.example:hostfork:1",
                                             "files": {"runtime": self.digests["host-fork.jar"]}, "licence": "MIT"}
        self.standalone = {"installer": ["{java}"], "server": ["{java}"], "client": ["{java}"], "overlay": "standalone"}
        self.host_launch = {
            "members": ["host:mod@both", "cube:mod@both", "ui@both"], "base": "cube:standalone",
            "configure": "launch/configure.py", "overlay": "host",
            "params": {"seed": 1, "scale": {"type": "int", "default": 1, "min": 1, "max": 4}},
            "jvm": {"client": ["-Dhost.x=1", "-Dkeep=1", "-Xmx4g"]},
            "tour": {"captured": "Host captured", "step": 16},
            "drivers": {"pipeline": {"command": ["{python}", "{root}/tools/pipeline.py"]}},
            "collect": ["host/*.log"]}
        self.host_toml = {"project": {"name": "host"},
                          "artifacts": {"mod": {"check": "build", "file": "build/libs/host-*.jar"}},
                          "launch": {"default": self.host_launch,
                                     "member": {"failures": ["host refused: .*"]}}}
        self.solo_toml = {"project": {"name": "solo"}, "artifacts": {"mod": {"file": "build/libs/solo-*.jar"}}}
        artifacts.record(self.store, "solo", "6" * 40, "5" * 40, {"solo-1.jar": {
            "sha256": self.digests["solo-1.jar"], "job": "build", "run": "r", "from": "build/libs/solo-1.jar"}})
        self.branches = {"host": (self.HOST_COMMIT, "refs/remotes/origin/main"), "solo": ("5" * 40, "refs/heads/main")}
        self.trees = {("host", ""): ("tree", self.HOST_TREE), ("host", "launch"): ("tree", "7" * 40),
                      ("host", "launch/host"): ("tree", "8" * 40), ("host", "launch/configure.py"): ("blob", "9" * 40),
                      ("cube", "launch/standalone"): ("tree", "a1" * 20), ("game", "launch"): ("tree", "b1" * 20),
                      ("game", "launch/game"): ("tree", "c1" * 20), ("solo", ""): ("tree", "6" * 40)}
        self.derived = {"base": "host:default", "members": ["game:mod@both", "draw:mod@client"], "overlay": "game",
                        "params": {"seed": 5}, "jvm": {"client": ["-Dhost.x=2", "-Xmx6g"]}, "tour": {"step": 8}}

    def extras(self, project, commit):
        return {"host": self.host_toml, "solo": self.solo_toml,
                "cube": {"launch": {"standalone": self.standalone}}}.get(project)

    def derive(self, table=None, follow="default", set_doc=None):
        follow = self.branches.get if follow == "default" else follow
        return launch.resolve(product="game", commit=COMMIT, tree=TREE, launch="default",
                              table=launch.definition({"launch": {"default": table or self.derived}}, "default"),
                              set_doc=set_doc or self.set_doc, revision="9" * 40, store=self.store,
                              product_artifacts={"mod": {"check": "build", "file": "build/libs/game-*.jar"}},
                              product_index=artifacts.find(self.store, "game", TREE), extras=self.extras,
                              objects=lambda project, commit, path: self.trees.get((project, path)), follow=follow)

    def test_the_base_launch_is_inherited_and_the_launchs_own_added(self):
        doc = self.derive()
        self.assertEqual([m["member"] for m in doc["members"]],
                         ["host:mod@both", "cube:mod@both", "ui@both", "game:mod@both", "draw:mod@client"])
        by = {m["member"]: m for m in doc["members"]}
        self.assertEqual((by["host:mod@both"]["kind"], by["host:mod@both"]["commit"], by["host:mod@both"]["tree"],
                          by["host:mod@both"]["sha256"]),
                         ("product", self.HOST_COMMIT, self.HOST_TREE, self.digests["host-1.jar"]))
        self.assertEqual({m["member"]: m.get("via") for m in doc["members"]},
                         {"host:mod@both": "host:default", "cube:mod@both": "host:default", "ui@both": "host:default",
                          "game:mod@both": None, "draw:mod@client": None})
        self.assertEqual(doc["inherits"], [{"name": "host:default", "project": "host", "launch": "default",
                                            "commit": self.HOST_COMMIT, "tree": self.HOST_TREE, "chosen": "branch",
                                            "ref": "refs/remotes/origin/main", "launch_tree": "7" * 40}])
        # the base launch's own base, read in its context: cube's line
        self.assertEqual((doc["base"]["name"], doc["base"]["commit"], doc["base"]["chosen"],
                          doc["base"]["overlay"]["tree"]), ("cube:standalone", "c" * 40, "line", "a1" * 20))
        self.assertEqual(doc["configure"], {"project": "host", "commit": self.HOST_COMMIT,
                                            "path": "launch/configure.py", "blob": "9" * 40})
        self.assertEqual([o["tree"] for o in doc["overlays"]], ["8" * 40, "c1" * 20])
        self.assertEqual(doc["overlay"]["project"], "game")
        self.assertEqual(doc["params"], {"seed": 5, "scale": 1})
        self.assertEqual(doc["param_types"]["scale"], {"type": "int", "min": 1, "max": 4})
        self.assertEqual(doc["jvm"]["client"], ["-Dkeep=1", "-Dhost.x=2", "-Xmx6g"])
        self.assertEqual(doc["tour"], {"captured": "Host captured", "step": 8})
        self.assertNotIn("drivers", doc)  # a driver runs from its own product's tree
        self.assertEqual((doc["collect"], doc["failures"]), (["host/*.log"], []))
        self.assertEqual(doc["definition"], {"commit": COMMIT, "launch_tree": "b1" * 20})

    def test_a_priming_start_is_inherited_unless_the_launch_names_its_own(self):
        self.host_launch["prime"] = {"commands": ["say primed"], "seconds": 5}
        self.assertEqual(self.derive()["prime"], {"commands": ["say primed"], "seconds": 5})
        self.assertEqual(self.derive(table={**self.derived, "prime": True})["prime"], {"commands": [], "seconds": 0})
        self.assertNotIn("prime", self.derive(table={**self.derived, "prime": False}))

    def test_a_base_on_its_line_says_so(self):
        set_doc = json.loads(json.dumps(self.set_doc))
        set_doc["projects"]["host"] = {"commit": self.HOST_COMMIT, "artifacts": {}}
        doc = self.derive(follow=None, set_doc=set_doc)
        self.assertEqual((doc["inherits"][0]["chosen"], "ref" in doc["inherits"][0]), ("line", False))

    def test_a_param_of_another_type_than_the_inherited_one_is_refused(self):
        with self.assertRaisesRegex(launch.LaunchError, r"param scale is int, not 'big'"):
            self.derive(table={**self.derived, "params": {"scale": "big"}})
        with self.assertRaisesRegex(launch.LaunchError, r"param scale is 1..4, not 9"):
            self.derive(table={**self.derived, "params": {"scale": 9}})
        doc = self.derive(table={**self.derived, "params": {"scale": {"type": "string", "default": "big"}}})
        self.assertEqual((doc["params"]["scale"], doc["param_types"]["scale"]), ("big", {"type": "string"}))

    def test_one_build_per_mod_id_across_the_union_and_no_member_twice(self):
        with self.assertRaisesRegex(launch.LaunchError,
                                    r"two builds of one mod id: host from host:mod@both \(from host:default\), "
                                    r"hostfork@both"):
            self.derive(table={**self.derived, "members": ["game:mod@both", "hostfork@both"]})
        with self.assertRaisesRegex(launch.LaunchError, r"member cube:mod@both: the base host:default already names it"):
            self.derive(table={**self.derived, "members": ["game:mod@both", "cube:mod@both"]})

    def test_a_base_product_the_store_lacks_names_the_check_at_its_commit(self):
        self.branches["host"] = ("2" * 40, "refs/heads/main")
        self.trees[("host", "")] = ("tree", "3" * 40)
        with self.assertRaisesRegex(launch.MissingBuild,
                                    r"base host:default: CI has not built host tree 333333333333 .*run wb check in "
                                    r"host at 222222222222") as caught:
            self.derive()
        self.assertEqual(caught.exception.missing, ("host", "2" * 40))

    def test_a_cycle_and_a_chain_too_deep_are_refused(self):
        self.host_launch["base"] = "game:default"
        self.branches["game"] = (COMMIT, "HEAD")
        game_toml = {"launch": {"default": self.derived}}
        extras = self.extras
        self.extras = lambda project, commit: game_toml if project == "game" else extras(project, commit)
        with self.assertRaisesRegex(launch.LaunchError,
                                    r"bases go round: game:default -> host:default -> game:default"):
            self.derive()
        self.host_launch["base"] = "cube:standalone"
        with mock.patch.object(launch, "MAX_DEPTH", 1), \
                self.assertRaisesRegex(launch.LaunchError, r"more than 1 launches deep: game:default -> host:default"):
            self.derive()

    def test_a_base_with_no_line_and_no_branch_to_follow_is_refused(self):
        with self.assertRaisesRegex(launch.LaunchError, r"base host:default: the set has no line for host$"):
            self.derive(follow=None)
        with self.assertRaisesRegex(launch.LaunchError, r"host is not registered on this machine"):
            self.derive(follow=lambda project: None)

    def test_a_member_of_a_project_with_no_line_follows_its_branch(self):
        doc = self.derive(table={"members": ["game:mod@both", "solo:mod@client"]})
        solo = doc["members"][1]
        self.assertEqual((solo["kind"], solo["commit"], solo["tree"], solo["ref"], solo["sha256"]),
                         ("branch", "5" * 40, "6" * 40, "refs/heads/main", self.digests["solo-1.jar"]))
        self.solo_toml["artifacts"] = {}
        with self.assertRaisesRegex(launch.LaunchError, r"solo has no line in the set, and at 555555555555 "
                                                        r"\(refs/heads/main\) its project.toml declares no "
                                                        r"\[artifacts.mod\]"):
            self.derive(table={"members": ["game:mod@both", "solo:mod@client"]})
        with self.assertRaisesRegex(launch.LaunchError, r"the set names no project or third entry solo"):
            self.derive(table={"members": ["game:mod@both", "solo:mod@client"]}, follow=None)

    def test_an_inheriting_launch_runs_on_its_bases_runtime_and_may_name_only_that(self):
        self.host_launch["runtime"] = {"kind": "cleanroom", "version": "0.7.7-alpha", **runtime_files("0.7.7-alpha")}
        doc = self.derive()
        self.assertEqual((doc["runtime"]["version"], doc["runtime"]["source"]), ("0.7.7-alpha", "launch"))
        self.assertEqual(doc["runtime"]["files"], runtime_files("0.7.7-alpha"))
        same = {**self.derived, "runtime": dict(self.host_launch["runtime"])}
        self.assertEqual(self.derive(table=same)["runtime"], doc["runtime"])
        other = {**self.derived, "runtime": {"kind": "cleanroom", "version": "0.7.14-alpha"}}
        with self.assertRaisesRegex(launch.LaunchError, "runtime cleanroom 0.7.14-alpha is not its base host:default's "
                                                        "cleanroom 0.7.7-alpha"):
            self.derive(table=other)


class StandIns(Base):
    """A project standing in for its line: its members, holds included, come from the Store by the stand-in's tree,
    are marked as stand-ins, and every other line stays the set's."""

    STAND_COMMIT, STAND_TREE = "1" * 40, "2" * 40

    def setUp(self):
        super().setUp()
        for name, data in {"lib-2.jar": jar_bytes("lib", extra=b"two"),
                           "lib-adapter-2.jar": jar_bytes("lib_adapter", extra=b"two")}.items():
            source = self.base / name
            source.write_bytes(data)
            self.digests[name] = artifacts.put(self.store, source).parent.name
        artifacts.record(self.store, "lib", self.STAND_TREE, self.STAND_COMMIT, {
            "lib-2.jar": {"sha256": self.digests["lib-2.jar"], "job": "build", "run": "r",
                          "from": "build/libs/lib-2.jar"},
            "lib-adapter-2.jar": {"sha256": self.digests["lib-adapter-2.jar"], "job": "build", "run": "r",
                                  "from": "adapter/build/libs/lib-adapter-2.jar"}})
        self.lib_toml = {"project": {"name": "lib"}, "artifacts": {
            "mod": {"file": "build/libs/lib-*.jar"}, "adapter": {"file": "adapter/build/libs/lib-adapter-*.jar"},
            "docs": {"file": "build/docs/lib-*.zip"}}}  # declared, not built by this check: left out

    def stood(self, stand_ins):
        return launch.stand_in(self.set_doc, stand_ins, product="game", store=self.store,
                               extras=lambda project, commit: self.lib_toml if project == "lib" else None,
                               objects=lambda project, commit, path: ("tree", self.STAND_TREE)
                               if (project, commit, path) == ("lib", self.STAND_COMMIT, "") else None)

    def test_a_stand_in_replaces_its_line_and_its_holds_and_nothing_else(self):
        set_doc, record = self.stood({"lib": self.STAND_COMMIT})
        self.assertEqual(record, {"lib": {"commit": self.STAND_COMMIT, "tree": self.STAND_TREE, "replaces": "d" * 40}})
        self.assertNotIn("lib:adapter", set_doc["holds"])
        self.assertIn("lib:adapter", self.set_doc["holds"])  # the set's own document is left as it was
        doc = self.run_resolve(set_doc=set_doc)
        launch._mark_stand_ins(doc, record)
        by = {m["member"]: m for m in doc["members"]}
        self.assertEqual((by["lib:mod@both"]["kind"], by["lib:mod@both"]["commit"], by["lib:mod@both"]["tree"],
                          by["lib:mod@both"]["sha256"]),
                         ("stand-in", self.STAND_COMMIT, self.STAND_TREE, self.digests["lib-2.jar"]))
        self.assertEqual((by["lib:adapter@both"]["kind"], by["lib:adapter@both"]["sha256"]),
                         ("stand-in", self.digests["lib-adapter-2.jar"]))
        self.assertEqual((by["cube:mod@both"]["kind"], by["cube:mod@both"]["commit"]), ("line", "c" * 40))
        self.assertEqual(doc["stand_ins"], record)

    def test_a_tree_ci_has_not_built_names_the_check_that_builds_it(self):
        self.lib_toml = dict(self.lib_toml)
        objects = {("lib", "3" * 40, ""): ("tree", "4" * 40)}
        with self.assertRaisesRegex(launch.MissingBuild, r"stand-in lib: CI has not built lib tree 444444444444 .*run "
                                                         r"wb check in lib at 333333333333") as caught:
            launch.stand_in(self.set_doc, {"lib": "3" * 40}, product="game", store=self.store,
                            extras=lambda project, commit: self.lib_toml,
                            objects=lambda project, commit, path: objects.get((project, commit, path)))
        self.assertEqual(caught.exception.missing, ("lib", "3" * 40))

    def test_the_launchs_own_project_and_a_short_commit_are_refused(self):
        with self.assertRaisesRegex(launch.LaunchError, r"stand-in game: it is the launch's own project"):
            self.stood({"game": self.STAND_COMMIT})
        with self.assertRaisesRegex(launch.LaunchError, r"stand-in lib: give a full commit id, not '1111'"):
            self.stood({"lib": "1111"})


class InheritedCommand(unittest.TestCase):
    """lab.resolve in real repositories: the base product with no line follows origin's branch, else the local one,
    and its artifact must be in the Store for that commit's tree."""

    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="wb-inherit-"))
        self.addCleanup(__import__("shutil").rmtree, self.base, ignore_errors=True)
        self.store = self.base / "store"
        jars = {}
        for name, data in {"game-1.0.jar": jar_bytes("game"), "host-1.jar": jar_bytes("host")}.items():
            source = self.base / name
            source.write_bytes(data)
            jars[name] = artifacts.put(self.store, source).parent.name
        self.host = self.base / "host"
        self.host.mkdir()
        git(self.host, "init", "-q", "-b", "main")
        (self.host / "project.toml").write_text(
            'schema = 1\n\n[project]\nname = "host"\n\n[artifacts.mod]\ncheck = "build"\nfile = "build/libs/host-*.jar"\n'
            '\n[launch.default]\nmembers = ["host:mod@both"]\nparams = { seed = 3 }\n', encoding="utf-8")
        git(self.host, "add", "-A")
        git(self.host, "commit", "-q", "-m", "host")
        self.pushed = git(self.host, "rev-parse", "HEAD")
        git(self.host, "update-ref", "refs/remotes/origin/main", self.pushed)  # as a fetch leaves it
        artifacts.record(self.store, "host", git(self.host, "rev-parse", "HEAD^{tree}"), self.pushed,
                         {"host-1.jar": {"sha256": jars["host-1.jar"], "job": "build", "run": "r",
                                         "from": "build/libs/host-1.jar"}})
        (self.host / "README").write_text("local work, not pushed", encoding="utf-8")
        git(self.host, "add", "-A")
        git(self.host, "commit", "-q", "-m", "local")
        self.product = self.base / "game"
        self.product.mkdir()
        git(self.product, "init", "-q", "-b", "main")
        (self.product / "project.toml").write_text(
            'schema = 1\n\n[project]\nname = "game"\n\n[artifacts.mod]\ncheck = "build"\nfile = "build/libs/game-*.jar"\n'
            '\n[launch.default]\nbase = "host:default"\nmembers = ["game:mod@both"]\n', encoding="utf-8")
        git(self.product, "add", "-A")
        git(self.product, "commit", "-q", "-m", "game")
        artifacts.record(self.store, "game", git(self.product, "rev-parse", "HEAD^{tree}"),
                         git(self.product, "rev-parse", "HEAD"),
                         {"game-1.0.jar": {"sha256": jars["game-1.0.jar"], "job": "build", "run": "r",
                                           "from": "build/libs/game-1.0.jar"}})
        self.home = self.base / "set"
        self.home.mkdir()
        git(self.home, "init", "-q", "-b", "main")
        (self.home / "set.json").write_text(json.dumps({"schema": 1, "projects": {}, "platform": {
            "java": 26, "jdk": {"26": {"build": "26.0.2.1+1"}}, "cleanroom": {"version": "0.7.14-alpha"}}}),
            encoding="utf-8")
        git(self.home, "add", "-A")
        git(self.home, "commit", "-q", "-m", "set")
        revision = git(self.home, "rev-parse", "HEAD")
        workspace = FakeWorkspace(self.store, self.home, {"host": type("P", (), {"repo": self.host, "entry": {}})})
        spec = {"job": "j", "command": "lab.resolve", "module": "lab", "params": {}, "module_config": {},
                "machine": "host", "work": str(self.base / "job" / "work"), "out": str(self.base / "job" / "out")}
        self.ctx = Context(spec, lambda event: None)
        for patch in (mock.patch("wb.workspace.Workspace", lambda: workspace),
                      mock.patch("wb.setwork.resolve", lambda workspace, folder, say=None: revision)):
            patch.start()
            self.addCleanup(patch.stop)

    def test_the_base_follows_origins_branch_and_launch_json_names_the_chain(self):
        result = launch.resolve_command(self.ctx, str(self.product))
        doc = json.loads(Path(result["launch"]).read_text(encoding="utf-8"))
        self.assertEqual([m["member"] for m in doc["members"]], ["host:mod@both", "game:mod@both"])
        self.assertEqual([(s["name"], s["commit"], s["chosen"], s["ref"]) for s in doc["inherits"]],
                         [("host:default", self.pushed, "branch", "refs/remotes/origin/main")])
        self.assertEqual(doc["params"], {"seed": 3})
        self.assertEqual(result["inherits"], [{"name": "host:default", "commit": self.pushed, "chosen": "branch"}])

    def test_without_origin_the_local_branch_and_its_missing_build_is_named(self):
        git(self.host, "update-ref", "-d", "refs/remotes/origin/main")
        local = git(self.host, "rev-parse", "main")
        with self.assertRaisesRegex(ParamError, rf"run wb check in host at {local[:12]}") as caught:
            launch.resolve_command(self.ctx, str(self.product))
        self.assertEqual(caught.exception.__cause__.missing, ("host", local))



def runtime_files(version: str) -> dict:
    base = f"https://github.com/CleanroomMC/Cleanroom/releases/download/{version}/cleanroom-{version}"
    return {"installer": {"url": base + "-installer.jar", "file": f"cleanroom-{version}-installer.jar",
                          "sha256": "4" * 64},
            "universal": {"url": base + "-universal.jar", "file": f"cleanroom-{version}-universal.jar",
                          "sha256": "5" * 64},
            "prism": {"url": base + ".zip", "file": f"cleanroom-{version}.zip", "sha256": "6" * 64}}


class Runtimes(Base):
    """A launch may run on another version of the runtime than the platform's, naming that version's files by SHA-256
    in the launch or through a third entry; a launch that inherits another runs on that launch's runtime."""

    def test_a_launch_names_its_own_cleanroom_version_and_files(self):
        table = {**self.table, "runtime": {"kind": "cleanroom", "version": "0.7.7-alpha",
                                           **runtime_files("0.7.7-alpha")}}
        runtime = self.run_resolve(table=table)["runtime"]
        self.assertEqual((runtime["kind"], runtime["version"], runtime["source"]), ("cleanroom", "0.7.7-alpha", "launch"))
        self.assertEqual(runtime["files"], runtime_files("0.7.7-alpha"))
        self.assertEqual((runtime["java"], runtime["jdk"]), (26, "26.0.2.1+1"))  # the platform's JDK still

    def test_another_version_without_its_files_is_refused(self):
        table = {**self.table, "runtime": {"kind": "cleanroom", "version": "0.7.7-alpha"}}
        with self.assertRaisesRegex(launch.LaunchError, "0.7.7-alpha is not the platform's 0.7.14-alpha, and the "
                                                        "launch names none of its files"):
            self.run_resolve(table=table)
        same = {**self.table, "runtime": {"kind": "cleanroom", "version": "0.7.14-alpha"}}
        self.assertEqual(self.run_resolve(table=same)["runtime"]["version"], "0.7.14-alpha")  # the platform's own

    def test_a_third_entry_names_the_runtimes_files_in_the_store(self):
        names = {}
        for part, name in (("installer", "cr-installer.jar"), ("universal", "cr-universal.jar"),
                           ("prism", "cr.zip")):
            source = self.base / name
            source.write_bytes(part.encode() * 8)
            names[part] = (artifacts.put(self.store, source).parent.name, name)
        set_doc = json.loads(json.dumps(self.set_doc))
        set_doc["third"]["cleanroom-077"] = {"module": "com.cleanroommc:cleanroom:0.7.7-alpha", "licence": "LGPL-2.1",
                                             "files": {part: digest for part, (digest, _) in names.items()},
                                             "urls": {"installer": "https://example.org/cr-installer.jar"}}
        table = {**self.table, "runtime": {"kind": "cleanroom", "version": "0.7.7-alpha", "third": "cleanroom-077"}}
        runtime = self.run_resolve(table=table, set_doc=set_doc)["runtime"]
        self.assertEqual(runtime["source"], "third cleanroom-077")
        self.assertEqual({part: (f["sha256"], f["file"]) for part, f in runtime["files"].items()}, names)
        self.assertEqual(runtime["files"]["installer"]["url"], "https://example.org/cr-installer.jar")
        self.assertNotIn("url", runtime["files"]["prism"])  # the Store holds it; lab.build needs no URL
        del set_doc["third"]["cleanroom-077"]["files"]["prism"]
        with self.assertRaisesRegex(launch.LaunchError, "third entry cleanroom-077 has no prism file"):
            self.run_resolve(table=table, set_doc=set_doc)
        with self.assertRaisesRegex(launch.LaunchError, "names cleanroom-0, which the set has no third entry for"):
            self.run_resolve(table={**table, "runtime": {**table["runtime"], "third": "cleanroom-0"}},
                             set_doc=set_doc)

    def test_the_runtime_table_is_checked(self):
        files = runtime_files("0.7.7-alpha")
        for runtime, pattern in [
                ({"kind": "forge", "version": "1"}, "runtime.kind is one of cleanroom"),
                ({"version": "latest"}, "runtime.version is the runtime's version"),
                ({"version": "0.7.7-alpha", "installer": files["installer"]}, "but not universal, prism"),
                ({"version": "0.7.7-alpha", **files, "third": "x"}, "a third entry and files"),
                ({"version": "0.7.7-alpha", **files, "installer": {**files["installer"], "url": "http://x/a.jar"}},
                 "url is an https URL"),
                ({"version": "0.7.7-alpha", **files, "prism": {**files["prism"], "file": "a/b.zip"}},
                 "is a bare file name"),
                ({"version": "0.7.7-alpha", **files, "universal": {**files["universal"], "sha256": "x"}},
                 "is 64 hex digits"),
                ({"version": "0.7.7-alpha", "java": 25}, "unknown key runtime.java"),
                (["cleanroom"], "a kind \\(cleanroom\\) or a table")]:
            with self.subTest(pattern=pattern), self.assertRaisesRegex(launch.LaunchError, pattern):
                launch.definition({"launch": {"default": {"members": ["a:mod@both"], "runtime": runtime}}}, "default")


if __name__ == "__main__":
    unittest.main()
