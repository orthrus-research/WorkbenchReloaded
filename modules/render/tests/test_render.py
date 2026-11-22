"""render: the shader pack contract, flattening, profiles and the driver-hazard lint on tiny synthetic packs; load
verdicts on synthetic logs; GL gates on synthetic probe reports; and the three commands through the daemon."""

import gzip
import json
import shutil
import struct
import sys
import tempfile
import textwrap
import unittest
import zipfile
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO / "tests"))
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(REPO / "modules" / "lab"))  # gl-run uses the lab's process runner, as Workbench installs it

from harness import Daemon  # noqa: E402

from wb_render import gl, hazards, profiles, verdict  # noqa: E402
from wb_render.pack import Pack, _Expression  # noqa: E402

GLSLANG = shutil.which("glslangValidator")

VERTEX = """\
#version 330 compatibility
#include "/lib/common.glsl"
out vec4 color;
void main() {
    color = gl_Color * vec4(tint(), 1.0);
    gl_Position = ftransform();
}
"""
FRAGMENT = """\
#version 330 compatibility
#include "/lib/common.glsl"
in vec4 color;
void main() {
    gl_FragData[0] = color;
}
"""
GOOD = {
    "shaders/lib/settings.glsl": """\
        #define SHADOWS // shadows
        #define QUALITY 2 // [1 2 3]
        //#define FOG
        const float sunPathRotation = -40.0; // [-40.0 0.0 40.0]
        """,
    "shaders/lib/common.glsl": """\
        #include "settings.glsl"
        #ifdef FOG
        #endif
        vec3 tint() {
        #ifdef SHADOWS
            return vec3(float(QUALITY) / 3.0);
        #else
            return vec3(1.0);
        #endif
        }
        """,
    "shaders/gbuffers_basic.vsh": VERTEX,
    "shaders/gbuffers_basic.fsh": FRAGMENT,
    "shaders/composite.vsh": VERTEX,
    "shaders/composite.fsh": FRAGMENT,
    "shaders/shaders.properties": """\
        screen=SHADOWS QUALITY [MORE] <empty>
        screen.MORE=FOG sunPathRotation
        sliders=QUALITY sunPathRotation
        profile.LOW=!SHADOWS QUALITY:1
        profile.HIGH=profile.LOW SHADOWS QUALITY:3
        #ifdef SHADOWS
        shadowMapResolution=2048
        #else
        shadowMapResolution=1024
        #endif
        """,
    "shaders/block.properties": "block.10001=minecraft:leaves minecraft:tallgrass\nblock.10002=minecraft:water\n",
}


def write(root: Path, files: dict) -> Path:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text), encoding="utf-8")
    return root


def pack(files: dict, **changes) -> Pack:
    root = Path(tempfile.mkdtemp(prefix="wb-render-"))
    write(root, {**files, **changes})
    return Pack(root / "shaders")


def codes(problems) -> list[str]:
    return [p.code for p in problems]


