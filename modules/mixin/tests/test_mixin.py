"""The mixin module: mapping files, mixin.remap-proof over production and development-named JARs built in
the test, mixin.apply against a small harness that follows the documented contract, and mixin.doctor over
JARs of hand-built classes (and a javac-built one where a JDK is found) under loader policies."""

import hashlib
import json
import shutil
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
JAR_TESTS = REPO / "modules" / "jar" / "tests"
for extra in (REPO / "tests", JAR_TESTS, REPO / "modules" / "jar", HERE.parent):
    sys.path.insert(0, str(extra))

import jvmkit  # noqa: E402
from classwriter import INVOKESTATIC, RETURN, Ann, ClassWriter, Cls  # noqa: E402
from harness import Daemon  # noqa: E402

from wb_mixin import policy as policies  # noqa: E402
from wb_mixin.mappings import MappingError, parse  # noqa: E402

JDKS = jvmkit.find_jdks()
JAVAC = jvmkit.newest(JDKS)
jvmkit.export(JDKS)

MIXIN = "Lorg/spongepowered/asm/mixin/Mixin;"
SHADOW = "Lorg/spongepowered/asm/mixin/Shadow;"
OVERWRITE = "Lorg/spongepowered/asm/mixin/Overwrite;"
INJECT = "Lorg/spongepowered/asm/mixin/injection/Inject;"
REDIRECT = "Lorg/spongepowered/asm/mixin/injection/Redirect;"
AT = "Lorg/spongepowered/asm/mixin/injection/At;"
ENTITY, RULES, MATH, WORLD = ("net/minecraft/entity/Entity", "net/minecraft/util/CombatRules",
                              "net/minecraft/util/math/MathHelper", "net/minecraft/world/World")
CALLBACK = "(Lorg/spongepowered/asm/mixin/injection/callback/CallbackInfo;)V"
ABSORB, ABSORB_SRG = f"L{RULES};getDamageAfterAbsorb(FFF)F", f"L{RULES};func_189427_a(FFF)F"
CLAMP, CLAMP_SRG = f"L{MATH};clamp(FFF)F", f"L{MATH};func_76131_a(FFF)F"

# The game's development -> production names (TSRG) and the annotation processor's mapping of the
# Mixins' own shadow and overwrite members (CSRG lines, as it writes them).
GAME = f"""{ENTITY} {ENTITY}
\tworld field_70170_p
\tgetEntityWorld ()L{WORLD}; func_130014_f_
\tonUpdate ()V func_70071_h_
\tonEntityUpdate ()V func_70030_z
{RULES} {RULES}
\tgetDamageAfterAbsorb (FFF)F func_189427_a
{MATH} {MATH}
\tclamp (FFF)F func_76131_a
"""
# The same game names the way a build cache keeps them: production -> development.
GAME_BACKWARDS = f"""{ENTITY} {ENTITY}
\tfield_70170_p world
\tfunc_130014_f_ ()L{WORLD}; getEntityWorld
\tfunc_70071_h_ ()V onUpdate
\tfunc_70030_z ()V onEntityUpdate
{RULES} {RULES}
\tfunc_189427_a (FFF)F getDamageAfterAbsorb
{MATH} {MATH}
\tfunc_76131_a (FFF)F clamp
"""
MEMBERS = f"""com/example/mixins/MixinEntity world field_70170_p
com/example/mixins/MixinEntity getEntityWorld ()L{WORLD}; func_130014_f_
com/example/mixins/MixinEntity onEntityUpdate ()V func_70030_z
"""


def mod_jar(path: Path, *, production: bool, refmap: dict | None, native_entry: bool = False) -> Path:
    """A mod with two Mixins, named as a production (reobfuscated) build names them, or not."""
    world, get_world, update = (("field_70170_p", "func_130014_f_", "func_70030_z") if production
                                else ("world", "getEntityWorld", "onEntityUpdate"))
    entity = ClassWriter("com/example/mixins/MixinEntity").annotate(Ann(MIXIN, {"value": [Cls(f"L{ENTITY};")]}))
    entity.field(world, f"L{WORLD};", annotations=[Ann(SHADOW)])
    entity.method(get_world, f"()L{WORLD};", code=None, access=0x0401, annotations=[Ann(SHADOW)])
    entity.method(update, "()V", annotations=[Ann(OVERWRITE)])
    body = entity.get_field("com/example/mixins/MixinEntity", world, f"L{WORLD};") + b"\x57" + RETURN
    entity.method("onTick", CALLBACK, code=body, annotations=[
        Ann(INJECT, {"method": ["onUpdate"], "at": [Ann(AT, {"value": "HEAD"}, visible=True)]}, visible=True)])
    rules = ClassWriter("com/example/mixins/MixinRules").annotate(Ann(MIXIN, {"value": [Cls(f"L{RULES};")]}))
    rules.method("onArmor", CALLBACK, annotations=[
        Ann(INJECT, {"method": ["applyArmor"], "at": [Ann(AT, {"value": "HEAD"}, visible=True)]}, visible=True)])
    rules.method("clampLess", "(FFF)F", annotations=[
        Ann(REDIRECT, {"method": ["getDamageAfterAbsorb"], "at": Ann(AT, {"value": "INVOKE", "target": CLAMP},
                                                                       visible=True)}, visible=True)])
    util = ClassWriter("com/example/Util")
    util.method("clamp", "()V", code=util.invoke(INVOKESTATIC, MATH, "func_76131_a" if production else "clamp",
                                                 "(FFF)F") + RETURN)
    config = {"package": "com.example.mixins", "mixins": ["MixinEntity", "MixinRules"]}
    entries = {"com/example/mixins/MixinEntity.class": entity.build(), "com/example/mixins/MixinRules.class": rules.build(),
               "com/example/Util.class": util.build()}
    if refmap is not None:
        config["refmap"] = "example.refmap.json"
        table = dict(refmap)
        if native_entry:
            table["com/example/mixins/MixinRules"] = dict(table.get("com/example/mixins/MixinRules", {}),
                                                          applyArmor=f"L{RULES};func_1_x(F)F")
        entries["example.refmap.json"] = json.dumps({"mappings": table})
    entries["example.mixins.json"] = json.dumps(config)
    return jvmkit.make_jar(path, entries=entries)


