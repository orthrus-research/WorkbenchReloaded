"""The jar module's typed commands (index, references, hook-targets) and the class reader behind them,
over JARs built in the test: hand-written class files always, javac-built ones where a JDK is found."""

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
for extra in (REPO / "tests", HERE, HERE.parent):
    sys.path.insert(0, str(extra))

import jvmkit  # noqa: E402
from classwriter import INVOKESPECIAL, INVOKEVIRTUAL, RETURN, Ann, ClassWriter, Cls, jar  # noqa: E402
from harness import Daemon  # noqa: E402

from wb_jar import classes  # noqa: E402
from orthrus_tools import classfile  # noqa: E402 - on the path once wb_jar is imported

JDKS = jvmkit.find_jdks()
JAVAC = jvmkit.newest(JDKS)
NEEDS_JDK = unittest.skipUnless(JAVAC, "no JDK with javac on this machine")

MIXIN = "Lorg/spongepowered/asm/mixin/Mixin;"
INJECT = "Lorg/spongepowered/asm/mixin/injection/Inject;"
REDIRECT = "Lorg/spongepowered/asm/mixin/injection/Redirect;"
AT = "Lorg/spongepowered/asm/mixin/injection/At;"
OVERWRITE = "Lorg/spongepowered/asm/mixin/Overwrite;"
MOD = "Lnet/minecraftforge/fml/common/Mod;"
SUBSCRIBE = "Lnet/minecraftforge/fml/common/eventhandler/SubscribeEvent;"
WORLD = "net/minecraft/world/World"
CALLBACK = "(Lorg/spongepowered/asm/mixin/injection/callback/CallbackInfo;)V"
UPDATE_DEV = f"L{WORLD};updateBlocks()V"
UPDATE = f"L{WORLD};func_147456_g()V"
TICK = f"L{WORLD};func_72835_b()V"


def world_mixin(name: str, *, overwrite: bool = True, redirect: bool = True, ordinal: int | None = None) -> bytes:
    """A Mixin of World with an injector into tick, and optionally a redirect and an overwrite."""
    w = ClassWriter(name).annotate(Ann(MIXIN, {"value": [Cls(f"L{WORLD};")]}))
    w.method("onTick", CALLBACK, annotations=[
        Ann(INJECT, {"method": ["tick"], "at": [Ann(AT, {"value": "HEAD"}, visible=True)]}, visible=True)])
    if redirect:
        point = {"value": "INVOKE", "target": UPDATE_DEV} | ({"ordinal": ordinal} if ordinal is not None else {})
        w.method("redirectUpdate", f"(L{WORLD};)V", annotations=[
            Ann(REDIRECT, {"method": ["tick"], "at": Ann(AT, point, visible=True)}, visible=True)])
    if overwrite:
        w.method("func_72867_j", "()J", code=b"\x09\xad", annotations=[Ann(OVERWRITE)])
    return w.build()


def refmap(mixin: str) -> str:
    return json.dumps({"mappings": {mixin: {"tick": TICK, UPDATE_DEV: UPDATE}}})


