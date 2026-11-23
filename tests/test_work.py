"""wb work: a Work's members are cut from the remote's default branch, a name in use is refused with the fix, a
worktree made by hand is adopted, and finish removes only members that hold nothing unsaved."""

import argparse
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness  # noqa: E402,F401

from wb import work  # noqa: E402
from wb.workspace import Workspace  # noqa: E402


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args], cwd=cwd,
                          check=True, capture_output=True, text=True).stdout.strip()


class Config:
    def __init__(self, values, folder):
        self.values, self.folder = values, folder


class Work(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name).resolve()
        remote = self.root / "remote.git"
        git(self.root, "init", "-q", "--bare", "-b", "main", str(remote))
        seed = self.root / "seed"
        git(self.root, "clone", "-q", str(remote), str(seed))
        (seed / "a.txt").write_text("a\n", encoding="utf-8")
        git(seed, "add", "-A")
        git(seed, "commit", "-q", "-m", "one")
        git(seed, "push", "-q", "origin", "HEAD:main")
        self.repo = self.root / "Demo"
        git(self.root, "clone", "-q", str(remote), str(self.repo))
        git(self.repo, "remote", "set-head", "origin", "main")
        self.values = {"workspace": {"work": str(self.root / "work")},
                       "modules": {"ci": {"projects_root": str(self.root), "records": str(self.root / "records"),
                                          "projects": {"demo": {"repo": "Demo"}}}}}
        self.cwd = os.getcwd()

    def tearDown(self):
        os.chdir(self.cwd)
        subprocess.run(["git", "worktree", "prune"], cwd=self.repo, capture_output=True)
        self.folder.cleanup()

    def wb(self, action, *names, everything=False, where=None):
        os.chdir(where or self.root)
        out, err = io.StringIO(), io.StringIO()
        with mock_workspace(self.values, self.root), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = work.main(argparse.Namespace(action=action, names=list(names), all=everything))
        return code, out.getvalue(), err.getvalue()

    def test_start_cuts_members_from_the_remote_and_refuses_a_name_in_use(self):
        code, out, _ = self.wb("start", "feat/ore-depth", "demo")
        self.assertEqual(code, 0, out)
        member = self.root / "work" / "feat-ore-depth" / "demo"
        self.assertEqual(git(member, "rev-parse", "--abbrev-ref", "HEAD"), "feat/ore-depth")
        self.assertEqual(git(member, "rev-parse", "HEAD"), git(self.repo, "rev-parse", "origin/main"))
        self.assertTrue(out.strip().endswith(f"cd {member}"))
        code, _, err = self.wb("start", "feat/ore-depth", "demo")
        self.assertEqual(code, 2)
        self.assertIn("exists already", err)

    def test_finish_removes_what_holds_nothing_and_keeps_unsaved_work(self):
        self.wb("start", "fix/one", "demo")
        topic = self.root / "work" / "fix-one"
        member = topic / "demo"
        (member / "build").mkdir()
        (member / "build" / "out.bin").write_text("scratch", encoding="utf-8")
        (member / "notes.txt").write_text("unsaved", encoding="utf-8")
        code, out, _ = self.wb("finish", where=topic)
        self.assertEqual(code, 1)
        self.assertIn("kept", out)
        self.assertTrue(member.is_dir())
        (member / "notes.txt").unlink()
        code, out, _ = self.wb("finish", where=topic)
        self.assertEqual(code, 0, out)
        self.assertFalse(member.exists())
        self.assertIn("deleted here", out)  # the branch had nothing origin/main lacks
        self.assertNotIn("--delete", out)  # it was never pushed, so there is no remote copy to delete
        self.assertFalse((topic / ".set").exists())  # the Work's revision goes with its last member

    def test_a_hand_made_worktree_is_adopted_and_a_main_checkout_is_not(self):
        hand = self.root / "hand-wt"
        git(self.repo, "worktree", "add", "-q", "-b", "topic/x", str(hand), "origin/main")
        code, _, err = self.wb("adopt", str(hand), where=hand)
        self.assertEqual(code, 2)
        self.assertIn("cd ..", err)
        code, out, _ = self.wb("adopt", str(hand))
        self.assertEqual(code, 0, out)
        self.assertTrue((self.root / "work" / "topic-x" / "demo" / "a.txt").is_file())
        code, _, err = self.wb("adopt", str(self.repo))
        self.assertEqual(code, 2)
        self.assertIn("main checkout", err)


@contextlib.contextmanager
def mock_workspace(values, root):
    original = work.Workspace
    work.Workspace = lambda: Workspace(Config(values, root / "config"))
    try:
        yield
    finally:
        work.Workspace = original


if __name__ == "__main__":
    unittest.main()
