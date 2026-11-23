"""The core's config schema, and the check of a whole config folder against it and the modules' schemas.

Unknown keys and wrong types stop startup with the file and line that set them (spec S10).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from wb import config as wbconfig

from . import types

CORE = {
    "api": {
        "port": {"type": "int", "default": 7377, "min": 0, "max": 65535, "summary": "0 picks a free port (tests)"},
        "listen": {"type": "string", "default": "0.0.0.0",
                   "summary": "address inside the container; the compose file publishes it on 127.0.0.1 only"},
        "publish": {"type": "enum", "values": ["127.0.0.1", "localhost", "::1"], "default": "127.0.0.1",
                    "summary": "where the compose file publishes the port: loopback only (D3)"},
        "host_check": {"type": "bool", "default": True},
        "token_file": {"type": "string", "default": "/run/secrets/wb_token"},
        "max_wait": {"type": "duration", "default": "60s"},
    },
    "logging": {
        "stdout_level": {"type": "enum", "values": ["DEBUG", "INFO", "WARNING", "ERROR"], "default": "INFO"},
        "file_level": {"type": "enum", "values": ["DEBUG", "INFO", "WARNING", "ERROR"], "default": "DEBUG"},
        "rotate_mb": {"type": "int", "default": 64, "min": 1},
        "color": {"type": "bool", "default": True},
    },
    "state": {
        "dir": {"type": "string", "default": "/var/lib/wb",
                "summary": "where the daemon keeps its database, jobs and logs; run natively, the default is the "
                           "user's own state folder"},
    },
    "budget": {
        "cpu_cores": {"type": "float", "optional": True, "min": 0.5},
        "mem_gb": {"type": "float", "optional": True, "min": 0.5},
    },
    "retention": {
        "days": {"type": "int", "default": 30, "min": 1},
    },
    "host": {
        "url": {"type": "string", "default": "http://host.docker.internal:7378"},
        "token_secret": {"type": "string", "default": "wb_host_token"},
        "enabled": {"type": "bool", "default": True},
        "heartbeat_timeout": {"type": "duration", "default": "60s"},
    },
    "image": {
        "name": {"type": "string", "default": "wb-core:dev"},
        "volume": {"type": "string", "default": "wb-state"},
        "tools": {"type": "list", "items": "string", "default": [],
                  "summary": "optional tools built into the image (image/pins.toml [tool.<name>]); wb build installs them"},
    },
    "quotes": {
        "ttl": {"type": "duration", "default": "5m", "min": "30s", "max": "30m"},
    },
    "jdks": {
        "root": {"type": "string", "default": "/opt/jdk"},
    },
    "config": {
        "layers": {"type": "list", "items": "string", "default": [],
                   "summary": "shared config folders this folder lies over, merged first in this order (only in the "
                              "machine folder's workbench.toml)"},
        "ignore": {"type": "list", "items": "string", "default": [],
                   "summary": "files in this config folder that hold other tools' settings, not Workbench config "
                              "(glob patterns relative to the folder); they are mounted with it but never read as config"},
    },
}

ROOT_FIELDS = {"host": {"type": "string"}, "container": {"type": "string", "optional": True},
               "mode": {"type": "enum", "values": ["ro", "rw"], "default": "ro"}}
SECRET_FIELDS = {"host": {"type": "string"}, "container": {"type": "string", "optional": True}}
SCHEDULE_FIELDS = {"command": {"type": "string"}, "every": {"type": "duration", "min": "1m"},
                   "params": {"type": "object", "default": {}}, "enabled": {"type": "bool", "default": True}}
HOST_RUNNER = {
    "port": {"type": "int", "default": 7378},
    "lab_home": {"type": "string", "default": "../Lab",
                 "summary": "the host's lab home; relative paths are from the Workbench checkout"},
    "lease_file": {"type": "string", "optional": True,
                   "summary": "the GPU lease file; defaults to <lab_home>/lease/gpu.lock"},
    "token_file": {"type": "string", "default": "~/.config/wb/run/host.token"},
    "coordination_dir": {"type": "string", "optional": True,
                         "summary": "the folder where every GPU user publishes its lane; defaults to <lab_home>/coordination"},
    "lane": {"type": "string", "default": "workbench", "summary": "Workbench's lane name in the coordination folder"},
    "token_env": {"type": "list", "items": "string", "default": [],
                  "summary": "more variables a host routine gets the turn token in (WB_TURN_TOKEN always), for "
                             "launchers that join a held lease by their own variable"},
    "lane_env": {"type": "list", "items": "string", "default": [],
                 "summary": "more variables a host routine gets the lane name in (WB_TURN_LANE always)"},
    "jdks": {"type": "object", "default": {},
             "summary": "the host's JDKs by key (\"26\" = its home); host routines get JAVA_HOME_<major>_X64 for each"},
    "java_home": {"type": "string", "default": "26", "summary": "the JDK key host routines get as JAVA_HOME"},
}
# Where a person's work lives on this host, read by the host CLI (the daemon only checks it).
WORKSPACE = {
    "work": {"type": "string", "default": "", "summary": "the folder Works are made in; empty: <projects root>/work"},
    "set": {"type": "string", "default": "",
            "summary": "the Set's home: a bare repository on this host that wb land writes with plumbing"},
}
TOP_LEVEL = {"core", "paths", "secrets", "schedules", "modules", "runner", "workspace"}


class SettingsError(Exception):
    pass


@dataclass
class Root:
    name: str
    host: str
    container: str
    mode: str


@dataclass
class Settings:
    config: wbconfig.Config
    core: dict
    roots: list[Root] = field(default_factory=list)
    secrets: dict[str, dict] = field(default_factory=dict)
    schedules: dict[str, dict] = field(default_factory=dict)
    modules: dict[str, dict] = field(default_factory=dict)
    runner: dict = field(default_factory=dict)
    workspace: dict = field(default_factory=dict)

    @property
    def state_dir(self) -> Path:
        if os.environ.get("WB_STATE_DIR"):
            return Path(os.environ["WB_STATE_DIR"])
        configured = self.core["state"]["dir"]
        if configured == DEFAULT_STATE and not in_container():
            return native_state_dir()  # the container's path would land at the root of a Windows drive
        return Path(configured)

    def secret_path(self, name: str) -> Path | None:
        entry = self.secrets.get(name)
        if entry is None:
            explicit = Path("/run/secrets") / name
            return explicit if explicit.exists() else None
        return Path(entry["container"])


def _where(config: wbconfig.Config, dotted: str) -> str:
    origin = config.origin(dotted)
    return f"{origin}: " if origin else ""


def _check(config: wbconfig.Config, prefix: str, fields: dict, values) -> dict:
    if values is None:
        values = {}
    if not isinstance(values, dict):
        raise SettingsError(f"{_where(config, prefix)}[{prefix}] must be a table")
    try:
        return types.validate(fields, values, where="keys")
    except types.ValueError_ as error:
        dotted = f"{prefix}.{error.field}"
        raise SettingsError(f"{_where(config, dotted)}{dotted}: {error.detail}") from None


def in_container() -> bool:
    return Path("/.dockerenv").exists() or os.environ.get("WB_IN_CONTAINER") == "1"


DEFAULT_STATE = "/var/lib/wb"  # the container's state volume


def native_state_dir() -> Path:
    """A daemon run outside the container keeps its state in the user's own folder: LOCALAPPDATA on Windows,
    XDG_STATE_HOME (or ~/.local/state) elsewhere."""
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "wb" / "state"
    base = os.environ.get("XDG_STATE_HOME")
    return (Path(base) if base else Path.home() / ".local" / "state") / "wb"


def resolve(config: wbconfig.Config, module_schemas: dict[str, dict] | None = None) -> Settings:
    values = config.values
    unknown = set(values) - TOP_LEVEL
    if unknown:
        key = sorted(unknown)[0]
        raise SettingsError(f"{_where(config, key)}unknown top-level table [{key}] "
                            f"(known: {', '.join(sorted(TOP_LEVEL))})")
    core_values = values.get("core", {}) or {}
    unknown = set(core_values) - set(CORE)
    if unknown:
        key = sorted(unknown)[0]
        raise SettingsError(f"{_where(config, 'core.' + key)}unknown table [core.{key}]")
    core = {section: _check(config, f"core.{section}", fields, core_values.get(section))
            for section, fields in CORE.items()}
    roots = []
    for name, raw in (values.get("paths", {}) or {}).get("roots", {}).items():
        entry = _check(config, f"paths.roots.{name}", ROOT_FIELDS, raw)
        # In the container a root lives where `wb up` mounted it; run natively, it is the host path itself.
        container = wbconfig.root_mount(name, entry) if in_container() else (entry.get("container") or entry["host"])
        roots.append(Root(name=name, host=entry["host"], container=container, mode=entry["mode"]))
    extra = set((values.get("paths") or {})) - {"roots"}
    if extra:
        raise SettingsError(f"{_where(config, 'paths.' + sorted(extra)[0])}unknown key paths.{sorted(extra)[0]}")
    secrets = {}
    for name, raw in (values.get("secrets") or {}).items():
        entry = _check(config, f"secrets.{name}", SECRET_FIELDS, raw)
        entry.setdefault("container", f"/run/secrets/{name}")
        if not in_container() and "container" not in raw:
            entry["container"] = os.path.expanduser(entry["host"])
        secrets[name] = entry
    schedules = {name: _check(config, f"schedules.{name}", SCHEDULE_FIELDS, raw)
                 for name, raw in (values.get("schedules") or {}).items()}
    module_values = values.get("modules") or {}
    module_schemas = module_schemas or {}
    unknown = set(module_values) - set(module_schemas)
    if unknown and module_schemas:
        key = sorted(unknown)[0]
        raise SettingsError(f"{_where(config, 'modules.' + key)}config for unknown module [modules.{key}]")
    modules = {name: _check(config, f"modules.{name}", schema, module_values.get(name))
               for name, schema in module_schemas.items()}
    runner = _check(config, "runner", HOST_RUNNER, values.get("runner"))
    workspace = _check(config, "workspace", WORKSPACE, values.get("workspace"))
    return Settings(config=config, core=core, roots=roots, secrets=secrets, schedules=schedules,
                    modules=modules, runner=runner, workspace=workspace)