def png(width: int = 2, height: int = 2) -> bytes:
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class Contract(unittest.TestCase):
    def test_a_sound_pack_keeps_its_contract(self):
        model = pack(GOOD)
        self.assertEqual(model.stages, ["composite.fsh", "composite.vsh", "gbuffers_basic.fsh", "gbuffers_basic.vsh"])
        self.assertEqual(set(model.programs), {"composite", "gbuffers_basic"})
        self.assertEqual(model.contract(required=["gbuffers_basic"]), [])
        options = model.options
        self.assertEqual((options["SHADOWS"].kind, options["SHADOWS"].default), ("switch", True))
        self.assertEqual((options["FOG"].kind, options["FOG"].default), ("switch", False))
        self.assertEqual(options["QUALITY"].allowed, ["1", "2", "3"])
        self.assertEqual(options["sunPathRotation"].default, "-40.0")

    def test_includes_must_resolve_inside_the_root_without_cycles(self):
        model = pack(GOOD, **{"shaders/lib/common.glsl": '#include "missing.glsl"\n#include "../../outside.glsl"\n'
                                                         '#include "loop.glsl"\nvec3 tint() { return vec3(1.0); }\n',
                              "shaders/lib/loop.glsl": '#include "common.glsl"\n'})
        found = model.contract()
        self.assertIn("include-missing", codes(found))
        self.assertIn("include-escapes", codes(found))
        self.assertIn("include-cycle", codes(found))
        missing = next(p for p in found if p.code == "include-missing")
        self.assertEqual((missing.file, missing.line), ("lib/common.glsl", 1))

    def test_an_include_that_matches_only_ignoring_case_is_named(self):
        model = pack(GOOD, **{"shaders/composite.fsh": FRAGMENT.replace("/lib/common.glsl", "/lib/Common.glsl")})
        found = model.contract()
        if (model.root / "lib" / "Common.glsl").exists():  # a case-insensitive file system
            self.assertIn("include-case", codes(found))
        else:
            self.assertIn("include-missing", codes(found))

    def test_stages_are_paired_and_required_programs_present(self):
        model = pack(GOOD, **{"shaders/composite1.fsh": FRAGMENT, "shaders/shadow.gsh": "#version 330\n",
                              "shaders/world-1/composite.vsh": VERTEX, "shaders/lib/helper.vsh": "// not a stage\n"})
        found = model.contract(required=["gbuffers_basic", "final"])
        unpaired = [p.message for p in found if p.code == "stage-unpaired"]
        self.assertTrue(any("composite1 has a fragment stage but no vertex" in m for m in unpaired), unpaired)
        self.assertTrue(any("shadow has a geometry stage" in m for m in unpaired), unpaired)
        self.assertTrue(any("world-1/composite has a vertex stage" in m for m in unpaired), unpaired)
        self.assertIn("program-missing", codes(found))
        self.assertNotIn("lib/helper.vsh", model.stages)  # a stage lives in the root or a world folder only

    def test_version_present_and_consistent(self):
        model = pack(GOOD, **{"shaders/composite.fsh": FRAGMENT.replace("#version 330 compatibility", "#version 120"),
                              "shaders/gbuffers_basic.fsh": "// header\n/* more */\n" + FRAGMENT.split("\n", 1)[1]})
        found = model.contract()
        self.assertIn("version-missing", codes(found))
        inconsistent = next(p for p in found if p.code == "version-inconsistent")
        self.assertIn("#version 120 in composite.fsh", inconsistent.message)
        exact = pack(GOOD).contract(version="460 compatibility")
        self.assertEqual(set(codes(exact)), {"version-differs"})
        self.assertEqual(pack(GOOD).contract(version="330 compatibility"), [])
        self.assertEqual(pack(GOOD).version_of("composite.vsh")[0], "330 compatibility")

    def test_a_version_reached_through_an_include_counts(self):
        model = pack(GOOD, **{"shaders/lib/version.glsl": "#version 330 compatibility\n",
                              "shaders/composite.fsh": '#include "/lib/version.glsl"\n' + FRAGMENT.split("\n", 1)[1]})
        self.assertEqual(model.contract(), [])

    def test_properties_duplicates_and_assignments(self):
        model = pack(GOOD, **{
            "shaders/shaders.properties": GOOD["shaders/shaders.properties"] + "sliders=QUALITY\n",
            "shaders/block.properties": "block.10001=minecraft:leaves\nblock.abc=minecraft:stone\n"
                                        "block.10002=minecraft:leaves\n#if QUALITY >= 2 && defined(SHADOWS)\n"
                                        "block.10003=minecraft:sand\n#elif FOO\nblock.10003=minecraft:gravel\n#endif\n"})
        found = model.contract()
        self.assertIn("property-duplicate", codes(found))
        self.assertIn("assignment-id", codes(found))
        duplicate = next(p for p in found if p.code == "assignment-duplicate")
        self.assertIn("minecraft:leaves is assigned to block.10001 and block.10002", duplicate.message)
        props, _ = model.properties("block.properties", model.environment({}))
        self.assertEqual(props["block.10003"][0], "minecraft:sand")
        props, _ = model.properties("block.properties", model.environment({"SHADOWS": False}))
        self.assertNotIn("block.10003", props)

    def test_option_references_profiles_and_screens(self):
        model = pack(GOOD, **{"shaders/shaders.properties": """\
            screen=SHADOWS UNKNOWN [NOWHERE]
            profile.LOW=!SHADOWS QUALITY:7 MISSING
            profile.HIGH=profile.LOW profile.NONE
            profile.A=profile.B
            profile.B=profile.A
            """})
        found = model.contract()
        messages = " | ".join(p.message for p in found)
        self.assertIn("screen names UNKNOWN, which no source declares", messages)
        self.assertIn("links to [NOWHERE], but there is no screen.NOWHERE", messages)
        self.assertIn("sets QUALITY to 7, not one of 1 2 3", messages)
        self.assertIn("sets MISSING, which no source", messages)
        self.assertIn("takes profile.NONE, which is not defined", messages)
        self.assertIn("profile inheritance cycle", messages)

    def test_a_switch_no_source_tests_is_not_an_option(self):
        model = pack(GOOD, **{"shaders/lib/settings.glsl": GOOD["shaders/lib/settings.glsl"] + "#define UNUSED\n",
                              "shaders/shaders.properties": "screen=UNUSED\n"})
        self.assertNotIn("UNUSED", model.options)
        self.assertIn("option-undefined", codes(model.contract()))

    def test_conditional_errors_are_problems(self):
        model = pack(GOOD, **{"shaders/shaders.properties": "#ifdef SHADOWS\nsliders=QUALITY\n#else\n#else\n#endif\n#endif\n"
                                                            "#if QUALITY >\n#endif\n#ifdef A\n"})
        messages = [p.message for p in model.contract() if p.code == "property-conditional"]
        self.assertTrue(any("second #else" in m for m in messages), messages)
        self.assertTrue(any("#endif without #if" in m for m in messages), messages)
        self.assertTrue(any("cannot evaluate #if" in m for m in messages), messages)
        self.assertTrue(any("never closed" in m for m in messages), messages)


