"""The command tables in README.md and docs/commands.md match the module manifests: a hand edit inside a generated
section, or a command added to a manifest without regenerating, fails."""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))
import command_tables  # noqa: E402


class CommandTables(unittest.TestCase):
    def copy(self) -> Path:
        root = Path(tempfile.mkdtemp(prefix="wb-tables-"))
        self.addCleanup(shutil.rmtree, root, True)
        for file in command_tables.SECTIONS:
            (root / file).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO / file, root / file)
        for manifest in REPO.glob("modules/*/wb_*/module.toml"):
            target = root / manifest.relative_to(REPO)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(manifest, target)
        return root

    def test_the_tables_match_the_manifests(self):
        self.assertEqual(command_tables.stale(), [], "run python tools/command_tables.py")

    def test_a_hand_edit_in_a_table_fails(self):
        root = self.copy()
        readme = root / "README.md"
        text = readme.read_text(encoding="utf-8")
        readme.write_text(text.replace("`receipt`, `archive`", "`receipt`, `archive`, `stage`", 1), encoding="utf-8")
        self.assertEqual(command_tables.stale(root), ["README.md"])

    def test_a_new_command_without_regenerating_fails(self):
        root = self.copy()
        manifest = root / "modules" / "build" / "wb_build" / "module.toml"
        manifest.write_text(manifest.read_text(encoding="utf-8") + '\n[[command]]\nname = "stage"\nsummary = "x"\n'
                            'handler = "wb_build.files:stage"\n', encoding="utf-8")
        self.assertEqual(command_tables.stale(root), ["README.md", "docs/commands.md"])
        self.assertIn("`build.stage`", command_tables.expected(root)["docs/commands.md"])

    def test_text_outside_the_sections_is_kept(self):
        root = self.copy()
        before = (root / "README.md").read_text(encoding="utf-8")
        after = command_tables.expected(root)["README.md"]
        self.assertEqual(before, after)
        self.assertIn("## CI", after)


if __name__ == "__main__":
    unittest.main()
