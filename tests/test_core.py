"""Core tests: config merge, types, paths, and the whole job lifecycle against a live in-process daemon."""

import json
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import Daemon  # noqa: E402

from wb import ApprovalRequired, WbError  # noqa: E402
from wb import config as wbconfig  # noqa: E402
from wb_core import types  # noqa: E402
from wb_core.paths import PathError, PathMap  # noqa: E402
from wb_core.settings import Root, SettingsError, resolve  # noqa: E402


class ConfigMerge(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())

    def write(self, name, text):
        path = self.folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def test_deeper_file_wins_and_path_is_namespace(self):
        self.write("workbench.toml", "[modules.vast]\ncap = 90\nmin_cores = 8\n[modules.vast.rent]\ndisk = 32\n")
        self.write("modules/vast.toml", "cap = 60\n")
        self.write("modules/vast/rent.toml", "disk = 48\n")
        config = wbconfig.load(self.folder)
        self.assertEqual(config.get("modules.vast.cap"), 60)
        self.assertEqual(config.get("modules.vast.min_cores"), 8)
        self.assertEqual(config.get("modules.vast.rent.disk"), 48)
        self.assertEqual(config.origin("modules.vast.cap"), "modules/vast.toml:1")
        self.assertEqual(config.origin("modules.vast.min_cores"), "workbench.toml:3")
        self.assertEqual(config.origin("modules.vast.rent.disk"), "modules/vast/rent.toml:1")

    def test_unknown_key_names_file_and_line(self):
        self.write("workbench.toml", "[core.api]\nport = 7377\nprot = 1\n")
        with self.assertRaises(SettingsError) as raised:
            resolve(wbconfig.load(self.folder))
        self.assertIn("workbench.toml:3", str(raised.exception))
        self.assertIn("prot", str(raised.exception))

    def test_root_mount_matches_compose_in_the_container(self):
        import os
        from wb import compose, platform
        self.write("paths.toml", "[roots.out]\nhost = '%s'\nmode = 'rw'\n" % self.folder.as_posix())
        config = wbconfig.load(self.folder)
        os.environ["WB_IN_CONTAINER"] = "1"
        try:
            root = resolve(config).roots[0]
        finally:
            os.environ.pop("WB_IN_CONTAINER")
        run = self.folder / "run"
        run.mkdir()
        (run / "token").write_text("t", encoding="ascii")
        mounts = compose.generate(config, run, platform.Profile("windows", "desktop"))["services"]["wb"]["volumes"]
        self.assertEqual(root.container, "/mnt/out")
        self.assertIn(f"{self.folder.as_posix()}:/mnt/out:rw", mounts)
        self.assertEqual(resolve(config).roots[0].container, self.folder.as_posix())  # natively: the host path

    def test_a_folder_may_keep_other_tools_files_beside_its_config(self):
        self.write("workbench.toml", "[core.config]\nignore = [\"scrub-rules.toml\", \"tools/*.toml\"]\n")
        self.write("scrub-rules.toml", "attribution-names = ['someone']\n")  # another tool's machine file
        self.write("tools/other.toml", "x = 1\n")
        self.write("modules/selftest.toml", "greeting = 'kept'\n")
        config = wbconfig.load(self.folder)
        self.assertNotIn("scrub-rules", config.values)  # never read as a config table
        self.assertNotIn("tools", config.values)
        self.assertEqual(config.values["modules"]["selftest"]["greeting"], "kept")
        resolve(config)  # and the settings accept the key
        self.write("workbench.toml", "")
        with self.assertRaises(SettingsError):  # without the declaration, an unknown table is refused as before
            resolve(wbconfig.load(self.folder))

    def test_layers_merge_under_the_machine_folder(self):
        import os
        shared = Path(tempfile.mkdtemp(prefix="wb-layer-"))
        (shared / "modules").mkdir()
        (shared / "modules" / "vast.toml").write_text("cap = 30\nmin_cores = 4\n", encoding="utf-8")
        (shared / "workbench.toml").write_text("[core.retention]\ndays = 9\n", encoding="utf-8")
        self.write("workbench.toml", f"[core.config]\nlayers = ['{shared.as_posix()}']\n[modules.vast]\ncap = 60\n")
        config = wbconfig.load(self.folder)
        self.assertEqual(config.layers, [shared.resolve()])
        self.assertEqual(config.get("modules.vast.cap"), 60)        # the machine folder wins
        self.assertEqual(config.get("modules.vast.min_cores"), 4)   # the layer fills what the machine leaves
        self.assertEqual(config.get("core.retention.days"), 9)
        self.assertEqual(config.origin("modules.vast.min_cores"), f"{shared.name}/modules/vast.toml:2")
        resolve(config)                                              # core.config.layers is a known key
        os.environ["WB_CONFIG_LAYERS"] = ""                         # the container names its mounts instead
        try:
            self.assertEqual(wbconfig.load(self.folder).layers, [])
        finally:
            os.environ.pop("WB_CONFIG_LAYERS")
        from wb import compose, platform
        run = self.folder / "run"
        run.mkdir(exist_ok=True)
        (run / "token").write_text("t", encoding="ascii")
        service = compose.generate(config, run, platform.Profile("windows", "desktop"))["services"]["wb"]
        self.assertIn(f"{shared.resolve()}:/etc/wb-layers/0:ro", service["volumes"])
        self.assertEqual(service["environment"]["WB_CONFIG_LAYERS"], "/etc/wb-layers/0")
        (shared / "workbench.toml").write_text("[core.config]\nlayers = ['elsewhere']\n", encoding="utf-8")
        with self.assertRaises(wbconfig.ConfigError):
            wbconfig.load(self.folder)

    def test_lab_table_names_its_keys_and_env_values(self):
        self.write("workbench.toml", "[lab]\nhome = 'L'\ntrusted = ['riven']\neula = true\n"
                                     "[lab.env]\nCUBIC_LAB_TURN_LEASE = 'token'\n")
        lab = resolve(wbconfig.load(self.folder)).lab
        self.assertEqual((lab["home"], lab["trusted"], lab["eula"]), ("L", ["riven"], True))
        self.assertEqual(lab["env"], {"CUBIC_LAB_TURN_LEASE": "token"})
        self.write("workbench.toml", "[lab.env]\nX = 'password'\n")
        with self.assertRaises(SettingsError) as raised:
            resolve(wbconfig.load(self.folder))
        self.assertIn("lab.env.X", str(raised.exception))
        self.write("workbench.toml", "")
        self.assertEqual(resolve(wbconfig.load(self.folder)).lab["trusted"], [])

    # [paths]: each folder named once, in host form; the container's spelling derived.
    FOLDER_SCHEMAS = {"ci": {key: {"type": "string", "default": ""} for key in ("store", "records", "pin_cache")},
                      "toolchain": {"pin_cache": {"type": "string", "default": ""}}}

    def test_paths_out_names_the_out_root_and_the_folders_in_it_once(self):
        import os
        from unittest import mock
        from wb import compose, platform
        from wb.workspace import Workspace
        out, projects = Path(tempfile.mkdtemp(prefix="wb-out-")), Path(tempfile.mkdtemp(prefix="wb-projects-"))
        self.write("paths.toml", f"out = '{out.as_posix()}'\n[roots.projects]\nhost = '{projects.as_posix()}'\n")
        config = wbconfig.load(self.folder)
        settings = resolve(config, self.FOLDER_SCHEMAS)
        self.assertEqual(settings.warnings, [])
        roots = {root.name: root for root in settings.roots}
        self.assertEqual((roots["out"].host, roots["out"].mode), (out.as_posix(), "rw"))
        ci = settings.modules["ci"]
        self.assertEqual(Path(ci["store"]), out / "store")  # natively: the host's own path
        self.assertEqual(Path(ci["records"]), out / "ci" / "records")
        self.assertEqual(Path(ci["pin_cache"]), out / "cache" / "pins")
        self.assertEqual(settings.modules["toolchain"]["pin_cache"], ci["pin_cache"])  # one pin cache
        with mock.patch.dict(os.environ, {"WB_IN_CONTAINER": "1"}):
            inside = resolve(config, self.FOLDER_SCHEMAS)
        self.assertEqual(inside.modules["ci"]["store"], "/mnt/out/store")
        self.assertEqual(inside.modules["ci"]["pin_cache"], "/mnt/out/cache/pins")
        self.assertEqual({r.name: r.container for r in inside.roots}, {"out": "/mnt/out", "projects": "/mnt/projects"})
        workspace = Workspace(config)  # the host reads the same folders, in its own spelling
        self.assertEqual(workspace.store_root(), Path(out.as_posix()) / "store")
        self.assertEqual(workspace.folder("pin_cache"), Path(out.as_posix()) / "cache" / "pins")
        self.assertEqual(workspace.records("demo"), Path(out.as_posix()) / "ci" / "records" / "demo")
        self.assertEqual(workspace.host_path("/mnt/out/store/x"), Path(out.as_posix()) / "store" / "x")
        run = Path(tempfile.mkdtemp(prefix="wb-run-"))
        (run / "token").write_text("t", encoding="ascii")
        service = compose.generate(config, run, platform.Profile("windows", "desktop"))["services"]["wb"]
        self.assertIn(f"{out.as_posix()}:/mnt/out:rw", service["volumes"])
        self.assertIn(f"{projects.as_posix()}:/mnt/projects:ro", service["volumes"])
        self.assertEqual(service["environment"]["WB_OWNED_DIRS"],
                         "/mnt/out/cache/pins:/mnt/out/ci/records:/mnt/out/store")

    def test_the_old_path_keys_are_read_for_one_release_with_a_warning(self):
        from wb.workspace import Workspace
        out = Path(tempfile.mkdtemp(prefix="wb-out-"))
        self.write("paths.toml", f"[roots.out]\nhost = '{out.as_posix()}'\ncontainer = '/mnt/out'\nmode = 'rw'\n")
        self.write("modules/ci.toml", "store = '/mnt/out/store'\n")
        self.write("modules/toolchain.toml", "pin_cache = '/mnt/out/cache/pins'\n")
        config = wbconfig.load(self.folder)
        settings = resolve(config, self.FOLDER_SCHEMAS)
        warnings = "\n".join(settings.warnings)
        self.assertEqual(len(settings.warnings), 4, warnings)
        self.assertIn(f"paths.toml:2: [paths.roots.out] is the old spelling of the out root: write [paths] out = "
                      f"'{out.as_posix()}'", warnings)
        self.assertIn("paths.toml:3: paths.roots.out.container: container paths are derived", warnings)
        self.assertIn("modules/ci.toml:1: modules.ci.store: this folder is <out>/store", warnings)
        self.assertIn("modules/toolchain.toml:1: modules.toolchain.pin_cache", warnings)
        self.assertEqual(settings.modules["ci"]["store"], "/mnt/out/store")  # still read, as given
        self.assertEqual(settings.modules["ci"]["records"], "")  # the old spelling derives nothing new
        workspace = Workspace(config)
        self.assertEqual(workspace.store_root(), Path(out.as_posix()) / "store")
        self.assertIsNone(workspace.folder("records"))
        self.assertEqual(wbconfig.deprecations(config), settings.warnings)

    def test_the_out_root_named_twice_and_unknown_paths_keys_are_refused(self):
        out = Path(tempfile.mkdtemp(prefix="wb-out-"))
        self.write("paths.toml", f"out = '{out.as_posix()}'\n[roots.out]\nhost = '{out.as_posix()}'\nmode = 'rw'\n")
        with self.assertRaises(SettingsError) as raised:
            resolve(wbconfig.load(self.folder))
        self.assertIn("both name the out root", str(raised.exception))
        self.write("paths.toml", "out = 3\n")
        with self.assertRaises(SettingsError) as raised:
            resolve(wbconfig.load(self.folder))
        self.assertIn("paths.toml:1", str(raised.exception))
        self.write("paths.toml", "store = 'x'\n")
        with self.assertRaises(SettingsError) as raised:
            resolve(wbconfig.load(self.folder))
        self.assertIn("unknown key paths.store (known: backup, out, releases, roots)", str(raised.exception))

    def test_releases_and_backup_are_read_from_paths(self):
        import os
        from unittest import mock
        from wb import backup, releases
        target, kept = Path(tempfile.mkdtemp(prefix="wb-releases-")), Path(tempfile.mkdtemp(prefix="wb-backup-"))
        self.write("paths.toml", f"releases = '{target.as_posix()}'\nbackup = '{kept.as_posix()}'\n")
        config = wbconfig.load(self.folder)
        resolve(config)
        environ = {k: v for k, v in os.environ.items() if k != "WB_RELEASES"}
        with mock.patch.dict(os.environ, environ, clear=True):
            self.assertEqual(releases.root(config), Path(target.as_posix()))
        other = Path(tempfile.mkdtemp(prefix="wb-releases-env-"))
        with mock.patch.dict(os.environ, {"WB_RELEASES": str(other)}):
            self.assertEqual(releases.root(config), other)  # the variable still wins
        self.assertEqual(backup._target(config, None), Path(kept.as_posix()))

    def test_wrong_type(self):
        self.write("core/api.toml", "port = 'seven'\n")
        with self.assertRaises(SettingsError) as raised:
            resolve(wbconfig.load(self.folder))
        self.assertIn("core/api.toml:1", str(raised.exception))


