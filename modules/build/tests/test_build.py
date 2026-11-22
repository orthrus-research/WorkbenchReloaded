"""build: freezes that never change what a label holds, staging that places a locked set whole or not at all, and
archives whose bytes depend on the tree alone."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO / "tests"))
from harness import Daemon  # noqa: E402

sys.path.insert(0, str(REPO / "modules" / "build"))
from wb_build import archive as archives  # noqa: E402
from wb_build import checkout as checkouts  # noqa: E402
from wb_build import commands  # noqa: E402
from wb_build.files import set_digest  # noqa: E402
from wb_core.module import Context, EnvironmentProblem, ParamError  # noqa: E402


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


def context(folder: Path, command: str) -> Context:
    """A real handler context outside the daemon, for faults a worker process cannot be made to have."""
    return Context({"job": "direct", "command": command, "module": "build", "work": str(folder / "work"),
                    "out": str(folder / "out")}, lambda event: None)


def checkout(folder: Path) -> str:
    """A git checkout with one tracked file and two build outputs git ignores; returns its commit."""
    folder.mkdir(parents=True)
    git(folder, "init", "-q", "-b", "main")
    (folder / "src.txt").write_text("one", encoding="utf-8")
    (folder / ".gitignore").write_text("build/\n", encoding="utf-8")
    git(folder, "add", "-A")
    git(folder, "commit", "-q", "-m", "one")
    libs = folder / "build" / "libs"
    libs.mkdir(parents=True)
    (libs / "lib.jar").write_bytes(b"binary one")
    (libs / "lib-sources.jar").write_bytes(b"sources one")
    return git(folder, "rev-parse", "HEAD")


class Freeze(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = Daemon().start()
        cls.c = cls.d.client
        cls.repo = cls.d.data / "lib"
        cls.commit = checkout(cls.repo)
        cls.a = cls.repo / "build" / "libs" / "lib.jar"
        cls.b = cls.repo / "build" / "libs" / "lib-sources.jar"
        cls.store = cls.d.out / "store"

    @classmethod
    def tearDownClass(cls):
        cls.d.stop()

    def freeze(self, **params):
        return self.c.submit("build.freeze", {"store": str(self.store), **params}).result(timeout=120)

    def manifest(self) -> dict:
        return json.loads((self.store / "manifest.json").read_text(encoding="utf-8"))

    def test_a_label_is_named_by_its_commit_and_never_changes(self):
        first = self.freeze(artifacts=[str(self.a), str(self.b)], name="lib", repo=str(self.repo))
        self.assertEqual(first.outcome, "ok", first.summary)
        label = f"lib-{self.commit[:12]}"
        self.assertEqual(first.data.label, label)
        self.assertEqual([a["state"] for a in first.data.artifacts], ["frozen", "frozen"])
        frozen = self.store / label / "lib.jar"
        self.assertEqual(frozen.read_bytes(), b"binary one")
        self.assertEqual(frozen.with_name("lib.jar.sha256").read_text(encoding="utf-8"),
                         f"{sha(b'binary one')}  lib.jar\n")
        entry = self.manifest()["labels"][label]["artifacts"]["lib.jar"]
        self.assertEqual((entry["sha256"], entry["commit"], entry["dirty"]), (sha(b"binary one"), self.commit, False))
        again = self.freeze(artifacts=[str(self.a), str(self.b)], name="lib", repo=str(self.repo))
        self.assertEqual(again.outcome, "ok", again.summary)
        self.assertEqual([a["state"] for a in again.data.artifacts], ["present", "present"])
        self.a.write_bytes(b"binary two")  # the same commit built again, to other bytes
        try:
            refused = self.freeze(artifacts=[str(self.a), str(self.b)], name="lib", repo=str(self.repo))
            self.assertEqual(refused.outcome, "finding", refused.summary)
            self.assertIn("never changes", refused.summary)
            self.assertEqual(refused.data.refused[0]["name"], "lib.jar")
            self.assertEqual(frozen.read_bytes(), b"binary one")
            self.assertEqual(self.manifest()["labels"][label]["artifacts"]["lib.jar"]["sha256"], sha(b"binary one"))
        finally:
            self.a.write_bytes(b"binary one")

    def test_uncommitted_changes_name_the_bytes_too(self):
        (self.repo / "src.txt").write_text("changed", encoding="utf-8")
        try:
            result = self.freeze(artifacts=[str(self.a)], name="lib", repo=str(self.repo))
        finally:
            git(self.repo, "checkout", "--", "src.txt")
        self.assertEqual(result.outcome, "ok", result.summary)
        together = set_digest({"lib.jar": sha(b"binary one")})
        self.assertTrue(result.data.dirty)
        self.assertEqual(result.data.label, f"lib-{self.commit[:12]}-dirty-{together[:12]}")
        self.assertTrue(self.manifest()["labels"][result.data.label]["artifacts"]["lib.jar"]["dirty"])

    def test_a_named_label_gathers_builds_and_a_commit_stands_in_for_the_checkout(self):
        other = self.d.data / "other.jar"
        other.write_bytes(b"other")
        one = self.freeze(artifacts=[str(self.a)], label="candidate-01", commit=self.commit)
        two = self.freeze(artifacts=[str(other)], label="candidate-01", commit="ab" * 20, dirty=True)
        self.assertEqual((one.outcome, two.outcome), ("ok", "ok"), (one.summary, two.summary))
        held = self.manifest()["labels"]["candidate-01"]["artifacts"]
        self.assertEqual(sorted(held), ["lib.jar", "other.jar"])
        self.assertEqual((held["other.jar"]["commit"], held["other.jar"]["dirty"]), ("ab" * 20, True))
        self.assertEqual(held["lib.jar"]["commit"], self.commit)
        unnamed = self.freeze(artifacts=[str(other)], name="x")  # the default label needs a commit
        self.assertEqual(unnamed.outcome, "usage", unnamed.summary)
        both = self.freeze(artifacts=[str(other)], name="x", repo=str(self.repo), commit=self.commit)
        self.assertEqual(both.outcome, "usage", both.summary)
        odd = self.freeze(artifacts=[str(other)], label="{name.__class__}", name="x")
        self.assertEqual(odd.outcome, "usage", odd.summary)

    def test_concurrent_freezes_keep_every_label(self):
        root = scratch(self, "wb-freeze-")
        store = root / "store"
        files = []
        for index in range(8):
            path = root / f"part{index}.jar"
            path.write_bytes(f"artifact {index}".encode())
            files.append(path)
        errors = []

        def one(index):
            try:
                commands.freeze(context(root / f"job{index}", "build.freeze"), [str(files[index])], str(store),
                                f"part{index}", "{name}-{commit:.12}", commit=f"{index + 1:040x}")
            except Exception as error:  # noqa: BLE001 - reported below
                errors.append(error)
        threads = [threading.Thread(target=one, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        self.assertEqual(errors, [])
        labels = json.loads((store / "manifest.json").read_text(encoding="utf-8"))["labels"]
        self.assertEqual(len(labels), 8)  # no freeze lost another's label
        self.assertFalse([p.name for p in store.iterdir() if p.name.endswith(".part")])


class Stage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = Daemon().start()
        cls.c = cls.d.client
        cls.lib = cls.d.data / "lib"
        cls.commit = checkout(cls.lib)
        cls.tree = git(cls.lib, "rev-parse", "HEAD^{tree}")
        cls.into = cls.d.out / "consumer"
        cls.count = 0

    @classmethod
    def tearDownClass(cls):
        cls.d.stop()

    def setUp(self):
        inputs = self.into / "inputs"
        inputs.mkdir(parents=True, exist_ok=True)
        for name in ("lib.jar", "lib-sources.jar"):
            (inputs / name).write_bytes(b"previous")

    def lock(self, **changes) -> str:
        data = {"repository": "https://example.invalid/lib.git", "commit": self.commit, "tree": self.tree,
                "artifacts": [{"artifact": "build/libs/lib.jar", "sha256": sha(b"binary one"),
                               "destination": "inputs/lib.jar"},
                              {"artifact": "build/libs/lib-sources.jar", "sha256": sha(b"sources one"),
                               "destination": "inputs/lib-sources.jar"}]}
        data.update(changes)
        Stage.count += 1
        path = self.d.data / f"lock-{Stage.count}.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return str(path)

    def stage(self, lock, **params):
        return self.c.submit("build.stage", {"lock": lock, "checkout": str(self.lib), "into": str(self.into),
                                             **params}).result(timeout=120)

    def staged(self) -> list[bytes]:
        return [(self.into / "inputs" / n).read_bytes() for n in ("lib.jar", "lib-sources.jar")]

    def test_stages_the_locked_set_from_a_clean_checkout(self):
        lock = self.lock()
        result = self.stage(lock)
        self.assertEqual(result.outcome, "ok", result.summary)
        self.assertEqual([s["state"] for s in result.data.staged], ["staged", "staged"])
        self.assertEqual(self.staged(), [b"binary one", b"sources one"])
        again = self.stage(lock)
        self.assertEqual([s["state"] for s in again.data.staged], ["unchanged", "unchanged"])
        self.assertEqual(git(self.lib, "status", "--porcelain"), "")  # the checkout was only read

    def test_a_checkout_that_moved_or_changed_stages_nothing(self):
        lock = self.lock()
        (self.lib / "src.txt").write_text("edited", encoding="utf-8")
        try:
            dirty = self.stage(lock)
            self.assertEqual(dirty.outcome, "finding", dirty.summary)
            self.assertIn("tracked modification", dirty.summary)
            git(self.lib, "commit", "-q", "-am", "two")
            moved = self.stage(lock)
            self.assertEqual(moved.outcome, "finding", moved.summary)
            self.assertIn("not the locked", moved.summary)
        finally:
            git(self.lib, "reset", "-q", "--hard", self.commit)
        self.assertEqual(self.staged(), [b"previous", b"previous"])
        wrong_tree = self.stage(self.lock(tree="0" * 40))
        self.assertEqual(wrong_tree.outcome, "finding", wrong_tree.summary)

    def test_one_changed_artifact_stages_none(self):
        sources = self.lib / "build" / "libs" / "lib-sources.jar"
        sources.write_bytes(b"sources two")
        try:
            result = self.stage(self.lock())
        finally:
            sources.write_bytes(b"sources one")
        self.assertEqual(result.outcome, "finding", result.summary)
        self.assertEqual(result.data.staged, [])
        self.assertEqual(self.staged(), [b"previous", b"previous"])  # not even the artifact that matched

    def test_locks_are_checked_and_paths_stay_inside(self):
        escape = self.lock(artifacts=[{"artifact": "build/libs/lib.jar", "sha256": sha(b"binary one"),
                                       "destination": "../outside.jar"}])
        self.assertEqual(self.stage(escape).outcome, "usage")
        self.assertFalse((self.d.out / "outside.jar").exists())
        outside = self.lock(artifacts=[{"artifact": "../lib.lock.json", "sha256": "0" * 64, "destination": "x"}])
        self.assertEqual(self.stage(outside).outcome, "usage")
        twice = self.lock(artifacts=[{"artifact": "build/libs/lib.jar", "sha256": sha(b"binary one"),
                                      "destination": "inputs/lib.jar"}] * 2)
        self.assertEqual(self.stage(twice).outcome, "usage")
        typo = self.lock(comit=self.commit)
        self.assertEqual(self.stage(typo).outcome, "usage")
        short = self.lock(commit=self.commit[:12])
        self.assertEqual(self.stage(short).outcome, "usage")
        self.assertEqual(self.staged(), [b"previous", b"previous"])

    def test_a_failed_replacement_restores_every_destination(self):
        root = scratch(self, "wb-stage-")
        lib = root / "lib"
        commit = checkout(lib)
        into = root / "project"
        (into / "inputs").mkdir(parents=True)
        for name in ("lib.jar", "lib-sources.jar"):
            (into / "inputs" / name).write_bytes(b"previous")
        lock = root / "lock.json"
        lock.write_text(json.dumps({"commit": commit, "artifacts": [
            {"artifact": "build/libs/lib.jar", "sha256": sha(b"binary one"), "destination": "inputs/lib.jar"},
            {"artifact": "build/libs/lib.jar", "sha256": sha(b"binary one"), "destination": "fresh/lib.jar"},
            {"artifact": "build/libs/lib-sources.jar", "sha256": sha(b"sources one"),
             "destination": "inputs/lib-sources.jar"}]}), encoding="utf-8")
        real = os.replace

        def held(source, destination):
            if Path(destination).name == "lib-sources.jar" and str(source).endswith(".stage"):
                raise PermissionError("the destination is open in another program")
            return real(source, destination)
        with mock.patch.object(os, "replace", held), self.assertRaises(EnvironmentProblem) as raised:
            commands.stage(context(root / "job", "build.stage"), str(lock), str(lib), str(into))
        self.assertIn("previous bytes", str(raised.exception))
        self.assertEqual((into / "inputs" / "lib.jar").read_bytes(), b"previous")
        self.assertEqual((into / "inputs" / "lib-sources.jar").read_bytes(), b"previous")
        self.assertFalse((into / "fresh").exists())  # a destination that was new is gone again, with its folder
        self.assertEqual(sorted(p.name for p in into.rglob("*") if p.is_file()), ["lib-sources.jar", "lib.jar"])


class Checkouts(unittest.TestCase):
    def test_a_linked_worktree_named_by_a_host_path_is_read_through_the_roots(self):
        root = scratch(self, "wb-checkout-")
        main = root / "main"
        commit = checkout(main)
        linked = root / "linked"
        git(main, "worktree", "add", "-q", "--detach", str(linked), commit)
        named = (linked / ".git").read_text(encoding="utf-8").split(":", 1)[1].strip()
        relative = Path(named).resolve().relative_to(root.resolve()).as_posix()
        (linked / ".git").unlink()  # git may have hidden it, and Windows will not overwrite a hidden file
        (linked / ".git").write_text(f"gitdir: /host/work/{relative}\n", encoding="utf-8")  # as another machine wrote it
        ctx = Context({"job": "direct", "command": "build.stage", "module": "build", "work": str(root / "job" / "work"),
                       "out": str(root / "job" / "out"),
                       "roots": [{"name": "work", "host": "/host/work", "container": str(root), "mode": "ro"}]},
                      lambda event: None)
        found = checkouts.state(ctx, linked, untracked=False)
        self.assertEqual((found.commit, found.dirty), (commit, False))
        sha, _ = checkouts.clone_at(ctx, linked, commit[:12], root / "clone")
        self.assertEqual(sha, commit)
        self.assertEqual((root / "clone" / "src.txt").read_text(encoding="utf-8"), "one")


SETTINGS = "#ifndef SETTINGS\n#define SETTINGS\n#define QUALITY 2 // [1 2 3]\n// #define SHADOWS\n#endif\n"


class Archive(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = Daemon().start()
        cls.c = cls.d.client
        cls.tree = cls.d.data / "pack"
        files = {"shaders/lib/settings.glsl": SETTINGS, "shaders/main.fsh": "void main() {}\n",
                 "docs/readme.md": "# pack\n", "LICENSE": "free\n", "scratch/notes.txt": "mine\n"}
        for relative, text in files.items():
            path = cls.tree / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(text.encode("utf-8"))
        cls.variants = {"high": {"shaders/lib/settings.glsl": {"QUALITY": 3, "SHADOWS": "on"}}}

    @classmethod
    def tearDownClass(cls):
        cls.d.stop()

    def pack(self, out: Path, **params):
        base = {"root": str(self.tree), "name": "pack-1.0", "exclude": ["scratch"]}
        return self.c.submit("build.archive", {**base, **params}, out=str(out)).result(timeout=120)

    def test_the_same_tree_packs_to_the_same_bytes(self):
        one = self.pack(self.d.out / "one", variants=self.variants)
        later = time.time() + 3600
        for path in self.tree.rglob("*"):
            os.utime(path, (later, later))  # only the modification times differ
        two = self.pack(self.d.out / "two", variants=self.variants)
        self.assertEqual((one.outcome, two.outcome), ("ok", "ok"), (one.summary, two.summary))
        self.assertEqual(one.data.sha256, two.data.sha256)
        for name in ("pack-1.0.zip", "pack-1.0.json", "pack-1.0.zip.sha256", "pack-1.0-high.zip", "pack-1.0-high.json"):
            self.assertEqual((self.d.out / "one" / name).read_bytes(), (self.d.out / "two" / name).read_bytes(), name)
        archive = self.d.out / "one" / "pack-1.0.zip"
        self.assertEqual(sha(archive.read_bytes()), one.data.sha256)
        with zipfile.ZipFile(archive) as bundle:
            infos = bundle.infolist()
            self.assertEqual([i.filename for i in infos],
                             ["LICENSE", "docs/readme.md", "shaders/lib/settings.glsl", "shaders/main.fsh"])
            self.assertEqual({i.date_time for i in infos}, {(1980, 1, 1, 0, 0, 0)})
            self.assertEqual({i.external_attr >> 16 for i in infos}, {0o100644})
            self.assertEqual(bundle.read("shaders/lib/settings.glsl").decode(), SETTINGS)
        manifest = json.loads((self.d.out / "one" / "pack-1.0.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["sha256"], one.data.sha256)
        self.assertEqual([f["path"] for f in manifest["files"]], [i.filename for i in infos])
        self.assertEqual((self.d.out / "one" / "pack-1.0.zip.sha256").read_text(encoding="utf-8"),
                         f"{one.data.sha256}  pack-1.0.zip\n")
        with zipfile.ZipFile(self.d.out / "one" / "pack-1.0-high.zip") as bundle:
            frozen = bundle.read("shaders/lib/settings.glsl").decode()
        self.assertIn("#define QUALITY 3 // [1 2 3]\n", frozen)
        self.assertIn("\n#define SHADOWS\n", frozen)
        variant = json.loads((self.d.out / "one" / "pack-1.0-high.json").read_text(encoding="utf-8"))["variant"]
        self.assertEqual((variant["of_sha256"], variant["modified"][0]["path"]),
                         (one.data.sha256, "shaders/lib/settings.glsl"))

    def test_an_existing_name_never_gets_other_bytes(self):
        out = self.d.out / "taken"
        out.mkdir()
        (out / "pack-1.0.zip").write_bytes(b"another archive")
        refused = self.pack(out)
        self.assertEqual(refused.outcome, "finding", refused.summary)
        self.assertEqual((out / "pack-1.0.zip").read_bytes(), b"another archive")
        self.assertFalse((out / "pack-1.0.json").exists())  # nothing was written
        fresh = self.d.out / "fresh"
        first = self.pack(fresh)
        again = self.pack(fresh)
        self.assertEqual((first.outcome, again.outcome), ("ok", "ok"), (first.summary, again.summary))
        self.assertEqual(again.data.written, [])  # a rebuild of the same tree is harmless

    def test_variant_values_are_checked_against_the_file(self):
        out = self.d.out / "variants"
        for values in ({"QUALITY": 4}, {"QUALITY": "on"}, {"SHADOWS": 2}, {"MISSING": "on"}, {"SETTINGS": "off"},
                       {"QUALITY": "1; rm"}):
            result = self.pack(out, variants={"x": {"shaders/lib/settings.glsl": values}})
            self.assertEqual(result.outcome, "usage", (values, result.summary))
        self.assertEqual(self.pack(out, variants={"x": {"absent.glsl": {"A": 1}}}).outcome, "usage")

    def test_stored_entries_and_tracked_files_only(self):
        repo = self.d.data / "tracked"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        (repo / "kept.txt").write_text("kept\n", encoding="utf-8")
        git(repo, "add", "kept.txt")
        git(repo, "commit", "-q", "-m", "kept")
        (repo / "scratch.txt").write_text("not versioned\n", encoding="utf-8")
        params = {"root": str(repo), "name": "t", "tracked": True, "compression": "stored"}
        result = self.c.submit("build.archive", params, out=str(self.d.out / "tracked")).result(timeout=120)
        self.assertEqual(result.outcome, "ok", result.summary)
        with zipfile.ZipFile(self.d.out / "tracked" / "t.zip") as bundle:
            self.assertEqual(bundle.namelist(), ["kept.txt"])
            self.assertEqual(bundle.infolist()[0].compress_type, zipfile.ZIP_STORED)

    def test_links_are_refused(self):
        tree = self.d.data / "linked"
        tree.mkdir()
        (tree / "real.txt").write_text("real\n", encoding="utf-8")
        try:
            os.symlink(tree / "real.txt", tree / "link.txt")
        except (OSError, NotImplementedError):
            self.skipTest("this machine does not let a test make symbolic links")
        result = self.c.submit("build.archive", {"root": str(tree), "name": "l"},
                               out=str(self.d.out / "linked")).result(timeout=120)
        self.assertEqual(result.outcome, "finding", result.summary)
        self.assertIn("link.txt", result.summary)
        allowed = self.c.submit("build.archive", {"root": str(tree), "name": "l", "exclude": ["link.txt"]},
                                out=str(self.d.out / "linked")).result(timeout=120)
        self.assertEqual(allowed.outcome, "ok", allowed.summary)

    def test_names_that_cannot_exist_everywhere(self):
        problems = archives.unportable(["CON.txt", "a/aux.log", "notes.", "a/B.txt", "a/b.txt", "A/c.txt",
                                        "ok/fine.txt", "x/y z.txt", "q?.txt"])
        joined = "\n".join(problems)
        for name in ("CON.txt", "aux.log", "notes.", "differ only in case", "also spelled", "q?.txt"):
            self.assertIn(name, joined)
        self.assertNotIn("fine.txt", joined)
        self.assertNotIn("y z.txt", joined)

    def test_defines_freeze_in_place(self):
        text = "#ifndef G\r\n#define G\r\n#define A 1 // [1 2]\r\n  //  #define B\n#define C\n#define D 0.5\n#endif\r\n"
        frozen = archives.freeze_defines(text, {"A": "2", "B": "on", "C": "off", "D": "-1e3"}, "t")
        self.assertEqual(frozen, "#ifndef G\r\n#define G\r\n#define A 2 // [1 2]\r\n  #define B\n// #define C\n"
                                 "#define D -1e3\n#endif\r\n")
        with self.assertRaises(ParamError):
            archives.freeze_defines(text, {"G": "off"}, "t")  # an include guard is no option, line endings aside


if __name__ == "__main__":
    unittest.main()
