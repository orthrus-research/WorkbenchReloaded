"""content: GroovyScript folders, recipe dumps and placement data, all written here: tiny script folders and
logs, JARs whose class files come from the test's own writer, JSON-lines dumps and CSVs of points. The readers
are checked against the formats and against the rules GroovyScript applies, not against themselves."""

import io
import json
import math
import random
import shutil
import struct
import sys
import tempfile
import textwrap
import unittest
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "tests"))
sys.path.insert(0, str(REPO / "modules" / "content"))
from harness import Daemon  # noqa: E402
from wb_content import classfile, logs, mods, placement, recipes, runconfig, syntax  # noqa: E402
from wb_content.lexer import STRING, tokenize  # noqa: E402
from wb_content.rules import Rules, parse  # noqa: E402
from wb_content.scripts import Analysis  # noqa: E402

CONTAINER = "com/cleanroommc/groovyscript/compat/mods/InternalModContainer"


def write(root: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")
    return root


def run_config(**loaders) -> str:
    return json.dumps({"packId": "demo", "loaders": loaders})


def analyse(root: Path, mods_present=None, *, packmode="", side="client", file_system="windows", rules=None,
            log=None, aliases=None) -> tuple[dict, Analysis]:
    listed = None
    if mods_present is not None:
        listed = mods.Mods()
        for mod in mods_present:
            listed.add(mod, "test")
        listed.aliases = dict(aliases or {})
    analysis = Analysis(root, packmode=packmode, side=side, file_system=file_system, mods=listed,
                        platform=["minecraft", "forge"], rules=rules or Rules())
    return analysis.run(log), analysis


class Temporary(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="wb-content-"))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)


# -- a class file writer -------------------------------------------------------------------------------------
class Pool:
    def __init__(self):
        self.entries: list[bytes] = []
        self.index: dict[tuple, int] = {}

    def add(self, key: tuple, data: bytes) -> int:
        if key not in self.index:
            self.entries.append(data)
            self.index[key] = len(self.entries)
        return self.index[key]

    def utf8(self, text: str) -> int:
        raw = text.encode()
        return self.add(("utf8", text), b"\x01" + struct.pack(">H", len(raw)) + raw)

    def cls(self, name: str) -> int:
        return self.add(("class", name), b"\x07" + struct.pack(">H", self.utf8(name)))

    def string(self, text: str) -> int:
        return self.add(("string", text), b"\x08" + struct.pack(">H", self.utf8(text)))

    def methodref(self, owner: str, name: str, descriptor: str) -> int:
        pair = self.add(("nat", name, descriptor), b"\x0c" + struct.pack(">HH", self.utf8(name), self.utf8(descriptor)))
        return self.add(("method", owner, name, descriptor), b"\x0a" + struct.pack(">HH", self.cls(owner), pair))


def class_file(pool: Pool, name: str, annotation: tuple[str, dict] | None = None, clinit: bytes | None = None) -> bytes:
    this, parent = pool.cls(name), pool.cls("java/lang/Object")
    attributes = b""
    count = 0
    if annotation:
        kind, values = annotation
        body = struct.pack(">HHH", 1, pool.utf8(kind), len(values))
        for key, value in values.items():
            body += struct.pack(">H", pool.utf8(key)) + b"s" + struct.pack(">H", pool.utf8(value))
        attributes += struct.pack(">HI", pool.utf8("RuntimeVisibleAnnotations"), len(body)) + body
        count += 1
    methods = struct.pack(">H", 0)
    if clinit is not None:
        code = struct.pack(">HHI", 8, 1, len(clinit)) + clinit + struct.pack(">HH", 0, 0)
        methods = struct.pack(">HHHHH", 1, 0x0008, pool.utf8("<clinit>"), pool.utf8("()V"), 1) + \
            struct.pack(">HI", pool.utf8("Code"), len(code)) + code
    head = b"\xca\xfe\xba\xbe" + struct.pack(">HHH", 0, 52, len(pool.entries) + 1) + b"".join(pool.entries)
    return head + struct.pack(">HHHH", 0x21, this, parent, 0) + struct.pack(">H", 0) + methods + \
        struct.pack(">H", count) + attributes


def containers(pool: Pool, entries: list[tuple[str, str, list[str]]]) -> bytes:
    """GroovyScript's way of declaring compat: new InternalModContainer(modid, name, supplier, aliases...)."""
    code = b"\x03\xaa" + b"\x00\x00" + struct.pack(">iii", 6, 0, 0) + struct.pack(">i", 6)  # a tableswitch first
    for modid, name, aliases in entries:
        code += b"\xbb" + struct.pack(">H", pool.cls(CONTAINER)) + b"\x59"
        code += b"\x12" + bytes([pool.string(modid)]) + b"\x13" + struct.pack(">H", pool.string(name)) + b"\x01"
        descriptor = "(Ljava/lang/String;Ljava/lang/String;Lcom/google/common/base/Supplier;"
        if aliases:
            code += bytes([0x03 + len(aliases)]) + b"\xbd" + struct.pack(">H", pool.cls("java/lang/String"))
            for index, alias in enumerate(aliases):
                code += b"\x59" + bytes([0x03 + index]) + b"\x13" + struct.pack(">H", pool.string(alias)) + b"\x53"
            descriptor += "[Ljava/lang/String;"
        code += b"\xb7" + struct.pack(">H", pool.methodref(CONTAINER, "<init>", descriptor + ")V")) + b"\x57"
    return code + b"\xb1"