class Types(unittest.TestCase):
    def test_coercion(self):
        spec = {"type": "duration"}
        self.assertEqual(types.coerce("d", spec, "90m"), 5400)
        self.assertEqual(types.coerce("b", {"type": "bool"}, "yes"), True)
        self.assertEqual(types.coerce("l", {"type": "list", "items": "int"}, "1,2,3"), [1, 2, 3])
        with self.assertRaises(types.ValueError_):
            types.coerce("m", {"type": "money", "max": 1}, 2)
        with self.assertRaises(types.ValueError_):
            types.validate({"a": {"type": "int"}}, {"b": 1})

    def test_a_digest_is_how_a_path_input_is_recorded(self):
        self.assertEqual(types.normalise_spec("p", {"type": "path.in", "digest": "git"})["digest"], "git")
        for bad in ({"type": "string", "digest": "git"}, {"type": "path.in", "digest": "mtime"}):
            with self.assertRaises(types.ValueError_):
                types.normalise_spec("p", bad)
        from wb_core.manifest import ManifestError, parse
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "module.toml"
            path.write_text('[module]\nname = "m"\nversion = "1"\n\n[[command]]\nname = "c"\nhandler = "m:c"\n'
                            'deterministic = true\nparams.repo = { type = "path.in", digest = "git" }\n',
                            encoding="utf-8")
            with self.assertRaises(ManifestError):  # a cached command is cached by its inputs' content
                parse(path, "m")

