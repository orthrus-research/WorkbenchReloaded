"""``wb host serve | install | uninstall | status`` on Windows.

``install`` makes wb-host start when the user logs in, the way the platform does it (wb_host.startup): a Task
Scheduler task on Windows, a systemd user unit on Linux, a LaunchAgent on macOS. That changes the system, so it
shows exactly what it will write and run, and does nothing without ``--yes``.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets as pysecrets
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path


def _repo() -> Path:
    """The Workbench folder the task should start: as the CLI was started (a release's current link stays a link, so
    the next deploy's release is what the task runs), else this file's checkout."""
    if os.environ.get("WB_REPO"):
        return Path(os.path.abspath(os.environ["WB_REPO"]))
    return Path(__file__).resolve().parents[2]


def _token(path: Path) -> str:
    if not path.exists():
        from wb.compose import write_secret_file
        write_secret_file(path, pysecrets.token_urlsafe(32))
    return path.read_text(encoding="ascii").strip()


def serve(args) -> int:
    from wb import config as wbconfig
    from wb_core.manifest import discover
    from wb_core.service import REPO_ROOT
    from wb_core.settings import resolve

    from .runner import HostServer, Runner

    config = wbconfig.load(args.config) if args.config else wbconfig.load()
    modules = discover([REPO_ROOT / "modules"])
    schemas = {m.name: m.config for m in modules}
    settings = resolve(config, schemas)
    folder = Path(args.config) if args.config else wbconfig.default_folder()

    def stamp():
        files = sorted(folder.rglob("*.toml")) if folder.is_dir() else []
        return tuple((str(f), f.stat().st_mtime_ns, f.stat().st_size) for f in files)

    def reload():
        return resolve(wbconfig.load(args.config) if args.config else wbconfig.load(), schemas)
    token_path = Path(os.path.expanduser(settings.runner["token_file"]))
    token = _token(token_path)
    port = args.port or settings.runner["port"]
    runner = Runner(settings, modules, reload=reload, stamp=stamp)
    server = HostServer(runner, token, port)
    print(f"wb-host serving {len(runner.routines)} routine(s) on 127.0.0.1:{server.server_address[1]}; "
          f"lab home {runner.lab_home}; lease {runner.lease_file}", flush=True)
    stop = threading.Event()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        while not stop.wait(1):
            pass
    except KeyboardInterrupt:
        pass
    server.shutdown()
    return 0


def install(args) -> int:
    from wb import platform

    from . import startup
    repo = _repo()
    plan = startup.install_plan(repo, args.platform or platform.os_name())
    print(plan.describe())
    if not args.yes:
        print("Nothing changed. Run again with --yes to do it.")
        return 0
    return startup.apply(plan)


def uninstall(args) -> int:
    from wb import platform

    from . import startup
    repo = _repo()
    plan = startup.uninstall_plan(repo, args.platform or platform.os_name())
    print(plan.describe())
    if not args.yes:
        print("Nothing changed. Run again with --yes to do it.")
        return 0
    return startup.apply(plan, removing=True)


def status(args) -> int:
    from wb import config as wbconfig
    config = wbconfig.load()
    port = (config.table("runner") or {}).get("port", 7378)
    token_path = Path(os.path.expanduser((config.table("runner") or {}).get("token_file", "~/.config/wb/run/host.token")))
    request = urllib.request.Request(f"http://127.0.0.1:{port}/v1/health")
    if token_path.exists():
        request.add_header("Authorization", "Bearer " + token_path.read_text(encoding="ascii").strip())
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            print(json.dumps(json.loads(response.read()), indent=1))
            return 0
    except Exception as error:  # noqa: BLE001
        print(f"wb-host is not answering on 127.0.0.1:{port}: {error}")
        return 3


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="wb host")
    sub = parser.add_subparsers(dest="action", required=True)
    p = sub.add_parser("serve")
    p.add_argument("--config")
    p.add_argument("--port", type=int)
    for name in ("install", "uninstall"):
        p = sub.add_parser(name)
        p.add_argument("--yes", action="store_true")
        p.add_argument("--platform", choices=["windows", "linux", "macos"], help="show another platform's plan")
    sub.add_parser("status")
    args = parser.parse_args(argv)
    return {"serve": serve, "install": install, "uninstall": uninstall, "status": status}[args.action](args)


if __name__ == "__main__":
    sys.exit(main())
