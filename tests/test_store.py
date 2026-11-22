"""The instance store through the host runner: one prepare per recipe, instances as hard links into shared
read-only files, rewritten manifests checked by the launcher's own load(), world snapshots, removal that spares
shared files, dedupe of existing full copies, and store GC."""

import json
import os
import shutil
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fake_riven  # noqa: E402
from harness import Daemon  # noqa: E402

from wb_core.hostclient import HostClient  # noqa: E402
from wb_host.runner import HostServer, Runner  # noqa: E402


def writable(path: Path) -> bool:
    return bool(os.stat(path).st_mode & stat.S_IWRITE)


class Store(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        base = Path(tempfile.mkdtemp(prefix="wb-store-"))
        cls.lab, cls.riven = base / "Lab", fake_riven.make(base / "Riven")
        cls.instances = cls.riven / ".local" / "cubic-standalone"
        cls.mod = base / "Riven" / "under-test.jar"
        cls.mod.write_bytes(b"Q" * 512)
        config = f"""
            [runner]
            lab_home = '{cls.lab}'
            lease_file = '{cls.lab / "lease" / "gpu.lock"}'
            coordination_dir = '{cls.lab / "coordination"}'
            [modules.lab]
            launcher = '{cls.riven}'
            """
        files = {"modules/lab/profile.toml": fake_riven.PROFILE_TOML}
        probe = Daemon(config, files=files)
        cls.runner = Runner(probe.settings, probe.modules)
        probe.hub.close()
        cls.server = HostServer(cls.runner, "store-token", 0)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.d = Daemon(config, files={"core/host.toml": "enabled = true\n", **files},
                       host_client=HostClient(url, "store-token")).start()
        cls.c = cls.d.client

    @classmethod
    def tearDownClass(cls):
        cls.d.stop()
        cls.server.shutdown()

    def ensure(self, name, **params):
        return self.c.submit("lab.ensure", dict({"name": name, "seed": 2485331}, **params)).result(timeout=120)

    def test_1_one_prepare_per_recipe_and_linked_instances(self):
        first = self.ensure("alpha")
        self.assertEqual(first.outcome, "ok", first.summary)
        self.assertTrue(first.data.prepared_template)
        second = self.ensure("beta")
        self.assertEqual(second.outcome, "ok", second.summary)
        self.assertFalse(second.data.prepared_template)
        self.assertEqual(second.data.recipe, first.data.recipe)
        self.assertEqual(len(fake_riven.prepare_calls(self.riven)), 1)  # prepared once, built twice
        lib = Path("server/libraries/org/x/lib.jar")
        for name in ("alpha", "beta"):
            self.assertGreaterEqual(os.stat(self.instances / name / lib).st_nlink, 3)  # blob + template + instances
            self.assertFalse(writable(self.instances / name / lib))  # shared: read-only
            self.assertTrue(writable(self.instances / name / "client" / "options.txt"))  # written by the game: own copy
            manifest = json.loads((self.instances / name / "standalone.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["name"], name)
            self.assertEqual(Path(manifest["server"]), self.instances / name / "server")
        self.assertGreater(second.data.linked_bytes, second.data.copied_bytes)

    def test_2_reuse_and_replace(self):
        again = self.ensure("alpha")
        self.assertTrue(again.data.reused)
        other = self.ensure("alpha", args=["--client-mod", str(self.mod)])
        self.assertEqual(other.outcome, "usage")  # another recipe under the same name
        replaced = self.ensure("alpha", args=["--client-mod", str(self.mod)], replace=True)
        self.assertEqual(replaced.outcome, "ok", replaced.summary)
        self.assertNotEqual(replaced.data.recipe, again.data.recipe)
        self.assertTrue((self.instances / "alpha" / "client" / "mods" / "under-test.jar").exists())
        self.assertEqual(len(fake_riven.prepare_calls(self.riven)), 2)

    def test_3_world_snapshot_and_reset(self):
        world = self.instances / "beta" / "server" / "world"
        (world / "region3d").mkdir(parents=True, exist_ok=True)
        (world / "region3d" / "0.0.0.3dr").write_bytes(b"terrain" * 100)
        (world / "level.dat").write_bytes(b"level")
        kept = self.c.submit("lab.snapshot", {"name": "beta", "label": "settled-1"}).result(timeout=120)
        self.assertEqual(kept.outcome, "ok", kept.summary)
        (world / "region3d" / "0.0.0.3dr").write_bytes(b"changed by a run")
        reset = self.c.submit("lab.reset", {"name": "beta", "world": "settled-1"}).result(timeout=120)
        self.assertEqual(reset.outcome, "ok", reset.summary)
        self.assertEqual((world / "region3d" / "0.0.0.3dr").read_bytes(), b"terrain" * 100)
        self.assertTrue(writable(world / "region3d" / "0.0.0.3dr"))  # a private copy the game may write
        gamma = self.ensure("gamma", world="settled-1")
        self.assertEqual(gamma.outcome, "ok", gamma.summary)
        self.assertEqual((self.instances / "gamma/server/world/level.dat").read_bytes(), b"level")
        wrong = self.ensure("delta", seed=1, world="settled-1")
        self.assertEqual(wrong.outcome, "usage")  # the snapshot was generated for another seed
        self.assertFalse((self.instances / "delta").exists())  # no half-made instance is left

    def test_4_remove_spares_shared_files(self):
        lib = self.instances / "gamma" / "server/libraries/org/x/lib.jar"
        before = os.stat(lib).st_nlink
        removed = self.c.submit("lab.remove", {"name": "gamma"}).result(timeout=120)
        self.assertEqual(removed.outcome, "ok", removed.summary)
        self.assertFalse((self.instances / "gamma").exists())
        beta_lib = self.instances / "beta" / "server/libraries/org/x/lib.jar"
        self.assertEqual(os.stat(beta_lib).st_nlink, before - 1)
        self.assertFalse(writable(beta_lib))  # still read-only for everyone else

    def test_5_dedupe_existing_full_copies(self):
        for name in ("old-1", "old-2"):  # full copies, as runtime.py prepare makes them today
            source = self.instances / "beta"
            shutil.copytree(source, self.instances / name, ignore=shutil.ignore_patterns(".wb-instance.json"),
                            copy_function=shutil.copyfile)
        dry = self.c.submit("lab.dedupe", {}).result(timeout=120)
        self.assertEqual(dry.outcome, "ok", dry.summary)
        self.assertGreater(dry.data.estimated_bytes, 0)
        self.assertEqual(os.stat(self.instances / "old-1/server/libraries/org/x/lib.jar").st_nlink, 1)
        done = self.c.submit("lab.dedupe", {"execute": True}).result(timeout=120)
        self.assertEqual(done.outcome, "ok", done.summary)
        self.assertGreater(done.data.freed_bytes, 0)
        self.assertGreater(os.stat(self.instances / "old-1/server/libraries/org/x/lib.jar").st_nlink, 1)
        self.assertTrue((self.instances / "old-1" / ".wb-instance.json").exists())
        listed = self.c.submit("lab.instances", {}).result(timeout=120)
        self.assertEqual(listed.data.legacy, 0)

    def test_6_store_gc(self):
        dry = self.c.submit("lab.store-gc", {"days": 1}).result(timeout=120)
        self.assertEqual(dry.outcome, "ok", dry.summary)
        self.assertEqual(dry.data.templates, [])  # recent and in use
        for name in [p.name for p in self.instances.iterdir()]:
            self.c.submit("lab.remove", {"name": name}).result(timeout=120)
        store = self.lab / "store"
        old = time.time() - 3 * 86400
        for folder in list((store / "templates").iterdir()) + list((store / "worlds").iterdir()):
            (folder / "last-used").write_text(str(old), encoding="ascii")
        done = self.c.submit("lab.store-gc", {"days": 1, "execute": True}).result(timeout=120)
        self.assertEqual(done.outcome, "ok", done.summary)
        self.assertEqual(len(done.data.templates), 3)  # alpha's two recipes and delta's seed
        self.assertEqual(done.data.worlds, ["settled-1"])
        self.assertEqual(list((store / "blobs").rglob("*.*")), [])
        self.assertEqual([p for p in (store / "blobs").rglob("*") if p.is_file()], [])

    def test_7_packs_join_the_recipe(self):
        pack = self.d.data / "light-shaders.zip"  # under the daemon's data root
        pack.write_bytes(b"a shader pack")
        plain = self.ensure("epsilon")
        packed = self.ensure("zeta", packs=[str(pack)])
        self.assertEqual(packed.outcome, "ok", packed.summary)
        self.assertNotEqual(packed.data.recipe, plain.data.recipe)  # another recipe, never the same template
        shaders = Path("client") / "shaderpacks"
        self.assertEqual((self.instances / "zeta" / shaders / "light-shaders.zip").read_bytes(), b"a shader pack")
        self.assertFalse((self.instances / "epsilon" / shaders).exists())
        again = self.ensure("eta", packs=[str(pack)])
        self.assertFalse(again.data.prepared_template)  # the same packs: the same template
        self.assertTrue((self.instances / "eta" / shaders / "light-shaders.zip").is_file())

    def test_8_fractional_and_whole_scales(self):
        half = self.ensure("theta", scale=0.5)
        self.assertEqual(half.outcome, "ok", half.summary)
        manifest = json.loads((self.instances / "theta" / "standalone.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["scaleArgument"], "0.5")
        self.assertEqual(manifest["generator"]["layoutScale"], 0.5)
        whole = self.ensure("iota", scale=4)
        self.assertEqual(whole.outcome, "ok", whole.summary)
        manifest = json.loads((self.instances / "iota" / "standalone.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["scaleArgument"], "4")  # a whole scale reaches the launcher as an integer
        template = json.loads((self.lab / "store" / "templates" / whole.data.recipe / "template.json")
                              .read_text(encoding="utf-8"))
        self.assertIs(type(template["inputs"]["scale"]), int)  # the recipe integer scales always had
        same = self.ensure("kappa", scale=4.0)
        self.assertEqual(same.data.recipe, whole.data.recipe)
        self.assertFalse(same.data.prepared_template)


if __name__ == "__main__":
    unittest.main()
