"""The ci module: a project's jobs from clean clones, pinned repositories at their pins, records kept in Workbench, the
push gate over them, and the Gradle home that serves toolchain's rescue repository. The source checkouts are only ever
read."""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import REPO, Daemon  # noqa: E402

JOBS = """
[settings]
name = "demo"

[repositories.lib]
path = "../lib"
pin = { file = "lib.lock.json", key = "commit" }

[workflows]
ci = ["hello", "pinned"]

[jobs.hello]
steps = [{ name = "say", run = ["{python}", "-c", "import pathlib; print(pathlib.Path('src/a.txt').read_text())"] }]

[jobs.pinned]
steps = [{ name = "lib", run = ["{python}", "-c", "import pathlib, sys; t = pathlib.Path('../lib/lib.txt').read_text(); sys.exit(0 if t == 'v1' else 3)"] }]

[jobs.built]
artifacts = ["build/out.jar"]
steps = [{ name = "build", run = ["{python}", "-c", "import pathlib; p = pathlib.Path('build'); p.mkdir(exist_ok=True); (p / 'out.jar').write_bytes(b'built bytes')"] }]

[jobs.clean-env]
steps = [{ name = "env", run = ["{python}", "-c", "import os, sys; sys.exit(3 if [k for k in os.environ if k.startswith('WB_') and k not in ('WB_PIN_CACHE', 'WB_COLD', 'WB_GRADLE')] else 0)"] }]

[jobs.unbuilt]
artifacts = ["build/missing.jar"]
steps = [{ name = "nothing", run = ["{python}", "-c", "pass"] }]

[jobs.lines]
steps = [{ name = "lines", run = ["{python}", "-c", "import pathlib, sys; t = pathlib.Path('src/a.txt').read_text().splitlines(); sys.exit(0 if t == ['line one', 'line two'] else 3)"] }]
"""


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args], cwd=cwd,
                          check=True, capture_output=True, text=True).stdout.strip()


def host_tree(repo: Path, excludes: Path) -> str:
    """The tree `git add --all` would commit in repo on a host whose git converts CRLF (Git for Windows' default)."""
    env = {**os.environ, "GIT_INDEX_FILE": str(repo.parent / "expected-index")}
    settings = ["-c", "core.autocrlf=true", "-c", f"core.excludesFile={excludes.as_posix()}"]
    for args in (["read-tree", "HEAD"], ["add", "--all"]):
        subprocess.run(["git", *settings, *args], cwd=repo, env=env, check=True, capture_output=True)
    tree = subprocess.run(["git", "write-tree"], cwd=repo, env=env, check=True, capture_output=True, text=True)
    Path(env["GIT_INDEX_FILE"]).unlink()
    return tree.stdout.strip()


