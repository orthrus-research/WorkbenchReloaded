"""Releases: a release is the commit's files and its RELEASE.json, made once; current is a link that moves to a new
release without touching the old one; a release names its commit without git; old releases go, the one in service
stays."""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness  # noqa: E402,F401

from wb import releases  # noqa: E402
from wb_core.identity import source_commit  # noqa: E402


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args], cwd=cwd,
                          check=True, capture_output=True, text=True).stdout.strip()


class Config:
    def __init__(self, values, folder):
        self.values, self.folder = values, folder


class Releases(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        base = Path(self.folder.name)
        self.repo = base / "wb"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "bin").mkdir()
        (self.repo / "bin" / "wb").write_text("one\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "one")
        self.first = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "bin" / "wb").write_text("two\n", encoding="utf-8")
        git(self.repo, "commit", "-q", "-am", "two")
        self.second = git(self.repo, "rev-parse", "HEAD")
        self.config = Config({}, base / "config")
        self.saved = os.environ.get("WB_RELEASES")
        os.environ["WB_RELEASES"] = str(base / "releases")

    def tearDown(self):
        link = releases.current(self.config)
        if link.exists() or link.is_symlink():
            os.rmdir(link) if os.name == "nt" else link.unlink()
        if self.saved is None:
            os.environ.pop("WB_RELEASES", None)
        else:
            os.environ["WB_RELEASES"] = self.saved
        self.folder.cleanup()

    def test_releases_never_live_in_the_config_folder(self):
        good = os.environ["WB_RELEASES"]
        os.environ["WB_RELEASES"] = str(self.config.folder / "releases")
        try:
            with self.assertRaises(RuntimeError) as raised:
                releases.root(self.config)
            self.assertIn("read as config", str(raised.exception))
        finally:
            os.environ["WB_RELEASES"] = good

    def test_a_release_is_the_commits_files_and_names_its_commit(self):
        made = releases.make(self.config, self.repo, self.first, {"tree": "t"})
        self.assertEqual((made / "bin" / "wb").read_text(encoding="utf-8"), "one\n")
        self.assertEqual(source_commit(made), self.first)  # no git in it
        self.assertEqual(releases.make(self.config, self.repo, self.first, {}), made)  # made once

    def test_current_moves_and_the_old_release_is_untouched(self):
        one = releases.make(self.config, self.repo, self.first, {})
        two = releases.make(self.config, self.repo, self.second, {})
        self.assertIsNone(releases.point(self.config, one))
        self.assertEqual((releases.current(self.config) / "bin" / "wb").read_text(encoding="utf-8"), "one\n")
        self.assertEqual(Path(os.path.realpath(releases.point(self.config, two))).name, one.name)
        self.assertEqual((releases.current(self.config) / "bin" / "wb").read_text(encoding="utf-8"), "two\n")
        self.assertEqual((one / "bin" / "wb").read_text(encoding="utf-8"), "one\n")

    def test_gc_lists_every_release_but_the_one_in_service_and_the_one_before_it(self):
        from wb import gc
        one = releases.make(self.config, self.repo, self.first, {})
        time.sleep(0.05)
        two = releases.make(self.config, self.repo, self.second, {})
        releases.point(self.config, one)
        places = gc.Places(releases=releases.root(self.config))
        self.assertEqual(gc.release_folders(places, gc.roots(places)), [])  # the one in service, and the newest
        releases.point(self.config, two)
        listed = gc.release_folders(gc.Places(releases=releases.root(self.config), previous_release=one.name),
                                    gc.roots(places))
        self.assertEqual(listed, [])  # two in service, one before it
        three = releases.root(self.config) / ("3" * 12)  # a third commit's release
        three.mkdir()
        (three / "RELEASE.json").write_text(json.dumps({"made_at": time.time()}), encoding="utf-8")
        releases.point(self.config, three)
        places = gc.Places(releases=releases.root(self.config), previous_release=two.name)
        self.assertEqual([i.path for i in gc.release_folders(places, gc.roots(places))], [str(one)])
        self.assertTrue(two.is_dir())

    def test_serving_names_the_release_current_points_at(self):
        self.assertIsNone(releases.serving(self.config))
        one = releases.make(self.config, self.repo, self.first, {"tree": "t"})
        releases.point(self.config, one)
        self.assertEqual(releases.serving(self.config), one)
        self.assertEqual(releases.info(one)["commit"], self.first)


