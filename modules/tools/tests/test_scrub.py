"""The commit scrub. Sensitive fixtures are assembled at runtime so this file passes its own scrub.

Every test runs against an empty rules file, so the machine's own rules never change a result."""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from orthrus_tools import scrub

CO = "Co-" + "Authored-By"
TRAILER = "Assisted-" + "by: a tool"
CREDIT = f"{CO}: Examplebot <bot@example.com>"
DRIVE = "E" + ":" + "\\Projects\\thing"
HOME = "/" + "home" + "/alice/work"
MAIL = "alice" + "@" + "corp.net"

_saved_rules = None
_empty_rules = None


def setUpModule():
    global _saved_rules, _empty_rules
    _empty_rules = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False)
    _empty_rules.close()
    _saved_rules = os.environ.get("ORTHRUS_SCRUB_RULES_FILE")
    os.environ["ORTHRUS_SCRUB_RULES_FILE"] = _empty_rules.name


def tearDownModule():
    if _saved_rules is None:
        os.environ.pop("ORTHRUS_SCRUB_RULES_FILE", None)
    else:
        os.environ["ORTHRUS_SCRUB_RULES_FILE"] = _saved_rules
    os.unlink(_empty_rules.name)


def rules(**overrides):
    base = scrub.Rules(
        allow_paths=[],
        deny_paths=list(scrub.DEFAULT_DENY_PATHS),
        allow_emails=[re.compile(p, re.I) for p in scrub.DEFAULT_ALLOW_EMAILS],
        terms=[],
    )
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


def categories(findings):
    return sorted({finding.category for finding in findings})


class LineTest(unittest.TestCase):
    def check(self, line, **overrides):
        return categories(scrub.check_line(line, "x", rules(**overrides)))

    def test_built_in_trailers(self):
        for line in (TRAILER, "Example-" + "Session: https://example.com/s/1", "Generated-" + "by: x"):
            self.assertIn("attribution", self.check(line), line)

    def test_names_come_from_the_rules(self):
        named = scrub.attribution_patterns(["examplebot", r"tool-?\d"])
        self.assertEqual(self.check(CREDIT), [])
        self.assertEqual(self.check(CREDIT, attribution=named), ["attribution"])
        self.assertEqual(self.check("Made " + "with [Examplebot](https://example.com)", attribution=named), ["attribution"])
        self.assertEqual(self.check("Written " + "using tool-2", attribution=named), ["attribution"])
        self.assertEqual(self.check("Made " + "with care", attribution=named), [])

    def test_denied_patterns(self):
        denied = [re.compile(r"example\.chat/share")]
        self.assertEqual(self.check("see https://example.chat/share/1", deny_patterns=denied), ["denied-text"])
        self.assertEqual(self.check("see https://example.chat/about", deny_patterns=denied), [])

    def test_human_co_authors_are_personal_data_not_attribution(self):
        line = f"{CO}: Alice <{MAIL}>"
        self.assertEqual(self.check(line, attribution=scrub.attribution_patterns(["examplebot"])), ["personal-data"])

    def test_local_paths(self):
        self.assertEqual(self.check(f"see {DRIVE}"), ["local-path"])
        self.assertEqual(self.check(f"cd {HOME}"), ["local-path"])
        self.assertEqual(self.check("cd /home/$USER/x and /Users/<name>/y"), [])
        self.assertEqual(self.check("a ratio 3:4 and http://x.org/a"), [])

    def test_allowed_and_placeholder_emails(self):
        self.assertEqual(self.check("mail 12345+bot@users.noreply.github.com"), [])
        self.assertEqual(self.check("mail someone@example.com"), [])
        self.assertEqual(self.check(f"mail {MAIL}"), ["personal-data"])
        self.assertEqual(self.check("pip install 'pkg @ git+https://x.org/r@v0.2.0'"), [])
        self.assertEqual(self.check("uses: org/repo/.github/workflows/ci.yml@vX.Y.Z"), [])
        self.assertEqual(self.check("mail someone" + "@mail.io"), ["personal-data"])

    def test_terms_are_whole_words_and_never_printed(self):
        terms = [re.compile(r"(?i)(?<!\w)Zed(?!\w)")]
        findings = list(scrub.check_line("ask zed about it", "x", rules(terms=terms)))
        self.assertEqual(len(findings), 1)
        self.assertNotIn("zed", findings[0].detail.lower())
        self.assertEqual(list(scrub.check_line("zedonk", "x", rules(terms=terms))), [])

    def test_the_allow_marker_skips_a_line(self):
        text = f"path = r'{DRIVE}'  # {scrub.ALLOW_MARKER}\nother {DRIVE}\n"
        found = list(scrub.check_text(text, "f.py", rules()))
        self.assertEqual([f.where for f in found], ["f.py:2"])


