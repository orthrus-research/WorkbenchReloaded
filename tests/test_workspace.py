"""The workspace on the host: a worktree's project is found by its repository, the tree it would commit is written
by the host's own git (changes, deletions and ignored files as git sees them) without moving any ref, index or
file, and the push gate answers from git and CI's records with no daemon."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness  # noqa: E402,F401  (puts the SDK and core on the path)

from wb import hooks, setup  # noqa: E402
from wb.workspace import Workspace, WorkspaceError  # noqa: E402


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args], cwd=cwd,
                          check=True, capture_output=True, text=True).stdout.strip()


class Config:
    def __init__(self, values, folder):
        self.values, self.folder = values, folder


class Trees(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        root = Path(self.folder.name)
        self.repo = root / "Demo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        (self.repo / ".gitignore").write_text("build/\n", encoding="utf-8")
        (self.repo / "kept.txt").write_text("one\n", encoding="utf-8")
        (self.repo / "gone.txt").write_text("bye\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "one")
        self.linked = root / "Demo-wt"
        git(self.repo, "worktree", "add", "-q", "-b", "topic", str(self.linked))
        values = {"modules": {"ci": {"projects_root": str(root), "records": str(root / "records"),
                                     "projects": {"demo": {"repo": "Demo"}}}}}
        self.workspace = Workspace(Config(values, root / "config"))

    def tearDown(self):
        self.folder.cleanup()

    def test_a_linked_worktree_belongs_to_its_repositorys_project(self):
        project, top = self.workspace.project_for(self.linked)
        self.assertEqual((project.name, top.resolve()), ("demo", self.linked.resolve()))
        with self.assertRaises(WorkspaceError) as raised:
            self.workspace.project_for(Path(self.folder.name))
        self.assertIn("not in a git repository", str(raised.exception))

    def test_a_clean_worktree_is_tested_at_head(self):
        found = self.workspace.would_be(self.linked)
        self.assertFalse(found["dirty"])
        self.assertEqual(found["commit"], git(self.linked, "rev-parse", "HEAD"))

    def test_the_would_be_tree_is_what_git_would_commit_and_nothing_moves(self):
        (self.linked / "kept.txt").write_text("two\n", encoding="utf-8")
        (self.linked / "gone.txt").unlink()
        (self.linked / "new.txt").write_text("new\n", encoding="utf-8")
        (self.linked / "build").mkdir()
        (self.linked / "build" / "out.bin").write_text("ignored\n", encoding="utf-8")
        head, status = git(self.linked, "rev-parse", "HEAD"), git(self.linked, "status", "--porcelain")
        index = Path(git(self.linked, "rev-parse", "--path-format=absolute", "--git-path", "index")).read_bytes()
        found = self.workspace.would_be(self.linked)
        self.assertTrue(found["dirty"])
        names = git(self.linked, "ls-tree", "--name-only", found["tree"]).splitlines()
        self.assertEqual(sorted(names), [".gitignore", "kept.txt", "new.txt"])  # a deletion, no ignored file
        self.assertEqual(git(self.linked, "show", f"{found['tree']}:kept.txt"), "two")
        self.assertEqual(git(self.linked, "rev-parse", f"{found['commit']}^"), head)
        # Nothing moved: HEAD, the index, the working tree's status.
        self.assertEqual(git(self.linked, "rev-parse", "HEAD"), head)
        self.assertEqual(git(self.linked, "status", "--porcelain"), status)
        self.assertEqual(Path(git(self.linked, "rev-parse", "--path-format=absolute", "--git-path",
                                  "index")).read_bytes(), index)
        # The same tree a real commit of everything would make.
        git(self.linked, "add", "-A")
        self.assertEqual(git(self.linked, "write-tree"), found["tree"])


class Gate(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        root = Path(self.folder.name)
        self.repo = root / "Demo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "actions.toml").write_text(
            '[workflows]\nci = ["check"]\n\n[jobs.check]\nsteps = [{ name = "ok", run = ["python", "-c", "0"] }]\n',
            encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "jobs")
        self.sha = git(self.repo, "rev-parse", "HEAD")
        self.values = {"modules": {"ci": {"projects_root": str(root), "records": str(root / "records"),
                                          "projects": {"demo": {"repo": "Demo"}}}}}
        self.root = root

    def tearDown(self):
        self.folder.cleanup()

    def line(self):
        return [f"refs/heads/main {self.sha} refs/heads/main {'0' * 40}"]

    def test_an_untested_commit_is_refused_on_the_host(self):
        workspace = Workspace(Config(self.values, self.root / "config"))
        answer = workspace.gate(workspace.find(self.repo), self.line())
        self.assertTrue(answer["gated"])
        self.assertEqual(len(answer["refused"]), 1, answer)
        self.assertIn("has not passed check", answer["refused"][0]["reason"])

    def test_a_project_whose_gate_is_off_is_not_gated(self):
        self.values["modules"]["ci"]["projects"]["demo"]["gate"] = False
        workspace = Workspace(Config(self.values, self.root / "config"))
        answer = workspace.gate(workspace.find(self.repo), self.line())
        self.assertEqual((answer["gated"], answer["refused"]), (False, []))


class PushEnvironment(unittest.TestCase):
    def test_the_pre_push_gate_reads_other_folders_although_git_set_git_dir(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ("one", "two"):
                (root / name).mkdir()
                git(root / name, "init", "-q", "-b", "main")
                (root / name / "f.txt").write_text(name, encoding="utf-8")
                git(root / name, "add", "-A")
                git(root / name, "commit", "-q", "-m", name)
            sha = git(root / "two", "rev-parse", "HEAD")
            saved = dict(os.environ)
            try:
                os.environ["GIT_DIR"] = str(root / "one" / ".git")  # what git sets for a hook in repository one
                for name in hooks.REPOSITORY_ENV:
                    os.environ.pop(name, None)
                self.assertEqual(git(root / "two", "rev-parse", "HEAD"), sha)  # read in its own folder again
            finally:
                os.environ.clear()
                os.environ.update(saved)
            self.assertIn("GIT_DIR", hooks.REPOSITORY_ENV)


class Stubs(unittest.TestCase):
    def test_a_stub_holds_no_path_and_runs_what_git_config_names(self):
        for name in hooks.NAMES:
            text = hooks.stub(name)
            self.assertIn(hooks.MARKER, text)
            self.assertIn('git config --get wb.cli', text)
            self.assertIn(f"hook {name}", text)
            self.assertNotIn(":\\", text)

    def test_the_cli_command_names_the_release_in_service(self):
        command = setup.cli_command()
        self.assertIn("current", command)  # never a working tree: a hook runs what wb deploy put in service
        self.assertTrue(command.endswith(("wb.cmd", "wb")), command)


if __name__ == "__main__":
    unittest.main()