def jar(path: Path, entries: dict[str, bytes | str]) -> Path:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(buffer.getvalue())
    return path


def groovyscript_jar(path: Path) -> Path:
    pool = Pool()
    code = containers(pool, [("thermalexpansion", "Thermal Expansion", ["te", "thermal"]),
                             ("appliedenergistics2", "Applied Energistics 2", ["ae2"]), ("mekanism", "Mekanism", [])])
    support = class_file(pool, "com/cleanroommc/groovyscript/compat/mods/ModSupport", clinit=code)
    return jar(path, {"mcmod.info": json.dumps([{"modid": "groovyscript"}]),
                      "com/cleanroommc/groovyscript/compat/mods/ModSupport.class": support})


# -- reading Groovy ------------------------------------------------------------------------------------------
class Lexing(unittest.TestCase):
    def test_strings_and_comments_hide_code(self):
        source = textwrap.dedent('''
            // mods.hidden.registry.add(item('a:b'))
            /* crafting.remove('x') */
            def a = "${item('in:interpolation')} and $name.path"
            def b = 'crafting.remove("y")'
            def c = """triple ${ "nested ${ 'deep' }" } quotes"""
            def d = $/dollar $$ slashy /$
            def e = /slashy \\/ (not division)/
            mods.real.registry.add(item('real:item'))
            ''')
        tokens, problems = tokenize(source)
        self.assertEqual(problems, [])
        calls = syntax.read(tokens).calls
        self.assertEqual([c.name for c in calls], [("mods", "real", "registry", "add"), ("item",)])
        strings = [t for t in tokens if t.kind == STRING]
        self.assertEqual([s.value for s in strings], [None, 'crafting.remove("y")', None, "dollar $ slashy ",
                                                       "slashy / (not division)", "real:item"])
        self.assertEqual(calls[0].line, 9)

    def test_division_and_unclosed_strings(self):
        tokens, problems = tokenize("def x = a / b / 2\ndef y = (c) / d\ndef s = 'open\n")
        self.assertEqual([t.text for t in tokens if t.text == "/"], ["/", "/", "/"])
        self.assertEqual([(p.line, p.message) for p in problems], [(3, "a string is not closed on its line")])
        tokens, problems = tokenize('def t = """never closed\n')
        self.assertEqual([p.message for p in problems], ["a string is not closed"])


class Reading(unittest.TestCase):
    def read(self, text: str) -> syntax.Source:
        return syntax.read(tokenize(textwrap.dedent(text).lstrip("\n"))[0])

    def test_calls_chains_and_command_expressions(self):
        source = self.read('''
            crafting.remove 'minecraft:stick'
            mods.gregtech.mixer.recipeBuilder()
                .inputs(ore('dustA'))
                .outputs(item('a:b') * 2)
                .buildAndRegister()
            def helper(String id) {
                item(id)
            }
            mods.x.y.streamRecipes().filter { r -> r.ok }.removeAll()
            ''')
        heads = [c for c in source.calls if not c.chained]
        self.assertEqual(heads[0].name, ("crafting", "remove"))
        self.assertEqual(source.literal(heads[0].args[0]), "minecraft:stick")
        chain = source.chain(heads[1])
        self.assertEqual([c.name[-1] for c in chain], ["recipeBuilder", "inputs", "outputs", "buildAndRegister"])
        self.assertEqual(source.inner(chain[2].args[0]).name, ("item",))
        self.assertNotIn(("helper",), [c.name for c in source.calls])  # a declaration, not a call
        stream = [c for c in heads if c.name[0] == "mods" and c.name[-1] == "streamRecipes"][0]
        self.assertEqual([c.name[-1] for c in source.chain(stream)], ["streamRecipes", "filter", "removeAll"])

    def test_aliases_and_guards(self):
        source = self.read('''
            def te = mods.thermalexpansion
            te.pulverizer.add(item('a:b'), item('c:d'))
            if (mods.ae2.isLoaded()) {
                item('appliedenergistics2:material')
            }
            if (!Loader.isModLoaded('mekanism')) return
            item('mekanism:ingot')
            ''')
        self.assertEqual(source.resolve(("te", "pulverizer", "add")), ("mods", "thermalexpansion", "pulverizer", "add"))
        guarded = {source.tokens[c.start].line: source.guarded(c.start) for c in source.calls if c.name == ("item",)}
        self.assertEqual(guarded, {2: set(), 4: {"ae2"}, 7: {"mekanism"}})


