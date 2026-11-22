import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orthrus_tools import jdk


def fake_jdk(root: Path, name: str, version: str, vendor: str) -> Path:
    home = root / name
    (home / "bin").mkdir(parents=True)
    (home / "bin" / jdk.EXECUTABLE).write_text("")
    (home / "release").write_text(f'JAVA_VERSION="{version}"\nIMPLEMENTOR="{vendor}"\n')
    return home


class JdkTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.homes = [
            fake_jdk(self.root, "temurin-25", "25.0.5", "Eclipse Adoptium"),
            fake_jdk(self.root, "temurin-26", "26.0.2.1", "Eclipse Adoptium"),
            fake_jdk(self.root, "zulu-26", "26.0.1", "Azul Systems, Inc."),
        ]
        patcher = mock.patch.object(
            jdk,
            "candidate_homes",
            side_effect=lambda major, given=None: iter(
                ([jdk._home_of(given)] if given else []) + self.homes
            ),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.temporary.cleanup()

    def test_major_exact_and_vendor_selection(self):
        self.assertEqual(jdk.find(25).version, "25.0.5")
        self.assertEqual(jdk.find(26).home, self.homes[1].resolve())
        self.assertEqual(jdk.find(26, vendor="azul").version, "26.0.1")
        self.assertEqual(jdk.find(26, exact="26.0.1").vendor, "Azul Systems, Inc.")
        self.assertIsNone(jdk.find(26, exact="26.0.9"))
        self.assertIsNone(jdk.find(21))

    def test_an_explicit_java_wins(self):
        explicit = fake_jdk(self.root, "custom", "26.0.3", "Custom")
        found = jdk.find(26, given=explicit / "bin" / jdk.EXECUTABLE)
        self.assertEqual(found.version, "26.0.3")

    def test_legacy_and_build_suffix_versions(self):
        self.assertEqual(jdk.Jdk(Path("x"), "1.8.0_402", "").major, 8)
        self.assertEqual(jdk.Jdk(Path("x"), "26+35", "").major, 26)
        found = jdk.inspect(fake_jdk(self.root, "build", "26.0.2.1+1", "Eclipse Adoptium"))
        self.assertEqual(found.major, 26)

    def test_cli_exit_codes(self):
        self.assertEqual(jdk.main(["find", "26", "--home"]), 0)
        self.assertEqual(jdk.main(["find", "17"]), 3)
        self.assertEqual(jdk.main(["list"]), 0)


class CandidateTest(unittest.TestCase):
    def test_github_setup_java_variables_come_first(self):
        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.dict(os.environ, {"JAVA_HOME_26_X64": temporary}),
        ):
            first = next(jdk.candidate_homes(26))
            self.assertEqual(first, Path(temporary))


if __name__ == "__main__":
    unittest.main()
