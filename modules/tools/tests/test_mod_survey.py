import struct
import tempfile
import unittest
from pathlib import Path

from orthrus_tools import classfile, mod_survey
from tests.classbuilder import Pool, build, jar

GL11 = "org/lwjgl/opengl/GL11"


def depth_func_class() -> bytes:
    pool = Pool()
    ref = pool.methodref(GL11, "glDepthFunc", "(I)V")
    return build(pool, "mod/Renderer", b"\x10\x07" + b"\xb8" + struct.pack(">H", ref) + b"\xb1")


def mixin_class() -> bytes:
    pool = Pool()
    pool.utf8(mod_survey.MIXIN)
    pool.utf8("Lnet/minecraft/client/renderer/EntityRenderer;")
    return build(pool, "mod/mixin/MixinEntityRenderer")


def depth_read_class() -> bytes:
    pool = Pool()
    ref = pool.methodref(GL11, "glReadPixels", "(IIIIIILjava/nio/FloatBuffer;)V")
    # sipush GL_DEPTH_COMPONENT (0x1902), then invokestatic glReadPixels
    code = b"\x11" + struct.pack(">h", 0x1902) + b"\xb8" + struct.pack(">H", ref) + b"\xb1"
    return build(pool, "mod/DepthReader", code)


class ClassFileTest(unittest.TestCase):
    def test_a_built_class_parses_completely(self) -> None:
        parsed = classfile.parse(depth_func_class())
        self.assertTrue(parsed.complete)
        self.assertIn((GL11, "glDepthFunc"), parsed.methods)
        self.assertEqual([name for name, _ in parsed.method_bodies], ["run"])

    def test_garbage_and_truncation_never_raise(self) -> None:
        self.assertFalse(classfile.parse(b"not a class").complete)
        data = depth_func_class()
        for cut in (12, len(data) // 2, len(data) - 3):
            self.assertFalse(classfile.parse(data[:cut]).complete)

    def test_long_constants_take_two_slots(self) -> None:
        pool = Pool()
        pool.entries.append(b"\x05" + struct.pack(">q", 7))
        pool.entries.append(b"")  # the unusable second slot of a long
        ref = pool.methodref(GL11, "glEnable", "(I)V")
        parsed = classfile.parse(build(pool))
        self.assertTrue(parsed.complete)
        self.assertEqual(parsed.member(ref), (GL11, "glEnable"))


class SurveyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write(self, name: str, entries: dict[str, bytes]) -> Path:
        path = self.root / name
        path.write_bytes(jar(entries))
        return path

    def test_calls_patches_and_depth_reads_are_found(self) -> None:
        path = self.write(
            "sample.jar",
            {
                "mod/Renderer.class": depth_func_class(),
                "mod/mixin/MixinEntityRenderer.class": mixin_class(),
                "mod/DepthReader.class": depth_read_class(),
                "assets/readme.txt": b"hello",
            },
        )
        result = mod_survey.survey_jar(path)
        self.assertEqual(result["classes"], 3)
        self.assertEqual(result["calls"]["depth function, direct"], 1)
        self.assertEqual(result["patches"], {"EntityRenderer": ["Mixin"]})
        self.assertEqual(result["depth"], {"reads depth pixels": ["mod/DepthReader"]})

    def test_nested_jars_count_toward_their_mod(self) -> None:
        inner = jar({"lib/Inner.class": depth_func_class()})
        path = self.write("outer.jar", {"META-INF/jarjar/inner.jar": inner})
        result = mod_survey.survey_jar(path)
        self.assertEqual(result["nested"], ["META-INF/jarjar/inner.jar"])
        self.assertEqual(result["calls"]["depth function, direct"], 1)

    def test_a_broken_archive_is_reported_not_raised(self) -> None:
        path = self.root / "broken.jar"
        path.write_bytes(b"PK not really")
        self.assertIn("error", mod_survey.survey_jar(path))

    def test_cli_accepts_a_directory_or_one_jar(self) -> None:
        path = self.write("sample.jar", {"mod/Renderer.class": depth_func_class()})
        out = self.root / "out" / "survey.json"
        self.assertEqual(mod_survey.main([str(self.root), "--out", str(out)]), 0)
        self.assertTrue(out.is_file())
        self.assertEqual(mod_survey.main([str(path)]), 0)
        self.assertEqual(mod_survey.main([str(self.root / "missing")]), 2)


if __name__ == "__main__":
    unittest.main()