class Ci(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(tempfile.mkdtemp(prefix="wb-ci-"))
        base = root / "data"  # the harness's read-only data root
        cls.lib, cls.demo = base / "lib", base / "demo"
        for repo in (cls.lib, cls.demo):
            repo.mkdir(parents=True)
            git(repo, "init", "-q", "-b", "main")
        (cls.lib / "lib.txt").write_text("v1", encoding="utf-8")
        git(cls.lib, "add", "-A")
        git(cls.lib, "commit", "-q", "-m", "v1")
        cls.lib_v1 = git(cls.lib, "rev-parse", "HEAD")
        (cls.lib / "lib.txt").write_text("v2", encoding="utf-8")  # the lib's HEAD moves on; the pin stays at v1
        git(cls.lib, "commit", "-q", "-am", "v2")
        (cls.demo / "src").mkdir()
        (cls.demo / "src" / "a.txt").write_text("hello", encoding="utf-8")
        (cls.demo / "actions.toml").write_text(JOBS, encoding="utf-8")
        (cls.demo / "requirements.txt").write_text("", encoding="utf-8")
        (cls.demo / "lib.lock.json").write_text(json.dumps({"commit": cls.lib_v1}), encoding="utf-8")
        (cls.demo / ".gitignore").write_text("build/\n", encoding="utf-8")  # .local is not ignored: ci must keep its links out
        git(cls.demo, "add", "-A")
        git(cls.demo, "commit", "-q", "-m", "demo")
        cls.head = git(cls.demo, "rev-parse", "HEAD")
        cls.d = Daemon(f"""
            [modules.ci]
            excludes = ["NOTES.local.md"]
            git_config = {{ "core.autocrlf" = "true" }}

            [modules.ci.projects.demo]
            repo = '{cls.demo}'
            jdk = ""
            python = ["requirements.txt"]

            [modules.toolchain.substitutions]
            "com.example:vanished:1.0" = "com.example:retained:1.0"
            """, root=root).start()
        cls.c = cls.d.client

    @classmethod
    def tearDownClass(cls):
        cls.d.stop()

    def test_runs_from_a_clean_clone_and_gates_on_the_records(self):
        first = self.c.submit("ci.run", {"project": "demo"}).result(timeout=600)
        self.assertEqual(first.outcome, "ok", first.summary)
        self.assertEqual(sorted(first.data.passed), ["hello", "pinned"])
        self.assertEqual(first.data.pinned["lib"], self.lib_v1)  # the pinned commit, not the lib's HEAD
        self.assertEqual(first.data.trees["demo"], git(self.demo, "rev-parse", "HEAD^{tree}"))  # the commit's own tree
        self.assertFalse((self.demo / ".local").exists())          # the checkout was only read
        self.assertEqual(git(self.demo, "status", "--porcelain"), "")
        again = self.c.submit("ci.run", {"project": "demo"}).result(timeout=600)
        self.assertEqual(again.outcome, "ok", again.summary)
        self.assertEqual(again.data.passed, [])                    # already passed for these trees
        status = self.c.submit("ci.status", {"project": "demo"}).result(timeout=120)
        self.assertEqual((status.outcome, status.data.missing), ("ok", []), status.summary)
        line = f"refs/heads/main {self.head} refs/heads/main {'0' * 40}"
        passed = self.c.submit("ci.gate", {"project": "demo", "refs": [line]}).result(timeout=120)
        self.assertEqual(passed.outcome, "ok", passed.summary)
        (self.demo / "src" / "a.txt").write_text("changed", encoding="utf-8")
        git(self.demo, "commit", "-q", "-am", "untested")
        untested = git(self.demo, "rev-parse", "HEAD")
        refs = self.d.data / "refs.txt"
        refs.write_text(f"refs/heads/main {untested} refs/heads/main {self.head}\n", encoding="utf-8")
        refused = self.c.submit("ci.gate", {"project": "demo", "refs_file": str(refs)}).result(timeout=120)
        self.assertEqual(refused.outcome, "finding", refused.summary)
        self.assertIn("has not passed", refused.data.refused[0]["reason"])
        git(self.demo, "reset", "-q", "--hard", self.head)

    def test_status_and_the_gate_read_each_commits_own_jobs_file(self):
        ran = self.c.submit("ci.run", {"project": "demo"}).result(timeout=600)  # HEAD passes (or already had)
        self.assertEqual(ran.outcome, "ok", ran.summary)
        git(self.demo, "rm", "-q", "actions.toml")
        git(self.demo, "commit", "-q", "-m", "no jobs file")
        bare = git(self.demo, "rev-parse", "HEAD")
        try:  # the checkout now has no jobs file, but the commit asked about does
            status = self.c.submit("ci.status", {"project": "demo", "commit": self.head}).result(timeout=120)
            self.assertEqual((status.outcome, status.data.missing), ("ok", []), status.summary)
            nothing = self.c.submit("ci.run", {"project": "demo"}).result(timeout=300)  # HEAD has none
            self.assertEqual(nothing.outcome, "usage", nothing.summary)
            self.assertIn("name another with commit=", nothing.summary)
            missing = self.c.submit("ci.status", {"project": "demo", "commit": bare}).result(timeout=120)
            self.assertEqual(missing.outcome, "usage", missing.summary)
            lines = [f"refs/heads/main {self.head} refs/heads/main {'0' * 40}",
                     f"refs/heads/side {bare} refs/heads/side {'0' * 40}"]
            gated = self.c.submit("ci.gate", {"project": "demo", "refs": lines}).result(timeout=120)
            self.assertEqual(gated.outcome, "finding", gated.summary)
            self.assertEqual([r["commit"] for r in gated.data.refused], [bare])  # the tested commit still passes
            self.assertIn("no project.toml or actions.toml", gated.data.refused[0]["reason"])
        finally:
            git(self.demo, "reset", "-q", "--hard", self.head)

    def test_a_linked_worktree_runs_its_own_working_tree(self):
        other = self.demo.parent / "demo-wt"
        git(self.demo, "worktree", "add", "-q", "-b", "side", str(other))
        try:
            (other / "src" / "a.txt").write_text("from the side worktree", encoding="utf-8")
            result = self.c.submit("ci.run", {"project": "demo", "commit": "worktree", "checkout": str(other),
                                              "workflow": "hello"}).result(timeout=600)
            self.assertEqual(result.outcome, "ok", result.summary)
            stranger = self.c.submit("ci.run", {"project": "demo", "commit": "worktree", "checkout": str(self.lib),
                                                "workflow": "hello"}).result(timeout=120)
            self.assertEqual(stranger.outcome, "usage")  # another repository's checkout is refused
        finally:
            git(self.demo, "worktree", "remove", "--force", str(other))
            git(self.demo, "branch", "-q", "-D", "side")

    def test_a_working_tree_run_tests_the_tree_the_host_would_commit(self):
        (self.demo / "src" / "gone.txt").write_text("tracked, then deleted", encoding="utf-8")
        git(self.demo, "add", "src/gone.txt")
        git(self.demo, "commit", "-q", "-m", "gone")
        try:
            (self.demo / "src" / "gone.txt").unlink()
            (self.demo / "src" / "a.txt").write_bytes(b"line one\r\nline two\r\n")  # what autocrlf leaves on disk
            (self.demo / "src" / "new.txt").write_bytes(b"untracked\r\n")
            (self.demo / "NOTES.local.md").write_text("local only", encoding="utf-8")  # kept out by the excludes
            excludes = self.demo.parent / "expected-excludes"
            excludes.write_text("NOTES.local.md\n", encoding="utf-8")
            expected = host_tree(self.demo, excludes)
            result = self.c.submit("ci.run", {"project": "demo", "commit": "worktree", "workflow": "lines"}) \
                .result(timeout=600)
            self.assertEqual(result.outcome, "ok", result.summary)
            # The blobs are LF and the clone checks them out as any checkout on this machine would.
            self.assertEqual(result.data.trees["demo"], expected)
        finally:
            (self.demo / "src" / "new.txt").unlink(missing_ok=True)
            (self.demo / "NOTES.local.md").unlink(missing_ok=True)
            git(self.demo, "reset", "-q", "--hard", self.head)

    def test_the_overlay_takes_the_hosts_blobs_not_the_bytes_on_disk(self):
        sys.path.insert(0, str(REPO / "modules" / "tools"))
        sys.path.insert(0, str(REPO / "modules" / "ci"))
        from wb_ci import commands

        class Ctx:  # runs git as the module would
            spec = {}

            @staticmethod
            def run(args, cwd=None, env=None, log_output=True):
                return subprocess.run(args, cwd=cwd, env={**os.environ, **(env or {})}, text=True,
                                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        clone = self.demo.parent / "overlay-clone"
        git(self.demo.parent, "clone", "-q", "--shared", "--no-checkout", str(self.demo), str(clone))
        git(clone, "config", "core.autocrlf", "false")  # a clone that converts nothing, like a Linux container's
        git(clone, "checkout", "-q", "--detach", self.head)
        (self.demo / "src" / "a.txt").write_bytes(b"line one\r\nline two\r\n")
        try:
            commands._overlay_worktree(Ctx(), self.demo, clone, ["NOTES.local.md"], {"core.autocrlf": True})
            self.assertEqual((clone / "src" / "a.txt").read_bytes(), b"line one\nline two\n")
            excludes = self.demo.parent / "expected-excludes"
            excludes.write_text("NOTES.local.md\n", encoding="utf-8")
            self.assertEqual(git(clone, "add", "--all", "--dry-run"), "add 'src/a.txt'")  # uncommitted over HEAD
            self.assertEqual(host_tree(clone, excludes), host_tree(self.demo, excludes))
        finally:
            git(self.demo, "checkout", "--", "src/a.txt")

    def test_a_producing_jobs_artifacts_are_kept_in_the_store_by_tree(self):
        import hashlib
        sys.path.insert(0, str(REPO / "core"))
        from wb_core import artifacts
        result = self.c.submit("ci.run", {"project": "demo", "workflow": "built"}).result(timeout=600)
        self.assertEqual(result.outcome, "ok", result.summary)
        digest = hashlib.sha256(b"built bytes").hexdigest()
        self.assertEqual(result.data.stored["out.jar"]["sha256"], digest)
        store = self.d.state / "store"
        tree = git(self.demo, "rev-parse", "HEAD^{tree}")
        index = artifacts.find(store, "demo", tree)
        self.assertEqual(index["artifacts"]["out.jar"]["sha256"], digest)
        self.assertIn(self.head, index["commits"])
        self.assertEqual(artifacts.path_of(store, digest, "out.jar").read_bytes(), b"built bytes")
        self.assertTrue((self.d.state / "cache" / "pins" / digest).is_file())  # where WB_PIN_CACHE readers look
        # A cold rebuild of what the Store keeps comes out to the same bytes, and the build says so.
        verified = self.c.submit("store.verify", {"project": "demo", "commit": self.head}).result(timeout=600)
        self.assertEqual(verified.outcome, "ok", verified.summary)
        self.assertTrue(artifacts.find(store, "demo", tree)["verified"]["reproduced"])
        self.assertEqual(len(verified.children), 1)  # one ci.run, cold
        # The cold builds read no Gradle build cache: every build's Gradle home has the script that turns it off.
        self.assertIn("WB_COLD", (self.d.state / "cache" / "gradle" / "init.d" / "workbench-cold.gradle").read_text())

    def test_a_step_gets_none_of_workbenchs_own_environment(self):
        result = self.c.submit("ci.run", {"project": "demo", "workflow": "clean-env"}).result(timeout=600)
        self.assertEqual(result.outcome, "ok", result.summary)

    def test_a_job_that_fails_after_its_steps_says_why(self):
        result = self.c.submit("ci.run", {"project": "demo", "workflow": "unbuilt"}).result(timeout=600)
        self.assertEqual(result.outcome, "finding", result.summary)
        job = [r for r in result.reasons if r["kind"] == "job"]
        self.assertTrue(job and "no file matches its artifact build/missing.jar" in job[0]["detail"], result.reasons)

    def test_working_tree_run_and_a_failing_job(self):
        (self.demo / "src" / "a.txt").write_text("uncommitted", encoding="utf-8")
        try:
            result = self.c.submit("ci.run", {"project": "demo", "commit": "worktree", "workflow": "hello"}) \
                .result(timeout=600)
            self.assertEqual(result.outcome, "ok", result.summary)
            self.assertTrue(result.data.worktree)
        finally:
            git(self.demo, "checkout", "--", "src/a.txt")
        (self.demo / "lib.lock.json").write_text(json.dumps({"commit": git(self.lib, "rev-parse", "HEAD")}),
                                                 encoding="utf-8")
        git(self.demo, "commit", "-q", "-am", "pin v2")
        try:
            failed = self.c.submit("ci.run", {"project": "demo", "workflow": "pinned"}).result(timeout=600)
            self.assertEqual(failed.outcome, "finding", failed.summary)  # v2 makes the pinned job exit 3
            self.assertEqual(failed.data.failed, ["pinned"])
            step = [r for r in failed.reasons if r["kind"] == "step"]  # the cause in the envelope, with where to look
            self.assertTrue(step and step[0]["name"].startswith("pinned / "), failed.reasons)
            self.assertIn("exited 3", step[0]["detail"])
            self.assertTrue(step[0]["fix"].startswith("read the step log: "), step[0])
        finally:
            git(self.demo, "reset", "-q", "--hard", self.head)

    def test_every_build_gets_toolchains_rescue_repository_in_the_gradle_home(self):
        home = self.d.state / "cache" / "gradle"
        stale = home / "init.d" / "workbench-repository.gradle"
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_text("// an earlier run's script\n", encoding="utf-8")
        # A dry run records no pass, so the other tests still see HEAD untested; it prepares the Gradle home all the same.
        result = self.c.submit("ci.run", {"project": "demo", "workflow": "hello", "dry_run": True}).result(timeout=600)
        self.assertEqual(result.outcome, "ok", result.summary)
        script = stale.read_text(encoding="utf-8")  # written afresh from toolchain's config, every run
        self.assertTrue(script.startswith("// Written by Workbench (toolchain.repo-init)"), script[:200])
        self.assertIn("['com.example:vanished:1.0', 'com.example:retained:1.0']", script)
        served = result.data.repository
        self.assertEqual((served["substitutions"], served["versions"]), (1, 0))
        self.assertEqual(served["script"], hashlib.sha256(script.encode("utf-8")).hexdigest())
        # Asked inline, in the run's own reservation: no child job that could queue behind the run's peers.
        self.assertNotIn("job", served)
        self.assertEqual(result.children, [])

    def test_hook_installs_and_restores(self):
        sys.path.insert(0, str(REPO / "modules" / "tools"))
        sys.path.insert(0, str(REPO / "modules" / "ci"))
        from wb_ci import commands

        class Ctx:
            machine = "host"
            spec = {}
            config = {"projects": {"demo": {"repo": str(self.demo)}}}
            notes = []

            def summary(self, text):
                self.notes.append(text)

            class log:
                @staticmethod
                def info(text):
                    pass
        hooks = self.demo / ".git" / "hooks"
        hooks.mkdir(exist_ok=True)
        (hooks / "pre-push").write_text("#!/bin/sh\necho mine\n", encoding="utf-8")
        commands.hook(Ctx(), "demo", True)
        text = (hooks / "pre-push").read_text(encoding="utf-8")
        self.assertIn("ci.gate project=demo", text)
        self.assertIn("wb.cmd" if os.name == "nt" else "/bin/wb", text)
        self.assertTrue((hooks / "pre-push.before-workbench").exists())
        commands.hook(Ctx(), "demo", False)
        self.assertEqual((hooks / "pre-push").read_text(encoding="utf-8"), "#!/bin/sh\necho mine\n")


CHECK = """import sys
print("on", sys.version.split()[0], "venv" if sys.prefix != sys.base_prefix else "bare")
sys.exit({code})
"""


class Tools(unittest.TestCase):
    """A check's tools (another project's files it reads) are laid beside the clone at the commit the set names."""

    @classmethod
    def setUpClass(cls):
        for module in ("tools", "build", "lab", "ci"):
            sys.path.insert(0, str(REPO / "modules" / module))
        from wb_ci import commands
        cls.commands = commands

    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="wb-tools-"))
        self.cube = self.base / "Cube"
        self.cube.mkdir()
        git(self.cube, "init", "-q", "-b", "main")
        for name, text in {"tools/a.py": "a1", "tools/sub/b.py": "b1", "lab/run.properties": "v=1", "README": "r"}.items():
            (self.cube / name).parent.mkdir(parents=True, exist_ok=True)
            (self.cube / name).write_text(text, encoding="utf-8")
        git(self.cube, "add", "-A")
        git(self.cube, "commit", "-q", "-m", "one")
        self.line = git(self.cube, "rev-parse", "HEAD")
        (self.cube / "tools/a.py").write_text("a2", encoding="utf-8")  # the checkout moves on; the line does not
        git(self.cube, "commit", "-q", "-am", "two")
        self.store = self.base / "out" / "store"
        self.store.mkdir(parents=True)
        export = self.base / "out" / "sets" / "r1" / "set.json"
        export.parent.mkdir(parents=True)
        export.write_text(json.dumps({"schema": 1, "projects": {"cube": {"commit": self.line, "artifacts": {}}}}),
                          encoding="utf-8")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.base, ignore_errors=True)

    def lay_out(self, tools):
        from unittest import mock
        from wb_core.module import Context
        ctx = Context({"job": "direct", "command": "ci.run", "module": "ci", "work": str(self.base / "job" / "work"),
                       "out": str(self.base / "job" / "out"), "module_config": {}}, lambda *a, **k: None)
        config = type("C", (), {"jobs": {"quality": {"tools": tools}, "build": {}}})()
        work = self.base / "trees" / "run"
        with mock.patch.object(self.commands, "_store_root", return_value=self.store), \
                mock.patch.object(self.commands, "_project", return_value={"source": self.cube}):
            return self.commands._tools(ctx, config, work, "r1"), work / "Cube"

    def test_the_lines_files_matching_each_glob_are_laid_beside_the_clone(self):
        laid, folder = self.lay_out(["cube:tools/**", "cube:lab/run.properties"])
        self.assertEqual(laid, {"cube": self.line})
        self.assertEqual((folder / "tools/a.py").read_text(encoding="utf-8"), "a1")
        self.assertTrue((folder / "tools/sub/b.py").is_file())
        self.assertTrue((folder / "lab/run.properties").is_file())
        self.assertFalse((folder / "README").exists())

    def test_a_project_with_no_line_follows_its_branch_not_its_checkout(self):
        main = git(self.cube, "rev-parse", "HEAD")
        git(self.cube, "checkout", "-q", "-b", "parked", self.line)  # the home checkout sits elsewhere
        laid, folder = self.lay_out(["other:tools/**"])  # _project gives the same repository: no line for "other"
        self.assertEqual(laid, {"other": main})
        self.assertEqual((folder / "tools/a.py").read_text(encoding="utf-8"), "a2")

    def test_no_tools_lays_out_nothing(self):
        self.assertEqual(self.lay_out([])[0], {})


