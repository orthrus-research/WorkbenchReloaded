"""An instance made from a template names its own folder in its manifests, whichever spelling of the prepared folder
the launcher wrote: the folder itself, or the way it reaches it through a link (a snapshot's lab folders)."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path[:0] = [str(REPO / "core"), str(REPO / "modules" / "lab")]

from wb_lab.profile import Layout  # noqa: E402
from wb_lab.store import Store  # noqa: E402


class SeenAs(unittest.TestCase):
    def test_the_launchers_spelling_through_a_link_is_rewritten_too(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            prepared = base / "instances" / "wb-template-1"
            seen = base / "snapshot" / ".local" / "instances" / "wb-template-1"  # the launcher's way in
            prepared.mkdir(parents=True)
            (prepared / "standalone.json").write_text(json.dumps({"name": "wb-template-1",
                                                                   "server": str(seen / "server")}), encoding="utf-8")
            store = Store(base / "store", Layout(rewrite=["standalone.json"]))
            store.adopt_template(prepared, "r1", {}, [seen])
            instance = base / "instances" / "tour"
            store.materialize("r1", instance)
            manifest = json.loads((instance / "standalone.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["server"], str(instance / "server"))
            self.assertEqual(manifest["name"], "tour")


if __name__ == "__main__":
    unittest.main()