# -- GroovyScript's rules --------------------------------------------------------------------------------------
class LoadOrder(Temporary):
    def test_specific_paths_and_the_separator_order_files(self):
        root = write(self.root, {
            "runConfig.json": run_config(preInit=["classes/", "preInit/"],
                                         postInit=["postInit/first.groovy", "postInit", "postInit/last.groovy"]),
            "classes/Util.groovy": "", "preInit/items.gvy": "", "postInit/last.groovy": "", "postInit/first.groovy": "",
            "postInit/a/x.groovy": "", "postInit/aB.groovy": "", "postInit/main.groovy": "", "postInit/notes.txt": "",
            "postInit/Z.groovy": ""})
        config = runconfig.load(root / "runConfig.json")

        def order(fs):
            return [s.file for stage in runconfig.STAGES for s in runconfig.stage_files(root, config, stage, fs)]
        # Java sorts by the whole path string: on Windows \ (0x5c) sorts after B (0x42), / (0x2f) before it
        self.assertEqual(order("windows"), ["classes/Util.groovy", "preInit/items.gvy", "postInit/first.groovy",
                                            "postInit/Z.groovy", "postInit/aB.groovy", "postInit/a/x.groovy",
                                            "postInit/main.groovy", "postInit/last.groovy"])
        self.assertEqual(order("linux")[3:6], ["postInit/Z.groovy", "postInit/a/x.groovy", "postInit/aB.groovy"])
        report, analysis = analyse(root)
        self.assertFalse(report["otherFileSystems"]["linux"]["same"])
        self.assertIn("on linux the load order differs", " ".join(analysis.notes))

    def test_loader_conflicts_unknown_loaders_and_the_deprecated_classes_entry(self):
        config = runconfig.parse(json.dumps({
            "loaders": {"preInit": ["shared/", "preInit/"], "postInit": ["shared/deep", "postInit/", "postInit/"],
                        "postinit": ["late/"]},
            "classes": ["classes/"]}))
        self.assertEqual(config.loaders["preInit"], ["shared", "preInit", "classes"])
        self.assertEqual(config.loaders["postInit"], ["postInit"])  # shared/deep is dropped, the repeat kept once
        kinds = [p["kind"] for p in config.problems]
        self.assertEqual(sorted(kinds), ["classes", "multiple-loaders", "unknown-loader"])
        lenient = runconfig.parse("{\n// a comment\n'loaders': {'postInit': ['postInit/',],},\n}")
        self.assertEqual(lenient.loaders, {"postInit": ["postInit"]})
        self.assertEqual(lenient.problems[0]["kind"], "lenient")

    def test_a_path_that_matches_only_ignoring_case(self):
        root = write(self.root, {"runConfig.json": run_config(postInit=["PostInit/"]), "postInit/main.groovy": ""})
        report, analysis = analyse(root)
        self.assertEqual([r["file"] for r in report["order"]], ["postInit/main.groovy"])
        self.assertTrue(any("only on a case-insensitive file system" in f for f in analysis.findings))
        report, _ = analyse(root, file_system="linux")
        self.assertEqual(report["order"], [])

    def test_preprocessors_as_groovyscript_reads_them(self):
        found, problems = runconfig.preprocessors(textwrap.dedent('''
            // side: client
            /*
             MODS_LOADED: alpha, beta,
            */
            // Mods loaded : gamma
            //PACKMODE:hard,Expert Mode

            def x = 1
            // NO_RUN
            '''))
        self.assertEqual([(p.name, p.args, p.line) for p in found],
                         [("SIDE", ["client"], 2), ("MODS_LOADED", ["alpha", "beta"], 4),
                          ("PACKMODE", ["hard", "Expert Mode"], 7)])
        self.assertEqual([(p["kind"], p["line"]) for p in problems], [("unread-preprocessor", 6),
                                                                      ("late-preprocessor", 10)])
        found, problems = runconfig.preprocessors("/* NO_RUN */\n// DEBUG_ONLY\n")
        self.assertEqual(found, [])  # a one-line block comment ends the header at once
        self.assertIn("one-line /* */ comment", problems[0]["detail"])
        self.assertEqual(runconfig.packmode_name("ExpertMode"), runconfig.packmode_name("expert_mode"))