class SetIdentity(unittest.TestCase):
    """A converted project's run is given the set's identity: the platform and the lines, holds and third entries it
    uses (a line it does not use moves nothing), and the platform's JDK builds; a project on actions.toml gets none."""

    @classmethod
    def setUpClass(cls):
        for module in ("tools", "build", "lab", "ci"):
            sys.path.insert(0, str(REPO / "modules" / module))
        from wb_ci import commands
        cls.commands = commands

    def identity(self, doc, manifest="project.toml"):
        from unittest import mock
        from wb_core import setfile
        from wb_core.module import Context
        base = Path(tempfile.mkdtemp(prefix="wb-identity-"))
        self.addCleanup(__import__("shutil").rmtree, base, True)
        setfile.export(base / "out" / "sets", "r1", json.dumps(doc))
        ctx = Context({"job": "direct", "command": "ci.run", "module": "ci", "work": str(base / "w"),
                       "out": str(base / "o"), "module_config": {}}, lambda *a, **k: None)
        config = type("C", (), {"path": Path(manifest), "data": {"uses": {"cube": "compile", "ui": "compile"}},
                                "settings": {"name": "game"}})()
        with mock.patch.object(self.commands, "_store_root", return_value=base / "out" / "store"):
            return self.commands._set_identity(ctx, config, "r1")

    def doc(self, **changes):
        doc = {"schema": 1, "projects": {"cube": {"commit": "c" * 40}, "other": {"commit": "e" * 40}},
               "third": {"ui": {"sha256": "1" * 64}}, "holds": {},
               "platform": {"java": 26, "jdk": {"26": {"build": "26.0.2.1+1"}}, "logic": "l1"}}
        for key, value in changes.items():
            section, _, name = key.partition("__")
            doc[section][name] = value
        return doc

    def test_what_moves_the_identities(self):
        base = self.identity(self.doc())
        self.assertEqual(set(base), {"WB_SET_IDENTITY", "WB_JDK_IDENTITY"})
        self.assertEqual(self.identity(self.doc(projects__other={"commit": "f" * 40})), base)  # not used: no move
        cube = self.identity(self.doc(projects__cube={"commit": "d" * 40}))
        self.assertNotEqual(cube["WB_SET_IDENTITY"], base["WB_SET_IDENTITY"])
        self.assertEqual(cube["WB_JDK_IDENTITY"], base["WB_JDK_IDENTITY"])
        jdk = self.identity(self.doc(platform__jdk={"26": {"build": "26.0.3+4"}}))
        self.assertNotEqual(jdk["WB_JDK_IDENTITY"], base["WB_JDK_IDENTITY"])
        self.assertNotEqual(self.identity(self.doc(platform__logic="l2"))["WB_SET_IDENTITY"], base["WB_SET_IDENTITY"])
        self.assertEqual(self.identity(self.doc(), manifest="actions.toml"), {})


