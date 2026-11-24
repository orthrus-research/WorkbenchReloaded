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

    def test_the_interim_base_is_a_base_and_the_tour_keys_are_lab_runs(self):
        from wb_lab import bases, run
        for name, table in bases.INTERIM_BASES.items():
            launch.base_definition(table, name)
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
                                       "source": "project.toml", "definition": standalone,
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

    def test_a_base_project_without_the_table_falls_back_to_the_interim_copy_with_a_warning(self):
        from wb_lab import bases
        bases.INTERIM_BASES["cube:standalone"] = {"installer": ["x"], "server": ["x"], "client": ["x"]}
        self.addCleanup(bases.INTERIM_BASES.pop, "cube:standalone", None)
        warned = []
        doc = self.run_resolve(table={**self.table, "base": "cube:standalone"}, extras=lambda p, c: None,
                               objects=lambda *a: None, warn=warned.append)
        self.assertEqual((doc["base"]["source"], doc["base"]["commit"]), ("interim", "c" * 40))
        self.assertEqual(doc["base"]["definition"], bases.INTERIM_BASES["cube:standalone"])
        self.assertRegex(warned[0], r"^INTERIM BASE: cube at cccccccccccc has no \[launch.standalone\]")
        with self.assertRaisesRegex(launch.LaunchError, r"cube at cccccccccccc has no \[launch.other\]"):
            self.run_resolve(table={**self.table, "base": "cube:other"}, extras=lambda p, c: None,
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


if __name__ == "__main__":
    unittest.main()