class Scripts(Temporary):
    def pack(self, files: dict[str, str], **loaders) -> Path:
        config = {"packId": "demo", "loaders": loaders or {"preInit": ["classes/"], "postInit": ["postInit/"]},
                  "packmode": {"values": ["normal", "hard"], "default": "normal"}}
        return write(self.root, {"runConfig.json": json.dumps(config), **files})

    def test_duplicates_count_only_where_both_declarations_run(self):
        root = self.pack({
            "postInit/common.groovy": """
                oreDict.add('ingotCopper', item('copper:ingot'))
                crafting.addShaped('stick', item('minecraft:stick'), [[item('minecraft:planks')]])
                content.createItem('dust').register()
                """,
            "postInit/normal.groovy": """
                // PACKMODE: normal
                crafting.addShaped('gear', item('minecraft:stick'), [[item('minecraft:iron_ingot')]])
                """,
            "postInit/hard.groovy": """
                // PACKMODE: hard
                crafting.shapedBuilder().name('gear').output(item('minecraft:stick')).shape('A').register()
                ore('ingotCopper').add(item('copper:ingot', 0))
                content.registerItem('dust', null)
                """})
        report, analysis = analyse(root)
        duplicates = {(d["kind"], d["id"]): d for d in report["duplicates"]}
        self.assertEqual(set(duplicates), {("ore dictionary entry", "ingotCopper copper:ingot:0"),
                                           ("item", "demo:dust")})  # gear is normal or hard, never both
        self.assertEqual(duplicates[("item", "demo:dust")]["configurations"], ["hard/client", "hard/server"])
        self.assertEqual(report["declared"]["recipe"], 2)  # counted in the selected configuration: normal/client
        self.assertIn("item demo:dust is declared 2 times (postInit/common.groovy:3, postInit/hard.groovy:4), "
                      "together in hard", analysis.findings)

    def test_a_packs_rules_declare_ids_through_constructors_and_helpers(self):
        root = self.pack({"classes/Mats.groovy": """
            package classes
            class Mats {
                static void register() {
                    Copper = new Material.Builder(8100, Util.id('copper')).ingot().build()
                    Tin = new Material.Builder(8100, Util.id('tin')).ingot().build()
                    Zinc = new Material.Builder(8102, Util.id('copper'))
                        .build()
                }
            }
            """}, preInit=["classes/"], postInit=[])
        rules = parse({"ids": [{"call": "Material.Builder", "arg": 0, "kind": "material id"},
                               {"call": "Material.Builder", "arg": 1, "kind": "material name"}]}, "test")
        report, analysis = analyse(root, rules=rules)
        self.assertEqual({(d["kind"], d["id"]): [p["line"] for p in d["places"]] for d in report["duplicates"]},
                         {("material id", "8100"): [4, 5], ("material name", "copper"): [4, 6]})
        self.assertEqual(report["recipes"]["additions"], 0)  # a constructor is not a registry call

    def test_recipe_counts_by_file_and_mod_and_unregistered_builders(self):
        root = self.pack({
            "postInit/main.groovy": """
                crafting.remove('minecraft:stick')
                crafting.removeByOutput(item('minecraft:torch'))
                furnace.add(item('minecraft:sand'), item('minecraft:glass'))
                mods.te.pulverizer.add(item('minecraft:iron_ore'), item('thermalfoundation:material'))
                mods.ae2.inscriber.recipeBuilder().input(item('a:b')).output(item('a:c'))
                def b = mods.ae2.inscriber.recipeBuilder()
                mods.jei.description.add(item('a:b'), 'text')
                mods.mekanism.crusher.streamRecipes().filter { it }.removeAll()
                crafting.replaceShaped('torch', item('minecraft:torch'), [[item('minecraft:coal')]])
                """})
        rules = parse({"not_recipes": ["jei.*"]}, "test")
        report, analysis = analyse(root, ["thermalexpansion", "appliedenergistics2", "mekanism", "jei",
                                          "thermalfoundation", "a"], rules=rules,
                                   aliases={"te": "thermalexpansion", "ae2": "appliedenergistics2"})
        recipes_ = report["recipes"]
        self.assertEqual(recipes_["byFile"]["postInit/main.groovy"], {
            "add": {"minecraft.crafting": 1, "minecraft.furnace": 1, "thermalexpansion.pulverizer": 1},
            "remove": {"mekanism.crusher": 1, "minecraft.crafting": 3}})
        self.assertEqual(recipes_["byMod"]["minecraft"], {"remove": 3, "add": 2})
        self.assertEqual(recipes_["unregistered"], [{"file": "postInit/main.groovy", "line": 5,
                                                     "registry": "appliedenergistics2.inscriber"}])
        self.assertEqual(report["references"]["missing"], [])
        self.assertEqual(sum("never registered" in f for f in analysis.findings), 1)  # line 6 keeps its builder

    def test_references_to_absent_mods_guards_and_malformed_ids(self):
        root = self.pack({
            "postInit/guarded.groovy": """
                // MODS_LOADED: mekanism
                mods.mekanism.crusher.add(item('mekanism:ore'), item('mekanism:dust'))
                """,
            "postInit/main.groovy": """
                mods.thermal.pulverizer.add(item('minecraft:iron_ore'), item('minecraft:iron_ingot'))
                if (mods.botania.isLoaded()) {
                    mods.botania.pure_daisy.add(item('botania:x'), item('botania:y'))
                }
                item('stone')
                item('demo:own_item')
                metaitem('dust.copper')
                """})
        rules = parse({"mappers": {"metaitem": "gregtech"}}, "test")
        report, analysis = analyse(root, ["thermalexpansion"], rules=rules, aliases={"thermal": "thermalexpansion"})
        self.assertEqual([m["mod"] for m in report["references"]["missing"]], ["gregtech"])
        self.assertEqual(report["references"]["malformed"][0]["via"], "item('stone')")
        self.assertEqual(report["files"]["postInit/guarded.groovy"]["runsIn"], [])  # mekanism is absent
        self.assertTrue(any("refers to mod gregtech (metaitem(...))" in f for f in analysis.findings))
        unchecked, _ = analyse(root)
        self.assertFalse(unchecked["references"]["checked"])

    def test_import_cycles_statics_and_unresolved_imports(self):
        root = self.pack({
            "classes/A.groovy": "package classes\nimport classes.B\nclass A { B peer }\n",
            "classes/B.groovy": "package classes\nclass B { A peer }\n",
            "classes/Maps.groovy": "package classes\nclass Maps {\n static final MIXER = mods.gregtech.mixer\n}\n",
            "postInit/main.groovy": """
                import classes.Missing
                import static classes.Maps.*
                MIXER.recipeBuilder().inputs(item('a:b')).buildAndRegister()
                """}, preInit=[], postInit=["postInit/"])
        report, analysis = analyse(root)
        self.assertEqual(report["imports"]["cycles"], [])  # A and B are imported by nothing that runs
        self.assertEqual(report["onDemand"], ["classes/Maps.groovy"])
        self.assertEqual(report["recipes"]["byRegistry"], {"gregtech.mixer": {"add": 1}})
        self.assertTrue(any("imports classes.Missing, which no script" in f for f in analysis.findings))
        (root / "runConfig.json").write_text(run_config(preInit=["classes/"], postInit=["postInit/"]))
        report, analysis = analyse(root)
        self.assertEqual(report["imports"]["cycles"][0]["cycle"],
                         ["classes/A.groovy", "classes/B.groovy", "classes/A.groovy"])
        self.assertIn("import cycle: classes/A.groovy -> classes/B.groovy -> classes/A.groovy", analysis.findings)

    def test_a_log_is_mapped_to_files_and_lines(self):
        root = self.pack({"postInit/main.groovy": "furnace.add(item('a:b'), item('c:d'))\n",
                          "postInit/other.groovy": "crafting.remove('x:y')\n"})
        log = self.root / "logs" / "groovy.log"
        write(self.root, {"logs/groovy.log": """
            [10:00:01] [CLIENT/INFO] [groovyscript]: Running scripts in loader 'postInit'
            [10:00:01] [CLIENT/INFO] [groovyscript]:  - running script postInit.main
            [10:00:01] [CLIENT/ERROR] [postInit.main:1]: Error adding Furnace recipe: - output must not be empty
            [10:00:01] [CLIENT/INFO] [groovyscript]:  - running script postInit.other
            [10:00:02] [CLIENT/ERROR] [postInit.other]: An exception occurred while running scripts.
            \tgroovy.lang.MissingMethodException: No signature of method
            \t\tat postInit.other.run(other.groovy:1)
            [10:00:03] [CLIENT/WARN] [postInit.other]: something odd
            [10:00:03] [main/ERROR] [FML]: an unrelated mod's error
            """})
        report, analysis = analyse(root, log=log)
        found = report["log"]
        self.assertEqual([e["at"] for e in found["errors"]], [[{"file": "postInit/main.groovy", "line": 1}],
                                                               [{"file": "postInit/other.groovy", "line": 1}]])
        self.assertEqual((len(found["warnings"]), found["other"]["errors"]), (1, 1))
        self.assertTrue(found["matchesOrder"])
        self.assertTrue(any(f.startswith("log line 3: error at postInit/main.groovy:1") for f in analysis.findings))


