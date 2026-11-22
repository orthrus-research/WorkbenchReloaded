"""assets: content generated from a manifest (written and checked), reference blocks copied from a tiny fake client
JAR into a pack complete but for its textures, a fake mod's blocks sorted by shape, and texture metrics on
synthetic faces; then the three commands through the daemon."""

import json
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO / "tests"))
sys.path.insert(0, str(HERE.parent))

from harness import Daemon  # noqa: E402

from wb_assets import blockstates, content  # noqa: E402

try:
    import numpy as np
    from PIL import Image
except ImportError:  # texture metrics need both
    np = Image = None


def element(*faces: str) -> list:
    return [{"from": [0, 0, 0], "to": [16, 16, 16], "faces": {f: {"texture": f"#{v}"} for f, v in
                                                              (pair.split("=") for pair in faces)}}]


PLANKS = {"bottom": "blocks/planks_oak", "top": "blocks/planks_oak", "side": "blocks/planks_oak"}
CLIENT = {
    "blockstates/oak_stairs.json": {"variants": {
        "facing=east,half=bottom,shape=straight": {"model": "oak_stairs"},
        "facing=west,half=bottom,shape=straight": {"model": "oak_stairs", "y": 180, "uvlock": True},
        "facing=east,half=bottom,shape=inner_left": {"model": "oak_inner_stairs", "y": 270, "uvlock": True},
        "facing=east,half=bottom,shape=outer_left": {"model": "oak_outer_stairs", "y": 270, "uvlock": True}}},
    "models/block/oak_stairs.json": {"parent": "block/stairs", "textures": PLANKS},
    "models/block/oak_inner_stairs.json": {"parent": "block/inner_stairs", "textures": PLANKS},
    "models/block/oak_outer_stairs.json": {"parent": "block/outer_stairs", "textures": PLANKS},
    "models/block/block.json": {"display": {}},
    "models/block/stairs.json": {"parent": "block/block", "textures": {"particle": "#side"},
                                 "elements": element("down=bottom", "up=top", "north=side")},
    "models/block/inner_stairs.json": {"parent": "block/block", "textures": {"particle": "#side"},
                                       "elements": element("down=bottom", "up=top", "east=side")},
    "models/block/outer_stairs.json": {"parent": "block/block", "textures": {"particle": "#side"},
                                       "elements": element("down=bottom", "up=top", "west=side")},
    "models/item/oak_stairs.json": {"parent": "block/oak_stairs"},
    "blockstates/furnace.json": {"variants": {"facing=north": {"model": "furnace"},
                                              "facing=east": {"model": "furnace", "y": 90}}},
    "models/block/furnace.json": {"parent": "block/orientable", "textures": {
        "top": "blocks/furnace_top", "front": "blocks/furnace_front_off", "side": "blocks/furnace_side"}},
    "models/block/orientable.json": {"parent": "block/block", "textures": {"particle": "#front"},
                                     "elements": element("up=top", "north=front", "east=side", "down=top")},
    "models/item/furnace.json": {"parent": "block/furnace"},
    "blockstates/oak_fence.json": {"multipart": [
        {"apply": {"model": "oak_fence_post"}},
        {"when": {"north": "true"}, "apply": {"model": "oak_fence_n", "uvlock": True}}]},
    "models/block/oak_fence_post.json": {"parent": "block/fence_post", "textures": {"texture": "blocks/planks_oak"}},
    "models/block/oak_fence_n.json": {"parent": "block/fence_side", "textures": {"texture": "blocks/planks_oak"}},
    "models/block/fence_post.json": {"elements": element("up=texture", "north=texture")},
    "models/block/fence_side.json": {"elements": element("north=texture")},
    "models/block/oak_fence_inventory.json": {"parent": "block/fence_inventory",
                                              "textures": {"texture": "blocks/planks_oak"}},
    "models/block/fence_inventory.json": {"elements": element("up=texture")},
    "models/item/oak_fence.json": {"parent": "block/oak_fence_inventory"},
    "blockstates/stone_slab.json": {"variants": {
        "half=bottom,variant=stone": {"model": "half_slab_stone"},
        "half=top,variant=stone": {"model": "upper_slab_stone"}}},
    "models/block/half_slab_stone.json": {"parent": "block/half_slab", "textures": {
        "bottom": "blocks/stone_slab_top", "top": "blocks/stone_slab_top", "side": "blocks/stone_slab_side"}},
    "models/block/upper_slab_stone.json": {"parent": "block/upper_slab", "textures": {
        "bottom": "blocks/stone_slab_top", "top": "blocks/stone_slab_top", "side": "blocks/stone_slab_side"}},
    "models/block/half_slab.json": {"textures": {"particle": "#side"}, "elements": element("down=bottom", "up=top",
                                                                                          "north=side")},
    "models/block/upper_slab.json": {"textures": {"particle": "#side"}, "elements": element("down=bottom", "up=top")},
    "models/item/stone_slab.json": {"parent": "block/half_slab_stone"},
    "blockstates/sea_lantern.json": {"variants": {"normal": {"model": "sea_lantern"}}},
    "models/block/sea_lantern.json": {"parent": "block/cube_all", "textures": {"all": "blocks/sea_lantern"}},
    "models/block/cube_all.json": {"parent": "block/cube", "textures": {
        "particle": "#all", "down": "#all", "up": "#all", "north": "#all", "south": "#all", "west": "#all", "east": "#all"}},
    "models/block/cube.json": {"parent": "block/block", "elements": element("down=down", "up=up", "north=north",
                                                                            "south=south", "west=west", "east=east")},
    "models/item/sea_lantern.json": {"parent": "block/sea_lantern"},
    "textures/blocks/sea_lantern.png.mcmeta": {"animation": {"frametime": 5}},
    "blockstates/cauldron.json": {"variants": {"level=0": {"model": "cauldron_empty"}}},
    "models/block/cauldron_empty.json": {"textures": {"particle": "blocks/cauldron_side", "side": "blocks/cauldron_side",
                                                      "top": "blocks/cauldron_top"},
                                         "elements": element("up=top", "north=side")},
    "models/item/cauldron.json": {"parent": "item/generated", "textures": {"layer0": "items/cauldron"}},
    "models/item/generated.json": {"parent": "builtin/generated"},
    # a vanilla parent that names concrete textures itself: the copy must name them, or they stay vanilla's
    "blockstates/flower_pot.json": {"variants": {"contents=dandelion": {"model": "flower_pot_dandelion"}}},
    "models/block/flower_pot_dandelion.json": {"parent": "block/flower_pot_cross",
                                               "textures": {"plant": "blocks/flower_dandelion"}},
    "models/block/flower_pot_cross.json": {"textures": {"particle": "blocks/flower_pot", "flowerpot": "blocks/flower_pot",
                                                        "dirt": "blocks/dirt"},
                                           "elements": element("up=dirt", "north=flowerpot", "south=plant")},
}
MOD = {
    "blockstates/basalt.json": {"variants": {"normal": {"model": "examplemod:basalt"}}},
    "models/block/basalt.json": {"parent": "block/cube_all", "textures": {"all": "examplemod:blocks/basalt"}},
    "models/item/basalt.json": {"parent": "examplemod:block/basalt"},
    "blockstates/basalt_stairs.json": {"variants": {
        "facing=east,half=bottom,shape=straight": {"model": "examplemod:basalt_stairs"},
        "facing=east,half=bottom,shape=inner_left": {"model": "examplemod:basalt_inner_stairs"},
        "facing=east,half=bottom,shape=outer_left": {"model": "examplemod:basalt_outer_stairs"}}},
    "models/block/basalt_stairs.json": {"parent": "block/stairs", "textures": {"bottom": "examplemod:blocks/basalt",
                                                                              "top": "examplemod:blocks/basalt",
                                                                              "side": "examplemod:blocks/basalt"}},
    "models/block/basalt_inner_stairs.json": {"parent": "block/inner_stairs", "textures": {"side": "examplemod:blocks/basalt"}},
    "models/block/basalt_outer_stairs.json": {"parent": "block/outer_stairs", "textures": {"side": "examplemod:blocks/basalt"}},
    "blockstates/marble.json": {"variants": {"normal": [{"model": "examplemod:marble_0"}, {"model": "examplemod:marble_1"}]}},
    "models/block/marble_0.json": {"parent": "block/cube_all", "textures": {"all": "examplemod:blocks/marble_0"}},
    "models/block/marble_1.json": {"parent": "block/cube_all", "textures": {"all": "examplemod:blocks/marble_1"}},
    "blockstates/ore.json": {"forge_marker": 1, "defaults": {"model": "cube_all", "textures": {"all": "examplemod:blocks/ore"}},
                             "variants": {"normal": [{}], "inventory": [{}]}},
}
MOD_TEXTURES = ["blocks/basalt.png", "blocks/marble_0.png", "blocks/ore.png"]  # marble_1 is missing


