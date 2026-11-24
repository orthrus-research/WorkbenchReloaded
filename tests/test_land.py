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
                                                  '[checks.build]\nsteps = [["{gradle}", "hello"], '
                                                  '["{python}", "-c", "import os; print(os.environ.get(\'GRADLE_OPTS\'))"]]\n',
                                                  encoding="utf-8")
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
        # A ./gradlew a step script starts gets the same values, as org.gradle.project.* system properties.
        opts = Path(envelope.data["engine"]["jobs"]["build"]["steps"][1]["log"]).read_text(encoding="utf-8")
        self.assertIn(f"-Dorg.gradle.project.set.dir={self.store.parent / 'sets' / revision}", opts)
        self.assertIn("-Dorg.gradle.project.set.exact=true", opts)

    def test_9e_a_ref_is_read_in_the_worktree_you_stand_in(self):
        from wb.land import _targets
        workspace = Workspace(self.d.config)
        tree = self.root / "lib-wt"
        git(self.lib, "worktree", "add", "-q", "-b", "feat/here", str(tree), "HEAD")
        (tree / "here.txt").write_text("here", encoding="utf-8")
        git(tree, "add", "-A")
        git(tree, "commit", "-q", "-m", "here")
        mine = git(tree, "rev-parse", "HEAD")
        self.assertEqual(_targets(workspace, ["lib=HEAD"], tree), {"lib": mine})  # this worktree's HEAD
        self.assertEqual(_targets(workspace, [], tree), {"lib": mine})
        self.assertEqual(_targets(workspace, ["two=HEAD"], tree), {"two": "HEAD"})  # another project: its own HEAD

    def test_9f_stand_ins_reach_gradle_and_vouch_for_nothing(self):
        from wb_core import artifacts
        lib_tree = git(self.lib, "rev-parse", "HEAD~1^{tree}")  # a tree CI built and kept (test_9b's hold landed after)
        kept = [p.stem for p in (self.store / "index" / "lib").glob("*.json")]
        lib_tree = lib_tree if lib_tree in kept else kept[0]
        records = self.root / "out" / "records" / "gradled"
        before = sorted(p.parent.name for p in records.glob("*/summary.json"))
        envelope = self.c.submit("ci.run", {"project": "gradled", "set": "b" * 40, "stand_ins": {"lib": lib_tree},
                                            "if_needed": False}).result(timeout=300)
        self.assertEqual(envelope.outcome, "ok", envelope.summary)
        log = Path(envelope.data["engine"]["jobs"]["build"]["steps"][0]["log"]).read_text(encoding="utf-8")
        self.assertIn("-Pset.with.lib=", log)
        folder = log.split("-Pset.with.lib=")[1].split()[0]
        self.assertTrue(folder.endswith(str(Path("stand-ins") / "lib")) or folder.endswith("stand-ins/lib"))
        after = sorted(p.parent.name for p in records.glob("*/summary.json"))
        self.assertEqual(after, before)  # against stand-in bytes the run vouches for nothing
        self.assertEqual(envelope.data["stand_ins"], {"lib": lib_tree})
        self.assertTrue(artifacts.find(self.store, "lib", lib_tree))

    def test_9g_only_lands_one_of_what_was_asked_and_tag_tags_its_commit(self):
        lib_commit, two_before = advance(self.lib, "v7"), self.set_json()["projects"]["two"]["commit"]
        advance(self.two, "v7")
        with self.assertRaises(WorkspaceError) as raised:
            self.lander().land({"lib": "HEAD"}, only=["nosuch"])
        self.assertIn("--only nosuch", str(raised.exception))
        result = self.lander().land({"lib": "HEAD", "two": "HEAD"}, only=["lib"], tag="rel-1")
        self.assertEqual(result["state"], "landed", self.said)
        lines = self.set_json()["projects"]
        self.assertEqual((lines["lib"]["commit"], lines["two"]["commit"]), (lib_commit, two_before))
        self.assertEqual(git(self.lib_remote, "rev-parse", "refs/tags/rel-1^{commit}"), lib_commit)
        self.assertFalse(git(self.two_remote, "tag", "--list", "rel-1"))
        self.assertIn("tag rel-1 on lib", git(self.home, "log", "-1", "--format=%B", "main"))
        workspace = Workspace(self.d.config)
        lib = workspace.projects()["lib"]
        self.assertEqual(workspace.landing_rule(lib, lib_commit, "refs/tags/rel-2"), "")  # the line: taken
        refused = workspace.landing_rule(lib, advance(self.lib, "v8"), "refs/tags/rel-2")
        self.assertIn("--tag rel-2", refused)  # never a line: wb land names the way
        with self.assertRaises(WorkspaceError) as raised:  # a scrub refusal on the tag publishes nothing
            self.lander().land({"lib": "HEAD"}, tag="forbiddenword-1")
        self.assertIn("scrub", str(raised.exception))
        self.assertFalse(git(self.lib_remote, "tag", "--list", "forbiddenword-1"))

    def test_9h_a_pass_writes_its_record_and_the_gate_record_in_the_store(self):
        from wb_core import passes
        line = self.set_json()["projects"]["lib"]
        tree = git(self.lib, "rev-parse", f"{line['commit']}^{{tree}}")
        found = list((self.store / "passes" / "lib" / tree).glob("build.*.json"))
        self.assertEqual(len(found), 1, found)
        record = json.loads(found[0].read_text(encoding="utf-8"))
        self.assertEqual((record["check"], record["tree"], record["commit"]), ("build", tree, line["commit"]))
        self.assertEqual(record["key"], passes.key("build", tree, record["identity"]))
        self.assertEqual(record["artifacts"]["lib-1.jar"]["sha256"], line["artifacts"]["mod"]["sha256"])
        gate = passes.read_gate(self.store, "lib", tree)
        self.assertEqual(gate["checks"], {"build": record["key"]})
        # The push gate reads that record: no run summary is needed for the tree any more.
        workspace = Workspace(self.d.config)
        verdict = workspace.gate(workspace.projects()["lib"],
                                 [f"refs/heads/feat/x {line['commit']} refs/heads/feat/x {'0' * 40}"])
        self.assertEqual((verdict["refused"], verdict["notes"]), ([], []))

    def test_9h2_a_tree_built_again_under_another_set_lands_the_bytes_its_pass_names(self):
        # A build whose bytes depend on the set (a manifest naming the platform's digest) gives one tree other files
        # under another set. The Store index keeps the tree's first build; the pass record under the set the landing
        # lands onto names this set's bytes, and those are what the line must carry.
        import hashlib
        commit = advance(self.lib, "v-set-dependent")
        ran = self.c.submit("ci.run", {"project": "lib", "commit": commit}).result(timeout=120)
        self.assertEqual(ran.outcome, "ok", ran.summary)
        tree = git(self.lib, "rev-parse", f"{commit}^{{tree}}")
        record_path, = (self.store / "passes" / "lib" / tree).glob("build.*.json")
        record = json.loads(record_path.read_text(encoding="utf-8"))
        first = record["artifacts"]["lib-1.jar"]["sha256"]
        rebuilt = b"lib built under another platform digest"
        digest = hashlib.sha256(rebuilt).hexdigest()
        kept = self.store / "files" / digest / "lib-1.jar"
        kept.parent.mkdir(parents=True, exist_ok=True)
        kept.write_bytes(rebuilt)
        record["artifacts"]["lib-1.jar"]["sha256"] = digest
        record_path.chmod(0o644)
        record_path.write_text(json.dumps(record), encoding="utf-8")
        result = self.lander().land({"lib": commit})
        self.assertEqual(result["state"], "landed", self.said)
        line = self.set_json()["projects"]["lib"]
        self.assertNotEqual(first, digest)
        self.assertEqual(line["artifacts"]["mod"]["sha256"], digest)

    def test_9i_a_killed_publisher_holds_lock_set_until_its_deadline_then_the_next_lands(self):
        advance(self.lib, "v9")
        deadline = 12
        # The publisher is killed with its landing refs pushed: its set.land job holds lock set to the deadline.
        killed = self.lander().land({"lib": "HEAD"}, deadline=deadline, stop_after="refs")
        self.assertEqual(killed["state"], "refs-pushed")
        stopped_at = time.time()
        commit = advance(self.two, "v9")
        self.said.clear()
        result = self.lander().land({"two": "HEAD"})
        waited = time.time() - stopped_at
        self.assertEqual(result["state"], "landed", self.said)
        self.assertEqual(self.set_json()["projects"]["two"]["commit"], commit)
        self.assertTrue(any("lock set held by" in s for s in self.said), self.said)
        self.assertGreaterEqual(waited, deadline - 1)  # it waited for the deadline, not a moment less
        record = landings.read(self.store, killed["landing"])
        self.assertEqual(record["state"], "abandoned")
        self.assertIn("deadline", record["why"])
        self.lander().reconcile()  # the killed landing's refs go with the next landing's reconcile
        self.assertFalse([r for r in self.remote_refs(self.lib_remote) if "refs/landed/" in r])

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


