"""The Store: a file is kept by its SHA-256 and never replaced, an index names what a tree built, two builds of one
tree that differ are recorded instead of replaced, a file changed outside Workbench is refused, and seeded bytes
carry where they came from. What a crash leaves behind (a record that exists and is empty, a file cut short) is set
aside and written again, never left to refuse every later write."""

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness  # noqa: E402,F401

from wb_core import artifacts, filelock, landing, passes, setfile  # noqa: E402


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


WRITER = """import sys, threading
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from wb_core import artifacts
store, writer = Path(sys.argv[2]), sys.argv[3]
errors = []
def write(thread):
    try:
        for n in range(10):
            name = f"{writer}-{thread}-{n}"
            artifacts.put(store, Path(sys.argv[4]), None, Path(sys.argv[5]))  # one file, every writer at once
            artifacts.record(store, "demo", "t1", f"c-{name}", {f"{name}.jar": {"sha256": "a", "job": "build"}})
    except BaseException as error:  # a thread's error must fail the writer, not only lose its commits
        errors.append(f"{type(error).__name__}: {error}")
threads = [threading.Thread(target=write, args=(t,)) for t in range(4)]
for t in threads: t.start()
for t in threads: t.join()
if errors:
    sys.exit("; ".join(errors))
"""


class Concurrent(unittest.TestCase):
    """Two runs of one project write the Store at once: every commit and artifact each run records is kept."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.base = Path(self.folder.name)
        self.store = self.base / "store"

    def tearDown(self):
        self.folder.cleanup()

    def test_writers_in_several_processes_and_threads_lose_nothing(self):
        import subprocess
        core = str(Path(artifacts.__file__).resolve().parents[1])
        jar = self.base / "mod.jar"
        jar.write_bytes(b"the same bytes")
        writers = [subprocess.Popen([sys.executable, "-c", WRITER, core, str(self.store), f"w{n}", str(jar),
                                     str(self.base / "pins")], stderr=subprocess.PIPE, text=True) for n in range(4)]
        for writer in writers:
            _, err = writer.communicate(timeout=120)
            self.assertEqual(writer.returncode, 0, err)
        index = artifacts.find(self.store, "demo", "t1")
        self.assertEqual(len(index["commits"]), 4 * 4 * 10)
        self.assertEqual(len(index["artifacts"]), 4 * 4 * 10)
        left = sorted(p.name for p in self.store.rglob("*") if p.is_file() and p.suffix in (".lock", ".part"))
        self.assertEqual(left, [])  # no lock held and no part left behind
        digest = hashlib.sha256(b"the same bytes").hexdigest()
        self.assertEqual(artifacts.path_of(self.store, digest, "mod.jar").read_bytes(), b"the same bytes")

    def test_a_lock_whose_holder_died_is_broken(self):
        import os
        import time
        lock = self.store / "index" / "demo" / "t1.json.lock"
        lock.parent.mkdir(parents=True)
        lock.write_text("a holder that was killed\n", encoding="utf-8")
        old = time.time() - artifacts.INDEX_LOCK_STALE - 5
        os.utime(lock, (old, old))
        artifacts.record(self.store, "demo", "t1", "c1", {"mod.jar": {"sha256": "a"}})
        self.assertEqual(artifacts.find(self.store, "demo", "t1")["commits"], ["c1"])
        self.assertFalse(lock.exists())

    def test_an_entry_that_cannot_be_read_is_kept_aside_and_begun_again(self):
        entry = self.store / "index" / "demo" / "t1.json"
        entry.parent.mkdir(parents=True)
        for damaged in ("{not json", "", "[1, 2]"):  # cut short, left empty by a crash, not an entry at all
            with self.subTest(damaged=damaged):
                entry.write_text(damaged, encoding="utf-8")
                artifacts.record(self.store, "demo", "t1", "c1", {"mod.jar": {"sha256": "a"}})
                now = artifacts.find(self.store, "demo", "t1")
                self.assertEqual((now["commits"], now["artifacts"]), (["c1"], {"mod.jar": {"sha256": "a"}}))
                aside = entry.parent / now["recovered"]["from"]
                self.assertEqual(aside.read_text(encoding="utf-8"), damaged)  # the damage is kept, never overwritten
                entry.unlink()
        self.assertEqual(sorted(p.name for p in entry.parent.glob("*.json")), [])  # nothing aside reads as an entry


class Damaged(unittest.TestCase):
    """What a crash between a write and the disk leaves: the name exists and its bytes are not the record."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.store = Path(self.folder.name) / "store"
        self.addCleanup(self.folder.cleanup)

    def test_a_write_is_whole_and_on_the_disk_and_leaves_no_part(self):
        target = self.store / "a" / "b.json"
        filelock.write_json(target, {"x": 1})
        filelock.write_whole(target, b'{"x": 2}')
        self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"x": 2})
        self.assertEqual([p.name for p in target.parent.iterdir()], ["b.json"])
        with open(target, "rb") as stream:
            filelock.sync(stream.fileno())  # a descriptor opened for reading may be synced on any system that can

    def test_a_pass_or_gate_record_left_empty_is_written_again_and_a_whole_one_stands(self):
        for write, path in ((lambda: passes.write(self.store, "demo", "t1", "build", "k1", {"run": "r2"}),
                             passes.path(self.store, "demo", "t1", "build", "k1")),
                            (lambda: passes.write_gate(self.store, "demo", "t1", {"run": "r2"}),
                             passes.gate_path(self.store, "demo", "t1"))):
            with self.subTest(record=path.parent.name):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"")  # the name a crash left, with none of its bytes
                write()
                self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["run"], "r2")
                self.assertEqual(len(list(path.parent.glob(path.name + ".damaged-*"))), 1)
                before = path.read_bytes()
                write()  # a whole record is never replaced
                self.assertEqual(path.read_bytes(), before)
        self.assertEqual(passes.passed(self.store, "demo", "t1", {"build": "k1"}), {"build"})
        self.assertEqual(len(passes.records(self.store, "demo")), 1)  # what was set aside is no record

    def test_a_store_file_cut_short_is_set_aside_when_its_bytes_come_again(self):
        source = Path(self.folder.name) / "mod.jar"
        source.write_bytes(b"the whole jar")
        digest = hashlib.sha256(b"the whole jar").hexdigest()
        kept = self.store / "files" / digest / "mod.jar"
        kept.parent.mkdir(parents=True)
        kept.write_bytes(b"the who")
        with self.assertRaises(artifacts.StoreError):  # a read still refuses bytes that are not their name
            artifacts.path_of(self.store, digest, "mod.jar")
        self.assertEqual(artifacts.put(self.store, source, digest), kept)
        self.assertEqual(kept.read_bytes(), b"the whole jar")
        self.assertEqual(artifacts.path_of(self.store, digest, "mod.jar"), kept)
        self.assertEqual([p.read_bytes() for p in kept.parent.glob("mod.jar.damaged-*")], [b"the who"])

    def test_a_set_export_left_empty_is_written_again_and_another_set_is_still_refused(self):
        folder = self.store / "sets"
        (folder / "r1").mkdir(parents=True)
        (folder / "r1" / "set.json").write_text("", encoding="utf-8")
        text = '{"schema": 1, "projects": {}}\n'
        setfile.export(folder, "r1", text)
        self.assertEqual((folder / "r1" / "set.json").read_text(encoding="utf-8"), text)
        with self.assertRaises(setfile.SetError):
            setfile.export(folder, "r1", '{"schema": 1, "projects": {"x": {}}}\n')
        setfile.write_head(folder, "a" * 40)
        self.assertEqual(setfile.read_head(folder), "a" * 40)

    def test_a_landing_record_is_written_whole(self):
        record = landing.write(self.store, {"id": "l1", "state": "prepared"})
        self.assertEqual(landing.read(self.store, "l1")["updated"], record["updated"])
        self.assertEqual([p.name for p in landing.folder(self.store).iterdir()], ["l1.json"])


if __name__ == "__main__":
    unittest.main()