GOOD_REFMAP = {"com/example/mixins/MixinEntity": {"onUpdate": f"L{ENTITY};func_70071_h_()V"},
               "com/example/mixins/MixinRules": {"getDamageAfterAbsorb": ABSORB_SRG, CLAMP: CLAMP_SRG}}
STALE_REFMAP = {"com/example/mixins/MixinEntity": {"onUpdate": f"L{ENTITY};func_70071_h_()V"},
                "com/example/mixins/MixinRules": {"getDamageAfterAbsorb": ABSORB, CLAMP: CLAMP}}


class Mappings(unittest.TestCase):
    def check(self, text: str):
        mappings = parse(text)
        self.assertEqual(mappings.fields, {(WORLD, "rand"): "field_73012_v"})
        self.assertEqual(mappings.methods, {(WORLD, "tick", "()V"): "func_72835_b"})
        return mappings

    def test_formats(self):
        self.check(f"PK: . net/minecraft/src\nCL: {WORLD} {WORLD}\nFD: {WORLD}/rand {WORLD}/field_73012_v\n"
                   f"MD: {WORLD}/tick ()V {WORLD}/func_72835_b ()V\n")
        self.check(f"{WORLD} {WORLD}\n\trand field_73012_v\n\ttick ()V func_72835_b\n")
        self.check(f"tsrg2 left right\n{WORLD} {WORLD}\n\trand Ljava/util/Random; field_73012_v\n"
                   f"\ttick ()V func_72835_b\n\t\tstatic\n\t\t0 p p_1\n")
        self.check(f"# member mappings\n{WORLD} rand field_73012_v\n{WORLD} tick ()V func_72835_b\n")

    def test_reversed(self):
        backwards = parse(f"{WORLD} {WORLD}\n\tfield_73012_v rand\n\tfunc_72835_b ()V tick\n"
                          "\tequals (Ljava/lang/Object;)Z equals\n")
        self.check_reversed(backwards.reversed())
        moved = parse("b/Prod a/Dev\nb/Prod n (Lb/Prod;)V m\n").reversed()
        self.assertEqual(moved.classes, {"a/Dev": "b/Prod"})
        self.assertEqual(moved.methods, {("a/Dev", "m", "(La/Dev;)V"): "n"})

    def check_reversed(self, mappings):
        self.assertEqual(mappings.fields, {(WORLD, "rand"): "field_73012_v"})
        self.assertEqual(mappings.methods, {(WORLD, "tick", "()V"): "func_72835_b",
                                            (WORLD, "equals", "(Ljava/lang/Object;)Z"): "equals"})

    def test_classes_translate_descriptors(self):
        mappings = parse("a/Dev b/Prod\na/Dev f g\na/Dev m (La/Dev;I)La/Dev; n\n")
        self.assertEqual(mappings.owner("a/Dev"), "b/Prod")
        self.assertEqual(mappings.descriptor("(La/Dev;[La/Other;)La/Dev;"), "(Lb/Prod;[La/Other;)Lb/Prod;")
        with self.assertRaises(MappingError):
            parse("a/B c d e f\n")


