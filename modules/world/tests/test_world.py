"""world: reading saves and comparing worlds, on tiny saves written here byte by byte. The NBT and region
writers are the test's own, so the readers are checked against the formats rather than against themselves.
Goldens and previews run a tiny generator whose sampler is written here too."""

import gzip
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import zlib
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "tests"))
sys.path.insert(0, str(REPO / "modules" / "world"))
from harness import Daemon  # noqa: E402
from wb_core.module import EnvironmentProblem, ParamError  # noqa: E402
from wb_world import golden as goldens  # noqa: E402
from wb_world import nbt, units  # noqa: E402
from wb_world import sampler as samplers  # noqa: E402
from wb_world.region import ANVIL, EntryError, Region  # noqa: E402
from wb_world.save import Save  # noqa: E402

try:
    import numpy as np
    from PIL import Image
except ImportError:  # previews need both
    np = Image = None


# -- an NBT writer ---------------------------------------------------------------------------------
class Tag:
    def __init__(self, kind: int, value):
        self.kind, self.value = kind, value


def byte(v): return Tag(1, v)
def short(v): return Tag(2, v)
def int_(v): return Tag(3, v)
def long_(v): return Tag(4, v)
def float_(v): return Tag(5, v)
def double(v): return Tag(6, v)
def ints(values): return Tag(11, list(values))
def longs(values): return Tag(12, list(values))
def items(kind, values): return Tag(9, (kind, list(values)))


NUMBERS = {1: ">b", 2: ">h", 3: ">i", 4: ">q", 5: ">f", 6: ">d"}


def kind_of(value) -> int:
    if isinstance(value, Tag):
        return value.kind
    return 10 if isinstance(value, dict) else 8 if isinstance(value, str) else 7


def text(value: str) -> bytes:
    raw = value.encode("utf-8")
    return struct.pack(">H", len(raw)) + raw


def payload(value) -> bytes:
    kind = kind_of(value)
    if kind == 10:
        return b"".join(bytes([kind_of(v)]) + text(k) + payload(v) for k, v in value.items()) + b"\0"
    if kind == 8:
        return text(value)
    if kind == 7:
        return struct.pack(">i", len(value)) + bytes(value)
    if kind in NUMBERS:
        return struct.pack(NUMBERS[kind], value.value)
    if kind in (11, 12):
        return struct.pack(f">i{len(value.value)}{'i' if kind == 11 else 'q'}", len(value.value), *value.value)
    item_kind, values = value.value
    return struct.pack(">Bi", item_kind, len(values)) + b"".join(payload(v) for v in values)


def document(root: dict) -> bytes:
    return b"\x0a" + text("") + payload(root)


# -- region writers --------------------------------------------------------------------------------
def sectors(entries: dict[int, bytes], sector: int, table: int) -> bytes:
    """A region file: the table, then each record (a 4-byte length and its payload) in whole sectors."""
    header, body = bytearray(table), bytearray()
    at = table // sector
    for index, data in sorted(entries.items()):
        record = struct.pack(">I", len(data)) + data
        count = -(-len(record) // sector)
        struct.pack_into(">I", header, index * 4, at << 8 | count)
        body += record + bytes(count * sector - len(record))
        at += count
    return bytes(header + body)


def write_mca(root: Path, chunks: dict) -> None:
    files: dict = {}
    for (x, z), tree in chunks.items():
        files.setdefault((x >> 5, z >> 5), {})[(x & 31) + (z & 31) * 32] = b"\x02" + zlib.compress(document(tree))
    for (rx, rz), entries in files.items():
        (root / "region").mkdir(parents=True, exist_ok=True)
        (root / "region" / f"r.{rx}.{rz}.mca").write_bytes(sectors(entries, 4096, 8192))


def write_cubic(root: Path, cubes: dict, columns: dict, external=()) -> None:
    files: dict = {}
    for (x, y, z), tree in cubes.items():
        region, index = (x >> 4, y >> 4, z >> 4), (x & 15) << 8 | (y & 15) << 4 | (z & 15)
        data = gzip.compress(document(tree))
        if (x, y, z) in external:  # too big for its sectors: kept in a file of its own, the table entry zero
            folder = root / "region3d" / (".".join(map(str, region)) + ".3dr.ext")
            folder.mkdir(parents=True, exist_ok=True)
            (folder / str(index)).write_bytes(data)
        else:
            files.setdefault(("region3d", ".".join(map(str, region)) + ".3dr", 512 * 32), {})[index] = data
    for (x, z), tree in columns.items():
        name = f"{x >> 5}.{z >> 5}.2dr"
        files.setdefault(("region2d", name, 512 * 8), {})[(x & 31) << 5 | (z & 31)] = gzip.compress(document(tree))
    for (folder, name, table), entries in files.items():
        (root / folder).mkdir(parents=True, exist_ok=True)
        (root / folder / name).write_bytes(sectors(entries, 512, table))


# -- saves -----------------------------------------------------------------------------------------
BLOCKS = {0: "minecraft:air", 1: "minecraft:stone", 3: "minecraft:dirt", 61: "minecraft:furnace"}
RENUMBERED = {0: "minecraft:air", 7: "minecraft:stone", 3: "minecraft:dirt", 61: "minecraft:furnace"}


def level(registry: dict, tick: int) -> dict:
    ids = items(10, [{"K": name, "V": int_(number)} for number, name in registry.items()])
    return {"Data": {"LevelName": "test", "RandomSeed": long_(7), "generatorName": "flat", "Time": long_(tick),
                     "DataVersion": int_(1343), "Version": {"Name": "1.12.2"}},
            "FML": {"Registries": {"minecraft:blocks": {"ids": ids}},
                    "ModList": items(10, [{"ModId": "forge", "ModVersion": "14.23.5"}])}}


def write_world(root: Path, chunks=None, cubes=None, columns=None, registry=BLOCKS, tick=1000, external=()) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "level.dat").write_bytes(gzip.compress(document(level(registry, tick))))
    if chunks:
        write_mca(root, chunks)
    if cubes or columns:
        write_cubic(root, cubes or {}, columns or {}, external)
        (root / "data").mkdir(exist_ok=True)
        (root / "data" / "cubicChunksData.dat").write_bytes(gzip.compress(document(
            {"data": {"minHeight": int_(-128), "maxHeight": int_(384), "isCubicChunks": byte(1),
                      "storageFormat": "anvil3d"}})))
    return root


