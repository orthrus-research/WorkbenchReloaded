"""The vocabulary (docs/vocabulary.md) against the help text: every verb's and option's help and every module's and
command's summary keep to its words. A retired word, a word in another sense, or a "Kept in" place that no longer
uses its word fails, as the command tables' check fails on a stale table."""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))
import vocabulary  # noqa: E402


class Vocabulary(unittest.TestCase):
    def copy(self) -> Path:
        root = Path(tempfile.mkdtemp(prefix="wb-vocabulary-"))
        self.addCleanup(shutil.rmtree, root, True)
        (root / "docs").mkdir()
        shutil.copy2(REPO / vocabulary.VOCABULARY, root / vocabulary.VOCABULARY)
        for manifest in REPO.glob("modules/*/wb_*/module.toml"):
            target = root / manifest.relative_to(REPO)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(manifest, target)
        return root

    def test_the_help_text_agrees_with_the_vocabulary(self):
        self.assertEqual(vocabulary.findings(), [], "fix the help text, or the vocabulary's Kept in")

    def test_it_reads_every_verb_option_module_and_command(self):
        places = dict(vocabulary.help_texts())
        for where in ("wb check", "wb check --with", "wb land --hold", "ci", "ci.run", "vast.instances"):
            self.assertIn(where, places)
        self.assertNotIn("wb prune", places)  # a hidden old verb says nothing of its own

    def test_the_vocabulary_has_its_concepts_and_both_tables(self):
        text = (REPO / vocabulary.VOCABULARY).read_text(encoding="utf-8")
        concepts = text.split("## Concepts", 1)[1].split("## ", 1)[0]
        rows = [line for line in concepts.splitlines() if line.startswith("| ") and not line.startswith("| Concept")]
        self.assertEqual(len(rows), 25)
        rules = vocabulary.rules()
        self.assertIn("pin cache", [p for r in rules if r.kind == "retired" for p in r.phrases])
        self.assertIn("job", [r.word for r in rules if r.kind == "sense"])

    def with_texts(self, *extra: tuple[str, str]) -> list[str]:
        texts = vocabulary.help_texts()
        with mock.patch.object(vocabulary, "help_texts", return_value=texts + list(extra)):
            return vocabulary.findings()

    def test_a_retired_word_in_help_fails(self):
        found = self.with_texts(("wb gpu --note", "what the turn is for, on the pin chain"))
        self.assertEqual(len(found), 1, found)
        self.assertTrue(found[0].startswith("wb gpu --note: 'pin chain' is retired"), found)
        self.assertIn("say the Set", found[0])

    def test_a_word_in_another_sense_fails(self):
        found = self.with_texts(("vast.keys", "List the account's instances and keys."))
        self.assertEqual(len(found), 1, found)
        self.assertIn("uses instance in another sense", found[0])

    def test_a_kept_place_may_use_its_word_and_others_may_not(self):
        self.assertEqual(self.with_texts(("wb check --extra", "a workflow of the actions file")), [])
        found = self.with_texts(("wb work --extra", "a workflow of the actions file"))
        self.assertEqual(len(found), 1, found)
        self.assertIn("'workflow' is retired", found[0])

    def test_a_summary_that_takes_up_a_retired_word_fails(self):
        root = self.copy()
        manifest = root / "modules" / "vast" / "wb_vast" / "module.toml"
        text = manifest.read_text(encoding="utf-8")
        manifest.write_text(text.replace("List the account's SSH keys.", "List the account's SSH keys and its "
                                         "lab profile."), encoding="utf-8")
        found = vocabulary.findings(root)
        self.assertEqual([f.split(":")[0] for f in found], ["vast.keys"], found)

    def test_a_kept_place_that_no_longer_uses_its_word_fails(self):
        root = self.copy()
        manifest = root / "modules" / "ci" / "wb_ci" / "module.toml"
        text = manifest.read_text(encoding="utf-8")
        self.assertIn("(pinned repositories at their pins)", text)
        manifest.write_text(text.replace("(pinned repositories at their pins)", "(the Set's lines)"),
                            encoding="utf-8")
        found = vocabulary.findings(root)
        self.assertEqual(len(found), 1, found)
        self.assertIn("pins is kept in ci.run, which no longer uses it", found[0])


if __name__ == "__main__":
    unittest.main()
