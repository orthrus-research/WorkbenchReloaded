"""Kept worlds: a world is known by its world key (seed, layout scale, definitions' SHA-256) and writer build;
a settled world is kept once per key and writer, restored into an instance without one, given way to by a template's
primed world, set aside when another build wrote it, and a fresh world is removed link-first. A world lab.freeze
imported is found by the same key."""

import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path[:0] = [str(REPO / "core"), str(REPO / "sdk" / "python"), str(REPO / "modules" / "lab")]

from wb_lab import legacy, worlds  # noqa: E402
from wb_lab.store import MARKER  # noqa: E402

DEFINITIONS = {"riven-terrain.json": {"revision": 3, "sha256": "a" * 64}}


def receipt(seed=8675309, scale=1.0, cubature="c" * 64, definitions=None) -> dict:
    return {"params": {"seed": seed, "scale": scale},
            "configure": {"record": {"layoutScale": scale, "definitions": definitions or DEFINITIONS}},
            "launch": {"members": {"cubature:mod@both": cubature, "riven:mod@both": "r" * 64}}}


LAUNCH = {"base": {"name": "cubature:standalone", "project": "cubature"}}


class Worlds(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="wb-worlds-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.store = worlds.store_of(self.root)
        self.ident = worlds.identity(receipt(), LAUNCH)

    def instance(self, name: str, world: str | None = None, marked: dict | None = None) -> Path:
        folder = self.root / name
        (folder / "server").mkdir(parents=True, exist_ok=True)
        (folder / MARKER).write_text(json.dumps({"schema": 1, "world": marked}), encoding="utf-8")
        if world is not None:
            self.write_world(folder, world)
        return folder

    @staticmethod
    def write_world(folder: Path, text: str):
        (folder / "server" / "world" / "region").mkdir(parents=True, exist_ok=True)
        (folder / "server" / "world" / "level.dat").write_text(text, encoding="utf-8")
        (folder / "server" / "world" / "region" / "r.0.0.mca").write_bytes(text.encode() * 10)

    @staticmethod
    def level(folder: Path) -> str | None:
        path = folder / "server" / "world" / "level.dat"
        return path.read_text(encoding="utf-8") if path.is_file() else None

    def played(self, folder: Path, ident=None, session="s1"):
        """A session played and settled on the instance's world."""
        chosen = worlds.choose(self.store, folder, ident or self.ident)
        if not worlds.present(folder):
            self.write_world(folder, f"generated in {session}")
        return worlds.after(self.store, folder, ident or self.ident, chosen, session=session, settled=True, keeps=True)

    # -- the key and the writer ---------------------------------------------------------------------------------
    def test_the_key_is_seed_scale_and_definitions_and_the_writer_is_the_bases_project(self):
        self.assertEqual(self.ident["key"], {"seed": 8675309, "scale": 1.0, "definitions": hashlib.sha256(
            json.dumps(DEFINITIONS, sort_keys=True).encode()).hexdigest()})
        self.assertEqual(self.ident["writer"], {"member": "cubature:mod@both", "sha256": "c" * 64})
        self.assertNotEqual(worlds.identity(receipt(seed=1), LAUNCH)["key"], self.ident["key"])
        self.assertNotEqual(worlds.identity(receipt(scale=2.0), LAUNCH)["key"], self.ident["key"])
        self.assertNotEqual(worlds.identity(receipt(definitions={"x": 1}), LAUNCH)["key"], self.ident["key"])
        # the writer is the base project's member alone: a rebuilt Riven writes the same world
        other = receipt()
        other["launch"]["members"]["riven:mod@both"] = "s" * 64
        self.assertEqual(worlds.identity(other, LAUNCH), self.ident)
        # without one build of the base's project, every member together is the writer
        whole = worlds.identity(receipt(), {})["writer"]
        self.assertIsNone(whole["member"])
        self.assertNotEqual(whole["sha256"], "c" * 64)

    def test_a_legacy_world_has_the_key_lab_freeze_gives_it(self):
        description = {"seed": 8675309, "generator": {"layoutScale": 1.0, "definitions": DEFINITIONS},
                       "cubicJar": {"sha256": "c" * 64}}
        self.assertEqual(legacy.world_key(description), (self.ident["key"], "c" * 64))

    # -- keeping and restoring ----------------------------------------------------------------------------------
    def test_a_settled_world_is_kept_and_restored_into_an_instance_without_one(self):
        first = self.instance("first")
        record = self.played(first)
        self.assertEqual(record["from"], "generated")
        label = record["kept"]
        self.assertEqual(label, worlds.label_of(self.ident))
        self.assertEqual(worlds.marker(first)["world"]["played"], True)
        fresh = self.instance("fresh")
        chosen = worlds.choose(self.store, fresh, self.ident)
        self.assertEqual((chosen["from"], chosen["label"]), ("restored", label))
        self.assertEqual(self.level(fresh), "generated in s1")
        self.assertFalse(os.path.samefile(fresh / "server" / "world" / "level.dat",
                                          first / "server" / "world" / "level.dat"))
        (fresh / "server" / "world" / "level.dat").write_text("changed", encoding="utf-8")  # a private copy
        self.assertEqual(self.level(first), "generated in s1")

    def test_the_kept_world_is_the_latest_settled_one_and_never_a_measure_or_unsettled_one(self):
        one = self.instance("one")
        self.played(one)
        self.write_world(one, "played on")
        chosen = worlds.choose(self.store, one, self.ident)
        self.assertEqual(chosen["from"], "instance")
        for keeps, settled in ((False, True), (True, False)):
            done = worlds.after(self.store, one, self.ident, chosen, session="s2", settled=settled, keeps=keeps)
            self.assertIsNone(done["kept"])
            self.assertTrue(done["why"])
        self.assertEqual(self.level(self.restored()), "generated in s1")
        worlds.after(self.store, one, self.ident, chosen, session="s3", settled=True, keeps=True)
        self.assertEqual(self.level(self.restored("again")), "played on")
        self.assertEqual(len(worlds.kept(self.store, self.ident["key"])), 1)

    def restored(self, name: str = "probe") -> Path:
        folder = self.instance(name)
        worlds.choose(self.store, folder, self.ident)
        return folder

    def test_a_settled_world_takes_the_place_of_a_templates_primed_world(self):
        self.played(self.instance("one"))
        primed = {**self.ident, "played": False, "from": "primed"}
        warm = self.instance("warm", world="primed")
        chosen = worlds.choose(self.store, warm, self.ident, primed=primed)
        self.assertEqual(chosen["from"], "restored")
        self.assertEqual(self.level(warm), "generated in s1")
        alone = worlds.store_of(self.root / "elsewhere")
        cold = self.instance("cold", world="primed")
        self.assertEqual(worlds.choose(alone, cold, self.ident, primed=primed)["from"], "instance")
        self.assertEqual(self.level(cold), "primed")

    # -- another writer build -----------------------------------------------------------------------------------
    def test_a_world_of_another_writer_is_set_aside_and_keep_world_plays_it(self):
        one = self.instance("one")
        self.played(one)
        self.write_world(one, "the old build's")
        worlds.mark(one, {**worlds.marker(one)["world"]})  # still the old build's identity
        rebuilt = worlds.identity(receipt(cubature="d" * 64), LAUNCH)
        chosen = worlds.choose(self.store, one, rebuilt)
        self.assertEqual(chosen["from"], "generated")
        self.assertEqual(chosen["passed_over"]["writer"]["sha256"], "c" * 64)
        self.assertIn("keep_world", chosen["hint"])
        self.assertFalse(worlds.present(one))
        self.write_world(one, "the new build's")
        worlds.after(self.store, one, rebuilt, chosen, session="s2", settled=True, keeps=True)
        self.assertEqual(worlds.marker(one)["world"]["set_aside"], chosen["set_aside"])
        # a default session plays the new build's world, and still names the one set aside
        again = worlds.choose(self.store, one, rebuilt)
        self.assertEqual((again["from"], self.level(one)), ("instance", "the new build's"))
        worlds.after(self.store, one, rebuilt, again, session="s3", settled=False, keeps=True)
        kept = worlds.choose(self.store, one, rebuilt, "keep")
        self.assertEqual((kept["from"], kept["label"]), ("restored", chosen["set_aside"]))
        self.assertEqual(self.level(one), "the old build's")
        worlds.after(self.store, one, rebuilt, kept, session="s4", settled=False, keeps=True)
        self.assertIsNone(worlds.marker(one)["world"]["set_aside"])

    def test_a_new_instance_of_another_writer_generates_and_names_keep_world(self):
        self.played(self.instance("one"))
        rebuilt = worlds.identity(receipt(cubature="d" * 64), LAUNCH)
        fresh = self.instance("fresh")
        chosen = worlds.choose(self.store, fresh, rebuilt)
        self.assertEqual(chosen["from"], "generated")
        self.assertIn("keep_world", chosen["hint"])
        kept = worlds.choose(self.store, self.instance("kept"), rebuilt, "keep")
        self.assertEqual(kept["from"], "restored")

    def test_a_world_nothing_recorded_is_played_and_never_kept(self):
        old = self.instance("old", world="from before")
        chosen = worlds.choose(self.store, old, self.ident)
        self.assertEqual((chosen["from"], chosen["unrecorded"]), ("instance", True))
        self.assertIn("fresh_world", chosen["hint"])
        done = worlds.after(self.store, old, self.ident, chosen, session="s1", settled=True, keeps=True)
        self.assertIsNone(done["kept"])
        self.assertEqual(worlds.kept(self.store, self.ident["key"]), [])

    # -- a fresh world ------------------------------------------------------------------------------------------
    def test_a_fresh_world_removes_every_part_link_first(self):
        from wb.platform import make_link
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "kept.dat").write_bytes(b"not the world's")
        one = self.instance("one", world="old")
        make_link(one / "server" / "world" / "shared", outside)
        (one / "client" / "Distant_Horizons_server_data").mkdir(parents=True)
        os.chmod(one / "server" / "world" / "level.dat", 0o444)
        chosen = worlds.choose(self.store, one, self.ident, "fresh")
        self.assertEqual(chosen["from"], "generated")
        self.assertFalse(any((one / part).exists() for part in worlds.WORLD_PARTS))
        self.assertEqual((outside / "kept.dat").read_bytes(), b"not the world's")

    def test_a_frozen_legacy_world_is_found_by_its_key(self):
        source = self.instance("legacy", world="the old route's")
        self.store.snapshot(source, "legacy-abc", {"key": self.ident["key"], "cubature": "c" * 64, "from": "x"})
        found = worlds.kept(self.store, self.ident["key"], "c" * 64)
        self.assertEqual([r["label"] for r in found], ["legacy-abc"])
        fresh = self.instance("fresh")
        self.assertEqual(worlds.choose(self.store, fresh, self.ident)["label"], "legacy-abc")
        self.assertEqual(self.level(fresh), "the old route's")


if __name__ == "__main__":
    unittest.main()
