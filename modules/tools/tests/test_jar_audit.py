import io
import json
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path

from orthrus_tools import classfile, cli, jar_audit
from tests.classbuilder import Pool, build, jar
from tests.support import capture, capture_json

JAVA_21 = 65


def plain(name="app/Main", major=JAVA_21, minor=0):
    return build(Pool(), name, major=major, minor=minor)


def referring(name="app/Main", descriptor=None, owner=None, literal=None):
    pool = Pool()
    if descriptor:
        pool.utf8(descriptor)
    if owner:
        pool.methodref(owner, "tick")
    if literal:
        pool.string(literal)
    return build(pool, name, major=JAVA_21)


class ParseTest(unittest.TestCase):
    def test_the_header_and_string_literals_are_read(self):
        pool = Pool()
        pool.string("hello")
        parsed = classfile.parse(build(pool, "a/B", major=70, minor=0))
        self.assertEqual((parsed.major, parsed.minor, parsed.this_class), (70, 0, "a/B"))
        self.assertEqual({parsed.utf8[i] for i in parsed.string_literals}, {"hello"})


class AuditTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def write(self, entries, name="app.jar"):
        path = self.directory / name
        path.write_bytes(jar(entries))
        return path

    def kinds(self, path, release=21, deny=(), allow_preview=False):
        report = jar_audit.audit_jar(
            path, release, [jar_audit.internal(d) for d in deny], allow_preview
        )
        return [(f["entry"], f["kind"]) for f in report["findings"]], report

    def test_a_clean_jar_has_no_findings(self):
        path = self.write(
            {"app/Main.class": plain(), "META-INF/MANIFEST.MF": b"Manifest-Version: 1.0\n"}
        )
        found, report = self.kinds(path)
        self.assertEqual(found, [])
        self.assertEqual(report["classes"], 1)
        self.assertEqual(report["versions"], {"65.0": 1})

    def test_wrong_versions_preview_and_versioned_entries(self):
        path = self.write(
            {
                "app/Old.class": plain("app/Old", major=52),
                "app/Preview.class": plain("app/Preview", minor=0xFFFF),
                "META-INF/versions/25/app/New.class": plain("app/New", major=69),
                "META-INF/versions/25/app/Bad.class": plain("app/Bad", major=JAVA_21),
            }
        )
        found, _ = self.kinds(path)
        self.assertEqual(
            found,
            [
                ("app/Old.class", "version"),
                ("app/Preview.class", "preview"),
                ("META-INF/versions/25/app/Bad.class", "version"),
            ],
        )
        found, _ = self.kinds(path, release=None, allow_preview=True)
        self.assertEqual(found, [])

    def test_names_truncation_and_duplicates(self):
        buffer = io.BytesIO()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with zipfile.ZipFile(buffer, "w") as archive:
                archive.writestr("app/Moved.class", plain("app/Elsewhere"))
                archive.writestr("app/Cut.class", plain("app/Cut")[:-4])
                archive.writestr("app/Twice.class", plain("app/Twice"))
                archive.writestr("app/Twice.class", plain("app/Twice"))
                archive.writestr("app/Junk.class", b"not a class")
        path = self.directory / "bad.jar"
        path.write_bytes(buffer.getvalue())
        found, _ = self.kinds(path)
        self.assertIn(("app/Twice.class", "duplicate"), found)
        self.assertIn(("app/Moved.class", "name"), found)
        self.assertIn(("app/Cut.class", "malformed"), found)
        self.assertIn(("app/Junk.class", "malformed"), found)

    def test_denied_references_in_classes_and_descriptors_but_not_literals(self):
        path = self.write(
            {
                "app/Owner.class": referring("app/Owner", owner="net/minecraft/world/World"),
                "app/Field.class": referring(
                    "app/Field", descriptor="Ljava/util/List<Lorg/lwjgl/opengl/GL11;>;"
                ),
                "app/Literal.class": referring("app/Literal", literal="net.minecraft.client.Main"),
                "app/Fine.class": referring("app/Fine", owner="java/lang/String"),
                "net/minecraft/Own.class": referring(
                    "net/minecraft/Own", owner="net/minecraft/Other"
                ),
            }
        )
        found, report = self.kinds(path, deny=["net.minecraft", "org/lwjgl/"])
        self.assertEqual(
            sorted(found), [("app/Field.class", "reference"), ("app/Owner.class", "reference")]
        )
        self.assertEqual(report["stringMentions"], ["net.minecraft.client.Main"])
        found, _ = self.kinds(path, deny=["net.minecraftforge"])
        self.assertEqual(found, [])

    def test_a_file_that_is_not_a_jar_is_a_finding(self):
        path = self.directory / "broken.jar"
        path.write_bytes(b"not a zip")
        found, _ = self.kinds(path)
        self.assertEqual(found, [("", "unreadable")])


class CommandTest(AuditTest):
    def test_exit_codes_and_json(self):
        clean = self.write({"app/Main.class": plain()}, "clean.jar")
        old = self.write({"app/Main.class": plain(major=52)}, "old.jar")
        code, document = capture_json(jar_audit.main, str(clean), "--release", "21")
        self.assertEqual(code, cli.OK)
        self.assertEqual(document["result"]["findings"], 0)
        code, out, _ = capture(jar_audit.main, str(clean), str(old), "--release", "21")
        self.assertEqual(code, cli.FINDING)
        self.assertIn("version    old.jar!app/Main.class", out)
        code, _ = capture_json(jar_audit.main, str(self.directory / "none.jar"))
        self.assertEqual(code, cli.USAGE)

    def test_settings_name_the_jars_release_and_denials(self):
        root = self.directory
        (root / "build").mkdir()
        self.write({"app/Main.class": referring(owner="net/minecraft/X")}, "build/a.jar")
        (root / "orthrus.toml").write_text(
            '[jar-audit]\njars = ["build/*.jar"]\nrelease = 21\ndeny = ["net.minecraft"]\n',
            encoding="utf-8",
        )
        code, document = capture_json(jar_audit.main, "--root", str(root))
        self.assertEqual(code, cli.FINDING)
        self.assertEqual(document["result"]["deny"], ["net.minecraft"])
        self.assertEqual(document["result"]["jars"][0]["findings"][0]["kind"], "reference")
        (root / "orthrus.toml").write_text("[jar-audit]\n", encoding="utf-8")
        code, document = capture_json(jar_audit.main, "--root", str(root))
        self.assertEqual(code, cli.USAGE)
        self.assertIn("name the JARs", json.dumps(document["messages"]))


if __name__ == "__main__":
    unittest.main()
