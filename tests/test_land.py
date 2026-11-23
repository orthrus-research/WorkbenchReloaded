"""wb land end to end, against an in-process daemon and bare remotes on disk: a landing moves the Set and then the
set-named branch; one stopped before the Set moves leaves every branch as it was and the next landing removes its
landing refs; one stopped after it is finished by the next landing without a check; two landings started together
both land, the second waiting on lock set; a scrub refusal on a ref name publishes nothing; and the push gate takes
a set-named branch only from a landing."""

import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import Daemon  # noqa: E402

from wb.land import Lander  # noqa: E402
from wb.workspace import Workspace, WorkspaceError  # noqa: E402
from wb_core import landing as landings  # noqa: E402

MANIFEST = """schema = 1
gate = ["build"]

[project]
name = "{name}"

[artifacts.mod]
check = "build"
file = "out/{name}-*.jar"

[checks.build]
steps = [["{{python}}", "-c", "import pathlib; p = pathlib.Path('out'); p.mkdir(exist_ok=True); (p / '{name}-1.jar').write_bytes(pathlib.Path('src.txt').read_bytes())"]]
"""
EMPTY_SET = {"schema": 1, "projects": {}, "third": {}, "platform": {}, "holds": {}, "doctor": {}}


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args], cwd=cwd,
                          check=True, capture_output=True, text=True).stdout.strip()


