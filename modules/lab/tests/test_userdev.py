"""lab.userdev with a real Gradle build of a tiny project whose runClient probe writes a receipt and captures: the
init script moves the task into the run folder and records its class path, a missing or failing receipt is a
finding, inputs changed during the run are a finding, and a broken PNG is caught."""

import json
import os
import shutil
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path[:0] = [str(REPO / "core"), str(REPO / "sdk" / "python"), str(REPO / "modules" / "lab")]

from wb_core.module import Context  # noqa: E402
from wb_lab import userdev  # noqa: E402

GRADLE = shutil.which("gradle")
JDK25 = next(iter(sorted((Path(os.environ.get("ProgramFiles", "/")) / "Eclipse Adoptium").glob("jdk-25*"))),
             Path(os.environ.get("JAVA_HOME_25", "-")))

PROBE = r'''package dev;

import java.nio.file.*;

public final class Probe {
    public static void main(String[] args) throws Exception {
        Path evidence = Paths.get(System.getProperty("wb.run.output"));
        boolean pass = !"false".equals(System.getProperty("probe.pass"));
        Files.writeString(evidence.resolve("receipt.json"),
            "{\"passed\": " + pass + ", \"run\": \"" + System.getProperty("wb.run.id") + "\"}");
        Files.copy(Paths.get(System.getProperty("probe.png")), evidence.resolve("frame.png"));
        Path shots = Paths.get("screenshots");  // the working folder: the run's own
        Files.createDirectories(shots);
        Files.copy(Paths.get(System.getProperty("probe.png")), shots.resolve("shot.png"));
        if ("true".equals(System.getProperty("probe.broken"))) {
            Files.writeString(shots.resolve("broken.png"), "not a picture");
        }
        if (System.getProperty("probe.touch") != null) {
            Files.writeString(Paths.get(System.getProperty("probe.touch")), "changed during the run");
        }
    }
}
'''


def png(width: int, height: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class PngHeader(unittest.TestCase):
    def test_a_png_size_comes_from_its_checked_header(self):
        folder = Path(tempfile.mkdtemp(prefix="wb-png-"))
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        (folder / "a.png").write_bytes(png(3, 2))
        self.assertEqual(userdev.png_size(folder / "a.png"), (3, 2))
        bad = bytearray(png(3, 2))
        bad[20] ^= 0xFF  # the height, under the IHDR checksum
        (folder / "b.png").write_bytes(bytes(bad))
        with self.assertRaises(ValueError):
            userdev.png_size(folder / "b.png")


@unittest.skipUnless(GRADLE and JDK25.exists(), "Gradle or JDK 25 is not installed")
class DevRuns(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="wb-userdev-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.project = self.root / "project"
        (self.project / "src" / "main" / "java" / "dev").mkdir(parents=True)
        (self.project / "settings.gradle").write_text("rootProject.name = 'devrun'\n", encoding="utf-8")
        (self.project / "build.gradle").write_text(
            "plugins { id 'java' }\n"
            "tasks.register('runClient', JavaExec) {\n"
            "    classpath = sourceSets.main.runtimeClasspath\n"
            "    mainClass = 'dev.Probe'\n"
            "}\n", encoding="utf-8")
        (self.project / "src" / "main" / "java" / "dev" / "Probe.java").write_text(PROBE, encoding="utf-8")
        self.picture = self.root / "fixture.png"
        self.picture.write_bytes(png(4, 3))

    def run_dev(self, name: str, **properties):
        ctx = Context({"job": f"job-{name}", "command": "lab.userdev", "module": "lab",
                       "work": str(self.root / name / "work"), "out": str(self.root / name / "out"),
                       "jdks": {"25": str(JDK25)}}, lambda event: None)
        result = userdev.userdev(
            ctx, str(self.project), "runClient", "", [GRADLE, "--offline", "--no-daemon", "-q"], [],
            {"probe.png": str(self.picture), **properties}, ["src/**", "*.gradle"], "receipt.json", "passed",
            ["**/*.png"], 600, "25")
        receipt = json.loads((self.root / name / "out" / "receipt.json").read_text(encoding="utf-8"))
        return ctx, result, receipt

    def test_a_dev_run_keeps_its_class_path_receipt_and_captures(self):
        ctx, result, receipt = self.run_dev("ok")
        self.assertEqual(ctx.outcome, "ok", ctx.findings)
        self.assertEqual(receipt["receipt"]["run"], "job-ok")  # the probe saw wb.run.id
        self.assertTrue(any(entry["kind"] == "folder" for entry in receipt["classpath"]), receipt["classpath"])
        captures = receipt["captures"]
        self.assertEqual((captures["evidence/frame.png"]["width"], captures["evidence/frame.png"]["height"]), (4, 3))
        self.assertIn("run/screenshots/shot.png", captures)  # the task ran in the run folder
        self.assertFalse((self.project / "screenshots").exists())

    def test_a_receipt_that_does_not_pass_is_a_finding(self):
        ctx, result, receipt = self.run_dev("fail", **{"probe.pass": "false"})
        self.assertEqual(ctx.outcome, "finding")
        self.assertIn("does not say passed = true", " ".join(ctx.findings))

    def test_inputs_changed_during_the_run_are_a_finding(self):
        ctx, result, receipt = self.run_dev("changed", **{"probe.touch": str(self.project / "src" / "changed.txt")})
        self.assertIn("changed during the run, such as src/changed.txt", " ".join(ctx.findings))

    def test_a_broken_capture_is_a_finding(self):
        ctx, result, receipt = self.run_dev("broken", **{"probe.broken": "true"})
        self.assertIn("broken capture run/screenshots/broken.png: not a PNG", ctx.findings)


if __name__ == "__main__":
    unittest.main()
