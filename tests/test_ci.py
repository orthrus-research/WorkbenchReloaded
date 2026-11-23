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
            self.assertIn("no actions.toml", gated.data.refused[0]["reason"])
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


if __name__ == "__main__":
    unittest.main()