def project(base: Path, name: str) -> tuple[Path, Path]:
    remote, repo = base / "remotes" / f"{name}.git", base / "data" / name
    remote.parent.mkdir(parents=True, exist_ok=True)
    git(base, "init", "-q", "--bare", "-b", "main", str(remote))
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "t")
    git(repo, "config", "user.email", "t@example.invalid")
    (repo / "project.toml").write_text(MANIFEST.format(name=name), encoding="utf-8")
    (repo / "src.txt").write_text("v1", encoding="utf-8")
    (repo / ".gitignore").write_text("out/\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "v1")
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-q", "origin", "main")
    git(repo, "remote", "set-head", "origin", "main")
    return repo, remote


def advance(repo: Path, text: str) -> str:
    (repo / "src.txt").write_text(text, encoding="utf-8")
    git(repo, "commit", "-q", "-am", text)
    return git(repo, "rev-parse", "HEAD")


class Land(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(tempfile.mkdtemp(prefix="wb-land-"))
        cls.root = root
        cls.lib, cls.lib_remote = project(root, "lib")
        cls.two, cls.two_remote = project(root, "two")
        cls.home = root / "set.git"
        git(root, "init", "-q", "--bare", "-b", "main", str(cls.home))
        seed = root / "seed"
        seed.mkdir()
        git(seed, "init", "-q", "-b", "main")
        (seed / "set.json").write_text(json.dumps(EMPTY_SET, indent=1) + "\n", encoding="utf-8")
        git(seed, "add", "-A")
        git(seed, "commit", "-q", "-m", "seed")
        git(seed, "push", "-q", str(cls.home), "main")
        terms = root / "terms.txt"
        terms.write_text("forbiddenword\n", encoding="utf-8")
        out = root / "out"
        cls.store = out / "store"
        cls.d = Daemon(f"""
            [workspace]
            set = '{cls.home}'

            [modules.ci]
            store = '{(out / "store").as_posix()}'
            records = '{(out / "records").as_posix()}'
            tool_env = {{ ORTHRUS_SCRUB_TERMS_FILE = '{terms.as_posix()}' }}

            [modules.ci.projects.lib]
            repo = '{cls.lib}'
            jdk = ""

            [modules.ci.projects.two]
            repo = '{cls.two}'
            jdk = ""
            """, root=root).start()
        cls.c = cls.d.client
        cls.said = []

    @classmethod
    def tearDownClass(cls):
        cls.d.stop()

    def lander(self):
        return Lander(Workspace(self.d.config), self.c, say=self.said.append)

    def set_json(self):
        return json.loads(git(self.home, "show", "main:set.json"))

    def remote_refs(self, remote):
        return git(remote, "for-each-ref", "--format=%(refname) %(objectname)").splitlines()

    def wait_settled(self, job_id, timeout=60):
        end = time.time() + timeout
        while time.time() < end:
            if self.c.job(job_id).refresh().state in ("settled", "cancelled", "interrupted"):
                return
            time.sleep(0.3)
        self.fail(f"{job_id} did not settle")

    def test_1_a_landing_moves_the_set_then_the_branch(self):
        commit = advance(self.lib, "v2")
        result = self.lander().land({"lib": "HEAD"})
        self.assertEqual(result["state"], "landed", self.said)
        line = self.set_json()["projects"]["lib"]
        self.assertEqual((line["commit"], line["branch"]), (commit, "main"))
        self.assertEqual(list(line["artifacts"]), ["mod"])
        self.assertEqual(line["artifacts"]["mod"]["file"], "lib-1.jar")
        self.assertNotIn("url", line)  # no --add url: a line carries none unless it is given
        self.assertEqual(git(self.lib_remote, "rev-parse", "main"), commit)
        self.assertFalse([r for r in self.remote_refs(self.lib_remote) if "refs/landed/" in r])
        message = git(self.home, "log", "-1", "--format=%B", "main")
        self.assertTrue(message.startswith(f"Land lib {commit[:8]}"), message)
        again = self.lander().land({"lib": "HEAD"})  # nothing moved since: nothing lands
        self.assertEqual(again["state"], "unchanged")

    def test_2_stopped_before_the_set_moves_changes_no_branch_and_is_cleaned_up(self):
        before_set, before_branch = git(self.home, "rev-parse", "main"), git(self.lib_remote, "rev-parse", "main")
        advance(self.lib, "v3")
        result = self.lander().land({"lib": "HEAD"}, deadline=5, stop_after="refs")
        self.assertEqual(result["state"], "refs-pushed")
        self.assertTrue([r for r in self.remote_refs(self.lib_remote) if "refs/landed/" in r])
        self.assertEqual((git(self.home, "rev-parse", "main"), git(self.lib_remote, "rev-parse", "main")),
                         (before_set, before_branch))
        record = landings.read(self.store, result["landing"])
        self.wait_settled(record["job"])  # it gives the lock back at its deadline
        done = self.lander().reconcile()
        self.assertTrue(any("removed" in d or "cleaned" in d for d in done), done)
        self.assertFalse([r for r in self.remote_refs(self.lib_remote) if "refs/landed/" in r])
        self.assertEqual(landings.read(self.store, result["landing"])["state"], "abandoned")

    def test_3_stopped_after_the_set_moves_is_finished_without_a_check(self):
        commit = git(self.lib, "rev-parse", "HEAD")
        result = self.lander().land({"lib": "HEAD"}, deadline=5, stop_after="set")
        self.assertEqual(result["state"], "set-pushed")
        self.assertEqual(self.set_json()["projects"]["lib"]["commit"], commit)
        self.assertNotEqual(git(self.lib_remote, "rev-parse", "main"), commit)  # landed, branch behind
        self.wait_settled(landings.read(self.store, result["landing"])["job"])
        jobs_before = len(self.c.jobs(limit=500))
        done = self.lander().reconcile()
        self.assertIn(f"{result['landing']} finished", done)
        self.assertEqual(git(self.lib_remote, "rev-parse", "main"), commit)
        self.assertEqual(len(self.c.jobs(limit=500)), jobs_before)  # no check ran to finish it

    def test_4_two_landings_started_together_both_land(self):
        lib_commit, two_commit = advance(self.lib, "v4"), advance(self.two, "v2")
        first = self.lander()
        slow = first.publish

        def publish(record, stop_after=None):
            time.sleep(4)  # hold lock set long enough for the other landing to wait on it
            return slow(record, stop_after)
        first.publish = publish
        results = {}
        threads = [threading.Thread(target=lambda: results.__setitem__("lib", first.land({"lib": "HEAD"}))),
                   threading.Thread(target=lambda: (time.sleep(1.5),
                                                    results.__setitem__("two", self.lander().land({"two": "HEAD"}))))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(120)
        self.assertEqual({k: v["state"] for k, v in results.items()}, {"lib": "landed", "two": "landed"}, self.said)
        lines = self.set_json()["projects"]
        self.assertEqual((lines["lib"]["commit"], lines["two"]["commit"]), (lib_commit, two_commit))
        self.assertTrue(any("lock set held by" in s for s in self.said), self.said)

    def test_5_a_scrub_refusal_on_a_ref_name_publishes_nothing(self):
        before = git(self.home, "rev-parse", "main")
        advance(self.two, "v3")
        with self.assertRaises(WorkspaceError) as raised:
            self.lander().land({"two": "HEAD"}, branch="feat/forbiddenword")
        self.assertIn("scrub", str(raised.exception))
        self.assertEqual(git(self.home, "rev-parse", "main"), before)
        self.assertFalse([r for r in self.remote_refs(self.two_remote) if "forbiddenword" in r or "landed" in r])

    def test_6_the_gate_takes_a_set_named_branch_only_from_a_landing(self):
        workspace = Workspace(self.d.config)
        lib = workspace.projects()["lib"]
        commit = advance(self.lib, "v5")
        refused = workspace.landing_rule(lib, commit, "refs/heads/main")
        self.assertIn("wb land", refused)
        self.assertIsNone(workspace.landing_rule(lib, commit, "refs/heads/feat/other"))  # the ordinary gate's
        landings.write(self.store, {"id": "ltest", "state": "refs-pushed", "job": "j_none",
                                    "projects": {"lib": {"commit": commit, "branch": "main"}}})
        self.assertEqual(workspace.landing_rule(lib, commit, "refs/heads/main"), "")
        landings.move(self.store, "ltest", "abandoned", refs_dropped=True)

    def test_7_a_worktree_two_landings_behind_still_checks_and_pushes(self):
        from wb import setwork
        workspace = Workspace(self.d.config)
        old = git(self.home, "rev-parse", "main~2")
        line = self.set_json()["projects"]["lib"]["commit"]
        tree = self.root / "behind-wt"
        git(self.lib, "worktree", "add", "-q", "-b", "feat/behind", str(tree), line)
        setwork.write(tree, old)
        said = []
        self.assertEqual(setwork.resolve(workspace, tree, say=said.append), old)  # its own revision, not the head
        self.assertRegex(said[0], r"^set [0-9a-f]{7}, 2 behind \(.+ moved\): wb work sync$")
        verdict = workspace.gate(workspace.projects()["lib"],
                                 [f"refs/heads/feat/behind {line} refs/heads/feat/behind {'0' * 40}"])
        self.assertEqual((verdict["gated"], verdict["refused"]), (True, []))  # the same pass decides, behind or not
        self.assertIn("/.set", (Path(git(tree, "rev-parse", "--git-common-dir")) / "info" / "exclude")
                      .read_text(encoding="utf-8"))

    def test_8_an_unconverted_consumer_is_listed_with_its_legacy_pin(self):
        from wb import setview
        from wb.workspace import Project
        old = git(self.lib, "rev-list", "--max-parents=0", "HEAD")  # lib's first commit, well behind its line
        app, _ = project(self.root, "app")
        (app / "project.toml").unlink()
        (app / "actions.toml").write_text("[jobs.x]\nsteps = [{ name = 'x', run = ['true'] }]\n", encoding="utf-8")
        (app / "lib.lock.json").write_text(json.dumps({"commit": old}) + "\n", encoding="utf-8")
        git(app, "add", "-A")
        git(app, "commit", "-q", "-m", "unconverted")
        git(app, "push", "-q", "origin", "main")
        git(app, "fetch", "-q", "origin")
        workspace = Workspace(self.d.config)
        workspace.projects()["app"] = Project("app", {}, app, None)
        text = "\n".join(setview.report(workspace))
        line = self.set_json()["projects"]["lib"]
        self.assertIn("app (no project.toml on origin/main)", text)
        self.assertIn(f"lib.lock.json:1 pins lib {old[:12]}", text)
        self.assertIn(f"copy commit {line['commit']} (mod {line['artifacts']['mod']['sha256']})", text)
        self.assertIn("lib ", text.split("not converted")[0])  # lib's own line is listed with its branch state

    def test_9_a_hold_lands_with_seeded_bytes_and_is_dropped(self):
        adapter = self.root / "adapter.jar"
        adapter.write_bytes(b"qualified adapter bytes")
        old = git(self.lib, "rev-list", "--max-parents=0", "HEAD")
        hold = {"file": str(adapter), "commit": old, "reason": "qualified once; requalify on current lines",
                "source": "the owner's backup"}
        result = self.lander().land({}, holds={"lib:adapter": hold})
        self.assertEqual(result["state"], "landed", self.said)
        held = self.set_json()["holds"]["lib:adapter"]
        self.assertEqual((held["commit"], held["artifacts"]["adapter"]["file"]), (old, "adapter.jar"))
        self.assertTrue((self.store / "seeded" / f"{held['artifacts']['adapter']['sha256']}.json").is_file())
        self.assertIn("Land hold lib:adapter", git(self.home, "log", "-1", "--format=%s", "main"))
        dropped = self.lander().land({}, drop_holds=["lib:adapter"])
        self.assertEqual(dropped["state"], "landed")
        self.assertNotIn("lib:adapter", self.set_json()["holds"])


    def test_y_the_backup_keeps_the_set_and_the_seeded_bytes(self):
        from wb import backup
        folder = Path(tempfile.mkdtemp(prefix="wb-backup-"))
        bundled = backup._set_bundle(self.d.config, folder)
        self.assertIn("set.bundle", bundled)
        self.assertEqual(subprocess.run(["git", "bundle", "verify", str(folder / "set.bundle")], cwd=self.home,
                                        capture_output=True).returncode, 0)
        seeded = backup._seeded(self.d.config, folder)
        self.assertTrue(any(name.endswith("/adapter.jar") for name in seeded), seeded)  # test_9's hold bytes

    def test_z_a_landing_made_elsewhere_is_built_on(self):
        remote = self.root / "remotes" / "set.git"
        git(self.root, "clone", "-q", "--bare", str(self.home), str(remote))
        git(self.home, "remote", "add", "origin", str(remote))
        elsewhere = self.root / "elsewhere"  # another machine's landing, pushed to the Set's remote
        git(self.root, "clone", "-q", str(remote), str(elsewhere))
        data = json.loads((elsewhere / "set.json").read_text(encoding="utf-8"))
        data["doctor"] = {"exempt": ["evidence/**"]}
        (elsewhere / "set.json").write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
        git(elsewhere, "commit", "-q", "-am", "elsewhere")
        git(elsewhere, "push", "-q", "origin", "main")
        commit = advance(self.lib, "v6")
        result = self.lander().land({"lib": "HEAD"})
        self.assertEqual(result["state"], "landed", self.said)
        self.assertEqual(git(remote, "rev-parse", "main"), git(self.home, "rev-parse", "main"))  # pushed: the commit point
        landed = json.loads(git(remote, "show", "main:set.json"))
        self.assertEqual((landed["projects"]["lib"]["commit"], landed["doctor"]), (commit, {"exempt": ["evidence/**"]}))
        self.assertTrue(any("moved elsewhere" in s for s in self.said), self.said)


if __name__ == "__main__":
    unittest.main()
