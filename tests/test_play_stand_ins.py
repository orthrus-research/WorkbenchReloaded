"""wb play's stand-ins: played from a Work, its other members stand in for their lines at their HEADs; --with names
one at a ref or a worktree; a member with uncommitted changes is refused; played from outside a Work, none."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness  # noqa: E402,F401

from test_work import Config, mock_workspace  # noqa: E402

from wb import play, work  # noqa: E402
from wb.workspace import Workspace  # noqa: E402


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args], cwd=cwd,
                          check=True, capture_output=True, text=True).stdout.strip()


class StandIns(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name).resolve()
        self.repos = {}
        for name, folder in (("game", "Game"), ("lib", "Lib")):
            remote = self.root / f"{name}.git"
            git(self.root, "init", "-q", "--bare", "-b", "main", str(remote))
            seed = self.root / f"seed-{name}"
            git(self.root, "clone", "-q", str(remote), str(seed))
            (seed / "a.txt").write_text(name + "\n", encoding="utf-8")
            git(seed, "add", "-A")
            git(seed, "commit", "-q", "-m", "one")
            git(seed, "push", "-q", "origin", "HEAD:main")
            repo = self.root / folder
            git(self.root, "clone", "-q", str(remote), str(repo))
            git(repo, "remote", "set-head", "origin", "main")
            self.repos[name] = repo
        self.values = {"workspace": {"work": str(self.root / "work")},
                       "modules": {"ci": {"projects_root": str(self.root), "records": str(self.root / "records"),
                                          "projects": {"game": {"repo": "Game"}, "lib": {"repo": "Lib"}}}}}
        self.cwd = os.getcwd()
        os.chdir(self.root)
        with mock_workspace(self.values, self.root):
            import argparse
            import contextlib
            import io
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(work.main(argparse.Namespace(action="start", names=["feat/x", "game", "lib"],
                                                              all=False)), 0)
        self.work = self.root / "work" / "feat-x"
        self.workspace = Workspace(Config(self.values, self.root / "config"))

    def tearDown(self):
        os.chdir(self.cwd)
        for repo in self.repos.values():
            subprocess.run(["git", "worktree", "prune"], cwd=repo, capture_output=True)
        self.folder.cleanup()

    def found(self, top, given=None):
        with mock_workspace(self.values, self.root):
            return play.stand_ins(self.workspace, "game", top, given)

    def test_a_works_other_members_stand_in_at_their_heads(self):
        lib = self.work / "lib"
        (lib / "b.txt").write_text("change\n", encoding="utf-8")
        git(lib, "add", "-A")
        git(lib, "commit", "-q", "-m", "two")
        found = self.found(self.work / "game")
        self.assertEqual(list(found), ["lib"])
        self.assertEqual(found["lib"]["commit"], git(lib, "rev-parse", "HEAD"))
        self.assertEqual(Path(found["lib"]["folder"]).resolve(), lib.resolve())

    def test_a_member_with_uncommitted_changes_is_refused(self):
        (self.work / "lib" / "a.txt").write_text("unsaved\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, r"lib stands in from .* uncommitted changes"):
            self.found(self.work / "game")

    def test_outside_a_work_none_and_with_names_one(self):
        self.assertEqual(self.found(self.repos["game"]), {})
        found = self.found(self.repos["game"], ["lib=origin/main"])
        self.assertEqual(found["lib"], {"commit": git(self.repos["lib"], "rev-parse", "origin/main"),
                                        "ref": "origin/main"})
        with self.assertRaisesRegex(ValueError, r"--with 'game=main': game is the launch's own project"):
            self.found(self.repos["game"], ["game=main"])
        with self.assertRaisesRegex(ValueError, r"is a worktree of game, not of lib"):
            self.found(self.repos["game"], [f"lib={self.repos['game']}"])


if __name__ == "__main__":
    unittest.main()