class PythonLookup(unittest.TestCase):
    """ci.matrix finds a version wherever the host keeps it, and takes only an interpreter that says it is that one."""

    @classmethod
    def setUpClass(cls):
        for module in ("build", "ci"):
            sys.path.insert(0, str(REPO / "modules" / module))
        from wb_ci import matrix
        cls.matrix = matrix

    def test_the_py_launchers_list_is_read_with_any_company(self):
        drive = "X" + ":"  # an install folder as the launcher prints it, spaces and all
        own, other = f"{drive}\\Python314\\python.exe", f"{drive}\\Program Files\\uv python\\python.exe"
        listed = [f" -V:3.14 *        {own}", f" -V:Astral/CPython3.11.15 {other}"]
        named = [self.matrix.LISTED.match(line) for line in listed]
        self.assertEqual([(n["tag"], n["path"]) for n in named], [("3.14", own), ("Astral/CPython3.11.15", other)])

    def test_a_version_is_taken_only_from_an_interpreter_that_is_it(self):
        from unittest import mock
        answers = {"a": ("/py/a", "3.10.1"), "b": ("/py/b", "3.11.2")}
        with mock.patch.object(self.matrix, "_candidates", return_value=[["a"], ["b"], ["gone"]]), \
                mock.patch.object(self.matrix, "_ask", side_effect=lambda argv: answers.get(argv[0])):
            self.assertEqual(self.matrix.find_python("3.11"), "/py/b")
            self.assertEqual(self.matrix.find_python("3.11.2"), "/py/b")
            self.assertIsNone(self.matrix.find_python("3.11.3"))
            self.assertIsNone(self.matrix.find_python("3.1"))  # 3.10 is not 3.1
            self.assertEqual(self.matrix.find_python("pypy3.10"), "/py/a")  # not a version: the first answer