class Logs(Temporary):
    def test_frames_and_compiler_errors_name_their_files(self):
        resolve = logs.Resolver(["postInit/main.groovy", "postInit/extra/main.groovy", "preInit/items.groovy"], "groovy")
        log = write(self.root, {"latest.log": """
            [10:00:00] [main/INFO]: starting
            [10:00:04] [Client thread/ERROR] [groovyscript]: Error loading script preInit/items.groovy
            org.codehaus.groovy.control.MultipleCompilationErrorsException: startup failed:
            file:/srv/game/groovy/preInit/items.groovy: 7: unexpected token: } @ line 7, column 1.
            [10:00:05] [Client thread/WARN] [groovyscript]: closure failed
            \tat postInit.extra.main$_run_closure1.doCall(main.groovy:14)
            [10:00:06] [Client thread/ERROR] [groovyscript]: in main.groovy in line 3
            [10:00:07] [Server thread/WARN] [somemod]: not about scripts
            """}) / "latest.log"
        entries, ran, count = logs.read(log, resolve)
        self.assertEqual([(e.level, e.places, e.groovy) for e in entries], [
            ("ERROR", [("preInit/items.groovy", 7)], True), ("WARN", [("postInit/extra/main.groovy", 14)], True),
            ("ERROR", [], True), ("WARN", [], False)])  # two files are named main.groovy: not placed
        self.assertEqual((ran, count), ([], 8))
        self.assertEqual(logs.class_name("postInit/extra/main.groovy"), "postInit.extra.main")


