"""lab.freeze and lab.thaw: a legacy instance (no instance.json) is inventoried without change, then frozen: a manifest
of every file, its world in the world store by world key, its tours/ and runs/ in a kept session, every file
read-only and no link entered; a second freeze changes nothing; a folder reached through a link is refused; thaw
clears exactly the marks the freeze made, and leaves a file changed since then marked."""

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path[:0] = [str(REPO / "core"), str(REPO / "sdk" / "python"), str(REPO / "modules" / "lab")]

from wb.platform import make_link, remove_tree  # noqa: E402
from wb_core.module import Context, ParamError  # noqa: E402
from wb_lab import legacy  # noqa: E402
from wb_lab import run as runs  # noqa: E402

DESCRIPTION = {"name": "tour-a", "seed": 8675309, "generator": {"layoutScale": 1.0, "definitions": {"a.json": 1}},
               "cubicJar": {"sha256": "c" * 64}, "purpose": "a test",
               "launch": {"sha256": "d" * 64, "project": "riven", "launch": "default", "commit": "e" * 40},
               "modHashes": {"server": {"mod.jar": "f" * 64}}}


def writable(path: Path) -> bool:
    return bool(os.stat(path).st_mode & stat.S_IWRITE)


class Freeze(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="wb-legacy-"))
        self.addCleanup(remove_tree, self.root)
        self.home = self.root / "lab"
        (self.home / "instances" / ".store").mkdir(parents=True)
        self.checkout = self.root / "riven" / ".local" / "cubic-standalone"
        self.instance = self.checkout / "tour-a"
        files = {"standalone.json": json.dumps(DESCRIPTION), "server/world/level.dat": "level",
                 "server/world/region2d/r.0.0": "region", "server/world/session.lock": "lock",
                 "server/world.cubature-cache/c.bin": "cache", "server/mods/mod.jar": "jar",
                 "client/options.txt": "fov:70", "tours/20261101-010203/01-arrive.png": "png",
                 "tours/20261101-010203/views.json": "[]", "runs/preflight.json": "{}",
                 "runs/write/result.json": "{\"ok\": true}"}
        for rel, text in files.items():
            (self.instance / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.instance / rel).write_text(text, encoding="utf-8")
        os.chmod(self.instance / "server/mods/mod.jar", stat.S_IREAD)  # read-only before the freeze
        self.shared = self.root / "shared"
        (self.shared / "inside.txt").parent.mkdir(parents=True)
        (self.shared / "inside.txt").write_text("not ours", encoding="utf-8")
        make_link(self.instance / "client" / "shared", self.shared)
        built = self.checkout / "built"  # lab.build's: passed over
        built.mkdir()
        (built / "instance.json").write_text("{}", encoding="utf-8")

    def context(self) -> Context:
        return Context({"job": "direct", "command": "lab.freeze", "module": "lab", "work": str(self.root / "work"),
                        "out": str(self.root / "out"), "module_config": {}, "lab": {"home": str(self.home)}},
                       lambda event: None)

    def all_files(self):
        return [p for p, _rel, st in legacy.walk(self.instance) if st is not None]

    def test_an_inventory_changes_nothing(self):
        result = legacy.freeze(self.context(), str(self.checkout))
        self.assertFalse(result["applied"])
        [row] = result["instances"]
        self.assertEqual(row["state"], "inventoried")
        self.assertEqual(row["links"], 1)
        self.assertEqual(row["world"]["writer"], "c" * 64)
        self.assertEqual([s["name"] for s in result["skipped"]], ["built"])
        self.assertTrue(writable(self.instance / "standalone.json"))
        self.assertFalse((self.home / "legacy").exists())
        self.assertFalse((self.home / "sessions").exists())

    def test_a_freeze_records_imports_and_marks(self):
        result = legacy.freeze(self.context(), str(self.checkout), apply=True)
        [row] = result["instances"]
        self.assertEqual(row["state"], "frozen")
        manifest = json.loads(Path(row["manifest"]).read_text(encoding="utf-8"))
        self.assertEqual(manifest["state"], "frozen")
        self.assertIn("lab.thaw", manifest["undo"])
        self.assertEqual(len(manifest["files"]["server/world/level.dat"]["sha256"]), 64)
        self.assertTrue(manifest["files"]["server/mods/mod.jar"]["read_only"])
        self.assertNotIn("marked", manifest["files"]["server/mods/mod.jar"])
        self.assertEqual([link["path"] for link in manifest["links"]], ["client/shared"])
        for path in self.all_files():
            self.assertFalse(writable(path), path)
        self.assertTrue(writable(self.shared / "inside.txt"), "a link was entered")
        # the world, by world key
        world = json.loads((self.home / "instances" / ".store" / "worlds" / row["world"]["label"] / "world.json")
                           .read_text(encoding="utf-8"))
        self.assertEqual(world["key"]["key"]["seed"], 8675309)
        self.assertEqual(world["key"]["cubature"], "c" * 64)
        self.assertIn("server/world/level.dat", world["files"])
        self.assertIn("server/world.cubature-cache/c.bin", world["files"])
        self.assertNotIn("server/world/session.lock", world["files"])
        # tours/ and runs/, in a kept session
        session = Path(row["session"]["folder"])
        self.assertTrue((session / "tours" / "20261101-010203" / "01-arrive.png").is_file())
        self.assertTrue((session / "runs" / "write" / "result.json").is_file())
        record = json.loads((session / runs.SESSION).read_text(encoding="utf-8"))
        self.assertEqual(record["kind"], "legacy")
        self.assertEqual(record["launch"]["sha256"], "d" * 64)
        self.assertEqual(record["kept"]["reason"].split(" ")[0], "evidence")
        self.assertTrue((session / runs.KEPT).is_file())
        self.assertIn(row["session"]["session"], runs.kept_roots(self.home / "sessions")["sessions"])
        self.assertTrue(runs.ID.match(row["session"]["session"]))
        # the instance is still all there
        self.assertTrue((self.instance / "tours" / "20261101-010203" / "01-arrive.png").is_file())
        # a second freeze changes nothing
        again = legacy.freeze(self.context(), str(self.checkout), apply=True)
        self.assertEqual(again["instances"][0]["state"], "frozen already")
        self.assertEqual(len(list((self.home / "sessions").iterdir())), 1)

    def test_an_imported_session_is_kept_beside_a_played_one(self):
        played = self.home / "sessions" / "20261124-000000-abcdef"
        played.mkdir(parents=True)
        (played / runs.KEPT).write_text(json.dumps({"session": played.name, "instance": "game", "template": "kgame",
                                                    "launch": "a" * 64}), encoding="utf-8")
        description = dict(DESCRIPTION)
        del description["launch"]
        (self.instance / "standalone.json").write_text(json.dumps(description), encoding="utf-8")
        [row] = legacy.freeze(self.context(), str(self.instance), apply=True)["instances"]
        roots = runs.kept_roots(self.home / "sessions")
        self.assertEqual(roots["templates"], ["kgame"])
        self.assertEqual(roots["launches"], ["a" * 64])
        self.assertEqual(sorted(roots["sessions"]), sorted([played.name, row["session"]["session"]]))

    def test_a_folder_reached_through_a_link_is_refused(self):
        alias = self.root / "alias"
        make_link(alias, self.checkout)
        with self.assertRaises(ParamError):
            legacy.freeze(self.context(), str(alias), apply=True)
        self.assertEqual(legacy.freeze(self.context(), str(alias))["instances"][0]["state"], "inventoried")
        self.assertTrue(writable(self.instance / "standalone.json"))

    def test_without_a_world_store_the_key_is_recorded(self):
        remove_tree(self.home / "instances")
        [row] = legacy.freeze(self.context(), str(self.instance), apply=True)["instances"]
        self.assertIsNone(row["world"]["label"])
        self.assertEqual(row["world"]["key"]["seed"], 8675309)
        self.assertEqual(row["world"]["path"], str(self.instance / "server" / "world"))

    def test_thaw_clears_exactly_the_marks_made(self):
        legacy.freeze(self.context(), str(self.checkout), apply=True)
        changed = self.instance / "client" / "options.txt"
        os.chmod(changed, stat.S_IREAD | stat.S_IWRITE)
        changed.write_text("fov:90", encoding="utf-8")
        os.utime(changed, ns=(1, 1))
        os.chmod(changed, stat.S_IREAD)
        result = legacy.thaw(self.context(), str(self.checkout))
        [row] = result["instances"]
        self.assertEqual(row["not_thawed"], ["client/options.txt"])
        self.assertTrue(writable(self.instance / "standalone.json"))
        self.assertTrue(writable(self.instance / "server" / "world" / "level.dat"))
        self.assertFalse(writable(self.instance / "server" / "mods" / "mod.jar"), "was read-only before the freeze")
        self.assertFalse(writable(changed))
        manifest = json.loads(Path(row["manifest"]).read_text(encoding="utf-8"))
        self.assertEqual(manifest["state"], "partly thawed")


if __name__ == "__main__":
    unittest.main()