class Matrix(unittest.TestCase):
    """ci.matrix: a command run per Python in a clean clone; failures and missing Pythons are findings."""

    @classmethod
    def setUpClass(cls):
        for module in ("tools", "build", "lab", "ci"):
            sys.path.insert(0, str(REPO / "modules" / module))
        from wb_ci import matrix
        cls.matrix = matrix
        cls.root = Path(tempfile.mkdtemp(prefix="wb-matrix-"))
        cls.repo = cls.root / "repo"
        cls.repo.mkdir()
        git(cls.repo, "init", "-q", "-b", "main")
        (cls.repo / "requirements.txt").write_text("", encoding="utf-8")
        (cls.repo / "check.py").write_text(CHECK.format(code=0), encoding="utf-8")
        (cls.repo / "pkg" / "src").mkdir(parents=True)  # a package whose tests run in its folder with src on the path
        (cls.repo / "pkg" / "src" / "mod.py").write_text('NAME = "from src"\n', encoding="utf-8")
        (cls.repo / "pkg" / "run.py").write_text('import os\nimport mod\nprint(mod.NAME, os.environ["LANE"])\n',
                                                 encoding="utf-8")
        git(cls.repo, "add", "-A")
        git(cls.repo, "commit", "-q", "-m", "passes")
        cls.good = git(cls.repo, "rev-parse", "HEAD")
        (cls.repo / "check.py").write_text(CHECK.format(code=3), encoding="utf-8")
        git(cls.repo, "commit", "-q", "-am", "fails")
        cls.bad = git(cls.repo, "rev-parse", "HEAD")
        cls.linked = cls.root / "linked"  # a linked worktree whose HEAD is the passing commit
        git(cls.repo, "worktree", "add", "-q", "--detach", str(cls.linked), cls.good)

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls.root, ignore_errors=True)

    def context(self, name):
        from wb_core.module import Context
        return Context({"job": "direct", "command": "ci.matrix", "module": "ci", "work": str(self.root / name / "work"),
                        "out": str(self.root / name / "out"), "module_config": {}}, lambda *a, **k: None)

    def run_matrix(self, name, checkout, pythons, commit="", requirements=(), script="check.py", folder="", env=None):
        ctx = self.context(name)
        result = self.matrix.matrix(ctx, str(checkout), ["{python}", script], list(pythons), commit,
                                    list(requirements), 120, folder, env)
        return ctx, result

    def test_a_linked_worktree_runs_its_own_head(self):
        ctx, result = self.run_matrix("linked", self.linked, [sys.executable])
        self.assertEqual(ctx.outcome, "ok", ctx.findings)
        self.assertEqual(result["commit"], self.good)
        [one] = result["results"]
        self.assertEqual((one["exit"], one["version"]), (0, sys.version.split()[0]))
        self.assertEqual(one["tail"], [f"on {sys.version.split()[0]} bare"])
        record = json.loads((self.root / "linked" / "out" / "matrix.json").read_text(encoding="utf-8"))
        self.assertEqual(record["schema"], "wb.ci.matrix/1")
        self.assertFalse((self.root / "linked" / "work" / "tree").exists())

    def test_a_failing_version_and_a_missing_one_are_findings(self):
        ctx, result = self.run_matrix("bad", self.repo, [sys.executable, "9.99"])
        self.assertEqual(result["commit"], self.bad)
        self.assertIn(f"Python {sys.version.split()[0]} exited 3: on {sys.version.split()[0]} bare", ctx.findings)
        self.assertIn("no Python 9.99 on this host", ctx.findings)

    def test_requirements_give_each_version_its_own_environment(self):
        ctx, result = self.run_matrix("venv", self.repo, [sys.executable], commit=self.good,
                                      requirements=["requirements.txt"])
        self.assertEqual(ctx.outcome, "ok", ctx.findings)
        self.assertEqual(result["results"][0]["tail"], [f"on {sys.version.split()[0]} venv"])

    def test_a_folder_and_env_reach_the_command(self):
        ctx, result = self.run_matrix("folder", self.repo, [sys.executable], commit=self.good, script="run.py",
                                      folder="pkg", env={"PYTHONPATH": "src", "LANE": "one"})
        self.assertEqual(ctx.outcome, "ok", ctx.findings)
        self.assertEqual(result["results"][0]["tail"], ["from src one"])
        record = json.loads((self.root / "folder" / "out" / "matrix.json").read_text(encoding="utf-8"))
        self.assertEqual((record["folder"], record["env"]), ("pkg", ["LANE", "PYTHONPATH"]))

    def test_usage_errors_are_refused_and_leave_no_clone(self):
        from wb_core.module import ParamError
        ctx = self.context("usage")
        for command, commit, requirements, folder in ((["python", "check.py"], "", [], ""),
                                                      (["{python}", "check.py"], "nope", [], ""),
                                                      (["{python}", "check.py"], "", [], "../elsewhere"),
                                                      (["{python}", "check.py"], "", ["missing.txt"], ""),
                                                      (["{python}", "check.py"], "", [], "nowhere")):
            with self.subTest(command=command, commit=commit, requirements=requirements, folder=folder):
                with self.assertRaises(ParamError):
                    self.matrix.matrix(ctx, str(self.repo), command, [sys.executable], commit, requirements, 60,
                                       folder)
                self.assertFalse((self.root / "usage" / "work" / "tree").exists())


