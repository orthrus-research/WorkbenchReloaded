"""A launch bundle's runner on the rental (``runner/launch_run.py`` in a bundle vast.bundle-launch made).

``python3 launch_run.py build <bundle> <lab>`` builds the instance from the bundle's ``launch.json`` with Workbench's
own lab.build code (``runner/wb_lab``, copied from the Workbench that made the bundle): the members, the runtime
files and the JDK from the bundle's ``store/``, the overlay folders and configure script from ``sources/`` by the ids
the launch file names, the libraries and assets by SHA-1 into ``<lab>/cache``. ``python3 launch_run.py tour <bundle>
<lab>`` plays one tour session of it with lab.run, headless on the display the run script started, and copies its
session.json into ``results/``. The whole rental is the bundle's, so the session's turn is the rental itself.

It prints ``RUN <what>`` for each milestone (vast.rent shows those lines) and every log line indented beneath. The
exit code is 0 only when the step ended as it should: an instance built, a session whose views were all captured.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from wb_core.module import Context  # noqa: E402

BUNDLE_INFO = "bundle-launch.json"


def emit(event: dict):
    kind = event.get("kind")
    if kind == "log":
        print(f"  {event.get('level', '')} {event.get('msg', '')}", flush=True)
    elif kind == "progress" and event.get("msg"):
        print(f"  [{event.get('phase')}] {event.get('msg')}", flush=True)
    elif kind == "finding":
        print(f"  finding: {event.get('summary', '')}", flush=True)


def defaults() -> dict:
    """The lab module's own config defaults, from its manifest (the asset host, the timeouts), as a job gets them."""
    import tomllib
    table = tomllib.loads((HERE / "wb_lab" / "module.toml").read_text(encoding="utf-8")).get("config") or {}
    return {key: spec["default"] for key, spec in table.items() if isinstance(spec, dict) and "default" in spec}


def context(bundle: Path, lab: Path, command: str, info: dict) -> Context:
    results = bundle / "results"
    spec = {"job": f"rental-{command}", "command": f"lab.{command}", "module": "lab", "machine": "host",
            "work": str(lab / "work" / command), "out": str(results / command),
            "module_config": {**defaults(), "instances": str(lab / "instances"), "runtime_cache": str(lab / "cache"),
                              "sessions": str(results / "sessions"), "metadata": info.get("metadata") or "",
                              "runtime_hosts": []},
            "lab": {"home": str(lab), "trusted": list(info.get("trusted") or []), "eula": bool(info.get("eula")),
                    "env": {}}}
    return Context(spec, emit)


def build(bundle: Path, lab: Path, info: dict) -> int:
    from wb_lab import instance as instances
    ctx = context(bundle, lab, "build", info)
    result = instances.build(ctx, str(bundle / info["launch"]["file"]), info["instance"],
                             params=dict(info.get("params") or {}), eula=bool(info.get("eula")),
                             store=str(bundle / info["store"]), sources=str(bundle / info["sources"]))
    receipt = Path(result["instance"]) / instances.RECEIPT
    shutil.copyfile(receipt, bundle / "results" / "instance.json")
    print(f"RUN built {info['instance']} from template {result['template']} "
          f"({'new' if result.get('built') else 'kept'}): {result['instance']}", flush=True)
    return 0 if ctx.outcome == "ok" else 1


def bundle_jdk(bundle: Path, info: dict) -> Path:
    """The JDK home in the bundle's own Store copy for the build the instance was made on: the launch file names
    the Store of the machine that resolved it, which a rental does not have."""
    from wb_lab import instance as instances
    receipt = json.loads((bundle / "results" / "instance.json").read_text(encoding="utf-8"))
    (_, entry), = receipt["jdk"].items()
    return instances.jdk_home(bundle / info["store"], str(entry["build"]), receipt["system"])


def tour(bundle: Path, lab: Path, info: dict) -> int:
    from wb_lab import run as sessions
    os.environ.setdefault("WB_TURN_TOKEN", "rental-" + secrets.token_hex(8))  # the rental is the turn
    ctx = context(bundle, lab, "run", info)
    result = sessions.run(ctx, info["instance"], kind="tour", tour=str(bundle / info["tour"]["file"]),
                          window=str(info.get("window") or ""), limit=float(info.get("limit") or 5400),
                          jdk=str(bundle_jdk(bundle, info)))
    record = json.loads((Path(result["folder"]) / sessions.SESSION).read_text(encoding="utf-8"))
    shutil.copyfile(Path(result["folder"]) / sessions.SESSION, bundle / "results" / "session.json")
    captured = record.get("tour") or {}
    print(f"RUN session {result['session']} {result['state']}: {captured.get('captured', 0)} of "
          f"{captured.get('views', 0)} views captured", flush=True)
    failed = ctx.findings or result["state"] != "ended" or captured.get("captured") != captured.get("views")
    return 1 if failed else 0


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[0] not in ("build", "tour"):
        print("usage: launch_run.py build|tour <bundle> <lab>", file=sys.stderr)
        return 2
    step, bundle, lab = argv[0], Path(argv[1]).resolve(), Path(argv[2]).resolve()
    info = json.loads((bundle / BUNDLE_INFO).read_text(encoding="utf-8"))
    (bundle / "results").mkdir(parents=True, exist_ok=True)
    try:
        return (build if step == "build" else tour)(bundle, lab, info)
    except Exception as error:  # noqa: BLE001 - the step failed: say why on the line vast.rent watches
        traceback.print_exc()
        print(f"RUN {'build' if step == 'build' else 'session'} failed: {type(error).__name__}: "
              f"{str(error).splitlines()[0][:300] if str(error) else ''}", flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