# A consumer's check that resolves its producer's file the way set.jar does: from the set the run builds against
# (-Pset.dir, which ci.run hands a step's ./gradlew in GRADLE_OPTS) and the Store, and that needs a symbol in it.
CONSUMER_CHECK = """import json, os, pathlib, sys
values = dict(a[len("-Dorg.gradle.project."):].split("=", 1) for a in os.environ.get("GRADLE_OPTS", "").split()
              if a.startswith("-Dorg.gradle.project.") and "=" in a)
doc = json.loads((pathlib.Path(values["set.dir"]) / "set.json").read_text(encoding="utf-8"))
item = doc["projects"]["lib"]["artifacts"]["mod"]
data = (pathlib.Path(values["set.store"]) / "files" / item["sha256"] / item["file"]).read_text(encoding="utf-8")
need = pathlib.Path("needs.txt").read_text(encoding="utf-8").strip()
if need not in data:
    sys.exit(f"cannot find symbol {need} in lib {item['sha256'][:12]}")
out = pathlib.Path("out")
out.mkdir(exist_ok=True)
(out / "app-1.jar").write_text("app built against " + item["sha256"], encoding="utf-8")
"""
CONSUMER = """schema = 1
gate = ["build"]

[project]
name = "app"

[uses]
lib = "mod"

[artifacts.mod]
check = "build"
file = "out/app-*.jar"{mod_id}

[checks.build]
steps = [["{{python}}", "check.py"]]
{launch}"""


