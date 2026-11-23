"""An input step with store takes the JARs CI built for a clean checkout's tree from the Store and skips its build;
a checkout with changes, or a tree the Store does not hold, runs the step as written."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path[:0] = [str(REPO / "core"), str(REPO / "sdk" / "python"), str(REPO / "modules" / "lab")]

from wb_core import artifacts  # noqa: E402
from wb_core.module import Context  # noqa: E402
from wb_lab import inputs  # noqa: E402


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args], cwd=cwd,
                          check=True, capture_output=True, text=True).stdout.strip()


class FakeWorkspace:
    def __init__(self, store):
        self.ci = {"store": str(store)}

    def host_path(self, value):
        return Path(value)


class FromStore(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        base = Path(self.folder.name)
        self.repo, self.store = base / "mod", base / "store"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "src.txt").write_text("source", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "one")
        jar = base / "mod.jar"
        jar.write_bytes(b"ci bytes")
        digest = artifacts.sha256(jar)
        artifacts.put(self.store, jar, digest)
        artifacts.record(self.store, "mod", git(self.repo, "rev-parse", "HEAD^{tree}"), "c",
                         {"mod.jar": {"sha256": digest, "job": "build", "run": "r"}})
        self.step = {"name": "mod", "run": ["false"], "store": {"project": "mod", "files": {"mod.jar": "build/libs/mod.jar"}}}
        spec = {"job": "j", "command": "lab.inputs", "module": "lab", "params": {}, "module_config": {},
                "machine": "host", "work": str(base / "job" / "work"), "out": str(base / "job" / "out")}
        self.ctx = Context(spec, lambda event: None)
        self.patch = mock.patch("wb.workspace.Workspace", lambda: FakeWorkspace(self.store))
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.folder.cleanup()

    def test_a_clean_checkout_takes_cis_jar_and_skips_its_build(self):
        taken = inputs.from_store(self.ctx, self.step, self.repo)
        self.assertEqual(taken["from_store"]["project"], "mod")
        self.assertEqual((self.repo / "build" / "libs" / "mod.jar").read_bytes(), b"ci bytes")

    def test_a_checkout_with_changes_builds(self):
        (self.repo / "src.txt").write_text("changed", encoding="utf-8")
        self.assertIsNone(inputs.from_store(self.ctx, self.step, self.repo))

    def test_a_tree_the_store_does_not_hold_builds(self):
        (self.repo / "src.txt").write_text("two", encoding="utf-8")
        git(self.repo, "commit", "-q", "-am", "two")
        self.assertIsNone(inputs.from_store(self.ctx, self.step, self.repo))


if __name__ == "__main__":
    unittest.main()