HOST_PROJECT = """schema = 1
gate = ["plain"]

[project]
name = "demo"

[checks.plain]
steps = [["{python}", "-c", "import pathlib; print(pathlib.Path('src/a.txt').read_text())"]]

[checks.native]
on = "host"
steps = [["{python}", "-c", "import os, sys; sys.exit(3 if os.environ.get('WB_TURN_TOKEN') else 0)"]]

[checks.glcheck]
on = "host"
gpu = true
steps = [["{python}", "-B", "turn.py", "{run_dir}/turn.json"]]
"""

# A gpu check's step, standing in for a launcher that joins a held turn (RivenShaders' gpu_guard): the token it was
# given must be the one the lock's holder wrote, under the default name and the machine's alias.
TURN_STEP = """import json, os, sys
from pathlib import Path
token, lock = os.environ.get("WB_TURN_TOKEN"), os.environ.get("WB_LEASE_FILE")
with open(lock, "rb", buffering=0) as stream:
    stream.seek(1)
    owner = json.loads(stream.read(65536).decode("utf-8"))
Path(sys.argv[1]).parent.mkdir(parents=True, exist_ok=True)
Path(sys.argv[1]).write_text(json.dumps({"token": token, "owner": owner.get("turnToken"),
                                         "alias": os.environ.get("OLD_TURN"), "lane": os.environ.get("WB_TURN_LANE")}))
sys.exit(0 if token and token == owner.get("turnToken") == os.environ.get("OLD_TURN") else 4)
"""


