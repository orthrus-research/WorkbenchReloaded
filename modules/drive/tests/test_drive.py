"""drive: tours and their rule sets, camera paths, frame checks and block galleries on tiny synthetic inputs built
here (PNG frames written byte by byte), and the four commands through the daemon."""

import json
import math
import shutil
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO / "tests"))
sys.path.insert(0, str(HERE.parent))

from harness import Daemon  # noqa: E402

from wb_drive import frames, gallery, lint, path, rules, tour  # noqa: E402

try:
    import numpy  # noqa: F401
    import PIL  # noqa: F401
    IMAGING = True
except ImportError:
    IMAGING = False

LAB = {  # a rule set as config would give it
    "band": [-64, 320], "border": 30000, "allow": ["time", "gamerule", "weather", "effect {player} *", "fill",
                                                   "setblock"],
    "settle": [{"view_distance": 8, "settle": "2s", "arrive": "6s"},
               {"view_distance": 16, "settle": "100t", "arrive": "15s"}],
    "view_distance": 12, "min_move": [8, 8], "max_seconds": "10m", "view_overhead": 1,
    "path": {"max_speed": 10, "max_turn": 90},
}
LAB_TOML = """
    [modules.drive.rules.lab]
    band = [-64, 320]
    border = 30000
    allow = ["time", "gamerule", "weather", "effect {player} *", "fill", "setblock"]
    settle = [{ view_distance = 8, settle = "2s", arrive = "6s" },
              { view_distance = 16, settle = "100t", arrive = "15s" }]
    view_distance = 12
    min_move = [8, 8]
    max_seconds = "10m"
    view_overhead = 1
    path = { max_speed = 10, max_turn = 90 }
    """


def view(label, x, y, z, yaw=0, pitch=0, **extra) -> dict:
    return {"label": label, "x": x, "y": y, "z": z, "yaw": yaw, "pitch": pitch, **extra}


def lab(**overrides) -> rules.Rules:
    return rules.resolve({"lab": LAB}, "lab", overrides)


def findings(linted, rule: str) -> list[str]:
    return [f.text() for f in linted.findings if f.rule == rule]


