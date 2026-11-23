"""The Store: a file is kept by its SHA-256 and never replaced, an index names what a tree built, two builds of one
tree that differ are recorded instead of replaced, a file changed outside Workbench is refused, and seeded bytes
carry where they came from."""

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness  # noqa: E402,F401

from wb_core import artifacts  # noqa: E402


class Store(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.base = Path(self.folder.name)
        self.store, self.pins = self.base / "store", self.base / "pins"
        self.jar = self.base / "mod.jar"
        self.jar.write_bytes(b"one")
        self.digest = hashlib.sha256(b"one").hexdigest()

    def tearDown(self):
        self.folder.cleanup()

    def test_kept_by_hash_in_the_store_and_the_pin_cache(self):
        kept = artifacts.put(self.store, self.jar, self.digest, self.pins)
        self.assertEqual(kept, self.store / "files" / self.digest / "mod.jar")
        self.assertEqual((self.pins / self.digest).read_bytes(), b"one")
        self.assertEqual(artifacts.put(self.store, self.jar, self.digest, self.pins), kept)  # once
        with self.assertRaises(artifacts.StoreError):
            artifacts.put(self.store, self.jar, "0" * 64)

    def test_an_index_keeps_the_first_bytes_and_records_a_disagreement(self):
        artifacts.record(self.store, "demo", "t1", "c1", {"mod.jar": {"sha256": "a", "job": "build", "run": "r1"}})
        artifacts.record(self.store, "demo", "t1", "c2", {"mod.jar": {"sha256": "b", "job": "build", "run": "r2"}})
        index = artifacts.find(self.store, "demo", "t1")
        self.assertEqual(index["artifacts"]["mod.jar"]["sha256"], "a")
        self.assertEqual(index["commits"], ["c1", "c2"])
        self.assertEqual(index["disagreements"][0]["other"], "b")
        self.assertIsNone(artifacts.find(self.store, "demo", "t2"))

    def test_a_file_changed_outside_workbench_is_refused(self):
        kept = artifacts.put(self.store, self.jar, self.digest)
        kept.write_bytes(b"tampered")
        with self.assertRaises(artifacts.StoreError) as raised:
            artifacts.path_of(self.store, self.digest, "mod.jar")
        self.assertIn("changed outside Workbench", str(raised.exception))

    def test_seeded_bytes_say_where_they_came_from(self):
        note = artifacts.seed(self.store, self.jar, "lib:mod@c1", "a bundle", "no container builds it", self.pins)
        self.assertEqual((note["sha256"], note["source"]), (self.digest, "a bundle"))
        self.assertTrue((self.store / "seeded" / f"{self.digest}.json").is_file())

    def build(self):
        artifacts.put(self.store, self.jar, self.digest)
        artifacts.record(self.store, "demo", "t1", "c1", {"mod.jar": {"sha256": self.digest, "job": "build"}})

    def test_export_then_import_carries_builds_and_seeded_files_to_another_store(self):
        self.build()
        other = self.base / "other.jar"
        other.write_bytes(b"adapter")
        artifacts.seed(self.store, other, "lib:adapter@c0", "a bundle", "no container builds it")
        carried, there = self.base / "carried", self.base / "there"
        counts = artifacts.export(self.store, carried, "demo")
        self.assertEqual(counts, {"entries": 1, "files": 1, "seeded": 1})
        counts = artifacts.import_from(there, carried, self.pins)
        self.assertEqual(counts, {"files": 1, "seeded": 1})
        self.assertEqual(artifacts.find(there, "demo", "t1")["artifacts"]["mod.jar"]["sha256"], self.digest)
        self.assertEqual(artifacts.path_of(there, self.digest, "mod.jar").read_bytes(), b"one")
        self.assertTrue((self.pins / self.digest).is_file())

    def test_import_refuses_a_file_that_is_not_its_hash(self):
        self.build()
        carried = self.base / "carried"
        artifacts.export(self.store, carried)
        (carried / "files" / self.digest / "mod.jar").write_bytes(b"tampered")
        with self.assertRaises(artifacts.StoreError):
            artifacts.import_from(self.base / "there", carried)

    def test_a_verdict_is_kept_on_the_build(self):
        self.build()
        artifacts.note_verified(self.store, "demo", "t1", {"reproduced": True, "differ": []})
        self.assertTrue(artifacts.find(self.store, "demo", "t1")["verified"]["reproduced"])
        self.assertEqual([e["tree"] for e in artifacts.entries(self.store, "demo")], ["t1"])


if __name__ == "__main__":
    unittest.main()
