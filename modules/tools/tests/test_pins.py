import hashlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orthrus_tools import pins

PAYLOAD = b"pinned bytes"
DIGEST = hashlib.sha256(PAYLOAD).hexdigest()
URL = "https://example.invalid/thing.jar"


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def serve(payload=PAYLOAD):
    return mock.patch.object(
        pins.urllib.request, "urlopen", side_effect=lambda *a, **k: FakeResponse(payload)
    )


class PinsTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        # The machine's shared pin cache (a CI step always has one) is not these tests' to read: a file kept there
        # would answer a fetch the test expects to download.
        cache = os.environ.pop(pins.CACHE_ENV, None)
        if cache is not None:
            self.addCleanup(os.environ.__setitem__, pins.CACHE_ENV, cache)

    def tearDown(self):
        self.temporary.cleanup()

    def lock(self, entries):
        (self.root / "pins.lock.json").write_text(json.dumps({"pins": entries}), encoding="utf-8")

    def run_tool(self, *args):
        return pins.main(["--root", str(self.root), *args])

    def test_fetch_verifies_and_is_idempotent(self):
        self.lock([{"name": "thing.jar", "url": URL, "sha256": DIGEST}])
        self.assertEqual(self.run_tool("verify"), 1)
        with serve():
            self.assertEqual(self.run_tool("fetch"), 0)
        target = self.root / ".local" / "pins" / "thing.jar"
        self.assertEqual(target.read_bytes(), PAYLOAD)
        self.assertEqual(self.run_tool("verify"), 0)
        with mock.patch.object(pins.urllib.request, "urlopen", side_effect=AssertionError):
            self.assertEqual(self.run_tool("fetch"), 0)

    def test_the_pin_cache_serves_a_second_tree_without_downloading(self):
        cache = self.root / "cache"
        self.lock([{"name": "thing.jar", "url": URL, "sha256": DIGEST}])
        with mock.patch.dict(pins.os.environ, {"WB_PIN_CACHE": str(cache)}):
            with serve():
                self.assertEqual(self.run_tool("fetch"), 0)
            self.assertEqual((cache / DIGEST).read_bytes(), PAYLOAD)  # kept under its digest
            (self.root / ".local" / "pins" / "thing.jar").unlink()  # a fresh tree has none
            with mock.patch.object(pins.urllib.request, "urlopen", side_effect=AssertionError):
                self.assertEqual(self.run_tool("fetch"), 0)  # from the cache, no network
            self.assertEqual((self.root / ".local" / "pins" / "thing.jar").read_bytes(), PAYLOAD)
            (cache / DIGEST).write_bytes(b"damaged")  # a damaged entry is never used
            (self.root / ".local" / "pins" / "thing.jar").unlink()
            with serve():
                self.assertEqual(self.run_tool("fetch"), 0)
            self.assertEqual((self.root / ".local" / "pins" / "thing.jar").read_bytes(), PAYLOAD)

    def test_a_mismatched_download_never_lands(self):
        self.lock([{"name": "thing.jar", "url": URL, "sha256": DIGEST, "path": "inputs/t.jar"}])
        with serve(b"tampered"):
            self.assertEqual(self.run_tool("fetch"), 1)
        self.assertFalse((self.root / "inputs" / "t.jar").exists())
        self.assertFalse((self.root / "inputs" / "t.jar.part").exists())

    def test_a_differing_local_file_is_replaced(self):
        self.lock([{"name": "thing.jar", "url": URL, "sha256": DIGEST, "path": "t.jar"}])
        (self.root / "t.jar").write_bytes(b"stale")
        self.assertEqual(self.run_tool("verify"), 1)
        with serve():
            self.assertEqual(self.run_tool("fetch"), 0)
        self.assertEqual((self.root / "t.jar").read_bytes(), PAYLOAD)

    def test_add_records_the_digest(self):
        with serve():
            self.assertEqual(self.run_tool("add", "thing.jar", URL), 0)
        data = json.loads((self.root / "pins.lock.json").read_text(encoding="utf-8"))
        self.assertEqual(data["pins"], [{"name": "thing.jar", "url": URL, "sha256": DIGEST}])
        with serve():
            self.assertEqual(self.run_tool("add", "thing.jar", URL), 2)

    def test_unsafe_entries_are_refused(self):
        bad = [
            {"name": "a", "url": "http://example.invalid/a", "sha256": DIGEST},
            {"name": "a", "url": URL, "sha256": "abc"},
            {"name": "a", "url": URL, "sha256": DIGEST, "path": "../outside"},
            {"name": "a", "url": URL, "sha256": DIGEST, "path": "C" + ":/abs"},
        ]
        for entry in bad:
            with self.assertRaises(ValueError):
                pins.Pin.from_dict(entry)
        self.lock([{"name": "a", "url": URL, "sha256": DIGEST}] * 2)
        self.assertEqual(self.run_tool("verify"), 2)

    def test_network_failures_are_retried_then_reported(self):
        self.lock([{"name": "thing.jar", "url": URL, "sha256": DIGEST}])
        calls = []

        def flaky(*args, **kwargs):
            calls.append(1)
            if len(calls) < 3:
                raise pins.urllib.error.URLError("down")
            return FakeResponse(PAYLOAD)

        with (
            mock.patch.object(pins.urllib.request, "urlopen", side_effect=flaky),
            mock.patch.object(pins.time, "sleep"),
        ):
            self.assertEqual(self.run_tool("fetch"), 0)
        self.assertEqual(len(calls), 3)


if __name__ == "__main__":
    unittest.main()