class TreeDigests(unittest.TestCase):
    def test_unchanged_files_are_not_read_again(self):
        import os
        from unittest import mock
        from wb_core import module
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ("a.bin", "b.bin"):
                (root / name).write_bytes(name.encode() * 1000)
                os.utime(root / name, (time.time() - 60, time.time() - 60))  # older than the racy window
            known: dict = {}
            first = module.sha256_tree(root, known)
            with mock.patch.object(module, "sha256_file", side_effect=module.sha256_file) as read:
                self.assertEqual(module.sha256_tree(root, known), first)
                self.assertEqual(read.call_count, 0)  # both from what is known
                (root / "a.bin").write_bytes(b"changed")
                changed = module.sha256_tree(root, known)
                self.assertEqual(read.call_count, 1)  # only the file that changed
            self.assertNotEqual(changed, first)
            self.assertEqual(changed, module.sha256_tree(root))  # the same digest as without the record


class CacheKey(unittest.TestCase):
    def test_a_modules_config_is_part_of_a_cached_results_key(self):
        from types import SimpleNamespace
        from wb_core.manifest import Command
        from wb_core.service import Workbench
        service = Workbench.__new__(Workbench)  # only the key arithmetic
        service.image_digest = "sha256:" + "0" * 64
        command = Command(module="m", name="c", summary="", handler="m:c", version="1", deterministic=True)
        keys = []
        for greeting in ("hello", "hi", "hi"):
            service.settings = SimpleNamespace(modules={"m": {"greeting": greeting}, "other": {"x": greeting}})
            keys.append(service._cache_key(command, {"text": "a"}, []))
        self.assertNotEqual(keys[0], keys[1])  # a config change never serves the old result
        self.assertEqual(keys[1], keys[2])