class Jars(Temporary):
    def test_mod_ids_from_metadata_and_from_annotations(self):
        folder = self.root / "mods"
        jar(folder / "a.jar", {"mcmod.info": json.dumps({"modListVersion": 2, "modList": [{"modid": "alpha"}]})})
        jar(folder / "b.jar", {"META-INF/mods.toml": '[[mods]]\nmodId = "beta"\n'})
        pool = Pool()
        annotated = class_file(pool, "g/Main", ("Lnet/minecraftforge/fml/common/Mod;", {"modid": "gamma"}))
        jar(folder / "1.12.2" / "c.jar", {"mcmod.info": '[{"modid": "${modid}"}]', "g/Main.class": annotated})
        jar(folder / "disabled" / "d.jar", {"mcmod.info": json.dumps([{"modid": "delta"}])})
        (folder / "broken.jar").write_bytes(b"not a zip")
        groovyscript_jar(folder / "groovyscript.jar")
        found = mods.read(folder, ["extra"], "auto")
        self.assertEqual(sorted(found.ids), ["alpha", "beta", "extra", "gamma", "groovyscript"])
        self.assertEqual(found.aliases, {"thermalexpansion": "thermalexpansion", "te": "thermalexpansion",
                                         "thermal": "thermalexpansion", "appliedenergistics2": "appliedenergistics2",
                                         "ae2": "appliedenergistics2", "mekanism": "mekanism"})
        self.assertEqual(len(found.problems), 1)
        self.assertEqual(classfile.mod_ids(annotated), ["gamma"])

    def test_ids_from_a_list(self):
        listed = write(self.root, {"mods.txt": "# a comment\nalpha@1.0\nbeta 2.0\n\n",
                                   "mods.json": '["x", {"modId": "y"}]'})
        self.assertEqual(sorted(mods.read(listed / "mods.txt", [], "auto").ids), ["alpha", "beta"])
        self.assertEqual(sorted(mods.read(listed / "mods.json", [], "auto").ids), ["x", "y"])


# -- recipe graphs and placement --------------------------------------------------------------------------------
def dump(path: Path, rows: list, extra: str = "") -> Path:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n" + extra, encoding="utf-8")
    return path


RECIPES = [
    {"type": "smelting", "inputs": ["minecraft:iron_ore"], "outputs": ["minecraft:iron_ingot"]},
    {"type": "crafting", "inputs": [{"id": "minecraft:iron_ingot", "count": 9}], "outputs": ["minecraft:iron_block"]},
    {"type": "crafting", "inputs": ["minecraft:iron_block"], "outputs": [{"id": "minecraft:iron_ingot", "count": 9}]},
    {"type": "crafting", "id": "stick", "inputs": ["minecraft:planks:*"], "outputs": ["minecraft:stick"],
     "removed": True, "source": "postInit/main.groovy:3"},
    {"type": "crafting", "id": "torch", "inputs": ["minecraft:coal", "minecraft:stick"],
     "outputs": ["minecraft:torch"]},
    {"type": "crafting", "inputs": [{"any": ["minecraft:log:0", "minecraft:log:1"]}],
     "outputs": ["minecraft:planks:2"]},
    {"type": "press", "inputs": ["a:x"], "outputs": ["a:y"]},
    {"type": "press", "inputs": ["a:y"], "outputs": ["a:x"]},
    {"type": "press", "inputs": [{"id": "a:mold", "consumed": False}, "a:x"], "outputs": ["a:mold", "a:z"]},
]


class Graph(Temporary):
    def test_dead_ends_cycles_and_items_only_removed_recipes_made(self):
        found = recipes.analyse(recipes.read(dump(self.root / "d.jsonl", RECIPES, "not json\n"), ignore=[], types=[]),
                                obtainable=[])
        report = found.report
        self.assertEqual([d["id"] for d in report["deadEnds"]],
                         ["minecraft:coal", "minecraft:iron_ore", "minecraft:log:0 | minecraft:log:1"])
        self.assertEqual([(r["id"], r["neededBy"]) for r in report["removedOnly"]], [("minecraft:stick", 1)])
        cycles = {tuple(c["items"]): c["closed"] for c in report["cycles"]}
        self.assertEqual(cycles, {("a:x", "a:y"): True, ("minecraft:iron_block", "minecraft:iron_ingot"): False})
        self.assertEqual(report["unreached"], ["a:mold", "a:x", "a:y", "a:z", "minecraft:torch"])
        self.assertEqual(len(found.findings), 2)  # the stick and the unreadable line; dead ends need obtainable
        self.assertIn("an active recipe still needs it", found.findings[0])

    def test_with_what_the_world_supplies_dead_ends_and_closed_cycles_are_defects(self):
        rows = RECIPES + [{"type": "crafting", "inputs": ["minecraft:planks:2"], "outputs": ["minecraft:chest"]}]
        found = recipes.analyse(recipes.read(dump(self.root / "d.jsonl", rows), ignore=["minecraft:coal"], types=[]),
                                obtainable=["minecraft:*_ore", "minecraft:log:*"])
        self.assertEqual(sorted(f.split(":", 1)[0] for f in found.findings), ["closed cycle", "minecraft"])
        self.assertNotIn("minecraft:chest", found.report["unreached"])  # planks:2 from either log
        typed = recipes.read(dump(self.root / "t.jsonl", rows), ignore=[], types=["smelting"])
        self.assertEqual((len(typed.recipes), typed.skipped), (1, 9))