# -- PNG and JPEG writers ------------------------------------------------------------------------------------------
def chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def png(width: int, height: int, pixel, extra: bytes = b"") -> bytes:
    """An RGB PNG, every row unfiltered; extra chunks go between IHDR and IDAT."""
    rows = b"".join(b"\x00" + bytes(v for x in range(width) for v in pixel(x, y)) for y in range(height))
    return (frames.SIGNATURE + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + extra
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def jpeg(width: int, height: int) -> bytes:
    """A JPEG's skeleton: SOI, a baseline frame header, EOI (enough to name its size)."""
    return b"\xff\xd8\xff\xc0" + struct.pack(">HBHHB", 11, 8, height, width, 1) + b"\x01\x11\x00\xff\xd9"


def scene(pose: dict):
    """A picture that moves with the camera: stripes shifted by its x and its yaw."""
    shift = pose["x"] * 3 + pose["yaw"] / 4
    return lambda x, y: (int(127 + 100 * math.sin(2 * math.pi * (x + shift) / 16)),) * 3


def capture(folder: Path, document: dict, name=lambda pose: f"frame-{pose['frame']:04d}.png") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    for pose in document["poses"]:
        (folder / name(pose)).write_bytes(png(48, 24, scene(pose)))
    return folder


def keys(*items) -> list[path.Keyframe]:
    return path.keyframes(list(items))


def kf(label, x, y=64, z=0, yaw=0, pitch=0, **extra) -> dict:
    return {"label": label, "x": x, "y": y, "z": z, "yaw": yaw, "pitch": pitch, **extra}


# -- the tour format -----------------------------------------------------------------------------------------------
class TourFormat(unittest.TestCase):
    def test_a_list_and_a_table_read_the_same_views(self):
        views = [view("a", 0.5, 64, 0.5, 180, 30, commands=["time set 6000"]), view("b", 40, 70, 0, -90, 0)]
        listed, table = tour.parse(views), tour.parse({"viewDistance": {"horizontal": 12, "client": 20},
                                                       "views": views})
        self.assertEqual((listed.shape, table.shape), ("list", "table"))
        self.assertEqual(listed.findings + table.findings, [])
        self.assertEqual([(v.label, v.pos, v.yaw, v.pitch, v.commands, v.capture) for v in listed.views],
                         [(v.label, v.pos, v.yaw, v.pitch, v.commands, v.capture) for v in table.views])
        self.assertEqual(table.view_distance, {"horizontal": 12, "client": 20})
        self.assertEqual(tour.parse({"viewDistance": 16, "views": views}).view_distance, {"": 16})

    def test_settle_in_ticks_or_seconds_and_the_tours_defaults(self):
        parsed = tour.parse({"settleTicks": 40, "dimension": "nether", "views": [
            view("a", 0, 64, 0), view("b", 0, 64, 9, settleSeconds=3.5, dimension=0),
            view("c", 0, 64, 18, settleTicks=10, capture="c-shot.png"), view("d", 0, 64, 30, capture=False)]},
            tick_rate=20)
        self.assertEqual([v.settle for v in parsed.views], [2.0, 3.5, 0.5, 2.0])
        self.assertEqual([v.dimension for v in parsed.views], ["nether", "0", "nether", "nether"])
        self.assertEqual([(v.capture, v.captured) for v in parsed.views],
                         [("a", True), ("b", True), ("c-shot.png", True), (None, False)])

    def test_what_does_not_read_as_the_format_is_a_finding(self):
        parsed = tour.parse({"views": [
            {"label": "a", "x": 0, "y": 64, "z": 0, "yaw": 0, "pich": 3},
            view("b", "1", 64, 0, settleTicks=5, settleSeconds=1),
            view("c", 0, 64, 0, commands="time set 0", capture=3), "d"], "speed": 2, "format": "other/v2"})
        rules_broken = sorted({(f.rule, f.view) for f in parsed.findings}, key=str)
        self.assertIn(("unknown-key", 0), rules_broken)
        self.assertIn(("required", 0), rules_broken)       # no pitch: pich is a typo
        self.assertIn(("type", 1), rules_broken)           # a string where x is a number; both settles
        self.assertIn(("type", 2), rules_broken)           # commands not a list; capture not a name
        self.assertIn(("format", 3), rules_broken)         # a view that is not a table
        self.assertEqual(len([f for f in parsed.findings if f.rule == "format" and f.view is None]), 2)
        self.assertIsNone(parsed.views[1].pos)
        self.assertEqual(tour.parse({"view": []}).shape, "none")
        self.assertEqual(tour.parse(3).findings[0].rule, "format")

    def test_written_views_read_back(self):
        written = tour.document([tour.view("a", (0.5, 64.0, 2.25), 270.0, 12.345678, commands=["time set 6000"],
                                           settle_ticks=40, capture=False, extra={"blocks": ["stone"]})], 12)
        self.assertEqual(written["viewDistance"], 12)
        first = written["views"][0]
        self.assertEqual((first["x"], first["y"], first["yaw"], first["pitch"]), (0.5, 64, -90, 12.35))
        self.assertEqual((first["capture"], first["x-blocks"]), (False, ["stone"]))
        parsed = tour.parse(json.loads(json.dumps(written)))
        self.assertEqual(parsed.findings, [])
        self.assertEqual((parsed.views[0].settle, parsed.views[0].captured), (2.0, False))
        self.assertEqual(tour.document([]), {"views": []})

    def test_headings(self):
        for yaw, direction in ((0, (0, 0, 1)), (90, (-1, 0, 0)), (180, (0, 0, -1)), (-90, (1, 0, 0))):
            self.assertTrue(all(abs(a - b) < 1e-9 for a, b in zip(tour.forward(yaw, 0), direction)), yaw)
            self.assertAlmostEqual(tour.normal_yaw(tour.look(*direction)[0]), tour.normal_yaw(yaw))
        self.assertAlmostEqual(tour.look(0, -1, 1)[1], 45)
        self.assertEqual([tour.normal_yaw(y) for y in (180, -180, 540, -190, 359.5)], [180, 180, 180, 170, -0.5])


# -- rule sets -----------------------------------------------------------------------------------------------------
class RuleSets(unittest.TestCase):
    def test_the_built_in_rules_and_a_named_set_with_overrides(self):
        built_in = rules.resolve({}, "")
        self.assertEqual((built_in.name, built_in.area.band, built_in.area.border), ("built-in", None, 29_999_984))
        self.assertEqual((built_in.fill_limit, built_in.tick_rate, built_in.min_move), (32768, 20.0, None))
        chosen = lab(band=[0, 255], allow=["tp"])
        self.assertEqual((chosen.area.band, chosen.allow, chosen.min_move), ((0.0, 255.0), ["tp"], (8.0, 8.0)))
        self.assertEqual([(r.view_distance, r.settle, r.arrive) for r in chosen.settle], [(8, 2, 6), (16, 5, 15)])
        self.assertEqual((chosen.max_seconds, chosen.path.max_speed, chosen.path.max_accel), (600, 10, 0))
        self.assertEqual(chosen.settle_row(12), (chosen.settle[1], True))
        self.assertEqual(chosen.settle_row(32), (chosen.settle[1], False))
        nether = rules.resolve({"two": {"band": [0, 255], "dimensions": {"0": {}, "-1": {"band": [0, 127]}}}}, "two")
        self.assertEqual((nether.area_of("0").band, nether.area_of("-1").band, nether.area_of("1")),
                         ((0, 255), (0, 127), None))

    def test_rule_sets_that_cannot_be_used_are_refused(self):
        for configured, name, message in (({}, "lab", "no rule set 'lab'"),
                                          ({"x": {"bandd": [0, 1]}}, "x", "unknown keys bandd"),
                                          ({"x": {"band": [10, 0]}}, "x", "is above"),
                                          ({"x": {"settle": [{"settle": "5s"}]}}, "x", "a row is"),
                                          ({"x": {"settle": [{"view_distance": 8, "settle": "soon"}]}}, "x",
                                           "not a duration"),
                                          ({"x": {"min_move": [8]}}, "x", "two numbers"),
                                          ({"x": {"label_pattern": "("}}, "x", "label_pattern"),
                                          ({"x": {"path": {"max_sped": 3}}}, "x", "path"),
                                          ({"x": {"dimensions": {"0": {"bord": 3}}}}, "x", "unknown keys bord")):
            with self.assertRaisesRegex(rules.RuleError, message):
                rules.resolve(configured, name)

    def test_durations_take_seconds_and_ticks(self):
        self.assertEqual([rules.seconds(v, 20) for v in (5, "5s", "250ms", "2m", "100t", "1h", 0.5)],
                         [5, 5, 0.25, 120, 5, 3600, 0.5])
        self.assertEqual(rules.seconds("100t", 40), 2.5)
        for bad in ("-1s", "soon", True, -2):
            with self.assertRaises(rules.RuleError):
                rules.seconds(bad)

    def test_commands_match_by_name_or_pattern(self):
        self.assertTrue(rules.matches("/Fill 0 0 0 1 1 1 minecraft:air", "fill"))
        self.assertFalse(rules.matches("filler 1", "fill"))
        self.assertTrue(rules.matches("gamerule   doDaylightCycle false", "gamerule doDaylightCycle *"))
        self.assertFalse(rules.matches("gamerule randomTickSpeed 0", "gamerule doDaylightCycle *"))
        self.assertTrue(rules.matches("effect {player} minecraft:night_vision 100000 0 true", "effect {player} *"))
        self.assertTrue(rules.matches("kill @e", "kill @e"))
        self.assertFalse(rules.matches("kill @e[type=sheep]", "kill @e"))


# -- the lint ------------------------------------------------------------------------------------------------------
def clean_tour() -> dict:
    return {"viewDistance": {"horizontal": 12, "client": 12}, "views": [
        view("arrive", 0.5, 100, 0.5, 0, 20, settleSeconds=15, commands=["time set 6000", "weather clear 100000"]),
        view("near-01", 0.5, 100, 20.5, 0, 20, settleTicks=100,
             commands=["fill 0 90 30 40 99 60 minecraft:air", "effect {player} minecraft:night_vision 999 0 true"]),
        view("near-02", 12.5, 100, 20.5, 90, 20, settleTicks=100),
        view("far-01", 2000.5, 150, 0.5, 180, 30, settleSeconds=15)]}


class Lint(unittest.TestCase):
    def test_a_clean_tour_keeps_every_rule(self):
        linted = lint.lint(clean_tour(), lab())
        self.assertEqual(linted.findings, [])
        self.assertEqual((linted.view_distance, linted.captures), (12, 4))
        self.assertEqual([row["arrives"] for row in linted.rows], [True, False, False, True])
        self.assertEqual(linted.seconds, 15 + 5 + 5 + 15 + 4)
        self.assertEqual({rule for rule, state in linted.checked.items() if state != "checked"},
                         {"dimension", "view-distance"})
        built_in = lint.lint(clean_tour(), rules.resolve({}, ""))
        self.assertEqual(built_in.findings, [])
        self.assertIn("band", {rule for rule, state in built_in.checked.items() if state != "checked"})

    def test_required_fields_labels_and_rotation(self):
        document = clean_tour()
        del document["views"][1]["yaw"]
        document["views"][2]["label"] = "near 02"
        document["views"][3]["label"] = "arrive"
        document["views"][3]["pitch"] = 95
        linted = lint.lint(document, lab())
        self.assertEqual(findings(linted, "required"), ["required: view 1 (near-01) has no yaw"])
        self.assertEqual(len(findings(linted, "label")), 2)  # the space, and the repeat of view 0's label
        self.assertIn("repeats the label of view 0", " ".join(findings(linted, "label")))
        self.assertEqual(findings(linted, "rotation"), ["rotation: view 3 (arrive) has pitch 95, outside -90..90"])
        self.assertEqual(linted.broken(), ["required", "label", "rotation"])

    def test_height_band_border_and_dimensions(self):
        document = clean_tour()
        document["views"][1]["y"] = -100
        document["views"][2]["x"] = 40000.5
        linted = lint.lint(document, lab())
        self.assertIn("below the height band -64..320", findings(linted, "band")[0])
        self.assertIn("outside the border (30000 blocks either way of 0, 0)", findings(linted, "border")[0])
        two = rules.resolve({"two": {"dimensions": {"0": {"band": [0, 255]}, "-1": {"band": [0, 127]}},
                                     "dimension": "0"}}, "two")
        document = {"views": [view("a", 0, 200, 0), view("b", 0, 200, 0, dimension=-1),
                              view("c", 0, 50, 0, dimension="1")]}
        linted = lint.lint(document, two)
        self.assertEqual([f.view for f in linted.findings if f.rule == "band"], [1])
        self.assertIn("is in dimension 1", findings(linted, "dimension")[0])

    def test_commands_allow_deny_placeholders_and_fills(self):
        document = clean_tour()
        document["views"][1]["commands"] += [
            "stop", "tp {player} 0 100 0", "effect {plyer} minecraft:speed", "fill 0 0 0 100 100 100 minecraft:air",
            "fill ~-100 ~ ~-100 ~100 ~2 ~100 minecraft:air", "fill ~ ~ ~ 5 5 5 minecraft:air", "  ",
            "say a\nsay b"]
        linted = lint.lint(document, lab())
        commands = findings(linted, "command")
        self.assertTrue(any("'stop', which the rule set refuses (stop)" in c for c in commands))
        self.assertTrue(any("allows no tp command" in c for c in commands))
        self.assertTrue(any("unknown placeholder {plyer}" in c for c in commands))
        self.assertTrue(any("empty" in c for c in commands))
        self.assertTrue(any("more than one line" in c for c in commands))
        self.assertEqual(len(findings(linted, "fill")), 2)  # absolute and relative corners; a mixed one is unknown
        self.assertIn("fills 1,030,301 blocks", findings(linted, "fill")[0])
        built_in = lint.lint({"views": [view("a", 0, 64, 0, commands=["kill {player}", "kill @e[type=pig]"])]},
                             rules.resolve({}, ""))
        self.assertEqual(len(findings(built_in, "command")), 1)  # killing the player stalls a tour; pigs do not

    def test_settle_follows_the_table_and_arrivals(self):
        document = clean_tour()
        document["views"][0]["settleSeconds"] = 10   # arrives: needs 15
        document["views"][2]["settleTicks"] = 60     # needs 100t
        del document["views"][3]["settleSeconds"]    # arrives 2000 blocks away, gets the default 0
        linted = lint.lint(document, lab())
        settle = findings(linted, "settle")
        self.assertEqual(len(settle), 3)
        self.assertIn("settles 10 s; at view distance 12, a view that arrives needs 15 s", settle[0])
        self.assertIn("settles 3 s; at view distance 12, a view needs 5 s", settle[1])
        self.assertIn("declares no settle, so gets the default 0 s", settle[2])
        lifted = lint.lint(document, lab(settle_default="20s"))
        self.assertEqual(len(findings(lifted, "settle")), 2)
        near = lint.lint(document, lab(), view_distance=8)  # the command's view distance wins: 2 s, 6 s
        self.assertEqual(len(findings(near, "settle")), 1)
        beyond = lint.lint(document, lab(), view_distance=32)
        self.assertIn("stops at view distance 16", findings(beyond, "settle")[0])
        unknown = lint.lint({"views": document["views"]}, lab(view_distance=0))
        self.assertNotEqual(unknown.checked["settle"], "checked")

    def test_capture_spacing_and_names(self):
        document = {"views": [view("a", 0, 100, 0, settleSeconds=15), view("b", 4, 100, 4, settleSeconds=5),
                              view("c", 4, 100, 4, settleSeconds=5, capture=False),
                              view("d", 4, 109, 4, yaw=90, settleSeconds=5, capture="A")]}
        linted = lint.lint(document, lab())
        moves = findings(linted, "move")
        self.assertEqual(len(moves), 1)
        self.assertIn("view 1 (b) is 8 blocks across and 0 up or down from the capture before (view 0)", moves[0])
        self.assertIn("view 0 captures 'a', the same name but for case", findings(linted, "capture")[0])
        names = {"views": [view(f"step_{n}", n * 20, 100, 0, settleSeconds=15, capture=c)
                           for n, c in ((1, True), (2, True), (10, True), (11, "step_11.png"), (12, "con.png"))]}
        captures = findings(lint.lint(names, lab()), "capture")
        self.assertTrue(any("a name Windows cannot keep" in c for c in captures))
        self.assertTrue(any("mix extensions: none (3), .png (2)" in c for c in captures))
        self.assertTrue(any("number without a fixed width ('step_1', 'step_10')" in c for c in captures))
        padded = {"views": [view(f"step_{n:02d}", n * 20, 100, 0, settleSeconds=15) for n in (1, 2, 10)]}
        self.assertEqual(lint.lint(padded, lab()).findings, [])

    def test_duration_limit(self):
        document = {"views": [view(f"v{n:03d}", n * 20, 100, 0, settleSeconds=15) for n in range(40)]}
        linted = lint.lint(document, lab())
        self.assertEqual(linted.seconds, 40 * 16)
        self.assertIn("the tour takes about 640 s", findings(linted, "duration")[0])
        self.assertEqual(lint.lint(document, lab(max_seconds="15m")).findings, [])


# -- camera paths --------------------------------------------------------------------------------------------------
def speeds(route) -> list:
    return [row["speed"] for row in path.motion(route)]


class Paths(unittest.TestCase):
    def test_constant_pacing_keeps_the_speed_and_reaches_every_stop(self):
        route = path.build(keys(kf("a", 0), kf("b", 10, hold=0), kf("c", 10, z=10)), 20, "linear", "constant", 5)
        self.assertEqual([(s.kind, s.label, s.frames) for s in route.spans],
                         [("hold", "a", 1), ("move", "a-b", 40), ("move", "b-c", 40)])
        self.assertEqual(len(route.poses), 81)
        self.assertEqual((route.poses[40][:3], route.poses[-1][:3]), ((10, 64, 0), (10, 64, 10)))
        self.assertTrue(all(abs(v - 5) < 1e-9 for v in speeds(route)[1:]))
        self.assertAlmostEqual(route.seconds, 4.0)
        self.assertAlmostEqual(route.length, 20.0)

    def test_ease_leaves_and_reaches_rest_over_the_ramp(self):
        route = path.build(keys(kf("a", 0), kf("b", 16)), 20, "linear", "ease", 4, ramp=1)
        self.assertEqual(len(route.poses), 101)  # 4 s at speed, half the ramp at each end
        moving = speeds(route)[1:]
        self.assertLess(moving[0], 0.1)
        self.assertLess(moving[-1], 0.1)
        self.assertAlmostEqual(max(moving), 4.0, places=6)
        _, peaks = path.check(route, {})
        self.assertLess(peaks["accel"], math.pi / 2 * 4 + 0.05)  # a cosine ramp: pi/2 x speed / ramp at most
        short = path.build(keys(kf("a", 0), kf("b", 1)), 20, "linear", "ease", 4, ramp=1)
        self.assertEqual(len(short.poses), 11)  # too short for two ramps: they meet, and it takes twice as long

    def test_holds_and_cuts_make_their_own_segments(self):
        route = path.build(keys(kf("a", 0, hold=1), kf("b", 8), kf("c", 40, yaw=90, cut=True, hold=0.5),
                                kf("d", 48, yaw=90)), 20, "linear", "constant", 4)
        self.assertEqual([(s.kind, s.label, s.frames) for s in route.spans],
                         [("hold", "a", 21), ("move", "a-b", 40), ("cut", "b-c", 1), ("hold", "c", 10),
                          ("move", "c-d", 40)])
        firsts = [s.first for s in route.spans]
        self.assertEqual(firsts, [0, 21, 61, 62, 72])
        self.assertEqual(sum(s.frames for s in route.spans), len(route.poses))
        cut = route.poses[61]
        self.assertEqual((cut[:3], cut[3], cut[6]), ((40, 64, 0), 90, "cut"))
        self.assertEqual(route.poses[60][:3], (8, 64, 0))  # the move reaches b at full speed, then cuts
        motion = path.motion(route)
        self.assertIsNone(motion[61]["speed"])
        self.assertEqual(motion[62]["speed"], 0)
        document = json.loads(json.dumps(route.document()))
        self.assertEqual((document["format"], document["frames"], len(document["segments"])), ("wb/route/v1", 112, 5))
        self.assertEqual(document["poses"][61], {"frame": 61, "t": 3.05, "x": 40, "y": 64, "z": 0, "yaw": 90,
                                                 "pitch": 0, "segment": 2, "phase": "cut"})
        self.assertEqual(path.read(document).frames, 112)

    def test_yaw_takes_the_shorter_way_and_never_wraps(self):
        route = path.build(keys(kf("a", 0, yaw=170), kf("b", 8, yaw=-170), kf("c", 16, yaw=-100)), 20)
        yaws = [pose[3] for pose in route.poses]
        self.assertEqual((yaws[0], yaws[-1]), (170, 260))
        self.assertTrue(all(abs(b - a) < 2 for a, b in zip(yaws, yaws[1:])))
        self.assertTrue(all(a <= b + 1e-9 for a, b in zip(yaws, yaws[1:])))  # monotone: no overshoot, no spin
        ahead = path.build(keys(kf("a", 0, yaw=0), kf("b", 8, yaw=90), kf("c", 16, yaw=90)), 20)
        self.assertLessEqual(max(pose[3] for pose in ahead.poses), 90 + 1e-9)

    def test_a_smooth_curve_passes_through_its_keyframes(self):
        route = path.build(keys(kf("a", 0), kf("b", 10, z=6), kf("c", 20), kf("d", 30, z=6)), 20, "catmull-rom",
                           "constant", 5)
        nearest = min(math.dist(pose[:3], (10, 64, 6)) for pose in route.poses)
        self.assertLess(nearest, 5 / 20)  # within a frame's travel
        self.assertTrue(all(abs(v - 5) < 0.05 for v in speeds(route)[1:]))  # even along the curve's length
        straight = path.build(keys(kf("a", 0), kf("b", 10, z=6), kf("c", 20), kf("d", 30, z=6)), 20, "linear",
                              "constant", 5)
        self.assertGreater(route.length, straight.length)

    def test_a_turn_in_place_lasts_its_turn_at_the_turn_speed(self):
        route = path.build(keys(kf("a", 0, yaw=0), kf("b", 0, yaw=90, pitch=30)), 20, "catmull-rom", "constant",
                           4, turn_speed=45)
        self.assertEqual(route.spans[1].frames, 40)
        self.assertTrue(all(pose[:3] == (0, 64, 0) for pose in route.poses))
        _, peaks = path.check(route, {})
        self.assertAlmostEqual(peaks["turn"], 45)  # a lone segment turns at an even rate
        timed = path.build(keys(kf("a", 0), kf("b", 4, seconds=4)), 20, "linear", "constant")
        self.assertEqual(len(timed.poses), 81)

    def test_limits_name_each_stretch_over_them(self):
        route = path.build(keys(kf("a", 0), kf("b", 40, yaw=180), kf("c", 41, yaw=180, seconds=2)), 20, "linear",
                           "constant", 10, turn_speed=60)
        found, peaks = path.check(route, {"max_speed": 8, "max_turn": 45, "max_accel": 100, "max_seconds": 4})
        rules_broken = [f.rule for f in found]
        self.assertEqual(rules_broken.count("speed"), 1)
        self.assertIn("segment a-b reaches 10.00 blocks/s", [f.message for f in found if f.rule == "speed"][0])
        self.assertIn("turn", rules_broken)
        self.assertIn("accel", rules_broken)      # constant pacing starts and stops at once
        self.assertIn("length", rules_broken)
        self.assertEqual(peaks["speed"], 10)
        self.assertEqual(path.check(route, {})[0], [])

    def test_keyframes_that_cannot_be_read_are_refused(self):
        for document, message in (([], "no keyframes"), ({"frames": []}, "a list"), ([kf("a", 0, pitch=120)], "pitch"),
                                  ([{"x": 0, "y": 0, "z": 0, "yaw": 0}], "needs pitch"),
                                  ([kf("a", 0), kf("b", 1, seconds=0)], "seconds"),
                                  ([kf("a", 0, cut=True)], "first keyframe"),
                                  ([kf("a", 0), kf("b", 1, cut=True, seconds=1)], "takes no time"),
                                  ([kf("a", 0, pich=3)], "unknown keys pich"), ([kf("a", 0, hold=-1)], "hold")):
            with self.assertRaisesRegex(path.PathError, message):
                path.keyframes(document)
        toured = path.keyframes({"views": [view("a", 0, 64, 0, commands=["time set 0"], settleTicks=20)]})
        self.assertEqual(toured[0].label, "a")
        with self.assertRaisesRegex(path.PathError, "format"):
            path.read({"format": "wb/tour/v1"})


# -- frames --------------------------------------------------------------------------------------------------------
class Frames(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="wb-drive-"))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def route(self, *items, pacing="ease") -> path.Read:
        document = path.build(keys(*items), 20, "linear", pacing, 4).document()
        return path.read(json.loads(json.dumps(document)))

    def test_image_data_leaves_out_metadata_and_broken_files_are_named(self):
        plain = png(8, 4, lambda x, y: (x * 30, y * 60, 0))
        stamped = png(8, 4, lambda x, y: (x * 30, y * 60, 0), chunk(b"tIME", bytes(7)) + chunk(b"tEXt", b"a\0b"))
        self.assertEqual(frames.png(plain), frames.png(stamped))
        self.assertEqual(frames.png(plain)[0], (8, 4))
        self.assertNotEqual(frames.png(plain)[1], frames.png(png(8, 4, lambda x, y: (x * 31, y * 60, 0)))[1])
        for broken, message in ((plain[:-20], "truncated"), (plain[:30] + b"\xff" + plain[31:], "CRC"),
                                (b"GIF89a", "not a PNG")):
            with self.assertRaisesRegex(ValueError, message):
                frames.png(broken)
        self.assertEqual(frames.jpeg(jpeg(640, 360))[0], (640, 360))
        with self.assertRaisesRegex(ValueError, "truncated"):
            frames.jpeg(jpeg(640, 360)[:-2])

    def test_missing_and_repeated_frames_against_the_route(self):
        route = self.route(kf("a", 0, hold=0.5), kf("b", 8, hold=0.5))
        folder = capture(self.root / "run", {"poses": route.poses})
        clean = frames.check(folder, route, continuity=False)
        self.assertEqual((clean.findings, clean.mapping, clean.expected), ([], "index", route.frames))
        self.assertEqual([row["kind"] for row in clean.segments], ["hold", "move", "hold"])
        self.assertTrue(any(f.repeats for f in clean.frames))  # a hold repeats its picture, as it should
        (folder / "frame-0030.png").unlink()
        (folder / "frame-0031.png").write_bytes((folder / "frame-0032.png").read_bytes())
        (folder / "frame-0200.png").write_bytes((folder / "frame-0001.png").read_bytes())
        (folder / "notes.png").write_bytes(b"\x89PNG\r\n\x1a\nbroken")
        checked = frames.check(folder, route, continuity=False)
        kinds = [f.rule for f in checked.findings]
        self.assertEqual(checked.missing, [30])
        self.assertIn("path frame 30 has no capture (1 frame, in segment a-b)", [f.message for f in checked.findings])
        self.assertIn("frame-0032.png repeats frame-0031.png while the camera moves",
                      [f.message for f in checked.findings])
        self.assertIn("frame-0200.png shows path frame 200, outside the path's 0-80",
                      [f.message for f in checked.findings])
        self.assertTrue({"frame", "name", "count", "missing", "repeat"} <= set(kinds))
        move = checked.segments[1]
        self.assertEqual((move["missing"], move["repeats"]), (1, 1))
        offset = self.root / "from-one"
        capture(offset, {"poses": route.poses}, lambda pose: f"shot_{pose['frame'] + 1}.png")
        self.assertEqual(frames.check(offset, route, offset=1, continuity=False).findings, [])

    def test_times_in_names_place_frames_and_show_the_pace(self):
        route = self.route(kf("a", 0), kf("b", 4), pacing="constant")
        timed = self.root / "timed"

        def stamped(pose):  # every 50 ms, but 150 ms lost after frame 9
            return f"cap-{1000 + pose['frame'] * 50 + (150 if pose['frame'] > 9 else 0)}.png"
        capture(timed, {"poses": route.poses}, stamped)
        checked = frames.check(timed, route, naming="ms", continuity=False)
        self.assertEqual(checked.mapping, "time")
        self.assertEqual(checked.missing, [10, 11, 12])  # 150 ms lost after frame 9: three frames
        slow = self.root / "slow"
        capture(slow, {"poses": route.poses}, lambda pose: f"f{pose['frame']:03d}-t{pose['frame'] * 75}.png")
        paced = frames.check(slow, route, pattern=r"f(?P<index>\d+)-t(?P<ms>\d+)", continuity=False)
        self.assertEqual((paced.mapping, paced.missing), ("index", []))
        self.assertEqual(paced.pace["medianMs"], 75)
        self.assertIn("frames came every 75.0 ms", [f.message for f in paced.findings if f.rule == "pace"][0])
        self.assertEqual(paced.segments[1]["intervalMs"]["mean"], 75)

    def test_timestamps_in_names(self):
        folder = self.root / "stamps"
        folder.mkdir()
        for name in ("2026-11-21_12.30.05.png", "2026-11-21_12.30.05_2.png", "2026-11-21_12.30.06.png",
                     "2026-11-21_12.30.08.png"):
            (folder / name).write_bytes(png(8, 4, lambda x, y, n=name: (len(n) + x, y, 0)))
        checked = frames.check(folder, None, naming="timestamp", fps=1, continuity=False)
        self.assertEqual([f.frame for f in sorted(checked.frames, key=lambda f: (f.time, f.suffix))], [0, 0, 1, 3])
        self.assertEqual(checked.missing, [2])
        self.assertTrue(any(f.rule == "duplicate" for f in checked.findings))
        unnamed = self.root / "unnamed"
        unnamed.mkdir()
        (unnamed / "first.png").write_bytes(png(8, 4, lambda x, y: (x, y, 0)))
        with self.assertRaisesRegex(ValueError, "no frame name"):
            frames.check(unnamed, None, naming="index", continuity=False)

    @unittest.skipUnless(IMAGING, "needs NumPy and Pillow")
    def test_continuity_finds_a_jump_and_a_change_while_the_camera_holds(self):
        route = self.route(kf("a", 0, hold=1), kf("b", 8))
        folder = capture(self.root / "run", {"poses": route.poses})
        smooth = frames.check(folder, route, scale=24)
        self.assertEqual((smooth.continuity, smooth.spikes, smooth.findings), ("checked", 0, []))
        (folder / "frame-0040.png").write_bytes(png(48, 24, lambda x, y: (255 - x, 0, y * 9)))
        (folder / "frame-0010.png").write_bytes(png(48, 24, lambda x, y: (40, 40, 40)))
        jumped = frames.check(folder, route, scale=24)
        flagged = {f.name for f in jumped.frames if f.flag == "spike"}
        self.assertIn("frame-0040.png", flagged)
        self.assertIn("frame-0010.png", flagged)
        holding = [f.message for f in jumped.findings if "frame-0010.png" in f.message][0]
        self.assertIn("while the camera holds", holding)
        cut = self.route(kf("a", 0), kf("b", 8), kf("c", 80, yaw=180, cut=True), kf("d", 88, yaw=180))
        folder = capture(self.root / "cut", {"poses": cut.poses})
        across = frames.check(folder, cut, scale=24)
        self.assertEqual(across.spikes, 0)
        self.assertEqual([f.flag for f in across.frames if f.frame == cut.segments[2]["first"]], ["cut"])