# -- the image holds no code: what decides a build ------------------------------------------------------------------
DOCKERFILE = ("FROM scratch\nCOPY image/entrypoint.sh image/code.sh /opt/wb/\nCOPY requirements.txt /tmp/\n"
              "COPY --from=other /opt/x /opt/x\n")
ACTIONS = '[workflows]\nci = ["check"]\n\n[jobs.check]\nsteps = [{ name = "ok", run = ["python", "-c", "0"] }]\n'


def workbench_repo(base: Path) -> Path:
    """A small Workbench-shaped checkout: an image folder, code, a jobs file and a suite that passes."""
    repo = base / "wb"
    files = {"image/Dockerfile": DOCKERFILE, "image/entrypoint.sh": "#!/bin/sh\n", "image/code.sh": "# code\n",
             "requirements.txt": "nothing\n", ".dockerignore": ".git\n", "core/wb_core/__init__.py": "",
             "sdk/python/wb/__init__.py": "", "sdk/python/wb/cli.py": "# one\n", "actions.toml": ACTIONS,
             "tests/run_all.py": "import sys\nsys.exit(0)\n"}
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text, encoding="utf-8", newline="\n")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "one")
    return repo


def change(repo: Path, name: str, text: str) -> str:
    (repo / name).write_text(text, encoding="utf-8", newline="\n")
    git(repo, "commit", "-q", "-am", f"change {name}")
    return git(repo, "rev-parse", "HEAD")