def packed(values: list[int]) -> bytes:
    return bytes(values[i] | values[i + 1] << 4 for i in range(0, len(values), 2))


def section(y=None, floor=1, cells=None, block_light=0, sky_light=15) -> dict:
    """A 1.12 section: a floor of one block at local y 0, the cells given ((x, y, z): (id, meta)), even light."""
    blocks, meta = bytearray(4096), [0] * 4096
    blocks[:256] = bytes([floor]) * 256
    for (x, ly, z), (block, data) in (cells or {}).items():
        blocks[ly << 8 | z << 4 | x], meta[ly << 8 | z << 4 | x] = block, data
    out = {"Blocks": bytes(blocks), "Data": packed(meta), "BlockLight": packed([block_light] * 4096),
           "SkyLight": packed([sky_light] * 4096)}
    if y is not None:
        out["Y"] = byte(y)
    return out


def chunk(x, z, sections_, tiles=(), entities=(), last_update=10) -> dict:
    return {"DataVersion": int_(1343), "Level": {
        "xPos": int_(x), "zPos": int_(z), "LastUpdate": long_(last_update), "InhabitedTime": long_(last_update * 2),
        "LightPopulated": byte(1), "TerrainPopulated": byte(1), "V": byte(1), "Biomes": bytes(256),
        "HeightMap": ints([1] * 256), "Sections": items(10, sections_), "TileEntities": items(10, list(tiles)),
        "Entities": items(10, list(entities))}}


def furnace(x, y, z, coal=3) -> dict:
    return {"id": "minecraft:furnace", "x": int_(x), "y": int_(y), "z": int_(z), "BurnTime": short(0),
            "Items": items(10, [{"Slot": byte(0), "id": "minecraft:coal", "Count": byte(coal), "Damage": short(0)}])}


def pig(x, y, z) -> dict:
    return {"id": "minecraft:pig", "UUIDMost": long_(1), "UUIDLeast": long_(2),
            "Pos": items(6, [double(x), double(y), double(z)])}


def anvil_chunks(stone=1, granite=True, coal=3, edge_light=0, centre_light=0, pig_x=24.5, last_update=10) -> dict:
    """3 x 3 chunks, each a stone floor; the centre one also holds granite, a furnace (block and NBT) and a pig."""
    chunks = {}
    for x in range(3):
        for z in range(3):
            centre = (x, z) == (1, 1)
            cells = ({(0, 1, 0): (stone, 1)} if granite else {}) | {(1, 1, 2): (61, 3)} if centre else {}
            light = centre_light if centre else edge_light
            chunks[(x, z)] = chunk(x, z, [section(0, stone, cells, block_light=light)],
                                   [furnace(17, 1, 18, coal)] if centre else [],
                                   [pig(pig_x, 1.0, 24.5)] if centre else [], last_update)
    return chunks


def cube(x, y, z, sky_light=15, entities=()) -> dict:
    return {"Level": {"v": byte(1), "x": int_(x), "y": int_(y), "z": int_(z), "populated": byte(1),
                      "fullyPopulated": byte(1), "initLightDone": byte(1), "isSurfaceTracked": byte(1),
                      "Sections": items(10, [section(sky_light=sky_light)]), "TileEntities": items(10, []),
                      "Entities": items(10, list(entities))}}


def column(x, z) -> dict:
    return {"Level": {"v": byte(1), "x": int_(x), "z": int_(z), "InhabitedTime": long_(0), "Biomes": bytes(256),
                      "OpacityIndex": struct.pack(">iiH", 0, 15, 0) * 256}}


def cubic_world(root: Path, centre_sky=15, external=((2, 2, 2),)) -> Path:
    cubes = {(x, y, z): cube(x, y, z, centre_sky if (x, y, z) == (1, 1, 1) else 15,
                             [pig(24.5, 17.0, 24.5)] if (x, y, z) == (1, 1, 1) else [])
             for x in range(3) for y in range(3) for z in range(3)}
    return write_world(root, cubes=cubes, columns={(x, z): column(x, z) for x in range(3) for z in range(3)},
                       external=external)


def overflow(path: Path, index: int, count: int = 1) -> None:
    """Rewrites one table entry's sector count, as a one-byte count that wrapped would."""
    data = bytearray(path.read_bytes())
    entry, = struct.unpack_from(">I", data, index * 4)
    struct.pack_into(">I", data, index * 4, (entry & ~0xFF) | count)
    path.write_bytes(bytes(data))


HOLD_LOCK = """
import os, sys, time
handle = open(sys.argv[1], "r+b")
if os.name == "nt":
    import msvcrt
    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 3)
else:
    import fcntl
    fcntl.lockf(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
print("locked", flush=True)
time.sleep(60)
"""


def signed(word: int) -> int:
    return word - (1 << 64) if word >= 1 << 63 else word


def pack_padded(values: list[int], bits: int) -> list[int]:
    per = 64 // bits
    words = []
    for start in range(0, len(values), per):
        word = 0
        for offset, value in enumerate(values[start:start + per]):
            word |= value << (offset * bits)
        words.append(signed(word))
    return words