class RemapProof(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = Daemon().start()
        cls.c = cls.d.client
        data = cls.d.data
        (data / "game.tsrg").write_text(GAME, encoding="utf-8")
        (data / "members.tsrg").write_text(MEMBERS, encoding="utf-8")
        cls.mappings = [str(data / "members.tsrg"), str(data / "game.tsrg")]
        cls.good = mod_jar(data / "good.jar", production=True, refmap=GOOD_REFMAP)
        cls.dev = mod_jar(data / "dev.jar", production=False, refmap=STALE_REFMAP, native_entry=True)
        cls.lost = mod_jar(data / "lost.jar", production=True, refmap=GOOD_REFMAP)
        shutil.copy(cls.lost, data / "lost.zip")
        import zipfile
        with zipfile.ZipFile(data / "lost.zip") as source, zipfile.ZipFile(cls.lost, "w") as target:
            for item in source.infolist():
                if item.filename != "example.refmap.json":
                    target.writestr(item, source.read(item))

    @classmethod
    def tearDownClass(cls):
        cls.d.stop()

    def prove(self, jar, **params):
        return self.c.submit("mixin.remap-proof", {"jar": str(jar), **params}).result(timeout=180)

    def test_production_jar_is_proved(self):
        expect = {"com/example/mixins/MixinRules": {"getDamageAfterAbsorb": ABSORB_SRG, "applyArmor": ""}}
        result = self.prove(self.good, mappings=self.mappings, expect=expect, native=["applyArmor"],
                            owners=["com/example/mixins/"])
        self.assertEqual(result.outcome, "ok", result.summary)
        self.assertTrue(result.data.proved)
        checked = result.data.checked
        self.assertEqual((checked["refmap"], checked["members"], checked["shadows"], checked["expect"]), (3, 3, 3, 2))
        self.assertEqual(result.data.refmaps, ["example.refmap.json"])
        report = json.loads(Path(result.data.report).read_text(encoding="utf-8"))
        self.assertEqual(len(report["inputs"]), 3)
        alone = self.prove(self.good)  # the refmap's own pairs still prove the shadows and references
        self.assertEqual(alone.outcome, "ok", alone.summary)
        self.assertEqual(alone.data.checked["refmap"], 3)

    def test_development_names_are_found(self):
        result = self.prove(self.dev, mappings=self.mappings, native=["applyArmor"],
                            expect={"com/example/mixins/MixinRules": {"getDamageAfterAbsorb": ABSORB_SRG}})
        self.assertEqual((result.outcome, result.code), ("finding", 1))
        self.assertFalse(result.data.proved)
        counts = result.data.failure_counts
        self.assertEqual(counts["member"], 3)      # world, getEntityWorld, onEntityUpdate kept their names
        self.assertEqual(counts["shadow"], 3)      # and so shadow and overwrite Entity's development names
        self.assertEqual(counts["native"], 1)      # applyArmor has an entry
        self.assertEqual(counts["expect"], 1)
        self.assertEqual(counts["refmap"], 2)      # both MixinRules entries map to development names
        self.assertEqual(counts["reference"], 2)   # MixinEntity.world and MathHelper.clamp, by development names
        details = [f"{f['check']} {f['where']} {f['detail']}" for f in result.data.failures]
        self.assertIn("member com.example.mixins.MixinEntity#world still under its development name, not "
                      "field_70170_p", details)
        self.assertTrue(any(d.startswith("reference com.example.Util refers to net.minecraft.util.math.MathHelper.clamp")
                            for d in details), details)
        backwards = self.d.data / "srg2mcp.tsrg"  # the game's names as a build cache keeps them
        backwards.write_text(GAME_BACKWARDS, encoding="utf-8")
        reversed_game = self.prove(self.dev, mappings=[str(self.d.data / "members.tsrg")],
                                   reverse_mappings=[str(backwards)])
        self.assertEqual(reversed_game.data.failure_counts, {"member": 3, "shadow": 3, "refmap": 2, "reference": 2})

    def test_missing_refmap_and_nothing_to_prove(self):
        lost = self.prove(self.lost, mappings=self.mappings)
        self.assertEqual(lost.outcome, "finding")
        self.assertIn("refmap: example.refmap.json: the refmap is not packaged", lost["findings"])
        empty = jvmkit.make_jar(self.d.data / "plain.jar", entries={"a/B.class": ClassWriter("a/B").build()})
        nothing = self.prove(empty)
        self.assertEqual(nothing.outcome, "finding")
        self.assertIn("nothing to prove", nothing.summary)
        (self.d.data / "bad.tsrg").write_text("a b c d e\n", encoding="utf-8")
        bad = self.prove(self.good, mappings=[str(self.d.data / "bad.tsrg")])
        self.assertEqual(bad.outcome, "usage")


HARNESS = """
    package fake;
    import java.nio.file.*;
    import java.util.*;
    /** Follows the offline Mixin harness contract with canned rows, so the runner can be tested. */
    public class Harness {
        public static void main(String[] args) throws Exception {
            if (args.length == 0) usage("no command");
            if (args[0].equals("apply")) apply(args); else if (args[0].equals("compare")) compare(args);
            else usage("unknown command " + args[0]);
        }
        static void usage(String why) { System.err.println("usage: " + why); System.exit(2); }
        static void apply(String[] args) throws Exception {
            String out = null, config = null; boolean strict = false, production = false, server = false;
            List<String> entries = new ArrayList<>();
            for (int i = 1; i < args.length; i++) {
                switch (args[i]) {
                    case "--out": out = args[++i]; break;
                    case "--config": config = args[++i]; break;
                    case "--strict": strict = true; break;
                    case "--production": production = true; break;
                    case "--server": server = true; break;
                    default: if (args[i].startsWith("--")) usage("unknown option " + args[i]); entries.add(args[i]);
                }
            }
            if (out == null || config == null) usage("--out and --config are required");
            List<String> rows = new ArrayList<>();
            boolean failed = false;
            for (String name : config.split(",")) {
                if (name.contains("usage")) usage("cannot read " + name);
                rows.add("TARGET\\t" + name + "\\tcom.example.Target");
                rows.add("HOOK\\t" + name + "\\tonTick@MixinTarget\\ttick\\t12");
                if (name.contains("broken")) { rows.add("FAIL\\t" + name + "\\tMixinBroken\\tno injection point"); failed = true; }
                if (name.contains("idle") && strict) { rows.add("FAIL\\t" + name + "\\tonIdle@MixinIdle\\thooks nothing"); failed = true; }
            }
            rows.add("NOTE\\tproduction=" + production + " server=" + server + " entries=" + entries.size());
            Files.write(Paths.get(out), rows);
            System.out.println("applied " + config);
            System.exit(failed ? 1 : 0);
        }
        static void compare(String[] args) throws Exception {
            List<String> files = new ArrayList<>();
            for (int i = 1; i < args.length; i++) {
                if (args[i].equals("--removed")) i++; else if (!args[i].startsWith("--")) files.add(args[i]);
            }
            Set<String> before = hooks(files.get(0)), after = hooks(files.get(1));
            for (String hook : before) if (!after.contains(hook)) System.out.println("LOST\\t" + hook);
            for (String hook : after) if (!before.contains(hook)) System.out.println("NEW\\t" + hook);
            System.exit(before.equals(after) ? 0 : 1);
        }
        static Set<String> hooks(String file) throws Exception {
            Set<String> found = new TreeSet<>();
            for (String row : Files.readAllLines(Paths.get(file))) if (row.startsWith("HOOK\\t")) found.add(row);
            return found;
        }
    }
"""


@unittest.skipUnless(JAVAC, "no JDK with javac on this machine")
class Apply(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = Daemon().start()
        cls.c = cls.d.client
        classes = jvmkit.compile_java(JAVAC, {"fake/Harness.java": HARNESS}, cls.d.root / "harness", release=8)
        cls.tool = jvmkit.make_jar(cls.d.data / "harness.jar", folders=[classes], manifest={"Main-Class": "fake.Harness"})
        cls.mods = jvmkit.make_jar(cls.d.data / "mods.jar", entries={"good.mixins.json": "{}"})
        cls.jdk = max(JDKS, key=int)

    @classmethod
    def tearDownClass(cls):
        cls.d.stop()

    def apply(self, configs, **params):
        params = {"tool": [str(self.tool)], "configs": configs, "classpath": [str(self.mods)], "jdk": self.jdk, **params}
        return self.c.submit("mixin.apply", params).result(timeout=180)

    def test_clean_failed_and_refused(self):
        clean = self.apply(["good.mixins.json"], production=True, side="server")
        self.assertEqual(clean.outcome, "ok", clean.summary)
        self.assertEqual((clean.data.counts["TARGET"], clean.data.counts["HOOK"], clean.data.counts["FAIL"]), (1, 1, 0))
        rows = (Path(clean.data.report).parent / "report.txt").read_text(encoding="utf-8")
        self.assertIn("NOTE\tproduction=true server=true entries=1", rows)
        broken = self.apply(["good.mixins.json", "broken.mixins.json"])
        self.assertEqual((broken.outcome, broken.code), ("finding", 1))
        self.assertEqual(broken.data.failures, ["FAIL\tbroken.mixins.json\tMixinBroken\tno injection point"])
        idle = self.apply(["idle.mixins.json"])
        self.assertEqual(idle.outcome, "finding")
        self.assertIn("hooks nothing", idle.summary)
        lenient = self.apply(["idle.mixins.json"], strict=False)
        self.assertEqual(lenient.outcome, "ok", lenient.summary)
        refused = self.apply(["usage.mixins.json"])
        self.assertEqual(refused.outcome, "usage")
        self.assertIn("cannot read usage.mixins.json", refused.summary)

    def test_baseline(self):
        first = self.apply(["good.mixins.json"])
        baseline = self.d.data / "baseline.txt"
        shutil.copy(Path(first.data.report).parent / "report.txt", baseline)
        same = self.apply(["good.mixins.json"], baseline=str(baseline))
        self.assertEqual(same.outcome, "ok", same.summary)
        self.assertIn("the same as the baseline", same.summary)
        moved = self.apply(["good.mixins.json", "more.mixins.json"], baseline=str(baseline), by_target=True)
        self.assertEqual(moved.outcome, "finding")
        self.assertIn("the hooks differ from the baseline: NEW", moved.summary)


# -- mixin.doctor -----------------------------------------------------------------------------------
PSEUDO = "Lorg/spongepowered/asm/mixin/Pseudo;"
SLICE = "Lorg/spongepowered/asm/mixin/injection/Slice;"
RENDERER = "net/minecraft/client/renderer/EntityRenderer"
LOADER = """
    [modules.mixin.loaders.example]
    compatibility_levels = ["JAVA_8..JAVA_25"]
    mixin_version = "0.8.7"
    max_class_version = 70
    required_fields = ["compatibilityLevel", "minVersion"]
    injection_namespaces = ["MIXINEXTRAS"]
    remapped_packages = ["net/minecraft/"]
"""


def mixin_class(name: str, *, targets=(), strings=(), points=("HEAD",), major: int = 52, remap: bool = True,
                pseudo: bool = False) -> bytes:
    """A Mixin with one injector per injection point, the first point also bounded by a @Slice."""
    values = {"value": [Cls(f"L{t};") for t in targets]} if targets else {}
    values |= {"targets": list(strings)} if strings else {}
    values |= {} if remap else {"remap": False}
    writer = ClassWriter(name, major=major).annotate(Ann(MIXIN, values))
    if pseudo:
        writer.annotate(Ann(PSEUDO))
    for index, point in enumerate(points):
        inject = {"method": [f"tick{index}"], "at": [Ann(AT, {"value": point}, visible=True)]}
        if index == 0:
            inject["slice"] = Ann(SLICE, {"from": Ann(AT, {"value": "HEAD"}, visible=True)}, visible=True)
        writer.method(f"on{index}", CALLBACK, annotations=[Ann(INJECT, inject, visible=True)])
    return writer.build()


def plain_class(name: str, major: int = 52, literal: str | None = None) -> bytes:
    writer = ClassWriter(name, major=major)
    if literal is not None:  # a loading plugin's list of configurations: ldc "name.json"
        writer.method("configs", "()V", code=bytes([0x12, writer.pool.string(literal), 0x57]) + RETURN)
    return writer.build()


def doctor_jar(path: Path, configs: dict, classes: dict, *, declared=(), extra: dict | None = None) -> Path:
    entries = {name: json.dumps(value) for name, value in configs.items()}
    entries |= {f"{name}.class": data for name, data in classes.items()} | (extra or {})
    manifest = {"MixinConfigs": ",".join(declared)} if declared else None
    return jvmkit.make_jar(path, entries=entries, manifest=manifest)


class Policies(unittest.TestCase):
    def test_levels_versions_and_names(self):
        table = policies.check({"compatibility_levels": ["JAVA_8..JAVA_11", "java_17"],
                                "mixin_version": "0.8.7+build.3", "remapped_packages": ["net.minecraft"],
                                "injection_namespaces": ["mixinextras"]}, "t")
        self.assertEqual(table["compatibility_levels"], {8, 9, 10, 11, 17})
        self.assertEqual(policies.span(table["compatibility_levels"]), "JAVA_8..JAVA_11, JAVA_17")
        self.assertEqual((table["remapped_packages"], table["injection_namespaces"]),
                         (("net/minecraft/",), {"MIXINEXTRAS"}))
        self.assertEqual(policies.version("0.8"), ((0, 8, 0, 0), None))
        self.assertEqual(policies.version("1.2.3.4-beta_1"), ((1, 2, 3, 4), None))
        self.assertEqual(policies.version("0.8.x"), (None, "malformed"))
        self.assertEqual(policies.version("0.40000"), (None, "overflow"))
        self.assertEqual([policies.level_of(t) for t in (" java_25 ", "JAVA25", "JAVA_08")], [25, None, None])
        self.assertEqual((policies.class_version(25), policies.java(70), policies.java(52), policies.java(48)),
                         (69, "Java 26", "Java 8", "Java 1.4"))

    def test_layers_and_refusals(self):
        layers = [("a", policies.check({"mixin_version": "0.8", "severity": {"coverage": "warning"}}, "a")),
                  ("b", policies.check({"mixin_version": "0.9", "severity": {"registration": "off"}}, "b"))]
        merged = policies.resolve(layers)
        self.assertEqual((merged.mixin_version, merged.level("coverage"), merged.level("registration"),
                          merged.level("parse"), merged.sources), ("0.9", "warning", "off", "error", ["a", "b"]))
        self.assertIsNone(merged.levels)  # not given: any level passes
        for bad in ({"colour": 1}, {"compatibility_levels": ["JAVA_X"]}, {"compatibility_levels": ["JAVA_25..JAVA_8"]},
                    {"compatibility_levels": []}, {"compatibility_levels": ["JAVA_08"]}, {"mixin_version": 0.8},
                    {"max_class_version": True}, {"max_class_version": 12}, {"severity": {"bogus": "error"}},
                    {"severity": {"parse": "loud"}},
                    {"required_may_target_missing": "no"}, {"injection_namespaces": ["NOT A NAME"]}):
            with self.assertRaises(ValueError, msg=bad):
                policies.check(bad, "test")


class Doctor(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = Daemon(LOADER).start()
        cls.c = cls.d.client
        data = cls.d.data
        cls.game = jvmkit.make_jar(data / "game.jar", entries={f"{n}.class": plain_class(n) for n in (WORLD, RENDERER)})
        good = {"required": True, "minVersion": "0.8", "package": "com.example.mixins", "refmap": "good.refmap.json",
                "compatibilityLevel": "JAVA_8", "plugin": "com.example.Plugin", "mixins": ["MixinWorld"],
                "client": ["MixinRenderer"], "injectors": {"defaultRequire": 1}}
        cls.good = doctor_jar(data / "good.jar", {"good.mixins.json": good}, {
            "com/example/mixins/MixinWorld": mixin_class("com/example/mixins/MixinWorld", targets=[WORLD],
                                                         points=["HEAD", "INVOKE:LAST"]),
            "com/example/mixins/MixinRenderer": mixin_class("com/example/mixins/MixinRenderer",
                                                            strings=[RENDERER.replace("/", ".")],
                                                            points=["MIXINEXTRAS:EXPRESSION"]),
            "com/example/Plugin": plain_class("com/example/Plugin")},
            declared=["good.mixins.json"],
            extra={"good.refmap.json": json.dumps({"mappings": {"com/example/mixins/MixinWorld": {
                       "tick0": f"L{WORLD};func_1()V"}}}), "not-a-config.json": json.dumps({"hello": 1})})
        newer = {"required": True, "minVersion": "0.8", "package": "com.example.next", "refmap": "next.refmap.json",
                 "compatibilityLevel": "JAVA_25", "mixins": ["MixinTick", "MixinChunk"]}
        cls.newer = doctor_jar(data / "newer.jar", {"next.mixins.json": newer}, {
            f"com/example/next/{n}": mixin_class(f"com/example/next/{n}", targets=[WORLD], major=70)
            for n in ("MixinTick", "MixinChunk")}, declared=["next.mixins.json"],
            extra={"next.refmap.json": json.dumps({"mappings": {}})})
        broken = {"required": "yes", "package": "com.example.broken", "compatibilityLevel": "JAVA_26",
                  "refmap": "missing.refmap.json", "plugin": "com.example.NoPlugin",
                  "mixins": ["MixinMissing", "com.example.Outside", "com.example.broken.MixinFull", "NotAMixin",
                             "MixinTwice", "MixinPoints"],
                  "client": ["MixinTwice"]}
        cls.broken = doctor_jar(data / "broken.jar", {
            "broken.mixins.json": broken,
            "stray.mixins.json": {"package": "com.example.stray", "minVersion": "0.9", "compatibilityLevel": "JAVA_8",
                                  "mixins": []},
            "code.mixins.json": {"package": "com.example.code", "minVersion": "0.8.x", "compatibilityLevel": "JAVA_8",
                                 "mixins": []}}, {
            "com/example/Outside": mixin_class("com/example/Outside", targets=[WORLD]),
            "com/example/broken/MixinFull": mixin_class("com/example/broken/MixinFull", targets=[WORLD]),
            "com/example/broken/NotAMixin": plain_class("com/example/broken/NotAMixin"),
            "com/example/broken/MixinTwice": mixin_class("com/example/broken/MixinTwice", targets=[WORLD]),
            "com/example/broken/MixinPoints": mixin_class("com/example/broken/MixinPoints", targets=[WORLD],
                                                          points=["HAED", "OTHER:THING", "com.example.NoPoint"]),
            "com/example/Loader": plain_class("com/example/Loader", literal="code.mixins.json")},
            declared=["broken.mixins.json", "gone.mixins.json"])

    @classmethod
    def tearDownClass(cls):
        cls.d.stop()

    def doctor(self, *jars, **params):
        return self.c.submit("mixin.doctor", {"jars": [str(j) for j in jars], **params}).result(timeout=180)

    def problems(self, result) -> dict:
        report = json.loads(Path(result.data.report).read_text(encoding="utf-8"))
        found: dict = {}
        for problem in report["problems"]:
            found.setdefault(problem["check"], []).append((problem["severity"], problem["detail"]))
        return found

    def test_a_sound_jar_passes(self):
        result = self.doctor(self.good, loader="example", classpath=[str(self.game)])
        self.assertEqual(result.outcome, "ok", result.summary)
        self.assertTrue(result.data.passed)
        self.assertEqual((result.data.counts["configs"], result.data.counts["mixins"], result.data.counts["warnings"]),
                         (1, 2, 0))
        self.assertIn("pass under [modules.mixin.loaders.example]", result.summary)
        self.assertIn("targets found among the class path's 2 classes", result.summary)
        report = json.loads(Path(result.data.report).read_text(encoding="utf-8"))
        self.assertEqual([c["config"] for c in report["configs"]], ["good.mixins.json"])  # not-a-config.json is not one
        config = report["configs"][0]
        self.assertEqual((config["config"], config["registered"], set(config["checks"].values())),
                         ("good.mixins.json", "metadata", {"ok"}))
        self.assertEqual([m["class_version"] for m in config["mixins"]], [52, 52])
        self.assertEqual(config["mixins"][1]["targets"], ["net.minecraft.client.renderer.EntityRenderer"])
        self.assertEqual(report["jars"][0]["sha256"], sha256_of(self.good))
        self.assertEqual(report["policy"]["compatibility_levels"], "JAVA_8..JAVA_25")
        bare = self.doctor(self.good)  # no policy: the structural checks still run
        self.assertEqual(bare.outcome, "ok", bare.summary)
        self.assertIn("targets not checked (no classpath)", bare.summary)

    def test_java_25_configs_over_java_26_classes(self):
        result = self.doctor(self.newer, loader="example")
        self.assertEqual((result.outcome, result.code), ("finding", 1))
        expected = ("coverage: newer.jar!/next.mixins.json: compatibilityLevel JAVA_25 covers class version 69 "
                    "(Java 25), but 2 of its Mixins are class version 70 (Java 26): com.example.next.MixinTick, "
                    "com.example.next.MixinChunk")
        self.assertEqual(result["findings"], [expected])
        older = self.doctor(self.newer, loader="example", max_class_version=69)  # a runtime that reads Java 25 at most
        self.assertEqual(sorted(self.problems(older)), ["class-version", "coverage"])
        self.assertIn("above the runtime's highest, 69 (Java 25)", self.problems(older)["class-version"][0][1])
        relaxed = self.doctor(self.newer, loader="example", severity={"coverage": "warning"})
        self.assertEqual(relaxed.outcome, "ok", relaxed.summary)
        self.assertIn("pass under [modules.mixin.loaders.example] and params: 2 Mixins", relaxed.summary)
        self.assertIn("1 warning(s), first coverage: newer.jar!/next.mixins.json", relaxed.summary)
        self.assertEqual(relaxed.data.configs[0]["checks"], {"coverage": "warning", "targets": "skipped"})
        off = self.doctor(self.newer, loader="example", severity={"coverage": "off"})
        self.assertEqual(off.outcome, "ok", off.summary)
        self.assertEqual(off.data.configs[0]["checks"]["coverage"], "off")

    def test_every_config_check(self):
        result = self.doctor(self.broken, loader="example")
        self.assertEqual(result.outcome, "finding")
        found = self.problems(result)
        details = {check: [detail for _, detail in rows] for check, rows in found.items()}
        self.assertEqual(sorted(details["parse"]), ["required must be true or false, not \"yes\"",
                                                    "the JAR's metadata names it, but it is not packaged"])
        self.assertEqual(details["fields"], ["does not set minVersion, which the policy requires"])
        self.assertEqual(details["compatibility"],
                         ["compatibilityLevel JAVA_26 is not one the loader defines (JAVA_8..JAVA_25)"])
        self.assertEqual(details["classes"], [
            "lists com.example.broken.MixinMissing, which is not in the JAR",
            "lists com.example.Outside, a class outside its package com.example.broken: Mixin reads names under the "
            "package, so it looks for com.example.broken.com.example.Outside",
            "lists com.example.broken.MixinFull by its full name: Mixin reads names under the package, so it looks "
            "for com.example.broken.com.example.broken.MixinFull",
            "lists com.example.broken.NotAMixin, which carries no @Mixin",
            "lists com.example.broken.MixinTwice in both mixins and client"])
        self.assertEqual(details["refmap"], ["its refmap missing.refmap.json is not packaged"])
        self.assertEqual(details["plugin"], ["its plugin com.example.NoPlugin is not in the JARs"])
        self.assertEqual(details["injection-points"], [
            "com.example.broken.MixinPoints: @At(\"HAED\") is not an injection point Mixin defines or a configuration "
            "of the JARs registers",
            "com.example.broken.MixinPoints: @At(\"OTHER:THING\") is in namespace OTHER, which the loader does not "
            "provide (it provides MIXINEXTRAS)",
            "com.example.broken.MixinPoints: @At(\"com.example.NoPoint\") names a class that is not in the JARs"])
        self.assertEqual(sorted(details["min-version"]), [
            "minVersion '0.8.x' is not a version Mixin reads, so it reads it as no minimum",
            "minVersion 0.9 is above the loader's Mixin 0.8.7"])
        self.assertEqual(found["registration"], [
            ("warning", "no manifest key, loader entry or class names it, so nothing registers it")])
        self.assertTrue(all(severity == "error" for check, rows in found.items() if check != "registration"
                            for severity, _ in rows))
        registered = {row["config"]: row["registered"] for row in result.data.configs}
        self.assertEqual(registered, {"broken.mixins.json": "metadata", "gone.mixins.json": "metadata",
                                      "stray.mixins.json": None, "code.mixins.json": "code"})
        self.assertEqual(result.data.counts["errors"], len(result["findings"]))
        self.assertEqual(result.data.configs[0]["mixins"], 2)  # MixinTwice and MixinPoints; NotAMixin is not one

    def test_targets_and_refmaps(self):
        data = self.d.data
        configs = {
            "req.mixins.json": {"required": True, "package": "com.example.req", "refmap": "req.refmap.json",
                                "mixins": ["MixinGhost", "MixinMaybe"]},
            "opt.mixins.json": {"package": "com.example.opt", "refmap": "req.refmap.json", "mixins": ["MixinGone"]},
            "remap.mixins.json": {"package": "com.example.remap", "mixins": ["MixinNeeds", "MixinOwn", "MixinOff"]}}
        jar = doctor_jar(data / "targets.jar", configs, {
            "com/example/req/MixinGhost": mixin_class("com/example/req/MixinGhost",
                                                      targets=["net/minecraft/world/Ghost"]),
            "com/example/req/MixinMaybe": mixin_class("com/example/req/MixinMaybe",
                                                      targets=["net/minecraft/world/Maybe"], pseudo=True),
            "com/example/opt/MixinGone": mixin_class("com/example/opt/MixinGone", strings=["net.minecraft.world.Gone"]),
            "com/example/remap/MixinNeeds": mixin_class("com/example/remap/MixinNeeds", targets=[WORLD]),
            "com/example/remap/MixinOwn": mixin_class("com/example/remap/MixinOwn", targets=["com/example/Target"]),
            "com/example/remap/MixinOff": mixin_class("com/example/remap/MixinOff", targets=[WORLD], remap=False),
            "com/example/Target": plain_class("com/example/Target")},
            declared=list(configs), extra={"req.refmap.json": json.dumps({"mappings": {}})})
        result = self.doctor(jar, classpath=[str(self.game)], remapped_packages=["net/minecraft/"])
        found = self.problems(result)
        self.assertEqual(found["targets"], [
            ("error", "com.example.req.MixinGhost targets net.minecraft.world.Ghost, which is not on the class path"),
            ("warning", "com.example.opt.MixinGone targets net.minecraft.world.Gone, which is not on the class path")])
        self.assertEqual(found["refmap"], [("error", "names no refmap, but its Mixins remap selectors: "
                                                     "com.example.remap.MixinNeeds")])
        self.assertEqual(sorted(found), ["refmap", "targets"])
        lenient = self.doctor(jar, classpath=[str(self.game)], required_may_target_missing=True,
                              severity={"refmap": "off"})
        self.assertEqual(lenient.outcome, "ok", lenient.summary)
        self.assertEqual([s for s, _ in self.problems(lenient)["targets"]], ["warning", "warning"])
        everywhere = self.doctor(jar)  # every target counts as remapped; no class path, so targets are not checked
        self.assertEqual([d for _, d in self.problems(everywhere)["refmap"]],
                         ["names no refmap, but its Mixins remap selectors: com.example.remap.MixinNeeds, "
                          "com.example.remap.MixinOwn"])
        self.assertEqual({row["checks"].get("targets") for row in everywhere.data.configs}, {"skipped"})
        folder = data / "classes"  # a class folder on the class path holds classes as a JAR does
        (folder / "net/minecraft/world").mkdir(parents=True, exist_ok=True)
        (folder / "net/minecraft/world/Ghost.class").write_bytes(plain_class("net/minecraft/world/Ghost"))
        (data / "nothing").mkdir(exist_ok=True)
        widened = self.doctor(jar, classpath=[str(self.game), str(folder), str(data / "nothing")],
                              remapped_packages=["net/minecraft/"])
        self.assertEqual([d for _, d in self.problems(widened)["targets"]],
                         ["com.example.opt.MixinGone targets net.minecraft.world.Gone, which is not on the class path"])
        report = json.loads(Path(widened.data.report).read_text(encoding="utf-8"))
        self.assertEqual([entry.get("error") for entry in report["classpath"]["jars"]],
                         [None, None, "no readable JAR or class file"])

    def test_duplicates_nested_jars_and_unreadable_ones(self):
        pack = self.d.data / "pack"
        shared = {"shared.mixins.json": {"package": "com.example.shared", "mixins": []}}
        doctor_jar(pack / "dup-a.jar", shared, {}, declared=["shared.mixins.json"])
        doctor_jar(pack / "sub" / "dup-b.jar", shared, {}, declared=["shared.mixins.json"])
        lib = doctor_jar(pack / "lib.jar", {"lib.mixins.json": {"package": "com.example.lib",
                                                                "mixins": ["MixinAbsent"]}},
                         {}, declared=["lib.mixins.json"])
        jvmkit.make_jar(pack / "outer.jar", entries={"META-INF/jarjar/lib.jar": lib.read_bytes()})
        (pack / "garbage.jar").write_bytes(b"not a zip")
        result = self.doctor(pack)
        found = self.problems(result)
        self.assertEqual(found["duplicates"], [("error", "is in 2 JARs: dup-a.jar, sub/dup-b.jar")])
        self.assertIn("duplicates: shared.mixins.json: is in 2 JARs: dup-a.jar, sub/dup-b.jar", result["findings"])
        self.assertIn("classes: outer.jar!/META-INF/jarjar/lib.jar!/lib.mixins.json: lists "
                      "com.example.lib.MixinAbsent, which is not in the JAR", result["findings"])
        self.assertTrue(any(line.startswith("parse: garbage.jar: not a readable JAR") for line in result["findings"]))
        self.assertEqual(len(found["duplicates"]), 1)  # the nested copy of lib.mixins.json is not a duplicate
        rows = {row["jar"]: row for row in result.data.jars}
        self.assertEqual((rows["dup-a.jar"]["errors"], rows["outer.jar!/META-INF/jarjar/lib.jar"]["nested"]), (1, True))
        flat = self.doctor(pack, nested=False)
        self.assertNotIn("outer.jar!/META-INF/jarjar/lib.jar", {row["jar"] for row in flat.data.jars})
        same = self.doctor(pack / "dup-a.jar", pack)  # one JAR given twice is read once
        self.assertEqual([row["jar"] for row in same.data.jars].count("dup-a.jar"), 1)
        (self.d.data / "other").mkdir(exist_ok=True)
        shutil.copy(pack / "dup-a.jar", self.d.data / "other" / "dup-a.jar")
        twins = self.doctor(pack / "dup-a.jar", self.d.data / "other" / "dup-a.jar")  # two JARs of one name
        labels = [row["jar"] for row in twins.data.jars]
        self.assertEqual(len(set(labels)), 2)
        self.assertTrue(all(Path(label).name == "dup-a.jar" and Path(label).is_absolute() for label in labels), labels)

    def test_policy_files_layers_and_refusals(self):
        data = self.d.data
        (data / "policy.toml").write_text('mixin_version = "0.8"\n[severity]\nregistration = "error"\n',
                                          encoding="utf-8")
        (data / "policy.json").write_text(json.dumps({"compatibility_levels": ["JAVA_8"],
                                                      "severity": {"coverage": "off"}}), encoding="utf-8")
        layered = self.doctor(self.broken, loader="example", policy=str(data / "policy.toml"))
        policy = layered.data.policy
        self.assertEqual((policy["sources"], policy["mixin_version"], policy["severity"]["registration"]),
                         (["[modules.mixin.loaders.example]", "policy.toml"], "0.8", "error"))
        self.assertIn("registration: broken.jar!/stray.mixins.json: no manifest key", "\n".join(layered["findings"]))
        self.assertIn("minVersion 0.9 is above the loader's Mixin 0.8", "\n".join(layered["findings"]))
        report = json.loads(Path(layered.data.report).read_text(encoding="utf-8"))
        self.assertEqual(report["policy"]["file"]["sha256"], sha256_of(data / "policy.toml"))
        narrow = self.doctor(self.newer, policy=str(data / "policy.json"))
        self.assertEqual(sorted(self.problems(narrow)), ["compatibility"])
        self.assertEqual(narrow.data.configs[0]["checks"]["coverage"], "off")
        empty = data / "empty"
        empty.mkdir(exist_ok=True)
        (data / "bad.toml").write_text('colour = "blue"\n', encoding="utf-8")
        plain = jvmkit.make_jar(data / "plain.jar", entries={"a/B.class": plain_class("a/B")})
        for params, words in ((dict(jars=[str(self.good)], loader="nope"), "no 'nope' under [modules.mixin.loaders]"),
                              (dict(jars=[str(self.good)], compatibility_levels=["JAVA_X"]), "is not a level"),
                              (dict(jars=[str(self.good)], policy=str(data / "bad.toml")), "unknown keys ['colour']"),
                              (dict(jars=[str(self.good)], severity={"bogus": "error"}), "unknown check 'bogus'"),
                              (dict(jars=[str(empty)]), "no JAR files")):
            refused = self.c.submit("mixin.doctor", params).result(timeout=180)
            self.assertEqual(refused.outcome, "usage", params)
            self.assertIn(words, refused.summary)
        nothing = self.doctor(plain)
        self.assertEqual(nothing.outcome, "finding")
        self.assertIn("nothing to check", nothing.summary)
        self.assertFalse(nothing.data.passed)


def sha256_of(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


COMPILED = """
    package com.example.compiled;
    import org.spongepowered.asm.mixin.Mixin;
    import org.spongepowered.asm.mixin.injection.At;
    import org.spongepowered.asm.mixin.injection.Inject;
    import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;
    import net.minecraft.world.World;
    @Mixin(World.class)
    public abstract class MixinCompiled {
        @Inject(method = "tick", at = @At("HEAD"))
        private void onTick(CallbackInfo info) {}
    }
"""


@unittest.skipUnless(JAVAC, "no JDK with javac on this machine")
class DoctorCompiled(unittest.TestCase):
    """A javac-built Mixin at the newest JDK's class version, in a configuration one level below it."""

    def test_a_level_below_the_compiler(self):
        major = max(JDKS, key=int)
        d = Daemon().start()
        try:
            sources = dict(jvmkit.STUBS, **{"net/minecraft/world/World.java": "package net.minecraft.world;\n"
                                            "public class World { public void tick() {} }\n",
                                            "com/example/compiled/MixinCompiled.java": COMPILED})
            built = jvmkit.compile_java(JAVAC, sources, d.root / "compiled")
            config = {"required": True, "minVersion": "0.8", "package": "com.example.compiled",
                      "refmap": "compiled.refmap.json", "compatibilityLevel": f"JAVA_{int(major) - 1}",
                      "mixins": ["MixinCompiled"]}
            jar = jvmkit.make_jar(d.data / "compiled.jar", folders=[built], skip=("org/", "net/"), entries={
                "compiled.mixins.json": json.dumps(config), "compiled.refmap.json": json.dumps({"mappings": {}})},
                manifest={"MixinConfigs": "compiled.mixins.json"})
            game = jvmkit.make_jar(d.data / "game.jar", folders=[built], skip=("org/", "com/"))
            result = d.client.submit("mixin.doctor", {"jars": [str(jar)], "classpath": [str(game)]}).result(timeout=180)
            version = int(major) + 44
            self.assertEqual(result["findings"], [
                f"coverage: compiled.jar!/compiled.mixins.json: compatibilityLevel JAVA_{int(major) - 1} covers class "
                f"version {version - 1} (Java {int(major) - 1}), but 1 of its Mixins is class version {version} "
                f"(Java {major}): com.example.compiled.MixinCompiled"])
        finally:
            d.stop()


if __name__ == "__main__":
    unittest.main()