class Flattening(unittest.TestCase):
    def test_includes_options_and_macros(self):
        model = pack(GOOD)
        flat = model.flatten("composite.fsh", {"SHADOWS": False, "QUALITY": "3"}, [("MC_VERSION", "11202"), ("DH", None)])
        lines = flat.text.splitlines()
        self.assertEqual(lines[0], "#version 330 compatibility")
        self.assertEqual(lines[1:3], ["#define MC_VERSION 11202", "#define DH"])
        self.assertIn("//#define SHADOWS", lines)
        self.assertIn("#define QUALITY 3 // [1 2 3]", lines)
        self.assertNotIn("#include", flat.text)
        index = lines.index("vec3 tint() {") + 1
        self.assertEqual(flat.where(index), ("lib/common.glsl", 4))
        self.assertEqual(flat.where(2), ("<profile macros>", 0))

    def test_const_options_are_set_too(self):
        flat = pack(GOOD).flatten("composite.fsh", {"sunPathRotation": "40.0"})
        self.assertIn("const float sunPathRotation = 40.0; // [-40.0 0.0 40.0]", flat.text)

    def test_expressions(self):
        env = {"A": 2, "B": 1}
        cases = {"A >= 2 && defined(B)": True, "defined C || !A": False, "(A + 1) * 2 == 6": True, "C": False,
                 "defined A && A % 2 == 0": True, "0x10 > 15": True}
        for text, expected in cases.items():
            self.assertEqual(bool(_Expression(text, env).value()), expected, text)
        with self.assertRaises(ValueError):
            _Expression("A >", env).value()


class Profiles(unittest.TestCase):
    def test_pack_profiles_options_and_macros(self):
        model = pack(GOOD)
        spec = profiles.parse({"defines": ["MC_VERSION 11202"], "pack_profiles": True,
                               "profile": {"lean": {"options": {"FOG": True}, "defines": ["LEAN=1"], "exclude": ["comp*"]}}})
        chosen = profiles.resolve(model, spec)
        self.assertEqual([p.name for p in chosen], ["lean", "LOW", "HIGH"])
        lean, low, high = chosen
        self.assertEqual(lean.values, {"FOG": True})
        self.assertEqual(lean.defines, [("MC_VERSION", "11202"), ("LEAN", "1")])
        self.assertEqual(low.values, {"SHADOWS": False, "QUALITY": "1"})
        self.assertEqual(high.values, {"SHADOWS": True, "QUALITY": "3"})
        self.assertFalse(model.selected("composite", lean.only, lean.exclude))
        self.assertTrue(model.selected("gbuffers_basic", lean.only, lean.exclude))

    def test_what_does_not_fit_the_pack_is_refused(self):
        model = pack(GOOD)
        for data, words in (({"profile": {"x": {"options": {"NOPE": 1}}}}, "does not declare"),
                            ({"profile": {"x": {"options": {"QUALITY": 9}}}}, "not one of"),
                            ({"profile": {"x": {"options": {"SHADOWS": "yes"}}}}, "use true or false"),
                            ({"profile": {"x": {"pack_profile": "ULTRA"}}}, "no profile.ULTRA"),
                            ({"profile": {"x": {"bogus": 1}}}, "unknown keys"),
                            ({"defines": ["9BAD"]}, "not a macro")):
            with self.assertRaises(ValueError) as caught:
                profiles.resolve(model, profiles.parse(data))
            self.assertIn(words, str(caught.exception))


