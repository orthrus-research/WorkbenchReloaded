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
        cls.gradled, _ = project(root, "gradled")  # a project whose check runs {gradle}
        (cls.gradled / "project.toml").write_text('schema = 1\ngate = ["build"]\n\n[project]\nname = "gradled"\n\n'
                                                  '[checks.build]\nsteps = [["{gradle}", "hello"]]\n', encoding="utf-8")
        git(cls.gradled, "commit", "-q", "-am", "gradle")
        cls.set_checkout = root / "data" / "Set"  # the Set's home checkout, as a project
        git(root, "clone", "-q", str(cls.home), str(cls.set_checkout))
        git(cls.set_checkout, "config", "user.name", "t")
        git(cls.set_checkout, "config", "user.email", "t@example.invalid")
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

            [modules.ci.projects.gradled]
            repo = '{cls.gradled}'
            jdk = ""

            [modules.ci.projects.set]
            repo = '{cls.set_checkout}'
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


    def test_9b_pins_inside_a_held_files_entry_are_the_holds(self):
        from wb import setview
        from wb.workspace import Project
        adapter = self.root / "held.jar"
        adapter.write_bytes(b"held bytes")
        first = git(self.lib, "rev-list", "--max-parents=0", "HEAD")
        built_against = git(self.two, "rev-list", "--max-parents=0", "HEAD")
        hold = {"file": str(adapter), "commit": first, "reason": "kept as qualified", "source": "a backup"}
        self.assertEqual(self.lander().land({}, holds={"lib:held": hold})["state"], "landed", self.said)
        digest = self.set_json()["holds"]["lib:held"]["artifacts"]["held"]["sha256"]
        consumer, _ = project(self.root, "consumer")
        (consumer / "project.toml").unlink()
        (consumer / "lock.json").write_text(json.dumps({"mods": [
            {"sha256": digest, "sourceCommit": first, "build": {"inputs": {"two": built_against}}},
            {"sha256": "0" * 64, "commit": built_against}]}, indent=1) + "\n", encoding="utf-8")
        git(consumer, "add", "-A")
        git(consumer, "commit", "-q", "-m", "unconverted, with a held entry")
        git(consumer, "push", "-q", "origin", "main")
        git(consumer, "fetch", "-q", "origin")
        workspace = Workspace(self.d.config)
        workspace.projects()["consumer"] = Project("consumer", {}, consumer, None)
        text = "\n".join(setview.report(workspace))
        self.assertIn(f"pins {first[:12]} for the hold lib:held", text)
        self.assertIn(f"pins {built_against[:12]} for the hold lib:held", text)  # built against, inside its entry
        self.assertIn(f"pins two {built_against[:12]}", text)  # the same commit outside the entry is a real pin
        self.assertIn("hold lib:held at", text)
        self.assertNotIn(f"pins {digest[:12]}", text)  # a SHA-256 is not a commit, though it starts with 40 hex
        self.assertEqual(self.lander().land({}, drop_holds=["lib:held"])["state"], "landed")

    def test_9c_a_change_to_the_sets_files_lands_under_its_landing_commit(self):
        checkout = self.set_checkout
        git(checkout, "fetch", "-q", "origin")
        git(checkout, "checkout", "-q", "-B", "feat/logic", "origin/main")
        (checkout / "project.toml").write_text('schema = 1\ngate = ["schema"]\n\n[project]\nname = "set"\n\n'
                                              '[checks.schema]\nsteps = [["{python}", "-c", "pass"]]\n',
                                              encoding="utf-8")
        (checkout / "build-logic").mkdir(exist_ok=True)
        (checkout / "build-logic" / "README.md").write_text("set.base\n", encoding="utf-8")
        git(checkout, "add", "-A")
        git(checkout, "commit", "-q", "-m", "build-logic")
        change = git(checkout, "rev-parse", "HEAD")
        before = self.set_json()
        result = self.lander().land({"set": "HEAD"})
        self.assertEqual(result["state"], "landed", self.said)
        self.assertEqual(git(self.home, "rev-parse", "main^"), change)  # the landing's commit sits on the change
        after = self.set_json()
        self.assertEqual(after["platform"]["logic"], git(checkout, "rev-parse", "HEAD:build-logic"))
        self.assertEqual(after["projects"], before["projects"])
        self.assertTrue(git(self.home, "log", "-1", "--format=%s", "main").startswith(f"Land set files {change[:8]}"))
        self.assertFalse(git(self.home, "for-each-ref", "refs/landing/"))  # the temporary ref is gone
        # A change that edits set.json is refused: only a landing writes it.
        git(checkout, "fetch", "-q", "origin")
        git(checkout, "checkout", "-q", "-B", "feat/sneaky", "origin/main")
        data = json.loads((checkout / "set.json").read_text(encoding="utf-8"))
        data["doctor"] = {"exempt": ["sneaky/**"]}
        (checkout / "set.json").write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
        git(checkout, "commit", "-q", "-am", "sneaky")
        with self.assertRaises(WorkspaceError) as raised:
            self.lander().land({"set": "HEAD"})
        self.assertIn("edits set.json", str(raised.exception))
        workspace = Workspace(self.d.config)
        refused = workspace.landing_rule(workspace.projects()["set"], change, "refs/heads/main")
        self.assertIn("wb land set=", refused)

    def test_9d_gradle_in_a_step_is_the_sets_gradle_with_the_sets_arguments(self):
        import os
        from wb_core import setfile
        revision = "b" * 40
        setfile.export(self.store.parent / "sets", revision, setfile.dump(
            {**EMPTY_SET, "platform": {"java": 26, "gradle": {"version": "9.8.1"}}}))
        binary = self.store / "gradle" / "9.8.1" / "gradle-9.8.1" / "bin" / ("gradle.bat" if os.name == "nt" else "gradle")
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_text("@echo gradle %*\r\n" if os.name == "nt" else '#!/bin/sh\necho gradle "$@"\n', encoding="utf-8")
        binary.chmod(0o755)
        envelope = self.c.submit("ci.run", {"project": "gradled", "set": revision}).result(timeout=300)
        self.assertEqual(envelope.outcome, "ok", envelope.summary)
        self.assertEqual(envelope.data["set"], revision)
        step = envelope.data["engine"]["jobs"]["build"]["steps"][0]
        log = Path(step["log"]).read_text(encoding="utf-8")
        self.assertIn(f"-Pset.dir={self.store.parent / 'sets' / revision}", log)
        self.assertIn("-Pset.exact=true", log)
        self.assertIn("hello", log)
        self.assertEqual(step["name"], "gradle hello")

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
