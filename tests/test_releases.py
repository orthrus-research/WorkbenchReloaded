"""Releases: a release is the commit's files and its RELEASE.json, made once; current is a link that moves to a new
release without touching the old one; a release names its commit without git; old releases go, the one in service
stays."""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness  # noqa: E402,F401

from wb import releases  # noqa: E402
from wb_core.identity import source_commit  # noqa: E402


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args], cwd=cwd,
                          check=True, capture_output=True, text=True).stdout.strip()


class Config:
    def __init__(self, values, folder):
        self.values, self.folder = values, folder


class Releases(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        base = Path(self.folder.name)
        self.repo = base / "wb"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "bin").mkdir()
        (self.repo / "bin" / "wb").write_text("one\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "one")
        self.first = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "bin" / "wb").write_text("two\n", encoding="utf-8")
        git(self.repo, "commit", "-q", "-am", "two")
        self.second = git(self.repo, "rev-parse", "HEAD")
        self.config = Config({}, base / "config")
        self.saved = os.environ.get("WB_RELEASES")
        os.environ["WB_RELEASES"] = str(base / "releases")

    def tearDown(self):
        link = releases.current(self.config)
        if link.exists() or link.is_symlink():
            os.rmdir(link) if os.name == "nt" else link.unlink()
        if self.saved is None:
            os.environ.pop("WB_RELEASES", None)
        else:
            os.environ["WB_RELEASES"] = self.saved
        self.folder.cleanup()

    def test_releases_never_live_in_the_config_folder(self):
        good = os.environ["WB_RELEASES"]
        os.environ["WB_RELEASES"] = str(self.config.folder / "releases")
        try:
            with self.assertRaises(RuntimeError) as raised:
                releases.root(self.config)
            self.assertIn("read as config", str(raised.exception))
        finally:
            os.environ["WB_RELEASES"] = good

    def test_a_release_is_the_commits_files_and_names_its_commit(self):
        made = releases.make(self.config, self.repo, self.first, {"tree": "t"})
        self.assertEqual((made / "bin" / "wb").read_text(encoding="utf-8"), "one\n")
        self.assertEqual(source_commit(made), self.first)  # no git in it
        self.assertEqual(releases.make(self.config, self.repo, self.first, {}), made)  # made once

    def test_current_moves_and_the_old_release_is_untouched(self):
        one = releases.make(self.config, self.repo, self.first, {})
        two = releases.make(self.config, self.repo, self.second, {})
        self.assertIsNone(releases.point(self.config, one))
        self.assertEqual((releases.current(self.config) / "bin" / "wb").read_text(encoding="utf-8"), "one\n")
        self.assertEqual(Path(os.path.realpath(releases.point(self.config, two))).name, one.name)
        self.assertEqual((releases.current(self.config) / "bin" / "wb").read_text(encoding="utf-8"), "two\n")
        self.assertEqual((one / "bin" / "wb").read_text(encoding="utf-8"), "one\n")

    def test_gc_lists_every_release_but_the_one_in_service_and_the_one_before_it(self):
        from wb import gc
        one = releases.make(self.config, self.repo, self.first, {})
        time.sleep(0.05)
        two = releases.make(self.config, self.repo, self.second, {})
        releases.point(self.config, one)
        places = gc.Places(releases=releases.root(self.config))
        self.assertEqual(gc.release_folders(places, gc.roots(places)), [])  # the one in service, and the newest
        releases.point(self.config, two)
        listed = gc.release_folders(gc.Places(releases=releases.root(self.config), previous_release=one.name),
                                    gc.roots(places))
        self.assertEqual(listed, [])  # two in service, one before it
        three = releases.root(self.config) / ("3" * 12)  # a third commit's release
        three.mkdir()
        (three / "RELEASE.json").write_text(json.dumps({"made_at": time.time()}), encoding="utf-8")
        releases.point(self.config, three)
        places = gc.Places(releases=releases.root(self.config), previous_release=two.name)
        self.assertEqual([i.path for i in gc.release_folders(places, gc.roots(places))], [str(one)])
        self.assertTrue(two.is_dir())

if __name__ == "__main__":
    unittest.main()