class PathTest(unittest.TestCase):
    def test_globs(self):
        self.assertTrue(scrub.glob_match("NOTES.md", "**/NOTES.md"))
        self.assertTrue(scrub.glob_match("a/b/NOTES.md", "**/NOTES.md"))
        self.assertTrue(scrub.glob_match(".tooling/settings.json", ".tooling/**"))
        self.assertFalse(scrub.glob_match("docs/tooling.md", ".tooling/**"))
        self.assertTrue(scrub.glob_match(".editor.conf.yml", ".editor*"))

    def test_deny_and_allow(self):
        self.assertEqual(categories(scrub.check_path(".local/run.log", rules())), ["private-file"])
        self.assertEqual(list(scrub.check_path(".tooling/x.json", rules())), [])
        denied = rules(deny_paths=scrub.DEFAULT_DENY_PATHS + [".tooling/**", "**/NOTES.md"])
        self.assertEqual(categories(scrub.check_path(".tooling/x.json", denied)), ["private-file"])
        denied.allow_paths = ["shared/**"]
        self.assertEqual(list(scrub.check_path("shared/templates/NOTES.md", denied)), [])
        self.assertEqual(list(scrub.check_path("src/app.py", rules())), [])

    def test_added_lines_of_a_unified_diff(self):
        diff = (
            "diff --git a/f.txt b/f.txt\n--- a/f.txt\n+++ b/f.txt\n@@ -1,0 +3,2 @@\n+one\n+two\n"
            "diff --git a/g b/g\n--- a/g\n+++ /dev/null\n@@ -1 +0,0 @@\n-gone\n"
        )
        self.assertEqual(list(scrub.added_lines(diff)), [("f.txt", 3, "one"), ("f.txt", 4, "two")])


class RulesFileTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.saved = os.environ.get("ORTHRUS_SCRUB_RULES_FILE")

    def tearDown(self):
        os.environ["ORTHRUS_SCRUB_RULES_FILE"] = self.saved
        self.temporary.cleanup()

    def test_the_machine_rules_add_to_the_repository_settings(self):
        (self.root / "orthrus.toml").write_text('[scrub]\ndeny-paths = ["**/NOTES.md"]\n', encoding="utf-8")
        machine = self.root.parent / f"{self.root.name}-rules.toml"
        machine.write_text(
            'attribution-names = ["examplebot"]\ndeny-patterns = [\'example\\.chat/share\']\n'
            'deny-paths = [".tooling/**"]\n',
            encoding="utf-8",
        )
        self.addCleanup(machine.unlink)
        os.environ["ORTHRUS_SCRUB_RULES_FILE"] = str(machine)
        loaded = scrub.Rules.load(self.root)
        self.assertEqual(categories(scrub.check_path("a/NOTES.md", loaded)), ["private-file"])
        self.assertEqual(categories(scrub.check_path(".tooling/x", loaded)), ["private-file"])
        self.assertEqual(categories(scrub.check_line(CREDIT, "x", loaded)), ["attribution"])
        self.assertEqual(categories(scrub.check_line("example.chat/share/9", "x", loaded)), ["denied-text"])

    def test_a_named_rules_file_must_exist(self):
        os.environ["ORTHRUS_SCRUB_RULES_FILE"] = str(self.root / "missing.toml")
        with self.assertRaises(scrub.ToolError):
            scrub.Rules.load(self.root)


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class RepositoryTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "core.hooksPath", ".git/hooks")

    def tearDown(self):
        self.temporary.cleanup()

    def git(self, *args, stdin=None):
        return subprocess.run(
            ["git", "-C", str(self.root), *args],
            check=True,
            capture_output=True,
            text=True,
            input=stdin,
        ).stdout

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def run_tool(self, *args):
        return scrub.main(["--root", str(self.root), *args])

    def commit(self, message):
        self.git("add", "-A")
        self.git("commit", "-q", "--no-verify", "--allow-empty", "-m", message)
        return self.git("rev-parse", "HEAD").strip()

    def test_staged_changes(self):
        self.write("ok.txt", "fine\n")
        self.git("add", "-A")
        self.assertEqual(self.run_tool("staged"), 0)
        self.write("notes.txt", f"build in {DRIVE}\n")
        self.write(".local/run.log", "output\n")
        self.git("add", "-A", "--force")
        self.assertEqual(self.run_tool("staged"), 1)

    def test_range_checks_messages_and_diffs(self):
        base = self.commit("Start")
        self.write("a.txt", "clean\n")
        self.commit("Add a clean file")
        self.assertEqual(self.run_tool("range", f"{base}..HEAD"), 0)
        self.write("b.txt", "clean too\n")
        self.commit(f"Add b\n\n{TRAILER}\n")
        self.assertEqual(self.run_tool("range", f"{base}..HEAD"), 1)

    def test_message_file_ignores_git_comments(self):
        message = self.root / "MSG"
        message.write_text(f"Fix it\n# {TRAILER}\n", encoding="utf-8")
        self.assertEqual(self.run_tool("message", str(message)), 0)
        message.write_text(f"Fix it\n\n{TRAILER}\n", encoding="utf-8")
        self.assertEqual(self.run_tool("message", str(message)), 1)

    def test_tree_and_settings(self):
        self.write("shared/NOTES.md", "shared rules\n")
        self.write("orthrus.toml", '[scrub]\ndeny-paths = ["**/NOTES.md"]\nallow-paths = ["shared/**"]\n')
        self.commit("Add shared rules")
        self.assertEqual(self.run_tool("tree"), 0)
        self.write(".local/scrub-terms.txt", "Zed\n")
        self.write("doc.md", "Ask Zed.\n")
        self.commit("Add doc")
        self.assertEqual(self.run_tool("tree"), 1)

    def test_pre_push_checks_only_unpushed_commits(self):
        base = self.commit("Start")
        self.write("b.txt", "x\n")
        bad = self.commit(f"Add b\n\n{TRAILER}\n")
        zero = "0" * 40
        findings = scrub.pre_push(
            self.root, f"refs/heads/main {bad} refs/heads/main {base}\n", rules()
        )
        self.assertEqual(categories(findings), ["attribution"])
        self.assertEqual(
            scrub.pre_push(self.root, f"refs/heads/main {base} refs/heads/main {base}\n", rules()),
            [],
        )
        self.assertEqual(
            scrub.pre_push(self.root, f"(delete) {zero} refs/heads/old {base}\n", rules()), []
        )
        findings = scrub.pre_push(
            self.root, f"refs/heads/main {bad} refs/heads/main {zero}\n", rules()
        )
        self.assertEqual(categories(findings), ["attribution"])

    @unittest.skipIf(sys.platform == "win32", "the hook test runs the shell script directly")
    def test_installed_hooks_block_a_bad_commit(self):
        self.assertEqual(self.run_tool("install"), 0)
        self.assertEqual(self.run_tool("install"), 0)  # reinstalling over its own hooks is fine
        environment = dict(os.environ)
        source = str(Path(scrub.__file__).resolve().parents[1])
        environment["PYTHONPATH"] = source + os.pathsep + environment.get("PYTHONPATH", "")
        self.write("a.txt", "clean\n")
        self.git("add", "-A")
        result = subprocess.run(
            ["git", "-C", str(self.root), "commit", "-q", "-m", f"Add a\n\n{TRAILER}\n"],
            capture_output=True,
            text=True,
            env=environment,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("attribution", result.stderr)
        result = subprocess.run(
            ["git", "-C", str(self.root), "commit", "-q", "-m", "Add a"],
            capture_output=True,
            text=True,
            env=environment,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_install_refuses_to_replace_a_foreign_hook(self):
        hooks = self.root / ".git" / "hooks"
        hooks.mkdir(parents=True, exist_ok=True)
        (hooks / "pre-commit").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.assertEqual(self.run_tool("install"), 2)
        self.assertEqual(self.run_tool("install", "--force"), 0)


if __name__ == "__main__":
    unittest.main()