def points_csv(path: Path, rows: list[tuple], header: str = "x,z,kind") -> Path:
    path.write_text(header + "\n" + "\n".join(",".join(str(v) for v in row) for row in rows) + "\n", encoding="utf-8")
    return path


def patterns(seed: int = 4) -> list[tuple]:
    rng = random.Random(seed)
    rows = [(rng.randrange(512), rng.randrange(512), "random") for _ in range(400)]
    rows += [(gx + 16, gz + 16, "grid") for gx in range(0, 512, 32) for gz in range(0, 512, 32)]
    for _ in range(10):
        cx, cz = rng.randrange(40, 472), rng.randrange(40, 472)
        rows += [(cx + rng.randint(-6, 6), cz + rng.randint(-6, 6), "clump") for _ in range(30)]
    return rows


class Placement(Temporary):
    def test_random_regular_and_clumped_points(self):
        data = placement.read(points_csv(self.root / "p.csv", patterns()), x="x", z="z", kind="kind", kinds=[])
        found = placement.analyse(data, area=[0, 0, 512, 512], quadrat=32, edge="donnelly", ranges={}, min_points=5)
        kinds = found.report["kinds"]
        self.assertEqual({k: kinds[k]["pattern"] for k in ("random", "grid", "clump")},
                         {"random": "random", "grid": "regular", "clump": "clumped"})
        self.assertEqual({k: kinds[k]["dispersion_pattern"] for k in ("random", "grid", "clump")},
                         {"random": "random", "grid": "regular", "clump": "patchy"})
        self.assertAlmostEqual(kinds["random"]["clark_evans"], 1.0, delta=0.1)
        self.assertEqual((kinds["grid"]["nn_mean"], kinds["grid"]["per_chunk"]), (32.0, 0.25))
        self.assertEqual(kinds["grid"]["quadrats"], 256)
        self.assertEqual(found.findings, [])

    def test_nearest_neighbours_match_a_brute_force_search(self):
        rng = random.Random(9)
        points = [(rng.uniform(0, 300), rng.uniform(0, 40)) for _ in range(300)] + [(5.0, 5.0), (5.0, 5.0)]
        brute = [min(math.hypot(px - qx, pz - qz) for j, (qx, qz) in enumerate(points) if j != i)
                 for i, (px, pz) in enumerate(points)]
        self.assertEqual(placement.nearest(points), brute)

    def test_expected_ranges_and_unreadable_rows(self):
        rows = patterns() + [("east", 3, "random")]
        data = placement.read(points_csv(self.root / "p.csv", rows), x="x", z="z", kind="kind", kinds=["clump", "rare"])
        ranges = placement.ranges({"*": {"clark_evans": [0.8, None]}, "rare": {"points": [1, None]}})
        found = placement.analyse(data, area=None, quadrat=32, edge="none", ranges=ranges, min_points=5)
        self.assertEqual(sorted(found.report["kinds"]), ["clump"])
        self.assertTrue(found.report["area"]["assumed"])
        self.assertEqual(len(found.findings), 3)  # clump below 0.8, no rare point, one unreadable row
        with self.assertRaises(ValueError):
            placement.ranges({"tree": {"height": [1, 2]}})
        self.assertAlmostEqual(placement._upper_gamma(1.0, 2.0), math.exp(-2.0), places=12)
        self.assertAlmostEqual(placement._upper_gamma(5.0, 3.0), 0.8152632445237722, places=10)