class Hazards(unittest.TestCase):
    SOURCE = """\
        int counter;
        void ok(out int n) { for (int i = 0; i < 4; i++) { n = i; } }
        void borrowed() { for (counter = 0; counter < 4; counter++) { } }
        bool lookup(out int layer) {
            for (layer = 0; layer < 4; layer++) {   // borrowed too
                if (layer == 2) { return true; }
            }
            return false;
        }
        bool found(inout float best, float x) {
            for (int i = 0; i < 3; i++) {
                best = x;
                if (x > 1.0) return true;
            }
            return false;
        }
        void early(out vec3 colour, bool skip) {
            if (skip) return;
            colour = vec3(1.0);
        }
        void handed(out vec3 colour) { fill(colour); return; }
        /* for (k = 0; k < 2; k++) is a comment */
        """

    def test_the_three_shapes(self):
        found = hazards.lint({"lib/x.glsl": textwrap.dedent(self.SOURCE)})
        kinds = [(h.kind, h.function, h.line) for h in found]
        self.assertIn(("borrowed-counter", "", 3), kinds)
        self.assertIn(("borrowed-counter", "", 5), kinds)  # lookup's out parameter as its loop counter
        self.assertNotIn(("out-in-loop", "lookup", 4), kinds)  # the header is not the loop's body
        self.assertIn(("out-in-loop", "found", 10), kinds)
        self.assertIn(("out-unwritten", "early", 17), kinds)
        self.assertNotIn("handed", [h.function for h in found])
        self.assertNotIn("ok", [h.function for h in found])
        self.assertEqual(len([h for h in found if h.kind == "borrowed-counter"]), 2)

    def test_reviewed_shapes_are_marked(self):
        found = hazards.lint({"lib/x.glsl": textwrap.dedent(self.SOURCE)}, reviewed=["x.glsl:found", "lib/x.glsl:early"])
        reviewed = {h.function for h in found if h.reviewed}
        self.assertEqual(reviewed, {"found", "early"})


LOG = """\
[12:00:00] [main/INFO]: Loading
[12:00:01] [Render thread/INFO]: [Shaders] Using shaderpack: Tiny.zip
[12:00:01] [Render thread/INFO]: Profile: HIGH (+0 options changed by user)
[12:00:02] [Render thread/INFO]: Total shaderpack load time for 'Tiny.zip' in dimension 'overworld': 120.5 ms
[12:00:03] [Render thread/ERROR]: Some mod complained
[12:00:04] [Client thread/INFO]: [CHAT] Saved screenshot as 2026-11-21_12.00.04.png
"""


class Verdict(unittest.TestCase):
    def setUp(self):
        self.rules = verdict.rules("optifine", {})

    def run_log(self, text, **kw):
        return verdict.evaluate(text.splitlines(), self.rules, **kw)

    def test_a_loaded_pack(self):
        result = self.run_log(LOG, pack="Tiny", profile="HIGH", dimensions=["overworld"], min_captures=1)
        self.assertTrue(result["passed"], result["reasons"])
        self.assertEqual(len(result["other_errors"]), 1)
        self.assertEqual(result["loads"][0]["milliseconds"], 120.5)

    def test_fatal_lines_refuse_the_run(self):
        for line, kind in (("[Shaders] Error compiling fragment shader: /shaders/composite.fsh", "compile failure"),
                           ("ERROR: 0:12: 'x' : undeclared identifier", "compile failure"),
                           ("Falling back to vanilla rendering", "fallback"),
                           ("Failed to load shaderpack Tiny.zip", "load failure"),
                           ("Unable to resolve shader pack option menu element FOO", "missing option"),
                           ("Failed to process ConstDirective shadowMapResolution", "directive failure")):
            result = self.run_log(LOG + line + "\n", pack="Tiny.zip")
            self.assertFalse(result["passed"], line)
            self.assertIn(kind, result["reasons"][0])

    def test_wrong_pack_profile_dimension_and_early_captures(self):
        log = ("[INFO]: [CHAT] Saved screenshot as early.png\n" + LOG +
               "[INFO]: [Shaders] Using shaderpack: Other.zip\n")
        result = self.run_log(log, pack="Tiny.zip", profile="LOW", dimensions=["overworld", "the_nether"], min_captures=3)
        reasons = " | ".join(result["reasons"])
        self.assertIn("last active shader pack is 'Other.zip'", reasons)
        self.assertIn("never selects the profile 'LOW' (it selects HIGH)", reasons)
        self.assertIn("no completed load of the pack in the_nether", reasons)
        self.assertIn("2 capture(s), fewer than 3", reasons)
        self.assertIn("1 capture(s) precede any load of the pack, first early.png", reasons)
        self.assertIn("never names 'Absent' as the active", " ".join(self.run_log(LOG, pack="Absent")["reasons"]))

    def test_a_configured_loader_and_a_patterns_file_lay_over_the_defaults(self):
        configured = {"plain": {"markers": {"using": r"Activated pack (?P<pack>\S+)", "loaded": ""}}}
        rules = verdict.rules("plain", configured, {"fatal": {"fallback": ["gave up"]}, "ignore": ["harmless"]})
        ok = verdict.evaluate(["Activated pack Tiny", "a harmless ERROR: 0:1: x"], rules, pack="Tiny")
        self.assertTrue(ok["passed"], ok["reasons"])
        refused = verdict.evaluate(["Activated pack Tiny", "the loader gave up"], rules, pack="Tiny")
        self.assertIn("fallback", refused["reasons"][0])
        for bad, words in (({"markers": {"using": "no group"}}, "named group"), ({"nope": 1}, "unknown keys"),
                           ({"fatal": {"x": ["("]}}, "does not compile")):
            with self.assertRaises(ValueError) as caught:
                verdict.rules("optifine", {}, bad)
            self.assertIn(words, str(caught.exception))
        with self.assertRaises(ValueError):
            verdict.rules("unknown", {})

    def test_gzip_logs_and_capture_files(self):
        folder = Path(tempfile.mkdtemp(prefix="wb-verdict-"))
        with gzip.open(folder / "latest.log.gz", "wt", encoding="utf-8") as handle:
            handle.write(LOG)
        result = verdict.evaluate(verdict.read_lines(folder / "latest.log.gz"), self.rules, pack="Tiny.zip")
        self.assertTrue(result["passed"], result["reasons"])
        shots = folder / "shots"
        shots.mkdir()
        self.assertEqual(verdict.check_captures(result, shots), ["capture 2026-11-21_12.00.04.png is not in the "
                                                                  "screenshots folder"])
        (shots / "2026-11-21_12.00.04.png").write_bytes(png())
        self.assertEqual(verdict.check_captures(result, shots), [])
        self.assertEqual(result["captures"][0]["width"], 2)
        (shots / "2026-11-21_12.00.04.png").write_bytes(png()[:-6] + b"XXXXXX")
        self.assertIn("not a well-formed PNG", verdict.check_captures(result, shots)[0])
        unsafe = {"captures": [{"file": "../escape.png", "line": 1}]}
        self.assertIn("unsafe", verdict.check_captures(unsafe, shots)[0])