class Reach(unittest.TestCase):
    """A producer and its consumer: landed together, the consumer builds against the producer's new line; a producer
    landed alone reaches the consumer, whose gate runs against the would-be set (or whose pass stands); a
    byte-identical producer reaches nobody; and the Set gate refuses a private URL and two builds of one mod id."""

    @classmethod
    def setUpClass(cls):
        import os
        root = Path(tempfile.mkdtemp(prefix="wb-reach-"))
        cls.root = root
        cls.lib, cls.lib_remote = project(root, "lib")
        cls.app, cls.app_remote = project(root, "app")
        cls.home = root / "set.git"
        git(root, "init", "-q", "--bare", "-b", "main", str(cls.home))
        seed = root / "seed"
        seed.mkdir()
        git(seed, "init", "-q", "-b", "main")
        platform = {"java": 26, "gradle": {"version": "9.8.1"}}
        (seed / "set.json").write_text(json.dumps({**EMPTY_SET, "platform": platform}, indent=1) + "\n",
                                       encoding="utf-8")
        git(seed, "add", "-A")
        git(seed, "commit", "-q", "-m", "seed")
        git(seed, "push", "-q", str(cls.home), "main")
        out = root / "out"
        cls.store = out / "store"
        # The set's Gradle in the Store, so a run hands its steps -Pset.dir and -Pset.store (never run here).
        binary = cls.store / "gradle" / "9.8.1" / "gradle-9.8.1" / "bin" / ("gradle.bat" if os.name == "nt" else "gradle")
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_text("@echo gradle %*\r\n" if os.name == "nt" else '#!/bin/sh\necho gradle "$@"\n', encoding="utf-8")
        binary.chmod(0o755)
        cls.d = Daemon(f"""
            [workspace]
            set = '{cls.home}'

            [modules.ci]
            store = '{cls.store.as_posix()}'
            records = '{(out / "records").as_posix()}'

            [modules.ci.projects.lib]
            repo = '{cls.lib}'
            jdk = ""

            [modules.ci.projects.app]
            repo = '{cls.app}'
            jdk = ""
            """, root=root).start()
        cls.c = cls.d.client
        cls.said = []

    @classmethod
    def tearDownClass(cls):
        cls.d.stop()

    def lander(self):
        self.said.clear()
        return Lander(Workspace(self.d.config), self.c, say=self.said.append)

    def set_json(self):
        return json.loads(git(self.home, "show", "main:set.json"))

    def consumer(self, needs: str, mod_id: str = "", launch: str = "") -> str:
        (self.app / "project.toml").write_text(CONSUMER.format(
            mod_id=f'\nmod_id = "{mod_id}"' if mod_id else "", launch=launch), encoding="utf-8")
        (self.app / "check.py").write_text(CONSUMER_CHECK, encoding="utf-8")
        (self.app / "needs.txt").write_text(needs + "\n", encoding="utf-8")
        git(self.app, "add", "-A")
        git(self.app, "commit", "-q", "-m", f"needs {needs}")
        return git(self.app, "rev-parse", "HEAD")

    def test_1_a_producer_and_a_consumer_needing_its_new_symbol_land_together(self):
        self.assertEqual(self.lander().land({"lib": "HEAD"})["state"], "landed", self.said)  # lib v1: no symbol yet
        lib = advance(self.lib, "v2 EMISSIVE_PARTICLES")
        app = self.consumer("EMISSIVE_PARTICLES")
        result = self.lander().land({"lib": "HEAD", "app": "HEAD"})
        self.assertEqual(result["state"], "landed", self.said)
        lines = self.set_json()["projects"]
        self.assertEqual((lines["lib"]["commit"], lines["app"]["commit"]), (lib, app))
        # app ran after lib, against the would-be set that names lib's new file, not against the head.
        order = [s for s in self.said if s.startswith("checking ")]
        self.assertTrue(order[0].startswith("checking lib") and order[1].startswith("checking app"), order)
        self.assertIn("would-be set candidate-", order[1])
        tree = git(self.app, "rev-parse", "HEAD^{tree}")
        record = json.loads(next((self.store / "passes" / "app" / tree).glob("build.*.json")).read_text("utf-8"))
        self.assertTrue(record["set"].startswith("candidate-"), record)
        built = (self.store / "files" / lines["app"]["artifacts"]["mod"]["sha256"] / "app-1.jar").read_text("utf-8")
        self.assertEqual(built, "app built against " + lines["lib"]["artifacts"]["mod"]["sha256"])

    def test_2_a_producer_alone_reaches_its_consumer_and_a_recorded_pass_stands(self):
        app = self.set_json()["projects"]["app"]["commit"]
        advance(self.lib, "v3 EMISSIVE_PARTICLES")
        dry = self.lander().land({"lib": "HEAD"}, dry=True)  # who breaks: the consumer runs against the would-be set
        self.assertEqual(dry["state"], "dry", self.said)
        self.assertEqual(dry["reached"]["app"]["why"], ["uses lib"])
        self.assertTrue(any(s.startswith("reached app") and "checking it" in s for s in self.said), self.said)
        result = self.lander().land({"lib": "HEAD"})
        self.assertEqual(result["state"], "landed", self.said)
        self.assertEqual((result["reached"]["app"]["commit"], result["reached"]["app"]["stands"]), (app, True))
        self.assertTrue(any(s.startswith("reached app") and "its pass stands" in s for s in self.said), self.said)
        self.assertIn(f"reached app at {app[:12]} (uses lib): gate passed",
                      git(self.home, "log", "-1", "--format=%B", "main"))

    def test_3_a_byte_identical_producer_reaches_nobody(self):
        (self.lib / "README.md").write_text("docs only\n", encoding="utf-8")
        git(self.lib, "add", "-A")
        git(self.lib, "commit", "-q", "-m", "docs")
        result = self.lander().land({"lib": "HEAD"})
        self.assertEqual((result["state"], result["reached"]), ("landed", {}), self.said)
        self.assertTrue(any("byte-identical" in s for s in self.said), self.said)

    def test_4_a_producer_that_breaks_its_consumer_lands_nothing(self):
        before = (git(self.home, "rev-parse", "main"), git(self.lib_remote, "rev-parse", "main"))
        advance(self.lib, "v5 without the symbol")
        with self.assertRaises(WorkspaceError) as raised:
            self.lander().land({"lib": "HEAD"})
        self.assertIn("would break what it reaches", str(raised.exception))
        self.assertIn("app", str(raised.exception))
        self.assertEqual((git(self.home, "rev-parse", "main"), git(self.lib_remote, "rev-parse", "main")), before)
        git(self.lib, "reset", "-q", "--hard", "HEAD~1")

    def test_5_the_set_gate_refuses_two_builds_of_one_mod_id_and_warns_of_none_declared(self):
        launch = '\n[launch.default]\nmembers = ["app:mod@both", "lib:mod@both"]\n'
        self.consumer("EMISSIVE_PARTICLES", mod_id="app", launch=launch)
        result = self.lander().land({"app": "HEAD"})
        self.assertEqual(result["state"], "landed", self.said)
        self.assertEqual(self.set_json()["projects"]["app"]["artifacts"]["mod"]["mod_id"], "app")
        self.assertTrue(any("warning:" in s and "lib:mod@both declares no mod id" in s for s in self.said), self.said)
        before = git(self.home, "rev-parse", "main")
        manifest = (self.lib / "project.toml").read_text(encoding="utf-8")
        (self.lib / "project.toml").write_text(manifest.replace('file = "out/lib-*.jar"',
                                                                'file = "out/lib-*.jar"\nmod_id = "app"'),
                                               encoding="utf-8")
        advance(self.lib, "v6 EMISSIVE_PARTICLES")
        with self.assertRaises(WorkspaceError) as raised:
            self.lander().land({"lib": "HEAD"})
        self.assertIn("two builds of one mod id: app from app:mod@both, lib:mod@both", str(raised.exception))
        self.assertEqual(git(self.home, "rev-parse", "main"), before)

    def test_6_the_set_gate_refuses_a_url_a_public_set_may_not_carry(self):
        from wb_core import setfile
        drive = "Z:" + "\\repos\\lib"  # a drive path, built so no literal one sits in the tree
        for url, why in (("file:///srv/git/lib.git", "file:"), (drive, "local path"),
                         ("/srv/git/lib.git", "local path"), ("http://example.org/lib.git", "not encrypted"),
                         ("https://user:secret@example.org/lib.git", "credentials"),
                         ("https://token@example.org/lib.git", "credentials"),
                         ("https://127.0.0.1/lib.git", "private"), ("https://buildbox/lib.git", "private"),
                         ("ssh://me@example.org/lib.git", "credentials")):
            self.assertIn(why, setfile.url_problem(url) or "", url)
        scp = "git" + "@example.org:org/lib.git"  # git's scp-like address, not anyone's mail
        for url in ("https://github.com/org/lib.git", scp, "ssh://git@example.org/lib.git"):
            self.assertIsNone(setfile.url_problem(url), url)
        self.assertEqual(setfile.private_urls({"projects": {"lib": {"url": "https://a:b@example.org/x"}}}),
                         ["projects.lib.url: credentials in the URL; a public set names none"])  # never the URL
        before = git(self.home, "rev-parse", "main")
        with self.assertRaises(WorkspaceError) as raised:
            self.lander().land({}, urls={"lib": "http://example.org/lib.git"})
        self.assertIn("not encrypted", str(raised.exception))
        self.assertEqual(git(self.home, "rev-parse", "main"), before)


if __name__ == "__main__":
    unittest.main()