def client_jar(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("net/minecraft/client/Minecraft.class", b"\xca\xfe\xba\xbe")
        for name, data in CLIENT.items():
            archive.writestr(f"assets/minecraft/{name}", json.dumps(data))
    return path


def mod_resources(root: Path) -> Path:
    for name, data in MOD.items():
        path = root / "assets" / "examplemod" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
    for name in MOD_TEXTURES:
        path = root / "assets" / "examplemod" / "textures" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x89PNG not checked here")
    return root


MANIFEST = """
namespace = "examplemod"
keep = ["blockstates/hand_*.json"]

[lang]
entries = { "itemGroup.examplemod" = "Example Mod" }

[[block]]
id = "basalt"
name = "Basalt"
textures = { all = "blocks/basalt" }

[[block]]
id = "rock"
name = "Rock"
textures = { all = "blocks/rock_{n}" }
alternates = 2

[[block]]
id = "spike"
name = "Spike"
shape = "cross"
properties = { size = ["small", "large"], direction = ["up", "down"], wet = [false, true] }
textures = { cross = "blocks/spike_{direction}_{size}" }

[[block]]
id = "litter"
shape = "plane"
height = 0.25
textures = { surface = "blocks/litter" }
item = "none"

[[block]]
id = "kiln"
name = "Kiln"
shape = "model"
parent = "block/cube_bottom_top"
textures = { top = "blocks/kiln_top", bottom = "minecraft:blocks/stone", side = "blocks/kiln_side" }

[[item]]
id = "seed"
name = "Seed"
textures = { layer0 = "items/seed" }

[[family]]
subjects = { ash = "Ash", elm = { name = "Elm", grain = "rough" } }

[[family.block]]
id = "log_{subject}"
name = "{name} Log"
shape = "log"
textures = { side = "blocks/wood/{subject}_bark", end = "blocks/wood/{subject}_end" }

[[family.block]]
id = "leaves_{subject}"
name = "{name} Leaves"
properties = { check_decay = [false, true], decayable = [false, true] }
textures = { all = "blocks/wood/{subject}_leaves_{n}" }
alternates = 2

[[family.item]]
id = "{subject}_seed"
name = "{name} Seed"
textures = { layer0 = "items/{subject}_seed" }
"""


def manifest(text: str = MANIFEST):
    import tomllib
    return content.load(tomllib.loads(text))


class Generate(unittest.TestCase):
    def test_every_shape_and_family(self):
        files = content.render(manifest())
        base = "assets/examplemod"

        def read(name):
            return json.loads(files[f"{base}/{name}"])

        self.assertEqual(read("blockstates/basalt.json"), {"variants": {"normal": {"model": "examplemod:basalt"}}})
        self.assertEqual(read("models/block/basalt.json"),
                         {"parent": "block/cube_all", "textures": {"all": "examplemod:blocks/basalt"}})
        self.assertEqual(read("models/item/basalt.json"), {"parent": "examplemod:block/basalt"})
        self.assertEqual(read("blockstates/rock.json")["variants"]["normal"],
                         [{"model": "examplemod:rock_0"}, {"model": "examplemod:rock_1"}])
        self.assertEqual(read("models/block/rock_1.json")["textures"]["all"], "examplemod:blocks/rock_1")
        spike = read("blockstates/spike.json")["variants"]
        self.assertEqual(len(spike), 8)  # direction x size x wet, named in property order
        self.assertEqual(spike["direction=down,size=large,wet=true"], {"model": "examplemod:spike_down_large"})
        self.assertEqual(read("models/block/spike_down_large.json"),
                         {"parent": "block/cross", "textures": {"cross": "examplemod:blocks/spike_down_large"}})
        self.assertNotIn(f"{base}/models/block/spike.json", files)
        self.assertEqual(read("models/item/spike.json"),
                         {"parent": "item/generated", "textures": {"layer0": "examplemod:blocks/spike_up_small"}})
        litter = read("models/block/litter.json")
        self.assertEqual(litter["elements"][0]["from"], [0, 0.25, 0])
        self.assertNotIn(f"{base}/models/item/litter.json", files)
        self.assertEqual(read("models/block/kiln.json")["textures"]["bottom"], "minecraft:blocks/stone")
        log = read("blockstates/log_elm.json")["variants"]
        self.assertEqual(log, {"axis=x": {"model": "examplemod:log_elm", "x": 90, "y": 90},
                               "axis=y": {"model": "examplemod:log_elm"},
                               "axis=z": {"model": "examplemod:log_elm", "x": 90},
                               "axis=none": {"model": "examplemod:log_elm_bark"}})
        self.assertEqual(read("models/block/log_elm_bark.json")["textures"]["all"], "examplemod:blocks/wood/elm_bark")
        leaves = read("blockstates/leaves_ash.json")["variants"]
        self.assertEqual(sorted(leaves), ["check_decay=false,decayable=false", "check_decay=false,decayable=true",
                                          "check_decay=true,decayable=false", "check_decay=true,decayable=true"])
        self.assertEqual(leaves["check_decay=true,decayable=false"],
                         [{"model": "examplemod:leaves_ash_0"}, {"model": "examplemod:leaves_ash_1"}])
        self.assertEqual(read("models/item/leaves_ash.json"), {"parent": "examplemod:block/leaves_ash_0"})
        self.assertEqual(read("models/item/elm_seed.json")["textures"]["layer0"], "examplemod:items/elm_seed")
        lang = files[f"{base}/lang/en_us.lang"].decode("utf-8").splitlines()
        self.assertEqual(lang[0], "item.examplemod.ash_seed.name=Ash Seed")
        self.assertIn("tile.examplemod.log_elm.name=Elm Log", lang)
        self.assertIn("itemGroup.examplemod=Example Mod", lang)
        self.assertEqual(lang, sorted(lang))
        self.assertFalse(any("litter" in line for line in lang))  # no name, no entry

    def test_a_subjects_other_keys_fill_strings(self):
        files = content.render(manifest('namespace = "m"\n[[family]]\nsubjects = { elm = { name = "Elm", grain = '
                                        '"rough" } }\n[[family.block]]\nid = "planks_{subject}"\n'
                                        'name = "{name} Planks ({grain})"\ntextures = { all = "blocks/{subject}_{grain}" }\n'))
        self.assertIn("tile.m.planks_elm.name=Elm Planks (rough)", files["assets/m/lang/en_us.lang"].decode("utf-8"))
        model = json.loads(files["assets/m/models/block/planks_elm.json"])
        self.assertEqual(model["textures"]["all"], "m:blocks/elm_rough")

    def test_json_lang_and_its_keys(self):
        loaded = manifest(MANIFEST.replace("[lang]", '[lang]\nfile = "en_us.json"\nblock = "block.{namespace}.{id}"'))
        files = content.render(loaded)
        lang = json.loads(files["assets/examplemod/lang/en_us.json"])
        self.assertEqual(lang["block.examplemod.basalt"], "Basalt")

    def test_check_and_write(self):
        loaded = manifest()
        files = content.render(loaded)
        root = Path(tempfile.mkdtemp(prefix="wb-assets-"))
        done = content.write(loaded, files, root)
        self.assertEqual(len(done["written"]), len(files))
        self.assertEqual(content.compare(loaded, files, root), {"stale": [], "missing": [], "orphaned": []})
        base = root / "assets" / "examplemod"
        basalt = base / "blockstates" / "basalt.json"
        basalt.write_bytes(basalt.read_bytes().replace(b"\n", b"\r\n"))  # a CRLF checkout still matches
        self.assertEqual(content.compare(loaded, files, root)["stale"], [])
        (base / "models" / "block" / "rock_0.json").write_text("{}", encoding="utf-8")
        (base / "models" / "item" / "seed.json").unlink()
        (base / "blockstates" / "gone.json").write_text("{}", encoding="utf-8")
        (base / "blockstates" / "hand_made.json").write_text("{}", encoding="utf-8")  # kept by the manifest
        (base / "textures" / "blocks").mkdir(parents=True)
        (base / "textures" / "blocks" / "basalt.png").write_bytes(b"png")  # textures are not the manifest's
        found = content.compare(loaded, files, root)
        self.assertEqual(found, {"stale": ["assets/examplemod/models/block/rock_0.json"],
                                 "missing": ["assets/examplemod/models/item/seed.json"],
                                 "orphaned": ["assets/examplemod/blockstates/gone.json"]})
        again = content.write(loaded, files, root)
        self.assertEqual(sorted(again["written"]), ["assets/examplemod/blockstates/basalt.json",
                                                    "assets/examplemod/models/block/rock_0.json",
                                                    "assets/examplemod/models/item/seed.json"])
        self.assertEqual(again["removed"], ["assets/examplemod/blockstates/gone.json"])
        self.assertTrue((base / "blockstates" / "hand_made.json").exists())
        self.assertTrue((base / "textures" / "blocks" / "basalt.png").exists())

    def test_what_the_manifest_gets_wrong(self):
        cases = [
            ('namespace = "Bad Name"', "namespace"),
            ('namespace = "m"\nbogus = 1', "unknown keys"),
            ('namespace = "m"\n[[block]]\nid = "a"\ntextures = { side = "x" }', "cube takes the textures all"),
            ('namespace = "m"\n[[block]]\nid = "a"\nshape = "sphere"\ntextures = {}', "shape must be one of"),
            ('namespace = "m"\n[[block]]\nid = "a"\ntextures = { all = "x_{size}" }', "{size} names nothing"),
            ('namespace = "m"\n[[block]]\nid = "a"\ntextures = { all = "x" }\nalternates = 3', "need {n}"),
            ('namespace = "m"\n[[block]]\nid = "a"\ntextures = { all = "x" }\n[[block]]\nid = "a"\n'
             'textures = { all = "y" }', "defined twice"),
            ('namespace = "m"\n[[block]]\nid = "a"\nshape = "log"\nproperties = { axis = ["x"] }\n'
             'textures = { side = "s", end = "e" }', "axis"),
            ('namespace = "m"\n[[block]]\nid = "a"\ntextures = { all = "x_{n}" }\nalternates = 2\n[[block]]\n'
             'id = "a_1"\ntextures = { all = "y" }', "both make"),
            ('namespace = "m"\n[[family]]\nsubjects = { a = "A" }', "makes nothing"),
            ('namespace = "m"\n[[family]]\nsubjects = { a = "A" }\n[[family.block]]\nid = "x_{subject}"\n'
             'name = "{nope}"\ntextures = { all = "t" }', "nothing left to fill"),
            ('namespace = "m"\n[[block]]\nid = "a"\nshape = "model"\ntextures = { all = "x" }', "needs parent"),
        ]
        for text, words in cases:
            with self.assertRaises(content.ManifestError, msg=text) as caught:
                content.render(manifest(text))
            self.assertIn(words, str(caught.exception), text)


class Blockstates(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(prefix="wb-blocks-"))
        cls.jar = client_jar(cls.root / "client.jar")
        cls.mod = mod_resources(cls.root / "mod")
        cls.res = blockstates.Resources(cls.jar, cls.mod)

    @classmethod
    def tearDownClass(cls):
        cls.res.jar.close()
        shutil.rmtree(cls.root, ignore_errors=True)

    def build(self, *refs: str):
        types = {}
        for text in refs:
            spec = blockstates.reference(text)
            types[spec["name"]] = {"about": {}, "roles": {}, "blocks": [spec]}
        files, listing = blockstates.pack_files(types, self.res, "example", "test")
        return files, listing, blockstates.verify(files, self.res, "example", listing)

    def test_references_become_a_pack_complete_but_for_textures(self):
        files, listing, problems = self.build("stairs=oak_stairs", "furnace", "fence=oak_fence", "slab=stone_slab[half=bottom]",
                                              "lantern=sea_lantern", "cauldron")
        self.assertEqual(problems, [])
        state = json.loads(files["assets/example/blockstates/stairs.json"])
        self.assertEqual(state["variants"]["facing=west,half=bottom,shape=straight"],
                         {"model": "example:stairs", "y": 180, "uvlock": True})
        self.assertEqual(state["variants"]["facing=east,half=bottom,shape=inner_left"]["model"], "example:stairs_inner")
        stairs = json.loads(files["assets/example/models/block/stairs.json"])
        self.assertEqual(stairs, {"parent": "block/stairs", "textures": {"bottom": "example:blocks/stairs",
                                                                       "top": "example:blocks/stairs",
                                                                       "side": "example:blocks/stairs"}})
        self.assertEqual(json.loads(files["assets/example/models/item/stairs.json"]), {"parent": "example:block/stairs"})
        furnace = json.loads(files["assets/example/models/block/furnace.json"])["textures"]
        self.assertEqual(furnace, {"top": "example:blocks/furnace_top", "front": "example:blocks/furnace_front",
                                   "side": "example:blocks/furnace_side"})
        fence = json.loads(files["assets/example/blockstates/fence.json"])
        self.assertEqual([p["apply"]["model"] for p in fence["multipart"]], ["example:fence_post", "example:fence_n"])
        self.assertEqual(json.loads(files["assets/example/models/item/fence.json"])["parent"], "example:block/fence_inventory")
        slab = json.loads(files["assets/example/blockstates/slab.json"])
        self.assertEqual(slab, {"variants": {"variant=stone": {"model": "example:slab"}}})
        # a slot is named by the variable that first uses its texture
        self.assertEqual(json.loads(files["assets/example/models/block/slab.json"])["textures"],
                         {"bottom": "example:blocks/slab_bottom", "top": "example:blocks/slab_bottom",
                          "side": "example:blocks/slab_side"})
        self.assertIn("assets/example/textures/blocks/lantern.png.mcmeta", files)
        cauldron = json.loads(files["assets/example/models/block/cauldron.json"])
        self.assertEqual(cauldron["parent"], "block/cauldron_empty")  # vanilla geometry inherited, not copied
        self.assertNotIn("elements", cauldron)
        slots = {slot: info for t in listing["types"].values() for b in t["blocks"] for slot, info in b["slots"].items()}
        self.assertEqual(slots["example:blocks/lantern"]["animated"], True)
        self.assertEqual(slots["example:items/cauldron_layer0"]["source"], "minecraft:items/cauldron")
        self.assertEqual(slots["example:blocks/stairs"]["vars"], ["bottom", "side", "top"])
        lang = files["assets/example/lang/en_us.lang"]
        self.assertIn("tile.example.stairs.name=Stairs", lang)
        self.assertEqual(json.loads(files["pack.mcmeta"])["pack"]["pack_format"], 3)

    def test_textures_a_vanilla_parent_names_become_slots_too(self):
        files, listing, problems = self.build("pot=flower_pot")
        self.assertEqual(problems, [])
        textures = json.loads(files["assets/example/models/block/pot.json"])["textures"]
        self.assertEqual(textures, {"plant": "example:blocks/pot_plant", "particle": "example:blocks/pot_flowerpot",
                                    "flowerpot": "example:blocks/pot_flowerpot", "dirt": "example:blocks/pot_dirt"})

    def test_the_check_catches_a_texture_left_behind(self):
        files, listing, _ = self.build("stairs=oak_stairs")
        model = json.loads(files["assets/example/models/block/stairs.json"])
        model["textures"]["side"] = "minecraft:blocks/planks_oak"
        files["assets/example/models/block/stairs.json"] = json.dumps(model)
        problems = blockstates.verify(files, self.res, "example", listing)
        self.assertIn("a texture outside the pack is still named: minecraft:blocks/planks_oak", problems)

    def test_slot_names(self):
        uses = [("minecraft:blocks/a", "side", "x_one"), ("minecraft:blocks/b", "side", "x_two"),
                ("minecraft:blocks/c", "top", "x_one")]
        slots = blockstates.name_slots("x", uses, {"x_one": "one", "x_two": "two"})
        self.assertEqual(list(slots), ["blocks/x_one_side", "blocks/x_two_side", "blocks/x_top"])
        same = blockstates.name_slots("y", [("minecraft:blocks/a", "all", "y"), ("minecraft:blocks/b", "all", "y")], {"y": ""})
        self.assertEqual(list(same), ["blocks/y_all_0", "blocks/y_all_1"])

    def test_references_and_catalogs(self):
        self.assertEqual(blockstates.reference("slab=stone_slab[half=bottom]"),
                         {"name": "slab", "ref": "stone_slab", "select": {"half": "bottom"}})
        self.assertEqual(blockstates.reference("examplemod:basalt")["name"], "basalt")
        with self.assertRaises(blockstates.BlockError):
            blockstates.reference("Bad Ref")
        types = blockstates.catalog_types({"types": {"stairs": {"about": "a stair", "group": "building",
                                                                "roles": {"*": {"owner": "material"}},
                                                                "blocks": [{"name": "stairs", "ref": "oak_stairs"}]}}})
        self.assertEqual(types["stairs"]["about"], {"about": "a stair", "group": "building"})
        files, listing = blockstates.pack_files(types, self.res, "example", "test")
        slot = listing["types"]["stairs"]["blocks"][0]["slots"]["example:blocks/stairs"]
        self.assertEqual((slot["owner"], listing["types"]["stairs"]["group"]), ("material", "building"))
        with self.assertRaises(blockstates.BlockError):
            blockstates.catalog_types({"types": {"x": {"blocks": [{"ref": "a", "colour": 1}]}}})

    def test_a_mods_blocks_sorted_by_shape(self):
        types = {"stairs": {"about": {}, "roles": {}, "blocks": [blockstates.reference("oak_stairs")]},
                 "cube": {"about": {}, "roles": {}, "blocks": [blockstates.reference("sea_lantern")]}}
        result = blockstates.classify(self.res, types)
        records = result["blocks"]
        self.assertEqual(records["examplemod:basalt"]["type"], "cube")
        self.assertEqual(records["examplemod:basalt_stairs"]["type"], "stairs")
        self.assertEqual(records["examplemod:marble"]["type"], "cube")
        self.assertEqual(records["examplemod:marble"]["alternates"], 2)
        self.assertEqual(records["examplemod:marble"]["missing"], ["examplemod:blocks/marble_1"])
        self.assertIsNone(records["examplemod:ore"]["type"])  # a Forge blockstate is a shape of its own
        self.assertEqual(result["unmatched"], ["examplemod:ore"])
        self.assertEqual(result["counts"], {"cube": 2, "stairs": 1, "(no type)": 1})
        self.assertEqual(result["shared_textures"], {"examplemod:blocks/basalt": 2})
        ore = next(s for s in result["shapes"] if s["blocks"] == ["examplemod:ore"])
        self.assertEqual((ore["format"], ore["models"]), ("forge", ["block/cube_all"]))

    def test_a_reference_from_the_mod_and_its_resources_as_a_jar(self):
        jar = self.root / "mod.jar"
        with zipfile.ZipFile(jar, "w") as archive:
            for path in (self.mod / "assets").rglob("*"):
                if path.is_file():
                    archive.write(path, path.relative_to(self.mod).as_posix())
        res = blockstates.Resources(self.jar, jar)
        types = {"rock": {"about": {}, "roles": {}, "blocks": [blockstates.reference("rock=examplemod:marble")]}}
        files, listing = blockstates.pack_files(types, res, "example", "test")
        self.assertEqual(blockstates.verify(files, res, "example", listing), [])
        state = json.loads(files["assets/example/blockstates/rock.json"])
        self.assertEqual(state["variants"]["normal"], [{"model": "example:rock_0"}, {"model": "example:rock_1"}])
        self.assertEqual(sorted(listing["types"]["rock"]["blocks"][0]["slots"]), ["example:blocks/rock_0", "example:blocks/rock_1"])
        res.jar.close()
        res.mod_zip.close()


@unittest.skipIf(np is None, "NumPy and Pillow are not installed")
class Metrics(unittest.TestCase):
    def setUp(self):
        from wb_assets import metrics
        self.metrics = metrics
        self.faces = metrics.Faces()

    @staticmethod
    def face(fill=(120, 120, 120)):
        pixels = np.zeros((16, 16, 4), np.uint8)
        pixels[..., :3] = fill
        pixels[..., 3] = 255
        return pixels

    def test_measures(self):
        flat = self.faces.measure(self.face())
        self.assertEqual((flat["spread"], flat["edge_density"], flat["quadrant"], flat["diagonal_chain"]), (0.0, 0.0, 0.0, 0))
        checker = self.face()
        checker[(np.indices((16, 16)).sum(0) % 2) == 1, :3] = 250
        busy = self.faces.measure(checker)
        self.assertEqual(busy["edge_density"], 1.0)
        corner = self.face()
        corner[:8, :8, :3] = 240
        self.assertGreater(self.faces.measure(corner)["quadrant"], 0.1)
        diagonal = self.face((200, 200, 200))
        for i in range(16):
            diagonal[i, i, :3] = 10
        self.assertGreaterEqual(self.faces.measure(diagonal)["diagonal_chain"], 16)
        striped = self.face()
        striped[:4, :, :3] = 230
        self.assertGreater(self.faces.measure(striped)["column_band"], 0.2)
        leaves = self.face()
        leaves[:8, :8, 3] = 0
        self.assertEqual(self.faces.measure(leaves)["opening_quadrant"], 0.75)

    def test_pairs_and_limits(self):
        pair = self.faces.pair(self.face((100, 100, 100)), self.face((140, 140, 140)))
        self.assertGreater(pair["alternate_mean"], 0.05)
        self.assertIn("mip2", pair["field"])
        measured = {**self.faces.measure(self.face()), **pair}
        rules = self.metrics.judge(measured, {"spread": [0.01, 0.2], "edge_density": 0.5, "alternate_mean": 0.02})
        self.assertEqual({k: r["passed"] for k, r in rules.items()},
                         {"spread": False, "edge_density": True, "alternate_mean": False})
        with self.assertRaises(ValueError):
            self.metrics.check_limits({"kinds": {"stone": {"spread": 3}}})
        with self.assertRaises(ValueError):
            self.metrics.check_limits({"kinds": {}, "assign": {"*.png": "stone"}})
        self.assertEqual(self.metrics.kind_of("blocks/stone_a.png", {"stone_*.png": "stone"}, ""), "stone")


class Commands(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = Daemon().start()
        cls.c = cls.d.client
        (cls.d.data / "content.toml").write_text(MANIFEST, encoding="utf-8")
        cls.jar = client_jar(cls.d.data / "client.jar")
        cls.mod = mod_resources(cls.d.data / "mod")
        (cls.d.data / "catalog.toml").write_text(
            '[types.stairs]\nabout = "a stair"\n[[types.stairs.blocks]]\nname = "stairs"\nref = "oak_stairs"\n'
            '[types.cube]\n[[types.cube.blocks]]\nname = "cube"\nref = "sea_lantern"\n', encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.d.stop()

    def test_generate_writes_then_checks(self):
        out = self.d.out / "resources"
        wrote = self.c.submit("assets.generate", {"manifest": str(self.d.data / "content.toml")},
                              out=str(out)).result(timeout=120)
        self.assertEqual(wrote.outcome, "ok", wrote.summary)
        self.assertTrue((out / "assets" / "examplemod" / "blockstates" / "log_ash.json").exists())
        self.assertIn("wrote", wrote.summary)
        checked = self.c.submit("assets.generate", {"manifest": str(self.d.data / "content.toml"), "check": True,
                                                    "resources": str(out)}).result(timeout=120)
        self.assertEqual(checked.outcome, "ok", checked.summary)
        (out / "assets" / "examplemod" / "blockstates" / "basalt.json").write_text("{}", encoding="utf-8")
        stale = self.c.submit("assets.generate", {"manifest": str(self.d.data / "content.toml"), "check": True,
                                                  "resources": str(out)}).result(timeout=120)
        self.assertEqual((stale.outcome, stale.summary), ("finding", "out of date: assets/examplemod/blockstates/basalt.json"))
        usage = self.c.submit("assets.generate", {"manifest": str(self.d.data / "content.toml"),
                                                  "check": True}).result(timeout=120)
        self.assertEqual(usage.outcome, "usage")

    def test_blockstates_pack_and_classification(self):
        out = self.d.out / "blocks"
        result = self.c.submit("assets.blockstates", {"client_jar": str(self.jar), "catalog": str(self.d.data / "catalog.toml"),
                                                      "blocks": ["fence=oak_fence"], "mod": str(self.mod)},
                               out=str(out)).result(timeout=120)
        self.assertEqual(result.outcome, "finding", result.summary)  # the mod lacks a texture
        self.assertEqual(result["findings"], ["examplemod:marble uses examplemod:blocks/marble_1, which the mod does "
                                              "not have"])
        self.assertEqual(result.data.blocks, 3)
        self.assertEqual(result.data.counts, {"cube": 2, "stairs": 1, "(no type)": 1})
        slots = json.loads((out / "pack" / "slots.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(slots["types"]), ["cube", "fence", "stairs"])
        self.assertTrue((out / "pack" / "assets" / "example" / "blockstates" / "fence.json").exists())
        self.assertTrue((out / "classify.json").exists())
        clean = self.c.submit("assets.blockstates", {"client_jar": str(self.jar), "blocks": ["oak_stairs"],
                                                     "namespace": "demo"}).result(timeout=120)
        self.assertEqual(clean.outcome, "ok", clean.summary)
        self.assertIn("1 reference block(s) copied into demo", clean.summary)
        missing = self.c.submit("assets.blockstates", {"client_jar": str(self.jar), "blocks": ["nether_star"]}).result(timeout=120)
        self.assertEqual(missing.outcome, "usage")
        self.assertIn("no blockstate", missing.summary)

    @unittest.skipIf(np is None, "NumPy and Pillow are not installed")
    def test_texture_metrics(self):
        folder = self.d.data / "faces"
        folder.mkdir(exist_ok=True)
        flat = np.full((16, 16, 4), 255, np.uint8)
        flat[..., :3] = 120
        Image.fromarray(flat).save(folder / "stone_a.png")
        Image.fromarray(flat).save(folder / "stone_a-alt.png")
        busy = flat.copy()
        busy[(np.indices((16, 16)).sum(0) % 2) == 1, :3] = 250
        Image.fromarray(busy).save(folder / "stone_b.png")
        Image.fromarray(np.zeros((8, 8, 4), np.uint8)).save(folder / "tiny.png")
        (self.d.data / "limits.toml").write_text('[kinds.stone]\nedge_density = 0.3\nalternate_mean = 0.01\n'
                                                 '[assign]\n"stone_*.png" = "stone"\n', encoding="utf-8")
        result = self.c.submit("assets.texture-metrics", {"textures": str(folder), "limits": str(self.d.data / "limits.toml")},
                               out=str(self.d.out / "metrics")).result(timeout=120)
        self.assertEqual(result.outcome, "finding", result.summary)
        findings = " | ".join(result["findings"])
        self.assertIn("stone_b.png (stone): edge_density 1.0", findings)
        self.assertIn("cannot measure tiny.png: 8 x 8 is not a 16 x 16 face", findings)
        self.assertNotIn("stone_a", findings)
        report = json.loads((self.d.out / "metrics" / "metrics.json").read_text(encoding="utf-8"))
        first = next(t for t in report["textures"] if t["texture"] == "stone_a.png")
        self.assertEqual(first["metrics"]["alternate"], "stone_a-alt.png")
        self.assertEqual(result.data.measured, 2)


if __name__ == "__main__":
    unittest.main()