REPORT = {"schema": "wb/gl-probe/v1", "machine": "local", "os": "linux", "vendor": "NVIDIA Corporation",
          "renderer": "NVIDIA GeForce RTX 3060/PCIe/SSE2", "version": "4.6.0 NVIDIA 580.95.05", "profile": "compatibility",
          "memory_mib": 12288, "extensions": ["GL_ARB_bindless_texture", "GL_NV_mesh_shader", "GL_EXT_memory_object_fd",
                                              "GL_KHR_debug"], "limits": {"GL_MAX_TEXTURE_SIZE": "32768"}}
GLXINFO = """\
PROBE glxB name of display: :1
PROBE glxB Memory info (GL_NVX_gpu_memory_info):
PROBE glxB     Dedicated video memory: 8192 MB
PROBE glxB OpenGL vendor string: NVIDIA Corporation
PROBE glxB OpenGL renderer string: NVIDIA GeForce RTX 2060/PCIe/SSE2
PROBE glxB OpenGL core profile version string: 4.6.0 NVIDIA 570.10
PROBE glxB OpenGL core profile profile mask: core profile
PROBE glxB OpenGL version string: 4.6.0 NVIDIA 570.10
PROBE glxB OpenGL profile mask: compatibility profile
PROBE glx OpenGL extensions:
PROBE glx     GL_ARB_bindless_texture, GL_EXT_mesh_shader,
PROBE glx     GL_KHR_debug
PROBE glx OpenGL ES profile version string: OpenGL ES 3.2 NVIDIA 570.10
PROBE glx OpenGL ES profile extensions:
PROBE glx     GL_OES_only_on_es
PROBE glx     GL_MAX_TEXTURE_SIZE = 16384
"""