# -- galleries -----------------------------------------------------------------------------------------------------
def blocks(*texts) -> list[gallery.Block]:
    return [gallery.parse_block(text) for text in texts]


class Galleries(unittest.TestCase):
    def test_block_forms_and_labels(self):
        parsed = blocks("minecraft:stone", "minecraft:stone 1", "granite=minecraft:stone 1",
                        "minecraft:log axis=y,variant=oak", "minecraft:log[axis=x,variant=birch]", "glass")
        self.assertEqual([b.label for b in parsed],
                         ["stone", "stone-1", "granite", "log-y-oak", "log-x-birch", "glass"])
        self.assertEqual([b.spec("legacy") for b in parsed[2:5]],
                         ["minecraft:stone 1", "minecraft:log axis=y,variant=oak",
                          "minecraft:log axis=x,variant=birch"])
        self.assertEqual(parsed[4].spec("flattened"), "minecraft:log[axis=x,variant=birch]")
        with self.assertRaisesRegex(gallery.GalleryError, "data values"):
            parsed[1].spec("flattened")
        for bad in ("Minecraft:Stone", "minecraft:stone axis", "minecraft:log[axis=y", ""):
            with self.assertRaises(gallery.GalleryError):
                gallery.parse_block(bad)
        listed = gallery.catalog({"blocks": ["minecraft:dirt", {"id": "minecraft:wool", "state": 14, "label": "red"},
                                             {"id": "minecraft:stone"}]})
        self.assertEqual([(b.id, b.state, b.label) for b in listed],
                         [("minecraft:dirt", "", "dirt"), ("minecraft:wool", "14", "red"),
                          ("minecraft:stone", "", "stone")])
        with self.assertRaisesRegex(gallery.GalleryError, "unknown keys"):
            gallery.catalog([{"id": "minecraft:stone", "meta": 1}])
        made, found = gallery.unique(blocks("minecraft:stone", "stone=minecraft:dirt", "minecraft:stone"))
        self.assertEqual([b.label for b in made], ["stone", "stone-2", "stone-3"])
        self.assertEqual([f.rule for f in found], ["blocks"])

    def test_rows_read_left_to_right_whatever_the_facing(self):
        listed = blocks(*(f"minecraft:wool {n}" for n in range(7)))
        for facing in gallery.FACINGS:
            plan = gallery.layout(listed, (100, 64, -50), facing, 4, 3, 9, 6, 20, 70, 16 / 9, "g")
            self.assertEqual([len(row) for row in plan.rows], [4, 3])
            for row, camera in zip(plan.rows, plan.cameras):
                ahead = tour.forward(camera.yaw, 0)
                right = (-ahead[2], ahead[0])
                across = [(p.pos[0] + 0.5 - camera.pos[0]) * right[0] + (p.pos[2] + 0.5 - camera.pos[2]) * right[1]
                          for p in row]
                self.assertEqual(across, sorted(across), facing)            # column 0 on the left
                self.assertAlmostEqual(sum(across) / len(across), 0, places=9)  # the camera centred on its row
                middle = tuple(sum(p.pos[i] + 0.5 for p in row) / len(row) for i in range(3))
                reach = 6 / math.cos(math.radians(20))
                seen = tuple(e + a * reach for e, a in zip(camera.eye, tour.forward(camera.yaw, camera.pitch)))
                self.assertTrue(all(abs(a - b) < 1e-9 for a, b in zip(seen, middle)), facing)  # it looks at the middle
            ahead = FACING = gallery.FACINGS[facing]
            middles = [(sum(p.pos[0] for p in row) / len(row), sum(p.pos[2] for p in row) / len(row))
                       for row in plan.rows]
            depth = (middles[1][0] - middles[0][0]) * FACING[0] + (middles[1][1] - middles[0][1]) * FACING[1]
            sideways = (middles[1][0] - middles[0][0]) * -ahead[1] + (middles[1][1] - middles[0][1]) * ahead[0]
            self.assertEqual(depth, 9)                    # the next row steps away from the cameras
            self.assertLessEqual(abs(sideways), 0.5)     # each row centred, to the nearest block
            self.assertTrue(all(isinstance(v, int) for p in plan.placed for v in p.pos))

    def test_fills_split_under_the_limit(self):
        for low, high, limit in (((0, 0, 0), (99, 9, 99), 32768), ((0, 0, 0), (255, 255, 0), 100),
                                 ((5, 5, 5), (-5, -5, -5), 7), ((0, 0, 0), (3, 3, 3), 0)):
            commands = gallery.fills(low, high, "minecraft:air", limit)
            total = 0
            for command in commands:
                words = command.split()
                a, b = list(map(int, words[1:4])), list(map(int, words[4:7]))
                volume = math.prod(y - x + 1 for x, y in zip(a, b))
                self.assertTrue(volume <= limit or not limit, command)
                total += volume
            self.assertEqual(total, math.prod(abs(b - a) + 1 for a, b in zip(low, high)))

    def test_the_spacing_check_finds_rows_that_do_not_read(self):
        listed = blocks(*(f"minecraft:wool {n}" for n in range(12)))

        def problems(**changes):
            settings = dict(columns=6, spacing=3, row_spacing=9, distance=7, pitch=20) | changes
            plan = gallery.layout(listed, (0, 64, 0), "south", settings["columns"], settings["spacing"],
                                  settings["row_spacing"], settings["distance"], settings["pitch"], 70, 16 / 9, "g")
            return " | ".join(f.message for f in gallery.spacing_check(plan, 70, 1920, 1080, 8, 16))
        self.assertEqual(problems(), "")
        self.assertIn("does not fit", problems(columns=12))
        self.assertIn("leave 0 px between them", problems(spacing=1))
        self.assertIn("16 px shows its texture", problems(distance=60, row_spacing=62))
        self.assertIn("stands in or before the camera", problems(row_spacing=5))
        self.assertIn("inside the platform", problems(pitch=-30))