def alpha_jar(path: Path, nested: bytes | None = None) -> Path:
    mod = ClassWriter("com/alpha/Alpha").annotate(
        Ann(MOD, {"modid": "alpha", "dependencies": "required-after:forge@[14.23,);required-after:gamma;after:jei"},
            visible=True))
    start = mod.invoke(INVOKESPECIAL, "java/lang/Thread", "<init>", "(Ljava/lang/Runnable;)V")
    mod.method("start", "()V", code=start + RETURN)
    mod.method("onWorldTick", "(Lnet/minecraftforge/fml/common/gameevent/TickEvent$WorldTickEvent;)V",
               annotations=[Ann(SUBSCRIBE, visible=True)])
    renderer = ClassWriter("com/alpha/mixins/MixinRenderer").annotate(
        Ann(MIXIN, {"targets": ["net.minecraft.client.renderer.EntityRenderer"], "priority": 900}))
    renderer.method("onRender", CALLBACK, annotations=[
        Ann(INJECT, {"method": ["func_78480_b"], "at": [Ann(AT, {"value": "RETURN"}, visible=True)]}, visible=True)])
    config = {"package": "com.alpha.mixins", "refmap": "alpha.refmap.json", "required": True,
              "mixins": ["MixinWorld"], "client": ["MixinRenderer"]}
    entries = {
        "alpha.mixins.json": json.dumps(config),
        "alpha.refmap.json": refmap("com/alpha/mixins/MixinWorld"),
        "com/alpha/Alpha.class": mod.build(),
        "com/alpha/mixins/MixinWorld.class": world_mixin("com/alpha/mixins/MixinWorld"),
        "com/alpha/mixins/MixinRenderer.class": renderer.build(),
        "mcmod.info": json.dumps([{"modid": "alpha", "requiredMods": ["beta@[1.0,)"]}]),
    }
    if nested:
        entries["META-INF/jarjar/inner.jar"] = nested
    return jvmkit.make_jar(path, entries=entries, manifest={"MixinConfigs": "alpha.mixins.json"})


def beta_jar(path: Path) -> Path:
    config = {"package": "com.beta.mixins", "refmap": "beta.refmap.json", "required": True,
              "mixins": ["MixinWorldToo", "MixinGone"]}
    mods_toml = ('[[mods]]\nmodId = "beta"\n[[dependencies.beta]]\nmodId = "delta"\nmandatory = true\n'
                 '[[dependencies.beta]]\nmodId = "optional_thing"\nmandatory = false\n'
                 '[[mixins]]\nconfig = "beta.optional.json"\n')
    return jvmkit.make_jar(path, entries={
        "beta.mixins.json": json.dumps(config),
        "beta.optional.json": json.dumps({"package": "com.beta.optional", "mixins": ["MixinAbsent"]}),
        "beta.refmap.json": refmap("com/beta/mixins/MixinWorldToo"),
        "com/beta/mixins/MixinWorldToo.class": world_mixin("com/beta/mixins/MixinWorldToo"),
        "META-INF/mods.toml": mods_toml,
    })


def inner_jar() -> bytes:
    mixin = ClassWriter("com/inner/MixinChunk").annotate(Ann(MIXIN, {"targets": ["net/minecraft/world/chunk/Chunk"]}))
    mixin.method("onLoad", CALLBACK, annotations=[
        Ann(INJECT, {"method": ["func_76631_c"], "at": [Ann(AT, {"value": "HEAD"}, visible=True)]}, visible=True)])
    return jar({"inner.mixins.json": json.dumps({"package": "com.inner", "mixins": ["MixinChunk"]}),
                "com/inner/MixinChunk.class": mixin.build()})


