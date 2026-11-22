import json
import tempfile
import unittest
from pathlib import Path

from orthrus_tools import strict_math


class CallTests(unittest.TestCase):
    def calls(self, text):
        return dict(strict_math.inexact_calls(text)[0])

    def test_inexact_calls_are_counted_and_exact_ones_allowed(self):
        text = "double a = Math.sin(x) + Math.sin(y) * Math.floor(z) + Math.pow(a, b);"
        self.assertEqual(self.calls(text), {"sin": 2, "pow": 1})

    def test_strict_math_constants_and_method_references(self):
        text = "StrictMath.sin(x); double p = Math.PI; f(Math::cos); f(Math::abs);"
        self.assertEqual(self.calls(text), {"cos": 1})

    def test_comments_and_literals_are_not_calls(self):
        text = '// Math.sin(x)\n/* Math.exp(y) */ String s = "Math.pow(a, b)"; char c = \'"\';'
        text += " Math.tanh(t);"
        self.assertEqual(self.calls(text), {"tanh": 1})

    def test_text_blocks_are_not_calls(self):
        text = 'String s = """\n  Math.sin(x)\n  """; Math.log(y);'
        self.assertEqual(self.calls(text), {"log": 1})

    def test_qualified_and_spaced_calls(self):
        self.assertEqual(self.calls("java.lang.Math.sin(x); Math . cos (y);"), {"sin": 1, "cos": 1})

    def test_static_imports_are_reported_by_line(self):
        text = "package a;\n\nimport static java.lang.Math.sin;\n"
        self.assertEqual(strict_math.inexact_calls(text)[1], [3])


class BaselineTests(unittest.TestCase):
    def test_growth_fails_and_shrinkage_is_noted(self):
        baseline = {"A.java": {"sin": 2}, "B.java": {"exp": 1}}
        found = {"A.java": {"sin": 3}, "C.java": {"pow": 1}}
        added, spare = strict_math.compare(found, baseline)
        self.assertEqual(len(added), 2)
        self.assertTrue(added[0].startswith("A.java: Math.sin x1"))
        self.assertEqual(spare, ["B.java: Math.exp 1 -> 0"])


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "src" / "main" / "java" / "a"
        self.source.mkdir(parents=True)

    def tearDown(self):
        self.temporary.cleanup()

    def java(self, text):
        (self.source / "A.java").write_text(text, encoding="utf-8")

    def run_tool(self, *args):
        return strict_math.main(["--root", str(self.root), *args])

    def test_ratchet_lifecycle(self):
        self.java("class A { double f(double x) { return Math.sin(x) + Math.cos(x); } }")
        self.assertEqual(self.run_tool(), 1)
        self.assertEqual(self.run_tool("--init"), 0)
        baseline = json.loads((self.root / "strict-math-baseline.json").read_text("utf-8"))
        self.assertEqual(baseline["files"], {"src/main/java/a/A.java": {"cos": 1, "sin": 1}})
        self.assertEqual(self.run_tool(), 0)
        self.assertEqual(self.run_tool("--init"), 2)
        self.java("class A { double f(double x) { return StrictMath.sin(x) + Math.cos(x); } }")
        self.assertEqual(self.run_tool("--update"), 0)
        self.java("class A { double f(double x) { return Math.sin(x) + Math.cos(x); } }")
        self.assertEqual(self.run_tool("--update"), 1)

    def test_settings_come_from_orthrus_toml(self):
        other = self.root / "core" / "java"
        other.mkdir(parents=True)
        (other / "B.java").write_text("class B { double g = Math.exp(1); }", encoding="utf-8")
        (self.root / "orthrus.toml").write_text(
            '[strict-math]\nsources = ["core/java"]\nbaseline = "tools/sm.json"\n', encoding="utf-8"
        )
        self.assertEqual(self.run_tool("--init"), 0)
        self.assertTrue((self.root / "tools" / "sm.json").is_file())

    def test_no_source_root_is_a_usage_error(self):
        self.assertEqual(self.run_tool("--sources", "nowhere"), 2)


if __name__ == "__main__":
    unittest.main()