# -- the commands, through the daemon -----------------------------------------------------------------------------
class Commands(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = Daemon(LAB_TOML).start()
        cls.c = cls.d.client

    @classmethod
    def tearDownClass(cls):
        cls.d.stop()

    def run_command(self, command: str, out: str | None = None, **params):
        return self.c.submit(command, params, out=str(self.d.out / out) if out else None).result(timeout=120)

    def write(self, name: str, document) -> str:
        target = self.d.data / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(document), encoding="utf-8")
        return str(target)

    def report(self, out: str, name: str = "report") -> dict:
        return json.loads((self.d.out / out / f"{name}.json").read_text(encoding="utf-8"))

    def test_tour_lint_passes_a_good_tour_and_names_each_violation(self):
        good = self.write("good.json", clean_tour())
        passed = self.run_command("drive.tour-lint", "lint-good", tour=good, rules="lab")
        self.assertEqual(passed.outcome, "ok", passed.summary)
        self.assertEqual((passed.data.views, passed.data.captures, passed.data.seconds), (4, 4, 44))
        self.assertIn("keep every rule of the lab rules", passed.summary)
        bad = clean_tour()
        bad["views"][1]["commands"].append("stop")
        bad["views"][2].update(x=4.5, settleTicks=20)
        bad["views"].append(view("near-01", 0.5, 400, 20.5))
        failed = self.run_command("drive.tour-lint", "lint-bad", tour=self.write("bad.json", bad), rules="lab")
        self.assertEqual((failed.outcome, failed.code), ("finding", 1))
        self.assertEqual(failed.data.rules, ["label", "band", "command", "settle", "move"])
        report = self.report("lint-bad")
        self.assertEqual(report["schema"], "wb/drive-tour-lint/v1")
        self.assertEqual(report["rules"]["name"], "lab")
        self.assertEqual(report["checked"]["settle"]["state"], "checked")
        self.assertEqual(len(report["findings"]), failed.data.findings)
        self.assertEqual([row["needs"] for row in report["rows"]], [15, 5, 5, 15, 15])
        overridden = self.run_command("drive.tour-lint", tour=good, rules="lab", band=[0, 50])
        self.assertEqual(overridden.data.rules, ["band"])
        built_in = self.run_command("drive.tour-lint", tour=self.write("bad-built-in.json", bad))
        self.assertEqual(built_in.data.rules, ["label", "command"])

    def test_path_then_path_check_of_frames_captured_along_it(self):
        keyframes = self.write("keys.json", {"keyframes": [kf("start", 0, hold=0.5), kf("turn", 12, z=6, yaw=30),
                                                           kf("end", 24, yaw=60, hold=0.5)]})
        made = self.run_command("drive.path", "path", keyframes=keyframes, rules="lab", output="both", every=10,
                                settle="1s", speed=6)
        self.assertEqual(made.outcome, "ok", made.summary)
        route = self.report("path", "route")
        self.assertEqual((route["format"], route["frames"]), ("wb/route/v1", made.data.frames))
        self.assertLessEqual(made.data.max_speed, 6 + 1e-6)
        self.assertIn("within the speed, turn limits", made.summary)
        toured = self.report("path", "tour")
        self.assertEqual([v["settleTicks"] for v in toured["views"][:2]], [300, 100])  # the table's, over 1 s
        self.assertEqual((toured["views"][0]["label"], toured["views"][-1]["label"]),
                         ("path-0000", f"path-{route['frames'] - 1:04d}"))
        for a, b in zip(toured["views"], toured["views"][1:]):  # past the rule set's least move, every one
            self.assertTrue(abs(b["x"] - a["x"]) + abs(b["z"] - a["z"]) > 8 or abs(b["y"] - a["y"]) > 8)
        fast = self.run_command("drive.path", keyframes=keyframes, rules="lab", speed=14)
        self.assertEqual(fast.outcome, "finding")
        self.assertIn("speed: segment", fast.summary)
        frames_dir = capture(self.d.data / "captured", route)
        checked = self.run_command("drive.path-check", "check", frames=str(frames_dir),
                                   route=str(self.d.out / "path" / "route.json"))
        self.assertEqual(checked.outcome, "ok", checked.summary)
        self.assertEqual((checked.data.frames, checked.data.expected, checked.data.continuity),
                         (route["frames"], route["frames"], IMAGING))
        report = self.report("check")
        self.assertEqual([row["kind"] for row in report["segments"]], ["hold", "move", "move", "hold"])
        self.assertEqual(len(report["rows"]), route["frames"])
        (frames_dir / "frame-0020.png").unlink()
        (frames_dir / "frame-0041.png").write_bytes((frames_dir / "frame-0040.png").read_bytes())
        broken = self.run_command("drive.path-check", frames=str(frames_dir),
                                  route=str(self.d.out / "path" / "route.json"))
        self.assertEqual(broken.outcome, "finding")
        self.assertEqual((broken.data.missing, broken.data.repeats), (1, 1))
        unrouted = self.run_command("drive.path-check", frames=str(frames_dir))
        self.assertEqual(unrouted.data.expected, None)
        self.assertGreater(unrouted.data.repeats, 1)  # without the route, a hold's frames read as stuck

    def test_gallery_writes_a_tour_that_keeps_its_rules(self):
        catalog = self.write("catalog.json", ["minecraft:planks 2",
                                              {"id": "minecraft:log", "state": "axis=y,variant=oak"}])
        made = self.run_command("drive.gallery", "gallery", blocks=[f"minecraft:wool {n}" for n in range(10)],
                                catalog=catalog, origin=[200, 120, -40], columns=6, rules="lab",
                                setup=["time set 6000", "gamerule doDaylightCycle false"])
        self.assertEqual(made.outcome, "ok", made.summary)
        self.assertEqual((made.data.blocks, made.data.rows, made.data.views), (12, 2, 4))
        written = self.report("gallery", "tour")
        labels = [v["label"] for v in written["views"]]
        self.assertEqual(labels, ["gallery-arrive", "gallery-00", "gallery-01", "gallery-overview"])
        arrive, first = written["views"][0], written["views"][1]
        self.assertEqual((arrive["capture"], arrive["settleTicks"], first["settleTicks"]), (False, 300, 100))
        self.assertEqual(arrive["commands"], ["time set 6000", "gamerule doDaylightCycle false"])
        self.assertEqual(first["commands"][0].split()[0], "fill")
        self.assertIn("setblock", first["commands"][-1])
        self.assertEqual(first["x-blocks"], ["wool-0", "wool-1", "wool-2", "wool-3", "wool-4", "wool-5"])
        report = self.report("gallery")
        self.assertEqual((report["rowSpacing"], len(report["legend"]), report["tour"]["findings"]), (12, 12, []))
        linted = self.run_command("drive.tour-lint", tour=str(self.d.out / "gallery" / "tour.json"), rules="lab")
        self.assertEqual(linted.outcome, "ok", linted.summary)
        crowded = self.run_command("drive.gallery", blocks=["minecraft:stone", "minecraft:dirt"], origin=[0, 100, 0],
                                   spacing=1, row_spacing=4, rules="lab")
        self.assertEqual(crowded.outcome, "finding")
        self.assertIn("leave 0 px between them", crowded.summary)

    def test_usage_errors(self):
        tour_file = self.write("usage-tour.json", clean_tour())
        for command, params, message in (
                ("drive.tour-lint", {"tour": tour_file, "rules": "nope"}, "no rule set 'nope'"),
                ("drive.tour-lint", {"tour": self.write("not-a-tour.json", {"view": []})}, "a tour is a list"),
                ("drive.path", {"keyframes": self.write("one-bad.json", [kf("a", 0, pitch=100)])}, "pitch"),
                ("drive.path-check", {"frames": str(self.d.data), "route": tour_file}, "a route is"),
                ("drive.gallery", {"blocks": ["Minecraft:Stone"], "origin": [0, 64, 0]}, "is not a block"),
                ("drive.gallery", {"blocks": ["minecraft:stone"], "origin": [0, 64]}, "origin")):
            result = self.run_command(command, **params)
            self.assertEqual(result.outcome, "usage", f"{command}: {result.summary}")
            self.assertIn(message, result.summary)


