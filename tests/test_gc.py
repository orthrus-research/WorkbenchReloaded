"""wb gc: a dry list is exactly what --yes <digest> removes; a list that changed is refused and removes nothing; the
roots (a pass record, a kept session and the bytes its launch file names, a byte a set revision names, seeded and
indexed files, a legacy instance not frozen, or frozen and changed since, or the only copy of its world or of
bytes a root names) are never listed; a frozen legacy instance as its manifest recorded it is; a removal never follows a link; lab-home items wait for the GPU
lease and the nightly run never touches them; the daemon's classes come over the API."""

import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import Daemon  # noqa: E402

from wb import cli, gc, platform  # noqa: E402
from wb_core import retention  # noqa: E402
from wb_host import lease as leases  # noqa: E402

DAYS = 30


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else json.dumps(data).encode())
    return path


def age(path: Path, days: float):
    """Sets every modification time in ``path`` (and the folder itself) ``days`` back."""
    when = time.time() - days * 86400
    for directory, folders, files in os.walk(path):
        folders[:] = [f for f in folders if not platform.is_link(Path(directory) / f)]  # a link's target is not ours
        for name in files + folders:
            os.utime(Path(directory) / name, (when, when))
    os.utime(path, (when, when))


class Machine:
    """One machine's folders, every class with a candidate and every root with something it keeps."""

    def __init__(self):
        self.base = Path(tempfile.mkdtemp(prefix="wb-gc-"))
        out = self.base / "out"
        self.store, self.records, self.sets = out / "store", out / "ci" / "records", out / "sets"
        self.lab, self.releases = self.base / "Lab", self.base / "releases"
        self.blob = {}
        for name in ("indexed", "set-named", "seeded", "kept", "landed", "orphan", "fresh-orphan", "held"):
            data = f"bytes of {name}".encode()
            self.blob[name] = sha(data)
            write(self.store / "files" / sha(data) / f"{name}.jar", data)
        write(self.store / "index" / "riven" / ("a" * 40 + ".json"),
              {"project": "riven", "tree": "a" * 40, "commits": ["b" * 40],
               "artifacts": {"indexed.jar": {"sha256": self.blob["indexed"], "job": "build", "run": "r1"}}})
        write(self.store / "seeded" / f"{self.blob['seeded']}.json",
              {"sha256": self.blob["seeded"], "name": "seeded.jar", "as": "x:y@c", "source": "s", "reason": "r"})
        write(self.store / "landings" / "l1.json", {"state": "landed", "set": {"third": {"t": {
            "sha256": self.blob["landed"]}}}})
        write(self.sets / ("c" * 40) / "set.json",
              {"schema": 1, "projects": {}, "platform": {"jdk": {"8": {"windows": {"sha256": self.blob["set-named"]}}}},
               "holds": {"h": {"artifacts": {"x": {"sha256": self.blob["held"]}}}}})
        # CI records: a pass, a failed run, a failed run a cache entry names, a fresh failure, a stand-in run.
        project = self.records / "riven"
        self.pass_run = write(project / "pass" / "summary.json", {"jobs": {"build": {"status": "passed"}},
                                                                  "treesChanged": False}).parent
        write(self.pass_run / "build" / "artifacts" / "indexed.jar", b"bytes of indexed")
        write(self.pass_run / "build" / "artifacts" / "other.jar", b"not in the Store")
        self.failed = write(project / "failed" / "summary.json", {"jobs": {"build": {"status": "failed"}}}).parent
        write(self.failed / "build" / "01-build.log", b"x" * 1000)
        self.cached = write(project / "cached" / "summary.json", {"jobs": {"build": {"status": "failed"}}}).parent
        write(project / "cache" / "build-k.json", {"job": "build", "key": "k", "runId": "cached"})
        self.unfinished = write(project / "unfinished" / "build" / "01-build.log", b"y").parent.parent
        self.fresh = write(project / "fresh" / "summary.json", {"jobs": {"build": {"status": "failed"}}}).parent
        self.stand_in = write(project / "stand-ins" / "s1" / "summary.json",
                              {"jobs": {"build": {"status": "passed"}}}).parent
        self.stand_in_cache = write(project / "stand-ins" / "cache" / "build-j.json", {"runId": "s2"}).parent
        self.stand_in_cached = write(project / "stand-ins" / "s2" / "summary.json", {"jobs": {}}).parent
        # A kept session: its folder and the bytes and the instance its files name.
        self.session = write(out / "sessions" / "s1" / "kept.json", {"reason": "baseline"}).parent
        write(self.session / "launch.json", {"members": [{"sha256": self.blob["kept"]},
                                                         {"sha256": sha(b"the only copy")}]})
        write(self.session / "session.json", {"instance": "kept-instance"})
        # The lab home: instances lab.build made, a legacy one, templates and blobs.
        instances = self.lab / "instances"
        templates = instances / ".store" / "templates"
        self.old_instance = write(instances / "old" / "instance.json", {"key": "t-used", "launch": {"sha256": "0"}}).parent
        write(self.old_instance / ".wb-instance.json", {"template": str(templates / "t-used")})
        self.kept_instance = write(instances / "kept-instance" / "instance.json", {"key": "t-used"}).parent
        self.legacy = write(instances / "legacy" / "options.txt", b"made by hand").parent
        self.used_template = write(templates / "t-used" / "template.json", {"links": {"a": "1" * 64}}).parent
        self.unused_template = write(templates / "t-unused" / "template.json", {"links": {}}).parent
        write(instances / ".store" / "blobs" / "11" / ("1" * 64), b"named")
        self.orphan_blob = write(instances / ".store" / "blobs" / "22" / ("2" * 64), b"orphan")
        # Old-route instances lab.freeze froze (manifests written below, after the ageing): one as recorded, one
        # changed since, one whose world was not imported, one holding the only copy of bytes a kept session names.
        old_route = self.lab / "cubic-standalone"
        self.frozen = write(old_route / "frozen" / "server" / "options.txt", b"frozen").parent.parent
        write(self.frozen / "client" / "mods" / "Old-1.0.jar", b"bytes of indexed")
        self.changed = write(old_route / "changed" / "server" / "options.txt", b"before").parent.parent
        self.unimported = write(old_route / "unimported" / "server" / "world" / "level.dat", b"w").parent.parent.parent
        self.only_copy = write(old_route / "only-copy" / "client" / "mods" / "Only.jar", b"the only copy"
                               ).parent.parent.parent
        # Releases: three, the newest in service, the one before it kept.
        for index, name in enumerate(("aaaaaaaaaaaa", "bbbbbbbbbbbb", "cccccccccccc")):
            write(self.releases / name / "RELEASE.json", {"made_at": 1000 + index})
        platform.make_link(self.releases / "current", self.releases / "cccccccccccc")
        for path in (out, self.lab, *[p for p in self.releases.iterdir() if not platform.is_link(p)]):
            age(path, 40)
        age(self.fresh, 1)
        os.utime(self.store / "files" / self.blob["fresh-orphan"], None)
        os.utime(self.store / "files" / self.blob["fresh-orphan"] / "fresh-orphan.jar", None)
        for instance in (self.frozen, self.changed, self.unimported, self.only_copy):
            world = ({"path": str(instance / "server" / "world"), "label": None, "why": "no world store"}
                     if instance == self.unimported else {"path": None, "label": None, "why": "no world"})
            self.freeze(instance, world)
        write(self.changed / "server" / "options.txt", b"after the freeze")
        for directory, _, names in os.walk(self.frozen):  # lab.freeze marks every file read-only
            for name in names:
                os.chmod(Path(directory) / name, 0o444)

    def freeze(self, instance: Path, world: dict):
        """The manifest lab.freeze writes: every file by size, time and SHA-256, its world and kept session."""
        files = {}
        for directory, _, names in os.walk(instance):
            for name in names:
                path = Path(directory) / name
                st = path.stat()
                files[path.relative_to(instance).as_posix()] = {
                    "size": st.st_size, "mtime_ns": st.st_mtime_ns, "read_only": False, "marked": True,
                    "sha256": sha(path.read_bytes())}
        write(self.lab / "legacy" / instance.name / "frozen.json",
              {"schema": "wb.lab.frozen/1", "id": instance.name, "instance": str(instance), "name": instance.name,
               "state": "frozen", "frozen": "2026-11-24T06:00:00", "files": files, "links": [],
               "world": world, "session": {"session": f"legacy-{instance.name}", "kept": True}})

    def places(self, **changes) -> gc.Places:
        values = dict(store=self.store, records=self.records, sets=self.sets, out=self.store.parent,
                      lab_home=self.lab, releases=self.releases, previous_release="bbbbbbbbbbbb",
                      lease_file=self.lab / "lease" / "gpu.lock", coordination=self.lab / "coordination", days=DAYS)
        values.update(changes)
        return gc.Places(**values)

    def files(self) -> set[str]:
        return {str(Path(d) / n) for d, _, names in os.walk(self.base) for n in names}