class StateFolder(unittest.TestCase):
    def test_a_native_daemon_keeps_its_state_in_the_users_folder(self):
        import os
        from unittest import mock
        from wb_core import settings as wbsettings
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "workbench.toml").write_text("", encoding="utf-8")
            resolved = resolve(wbconfig.load(folder))
            with mock.patch.dict(os.environ, {"WB_IN_CONTAINER": ""}), \
                    mock.patch.object(wbsettings, "in_container", return_value=False):
                os.environ.pop("WB_STATE_DIR", None)
                self.assertEqual(resolved.state_dir, wbsettings.native_state_dir())  # never a drive's root
            with mock.patch.object(wbsettings, "in_container", return_value=True):
                self.assertEqual(resolved.state_dir, Path("/var/lib/wb"))       # the container's volume
            with mock.patch.dict(os.environ, {"WB_STATE_DIR": folder}):
                self.assertEqual(resolved.state_dir, Path(folder))             # the environment wins


class Quiet(unittest.TestCase):
    def test_a_measurement_on_the_host_keeps_new_container_work_waiting(self):
        from wb_core.service import Running, Workbench
        service = Workbench.__new__(Workbench)  # only the scheduling arithmetic
        service.cpu_budget, service.mem_budget = 8.0, 8.0
        service.running = {"j1": Running("j1", "host", 0.0, 0.0, set(), True)}
        self.assertFalse(service._fits("container", 1.0, 1.0, set(), False))  # the same hardware
        self.assertTrue(service._fits("rental", 0.0, 0.0, set(), False))      # another machine
        service.running = {"j2": Running("j2", "container", 1.0, 1.0, set(), False)}
        self.assertFalse(service._fits("host", 0.0, 0.0, set(), True))         # a measurement waits for quiet


