"""The Gradle home's set block: written once and replaced in place, the person's own lines kept, the platform's JDKs
joining Workbench's own JDK list, and a JDK list of the person's own left alone and reported."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness  # noqa: E402,F401

from wb import gradlehome  # noqa: E402

VALUES = {"set.home": "/srv/out/sets", "set.store": "/srv/out/store", "set.plugins": "file:////srv/out/store/plugins"}


class Block(unittest.TestCase):
    def test_written_once_and_replaced_in_place_keeping_other_lines(self):
        own = "org.gradle.daemon=false\nsecretKey=kept as it is\n"
        first, notes = gradlehome.merged(own, VALUES, ["/srv/out/store/jdks/8/windows/jdk8"])
        self.assertTrue(first.startswith(own))
        self.assertIn("set.home=/srv/out/sets\n", first)
        self.assertIn("org.gradle.java.installations.paths=/srv/out/store/jdks/8/windows/jdk8\n", first)
        again, _ = gradlehome.merged(first, VALUES, ["/srv/out/store/jdks/8/windows/jdk8"])
        self.assertEqual(again, first)
        moved, _ = gradlehome.merged(first, {**VALUES, "set.home": "/var/sets"}, [])
        self.assertEqual(moved.count(gradlehome.BEGIN), 1)
        self.assertIn("set.home=/var/sets", moved)
        self.assertIn("secretKey=kept as it is", moved)
        self.assertEqual(notes, [])

    def test_the_platforms_jdks_join_workbenchs_own_jdk_block(self):
        existing = (f"{gradlehome.JDK_BEGIN}\norg.gradle.java.installations.paths=/opt/jdk26\n"
                    "org.gradle.java.installations.auto-detect=false\n# <<< Workbench\n")
        text, _ = gradlehome.merged(existing, VALUES, ["/srv/out/store/jdks/8/windows/jdk8", "/opt/jdk26"])
        self.assertIn("org.gradle.java.installations.paths=/opt/jdk26,/srv/out/store/jdks/8/windows/jdk8\n", text)
        self.assertEqual(text.count("org.gradle.java.installations.paths="), 1)
        self.assertIn("auto-detect=false", text)  # left as it was

    def test_a_jdk_list_of_the_persons_own_is_left_alone_and_reported(self):
        own = "org.gradle.java.installations.paths=/opt/own-jdk\n"
        text, notes = gradlehome.merged(own, VALUES, ["/srv/out/store/jdks/8/windows/jdk8"])
        self.assertEqual(text.count("org.gradle.java.installations.paths="), 1)
        self.assertTrue(notes and "line of your own" in notes[0])


if __name__ == "__main__":
    unittest.main()
