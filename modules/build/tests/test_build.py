"""build: checkouts read through the roots, and archives whose bytes depend on the tree alone."""

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
        ctx = Context({"job": "direct", "command": "build.receipt", "module": "build", "work": str(root / "job" / "work"),
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