class Reader(unittest.TestCase):
    def test_hand_built_class(self):
        w = ClassWriter("com/example/Thing", "com/example/Base", ["java/lang/Runnable"])
        w.annotate(Ann(MIXIN, {"value": [Cls(f"L{WORLD};")], "priority": 1200, "remap": False}))
        code = w.invoke(INVOKEVIRTUAL, WORLD, "tick", "()V") + w.method_reference("com/example/Util", "help", "()V")
        w.method("run", "()V", code=code + RETURN, annotations=[Ann(OVERWRITE)])
        w.field("count", "I", annotations=[Ann("Lorg/spongepowered/asm/mixin/Shadow;", {"prefix": "x$"})])
        data = w.build()
        info = classes.read(data)
        self.assertTrue(info.complete)
        self.assertEqual((info.this_class, info.super_name, info.interfaces),
                         ("com/example/Thing", "com/example/Base", ["java/lang/Runnable"]))
        mixin = info.annotation(MIXIN)
        self.assertEqual(mixin.values, {"value": [f"L{WORLD};"], "priority": 1200, "remap": False})
        self.assertFalse(mixin.visible)
        run = info.method("run", "()V")
        self.assertEqual(list(info.calls(run)), [("virtual", WORLD, "tick", "()V"),
                                                 ("handle", "com/example/Util", "help", "()V")])
        self.assertEqual(info.declared_fields[0].annotations[0].get("prefix"), "x$")
        base = classfile.parse(data)  # the same ClassFile as the survey's reader fills
        for name in ("utf8", "classes", "name_and_type", "member_refs", "integers", "string_literals",
                     "method_bodies", "this_class", "major", "complete"):
            self.assertEqual(getattr(info, name), getattr(base, name), name)

    def test_damage_never_raises(self):
        data = world_mixin("com/example/MixinWorld")
        for cut in (3, 9, 40, len(data) // 2, len(data) - 1):
            info = classes.read(data[:cut])
            self.assertFalse(info.complete, cut)
        self.assertEqual(list(classes.instructions(b"\xaa\x00")), [])  # a tableswitch cut short
        self.assertEqual(list(classes.instructions(b"\x12\x05\xcb\x12\x06")), [(0x12, 5)])  # stops at a non-opcode
        self.assertEqual(classes.parameters("(I[JLa/B;[[Lc/D;)V"), ["I", "[J", "La/B;", "[[Lc/D;"])
        self.assertEqual(classes.parameters("(La/B"), [])

    @NEEDS_JDK
    def test_javac_built_classes(self):
        scratch = tempfile.TemporaryDirectory(prefix="wb-reader-")
        self.addCleanup(scratch.cleanup)
        out = Path(scratch.name) / "classes"
        sources = dict(jvmkit.STUBS)
        sources["com/example/mixins/MixinThing.java"] = """
            package com.example.mixins;
            import org.spongepowered.asm.mixin.*;
            import org.spongepowered.asm.mixin.injection.*;
            import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;
            @Mixin(targets = "com.example.game.Thing", priority = 1200)
            public abstract class MixinThing implements Runnable {
                @Shadow private int count;
                @Shadow public abstract void step(int n);
                @Inject(method = "update()V", at = @At("HEAD"))
                private void onUpdate(CallbackInfo info) {
                    Runnable later = this::helper;
                    switch (count) { case 1: step(1); break; case 7: step(2); break; case 900: step(3); break; }
                    switch (count) { case 1: case 2: case 3: case 4: later.run(); break; default: step(5); }
                    long wide = 1L << 40; double d = 2.5; if (wide > d) step((int) wide);
                }
                public void run() { new Thread(() -> step(4)).start(); }
                private void helper() {}
                @Overwrite public int size() { return count; }
            }
        """
        jvmkit.compile_java(JAVAC, sources, out, release=8)
        data = (out / "com/example/mixins/MixinThing.class").read_bytes()
        info = classes.read(data)
        self.assertTrue(info.complete)
        self.assertEqual(info.annotation(MIXIN).values, {"targets": ["com.example.game.Thing"], "priority": 1200})
        update = info.method("onUpdate")
        self.assertEqual(update.annotation(INJECT).nested("at")[0].get("value"), "HEAD")
        calls = list(info.calls(update))
        self.assertIn(("handle", "com/example/mixins/MixinThing", "helper", "()V"), calls)
        self.assertEqual(sum(1 for c in calls if c[2] == "step"), 5)  # past both switches: the walk stays aligned
        self.assertIn(("special", "java/lang/Thread", "<init>", "(Ljava/lang/Runnable;)V"), list(info.calls(info.method("run"))))
        self.assertEqual([f.name for f in info.declared_fields if f.annotation("Lorg/spongepowered/asm/mixin/Shadow;")], ["count"])
        base = classfile.parse(data)
        self.assertEqual((info.utf8, info.member_refs, info.method_bodies), (base.utf8, base.member_refs, base.method_bodies))


def archive(path: Path, entries: list[tuple], level: int = 9) -> Path:
    """A ZIP of (name, bytes[, date_time]) in that order."""
    import zipfile
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=level) as out:
        for name, data, *when in entries:
            info = zipfile.ZipInfo(name, date_time=when[0] if when else (1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            out.writestr(info, data, compresslevel=level)
    return path


class ArchiveDiff(unittest.TestCase):
    """jar.diff names why two builds of one archive differ."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)

    def tearDown(self):
        self.folder.cleanup()

    def test_each_kind_of_difference_is_explained(self):
        from wb_jar import diff
        text = b"line one\nline two\n" * 40
        base = [("META-INF/LICENSE", text), ("table.json", b'{"b": 1, "a": 2}'), ("list.txt", b"x\ny\nz\n"),
                ("stamp.txt", b"same"), ("packed.bin", bytes(range(256)) * 64), ("gone.txt", b"only here")]
        other = [("META-INF/LICENSE", text.replace(b"\n", b"\r\n")), ("table.json", b'{"a": 2,\n "b": 1}'),
                 ("list.txt", b"z\nx\ny\n"), ("stamp.txt", b"same", (2026, 1, 2, 3, 4, 6)),
                 ("packed.bin", bytes(range(256)) * 64), ("new.txt", b"only there")]
        a = archive(self.root / "a.jar", base)
        b = archive(self.root / "b.jar", other)
        report = diff.compare(a, b)
        kinds = {e["name"]: e["kind"] for e in report["entries"]}
        self.assertEqual(kinds, {"META-INF/LICENSE": "line endings", "table.json": "JSON order or layout",
                                 "list.txt": "line order", "stamp.txt": "metadata"})
        self.assertEqual((report["onlyInA"], report["onlyInB"]), (["gone.txt"], ["new.txt"]))
        self.assertFalse(report["identical"])
        recompressed = archive(self.root / "c.jar", base, level=1)  # the same entries, another zlib level
        kinds = {e["name"]: e["kind"] for e in diff.compare(a, recompressed)["entries"]}
        self.assertEqual(kinds.get("packed.bin"), "compression")
        swapped = archive(self.root / "d.jar", list(reversed(base)))
        self.assertTrue(diff.compare(a, swapped)["orderDiffers"])
        self.assertTrue(diff.compare(a, archive(self.root / "e.jar", base))["identical"])

    def test_the_command_reports_identical_and_differing_builds(self):
        d = Daemon().start()
        try:
            a = archive(d.data / "one.jar", [("x.txt", b"a\nb\n")])
            same = archive(d.data / "two.jar", [("x.txt", b"a\nb\n")])
            crlf = archive(d.data / "three.jar", [("x.txt", b"a\r\nb\r\n")])
            ok = d.client.submit("jar.diff", {"a": str(a), "b": str(same)}).result(timeout=120)
            self.assertEqual((ok.outcome, ok.data.identical), ("ok", True), ok.summary)
            found = d.client.submit("jar.diff", {"a": str(a), "b": str(crlf)}).result(timeout=120)
            self.assertEqual(found.outcome, "finding", found.summary)
            self.assertEqual(found.data.kinds, {"line endings": 1})
        finally:
            d.stop()


class Commands(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = Daemon().start()
        cls.c = cls.d.client
        cls.pack = cls.d.data / "pack"
        alpha_jar(cls.pack / "alpha.jar", nested=inner_jar())
        beta_jar(cls.pack / "beta.jar")
        cls.clean = cls.d.data / "clean"
        jvmkit.make_jar(cls.clean / "gamma.jar", entries={
            "gamma.mixins.json": json.dumps({"package": "com.gamma", "mixins": ["MixinWorldG"]}),
            "com/gamma/MixinWorldG.class": world_mixin("com/gamma/MixinWorldG", overwrite=False, redirect=False),
            "fabric.mod.json": json.dumps({"id": "gamma", "depends": {"fabricloader": ">=0.14"}})})

    @classmethod
    def tearDownClass(cls):
        cls.d.stop()

    def run_command(self, command, params, **kw):
        return self.c.submit(command, params, **kw).result(timeout=180)

    def test_index_reports_per_jar_and_across(self):
        systems = {"net/minecraft/client/renderer/": "rendering", "net/minecraft/world/": "world",
                   "net/minecraft/world/chunk/": "chunks"}
        out = self.d.out / "index"
        result = self.run_command("jar.index", {"jars": str(self.pack), "systems": systems, "require_complete": True},
                                  out=str(out))
        self.assertEqual((result.outcome, result.code), ("finding", 1), result.summary)
        data = result.data
        self.assertEqual(sorted(data.shared), ["net.minecraft.world.World"])
        self.assertEqual(data.shared["net.minecraft.world.World"], ["alpha.jar", "beta.jar"])
        self.assertEqual(data.targets["net.minecraft.client.renderer.EntityRenderer"], ["alpha.jar"])
        self.assertEqual(data.targets["net.minecraft.world.chunk.Chunk"], ["alpha.jar!/META-INF/jarjar/inner.jar"])
        kinds = {(c["kind"], c["member"]) for c in data.collisions}
        self.assertEqual(kinds, {("overwrite", "func_72867_j()J"), ("redirect", "func_72835_b")})
        redirect = next(c for c in data.collisions if c["kind"] == "redirect")
        self.assertEqual(redirect["at"], UPDATE)  # the injection point, through each JAR's refmap
        self.assertIn("beta.mixins.json: lists com.beta.mixins.MixinGone, which is not in the JAR",
                      [e["error"] for e in data.errors])
        self.assertEqual({m["requires"] for m in data.missing}, {"gamma", "delta"})  # forge is the platform's
        jars = {j["jar"]: j for j in data.jars}
        self.assertEqual(jars["alpha.jar"]["mods"], ["alpha"])
        self.assertEqual(jars["alpha.jar"]["systems"], {"world": 1, "rendering": 1})
        self.assertEqual(jars["alpha.jar!/META-INF/jarjar/inner.jar"]["systems"], {"chunks": 1})
        self.assertEqual(jars["alpha.jar"]["threads"], ["Thread.<init>"])
        self.assertEqual(jars["alpha.jar"]["events"], 1)
        self.assertEqual(jars["beta.jar"]["mods"], ["beta"])
        report = json.loads((out / "report.json").read_text(encoding="utf-8"))
        alpha = next(j for j in report["jars"] if j["jar"] == "alpha.jar")
        world = next(m for m in alpha["mixins"] if m["name"] == "com/alpha/mixins/MixinWorld")
        self.assertEqual(world["handlers"][0]["methods"], [TICK])  # through the refmap
        self.assertEqual(world["config"], "alpha.mixins.json")
        self.assertEqual(alpha["events"], {"net.minecraftforge.fml.common.gameevent.TickEvent$WorldTickEvent": 1})
        self.assertEqual(alpha["threads"], {"Thread.<init>": ["com.alpha.Alpha"]})
        self.assertEqual(sorted(alpha["requires"]), ["beta", "forge", "gamma"])
        self.assertEqual(alpha["manifest"], {"MixinConfigs": "alpha.mixins.json"})
        beta = next(j for j in report["jars"] if j["jar"] == "beta.jar")
        self.assertEqual(beta["notes"], ["beta.optional.json: lists com.beta.optional.MixinAbsent, which is not in "
                                         "the JAR"])  # not a required configuration: Mixin only logs it
        patchers = report["targets"]["net.minecraft.world.World"]
        self.assertEqual({p["mixin"] for p in patchers}, {"com.alpha.mixins.MixinWorld", "com.beta.mixins.MixinWorldToo"})

    def test_index_clean_pack_is_ok_and_cached(self):
        first = self.run_command("jar.index", {"jars": str(self.clean)})
        self.assertEqual(first.outcome, "ok", first.summary)
        self.assertIn("1 JARs, 1 Mixins over 1 target classes", first.summary)
        self.assertEqual(first.data.jars[0]["mods"], ["gamma"])
        self.assertEqual(first.data.jars[0]["systems"], {"net/minecraft/world": 1})  # no groups: the package
        again = self.run_command("jar.index", {"jars": str(self.clean)})
        self.assertTrue(again["cache"]["hit"])
        (self.d.data / "empty").mkdir(exist_ok=True)
        empty = self.run_command("jar.index", {"jars": str(self.d.data / "empty")})
        self.assertEqual((empty.outcome, empty.summary), ("usage", f"jars: no JAR files at {self.d.data / 'empty'}"))

    def test_index_collides_only_across_jars_on_the_same_occurrence(self):
        folder = self.d.data / "ordinals"
        for name, ordinal in (("one", 0), ("two", 1)):
            jvmkit.make_jar(folder / f"{name}.jar", entries={
                f"{name}.mixins.json": json.dumps({"package": f"com.{name}", "mixins": ["MixinA", "MixinB"]}),
                f"com/{name}/MixinA.class": world_mixin(f"com/{name}/MixinA", ordinal=ordinal),
                f"com/{name}/MixinB.class": world_mixin(f"com/{name}/MixinB", redirect=False)})
        result = self.run_command("jar.index", {"jars": str(folder)})
        self.assertEqual(result.outcome, "finding", result.summary)
        # The overwrites collide across the two JARs; the redirects hook different occurrences of the call.
        self.assertEqual([(c["kind"], sorted({b["jar"] for b in c["by"]})) for c in result.data.collisions],
                         [("overwrite", ["one.jar", "two.jar"])])
        alone = self.run_command("jar.index", {"jars": str(folder / "one.jar")})
        self.assertEqual(alone.outcome, "ok", alone.summary)  # one JAR's own Mixins: its own alternatives

    def hierarchy(self):
        game = self.d.data / "game.jar"
        screen = ClassWriter("net/minecraft/client/gui/GuiScreen", "net/minecraft/client/gui/Gui")
        screen.method("drawScreen", "(IIF)V").method("initGui", "()V")
        jvmkit.make_jar(game, entries={"net/minecraft/client/gui/GuiScreen.class": screen.build(),
                                       "net/minecraft/client/gui/Gui.class": ClassWriter("net/minecraft/client/gui/Gui").build()})
        mods = self.d.data / "screens"
        mine = ClassWriter("com/a/MyScreen", "net/minecraft/client/gui/GuiScreen")
        mine.method("drawScreen", "(IIF)V").method("helper", "()V", access=0x000A)
        deeper = ClassWriter("com/a/Deeper", "com/a/MyScreen")
        opener = ClassWriter("com/a/Opener")
        code = (opener.invoke(INVOKEVIRTUAL, "com/a/MyScreen", "drawScreen", "(IIF)V")
                + opener.method_reference("net/minecraft/client/gui/GuiScreen", "initGui", "()V"))
        opener.method("open", "()V", code=code + RETURN)
        jvmkit.make_jar(mods / "screens.jar", entries={
            "com/a/MyScreen.class": mine.build(), "com/a/Deeper.class": deeper.build(),
            "com/a/Opener.class": opener.build(), "com/a/Unrelated.class": ClassWriter("com/a/Unrelated").build()})
        return game, mods

    def test_references_subclass_override_call(self):
        game, mods = self.hierarchy()
        targets = ["net.minecraft.client.gui.GuiScreen", "net.minecraft.client.gui.GuiScreen::drawScreen(IIF)V",
                   "net/minecraft/client/gui/GuiScreen::initGui", "net.minecraft.client.gui.Gui"]
        result = self.run_command("jar.references", {"jars": str(mods), "targets": targets, "hierarchy": str(game)})
        self.assertEqual(result.outcome, "ok", result.summary)
        rows = {t["target"]: t for t in result.data.targets}
        screen = rows[targets[0]]
        self.assertEqual(sorted(r["class"] for r in screen["subclass"]), ["com.a.Deeper", "com.a.MyScreen"])
        deeper = next(r for r in screen["subclass"] if r["class"] == "com.a.Deeper")
        self.assertEqual(deeper["ancestry"], ["com.a.Deeper", "com.a.MyScreen", "net.minecraft.client.gui.GuiScreen"])
        self.assertEqual([r["how"] for r in screen["call"]], ["handle"])  # initGui, bound through GuiScreen itself
        draw = rows[targets[1]]
        self.assertEqual([(r["class"], r["method"]) for r in draw["override"]], [("com.a.MyScreen", "drawScreen(IIF)V")])
        self.assertEqual([(r["class"], r["in"], r["how"]) for r in draw["call"]], [("com.a.Opener", "open()V", "virtual")])
        self.assertEqual(rows[targets[2]]["counts"], {"subclass": 0, "override": 0, "call": 1})
        self.assertEqual(rows[targets[3]]["counts"]["subclass"], 2)  # only through the hierarchy JAR
        alone = self.run_command("jar.references", {"jars": str(mods), "targets": [targets[3]]})
        self.assertEqual(alone.data.targets[0]["counts"]["subclass"], 0)

    def test_references_gates(self):
        game, mods = self.hierarchy()
        found = self.run_command("jar.references", {"jars": str(mods), "fail_if": "found", "kinds": ["call"],
                                                    "targets": ["net.minecraft.client.gui.GuiScreen::drawScreen"]})
        self.assertEqual(found.outcome, "finding")
        self.assertIn("is referenced", found.summary)
        missing = self.run_command("jar.references", {"jars": str(mods), "fail_if": "missing",
                                                      "targets": ["net.minecraft.client.gui.GuiScreen::onClose",
                                                                  "net.minecraft.client.gui.GuiScreen::drawScreen"]})
        self.assertEqual(missing.outcome, "finding")
        self.assertIn("onClose: no subclass or override or call found", missing.summary)
        bad = self.run_command("jar.references", {"jars": str(mods), "targets": ["::drawScreen"]})
        self.assertEqual(bad.outcome, "usage")

    def test_hook_targets_and_resources(self):
        target = ClassWriter("com/x/Target").method("run", "(I)V").field("count", "I")
        shader = b"void main() { gl_Position = vec4(0.0); }\n"
        jar = jvmkit.make_jar(self.d.data / "hooks" / "x.jar", entries={
            "com/x/Target.class": target.build(), "assets/x/a.json": "{}", "assets/x/sub/b.json": "[]",
            "shaders/s.vsh": shader})
        pin = hashlib.sha256(shader).hexdigest()
        good = self.run_command("jar.hook-targets", {
            "jars": str(jar), "resources": ["assets/**/*.json"], "expect": {"shaders/s.vsh": pin},
            "targets": ["com.x.Target", "com.x.Target::run", "com.x.Target::run(I)V", "com.x.Target::run(I)",
                        "com/x/Target#count", "com.x.Target#count:I"]})
        self.assertEqual(good.outcome, "ok", good.summary)
        self.assertTrue(good.data.passed)
        self.assertEqual(good.data.resources, {"assets/**/*.json": 2})
        self.assertTrue(all(t["where"] == ["x.jar"] for t in good.data.targets))
        bad = self.run_command("jar.hook-targets", {
            "jars": str(jar.parent), "resources": ["textures/**"], "expect": {"shaders/s.vsh": "0" * 64,
                                                                              "shaders/gone.fsh": pin},
            "targets": ["com.x.Target::run(J)V", "com.x.Gone", "com.x.Target#gone"]})
        self.assertEqual((bad.outcome, bad.code), ("finding", 1))
        self.assertFalse(bad.data.passed)
        texts = bad["findings"]
        self.assertTrue(any("run(J)V is not in" in t and "it declares run(I)V" in t for t in texts), texts)
        self.assertTrue(any("no entry matches textures/**" in t for t in texts), texts)
        self.assertTrue(any("shaders/s.vsh differs" in t for t in texts), texts)
        self.assertTrue(any("shaders/gone.fsh is not packaged" in t for t in texts), texts)
        self.assertEqual(len(texts), 6)
        usage = self.run_command("jar.hook-targets", {"jars": str(jar), "expect": {"a": "not-a-digest"}})
        self.assertEqual(usage.outcome, "usage")


@NEEDS_JDK
class Compiled(unittest.TestCase):
    """The commands over classes javac wrote, Mixin annotations and lambdas included."""

    @classmethod
    def setUpClass(cls):
        cls.d = Daemon().start()
        cls.c = cls.d.client
        out = cls.d.root / "classes"
        sources = dict(jvmkit.STUBS)
        sources.update({
            "com/game/Base.java": "package com.game; public class Base { public void render(int x) {} public void tick() {} }",
            "com/mod/Mid.java": "package com.mod; public class Mid extends com.game.Base {}",
            "com/mod/Leaf.java": "package com.mod; public class Leaf extends Mid { @Override public void render(int x) {} }",
            "com/mod/Caller.java": """
                package com.mod;
                public class Caller {
                    void go(Mid m) { m.render(1); Runnable r = m::tick; r.run(); }
                }
            """,
            "com/mod/mixins/MixinBase.java": """
                package com.mod.mixins;
                import org.spongepowered.asm.mixin.*;
                import org.spongepowered.asm.mixin.injection.*;
                import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;
                @Mixin(com.game.Base.class)
                public class MixinBase {
                    @Inject(method = "tick", at = @At("HEAD")) private void onTick(CallbackInfo info) {}
                }
            """,
        })
        jvmkit.compile_java(JAVAC, sources, out, release=8)
        cls.game = jvmkit.make_jar(cls.d.data / "game.jar", folders=[out], skip=("com/mod", "org", "net"))
        cls.mod = jvmkit.make_jar(cls.d.data / "mod" / "mod.jar", folders=[out], skip=("com/game", "org", "net"),
                                  entries={"mod.mixins.json": json.dumps({"package": "com.mod.mixins",
                                                                          "mixins": ["MixinBase"]})})

    @classmethod
    def tearDownClass(cls):
        cls.d.stop()

    def test_references_through_javac_output(self):
        result = self.c.submit("jar.references", {
            "jars": str(self.mod), "hierarchy": str(self.game),
            "targets": ["com.game.Base", "com.game.Base::render(I)V", "com.game.Base::tick()V"]}).result(timeout=180)
        self.assertEqual(result.outcome, "ok", result.summary)
        base, render, tick = result.data.targets
        self.assertEqual(sorted(r["class"] for r in base["subclass"]), ["com.mod.Leaf", "com.mod.Mid"])
        self.assertEqual([r["class"] for r in render["override"]], ["com.mod.Leaf"])
        self.assertEqual([(r["class"], r["how"]) for r in render["call"]], [("com.mod.Caller", "virtual")])
        self.assertIn(("com.mod.Caller", "handle"), [(r["class"], r["how"]) for r in tick["call"]])

    def test_index_through_javac_output(self):
        result = self.c.submit("jar.index", {"jars": str(self.mod)}).result(timeout=180)
        self.assertEqual(result.outcome, "ok", result.summary)
        self.assertEqual(result.data.targets, {"com.game.Base": ["mod.jar"]})


if __name__ == "__main__":
    unittest.main()
