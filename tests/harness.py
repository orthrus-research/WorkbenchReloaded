"""An in-process Workbench for tests: a temporary config folder and state folder, a free port, a token,
and a ``wb.Client`` pointed at it."""

from __future__ import annotations

import secrets
import shutil
import sys
import tempfile
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
for extra in (REPO / "sdk" / "python", REPO / "core", REPO / "host"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from wb import config as wbconfig  # noqa: E402
from wb.client import Client  # noqa: E402
from wb_core.api import serve  # noqa: E402
from wb_core.logs import LogHub  # noqa: E402
from wb_core.manifest import discover  # noqa: E402
from wb_core.service import Workbench  # noqa: E402
from wb_core.settings import resolve  # noqa: E402


class Daemon:
    def __init__(self, config_text: str = "", *, files: dict[str, str] | None = None, modules=None,
                 host_client=None, state: Path | None = None, root: Path | None = None):
        self.root = root or Path(tempfile.mkdtemp(prefix="wb-test-"))
        self.config_dir = self.root / "config"
        self.state = state or self.root / "state"
        self.data = self.root / "data"
        self.out = self.root / "out"
        for folder in (self.config_dir, self.data, self.out):
            folder.mkdir(parents=True, exist_ok=True)
        self.token = secrets.token_urlsafe(16)
        token_file = self.root / "token"
        token_file.write_text(self.token, encoding="ascii")
        base = textwrap.dedent(f"""
            [core.state]
            dir = '{self.state.as_posix()}'
            [core.api]
            port = 0
            listen = "127.0.0.1"
            token_file = '{token_file.as_posix()}'
            max_wait = "10s"
            [core.logging]
            stdout_level = "ERROR"
            color = false
            [core.host]
            enabled = false
            [core.quotes]
            ttl = "30s"
            [paths.roots.data]
            host = '{self.data}'
            mode = "ro"
            [paths.roots.out]
            host = '{self.out}'
            mode = "rw"
        """)
        (self.config_dir / "workbench.toml").write_text(base + textwrap.dedent(config_text), encoding="utf-8")
        for name, text in (files or {}).items():
            path = self.config_dir / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(textwrap.dedent(text), encoding="utf-8")
        self.config = wbconfig.load(self.config_dir, required=True)
        self.modules = modules if modules is not None else discover([REPO / "modules"])
        self.settings = resolve(self.config, {m.name: m.config for m in self.modules})
        self.hub = LogHub(self.state, stdout_level="ERROR", color=False)
        self.workbench = Workbench(self.settings, self.modules, hub=self.hub, host_client=host_client)
        self.server = None

    def start(self) -> "Daemon":
        self.server = serve(self.workbench, "127.0.0.1", 0, self.token, max_wait=10)
        self.port = self.server.server_address[1]
        self.workbench.settings.core["api"]["port"] = self.port
        self.workbench.start()
        self.client = Client(f"http://127.0.0.1:{self.port}", self.token)
        return self

    def stop(self, keep: bool = False):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        self.workbench.stop(timeout=5)
        self.hub.close()
        if not keep:
            shutil.rmtree(self.root, ignore_errors=True)
