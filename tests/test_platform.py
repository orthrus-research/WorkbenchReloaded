"""Platform neutrality: the compose profile per Docker flavour, login start-up on each OS, and directory links."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import REPO  # noqa: E402

from wb import compose, platform  # noqa: E402
from wb import config as wbconfig  # noqa: E402
from wb_host import startup  # noqa: E402


class Compose(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        (self.folder / "workbench.toml").write_text("[core.api]\nport = 7377\n", encoding="utf-8")
        self.run = self.folder / "run"
        self.run.mkdir()
        (self.run / "token").write_text("t", encoding="ascii")

    def test_desktop_publishes_on_loopback(self):
        document = compose.generate(wbconfig.load(self.folder), self.run, platform.Profile("windows", "desktop"))
        service = document["services"]["wb"]
        self.assertEqual(service["ports"], ["127.0.0.1:7377:7377"])
        self.assertEqual(service["environment"]["WB_HOST_URL"], "http://host.docker.internal:7378")
        self.assertNotIn("network_mode", service)

    def test_missing_secret_is_left_out_with_a_warning(self):
        (self.folder / "secrets.toml").write_text("[vast_api_key]\nhost = 'no/such/file'\n", encoding="utf-8")
        warnings = []
        document = compose.generate(wbconfig.load(self.folder), self.run, platform.Profile("windows", "desktop"),
                                    warnings)
        self.assertNotIn("vast_api_key", document["secrets"])
        self.assertNotIn("vast_api_key", document["services"]["wb"]["secrets"])
        self.assertIn("vast_api_key", warnings[0])

    def test_native_linux_shares_the_host_loopback(self):
        document = compose.generate(wbconfig.load(self.folder), self.run, platform.Profile("linux", "native"))
        service = document["services"]["wb"]
        self.assertEqual(service["network_mode"], "host")
        self.assertNotIn("ports", service)
        self.assertEqual(service["environment"]["WB_API_LISTEN"], "127.0.0.1")
        self.assertEqual(service["environment"]["WB_HOST_URL"], "http://127.0.0.1:7378")

    def test_folders_workbench_writes_in_an_rw_root_are_given_to_its_user(self):
        out, src = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
        (self.folder / "paths.toml").write_text(
            f"[roots.out]\nhost = '{out}'\ncontainer = '/mnt/out'\nmode = 'rw'\n"
            f"[roots.src]\nhost = '{src}'\ncontainer = '/mnt/src'\nmode = 'ro'\n", encoding="utf-8")
        modules = self.folder / "modules"
        modules.mkdir()
        (modules / "ci.toml").write_text("pin_cache = '/mnt/out/cache/pins'\nrecords = '/mnt/out/ci/records'\n",
                                         encoding="utf-8")
        (modules / "toolchain.toml").write_text("pin_cache = '/mnt/out/cache/pins'\n", encoding="utf-8")
        environment = compose.generate(wbconfig.load(self.folder), self.run,
                                       platform.Profile("windows", "desktop"))["services"]["wb"]["environment"]
        self.assertEqual(environment["WB_OWNED_DIRS"], "/mnt/out/cache/pins:/mnt/out/ci/records")
        # A ro root, a root itself, a relative path and an escape are never given away.
        (modules / "toolchain.toml").write_text("", encoding="utf-8")
        for value in ("/mnt/src/cache", "/mnt/out", "cache/pins", "/mnt/out/../src"):
            (modules / "ci.toml").write_text(f"pin_cache = '{value}'\n", encoding="utf-8")
            self.assertEqual(compose.owned_dirs(wbconfig.load(self.folder)), [], value)
        (modules / "ci.toml").write_text("", encoding="utf-8")
        environment = compose.generate(wbconfig.load(self.folder), self.run,
                                       platform.Profile("windows", "desktop"))["services"]["wb"]["environment"]
        self.assertNotIn("WB_OWNED_DIRS", environment)


class ToolPaths(unittest.TestCase):
    def test_posix_and_windows_host_paths_are_translated(self):
        sys.path.insert(0, str(REPO / "modules" / "tools"))
        from wb_tools import adapter

        class Ctx:
            spec = {"roots": [{"name": "home", "host": "/Users/kim", "container": "/mnt/home", "mode": "ro"},  # scrub: allow
                              {"name": "d", "host": r"D:\data", "container": "/mnt/d", "mode": "ro"}]}  # scrub: allow
        argv = ["compare", "/Users/kim/ref", "--profile=/Users/kim/p.json", r"D:\data\cand", "/opt/jdk/26", "x"]  # scrub: allow
        self.assertEqual(adapter._translate(Ctx(), argv),
                         ["compare", "/mnt/home/ref", "--profile=/mnt/home/p.json", "/mnt/d/cand", "/opt/jdk/26", "x"])


class Startup(unittest.TestCase):
    def test_each_platform_starts_the_launcher_in_the_desktop_session(self):
        home = Path(tempfile.mkdtemp())
        windows = startup.install_plan(REPO, "windows", home)
        script = windows.commands[0][-1]
        self.assertIn("-AtLogOn", script)
        self.assertIn(str(REPO / "bin" / "wb-host.pyw"), script)
        self.assertNotIn("PYTHONPATH", script)
        self.assertNotEqual(Path(windows.commands[0][0]).name.lower(), "pwsh")  # Windows PowerShell is always there
        linux = startup.install_plan(REPO, "linux", home)
        unit = next(iter(linux.files.values()))
        self.assertIn("WantedBy=graphical-session.target", unit)
        self.assertIn(f"ExecStart={REPO / 'bin' / 'wb'} host serve", unit)
        self.assertEqual(linux.commands[-1], ["systemctl", "--user", "enable", "--now", "wb-host.service"])
        macos = startup.install_plan(REPO, "macos", home)
        plist = next(iter(macos.files.values()))
        self.assertIn("<string>Aqua</string>", plist)
        self.assertIn("<key>RunAtLoad</key><true/>", plist)
        self.assertEqual(macos.commands[0][:2], ["launchctl", "bootstrap"])
        for name in ("windows", "linux", "macos"):
            self.assertTrue(startup.uninstall_plan(REPO, name, home).commands)

    def test_apply_writes_and_removes_files(self):
        home = Path(tempfile.mkdtemp())
        plan = startup.install_plan(REPO, "linux", home)
        plan.commands = []
        startup.apply(plan)
        unit = next(iter(plan.files))
        self.assertTrue(unit.exists())
        removal = startup.uninstall_plan(REPO, "linux", home)
        removal.commands = []
        startup.apply(removal, removing=True)
        self.assertFalse(unit.exists())


class Links(unittest.TestCase):
    def test_link_round_trip_leaves_the_target_alone(self):
        root = Path(tempfile.mkdtemp())
        target = root / "lab" / "frozen"
        target.mkdir(parents=True)
        (target / "manifest.json").write_text("{}", encoding="utf-8")
        link = root / "riven" / ".local" / "frozen"
        link.parent.mkdir(parents=True)
        kind = platform.make_link(link, target)
        self.assertEqual(kind, "junction" if os.name == "nt" else "symlink")
        self.assertTrue(platform.is_link(link))
        self.assertTrue((link / "manifest.json").exists())
        self.assertTrue(platform.same_volume(target, link.parent))
        platform.remove_link(link)
        self.assertFalse(link.exists())
        self.assertTrue((target / "manifest.json").exists())
        with self.assertRaises(ValueError):
            platform.remove_link(target)


class ImageTools(unittest.TestCase):
    """wb build passes the machine's optional tools to the image, and refuses one the pins do not name."""

    def test_named_tools_become_the_build_argument(self):
        from unittest import mock
        with mock.patch.object(compose.subprocess, "run") as run, mock.patch.object(compose, "repo_root",
                                                                                     return_value=REPO):
            run.return_value.returncode = 0
            self.assertEqual(compose.build("wb-core:test", ["node", "zig"]), 0)
            command = run.call_args[0][0]
            self.assertEqual(command[command.index("--build-arg") + 1], "TOOLS=node,zig")
            self.assertTrue(command[-2].startswith("WB_COMMIT="))  # the commit last: only the last layer changes
            compose.build("wb-core:test")
            arguments = [a for a in run.call_args[0][0] if a.startswith(("TOOLS=", "WB_COMMIT="))]
            self.assertEqual([a.split("=")[0] for a in arguments], ["WB_COMMIT"])  # no tools: no TOOLS argument

    def test_a_tool_the_pins_do_not_name_is_refused(self):
        from unittest import mock
        with mock.patch.object(compose, "repo_root", return_value=REPO):
            with self.assertRaises(compose.ComposeError) as raised:
                compose.build("wb-core:test", ["cobol"])
        self.assertIn("cobol, which image/pins.toml does not pin", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