class ImageInputs(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.repo = workbench_repo(Path(self.folder.name))

    def tearDown(self):
        self.folder.cleanup()

    def test_the_inputs_are_the_image_folder_its_copied_sources_and_dockerignore(self):
        from wb import compose
        names = [p.as_posix() for p in compose.image_inputs(self.repo)]
        self.assertEqual(names, [".dockerignore", "image/Dockerfile", "image/code.sh", "image/entrypoint.sh",
                                 "requirements.txt"])  # no code, and nothing from another stage

    def test_a_python_only_change_leaves_the_digest_and_an_image_input_changes_it(self):
        from wb import compose
        digest = compose.inputs_digest(self.repo)
        (self.repo / "sdk" / "python" / "wb" / "cli.py").write_text("# two\n", encoding="utf-8")
        (self.repo / "core" / "wb_core" / "new.py").write_text("x = 1\n", encoding="utf-8")
        self.assertEqual(compose.inputs_digest(self.repo), digest)
        for name in ("image/Dockerfile", "requirements.txt", "image/code.sh"):
            with self.subTest(name=name):
                before = (self.repo / name).read_bytes()
                (self.repo / name).write_bytes(before + b"# changed\n")
                self.assertNotEqual(compose.inputs_digest(self.repo), digest)
                (self.repo / name).write_bytes(before)
        self.assertEqual(compose.inputs_digest(self.repo), digest)
        self.assertNotEqual(compose.inputs_digest(self.repo, ["node"]), digest)  # the tools built in count

    def test_an_image_is_built_only_when_its_recorded_digest_differs(self):
        from wb import compose
        self.assertIn("no image", compose.build_reason("wb-core:dev", "abc", None))
        self.assertIn("records no inputs digest", compose.build_reason("wb-core:dev", "abc", ""))
        self.assertIn("inputs changed", compose.build_reason("wb-core:dev", "abc", "def"))
        self.assertIsNone(compose.build_reason("wb-core:dev", "abc", "abc"))

    def test_the_label_is_read_from_the_image(self):
        from unittest import mock
        from wb import compose
        with mock.patch.object(compose.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, json.dumps({compose.INPUTS_LABEL: "d1", "x": "y"}), "")
            self.assertEqual(compose.image_label("wb-core:dev"), "d1")
            run.return_value = subprocess.CompletedProcess([], 0, "null\n", "")
            self.assertEqual(compose.image_label("wb-core:dev"), "")  # an image from before the label
            run.return_value = subprocess.CompletedProcess([], 1, "", "no such image")
            self.assertIsNone(compose.image_label("wb-core:dev"))


# -- image/code.sh: which code the container runs -------------------------------------------------------------------
def _sh() -> str | None:
    import shutil
    found = shutil.which("sh")
    if found:
        return found
    exec_path = subprocess.run(["git", "--exec-path"], capture_output=True, text=True).stdout.strip()
    for parent in Path(exec_path).parents:  # Git for Windows: <git>/mingw64/libexec/git-core
        for candidate in (parent / "usr" / "bin" / "sh.exe", parent / "bin" / "sh.exe"):
            if candidate.is_file():
                return str(candidate)
    return None


SH = _sh()
CODE_SCRIPT = (Path(harness.REPO) / "image" / "code.sh").as_posix()
ENTRYPOINT = (Path(harness.REPO) / "image" / "entrypoint.sh").as_posix()


def code_tree(folder: Path, commit: str | None = None) -> Path:
    for name in ("core/wb_core/__init__.py", "sdk/python/wb/__init__.py", "host/wb_host/__init__.py"):
        (folder / name).parent.mkdir(parents=True, exist_ok=True)
        (folder / name).write_text("", encoding="utf-8")
    if commit:
        (folder / "RELEASE.json").write_text(json.dumps({"tests": {"commit": "not this"}, "commit": commit}),
                                             encoding="utf-8")
    return folder


@unittest.skipIf(SH is None, "no POSIX sh on this machine")
class CodeScript(unittest.TestCase):
    """The entrypoint's choice of code, run in a POSIX sh with the container's folders moved into a temp folder."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        base = Path(self.folder.name)
        self.state, self.mount, self.baked = base / "state", base / "mount", base / "baked"
        self.state.mkdir()
        self.mount.mkdir()
        self.name = "abcdef012345"
        self.commit = self.name + "6789" * 7

    def tearDown(self):
        self.folder.cleanup()

    def run_script(self, script: str, **env) -> subprocess.CompletedProcess:
        clean = {k: v for k, v in os.environ.items() if not k.startswith("WB_")}
        clean.update({"WB_STATE_DIR": self.state.as_posix(), "WB_RELEASE_MOUNT": self.mount.as_posix(),
                      "WB_BAKED_CODE": self.baked.as_posix(), "WB_PYTHON": Path(sys.executable).as_posix(),
                      "WB_CODE_SCRIPT": CODE_SCRIPT, "MSYS_NO_PATHCONV": "1"})
        clean.update(env)
        return subprocess.run([SH, "-c", script], env=clean, capture_output=True, text=True)

    def pick(self, mode: str = "copy", **env) -> tuple[str, str, str]:
        done = self.run_script(f'. "{CODE_SCRIPT}"; wb_pick_code {mode} || exit 1; '
                               'printf "%s\\n%s\\n%s\\n" "$WB_CODE" "$PYTHONPATH" "$WB_COMMIT"', **env)
        self.assertEqual(done.returncode, 0, done.stderr)
        code, path, commit = (done.stdout.splitlines() + ["", "", ""])[:3]
        return code, path, commit

    def test_the_first_start_copies_the_release_into_the_volume_and_runs_the_copy(self):
        code_tree(self.mount, self.commit)
        code, path, commit = self.pick(WB_RELEASE=self.name)
        copy = self.state / "releases" / self.name
        self.assertEqual(code, copy.as_posix())
        self.assertTrue(path.startswith(f"{copy.as_posix()}/sdk/python:"), path)
        self.assertTrue(path.endswith(f"{self.name}/host"), path)
        self.assertEqual(commit, self.commit)  # the release's own commit, not one nested in its record
        self.assertTrue((copy / "core" / "wb_core" / "__init__.py").is_file())
        self.assertEqual((self.state / "releases" / "last").read_text(encoding="utf-8").strip(), self.name)
        # Later starts run the copy even with nothing mounted: nothing on the host reaches it.
        import shutil
        shutil.rmtree(self.mount)
        self.mount.mkdir()
        self.assertEqual(self.pick(WB_RELEASE=self.name)[0], copy.as_posix())

    def test_a_half_made_copy_is_never_run(self):
        code_tree(self.mount, self.commit)
        broken = self.state / "releases" / self.name
        (broken / "core").mkdir(parents=True)  # a copy cut short: no RELEASE.json
        (self.state / "releases" / f"{self.name}.part").mkdir()
        self.assertEqual(self.pick("read", WB_RELEASE=self.name)[0], self.mount.as_posix())  # read never copies
        self.assertEqual(self.pick(WB_RELEASE=self.name)[0], broken.as_posix())
        self.assertTrue((broken / "RELEASE.json").is_file())
        self.assertFalse((self.state / "releases" / f"{self.name}.part").exists())

    def test_a_checkout_is_read_in_place_with_the_commit_wb_up_gave(self):
        code_tree(self.mount)
        code, _, commit = self.pick(WB_RELEASE="", WB_SOURCE_COMMIT="feedface-dirty")
        self.assertEqual((code, commit), (self.mount.as_posix(), "feedface-dirty"))
        self.assertFalse((self.state / "releases").exists())  # a checkout is never copied as a release

    def test_without_a_mount_the_last_release_then_an_image_with_code_then_nothing(self):
        last = code_tree(self.state / "releases" / self.name, self.commit)
        (self.state / "releases" / "last").write_text(self.name + "\n", encoding="utf-8")
        self.assertEqual(self.pick()[0], last.as_posix())
        (self.state / "releases" / "last").unlink()
        code_tree(self.baked)
        code, _, commit = self.pick(WB_COMMIT="baked-commit")
        self.assertEqual((code, commit), (self.baked.as_posix(), "baked-commit"))  # an older image keeps its own
        import shutil
        shutil.rmtree(self.baked)
        done = self.run_script(f'. "{CODE_SCRIPT}"; wb_pick_code copy')
        self.assertEqual(done.returncode, 1)
        self.assertIn("no Workbench code to run", done.stderr)

    def test_a_release_name_is_hex_only(self):
        code_tree(self.mount, self.commit)
        done = self.run_script(f'. "{CODE_SCRIPT}"; wb_pick_code copy && printf "%s" "$WB_CODE"', WB_RELEASE="../x")
        self.assertIn("is not a release name", done.stderr)
        self.assertEqual(done.stdout, self.mount.as_posix())  # read in place, nothing written outside releases/
        self.assertFalse((self.state / "x").exists())

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "as root the entrypoint drops privileges")
    def test_the_entrypoint_starts_its_command_on_the_chosen_code(self):
        code_tree(self.mount, self.commit)
        done = self.run_script(f'exec sh "{ENTRYPOINT}" sh -c \'printf "%s %s" "$WB_CODE" "$WB_COMMIT"\'',
                               WB_RELEASE=self.name)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout, f"{(self.state / 'releases' / self.name).as_posix()} {self.commit}")
        again = self.run_script(f'exec sh "{ENTRYPOINT}" sh -c \'printf "%s" "$WB_CODE"\'',
                                WB_RELEASE_MOUNT=(self.state / "none").as_posix())
        self.assertEqual(again.stdout, (self.state / "releases" / self.name).as_posix())  # the release it ran last
        empty = self.run_script(f'exec sh "{ENTRYPOINT}" true', WB_RELEASE_MOUNT=(self.state / "none").as_posix(),
                                WB_STATE_DIR=(self.state / "fresh").as_posix())
        self.assertEqual(empty.returncode, 1)  # no code anywhere: it says so instead of starting a broken daemon
        self.assertIn("no Workbench code to run", empty.stderr)


class ComposeCode(unittest.TestCase):
    """wb up mounts the code it starts and names the release; with no release, the checkout is read in place."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        base = Path(self.folder.name)
        (base / "config").mkdir()
        (base / "config" / "workbench.toml").write_text("[core.api]\nport = 7377\n", encoding="utf-8")
        self.run_dir = base / "config" / "run"
        self.run_dir.mkdir()
        (self.run_dir / "token").write_text("t", encoding="ascii")
        self.repo = workbench_repo(base)
        self.saved = os.environ.get("WB_RELEASES")
        os.environ["WB_RELEASES"] = str(base / "releases")
        from wb import config as wbconfig
        self.config = wbconfig.load(base / "config")

    def tearDown(self):
        link = releases.current(self.config)
        if link.exists() or link.is_symlink():
            os.rmdir(link) if os.name == "nt" else link.unlink()
        if self.saved is None:
            os.environ.pop("WB_RELEASES", None)
        else:
            os.environ["WB_RELEASES"] = self.saved
        self.folder.cleanup()

    def service(self, code):
        from wb import compose, platform
        return compose.generate(self.config, self.run_dir, platform.Profile("windows", "desktop"), code=code)[
            "services"]["wb"]

    def test_the_release_in_service_is_mounted_read_only_and_named(self):
        from unittest import mock
        from wb import compose
        sha = git(self.repo, "rev-parse", "HEAD")
        release = releases.make(self.config, self.repo, sha, {"tree": "t"})
        releases.point(self.config, release)
        with mock.patch.object(compose, "repo_root", return_value=self.repo):
            code = compose.code_source(self.config)
        self.assertEqual((code.host, code.release), (release, sha[:12]))
        service = self.service(code)
        self.assertIn(f"{release}:{compose.RELEASE_MOUNT}:ro", service["volumes"])
        self.assertEqual(service["environment"]["WB_RELEASE"], sha[:12])
        self.assertNotIn("WB_SOURCE_COMMIT", service["environment"])
        other = Path(self.folder.name) / "other"
        self.assertEqual(compose.code_source(self.config, other).release, "other")  # what wb deploy starts

    def test_with_no_release_the_checkout_is_read_in_place(self):
        from unittest import mock
        from wb import compose
        with mock.patch.object(compose, "repo_root", return_value=self.repo):
            code = compose.code_source(self.config)
        self.assertEqual((code.host, code.release, code.commit), (self.repo, "", git(self.repo, "rev-parse", "HEAD")))
        service = self.service(code)
        self.assertEqual(service["environment"]["WB_RELEASE"], "")
        self.assertEqual(service["environment"]["WB_SOURCE_COMMIT"], code.commit)
        with mock.patch.object(compose, "repo_root", return_value=None):
            self.assertIsNone(compose.code_source(self.config))
        self.assertFalse(any(compose.RELEASE_MOUNT in v for v in self.service(None)["volumes"]))


# -- wb deploy ------------------------------------------------------------------------------------------------------
class ContainerPass(unittest.TestCase):
    """The container half of the gate, read from CI's records on the host."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.base = Path(self.folder.name)
        self.repo = workbench_repo(self.base)
        self.sha = git(self.repo, "rev-parse", "HEAD")
        self.records = self.base / "records"
        values = {"modules": {"ci": {"projects_root": str(self.base), "records": str(self.records),
                                     "projects": {"workbench": {"repo": "wb"}}}}}
        self.config = Config(values, self.base / "config")

    def tearDown(self):
        self.folder.cleanup()

    def test_a_tree_ci_has_not_passed_is_named_and_a_passed_one_names_its_run(self):
        from wb import deploy
        answer = deploy.container_pass(self.config, self.repo, self.sha)
        self.assertFalse(answer["passed"])
        self.assertIn("has not passed check", answer["reason"])
        run = self.records / "workbench" / "run-1"
        run.mkdir(parents=True)
        (run / "summary.json").write_text(json.dumps({
            "trees": {"project": git(self.repo, "rev-parse", "HEAD^{tree}")}, "treesChanged": False,
            "jobs": {"check": {"status": "passed"}}}), encoding="utf-8")
        answer = deploy.container_pass(self.config, self.repo, self.sha)
        self.assertEqual((answer["passed"], answer["project"], answer["required"], answer["runs"]),
                         (True, "workbench", ["check"], ["run-1"]))

    def test_an_unregistered_checkout_has_no_records(self):
        from wb import deploy
        answer = deploy.container_pass(Config({}, self.base / "config"), self.repo, self.sha)
        self.assertFalse(answer["passed"])
        self.assertIn("not a registered project", answer["reason"])


class Deploy(unittest.TestCase):
    """wb deploy's decisions, with docker, the daemon and the host runner replaced: what is gated, what is built,
    what is started, and that the checkout never moves."""

    def setUp(self):
        from unittest import mock
        from wb import compose, deploy
        self.folder = tempfile.TemporaryDirectory()
        base = Path(self.folder.name)
        self.repo = workbench_repo(base)
        (base / "config").mkdir()
        (base / "config" / "workbench.toml").write_text("", encoding="utf-8")
        self.env = mock.patch.dict(os.environ, {"WB_CONFIG": str(base / "config"),
                                                "WB_RELEASES": str(base / "releases")})
        self.env.start()
        self.label = None
        self.built, self.started, self.suites = [], [], []
        self.passed = True

        def build(tag, tools=None, root=None):
            self.label = compose.inputs_digest(root, tools)
            self.built.append(root)
            return 0

        def up(config, wait=90, release=None):
            self.started.append(release)
            self.serving = releases.info(release)["commit"]
            return {"commit": self.serving}

        def suite(root, sha, head):
            self.suites.append((sha, head))
            return {"passed": True, "at": "now", "seconds": 0}

        client = mock.MagicMock()
        client.jobs.return_value = []
        client.health.side_effect = lambda: {"commit": self.serving, "host": {"commit": self.serving}}
        self.patches = [
            mock.patch.object(compose, "repo_root", return_value=self.repo),
            mock.patch.object(compose, "image_label", side_effect=lambda tag: self.label),
            mock.patch.object(compose, "build", side_effect=build),
            mock.patch.object(compose, "up", side_effect=up),
            mock.patch.object(compose, "down"),
            mock.patch.object(deploy, "container_pass", side_effect=lambda config, root, sha: {
                "passed": self.passed, "project": "workbench", "required": ["check"],
                "runs": ["run-1"] if self.passed else [], "reason": None if self.passed else "has not passed check"}),
            mock.patch.object(deploy, "host_suite", side_effect=suite),
            mock.patch.object(deploy, "Client", return_value=client),
            mock.patch.object(deploy, "_restart_host_runner", return_value="restarted"),
            mock.patch.object(deploy, "_task_script", return_value=None),
            mock.patch.object(deploy, "_point_hooks"),
            mock.patch.object(deploy.time, "sleep"),
        ]
        for patch in self.patches:
            patch.start()
        from wb import config as wbconfig
        self.config = wbconfig.load(base / "config")

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        link = releases.current(self.config)
        if link.exists() or link.is_symlink():
            os.rmdir(link) if os.name == "nt" else link.unlink()
        self.env.stop()
        self.folder.cleanup()

    def deploy(self, commit=None, override=None) -> int:
        import argparse
        import contextlib
        import io
        from wb import deploy
        with contextlib.redirect_stderr(io.StringIO()) as said:
            code = deploy.main(argparse.Namespace(commit=commit, override=override, wait="1s", now=False))
        self.said = said.getvalue()
        return code

    def test_a_python_only_deploy_builds_no_image_and_an_image_change_does(self):
        first = git(self.repo, "rev-parse", "HEAD")
        self.assertEqual(self.deploy(), 0, self.said)
        self.assertEqual(self.built, [releases.root(self.config) / first[:12]])  # no image yet: built, from the release
        python_only = change(self.repo, "sdk/python/wb/cli.py", "# two\n")
        self.assertEqual(self.deploy(), 0, self.said)
        self.assertEqual(len(self.built), 1, self.said)  # a restart on the new release, no build
        self.assertIn("no image build", self.said)
        self.assertEqual(self.started[-1], releases.root(self.config) / python_only[:12])
        self.assertEqual(releases.serving(self.config).name, python_only[:12])
        record = releases.info(releases.serving(self.config))
        self.assertEqual((record["ci"]["passed"], record["ci"]["runs"], record["tests"]["passed"]),
                         (True, ["run-1"], True))  # both halves of the gate, in RELEASE.json
        image_change = change(self.repo, "image/Dockerfile", DOCKERFILE + "# a new layer\n")
        self.assertEqual(self.deploy(), 0, self.said)
        self.assertEqual(self.built[-1], releases.root(self.config) / image_change[:12])
        records = json.loads((Path(self.config.folder) / "run" / "deploys.json").read_text(encoding="utf-8"))
        self.assertEqual([r["image"]["built"] for r in records["releases"]], [True, False, True])

    def test_a_tree_without_a_container_pass_is_not_deployed(self):
        self.passed = False
        self.assertEqual(self.deploy(), 1)
        self.assertIn("container pass is missing", self.said)
        self.assertEqual((self.built, self.started, self.suites), ([], [], []))
        self.assertIsNone(releases.serving(self.config))

    def test_an_override_skips_the_gate_and_is_recorded(self):
        self.passed = False
        self.assertEqual(self.deploy(override="fixing the gate itself"), 0, self.said)
        self.assertEqual(self.suites, [])
        record = releases.info(releases.serving(self.config))
        self.assertEqual(record["override"], "fixing the gate itself")
        self.assertFalse(record["ci"]["passed"])

    def test_commit_deploys_an_export_and_never_switches_the_checkout(self):
        old = git(self.repo, "rev-parse", "HEAD")
        head = change(self.repo, "sdk/python/wb/cli.py", "# two\n")
        (self.repo / "sdk" / "python" / "wb" / "cli.py").write_text("# uncommitted\n", encoding="utf-8")
        status = git(self.repo, "status", "--porcelain")
        self.assertEqual(self.deploy(commit=old[:8]), 0, self.said)  # a dirty checkout does not stop --commit
        self.assertEqual((git(self.repo, "rev-parse", "HEAD"), git(self.repo, "branch", "--show-current")),
                         (head, "main"))
        self.assertEqual(git(self.repo, "status", "--porcelain"), status)
        release = releases.serving(self.config)
        self.assertEqual(release.name, old[:12])
        self.assertEqual((release / "sdk" / "python" / "wb" / "cli.py").read_text(encoding="utf-8"), "# one\n")
        self.assertEqual(self.suites, [(old, False)])  # the host suite ran on the export, not the checkout
        self.assertEqual(self.deploy(commit="no-such-commit"), 2)
        self.assertIn("is not a commit", self.said)

    def test_the_host_suite_of_a_commit_runs_in_a_clone_of_it(self):
        from wb import deploy
        self.patches[6].stop()  # the real host suite
        try:
            passing = git(self.repo, "rev-parse", "HEAD")
            failing = change(self.repo, "tests/run_all.py", "import sys\nsys.exit(1)\n")
            self.assertTrue(deploy.host_suite(self.repo, passing, head=False)["passed"])
            self.assertFalse(deploy.host_suite(self.repo, failing, head=False)["passed"])
            self.assertEqual(git(self.repo, "rev-parse", "HEAD"), failing)
            self.assertEqual(git(self.repo, "status", "--porcelain"), "")
        finally:
            self.patches[6].start()


# -- wb setup: the shim on PATH and the first release ---------------------------------------------------------------
class Setup(unittest.TestCase):
    def setUp(self):
        from unittest import mock
        self.folder = tempfile.TemporaryDirectory()
        self.base = Path(self.folder.name)
        self.repo = workbench_repo(self.base)
        self.bin = self.base / "bin"
        self.env = mock.patch.dict(os.environ, {"WB_RELEASES": str(self.base / "releases"), "WB_BIN": str(self.bin)})
        self.env.start()
        self.config = Config({}, self.base / "config")

    def tearDown(self):
        link = releases.current(self.config)
        if link.exists() or link.is_symlink():
            os.rmdir(link) if os.name == "nt" else link.unlink()
        self.env.stop()
        self.folder.cleanup()

    def install(self, path_value: str = "") -> tuple[int, str]:
        import contextlib
        import io
        from unittest import mock
        from wb import setup
        with mock.patch.dict(os.environ, {"PATH": path_value}), contextlib.redirect_stdout(io.StringIO()) as out:
            code = setup.install_shims(self.config)
        return code, out.getvalue()

    def test_the_shim_runs_the_release_in_service_and_path_is_only_ever_printed(self):
        from wb import setup
        code, said = self.install()
        self.assertEqual(code, 0)
        target = releases.current(self.config) / "bin"
        names = sorted(p.name for p in self.bin.iterdir())
        self.assertEqual(names, ["wb", "wb.cmd"] if os.name == "nt" else ["wb"])
        for path in self.bin.iterdir():
            text = path.read_text(encoding="utf-8")
            self.assertIn(setup.SHIM_MARKER, text)
            self.assertTrue(str(target) in text or target.as_posix() in text, text)
        self.assertIn("does not change PATH", said)
        self.assertIn(setup.path_command(self.bin), said)  # the exact command, for the person to run
        self.assertIn(str(self.bin), setup.path_command(self.bin))
        code, said = self.install(str(self.bin))
        self.assertEqual((code, said.count("not on PATH")), (0, 0))  # on PATH: nothing to print
        self.assertIn("keep (already the shim)", [s["action"] for s in setup.shim_steps(self.config)])

    def test_another_file_named_wb_is_never_overwritten(self):
        self.bin.mkdir()
        (self.bin / "wb").write_text("someone else's\n", encoding="utf-8")
        code, said = self.install()
        self.assertEqual(code, 1)
        self.assertIn("another file", said)
        self.assertEqual((self.bin / "wb").read_text(encoding="utf-8"), "someone else's\n")

    def test_a_machine_with_no_release_gets_a_bootstrap_release_from_the_clone(self):
        from unittest import mock
        from wb import deploy, setup
        sha = git(self.repo, "rev-parse", "HEAD")
        answer = {"passed": False, "project": "workbench", "required": ["check"], "runs": [], "reason": "untested"}
        with mock.patch.object(setup, "REPO", self.repo), \
                mock.patch.object(deploy, "container_pass", return_value=answer):
            steps = setup.release_step(self.config)
            self.assertIn(f"make the first from {sha[:12]} (marked bootstrap)", steps["action"])
            made = setup.bootstrap(self.config)
            self.assertEqual(made["release"].name, sha[:12])
            self.assertEqual(releases.serving(self.config), made["release"])
            record = releases.info(made["release"])
            self.assertEqual((record["bootstrap"], record["commit"], record["ci"]["reason"]), (True, sha, "untested"))
            self.assertIsNone(setup.bootstrap(self.config))  # one release is enough; wb deploy makes the rest
            self.assertIn("in service", setup.release_step(self.config)["action"])
        self.assertTrue(setup.cli_command(self.config).endswith(("wb.cmd", "wb")))


if __name__ == "__main__":
    unittest.main()