class Gate(unittest.TestCase):
    GATE = {"min_version": "4.6", "profile": "compatibility", "vendors": ["nvidia"], "min_memory_mib": 8192,
            "extensions": ["GL_NV_mesh_shader|GL_EXT_mesh_shader", "GL_ARB_bindless_texture", "GL_EXT_memory_object_win32"]}

    def test_a_card_that_passes_and_one_that_does_not(self):
        checks = gl.judge(REPORT, self.GATE)
        self.assertTrue(all(c["passed"] for c in checks), checks)
        self.assertNotIn("GL_EXT_memory_object_win32", " ".join(c["check"] for c in checks))  # an OS-specific one
        weak = dict(REPORT, version="4.5.0 Mesa", memory_mib=4096, vendor="AMD", extensions=["GL_KHR_debug"])
        failed = [c["check"] for c in gl.judge(weak, self.GATE) if not c["passed"]]
        self.assertEqual(len(failed), 5, failed)
        es = dict(REPORT, version="OpenGL ES 3.2 NVIDIA")
        self.assertFalse(gl.judge(es, {"min_version": "3.0"})[0]["passed"])

    def test_glxinfo_is_read_into_a_report(self):
        path = Path(tempfile.mkdtemp(prefix="wb-gl-")) / "probe.txt"
        path.write_text(GLXINFO, encoding="utf-8")
        report = gl.read_report(path)
        self.assertEqual(report["renderer"], "NVIDIA GeForce RTX 2060/PCIe/SSE2")
        self.assertEqual((report["version"], report["profile"], report["memory_mib"]),
                         ("4.6.0 NVIDIA 570.10", "compatibility", 8192))
        self.assertEqual(report["core_version"], "4.6.0 NVIDIA 570.10")
        self.assertEqual(report["extensions"], ["GL_ARB_bindless_texture", "GL_EXT_mesh_shader", "GL_KHR_debug"])
        self.assertEqual(report["limits"], {"GL_MAX_TEXTURE_SIZE": "16384"})
        self.assertTrue(all(c["passed"] for c in gl.judge(report, self.GATE)))

    def test_reference_differences(self):
        here = dict(REPORT, extensions=["GL_ARB_bindless_texture", "GL_ARB_extra"], limits={"GL_MAX_TEXTURE_SIZE": "16384"})
        diff = gl.compare(here, REPORT, self.GATE)
        self.assertEqual(diff["named_missing"], ["GL_NV_mesh_shader"])
        self.assertEqual(diff["other_missing"], ["GL_KHR_debug"])  # the _fd handle is OS-specific
        self.assertEqual(diff["other_extra"], ["GL_ARB_extra"])
        self.assertEqual(diff["limits_differ"], {"GL_MAX_TEXTURE_SIZE": {"reference": "32768", "here": "16384"}})

    def test_bad_gates_and_reports(self):
        for gate in ({"min_version": "four"}, {"profile": "es"}, {"vendors": "NVIDIA"}, {"colour": 1}):
            with self.assertRaises(ValueError):
                gl.check_gate(gate, "test")
        path = Path(tempfile.mkdtemp(prefix="wb-gl-")) / "r.json"
        path.write_text(json.dumps({"schema": "other/v9", "version": "4.6"}), encoding="utf-8")
        with self.assertRaises(ValueError):
            gl.read_report(path)