class AgentSend(unittest.TestCase):
    """drive.send against a fake agent that answers its inbox the way the mod does."""

    def setUp(self):
        import threading
        self.root = Path(tempfile.mkdtemp(prefix="wb-send-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.inbox, self.outbox = self.root / "wb-inbox", self.root / "wb-outbox"
        self.inbox.mkdir()
        self.outbox.mkdir()
        self.stop = threading.Event()
        self.seen = []
        self.agent = threading.Thread(target=self.serve, daemon=True)
        self.agent.start()
        self.addCleanup(self.stop.set)

    def serve(self):
        import os
        import time
        while not self.stop.is_set():
            for request in sorted(self.inbox.glob("*.json")):
                body = json.loads(request.read_text(encoding="utf-8"))
                self.seen.append(body)
                if body["action"] == "status":
                    reply = {"id": request.stem, "ok": True, "result": {"world": True, "fps": 60}}
                elif body["action"] == "quit":
                    continue  # this agent never answers a quit
                else:
                    reply = {"id": request.stem, "ok": False, "error": "IllegalStateException: no screen is open"}
                part = self.outbox / f"{request.stem}.json.part"
                part.write_text(json.dumps(reply), encoding="utf-8")
                os.replace(part, self.outbox / f"{request.stem}.json")
                request.unlink()
            time.sleep(0.05)

    def send(self, action, args=None, timeout=5):
        from wb_core.module import Context
        from wb_drive import agent
        ctx = Context({"job": f"j-{action}", "command": "drive.send", "module": "drive", "work": str(self.root / "w"),
                       "out": str(self.root / "o" / action)}, lambda event: None)
        return ctx, agent.send(ctx, str(self.inbox), action, args or {}, timeout)

    def test_an_answer_comes_back(self):
        ctx, result = self.send("status")
        self.assertEqual(ctx.outcome, "ok", ctx.findings)
        self.assertEqual(result["answer"]["result"], {"world": True, "fps": 60})
        self.assertEqual(list(self.outbox.iterdir()), [])  # the answer is taken

    def test_a_refusal_and_no_answer_are_findings(self):
        ctx, _ = self.send("click", {"x": 1, "y": 2})
        self.assertEqual(ctx.findings, ["the agent refused click: IllegalStateException: no screen is open"])
        self.assertEqual(self.seen[-1], {"action": "click", "x": 1, "y": 2})
        ctx, result = self.send("quit", timeout=0.5)
        self.assertIsNone(result["answer"])
        self.assertIn("no answer to quit", ctx.findings[0])
        self.assertEqual(list(self.inbox.glob("*.json")), [])  # the unanswered request is taken back

    def test_requests_it_cannot_send_are_refused(self):
        from wb_core.module import ParamError
        with self.assertRaises(ParamError):
            self.send("fly")
        with self.assertRaises(ParamError):
            self.send("status", {"action": "quit"})


if __name__ == "__main__":
    unittest.main()
