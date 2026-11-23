"""Checkouts a profile names by ref or pin: each job reads a snapshot fixed at the commit it resolves to, a pin is read
from its holder's file at the holder's commit, a snapshot is never moved, and removing one never touches what its
links point at."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path[:0] = [str(REPO / "core"), str(REPO / "sdk" / "python"), str(REPO / "modules" / "lab"),
                str(REPO / "modules" / "build")]

from wb_core.module import Context, EnvironmentProblem  # noqa: E402
from wb_lab import commands, profile, snapshots  # noqa: E402

BASE = {"launcher": "run.py", "instances": "instances", "prepare": ["{python}", "run.py", "prepare", "{name}"],
        "play": ["{python}", "run.py", "play", "{name}"], "lab_root": ".local",
        "lab_items": [["shared", "instances"], ["settings.json", "settings"]], "stays": ["toolchains"]}


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args], cwd=cwd,
                          check=True, capture_output=True, text=True).stdout.strip()


def commit(repo: Path, files: dict[str, str], message: str) -> str:
    for name, text in files.items():
        (repo / name).write_text(text, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


class Snapshots(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        root = Path(self.folder.name)
        self.home, self.state = root / "lab", root / "state"
        (self.home / "shared").mkdir(parents=True)
        (self.home / "shared" / "world.txt").write_text("a world", encoding="utf-8")
        (self.home / "settings.json").write_text("{}", encoding="utf-8")
        self.launcher, self.lib, self.base = root / "launcher", root / "lib", root / "base"
        for repo in (self.launcher, self.lib, self.base):
            repo.mkdir()
            git(repo, "init", "-q", "-b", "main")
        self.base_1 = commit(self.base, {"base.txt": "b1"}, "b1")
        self.base_2 = commit(self.base, {"base.txt": "b2"}, "b2")
        self.lib_1 = commit(self.lib, {"platform.json": json.dumps({"base": {"commit": self.base_1}})}, "lib 1")
        (self.launcher / ".gitignore").write_text(".local/\n", encoding="utf-8")
        (self.launcher / ".local" / "toolchains").mkdir(parents=True)
        self.launcher_1 = commit(self.launcher, {"run.py": "# a launcher\n",
                                                 "lib.lock.json": json.dumps({"commit": self.lib_1})}, "one")
        self.config = {"snapshots": str(self.state / "checkouts"), "profile": dict(
            BASE, checkout={"repo": str(self.launcher), "ref": "main"},
            checkouts={"lib": {"repo": "../lib", "pin": {"file": "lib.lock.json"}},
                       "base": {"repo": "../base", "pin": {"checkout": "lib", "file": "platform.json",
                                                           "key": "base.commit"}}})}
        self.env = mock.patch.dict(os.environ, {"WB_LAB_HOME": str(self.home)})
        self.env.start()
        self.jobs = 0

    def tearDown(self):
        self.env.stop()
        self.folder.cleanup()

    def ctx(self) -> Context:
        self.jobs += 1
        where = Path(self.folder.name) / "jobs" / str(self.jobs)
        spec = {"job": f"j{self.jobs}", "command": "lab.checkouts", "module": "lab", "params": {},
                "module_config": self.config, "machine": "host", "work": str(where / "work"), "out": str(where / "out")}
        return Context(spec, lambda event: None)

    def test_each_checkout_is_read_at_the_commit_its_ref_or_pin_names(self):
        ctx = self.ctx()
        found = snapshots.resolve(ctx)
        self.assertEqual((found[""].commit, found["lib"].commit, found["base"].commit),
                         (self.launcher_1, self.lib_1, self.base_1))  # base's HEAD moved on; lib pins b1
        self.assertEqual((commands._launcher(ctx) / "run.py").read_text(encoding="utf-8"), "# a launcher\n")
        self.assertEqual((commands._profile_path(ctx, "{base}/base.txt")).read_text(encoding="utf-8"), "b1")
        self.assertIn("pin lib's platform.json base.commit", found["base"].via)
        record = json.loads((ctx.out / "checkouts.json").read_text(encoding="utf-8"))
        self.assertEqual(record["base"]["commit"], self.base_1)
        self.assertEqual(git(self.launcher, "status", "--porcelain"), "")  # the checkout itself is untouched
        self.assertTrue((commands._launcher(ctx) / ".git").is_file())  # a linked worktree: siblings resolve as ever

    def test_a_prepare_argument_may_name_a_file_in_a_checkout(self):
        ctx = self.ctx()
        self.assertEqual(commands._arg(ctx, "{lib}/platform.json"), str(snapshots.checkout(ctx, "lib") / "platform.json"))
        self.assertEqual(commands._arg(ctx, "{unknown}/x.jar"), "{unknown}/x.jar")  # no checkout of that name
        inputs = commands.recipe_inputs(ctx, 1, None, ["--client-mod", "{lib}/platform.json"])
        digest = commands._digest(snapshots.checkout(ctx, "lib") / "platform.json")
        self.assertEqual(inputs["args"], ["--client-mod", "sha256:" + digest])  # its bytes, never the snapshot's folder

    def test_a_snapshot_is_never_moved_and_a_new_commit_is_a_new_snapshot(self):
        first = snapshots.launcher(self.ctx())
        moved = commit(self.launcher, {"run.py": "# moved on\n"}, "two")
        second = snapshots.launcher(self.ctx())
        self.assertNotEqual(first, second)
        self.assertEqual((first / "run.py").read_text(encoding="utf-8"), "# a launcher\n")  # an earlier job's, intact
        self.assertEqual(second.name, moved[:12])
        self.assertEqual(snapshots.launcher(self.ctx()), second)  # made once, then reused

    def test_the_launchers_snapshot_links_the_lab(self):
        from wb import platform
        launcher = snapshots.launcher(self.ctx())
        local = launcher / ".local"
        self.assertTrue(platform.is_link(local / "shared"))
        self.assertEqual((local / "shared" / "world.txt").read_text(encoding="utf-8"), "a world")
        self.assertTrue(platform.is_link(local / "toolchains"))  # a stay, from the launcher repository
        self.assertEqual(os.stat(local / "settings.json").st_ino, os.stat(self.home / "settings.json").st_ino)
        snapshots.remove(launcher)
        self.assertFalse(launcher.exists())
        self.assertEqual((self.home / "shared" / "world.txt").read_text(encoding="utf-8"), "a world")  # untouched
        self.assertTrue((self.launcher / ".local" / "toolchains").is_dir())

    def test_prune_keeps_what_is_current_and_recently_used(self):
        old = snapshots.launcher(self.ctx())
        commit(self.launcher, {"run.py": "# two\n"}, "two")
        ctx = self.ctx()
        ctx.params = {}
        done = snapshots.checkouts_command(ctx, prune=True, keep=0, unused_days=1)
        self.assertEqual(done["removed"], [])  # used just now
        record = old.with_name(old.name + ".json")
        data = json.loads(record.read_text(encoding="utf-8"))
        record.write_text(json.dumps(dict(data, used=0)), encoding="utf-8")
        done = snapshots.checkouts_command(self.ctx(), prune=True, keep=0, unused_days=1)
        self.assertEqual(done["removed"], [str(old)])  # unused and not current; the current ones stay
        self.assertFalse(old.exists())
        self.assertNotIn(old.name, git(self.launcher, "worktree", "list"))  # its worktree entry went with it
        self.assertTrue(snapshots.launcher(self.ctx()).is_dir())

    def test_a_pin_its_holder_lacks_is_refused(self):
        commit(self.launcher, {"lib.lock.json": json.dumps({"elsewhere": "x"})}, "no pin")
        with self.assertRaises(EnvironmentProblem) as raised:
            snapshots.resolve(self.ctx())
        self.assertIn("lib.lock.json names no commit at commit", str(raised.exception))

    def test_checkout_tables_are_checked(self):
        cases = [({"checkout": {"repo": "x"}}, "a ref or a pin, one of them"),
                 ({"checkouts": {"a": {"repo": "x", "ref": "main", "pin": {"file": "f"}}}},
                  "a ref or a pin, one of them"),
                 ({"checkout": {"repo": "x", "pin": {"file": "f"}}}, "names a ref, not a pin"),
                 ({"checkout": {"ref": "main"}}, "a table with repo"),
                 ({"checkouts": {"a": {"repo": "x", "pin": {"checkout": "b", "file": "f"}},
                                 "b": {"repo": "y", "ref": "main"}}}, "not declared above it"),
                 ({"checkouts": {"a": {"repo": "x", "pin": {"key": "k"}}}}, "pin is a table with a file"),
                 ({"checkouts": {"a": {"repo": "x", "ref": "main", "fetch": "yes"}}}, "fetch is true or false")]
        for keys, expected in cases:
            with self.subTest(expected=expected):
                with self.assertRaises(profile.ProfileError) as raised:
                    profile.merged({"profile": dict(BASE, **keys)})
                self.assertIn(expected, str(raised.exception))


if __name__ == "__main__":
    unittest.main()