class Commands(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = Daemon("""
            [modules.render.gates.rental]
            min_version = "4.6"
            profile = "compatibility"
            vendors = ["NVIDIA"]
            min_memory_mib = 8192
            [modules.render.loaders.plain.markers]
            using = 'Activated pack (?P<pack>\\S+)'
            """).start()
        cls.c = cls.d.client
        cls.good = write(cls.d.data / "good", GOOD)
        cls.broken = write(cls.d.data / "broken", {**GOOD, "shaders/lib/common.glsl": GOOD["shaders/lib/common.glsl"]
                                                   + "vec3 broken() { return undefined_thing; }\n"})
        cls.zip = cls.d.data / "good.zip"
        with zipfile.ZipFile(cls.zip, "w") as archive:
            for path in (cls.good / "shaders").rglob("*"):
                if path.is_file():
                    archive.write(path, "Good/shaders/" + path.relative_to(cls.good / "shaders").as_posix())
        (cls.d.data / "profiles.toml").write_text(
            'defines = ["MC_VERSION 11202"]\npack_profiles = true\n[profile.plain]\n', encoding="utf-8")
        (cls.d.data / "bad-profiles.toml").write_text('[profile.x]\noptions = { NOPE = true }\n', encoding="utf-8")
        (cls.d.data / "latest.log").write_text(LOG, encoding="utf-8")
        (cls.d.data / "probe.json").write_text(json.dumps(REPORT), encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.d.stop()

    def test_commands_are_registered(self):
        names = {c["name"]: c for c in self.c.commands()}
        for name in ("render.validate", "render.load-verdict", "render.gl-gate"):
            self.assertIn(name, names)
        self.assertEqual(names["render.validate"]["active"], GLSLANG is not None)

    @unittest.skipUnless(GLSLANG, "glslangValidator is not on PATH")
    def test_validate_passes_a_sound_pack_under_every_profile(self):
        result = self.c.submit("render.validate", {"pack": str(self.good), "profiles": str(self.d.data / "profiles.toml"),
                                                   "required": ["gbuffers_basic"]},
                               out=str(self.d.out / "validate-good")).result(timeout=300)
        self.assertEqual(result.outcome, "ok", result.summary)
        self.assertEqual([p["profile"] for p in result.data.profiles], ["plain", "LOW", "HIGH"])
        self.assertTrue(all(p["compiled"] == 4 and p["linked"] == 2 for p in result.data.profiles), result.data.profiles)
        flat = (self.d.out / "validate-good" / "flat" / "LOW" / "composite.frag").read_text(encoding="utf-8")
        self.assertIn("#define MC_VERSION 11202", flat)
        self.assertIn("//#define SHADOWS", flat)
        report = json.loads((self.d.out / "validate-good" / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(len(report["stages"]), 12)
        zipped = self.c.submit("render.validate", {"pack": str(self.zip)}).result(timeout=300)
        self.assertEqual(zipped.outcome, "ok", zipped.summary)

    @unittest.skipUnless(GLSLANG, "glslangValidator is not on PATH")
    def test_validate_names_the_source_line_of_a_compile_error(self):
        result = self.c.submit("render.validate", {"pack": str(self.broken)}).result(timeout=300)
        self.assertEqual((result.outcome, result.code), ("finding", 1), result.summary)
        compile_findings = [f for f in result["findings"] if "not compile" in f]
        self.assertEqual(len(compile_findings), 1, result["findings"])  # one cause, every stage that shows it
        self.assertIn("composite.fsh, composite.vsh, gbuffers_basic.fsh and 1 more do not compile: "
                      "lib/common.glsl:11: 'undefined_thing'", compile_findings[0])
        self.assertEqual(result.data.profiles[0]["compiled"], 0)
        self.assertFalse(result.data.passed)

    @unittest.skipUnless(GLSLANG, "glslangValidator is not on PATH")
    def test_validate_names_link_failures_and_the_profiles_a_failure_shows_in(self):
        mixed = write(self.d.data / "mixed", {
            **GOOD,
            "shaders/composite.vsh": VERTEX.replace("out vec4 color;", "out vec2 color;")
                                           .replace("gl_Color * vec4(tint(), 1.0)", "gl_Color.xy"),
            "shaders/gbuffers_basic.fsh": FRAGMENT.replace("void main() {", "#if QUALITY == 3\nbroken here\n#endif\n"
                                                                            "void main() {")})
        result = self.c.submit("render.validate", {"pack": str(mixed), "profiles": str(self.d.data / "profiles.toml")}
                               ).result(timeout=300)
        self.assertEqual(result.outcome, "finding", result.summary)
        findings = " | ".join(result["findings"])
        link = next(f for f in result["findings"] if "does not link" in f)
        self.assertIn("program composite does not link (profile plain, LOW, HIGH): ", link)
        self.assertIn("color", link)  # the detail names the mismatched variable (each glslang spells its type its own way)
        self.assertIn("gbuffers_basic.fsh does not compile (profile HIGH): gbuffers_basic.fsh:5:", findings)
        high = next(p for p in result.data.profiles if p["profile"] == "HIGH")
        self.assertEqual(sorted(high["failed"]), ["composite", "gbuffers_basic.fsh"])

    @unittest.skipUnless(GLSLANG, "glslangValidator is not on PATH")
    def test_validate_refuses_profiles_that_do_not_fit_the_pack(self):
        result = self.c.submit("render.validate", {"pack": str(self.good),
                                                   "profiles": str(self.d.data / "bad-profiles.toml")}).result(timeout=120)
        self.assertEqual(result.outcome, "usage", result.summary)
        self.assertIn("NOPE", result.summary)

    def test_load_verdict(self):
        ok = self.c.submit("render.load-verdict", {"log": str(self.d.data / "latest.log"), "pack": "Tiny.zip",
                                                   "profile": "HIGH", "min_captures": 1}).result(timeout=120)
        self.assertEqual(ok.outcome, "ok", ok.summary)
        self.assertTrue(ok.data.passed)
        refused = self.c.submit("render.load-verdict", {"log": str(self.d.data / "latest.log"), "pack": "Tiny.zip",
                                                        "loader": "plain"}).result(timeout=120)
        self.assertEqual(refused.outcome, "finding", refused.summary)
        self.assertIn("never names 'Tiny.zip'", refused.summary)
        unknown = self.c.submit("render.load-verdict", {"log": str(self.d.data / "latest.log"),
                                                        "loader": "nope"}).result(timeout=120)
        self.assertEqual(unknown.outcome, "usage")

    def test_gl_gate(self):
        ok = self.c.submit("render.gl-gate", {"report": str(self.d.data / "probe.json"), "gate": "rental",
                                              "extensions": ["GL_NV_mesh_shader|GL_EXT_mesh_shader"]}).result(timeout=120)
        self.assertEqual(ok.outcome, "ok", ok.summary)
        self.assertTrue(ok.data.accepted)
        refused = self.c.submit("render.gl-gate", {"report": str(self.d.data / "probe.json"), "gate": "rental",
                                                   "min_memory_mib": 16384}).result(timeout=120)
        self.assertEqual(refused.outcome, "finding")
        self.assertIn("16,384 MiB or more", refused.summary)
        nothing = self.c.submit("render.gl-gate", {"report": str(self.d.data / "probe.json")}).result(timeout=120)
        self.assertEqual(nothing.outcome, "usage")


FAKE_GRADLE = r'''
import json, pathlib, sys
fail = "--fail" in sys.argv
results = pathlib.Path("build/test-results/glTest")
results.mkdir(parents=True, exist_ok=True)
cases = '<testcase classname="gl.ReadbackTest" name="rows_are_bottom_up"/>'
if fail:
    cases += ('<testcase classname="gl.StateTest" name="restores_read_buffer">'
              '<failure message="read buffer was GL_BACK, expected GL_FRONT">trace</failure></testcase>')
(results / "TEST-gl.xml").write_text(
    f'<testsuite name="gl" tests="{2 if fail else 1}" failures="{1 if fail else 0}" errors="0" skipped="0">{cases}</testsuite>')
if "--no-report" not in sys.argv:
    report = pathlib.Path(".local/verification/gl-1")
    report.mkdir(parents=True, exist_ok=True)
    (report / "capabilities.json").write_text(json.dumps({
        "vendor": "NVIDIA Corporation", "renderer": "Test GPU", "version": "4.6.0 NVIDIA 610.88",
        "profile": "compatibility", "extensions": ["GL_ARB_direct_state_access"]}))
raise SystemExit(1 if fail else 0)
'''


class GlRun(unittest.TestCase):
    """render.gl-run with a fake build: results read, failing tests named, the capability report judged."""

    def setUp(self):
        from wb_render import glrun
        self.glrun = glrun
        self.root = Path(tempfile.mkdtemp(prefix="wb-glrun-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        (self.root / "checkout").mkdir()
        (self.root / "fake_gradle.py").write_text(FAKE_GRADLE, encoding="utf-8")
        self.gates = {"dsa": {"min_version": "4.5", "extensions": ["GL_ARB_direct_state_access"]},
                      "mesh": {"min_version": "4.6", "extensions": ["GL_NV_mesh_shader|GL_EXT_mesh_shader"]}}

    def run_gl(self, name, *extra, gate="dsa"):
        from wb_core.module import Context
        ctx = Context({"job": "direct", "command": "render.gl-run", "module": "render",
                       "work": str(self.root / name / "work"), "out": str(self.root / name / "out"),
                       "module_config": {"gates": self.gates}}, lambda event: None)
        result = self.glrun.gl_run(ctx, str(self.root / "checkout"), "glTest", "",
                                   [sys.executable, str(self.root / "fake_gradle.py")], list(extra),
                                   ["**/build/test-results/**/*.xml"], ".local/verification/gl-*/capabilities.json",
                                   gate, 60, "")
        return ctx, result

    def test_a_passing_run_with_a_card_the_gate_accepts(self):
        ctx, result = self.run_gl("ok")
        self.assertEqual(ctx.outcome, "ok", ctx.findings)
        self.assertEqual((result["tests"], result["failures"]), (1, 0))
        self.assertEqual(result["card"]["renderer"], "Test GPU")
        self.assertTrue((self.root / "ok" / "out" / "capabilities.json").is_file())

    def test_a_failing_test_is_named(self):
        ctx, result = self.run_gl("fail", "--fail")
        self.assertIn("failed: gl.StateTest.restores_read_buffer: read buffer was GL_BACK, expected GL_FRONT",
                      ctx.findings)

    def test_a_card_the_gate_refuses_and_a_missing_report_are_findings(self):
        ctx, _ = self.run_gl("mesh", gate="mesh")
        self.assertTrue(any(f.startswith("gate mesh: not ") for f in ctx.findings), ctx.findings)
        ctx, _ = self.run_gl("noreport", "--no-report")
        self.assertTrue(any("wrote no capability report" in f for f in ctx.findings), ctx.findings)


if __name__ == "__main__":
    unittest.main()