class Gc(unittest.TestCase):
    def setUp(self):
        self.machine = Machine()
        self.addCleanup(shutil.rmtree, self.machine.base, True)
        self._poll = gc.LEASE_POLL
        gc.LEASE_POLL = 0.05
        self.addCleanup(setattr, gc, "LEASE_POLL", self._poll)

    def listed(self, **kw) -> dict[str, str]:
        return {i.path: i.cls for i in gc.listing(self.machine.places(), **kw).items}

    def test_pass_and_gate_records_keep_what_they_name(self):
        from wb_core import passes
        m = self.machine
        before = self.listed()
        self.assertIn(str(m.store / "files" / m.blob["orphan"]), before)
        self.assertIn(str(m.failed), before)
        # A pass record names the orphan's bytes and the failed run's folder (its job id); a gate record stands too.
        passes.write(m.store, "riven", "d" * 40, "build", "e" * 64,
                     {"run": m.failed.name, "artifacts": {"orphan.jar": {"sha256": m.blob["orphan"]}}})
        passes.write_gate(m.store, "riven", "d" * 40, {"checks": {"build": "e" * 64}})
        age(m.store / "passes", 400)
        after = self.listed()
        self.assertNotIn(str(m.store / "files" / m.blob["orphan"]), after)
        self.assertNotIn(str(m.failed), after)
        self.assertFalse([path for path in after if "passes" in Path(path).parts or "gate" in Path(path).parts])
        self.assertEqual(set(before) - set(after), {str(m.store / "files" / m.blob["orphan"]), str(m.failed)})

    def test_every_class_lists_its_candidates_and_nothing_else(self):
        m = self.machine
        self.assertEqual(self.listed(), {
            str(m.failed): "ci.runs", str(m.unfinished): "ci.runs", str(m.stand_in): "ci.runs",
            str(m.pass_run / "build" / "artifacts" / "indexed.jar"): "ci.copies",
            str(m.store / "files" / m.blob["orphan"]): "store.files",
            str(m.releases / "aaaaaaaaaaaa"): "releases",
            str(m.old_instance): "lab.instances", str(m.unused_template): "lab.templates",
            str(m.orphan_blob): "lab.blobs", str(m.frozen): "lab.legacy"})

    def test_a_dry_list_is_exactly_what_yes_removes(self):
        m = self.machine
        before = m.files()
        shown = gc.listing(m.places())
        done = gc.apply(m.places(), shown.digest, say=lambda _: None)
        self.assertEqual(sorted(done["removed"]), sorted(i.path for i in shown.items))
        self.assertEqual(done["refused"], [])
        gone = {f for f in before if any(f == i.path or f.startswith(i.path + os.sep) for i in shown.items)}
        self.assertTrue(gone)
        self.assertEqual(m.files() & before, before - gone)  # nothing else went (the lease adds its own files)
        self.assertEqual(gc.listing(m.places()).items, [])

    def test_a_list_that_changed_is_refused_and_nothing_is_removed(self):
        m = self.machine
        shown = gc.listing(m.places())
        before = m.files()
        failed_log = m.failed / "build" / "02-more.log"
        write(failed_log, b"written after the list was shown")
        age(m.failed, 40)
        with self.assertRaises(gc.GcError):
            gc.apply(m.places(), shown.digest, say=lambda _: None)
        self.assertEqual(m.files(), before | {str(failed_log)})
        with self.assertRaises(gc.GcError):
            gc.apply(m.places(), "", say=lambda _: None)

    def test_roots_are_never_listed(self):
        m = self.machine
        listed = self.listed()
        for path in (m.pass_run, m.pass_run / "summary.json"):  # its artifact copies may go, never the record
            self.assertFalse(any(p == str(path) or str(path).startswith(p + os.sep) for p in listed), path)
        protected = [m.cached, m.fresh, m.session, m.kept_instance, m.stand_in_cache, m.stand_in_cached,
                     m.legacy, m.changed, m.unimported, m.only_copy, m.lab / "legacy",
                     m.used_template, m.sets / ("c" * 40),
                     m.releases / "bbbbbbbbbbbb", m.releases / "cccccccccccc"]
        protected += [m.store / "files" / m.blob[n] for n in ("indexed", "set-named", "seeded", "kept", "landed",
                                                              "fresh-orphan", "held")]
        for path in protected:
            self.assertFalse(any(p == str(path) or p.startswith(str(path) + os.sep) or str(path).startswith(p + os.sep)
                                 for p in listed), path)
        self.assertNotIn(str(m.pass_run / "build" / "artifacts" / "other.jar"), listed)  # not in the Store

    def test_a_kept_session_named_by_its_launch_files_sha256_keeps_an_instance(self):
        m = self.machine
        launch = (m.session / "launch.json").read_bytes()
        write(m.old_instance / "instance.json", {"key": "t-used", "launch": {"sha256": sha(launch)}})
        age(m.old_instance, 40)
        self.assertNotIn(str(m.old_instance), self.listed())

    def test_removal_never_follows_a_link(self):
        m = self.machine
        outside = write(m.base / "evidence" / "frame.png", b"evidence")
        platform.make_link(m.failed / "linked", outside.parent)
        age(m.failed, 40)
        shown = gc.listing(m.places(), only={"ci.runs"})
        self.assertIn(str(m.failed), [i.path for i in shown.items])
        gc.apply(m.places(), shown.digest, only={"ci.runs"}, say=lambda _: None)
        self.assertFalse(m.failed.exists())
        self.assertEqual(outside.read_bytes(), b"evidence")

    def test_lab_home_items_wait_for_the_gpu_lease(self):
        m = self.machine
        shown = gc.listing(m.places(), only={"lab.templates"})
        self.assertEqual([i.path for i in shown.items], [str(m.unused_template)])
        self.addCleanup(setattr, leases, "COOLDOWN", leases.COOLDOWN)
        leases.COOLDOWN = 0  # the lease's own wait after a release is not what this proves
        game = leases.Lease(m.lab / "lease" / "gpu.lock", {"lane": "game", "pid": os.getpid()})
        said, done = [], {}
        worker = threading.Thread(target=lambda: done.update(gc.apply(m.places(), shown.digest, only={"lab.templates"},
                                                                       say=said.append)))
        worker.start()
        time.sleep(0.5)
        self.assertTrue(m.unused_template.exists())  # a game holds the lease: nothing removed yet
        self.assertTrue(any("held by game" in s for s in said), said)
        game.close()
        worker.join(30)
        self.assertEqual(done["removed"], [str(m.unused_template)])
        self.assertFalse(m.unused_template.exists())

    def test_the_nightly_run_removes_only_unattended_classes(self):
        m = self.machine
        done = gc.nightly(m.places())
        lab = {str(m.old_instance), str(m.unused_template), str(m.orphan_blob)}
        self.assertFalse(lab & set(done["removed"]))
        for path in lab:
            self.assertTrue(Path(path).exists())
        self.assertIn(str(m.failed), done["removed"])
        self.assertFalse(m.failed.exists())
        self.assertEqual({r.name for r in gc.RULES if r.unattended} & gc.LAB, set())

    def test_an_unreadable_set_export_stops_the_list(self):
        m = self.machine
        export = m.sets / ("c" * 40) / "set.json"
        export.unlink()
        export.mkdir()  # cannot be read as a file
        with self.assertRaises(gc.GcError):
            gc.listing(m.places())

    def test_the_old_verb_names_gc(self):
        self.assertEqual(cli.main(["prune", "--yes"]), 2)


class DaemonClasses(unittest.TestCase):
    def setUp(self):
        self.daemon = Daemon().start()
        self.addCleanup(self.daemon.stop)

    def test_the_daemon_lists_its_classes_and_removes_only_what_is_still_listed(self):
        state = self.daemon.state
        old = write(state / "logs" / "old.log.gz", b"rolled")
        age(old.parent, 40)
        call = lambda method, body: self.daemon.client.request(method, "/v1/gc", body)  # noqa: E731
        answer = call("GET", None)
        self.assertEqual({r["name"] for r in answer["rules"]}, {r.name for r in retention.DAEMON_RULES})
        self.assertEqual([i["path"] for i in answer["items"]], [str(old)])
        stale = dict(answer["items"][0], bytes=1)
        self.assertEqual(call("POST", {"remove": [stale]})["refused"], [str(old)])
        self.assertTrue(old.exists())
        shown = gc.listing(gc.Places(), daemon=call, only={"logs.archives"})
        self.assertIsNone(shown.daemon)
        done = gc.apply(gc.Places(), shown.digest, daemon=call, only={"logs.archives"}, say=lambda _: None)
        self.assertEqual(done["removed"], [str(old)])
        self.assertFalse(old.exists())


if __name__ == "__main__":
    unittest.main()