class Paths(unittest.TestCase):
    def setUp(self):
        self.map = PathMap([Root("projects", "D:\\", "/mnt/projects", "ro"),
                            Root("out", "D:\\work\\out", "/mnt/out", "rw")])  # scrub: allow

    def test_windows_wsl_and_container_forms(self):
        self.assertEqual(self.map.resolve("D:\\mymod\\tools").container, "/mnt/projects/mymod/tools")  # scrub: allow
        self.assertEqual(self.map.resolve("/mnt/d/mymod/tools").container, "/mnt/projects/mymod/tools")
        self.assertEqual(self.map.resolve("d:/work/out/x", need_write=True).container, "/mnt/out/x")  # scrub: allow
        self.assertEqual(self.map.resolve("/mnt/out/x").container, "/mnt/out/x")

    def test_refusals(self):
        with self.assertRaises(PathError):
            self.map.resolve("F:\\elsewhere")  # scrub: allow
        with self.assertRaises(PathError):
            self.map.resolve("D:\\mymod", need_write=True)  # scrub: allow
        with self.assertRaises(PathError):
            self.map.resolve("relative\\path")

    def test_host_path_in_caller_style(self):
        self.assertEqual(self.map.to_host("/mnt/out/cmp-01/report.json"), "D:\\work\\out\\cmp-01\\report.json")  # scrub: allow
        self.assertEqual(self.map.to_host("/mnt/out/cmp-01", "posix"), "/mnt/d/work/out/cmp-01")
        self.assertEqual(self.map.to_host("/mnt/projects/mymod"), "D:\\mymod")  # scrub: allow