# -- the commands, through the daemon ---------------------------------------------------------------------------
class Commands(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = Daemon('[modules.content.rulesets.pack]\nmappers = { metaitem = "gregtech" }\n').start()
        cls.c = cls.d.client
        data = cls.d.data
        cls.pack = write(data / "pack", {
            "groovy/runConfig.json": json.dumps({"packId": "demo", "loaders": {
                "preInit": ["classes/", "preInit/"], "postInit": ["postInit/"]},
                "packmode": {"values": ["normal", "hard"], "default": "normal"}}),
            "groovy/classes/Shared.groovy": "package classes\nclass Shared {}\n",
            "groovy/postInit/main.groovy": """
                import classes.Shared
                crafting.remove('minecraft:stick')
                crafting.addShaped('gear', item('minecraft:stick'), [[item('minecraft:iron_ingot')]])
                mods.te.pulverizer.add(item('minecraft:iron_ore'), item('thermalfoundation:material', 128))
                metaitem('plate.iron')
                """,
            "groovy/postInit/hard.groovy": """
                // PACKMODE: hard
                crafting.addShaped('gear', item('minecraft:stick'), [[item('minecraft:gold_ingot')]])
                """,
            "groovy/postInit/copy.groovy": """
                crafting.addShaped('gear', item('minecraft:stick'), [[item('minecraft:diamond')]])
                """,
            "logs/groovy.log": """
                [10:00:01] [CLIENT/ERROR] [postInit.main:4]: Error adding Pulverizer recipe
                """,
            "mods.txt": "thermalexpansion\nthermalfoundation\n"})
        groovyscript_jar(data / "pack" / "mods" / "groovyscript-1.4.3.jar")
        jar(data / "pack" / "mods" / "te.jar", {"mcmod.info": json.dumps([{"modid": "thermalexpansion"},
                                                                          {"modid": "thermalfoundation"}])})
        cls.clean = write(data / "clean", {
            "groovy/runConfig.json": run_config(postInit=["postInit/"]),
            "groovy/postInit/main.groovy": "furnace.add(item('minecraft:sand'), item('minecraft:glass'))\n"})
        dump(data / "recipes.jsonl", RECIPES)
        points_csv(data / "points.csv", patterns())

    @classmethod
    def tearDownClass(cls):
        cls.d.stop()

    def run_command(self, command: str, out: str | None = None, **params):
        return self.c.submit(command, params, out=str(self.d.out / out) if out else None).result(timeout=120)

    def report(self, out: str) -> dict:
        return json.loads((self.d.out / out / "report.json").read_text(encoding="utf-8"))

    def test_scripts_finds_duplicates_missing_mods_and_log_errors(self):
        result = self.run_command("content.scripts", "scripts", groovy=str(self.pack), mods=str(self.pack / "mods"),
                                  log=str(self.pack / "logs" / "groovy.log"), ruleset="pack")
        self.assertEqual((result.outcome, result.code), ("finding", 1), result.summary)
        self.assertEqual(result.data.files, 4)
        self.assertEqual(result.data.order, ["preInit:classes/Shared.groovy", "postInit:postInit/copy.groovy",
                                             "postInit:postInit/main.groovy"])
        self.assertEqual(result.data.missing, ["gregtech"])
        self.assertEqual(result.data.recipes["byMod"], {"minecraft": {"remove": 1, "add": 2},
                                                        "thermalexpansion": {"add": 1}})
        findings = "\n".join(result.findings)
        self.assertIn("recipe minecraft.crafting demo:gear is declared 3 times (postInit/copy.groovy:1, "
                      "postInit/hard.groovy:2 in hard, postInit/main.groovy:3)", findings)
        self.assertIn("log line 1: error at postInit/main.groovy:4: Error adding Pulverizer recipe", findings)
        report = self.report("scripts")
        self.assertEqual(report["schema"], "wb/content-scripts/v1")
        self.assertEqual(report["references"]["modList"]["aliasesFrom"], "groovyscript-1.4.3.jar")
        hard = self.run_command("content.scripts", groovy=str(self.pack), packmode="hard", side="server")
        self.assertIn("postInit:postInit/hard.groovy", hard.data.order)

    def test_a_clean_pack_settles_ok(self):
        result = self.run_command("content.scripts", groovy=str(self.clean / "groovy"), mod_ids=["groovyscript"])
        self.assertEqual(result.outcome, "ok", result.summary)
        self.assertIn("1 script: 1 run in client on windows", result.summary)
        self.assertEqual(result.data.recipes["additions"], 1)

    def test_scripts_refuses_what_it_cannot_use(self):
        wrong = self.run_command("content.scripts", groovy=str(self.pack), packmode="nightmare")
        self.assertEqual(wrong.outcome, "usage")
        self.assertIn("nightmare", wrong.summary)
        unknown = self.run_command("content.scripts", groovy=str(self.pack), ruleset="absent")
        self.assertEqual(unknown.outcome, "usage")

    def test_recipe_graph(self):
        result = self.run_command("content.recipe-graph", "graph", recipes=str(self.d.data / "recipes.jsonl"),
                                  obtainable=["minecraft:*_ore", "minecraft:log:*", "minecraft:coal"])
        self.assertEqual(result.outcome, "finding", result.summary)
        self.assertEqual(result.data.removed_only, ["minecraft:stick"])
        self.assertEqual(len(result.findings), 2)  # the stick, and the a:x <-> a:y cycle nothing leads into
        self.assertEqual(self.report("graph")["counts"]["removed"], 1)

    def test_placement(self):
        result = self.run_command("content.placement", "placement", points=str(self.d.data / "points.csv"),
                                  area=[0, 0, 512, 512], expect={"random": {"clark_evans": [0.9, 1.1]}})
        self.assertEqual(result.outcome, "ok", result.summary)
        self.assertEqual(result.data.kinds["grid"]["pattern"], "regular")
        clumped = self.run_command("content.placement", points=str(self.d.data / "points.csv"),
                                   expect={"clump": {"clark_evans": [0.9, 1.1]}})
        self.assertEqual(clumped.outcome, "finding")
        self.assertIn("clump: clark_evans", clumped.findings[0])


if __name__ == "__main__":
    unittest.main()
