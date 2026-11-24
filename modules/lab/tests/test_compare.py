"""compare_instances.py --host --container: two instances of one launch file built on two systems are the same apart
from natives, line endings and the files that name their system; any other difference is unexplained, and a tar of
an instance reads as its folder does."""

import json
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent / "tools"), str(HERE.parents[2] / "sdk" / "python")]

import compare_instances as compare  # noqa: E402
from wb.platform import remove_tree  # noqa: E402


def receipt(system: str, natives: str) -> bytes:
    return json.dumps({"system": system, "key": system[:3], "files": {
        "server/mods/a.jar": {"sha256": "a" * 64}, f"client/natives/{natives}": {"sha256": natives[0] * 64}}}).encode()


class Peers(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="wb-compare-"))
        self.addCleanup(remove_tree, self.root)
        self.host = self.make("host", {"server/mods/a.jar": b"jar", "server/eula.txt": b"eula=true\r\n",
                                       "client/natives/lwjgl.dll": b"win",
                                       "instance.json": receipt("windows", "lwjgl.dll")})
        self.container = self.make("container", {"server/mods/a.jar": b"jar", "server/eula.txt": b"eula=true\n",
                                                 "client/natives/liblwjgl.so": b"elf",
                                                 "instance.json": receipt("linux", "liblwjgl.so")})

    def make(self, name: str, files: dict) -> Path:
        folder = self.root / name
        for rel, data in files.items():
            (folder / rel).parent.mkdir(parents=True, exist_ok=True)
            (folder / rel).write_bytes(data)
        return folder

    def test_the_same_apart_from_natives(self):
        report = compare.compare_peers(compare.instance_files(self.host), compare.instance_files(self.container))
        self.assertTrue(report["same_apart_from_natives"], report["unexplained"])
        self.assertEqual(report["natives"], ["client/natives/liblwjgl.so", "client/natives/lwjgl.dll"])
        self.assertEqual(report["line_endings"], ["server/eula.txt"])
        self.assertIn("instance.json", report["by_system"])
        self.assertEqual(report["receipt"]["system"], ["windows", "linux"])

    def test_another_difference_is_unexplained_and_a_tar_reads_as_its_folder(self):
        (self.container / "server" / "mods" / "a.jar").write_bytes(b"other")
        archive = self.root / "container.tar"
        with tarfile.open(archive, "w") as tar:
            tar.add(self.container, arcname="rental")
        report = compare.compare_peers(compare.instance_files(self.host), compare.instance_files(archive))
        self.assertFalse(report["same_apart_from_natives"])
        self.assertEqual([item["path"] for item in report["unexplained"]], ["server/mods/a.jar"])


if __name__ == "__main__":
    unittest.main()