class Lifecycle(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = Daemon(files={"modules/selftest.toml": "greeting = 'hi'\n"}).start()
        cls.c = cls.d.client

    @classmethod
    def tearDownClass(cls):
        cls.d.stop()

    def test_health_and_registry(self):
        health = self.c.health()
        self.assertTrue(health["ok"])
        names = {c["name"]: c for c in self.c.commands()}
        self.assertIn("selftest.echo", names)
        self.assertTrue(names["selftest.echo"]["active"])
        self.assertTrue(names["selftest.spend"]["spends"])

    def test_echo_ok_with_output_measurement_and_module_config(self):
        result = self.c.submit("selftest.echo", {"text": "ab", "times": "3"}).result(timeout=60)
        self.assertEqual(result["schema"], "wb/result/v1")
        self.assertEqual((result.outcome, result.code), ("ok", 0))
        self.assertEqual(result.data.text, "ababab")
        self.assertEqual(result.data.greeting, "hi")
        self.assertEqual(result["measurements"][0]["value"], 6)
        self.assertEqual(result["outputs"][0]["name"], "echo")
        self.assertEqual(len(result["outputs"][0]["sha256"]), 64)
        self.assertIn("echo", result["timings"]["phases"])

    def test_cache_for_deterministic_command(self):
        first = self.c.submit("selftest.echo", {"text": "cache-me"}).result(timeout=60)
        second = self.c.submit("selftest.echo", {"text": "cache-me"}).result(timeout=60)
        fresh = self.c.submit("selftest.echo", {"text": "cache-me"}, fresh=True).result(timeout=60)
        self.assertFalse(first["cache"]["hit"])
        self.assertTrue(second["cache"]["hit"])
        self.assertEqual(second["cache"]["from"], first["job"])
        self.assertEqual(second["data"], first["data"])
        self.assertFalse(fresh["cache"]["hit"])

    def test_invalid_params_settle_with_usage(self):
        result = self.c.submit("selftest.echo", {"times": 0}).result(timeout=10)
        self.assertEqual((result.state, result.outcome, result.code), ("settled", "usage", 2))
        self.assertEqual(result["reasons"][0]["name"], "times")
        unknown = self.c.submit("selftest.echo", {"nope": 1}).result(timeout=10)
        self.assertEqual(unknown.outcome, "usage")

    def test_unknown_command_is_404(self):
        with self.assertRaises(WbError) as raised:
            self.c.submit("selftest.nothing", {})
        self.assertEqual(raised.exception.status, 404)

    def test_outcome_codes(self):
        for outcome, code in (("finding", 1), ("usage", 2), ("environment", 3), ("crash", 3)):
            result = self.c.submit("selftest.fail", {"outcome": outcome}).result(timeout=60)
            self.assertEqual(result.code, code, outcome)
        crash = self.c.submit("selftest.fail", {"outcome": "crash"}).result(timeout=60)
        self.assertIn("RuntimeError", crash.summary)
        self.assertNotIn("Traceback", json.dumps(crash))

    def test_cancel_running_job(self):
        job = self.c.submit("selftest.sleep", {"seconds": "30s"})
        for _ in range(100):
            if job.refresh().state == "running":
                break
            time.sleep(0.1)
        job.cancel()
        result = job.wait(timeout=30)
        self.assertEqual(result.state, "cancelled")
        self.assertEqual(result.exit_code, 4)

    def test_approval_flow_with_requote(self):
        job = self.c.submit("selftest.spend", {"price": 0.0, "raise_to": 0.5})
        with self.assertRaises(ApprovalRequired) as raised:
            job.result(timeout=60)
        first = raised.exception.quote
        self.assertEqual(first["dph"], 0.0)
        self.assertIn("question", raised.exception.prompt)
        with self.assertRaises(WbError) as wrong:
            job.approve("q_NOTTHEQUOTE")
        self.assertEqual(wrong.exception.status, 409)
        # A job's own token reads what the job needs but never approves: the person's token does.
        from wb.client import Client
        token = self.d.workbench.job_token(job.id)
        as_job = Client(self.c.url, token).job(job.id)
        self.assertEqual(as_job.refresh().state, "awaiting_approval")
        with self.assertRaises(WbError) as refused:
            as_job.approve(first["id"])
        self.assertEqual(refused.exception.status, 403)
        job.approve(first["id"], via="relayed")
        with self.assertRaises(ApprovalRequired) as again:
            job.result(timeout=60)  # the run found a higher price: a new quote, the old yes void
        second = again.exception.quote
        self.assertNotEqual(second["id"], first["id"])
        self.assertEqual(second["dph"], 0.5)
        job.approve(second["id"])
        result = job.result(timeout=60)
        self.assertEqual(result.outcome, "ok")
        self.assertEqual(result.data.quote, second["id"])
        self.assertEqual(result["approval"]["approved"]["via"], "direct")
        self.assertEqual(result["cost"]["amount"], 0.0)
        with self.assertRaises(WbError) as ended:
            as_job.refresh()  # the job ended, and its token with it
        self.assertEqual(ended.exception.status, 401)

    def test_idempotency_key(self):
        a = self.c.submit("selftest.echo", {"text": "idem"}, idempotency_key="same-key-1")
        b = self.c.submit("selftest.echo", {"text": "idem"}, idempotency_key="same-key-1")
        self.assertEqual(a.id, b.id)

    def test_paths_inputs_and_outputs(self):
        source = self.d.data / "input.txt"
        source.write_text("payload", encoding="utf-8")
        out = self.d.out / "copy-01"
        result = self.c.submit("selftest.copy", {"source": str(source)}, out=str(out)).result(timeout=60)
        self.assertEqual(result.outcome, "ok", result.summary)
        self.assertEqual(result["inputs"][0]["host_path"], str(source))
        self.assertTrue((out / "input.txt").exists())
        self.assertEqual(Path(result["outputs"][0]["host_path"]), out / "input.txt")
        refused = self.c.submit("selftest.copy", {"source": str(source)}, out=str(self.d.data / "x")).result(timeout=10)
        self.assertEqual(refused.outcome, "usage")
        outside = self.c.submit("selftest.copy", {"source": str(self.d.root / "token")}).result(timeout=10)
        self.assertEqual(outside.outcome, "usage")

    def test_config_reload_applies_module_config_and_refuses_fixed_changes(self):
        d = self.d

        def reloader():  # as the daemon's: the same folders, with the port the service is serving on
            fresh = resolve(wbconfig.load(d.config_dir, required=True), {m.name: m.config for m in d.modules})
            fresh.core["api"]["port"] = d.port
            return fresh
        d.workbench.reloader = reloader
        module_file = d.config_dir / "modules" / "selftest.toml"
        main_file = d.config_dir / "workbench.toml"
        original = main_file.read_text(encoding="utf-8")
        try:
            module_file.write_text("greeting = 'reloaded'\n", encoding="utf-8")
            self.assertTrue(self.c.reload_config()["reloaded"])
            result = self.c.submit("selftest.echo", {"text": "after a reload"}).result(timeout=60)
            self.assertEqual(result.data.greeting, "reloaded")  # a new job sees the new module config
            main_file.write_text(original.replace('max_wait = "10s"', 'max_wait = "11s"'), encoding="utf-8")
            with self.assertRaises(WbError) as raised:
                self.c.reload_config()
            self.assertIn("core.api", str(raised.exception))  # the API needs a restart; nothing changed
            self.assertEqual(d.workbench.settings.core["api"]["max_wait"], 10)
        finally:
            main_file.write_text(original, encoding="utf-8")
            module_file.write_text("greeting = 'hi'\n", encoding="utf-8")
            self.c.reload_config()
            d.workbench.reloader = None

    def test_a_list_of_input_paths_keys_the_cache_by_content(self):
        one, two = self.d.data / "list-one.txt", self.d.data / "list-two.txt"
        one.write_text("a", encoding="utf-8")
        two.write_text("bb", encoding="utf-8")
        params = {"sources": [str(one), str(two)]}
        first = self.c.submit("selftest.sizes", params).result(timeout=60)
        self.assertEqual((first.outcome, first.data.sizes), ("ok", [1, 2]), first.summary)
        self.assertEqual([i.get("index") for i in first["inputs"]], [0, 1])
        again = self.c.submit("selftest.sizes", params).result(timeout=60)
        self.assertTrue(again["cache"]["hit"])
        two.write_text("ccc", encoding="utf-8")  # one file of the list changed: the old result must not be served
        changed = self.c.submit("selftest.sizes", params).result(timeout=60)
        self.assertFalse(changed["cache"]["hit"])
        self.assertEqual(changed.data.sizes, [1, 3])

    def test_an_input_path_that_is_not_there_is_a_usage_refusal(self):
        there = self.d.data / "list-there.txt"
        there.write_text("a", encoding="utf-8")
        gone = self.d.data / "not-there.txt"
        for command, params in (("selftest.sizes", {"sources": [str(there), str(gone)]}),
                                ("selftest.checkout", {"repo": str(gone)})):
            with self.subTest(command=command):
                result = self.c.submit(command, params).result(timeout=60)
                self.assertEqual(result.outcome, "usage", result.summary)  # the request's mistake, not the machine's
                self.assertIn("does not exist", result.summary)
                self.assertIn("not-there.txt", result.summary)
                self.assertNotIn("Error", result.summary)

    def test_a_checkout_input_is_recorded_by_its_head_not_hashed(self):
        import subprocess
        repo = self.d.data / "checkout"
        repo.mkdir()
        for args in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "one"]):
            subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args], cwd=repo,
                           check=True, capture_output=True)
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
        result = self.c.submit("selftest.checkout", {"repo": str(repo)}).result(timeout=60)
        self.assertEqual(result.outcome, "ok", result.summary)
        self.assertEqual(result.data.head, head)
        self.assertEqual(result["inputs"][0]["head"], head)
        self.assertNotIn("sha256", result["inputs"][0])

    def test_chain_calls_through_the_api(self):
        result = self.c.submit("selftest.chain", {"text": "x"}).result(timeout=90)
        self.assertEqual(result.outcome, "ok", result.summary)
        self.assertEqual(result.data.inner_outcome, "ok")
        self.assertEqual(result["children"], [result.data.inner])

    def test_events_stream_and_json(self):
        job = self.c.submit("selftest.echo", {"text": "events", "times": 2})
        kinds = [e["kind"] for e in job.events()]
        self.assertEqual(kinds[0], "state")
        self.assertIn("progress", kinds)
        self.assertIn("measurement", kinds)
        batch = self.c.request("GET", f"/v1/jobs/{job.id}/events?format=json&after=0")
        self.assertTrue(batch["finished"])

    def test_auth_host_check_and_origin(self):
        url = f"http://127.0.0.1:{self.d.port}/v1/health"
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(url, timeout=5)
        self.assertEqual(raised.exception.code, 401)
        request = urllib.request.Request(url, headers={"Authorization": "Bearer " + self.d.token,
                                                       "Host": "evil.example:80"})
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(raised.exception.code, 421)
        request = urllib.request.Request(url, headers={"Authorization": "Bearer " + self.d.token,
                                                       "Origin": "https://evil.example"})
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(raised.exception.code, 403)

    def test_logs_minecraft_style(self):
        self.c.submit("selftest.echo", {"text": "log-me"}, fresh=True).result(timeout=60)
        logs = self.d.state / "logs"
        self.assertTrue((logs / "latest.log").exists())
        self.assertTrue((logs / "latest.jsonl").exists())
        self.assertTrue((logs / "modules" / "selftest" / "latest.log").exists())
        lines = self.c.logs(module="selftest", limit=50)
        self.assertTrue(any("echo" in line["msg"] for line in lines))


class Restart(unittest.TestCase):
    def test_running_job_is_interrupted_by_restart(self):
        first = Daemon().start()
        job = first.client.submit("selftest.sleep", {"seconds": "60s"})
        for _ in range(100):
            if job.refresh().state == "running":
                break
            time.sleep(0.1)
        pid = first.workbench.db.one("SELECT worker_pid FROM job WHERE id = ?", (job.id,))["worker_pid"]
        # Simulate a crash: the dead daemon's threads may no longer write anything.
        first.workbench._finish = lambda *a, **k: None
        first.workbench.add_event = lambda *a, **k: 0
        first.workbench.stopping.set()
        first.server.shutdown()
        first.server.server_close()
        first.hub.close()
        from wb_core.service import _kill_pid
        _kill_pid(pid)
        second = Daemon(root=first.root).start()
        try:
            env = second.client.job(job.id).refresh()
            self.assertEqual(env.state, "interrupted")
            self.assertEqual(env.exit_code, 4)
            done = second.client.submit("selftest.echo", {"text": "after"}).result(timeout=60)
            self.assertEqual(done.outcome, "ok")
        finally:
            second.stop()


if __name__ == "__main__":
    unittest.main()