def pack_spanning(values: list[int], bits: int) -> list[int]:
    whole = 0
    for index, value in enumerate(values):
        whole |= value << (index * bits)
    return [signed(whole >> (64 * k) & ((1 << 64) - 1)) for k in range(-(-len(values) * bits // 64))]


# -- the readers on their own ----------------------------------------------------------------------
class Formats(unittest.TestCase):
    def test_every_tag_type_reads_back_with_its_type(self):
        tree = {"b": byte(-1), "s": short(300), "i": int_(-70000), "l": long_(1 << 40), "f": float_(0.5),
                "d": double(2.25), "bytes": b"\x01\xff", "text": "hé", "ints": ints([1, -2]), "longs": longs([3]),
                "list": items(3, [int_(1), int_(2)]), "nested": {"empty": items(0, [])}}
        parsed = nbt.parse(document(tree))
        for key, kind in (("b", nbt.Byte), ("s", nbt.Short), ("i", nbt.Int), ("l", nbt.Long), ("f", nbt.Float),
                          ("d", nbt.Double), ("bytes", nbt.ByteArray), ("text", nbt.String), ("ints", nbt.IntArray),
                          ("longs", nbt.LongArray), ("list", nbt.List), ("nested", nbt.Compound)):
            self.assertIsInstance(parsed[key], kind, key)
        self.assertEqual((parsed["i"], parsed["l"], parsed["text"], parsed["ints"]), (-70000, 1 << 40, "hé", (1, -2)))
        self.assertIsInstance(parsed["list"][0], nbt.Int)
        self.assertEqual(nbt.to_json(parsed)["bytes"], [1, -1])
        reordered = nbt.parse(document(dict(reversed(tree.items()))))
        self.assertEqual(nbt.canonical(parsed), nbt.canonical(reordered))  # key order does not count
        self.assertNotEqual(nbt.canonical(parsed), nbt.canonical(nbt.parse(document(dict(tree, i=long_(-70000))))))

    def test_malformed_documents_are_refused(self):
        whole = document({"a": "text", "n": int_(1)})
        with self.assertRaises(nbt.NbtError):
            nbt.parse(whole[:-3])
        deep = b"\x0a\x00\x00" + b"\x0a\x00\x01x" * 600 + b"\x00" * 601  # nested past the game's 512
        with self.assertRaises(nbt.NbtError):
            nbt.parse(deep)
        forged = b"\x0a\x00\x00\x09\x00\x01l\x0a" + struct.pack(">i", 1 << 30) + b"\x00"
        with self.assertRaisesRegex(nbt.NbtError, "more than the data holds"):
            nbt.parse(forged)
        with self.assertRaises(nbt.NbtError):
            nbt.inflate(b"not compressed")
        with self.assertRaisesRegex(nbt.NbtError, "inflates past"):
            nbt.inflate(zlib.compress(bytes(4096)), limit=1024)

    def test_palette_words_with_and_without_spanning(self):
        values = [(i * 7) % 20 for i in range(4096)]  # 20 states need 5 bits
        padded, spanning = pack_padded(values, 5), pack_spanning(values, 5)
        self.assertEqual((len(padded), len(spanning)), (342, 320))
        self.assertEqual(units.unpack(padded, 5, 4096), values)
        self.assertEqual(units.unpack(spanning, 5, 4096), values)
        with self.assertRaises(units.SectionError):
            units.unpack(padded[:-1], 5, 4096)

    def test_palette_sections_of_later_versions(self):
        logs = [1 if i % 3 == 0 else 0 for i in range(4096)]
        palette = items(10, [{"Name": "minecraft:air"}, {"Name": "minecraft:oak_log", "Properties": {"axis": "y"}}])
        tree = {"DataVersion": int_(3465), "xPos": int_(0), "zPos": int_(0), "sections": items(10, [
            {"Y": byte(-4), "block_states": {"palette": palette, "data": longs(pack_padded(logs, 4))}},
            {"Y": byte(-3), "block_states": {"palette": items(10, [{"Name": "minecraft:stone"}])}},
            {"Y": byte(20), "SkyLight": packed([15] * 4096)}])}
        older = {"Level": {"xPos": int_(0), "zPos": int_(0), "Sections": items(10, [
            {"Y": byte(0), "Palette": palette, "BlockStates": longs(pack_spanning(logs, 4))}])}}
        problems = units.Problems()
        seen = units.view(units.Unit("chunk", (0, 0), "t", nbt.parse(document(tree)), b""), units.Namer({}), problems)
        states = {section.pos[1]: section.states for section in seen.sections}
        self.assertEqual(states[-4].count("minecraft:oak_log[axis=y]"), logs.count(1))
        self.assertEqual(set(states[-3]), {"minecraft:stone"})
        self.assertIsNone(states[20])  # light only
        old = units.view(units.Unit("chunk", (0, 0), "t", nbt.parse(document(older)), b""), units.Namer({}), problems)
        self.assertEqual(old.sections[0].states, states[-4])
        self.assertEqual(len(old.sections), 16)  # the sections a 1.17-or-earlier chunk leaves out are air
        self.assertEqual(problems, [])

    def test_a_session_lock_another_program_holds_refuses_the_world(self):
        root = Path(tempfile.mkdtemp(prefix="wb-world-"))
        try:
            world = write_world(root / "w", {(0, 0): chunk(0, 0, [section(0)])})
            (world / "session.lock").write_bytes("☃".encode())  # what newer games write, then lock
            holder = subprocess.Popen([sys.executable, "-c", HOLD_LOCK, str(world / "session.lock")],
                                      stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual(holder.stdout.readline().strip(), "locked")
                with self.assertRaisesRegex(EnvironmentProblem, "holds its session.lock"):
                    Save(world).check_stopped(0)  # even a world declared stopped
            finally:
                holder.kill()
                holder.wait()
            # Windows releases a killed process's file locks a moment after it has exited: allow that moment.
            for attempt in range(50):
                try:
                    Save(world).check_stopped(0)
                    break
                except EnvironmentProblem:
                    if attempt == 49:
                        raise
                    time.sleep(0.1)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_region_entries_are_checked_against_their_sectors(self):
        root = Path(tempfile.mkdtemp(prefix="wb-world-"))
        try:
            noisy = chunk(1, 0, [section(0)]) | {"Noise": os.urandom(9000)}  # 3 sectors, incompressible
            write_mca(root, {(0, 0): chunk(0, 0, [section(0)]), (1, 0): noisy, (2, 0): chunk(2, 0, [section(0)])})
            path = root / "region" / "r.0.0.mca"
            overflow(path, 1)
            data = bytearray(path.read_bytes())
            struct.pack_into(">I", data, 2 * 4, 1 << 8 | 1)  # entry 2 now starts inside the header
            path.write_bytes(bytes(data))
            with Region(path, ANVIL) as region:
                self.assertEqual(region.indices(), [0, 1, 2])
                self.assertEqual(int(nbt.parse(region.read(0))["Level"]["xPos"]), 0)
                with self.assertRaisesRegex(EntryError, "overflowed"):
                    region.read(1)
                with self.assertRaisesRegex(EntryError, "header"):
                    region.read(2)
        finally:
            shutil.rmtree(root, ignore_errors=True)


# -- the commands, through the daemon ---------------------------------------------------------------
class Commands(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = Daemon().start()
        cls.c = cls.d.client
        data = cls.d.data
        cls.a = write_world(data / "a", anvil_chunks())
        cls.same = write_world(data / "same", anvil_chunks(last_update=99))  # only volatile fields differ
        cls.renumbered = write_world(data / "renumbered", anvil_chunks(stone=7), registry=RENUMBERED)
        cls.changed = write_world(data / "changed", anvil_chunks(stone=7, granite=False, coal=4), registry=RENUMBERED)
        cls.lit = write_world(data / "lit", anvil_chunks(centre_light=5, pig_x=25.5), tick=2000)
        cls.edge = write_world(data / "edge", anvil_chunks(edge_light=3))
        cls.cubic = cubic_world(data / "cubic")
        cls.cubic_dark = cubic_world(data / "cubic-dark", centre_sky=9)

    @classmethod
    def tearDownClass(cls):
        cls.d.stop()

    def run_command(self, command: str, out: str | None = None, **params):
        return self.c.submit(command, params, out=str(self.d.out / out) if out else None).result(timeout=120)

    def report(self, out: str, name: str = "report") -> dict:
        return json.loads((self.d.out / out / f"{name}.json").read_text(encoding="utf-8"))

    def test_read_counts_block_states_block_entities_and_more(self):
        result = self.run_command("world.read", "read-a", world=str(self.a), block_entities=["minecraft:furnace"])
        self.assertEqual(result.outcome, "ok", result.summary)
        self.assertEqual(result.data.units, {"chunk": 9})
        report = self.report("read-a")
        self.assertEqual(report["blocks"], {"minecraft:air": 9 * 3840 - 2, "minecraft:furnace:3": 1,
                                            "minecraft:stone": 9 * 256, "minecraft:stone:1": 1})
        self.assertEqual(report["nonAirY"], [0, 1])
        kept = report["blockEntities"]["nbt"]["minecraft:furnace"][0]
        self.assertEqual((kept["pos"], kept["nbt"]["Items"][0]["Count"]), ([17, 1, 18], 3))
        self.assertEqual(report["entities"], {"minecraft:pig": 1})
        self.assertEqual(report["heightmaps"], {"HeightMap": 9})
        self.assertEqual(report["light"], {"blockLight": 9, "lit": 9, "sections": 9, "skyLight": 9})
        self.assertEqual((report["level"]["seed"], report["mods"][0]["id"]), (7, "forge"))

    def test_read_takes_a_box_and_region_names(self):
        result = self.run_command("world.read", "read-box", world=str(self.a), box=[16, 0, 16, 31, 0, 31])
        self.assertEqual(result.outcome, "ok", result.summary)
        report = self.report("read-box")
        self.assertEqual((report["units"], report["blocks"]), ({"chunk": 1}, {"minecraft:stone": 256}))
        self.assertEqual((report["blockEntities"]["counts"], report["entities"]), ({}, {}))
        named = self.run_command("world.read", world=str(self.a), regions=["r.0.0.mca"])
        self.assertEqual(named.data.units, {"chunk": 9})
        missing = self.run_command("world.read", world=str(self.a), regions=["r.9.9.mca"])
        self.assertEqual(missing.outcome, "usage")
        self.assertIn("r.9.9.mca", missing.summary)
        outside = self.run_command("world.read", world=str(self.a), dimension="../a")
        self.assertEqual(outside.outcome, "usage")

    def test_read_a_cubic_save_with_an_external_entry(self):
        result = self.run_command("world.read", "read-cubic", world=str(self.cubic))
        self.assertEqual(result.outcome, "ok", result.summary)
        self.assertEqual(result.data.units, {"cube": 27, "column": 9})  # (2, 2, 2) from its .ext file
        report = self.report("read-cubic")
        self.assertEqual(report["cubesByY"], {"0": 9, "1": 9, "2": 9})
        self.assertEqual(report["heightmaps"], {"OpacityIndex": 9})
        self.assertEqual(report["blocks"]["minecraft:stone"], 27 * 256)
        self.assertEqual(report["cubic"]["storageFormat"], "anvil3d")

    def test_read_refuses_a_world_open_in_a_game(self):
        world = self.d.data / "open"
        shutil.copytree(self.a, world)
        (world / "session.lock").write_bytes(struct.pack(">q", int(time.time() * 1000)))
        refused = self.run_command("world.read", world=str(world))
        self.assertEqual(refused.outcome, "environment")
        self.assertIn("session.lock", refused.summary)
        self.assertEqual(self.run_command("world.read", world=str(world), quiet=0).outcome, "ok")
        hours_ago = time.time() - 7200
        for path in [world / "session.lock", world / "level.dat", *(world / "region").iterdir()]:
            os.utime(path, (hours_ago, hours_ago))
        self.assertEqual(self.run_command("world.read", world=str(world)).outcome, "ok")

    def test_read_reports_entries_that_overflow_their_sectors(self):
        world = write_world(self.d.data / "corrupt", {(0, 0): chunk(0, 0, [section(0)]),
                                                      (1, 0): chunk(1, 0, [section(0)]) | {"Noise": os.urandom(9000)}})
        overflow(world / "region" / "r.0.0.mca", 1)
        result = self.run_command("world.read", "read-corrupt", world=str(world))
        self.assertEqual((result.outcome, result.code), ("finding", 1))
        self.assertIn("overflowed", result.summary)
        self.assertEqual(self.report("read-corrupt")["units"], {"chunk": 1})

    def test_digests_from_two_saves_compare_by_category(self):
        for name, world in (("digest-a", self.a), ("digest-same", self.same), ("digest-lit", self.lit)):
            done = self.run_command("world.read", name, world=str(world), digest=True, dump=[1, 0, 1])
            self.assertEqual(done.outcome, "ok", done.summary)
        digest = self.report("digest-a", "digest")
        self.assertEqual((digest["format"], digest["tick"], len(digest["cubes"]), len(digest["columns"])),
                         ("wb/world-digest/v1", 1000, 9 * 16, 9))
        paths = {name: str(self.d.out / name / "digest.json") for name in ("digest-a", "digest-same", "digest-lit")}
        agree = self.run_command("world.digest-diff", a=paths["digest-a"], b=paths["digest-same"])
        self.assertEqual(agree.outcome, "ok", agree.summary)
        self.assertTrue(agree.data.agree)
        differ = self.run_command("world.digest-diff", "digest-diff", a=paths["digest-a"], b=paths["digest-lit"])
        self.assertEqual(differ.outcome, "finding")
        self.assertEqual(differ.data.differences, {"tick": 1, "blockLight": 1, "entitiesMoved": 1})
        report = self.report("digest-diff")
        self.assertEqual(report["differences"]["blockLight"], ["1,0,1"])
        self.assertEqual(report["voxels"]["1,0,1"]["blockLight"]["cells"], 4096)
        self.assertEqual(report["voxels"]["1,0,1"]["blockLight"]["largest"], 5)
        untimed = self.run_command("world.digest-diff", a=paths["digest-a"], b=paths["digest-lit"], ignore_tick=True)
        self.assertNotIn("tick", untimed.data.differences)
        not_digest = self.run_command("world.digest-diff", a=str(self.d.out / "digest-a" / "report.json"),
                                      b=paths["digest-a"])
        self.assertEqual(not_digest.outcome, "usage")
        self.assertEqual(self.run_command("world.read", world=str(self.a), dump=[1, 0, 1]).outcome, "usage")

    def test_diff_compares_by_name_and_leaves_out_volatile_fields(self):
        same = self.run_command("world.diff", a=str(self.a), b=str(self.same))
        self.assertEqual(same.outcome, "ok", same.summary)
        self.assertEqual((same.data.compared, same.data.identical), (9, 9))
        renumbered = self.run_command("world.diff", "diff-renumbered", a=str(self.a), b=str(self.renumbered))
        self.assertEqual(renumbered.outcome, "ok", renumbered.summary)  # other ids, the same blocks by name
        self.assertEqual(self.report("diff-renumbered")["registry"]["renumbered"], ["minecraft:stone"])
        changed = self.run_command("world.diff", "diff-changed", a=str(self.a), b=str(self.changed))
        self.assertEqual(changed.outcome, "finding")
        self.assertEqual((changed.data.differing, changed.data.positions), (1, ["chunk 1,1"]))
        shown = {row["path"]: row for row in self.report("diff-changed")["details"]["chunk 1,1"]["shown"]}
        self.assertEqual(shown["Level.Sections[Y=0].blocks"]["cells"], 1)
        self.assertEqual(shown["Level.Sections[Y=0].blocks"]["examples"][0]["a"], "minecraft:stone:1")
        count = shown["Level.TileEntities[17,1,18].Items[Slot=0].Count"]
        self.assertEqual((count["a"], count["b"]), (3, 4))
        strict = self.run_command("world.diff", a=str(self.a), b=str(self.same), ignore=[])
        self.assertEqual(strict.outcome, "finding")
        self.assertEqual(set(strict.data.fields), {"Level.LastUpdate", "Level.InhabitedTime"})

    def test_light_diff_compares_only_the_interior(self):
        centre = self.run_command("world.light-diff", "light-centre", a=str(self.a), b=str(self.lit))
        self.assertEqual(centre.outcome, "finding")
        self.assertEqual((centre.data.compared, centre.data.differing, centre.data.same_blocks), (1, 1, 1))
        listed = self.report("light-centre")["listed"][0]
        self.assertEqual((listed["pos"], listed["blockLight"]["largest"], listed["skyLight"]), ([1, 0, 1], 5, None))
        edge = self.run_command("world.light-diff", a=str(self.a), b=str(self.edge))
        self.assertEqual(edge.outcome, "ok", edge.summary)  # the edge chunks differ, but have unsaved neighbours
        cubic = self.run_command("world.light-diff", "light-cubic", a=str(self.cubic), b=str(self.cubic_dark))
        self.assertEqual(cubic.outcome, "finding")
        self.assertEqual((cubic.data.compared, cubic.data.differing), (1, 1))
        self.assertEqual(self.report("light-cubic")["listed"][0]["skyLight"]["largest"], 6)
        mixed = self.run_command("world.light-diff", a=str(self.a), b=str(self.cubic))
        self.assertEqual(mixed.outcome, "usage")


# -- a generator and its sampler -------------------------------------------------------------------
SAMPLER = r'''
"""A tiny generator's sampler (wb_world/sampler.py): value noise over 8-block cells, a row of columns at a time, on
threads. --leak raises the first row a run samples, as state one window leaves for the next would; --bump raises
every column east of x = 0; --fail stops before sampling; --drop leaves out each window's last layer."""
import hashlib
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

windows_file, out_file, threads, order, *flags = sys.argv[1:]
taken, lock = [], threading.Lock()


def height(seed, x, z):
    noise = hashlib.sha256(f"{seed}:{x // 8}:{z // 8}".encode()).digest()[0]
    return 60 + noise % 32 + ("--bump" in flags and x >= 0)


def row(window, z):
    with lock:
        taken.append(z)
        leaked = "--leak" in flags and len(taken) == 1
    x1, _, x2, _ = window["columns"]
    heights = [height(window["seed"], x, z) + leaked for x in range(x1, x2 + 1, window["step"])]
    return {"height": heights, "biome": ["ocean" if h < 64 else "plains" if h < 80 else "hills" for h in heights]}


def sample(window):
    started = time.perf_counter()
    _, z1, _, z2 = window["columns"]
    rows = list(range(z1, z2 + 1, window["step"]))
    visits = rows[::-1] if order == "reverse" else rows
    with ThreadPoolExecutor(int(threads)) as pool:
        sampled = dict(zip(visits, pool.map(lambda z: row(window, z), visits)))
    layers = {}
    for layer in window["layers"]:
        values = [value for z in rows for value in sampled[z][layer]]  # z then x, lowest first, whatever the order
        layers[layer] = {"sha256": hashlib.sha256("".join(f"{v}\n" for v in values).encode()).hexdigest(),
                         "summary": {"min": min(values), "max": max(values)} if layer == "height"
                         else {name: values.count(name) for name in sorted(set(values))}}
        if layer in window["raster"]:
            layers[layer]["raster"] = {"width": len(sampled[z1][layer]), "height": len(rows), "values": values}
    if "--drop" in flags:
        layers.popitem()
    return {"window": window["name"], "layers": layers, "seconds": round(time.perf_counter() - started, 4)}


with open(windows_file, encoding="utf-8") as handle:
    spec = json.load(handle)
if "--fail" in flags:
    print("the generator broke")
    sys.exit(4)
with open(out_file, "w", encoding="utf-8") as out:
    for window in spec["windows"]:
        out.write(json.dumps(sample(window)) + "\n")
'''

FRESH = {"format": "wb/world-golden/v1", "comment": "a tiny generator's windows", "seed": 4242,
         "layers": ["height", "biome"], "palettes": {"biome": {"ocean": "#1f4e9c", "plains": "#7fb24d",
                                                               "hills": "#a08060"}},
         "windows": [{"name": "east", "columns": [0, 0, 63, 63]},
                     {"name": "west", "columns": [-1, -1, -64, -64], "step": 2, "seed": 17}]}


def sampler_command(*flags) -> list[str]:
    return ["{python}", "tools/sampler.py", "{windows}", "{out}", "{threads}", "{order}", *flags]


class Samplers(unittest.TestCase):
    """The contract's parts on their own: command lines, golden files, a sampler's lines, orders and summaries."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="wb-golden-"))
        self.addCleanup(shutil.rmtree, self.root, True)

    def golden_file(self, document: dict) -> Path:
        path = self.root / "golden.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def lines(self, entries: list, bom: bool = False) -> Path:
        path = self.root / "digests.jsonl"
        text = "".join((entry if isinstance(entry, str) else json.dumps(entry)) + "\n" for entry in entries)
        path.write_text("﻿" * bom + text, encoding="utf-8")
        return path

    def test_placeholders_are_filled_and_others_refused(self):
        values = {"windows": "w.json", "out": "o.jsonl", "threads": "4", "order": "reverse", "python": "py"}
        self.assertEqual(samplers.expand(["{python}", "--out={out}", "{{literal}}", "{threads}x{order}"], values),
                         ["py", "--out=o.jsonl", "{literal}", "4xreverse"])
        ctx = SimpleNamespace(spec={})
        self.assertEqual(samplers.command(ctx, ["gen", "{windows}", "{out}"]), ["gen", "{windows}", "{out}"])
        with self.assertRaisesRegex(ParamError, r"\{window\} is no placeholder"):
            samplers.command(ctx, ["gen", "{window}", "{out}"])
        with self.assertRaisesRegex(ParamError, r"must pass \{out\}"):
            samplers.command(ctx, ["gen", "{windows}", "{{out}}"])  # braces, not the placeholder
        with self.assertRaisesRegex(ParamError, "its program first"):
            samplers.command(ctx, [])

    def test_golden_windows_take_the_files_defaults(self):
        loaded = goldens.load(self.golden_file({
            "format": "wb/world-golden/v1", "seed": 5, "layers": ["height"], "step": 4, "options": {"a": 1, "b": 2},
            "windows": [{"name": "w", "columns": [10, 0, -10, 5]},
                        {"name": "b", "seed": "text", "box": [0, 9, 0, 3, 0, 3], "step": 1, "layers": ["blocks"],
                         "options": {"b": 3}, "comment": "kept", "digests": {"blocks": {"sha256": "AB" * 32}}}]}))
        first, second = loaded.windows
        self.assertEqual(first.entry(), {"name": "w", "seed": 5, "columns": [-10, 0, 10, 5], "step": 4,
                                         "layers": ["height"], "options": {"a": 1, "b": 2}, "raster": []})
        self.assertEqual((second.seed, second.box, second.step, second.options),
                         ("text", (0, 0, 0, 3, 9, 3), 1, {"a": 1, "b": 3}))
        self.assertEqual(loaded.recorded, {"w": {}, "b": {"blocks": {"sha256": "ab" * 32}}})
        window = {"name": "w", "seed": 1, "layers": ["h"], "columns": [0, 0, 1, 1]}
        for broken, message in (
                ({"windows": []}, "at least one window"),
                ({"windows": [dict(window, box=[0, 0, 0, 1, 1, 1])]}, "one of the two"),
                ({"windows": [dict(window, columns=[0, 0, 1])]}, "4 whole numbers"),
                ({"windows": [{k: v for k, v in window.items() if k != "seed"}]}, "needs a seed"),
                ({"windows": [dict(window, layers=["h", "h"])]}, "needs layers"),
                ({"windows": [dict(window, step=0)]}, "step must be"),
                ({"windows": [dict(window, colour="red")]}, "unknown keys"),
                ({"windows": [window, window]}, "two windows are named w"),
                ({"windows": [dict(window, digests={"h": {"sha256": "beef"}})]}, "64 hex"),
                ({"windows": [dict(window, name="../w")]}, "needs a name"),
                ({"windows": [dict(window, name="w\n")]}, "needs a name")):
            with self.subTest(message), self.assertRaisesRegex(ParamError, message):
                goldens.load(self.golden_file({"format": "wb/world-golden/v1", **broken}))
        with self.assertRaisesRegex(ParamError, "not a golden file"):
            goldens.load(self.golden_file({"windows": [window]}))

    def test_a_samplers_lines_are_held_to_the_contract(self):
        windows = [samplers.Window("a", 1, ("height", "biome"), columns=(0, 0, 3, 3)),
                   samplers.Window("b", 1, ("height",), columns=(0, 0, 3, 3), raster=("height",))]
        digest = {"sha256": "AB" * 32}
        raster = {"width": 2, "height": 2, "values": [1, 2, 3, 4]}
        found, timings, problems = samplers.read(self.lines([
            {"window": "a", "layers": {"height": dict(digest, summary={"max": 4})}, "seconds": 0.5},
            {"window": "a", "layers": {"biome": digest}},  # a window's layers may come in several lines
            {"window": "b", "layers": {"height": dict(digest, raster=raster)}}], bom=True), windows)
        self.assertEqual(problems, [])
        self.assertEqual(found["a"], {"height": {"sha256": "ab" * 32, "summary": {"max": 4}},
                                      "biome": {"sha256": "ab" * 32}})
        self.assertEqual((found["b"]["height"]["raster"], timings), (raster, {"a": 0.5}))
        _, _, problems = samplers.read(self.lines([
            {"window": "a", "layers": {"height": digest, "light": digest}},
            {"window": "a", "layers": {"height": digest}},
            {"window": "c", "layers": {"height": digest}},
            {"window": "b", "layers": {"height": dict(digest, raster=dict(raster, values=[1]))}},
            "not JSON"]), windows)
        for expected in ("layer light of window a, which was not asked for", "layer height of window a a second time",
                         "window 'c', which was not asked for", "where width x height is 4", "line 5 is not JSON",
                         "window a has no digest of biome"):
            self.assertTrue(any(expected in problem for problem in problems), (expected, problems))
        self.assertEqual(samplers.read(self.root / "none.jsonl", windows)[2], ["it wrote no none.jsonl"])

    def test_the_verifying_run_never_takes_the_first_runs_order(self):
        for count in range(1, 7):
            windows = [samplers.Window(f"w{i}", 1, ("h",), columns=(0, 0, 1, 1)) for i in range(count)]
            names = [window.name for window in windows]
            shuffled = [window.name for window in goldens.verifying_order(windows, shuffle=True)]
            self.assertEqual([window.name for window in goldens.verifying_order(windows, shuffle=False)], names[::-1])
            self.assertEqual(sorted(shuffled), names)
            self.assertEqual(shuffled, [window.name for window in goldens.verifying_order(windows, shuffle=True)])
            if count > 1:
                self.assertNotEqual(shuffled, names)

    def test_summaries_say_what_changed_key_by_key(self):
        self.assertEqual(goldens.summary_changes({"min": 60, "max": 91, "counts": {"ocean": 3, "hills": 1}},
                                                 {"min": 60, "max": 92, "counts": {"ocean": 3, "plains": 2}}),
                         ["counts.hills 1 -> none", "counts.plains none -> 2", "max 91 -> 92"])
        self.assertEqual(goldens.summary_changes(None, {}), [])

    @unittest.skipIf(np is None, "NumPy and Pillow are not installed")
    def test_a_palette_paints_text_by_name_and_numbers_along_its_stops(self):
        from wb_world import paint
        stops, named = paint.colours({"0": "#000000", "10": "#FFFFFF", "sea": "#0000ff"})
        picture, facts = paint.paint({"width": 3, "height": 2, "values": [0, 5, 10, "sea", "land", 20]},
                                     stops, named, 2)
        pixels = np.asarray(picture)
        self.assertEqual(picture.size, (6, 4))
        self.assertEqual([tuple(map(int, pixels[0, x])) for x in (0, 2, 4)], [(0, 0, 0), (128, 128, 128),
                                                                              (255, 255, 255)])
        self.assertEqual([tuple(map(int, pixels[3, x])) for x in (0, 4)], [(0, 0, 255), (255, 255, 255)])
        self.assertEqual(facts, {"values": 6, "unlisted": ["land"]})  # 20 is held at the last stop
        with self.assertRaisesRegex(ValueError, "not a colour"):
            paint.colours({"sea": "blue"})


class Goldens(unittest.TestCase):
    """world.golden and world.preview through the daemon, over the generator above."""

    @classmethod
    def setUpClass(cls):
        cls.d = Daemon().start()
        cls.c = cls.d.client
        cls.gen = cls.d.data / "gen"
        (cls.gen / "tools").mkdir(parents=True)
        (cls.gen / "tools" / "sampler.py").write_text(SAMPLER, encoding="utf-8")
        cls.fresh = cls.gen / "fresh.json"
        cls.fresh.write_text(json.dumps(FRESH, indent=2), encoding="utf-8")
        cls.updated = cls.golden_job("update", cls.fresh, update=True)
        if cls.updated.outcome != "ok":
            cls.d.stop()
            raise AssertionError(f"update: {cls.updated.summary}")
        cls.golden = cls.gen / "golden.json"
        shutil.copyfile(cls.d.out / "update" / "golden.json", cls.golden)

    @classmethod
    def tearDownClass(cls):
        cls.d.stop()

    @classmethod
    def golden_job(cls, out: str | None = None, golden: Path | None = None, flags=(), **params):
        params = {"golden": str(golden or cls.golden), "sampler": sampler_command(*flags), "cwd": str(cls.gen),
                  **params}
        return cls.c.submit("world.golden", params, out=str(cls.d.out / out) if out else None).result(timeout=120)

    def preview_job(self, out: str | None = None, **params):
        params = {"sampler": sampler_command(), "cwd": str(self.gen), **params}
        return self.c.submit("world.preview", params, out=str(self.d.out / out) if out else None).result(timeout=120)

    def read(self, *parts: str) -> dict:
        return json.loads(self.d.out.joinpath(*parts).read_text(encoding="utf-8"))

    def test_update_writes_the_golden_anew_into_the_output_folder(self):
        result = self.updated
        self.assertEqual(sorted(result.data.new), ["east/biome", "east/height", "west/biome", "west/height"])
        self.assertTrue(result.data.verified)
        self.assertEqual(Path(result.data.updated), self.d.out / "update" / "golden.json")
        self.assertIn("4 new", result.summary)
        written = self.read("update", "golden.json")
        self.assertEqual({key: value for key, value in written.items() if key != "windows"},
                         {key: value for key, value in FRESH.items() if key != "windows"})  # all else is kept
        east = written["windows"][0]
        self.assertEqual(sorted(east["digests"]), ["biome", "height"])
        self.assertRegex(east["digests"]["height"]["sha256"], "^[0-9a-f]{64}$")
        self.assertEqual(sorted(east["digests"]["height"]["summary"]), ["max", "min"])
        self.assertEqual(written["windows"][1]["columns"], [-1, -1, -64, -64])  # the file's own form, kept
        self.assertNotIn("digests", json.loads(self.fresh.read_text(encoding="utf-8"))["windows"][0])
        runs = [(run["threads"], run["order"]) for run in result.data.runs]
        self.assertEqual(runs[0], (1, "forward"))
        self.assertEqual(runs[1][1], "reverse")
        self.assertGreaterEqual(runs[1][0], 2)
        second = self.read("update", "run-2", "windows.json")
        self.assertEqual([window["name"] for window in second["windows"]], ["west", "east"])
        self.assertEqual(second["windows"][0]["columns"], [-64, -64, -1, -1])  # low corner first
        timed = [m["labels"]["run"] for m in result["measurements"] if m["name"] == "sampler_seconds"]
        self.assertEqual(timed, [1, 2])
        windows = sorted((m["labels"]["run"], m["labels"]["window"]) for m in result["measurements"]
                         if m["name"] == "window_seconds")
        self.assertEqual(windows, [(1, "east"), (1, "west"), (2, "east"), (2, "west")])

    def test_a_check_against_the_golden_agrees_and_is_verified(self):
        result = self.golden_job("check")
        self.assertEqual(result.outcome, "ok", result.summary)
        self.assertEqual((result.data.windows, result.data.layers, result.data.verified), (2, 4, True))
        self.assertEqual((result.data.changed, result.data.new, result.data.nondeterministic), ([], [], []))
        self.assertIsNone(result.data.updated)
        self.assertIn("all 4 layers of 2 windows match the golden", result.summary)
        self.assertEqual(self.read("check", "report.json")["windows"]["east"]["height"]["status"], "same")

    def test_a_regression_names_its_window_layer_and_summary(self):
        result = self.golden_job("bumped", flags=["--bump"])
        self.assertEqual(result.outcome, "finding", result.summary)
        self.assertIn("east/height", result.data.changed)
        self.assertNotIn("west/height", result.data.changed)
        self.assertTrue(result.data.verified)  # changed, but the same way both times
        recorded = json.loads(self.golden.read_text(encoding="utf-8"))["windows"][0]["digests"]["height"]["summary"]
        finding = next(line for line in result["findings"] if line.startswith("east:"))
        self.assertIn(f"min {recorded['min']} -> {recorded['min'] + 1}", finding)
        self.assertIn("from the golden", finding)
        report = self.read("bumped", "report.json")
        self.assertEqual((report["windows"]["east"]["height"]["status"], report["windows"]["west"]["height"]["status"]),
                         ("changed", "same"))

    def test_an_order_dependent_sampler_is_caught(self):
        result = self.golden_job("leaky", flags=["--leak"])
        self.assertEqual(result.outcome, "finding", result.summary)
        self.assertFalse(result.data.verified)
        self.assertLessEqual({"east/height", "west/height"}, set(result.data.nondeterministic))
        self.assertTrue(any(line.startswith("west is not deterministic: height") for line in result["findings"]))
        refused = self.golden_job("leaky-update", flags=["--leak"], update=True)
        self.assertEqual(refused.outcome, "finding", refused.summary)
        self.assertIsNone(refused.data.updated)
        self.assertFalse((self.d.out / "leaky-update" / "golden.json").exists())
        self.assertIn("golden.json was not written", refused["findings"][-1])

    def test_layers_without_a_digest_are_new(self):
        result = self.golden_job(golden=self.fresh)
        self.assertEqual(result.outcome, "finding", result.summary)
        self.assertEqual(len(result.data.new), 4)
        self.assertIn("record them with update=true", result.summary)

    def test_one_thread_count_is_one_run_and_shuffle_reorders(self):
        single = self.golden_job(threads=[3])
        self.assertEqual(single.outcome, "ok", single.summary)
        self.assertFalse(single.data.verified)
        self.assertEqual([run["threads"] for run in single.data.runs], [3])
        self.assertIn("determinism not checked", single.summary)
        shuffled = self.golden_job("shuffled", threads=[2, 3], shuffle=True)
        self.assertEqual(shuffled.outcome, "ok", shuffled.summary)
        second = self.read("shuffled", "run-2", "windows.json")
        self.assertEqual(([window["name"] for window in second["windows"]], second["threads"], second["order"]),
                         (["west", "east"], 3, "reverse"))

    def test_a_sampler_that_fails_or_breaks_the_contract_is_an_environment_problem(self):
        failed = self.golden_job("failed", flags=["--fail"])
        self.assertEqual((failed.outcome, failed.code), ("environment", 3))
        self.assertIn("exited 4 on run 1", failed.summary)
        self.assertIn("the generator broke", failed.summary)
        self.assertTrue((self.d.out / "failed" / "run-1" / "sampler.log").is_file())
        dropped = self.golden_job(flags=["--drop"])
        self.assertEqual(dropped.outcome, "environment")
        self.assertIn("broke the sampler contract: window east has no digest of biome", dropped.summary)
        nowhere = self.golden_job(cwd=str(self.d.data))  # no tools/sampler.py there
        self.assertEqual(nowhere.outcome, "environment")

    def test_mistakes_in_the_request_are_usage(self):
        for name, params in (("placeholder", {"sampler": ["{python}", "tools/sampler.py", "{window}", "{out}"]}),
                             ("threads", {"threads": [1, 2, 3]}),
                             ("golden", {"golden": self.gen / "tools" / "sampler.py"})):
            with self.subTest(name):
                self.assertEqual(self.golden_job(**params).outcome, "usage")

    @unittest.skipIf(np is None, "NumPy and Pillow are not installed")
    def test_a_preview_paints_a_golden_window_in_the_files_palette(self):
        result = self.preview_job("preview", golden=str(self.golden), window="east", layer="biome", scale=2)
        self.assertEqual(result.outcome, "ok", result.summary)
        self.assertEqual((result.data.width, result.data.height, result.data.unlisted), (64, 64, []))
        recorded = json.loads(self.golden.read_text(encoding="utf-8"))["windows"][0]["digests"]["biome"]["sha256"]
        self.assertEqual(result.data.sha256, recorded)  # the raster is of the very values the golden holds
        picture = Image.open(self.d.out / "preview" / "east.biome.png")
        self.assertEqual(picture.size, (128, 128))
        self.assertLessEqual({colour for _, colour in picture.convert("RGB").getcolors(1 << 16)},
                             {(0x1f, 0x4e, 0x9c), (0x7f, 0xb2, 0x4d), (0xa0, 0x80, 0x60)})

    @unittest.skipIf(np is None, "NumPy and Pillow are not installed")
    def test_a_preview_of_columns_paints_numbers_along_the_stops(self):
        result = self.preview_job("inline", columns=[-8, -8, 7, 7], seed=4242, layer="height",
                                  palette={"60": "#000000", "91": "#ffffff"})
        self.assertEqual(result.outcome, "ok", result.summary)
        picture = Image.open(self.d.out / "inline" / "preview.height.png")
        self.assertEqual(picture.size, (16, 16))
        self.assertTrue(all(r == g == b for _, (r, g, b) in picture.convert("RGB").getcolors(256)))
        self.assertEqual(result.data.unlisted, [])
        self.assertEqual(self.preview_job(layer="height").outcome, "usage")  # no window
        self.assertEqual(self.preview_job(golden=str(self.golden), window="north", layer="height").outcome, "usage")


if __name__ == "__main__":
    unittest.main()