class HostChecks(unittest.TestCase):
    """ci.host-run: a trusted project's host checks from a clean clone on the host, the gpu ones under a turn of the
    GPU lease that their steps join, recorded where ci.run records; an untrusted project is refused first."""

    @classmethod
    def setUpClass(cls):
        for folder in ("modules/tools", "modules/build", "modules/lab", "modules/ci", "host"):
            sys.path.insert(0, str(REPO / folder))
        from wb_ci import commands, host
        cls.commands, cls.host = commands, host
        cls.root = Path(tempfile.mkdtemp(prefix="wb-host-checks-"))
        cls.repo = cls.root / "Demo"
        (cls.repo / "src").mkdir(parents=True)
        git(cls.repo, "init", "-q", "-b", "main")
        (cls.repo / "src" / "a.txt").write_text("hello", encoding="utf-8")
        (cls.repo / "project.toml").write_text(HOST_PROJECT, encoding="utf-8")
        (cls.repo / "turn.py").write_text(TURN_STEP, encoding="utf-8")
        git(cls.repo, "add", "-A")
        git(cls.repo, "commit", "-q", "-m", "demo")
        cls.head = git(cls.repo, "rev-parse", "HEAD")
        cls.config = cls.root / "machine-config"  # an empty machine config: paths are taken as given
        cls.config.mkdir()

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls.root, ignore_errors=True)

    def setUp(self):
        from unittest import mock
        self.base = Path(tempfile.mkdtemp(prefix="run-", dir=self.root))
        self.records = self.base / "records"
        self.lab = {"home": str(self.base / "lab"), "trusted": ["demo"], "env": {"OLD_TURN": "token"}}
        patcher = mock.patch.dict(os.environ, {"WB_CONFIG": str(self.config),
                                               "WB_LEASE_FILE": str(self.base / "lease" / "gpu.lock"),
                                               "WB_COORDINATION_DIR": str(self.base / "coordination")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def context(self, job="h1"):
        from wb_core.module import Context
        config = {"projects": {"demo": {"repo": str(self.repo), "jdk": ""}}, "records": str(self.records),
                  "store": str(self.base / "out" / "store")}
        return Context({"job": job, "command": "ci.host-run", "module": "ci", "machine": "host", "lab": self.lab,
                        "work": str(self.base / job / "work"), "out": str(self.base / job / "out"),
                        "module_config": config}, lambda *a, **k: None)

    def host_run(self, checks, job="h1", **params):
        ctx = self.context(job)
        return ctx, self.host.host_run(ctx, "demo", checks, commit=self.head, **params)

    def test_an_untrusted_project_is_refused_before_anything_runs(self):
        from wb_core.module import ParamError
        self.lab["trusted"] = ["other"]
        with self.assertRaisesRegex(ParamError, r"demo is not in \[lab\] trusted"):
            self.host_run(["native"])
        self.assertFalse(self.records.exists())                       # no record folder made
        self.assertFalse((self.base / "lab" / "wb-state").exists())   # no clone, no Gradle home, no environment
        self.assertFalse((self.base / "lease").exists())              # no turn asked for

    def test_gpu_checks_run_under_the_lease_and_the_others_without_it(self):
        from unittest import mock
        turns = []
        fake = self.base / "fake.lock"  # what a holder writes from byte 1: the turn's token
        fake.write_bytes(b" " + json.dumps({"turnToken": "t1"}).encode())

        @__import__("contextlib").contextmanager
        def turn(ctx, project, checks):
            turns.append(list(checks))
            yield {"WB_TURN_TOKEN": "t1", "OLD_TURN": "t1", "WB_LEASE_FILE": str(fake), "WB_TURN_LANE": "check-demo"}
        with mock.patch.object(self.host, "_turn", turn), \
                mock.patch.dict(os.environ, {"WB_TURN_TOKEN": ""}):
            ctx, result = self.host_run(["native"])
            self.assertEqual((ctx.outcome, result["passed"], result["leased"]), ("ok", ["native"], []))
            self.assertEqual(turns, [])                                # no gpu check: no turn
            ctx, result = self.host_run(["native", "glcheck"], job="h2", if_needed=False)
        self.assertEqual(turns, [["glcheck"]])                         # one turn, for the gpu check alone
        self.assertEqual(ctx.outcome, "ok", (ctx.findings, ctx.reasons))
        self.assertEqual(result["runs"], ["h2", "h2-gpu"])
        self.assertEqual(sorted(result["passed"]), ["glcheck", "native"])
        self.assertEqual(result["leased"], ["glcheck"])
        with mock.patch.object(self.host, "_turn", turn):
            ctx, result = self.host_run(["glcheck"], job="h3")
        self.assertEqual(turns, [["glcheck"]])                         # passed for this tree: no turn taken again
        self.assertEqual((ctx.outcome, result["passed"]), ("ok", []))

    def test_a_gpu_checks_steps_join_the_turn_and_the_records_keep_the_pass(self):
        from wb_host import lease as leases
        ctx, result = self.host_run(["native", "glcheck"])
        self.assertEqual(ctx.outcome, "ok", (ctx.findings, ctx.reasons))
        self.assertEqual(sorted(result["passed"]), ["glcheck", "native"])
        self.assertEqual(result["commit"], self.head)
        joined = json.loads((self.records / "demo" / "h1-gpu" / "turn.json").read_text(encoding="utf-8"))
        self.assertEqual(joined["token"], joined["owner"])             # the token of the turn that held the lock
        self.assertEqual(joined["lane"], "check-demo")
        lock = self.base / "lease" / "gpu.lock"
        self.assertFalse(leases.occupied(lock))                        # released after the run
        self.assertIn("releasedUtc", leases.owner(lock))
        for run in ("h1", "h1-gpu"):
            self.assertTrue((self.records / "demo" / run / "summary.json").is_file())
        self.assertFalse((self.base / "lab" / "wb-state" / "ci" / "trees" / "demo-h1").exists())  # the clone is gone
        again = self.host_run(["native", "glcheck"], job="h3")[1]
        self.assertEqual(again["passed"], [])                          # recorded by tree: both already passed

    def test_a_container_check_is_left_to_ci_run(self):
        from wb_core.module import ParamError
        with self.assertRaisesRegex(ParamError, "plain run.* in the container"):
            self.host_run(["plain"])
        with self.assertRaisesRegex(ParamError, "no check nope"):
            self.host_run(["nope"])

    def test_the_container_run_leaves_host_checks_out(self):
        from wb_core.module import ParamError
        from orthrus_tools import actions
        config = actions.load(self.repo)
        ctx = self.context("c1")
        self.assertEqual(self.commands._container_targets(ctx, config, ["all"]), ["plain"])
        self.assertEqual(self.commands._container_targets(ctx, config, ["gate"]), ["gate"])  # as asked
        with self.assertRaisesRegex(ParamError, "run.* on the host"):
            self.commands._container_targets(ctx, config, ["glcheck"])

    def test_records_must_be_a_folder_the_host_reaches(self):
        from wb_core.module import EnvironmentProblem
        ctx = self.context("r1")
        ctx.config.pop("records")
        with self.assertRaisesRegex(EnvironmentProblem, "records is not set"):
            self.commands._records(ctx, "demo")


class CheckSplit(unittest.TestCase):
    """wb check: the container checks it is asked for go to ci.run, the host checks to ci.host-run; no names is the
    gate, as before."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(REPO / "sdk" / "python"))
        from wb import check, workspace
        cls.check, cls.workspace = check, workspace
        cls.root = Path(tempfile.mkdtemp(prefix="wb-check-split-"))
        cls.repo = cls.root / "Demo"
        cls.repo.mkdir()
        git(cls.repo, "init", "-q", "-b", "main")
        (cls.repo / "project.toml").write_text(HOST_PROJECT, encoding="utf-8")
        git(cls.repo, "add", "-A")
        git(cls.repo, "commit", "-q", "-m", "demo")
        cls.head = git(cls.repo, "rev-parse", "HEAD")
        space = workspace.Workspace.__new__(workspace.Workspace)
        space.values, space.ci = {}, {"records": str(cls.root / "records")}
        space._projects = None
        cls.space = space
        cls.project = workspace.Project("demo", {}, cls.repo, workspace.common_dir(cls.repo))

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls.root, ignore_errors=True)

    def split(self, *names):
        return self.check.split(self.space, self.project, self.head, list(names))

    def test_names_split_by_where_their_checks_run(self):
        self.assertEqual(self.split("all"), {"container": ["plain"], "host": ["native", "glcheck"]})
        self.assertEqual(self.split("plain", "glcheck"), {"container": ["plain"], "host": ["glcheck"]})
        self.assertEqual(self.split("native"), {"container": [], "host": ["native"]})
        self.assertEqual(self.split("gate"), {"container": ["gate"], "host": []})  # no host check: as given
        self.assertEqual(self.split(), {"container": [], "host": []})                # the gate, as before
        self.assertEqual(self.split("nope"), {"container": ["nope"], "host": []})    # ci.run says what is wrong

    def test_wb_check_submits_each_part_where_it_runs(self):
        import argparse
        import io
        from contextlib import redirect_stderr, redirect_stdout
        from unittest import mock
        from wb import cli
        from wb.client import Envelope
        submitted = []

        class Job:
            def __init__(self, command, params):
                self.id = f"j{len(submitted)}"
                self.command = command

            def wait(self, timeout=None):
                return Envelope(state="settled", outcome="ok", code=0, command=self.command, job=self.id)

        class Client:
            def submit(self, command, params):
                submitted.append((command, params))
                return Job(command, params)

        space = self.space
        space.project_for = lambda folder: (self.project, self.repo)
        space.would_be = lambda top: {"commit": self.head, "tree": "t" * 40, "dirty": False, "head": self.head}

        def check(*names):
            submitted.clear()
            args = argparse.Namespace(names=list(names), stand_in=None, quiet=True, timeout=None, json=False)
            err = io.StringIO()
            with mock.patch.object(self.workspace, "Workspace", return_value=space), \
                    mock.patch("wb.setwork.resolve", return_value=""), \
                    mock.patch.object(cli, "_client", return_value=Client()), \
                    redirect_stdout(io.StringIO()), redirect_stderr(err):
                code = cli.cmd_check(args)
            return code, list(submitted), err.getvalue()

        code, runs, said = check("plain", "glcheck")
        self.assertEqual(code, 0)
        self.assertEqual([(c, p.get("workflow"), p.get("jobs"), p.get("checks")) for c, p in runs],
                         [("ci.run", "plain", [], None), ("ci.host-run", None, None, ["glcheck"])])
        self.assertIn("in the container: plain (ok); on the host: glcheck (ok)", said)
        code, runs, _ = check()
        self.assertEqual([(c, sorted(p)) for c, p in runs], [("ci.run", ["commit", "project"])])  # the gate, as before
        code, runs, said = check("native")
        self.assertEqual([c for c, _ in runs], ["ci.host-run"])
        self.assertIn("on the host: native", said)


if __name__ == "__main__":
    unittest.main()
