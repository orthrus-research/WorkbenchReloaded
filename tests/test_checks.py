"""The absorbed checks through the daemon: typed frames (cached, path-translated) and the orthrus tools adapter."""

import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import Daemon  # noqa: E402


def frames(folder: Path, shift: int = 0, names=("a.png", "b.png")) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    for index, name in enumerate(names):
        pixels = np.full((32, 32, 3), 40 + index * 30, dtype=np.uint8)
        if shift:
            pixels[:16, :, :] = np.clip(pixels[:16, :, :].astype(int) + shift, 0, 255).astype(np.uint8)
        Image.fromarray(pixels).save(folder / name)
    return folder


class Checks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = Daemon().start()
        cls.c = cls.d.client
        cls.ref = frames(cls.d.data / "ref")
        cls.same = frames(cls.d.data / "same")
        cls.off = frames(cls.d.data / "off", shift=60)

    @classmethod
    def tearDownClass(cls):
        cls.d.stop()

    def test_compare_passes_fails_and_caches(self):
        ok = self.c.submit("frames.compare", {"reference": str(self.ref), "candidate": str(self.same),
                                              "profile": "exact"}).result(timeout=120)
        self.assertEqual(ok.outcome, "ok", ok.summary)
        self.assertTrue(ok.data.passed)
        bad = self.c.submit("frames.compare", {"reference": str(self.ref), "candidate": str(self.off),
                                               "profile": "near"}, out=str(self.d.out / "cmp")).result(timeout=120)
        self.assertEqual((bad.outcome, bad.code), ("finding", 1))
        self.assertEqual(sorted(bad.data.failed), ["a.png", "b.png"])
        self.assertTrue((self.d.out / "cmp" / "report.json").exists())
        again = self.c.submit("frames.compare", {"reference": str(self.ref), "candidate": str(self.same),
                                                 "profile": "exact"}).result(timeout=120)
        self.assertTrue(again["cache"]["hit"])
        frames(self.d.data / "same", shift=1)  # inputs changed: the cache key changes with their hashes
        changed = self.c.submit("frames.compare", {"reference": str(self.ref), "candidate": str(self.same),
                                                   "profile": "exact"}).result(timeout=120)
        self.assertFalse(changed["cache"]["hit"])
        frames(self.d.data / "same")

    def test_quality(self):
        result = self.c.submit("frames.quality", {"reference": str(self.ref), "candidate": str(self.off)}).result(timeout=120)
        self.assertEqual(result.outcome, "ok", result.summary)
        self.assertIn("psnr", {m["name"] for m in result["measurements"]})

    def test_tools_adapter_maps_the_contract(self):
        import zipfile
        listed = self.c.submit("toolchain.jdk", {"argv": ["list"]}).result(timeout=120)
        self.assertIn(listed.outcome, ("ok", "environment"), listed.summary)
        self.assertEqual(listed.data.tool if listed.outcome == "ok" else "jdk", "jdk")
        outside = Path(tempfile.mkdtemp(prefix="wb-outside-")) / "x.jar"
        with zipfile.ZipFile(outside, "w") as archive:
            archive.writestr("a/B.class", b"not a class file")
        # The JAR is outside the configured roots, so the adapter refuses it with a usage error.
        result = self.c.submit("jar.audit", {"argv": [str(outside)]}).result(timeout=120)
        self.assertEqual(result.outcome, "usage", result.summary)
        inside = self.d.data / "x.jar"
        inside.write_bytes(outside.read_bytes())
        result = self.c.submit("jar.audit", {"argv": [str(inside)]}).result(timeout=120)
        self.assertEqual((result.outcome, result.code), ("finding", 1), result.summary)
        self.assertEqual(result.data.tool, "jar-audit")  # the orthrus tool behind jar.audit
        former = self.c.submit("tools.jar-audit", {"argv": [str(inside)]}).result(timeout=120)
        self.assertEqual(former.command, "jar.audit")    # a former name resolves to the command
        self.assertEqual(former.outcome, "finding")

    def test_the_scrub_reads_the_machines_private_terms(self):
        import subprocess
        terms = self.d.root / "config" / "scrub-terms.txt"  # the machine's config folder, never a repository
        terms.write_text("Quillfeather\n", encoding="utf-8")
        d = Daemon(f"[modules.ci]\ntool_env = {{ ORTHRUS_SCRUB_TERMS_FILE = '{terms.as_posix()}' }}\n").start()

        def staged_repo(data: Path) -> Path:
            repo = data / "repo"
            repo.mkdir()
            git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
            subprocess.run([*git, "init", "-q"], cwd=repo, check=True)
            (repo / "notes.txt").write_text("Thanks to Quillfeather for the review.\n", encoding="utf-8")
            subprocess.run([*git, "add", "notes.txt"], cwd=repo, check=True)
            return repo
        try:
            found = d.client.submit("ci.scrub", {"argv": ["staged"], "cwd": str(staged_repo(d.data))})
            found = found.result(timeout=120)
            self.assertEqual(found.outcome, "finding", found.summary)  # the term, from the machine's file
            plain = self.c.submit("ci.scrub", {"argv": ["staged"], "cwd": str(staged_repo(self.d.data))})
            plain = plain.result(timeout=120)
            self.assertEqual(plain.outcome, "ok", plain.summary)  # a daemon without the file does not know it
        finally:
            d.stop()


if __name__ == "__main__":
    unittest.main()
