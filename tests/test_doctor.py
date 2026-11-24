"""Two proofs of the old routes' retirement, answered on the host from git and config.

wb check on a commit from before its project's conversion answers that the commit predates the set and names the
setup command its own tree carries, and still runs it from its actions.toml; only a commit with no checks file is
not run. wb status --doctor names every WB_PIN_CACHE read, sourceCommit, pin file, refresh task and sibling commit
on a converted project's set-named branch, and each refresh schedule, with project, branch, file and line, skips
the paths set.json exempts, and confirms that the old lock path and each checkout's old route to it are the lease."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness  # noqa: E402,F401  (puts the SDK, core and host on the path)

from wb import check, doctor  # noqa: E402
from wb.workspace import Workspace  # noqa: E402

ACTIONS = '[workflows]\nci = ["check"]\n\n[jobs.check]\nsteps = [{ name = "ok", run = ["python", "-c", "0"] }]\n'
PROJECT = '[project]\nname = "demo"\n\n[checks.check]\nsteps = [{ name = "ok", run = ["python", "-c", "0"] }]\n'
README = """# Demo

## Building from source

Needs a JDK.

```powershell
git clone https://example.invalid/Demo.git
cd Demo
python tools/setup.py
.\\demo.ps1 build
```

## Documentation
"""


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args], cwd=cwd,
                          check=True, capture_output=True, text=True).stdout.strip()


def commit(repo: Path, files: dict, message: str, remove=()) -> str:
    for name in remove:
        git(repo, "rm", "-q", name)
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


class Config:
    def __init__(self, values, folder, origins=None):
        self.values, self.folder, self.origins = values, folder, origins or {}

    def origin(self, dotted):
        return self.origins.get(dotted)


def temporary(test) -> Path:
    root = Path(tempfile.mkdtemp(prefix="wb-doctor-"))
    test.addCleanup(shutil.rmtree, root, True)
    return root


def link(target: Path, at: Path):
    """A folder link as the old route made them: a junction on Windows, a symlink elsewhere."""
    at.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(at), str(target)], check=True, capture_output=True)
    else:
        os.symlink(target, at, target_is_directory=True)


class PreConversion(unittest.TestCase):
    def setUp(self):
        self.root = temporary(self)
        self.repo = self.root / "Demo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        self.old = commit(self.repo, {"actions.toml": ACTIONS, "README.md": README, "tools/setup.py": "pass\n"},
                          "before the set")
        self.new = commit(self.repo, {"project.toml": PROJECT}, "convert", remove=["actions.toml"])
        values = {"modules": {"ci": {"projects_root": str(self.root), "records": str(self.root / "records"),
                                     "projects": {"demo": {"repo": "Demo"}}}}}
        self.workspace = Workspace(Config(values, self.root / "config"))
        self.project = self.workspace.projects()["demo"]

    def test_a_commit_before_the_conversion_predates_the_set_and_names_its_own_setup(self):
        answer = check.predates(self.workspace, self.project, self.old)
        self.assertTrue(answer["runs"])
        self.assertIn(f"demo at {self.old[:12]} predates the set: it carries actions.toml and no project.toml",
                      answer["message"])
        self.assertIn("from its README.md's section 'Building from source': python tools/setup.py "
                      "(then .\\demo.ps1 build)", answer["message"])
        self.assertIn("still run here from its actions.toml", answer["message"])
        self.assertNotIn("git clone", answer["message"])

    def test_a_converted_commit_has_nothing_to_answer(self):
        self.assertIsNone(check.predates(self.workspace, self.project, self.new))

    def test_a_project_not_converted_yet_has_no_conversion_to_predate(self):
        git(self.repo, "reset", "-q", "--hard", self.old)
        self.assertIsNone(check.predates(self.workspace, self.project, self.old))

    def test_the_published_branch_says_whether_the_project_is_converted(self):
        git(self.repo, "update-ref", "refs/remotes/origin/main", self.new)
        git(self.repo, "checkout", "-q", "--detach", self.old)
        git(self.repo, "branch", "-f", "main", self.old)
        answer = check.predates(self.workspace, self.project, self.old)
        self.assertIn(f"(origin/main at {self.new[:12]}) carries project.toml", answer["message"])

    def test_without_a_readme_section_its_setup_script_is_named(self):
        git(self.repo, "checkout", "-q", "-b", "old", self.old)
        bare = commit(self.repo, {"README.md": "# Demo\n"}, "no build section")
        self.assertIn("from its tools/setup.py: python tools/setup.py",
                      check.predates(self.workspace, self.project, bare)["message"])
        script = commit(self.repo, {"demo.ps1": "param([ValidateSet('setup','build')]$Action)\n"}, "script",
                        remove=["tools/setup.py"])
        self.assertIn("from its demo.ps1: .\\demo.ps1 setup",
                      check.predates(self.workspace, self.project, script)["message"])
        none = commit(self.repo, {}, "none", remove=["demo.ps1"])
        answer = check.predates(self.workspace, self.project, none)
        self.assertIn("names no setup command of its own", answer["message"])
        self.assertIn("rebased onto main", answer["message"])
        self.assertTrue(answer["runs"])

    def test_a_commit_with_no_checks_file_is_answered_and_not_run(self):
        git(self.repo, "checkout", "-q", "-b", "old", self.old)
        empty = commit(self.repo, {}, "no checks", remove=["actions.toml"])
        answer = check.predates(self.workspace, self.project, empty)
        self.assertFalse(answer["runs"])
        self.assertIn("carries neither project.toml nor actions.toml", answer["message"])
        self.assertIn("no checks to run", answer["message"])


class Doctor(unittest.TestCase):
    def setUp(self):
        self.root = root = temporary(self)
        for name in ("Alpha", "Beta", "Gamma", "Set"):
            (root / name).mkdir()
            git(root / name, "init", "-q", "-b", "main")
            (root / name / ".git" / "info").mkdir(exist_ok=True)
            (root / name / ".git" / "info" / "exclude").write_text(".local/\n", encoding="utf-8")
        self.beta_old = commit(root / "Beta", {"project.toml": PROJECT}, "beta one")
        self.beta = commit(root / "Beta", {"src/b.txt": "b\n"}, "beta two")
        commit(root / "Gamma", {"actions.toml": ACTIONS, "build.gradle": "def x = System.getenv('WB_PIN_CACHE')\n"},
               "gamma, not converted")
        alpha_own = commit(root / "Alpha", {"project.toml": PROJECT}, "alpha one")
        sha256 = "ab" * 32
        self.alpha = commit(root / "Alpha", {
            "build.gradle": "def cache = System.getenv('WB_PIN_CACHE')\ntasks.register('refreshBeta') {}\n"
                            "tasks.register('heiAsyncRepairJar') {}\n",
            "dev/cc.lock.json": json.dumps({"name": "Beta", "sha256": sha256}) + "\n",
            "runtime.lock.json": '{"sourceCommit": "none"}\n',
            "docs/notes.md": f"Beta landed at {self.beta}.\nBeta before: {self.beta_old}\nOur own: {alpha_own}\n"
                             f"A hash: {sha256}\n",
            "evidence/old.md": f"WB_PIN_CACHE and {self.beta}\n",
        }, "alpha two")
        set_file = {"schema": 1, "projects": {"alpha": {"branch": "main", "commit": self.alpha},
                                              "beta": {"branch": "main", "commit": self.beta}},
                    "doctor": {"exempt": ["evidence/**"]}}
        commit(root / "Set", {"set.json": json.dumps(set_file)}, "the set")
        lab = root / "Lab"
        (lab / "cubic-lab").mkdir(parents=True)
        (lab / "coordination").mkdir()
        (lab / "cubic-lab" / "native-measurement.lock").write_text("\n", encoding="utf-8")
        link(lab / "cubic-lab", root / "Alpha" / ".local" / "cubic-lab")
        link(lab / "coordination", root / "Alpha" / ".local" / "coordination")
        (root / "Beta" / ".local" / "coordination").mkdir(parents=True)
        values = {"modules": {"ci": {"projects_root": str(root), "records": str(root / "records"),
                                     "projects": {"alpha": {"repo": "Alpha"}, "beta": {"repo": "Beta"},
                                                  "gamma": {"repo": "Gamma"}, "set": {"repo": "Set"}}}},
                  "workspace": {"set": str(root / "Set" / ".git")},
                  "schedules": {"pins": {"command": "toolchain.refresh-pins", "every": "1h"},
                                "market": {"command": "vast.market", "every": "30m"},
                                "off": {"command": "ci.sync", "every": "1h", "enabled": False}}}
        self.workspace = Workspace(Config(values, root / "config", {"schedules.pins.command": "schedules.toml:2"}))
        self.settings = SimpleNamespace(runner={"lab_home": str(lab),
                                                "lease_file": str(lab / "cubic-lab" / "native-measurement.lock"),
                                                "coordination_dir": str(lab / "coordination")}, lab={})

    def report(self):
        return doctor.run(self.workspace, self.settings)

    def test_every_pin_on_a_set_named_branch_is_named_with_its_file_and_line(self):
        report = self.report()
        found = {(f["project"], f["kind"], f["file"], f["line"]) for f in report["findings"]
                 if f["kind"] not in ("lease", "refresh schedule")}
        self.assertEqual(found, {
            ("alpha", "WB_PIN_CACHE", "build.gradle", 1),
            ("alpha", "refresh task", "build.gradle", 2),
            ("alpha", "pin file", "dev/cc.lock.json", 1),
            ("alpha", "sourceCommit", "runtime.lock.json", 1),
            ("alpha", "sibling commit", "docs/notes.md", 1),
            ("alpha", "sibling commit", "docs/notes.md", 2),
        })
        branches = {f["branch"] for f in report["findings"] if f["project"] == "alpha"}
        self.assertEqual(branches, {f"main (landed {self.alpha[:12]} = tip)"})
        text = doctor.show(report)
        self.assertIn(f"alpha  main (landed {self.alpha[:12]} = tip)  docs/notes.md:2  sibling commit: beta "
                      f"{self.beta_old[:12]}", text)

    def test_exempt_paths_and_unconverted_branches_are_not_read(self):
        report = self.report()
        self.assertFalse([f for f in report["findings"] if f["file"].startswith("evidence/")])
        self.assertFalse([f for f in report["findings"] if f["project"] == "gamma"])
        self.assertTrue(any(note.startswith("gamma: main") and "not converted" in note for note in report["notes"]))
        self.assertEqual(report["exempt"], ["evidence/**"])

    def test_a_landed_commit_behind_its_branch_is_read_as_well_as_the_tip(self):
        tip = commit(self.root / "Alpha", {"build.gradle": "// clean\n"}, "alpha three")
        report = self.report()
        alpha = [item for item in report["read"] if item.startswith("alpha ")]
        self.assertEqual(alpha, [f"alpha main (landed {self.alpha[:12]})", f"alpha main (main {tip[:12]})"])
        at_tip = {f["file"] for f in report["findings"] if f["branch"] == f"main (main {tip[:12]})"}
        self.assertNotIn("build.gradle", at_tip)

    def test_a_schedule_that_refreshes_pins_is_named(self):
        schedules = [f for f in self.report()["findings"] if f["kind"] == "refresh schedule"]
        self.assertEqual([(f["file"], f["line"]) for f in schedules], [("schedules.toml", 2)])
        self.assertIn("toolchain.refresh-pins", schedules[0]["text"])

    def test_the_old_lock_path_and_each_checkouts_route_to_it_are_the_lease(self):
        report = self.report()
        confirmed = "\n".join(report["confirmed"])
        self.assertIn("native-measurement.lock is the lease file", confirmed)
        self.assertIn("alpha's old lock route .local/cubic-lab/native-measurement.lock resolves to", confirmed)
        self.assertIn("alpha's old lanes folder route .local/coordination resolves to", confirmed)
        lease = [(f["project"], f["file"]) for f in report["findings"] if f["kind"] == "lease"]
        self.assertEqual(lease, [("beta", ".local/coordination")])

    def test_a_clean_machine_has_nothing_to_report(self):
        commit(self.root / "Alpha", {}, "clean", remove=["build.gradle", "dev/cc.lock.json", "runtime.lock.json",
                                                         "docs/notes.md"])
        set_file = json.loads((self.root / "Set" / "set.json").read_text(encoding="utf-8"))
        set_file["projects"]["alpha"]["commit"] = git(self.root / "Alpha", "rev-parse", "HEAD")
        commit(self.root / "Set", {"set.json": json.dumps(set_file)}, "land alpha")
        shutil.rmtree(self.root / "Beta" / ".local")
        del self.workspace.values["schedules"]["pins"]
        report = self.report()
        self.assertEqual(report["findings"], [])
        self.assertIn("doctor: nothing found", doctor.show(report))


if __name__ == "__main__":
    unittest.main()
