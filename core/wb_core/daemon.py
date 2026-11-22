"""``python -m wb_core``: load the config folder and the modules, recover, run startup hooks, serve /v1.

Config comes from WB_CONFIG (in the image: /etc/wb). Modules come from the ``wb.modules`` entry points,
from WB_MODULE_PATH, and from the repository's own ``modules/`` folder.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
from pathlib import Path

from wb import config as wbconfig

from . import __version__
from .api import serve
from .hostclient import HostClient
from .logs import LogHub
from .manifest import ManifestError, discover
from .service import REPO_ROOT, Workbench
from .settings import SettingsError, resolve


def load(config_folder: str | None = None):
    config = wbconfig.load(config_folder or os.environ.get("WB_CONFIG") or "/etc/wb")
    modules = discover([REPO_ROOT / "modules"])
    settings = resolve(config, {m.name: m.config for m in modules})
    # The platform profile `wb up` chose (wb.platform): where to listen, and how to reach wb-host.
    if os.environ.get("WB_API_LISTEN"):
        settings.core["api"]["listen"] = os.environ["WB_API_LISTEN"]
    if os.environ.get("WB_API_PORT"):
        settings.core["api"]["port"] = int(os.environ["WB_API_PORT"])
    if os.environ.get("WB_HOST_URL"):
        settings.core["host"]["url"] = os.environ["WB_HOST_URL"]
    names = {c.full: c for m in modules for c in m.commands}
    for name, schedule in settings.schedules.items():
        command = names.get(schedule["command"])
        if command is None:
            raise SettingsError(f"schedule {name}: unknown command {schedule['command']}")
        if command.spends or not command.schedulable:
            raise SettingsError(f"schedule {name}: {command.full} spends money and may never run on a schedule (D13)")
    return settings, modules


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wb-core", description="The Workbench daemon.")
    parser.add_argument("--config", help="config folder (default: WB_CONFIG or /etc/wb)")
    parser.add_argument("--check", action="store_true", help="check the config and modules, then exit")
    args = parser.parse_args(argv)
    try:
        settings, modules = load(args.config)
    except (wbconfig.ConfigError, SettingsError, ManifestError) as error:
        print(f"wb-core: {error}", file=sys.stderr)
        return 2
    core = settings.core
    hub = LogHub(settings.state_dir, rotate_mb=core["logging"]["rotate_mb"], file_level=core["logging"]["file_level"],
                 stdout_level=core["logging"]["stdout_level"], color=core["logging"]["color"])
    workbench = Workbench(settings, modules, hub=hub, host_client=HostClient.from_settings(settings))
    workbench.reloader = lambda: load(args.config)[0]
    for name in sorted(workbench.registry):
        entry = workbench.registry[name]
        if entry.activation.active:
            hub.info(f"{name}: active" + (f" ({len(entry.activation.warnings)} warning(s))"
                                          if entry.activation.warnings else ""))
        else:
            hub.warning(f"{name}: inactive: " + "; ".join(r.detail for r in entry.activation.reasons))
    if args.check:
        hub.close()
        return 0
    token_path = Path(core["api"]["token_file"])
    token = token_path.read_text(encoding="ascii").strip() if token_path.is_file() else os.environ.get("WB_TOKEN")
    if not token:
        hub.error(f"no API token at {token_path}; refusing to serve without one (D14)")
        return 3
    workbench.start()
    server = serve(workbench, core["api"]["listen"], core["api"]["port"], token,
                   host_check=core["api"]["host_check"], max_wait=core["api"]["max_wait"])
    hub.info(f"wb-core {__version__} serving /v1 on {core['api']['listen']}:{core['api']['port']} "
             f"({len(modules)} modules, {sum(e.activation.active for e in workbench.registry.values())} active commands)")
    done = threading.Event()

    def stop(*_):
        done.set()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        while not done.wait(1):
            pass
    finally:
        hub.info("stopping")
        server.shutdown()
        workbench.stop()
        hub.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
