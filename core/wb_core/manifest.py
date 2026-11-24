"""Module manifests (spec S3): each module package ships a static ``module.toml``.

The core reads manifests without importing module code, so it can decide at startup which commands are
active, and the SDK schemas come from manifests alone. A command whose ``runs_on`` includes ``host`` is
also a host routine: the Windows host runner runs the same handler natively.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import types

MACHINES = {"container", "host", "rental"}
REQUIREMENT_KINDS = {"secrets", "binary", "jdk", "config", "module", "network", "disk", "host", "lease"}
VOLATILE = {"network", "disk", "host", "lease"}
MODULE_KEYS = {"name", "version", "summary", "startup", "profiles"}
COMMAND_KEYS = {"name", "summary", "handler", "deterministic", "spends", "quote", "runs_on", "requires",
                "params", "result", "resources", "version", "timeout", "cancel_grace", "schedulable", "turn",
                "role", "aliases", "inline"}
ROLES = {"prepare", "play"}  # what a step is to a plan that compiles its game steps for another machine


class ManifestError(Exception):
    pass


@dataclass
class Command:
    module: str
    name: str
    summary: str
    handler: str
    version: str
    deterministic: bool = False
    spends: bool = False
    quote: str | None = None
    runs_on: list[str] = field(default_factory=lambda: ["container"])
    requires: dict = field(default_factory=dict)
    params: dict[str, dict] = field(default_factory=dict)
    result: dict[str, dict] = field(default_factory=dict)
    resources: dict = field(default_factory=dict)
    timeout: float | None = None
    cancel_grace: float = 30.0
    schedulable: bool = True
    turn: bool = False          # may run inside a turn: several host routines under one held lease
    role: str | None = None     # prepare or play: how a plan compiles this step for a rental
    aliases: list[str] = field(default_factory=list)  # former full names that still resolve to this command
    inline: bool = False        # a running job may call it in its own reservation, with no job of its own

    @property
    def full(self) -> str:
        return f"{self.module}.{self.name}"

    def describe(self) -> dict:
        return {"name": self.full, "module": self.module, "summary": self.summary, "version": self.version,
                "deterministic": self.deterministic, "spends": self.spends, "runs_on": self.runs_on,
                "requires": self.requires, "resources": self.resources, "turn": self.turn, "role": self.role,
                "aliases": self.aliases, "inline": self.inline,
                "params": types.object_schema(self.params), "result": types.object_schema(self.result)
                if self.result else {"type": "object"}}


@dataclass
class Module:
    name: str
    version: str
    summary: str
    package: str
    directory: Path
    config: dict[str, dict] = field(default_factory=dict)
    requires: dict = field(default_factory=dict)
    startup: str | None = None
    commands: list[Command] = field(default_factory=list)
    profiles: list[str] = field(default_factory=list)

    @property
    def migrations(self) -> Path:
        return self.directory / "migrations"


def _requires(where: str, value) -> dict:
    if value in (None, {}):
        return {}
    if not isinstance(value, dict):
        raise ManifestError(f"{where}: requires must be a table")
    unknown = set(value) - REQUIREMENT_KINDS
    if unknown:
        raise ManifestError(f"{where}: unknown requirement kinds {sorted(unknown)}")
    out = {}
    for kind, items in value.items():
        if kind == "disk":
            out[kind] = float(items)
        elif kind in ("host", "lease"):
            out[kind] = items
        else:
            out[kind] = list(items) if isinstance(items, (list, tuple)) else [items]
    return out


def _fields(where: str, table) -> dict[str, dict]:
    if table is None:
        return {}
    if not isinstance(table, dict):
        raise ManifestError(f"{where} must be a table")
    try:
        return {name: types.normalise_spec(name, spec) for name, spec in table.items()}
    except types.ValueError_ as error:
        raise ManifestError(f"{where}.{error}") from None


def parse(path: Path, package: str) -> Module:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ManifestError(f"{path}: {error}") from None
    head = data.get("module") or {}
    unknown = set(head) - MODULE_KEYS
    if unknown:
        raise ManifestError(f"{path}: unknown [module] keys {sorted(unknown)}")
    if not head.get("name") or not head.get("version"):
        raise ManifestError(f"{path}: [module] needs name and version")
    name = head["name"]
    module = Module(name=name, version=head["version"], summary=head.get("summary", ""), package=package,
                    directory=path.parent, config=_fields(f"{path}: [config]", data.get("config")),
                    requires=_requires(f"{path}: [requires]", data.get("requires")),
                    startup=head.get("startup"), profiles=list(head.get("profiles", [])))
    seen = set()
    for index, raw in enumerate(data.get("command", [])):
        where = f"{path}: command {raw.get('name', index)}"
        unknown = set(raw) - COMMAND_KEYS
        if unknown:
            raise ManifestError(f"{where}: unknown keys {sorted(unknown)}")
        for key in ("name", "handler"):
            if not raw.get(key):
                raise ManifestError(f"{where}: needs {key}")
        if raw["name"] in seen:
            raise ManifestError(f"{where}: defined twice")
        seen.add(raw["name"])
        runs_on = list(raw.get("runs_on", ["container"]))
        if not runs_on or set(runs_on) - MACHINES:
            raise ManifestError(f"{where}: runs_on must name machines from {sorted(MACHINES)}")
        if raw.get("spends") and not raw.get("quote"):
            raise ManifestError(f"{where}: a command that spends must name its quote handler")
        resources = dict(raw.get("resources", {}))
        unknown = set(resources) - {"cpu", "mem_gb", "gpu", "quiet", "quiet_if", "locks"}
        if unknown:
            raise ManifestError(f"{where}: unknown resources {sorted(unknown)}")
        for key in ("quiet_if", "locks"):
            names = resources.get(key, [])
            if not isinstance(names, list) or not all(isinstance(v, str) and v for v in names):
                raise ManifestError(f"{where}: resources.{key} is a list of names")
        for entry in resources.get("quiet_if", []):
            param = entry.partition("=")[0]  # a param, or param=value: quiet only for that value
            if param not in (raw.get("params") or {}):
                raise ManifestError(f"{where}: resources.quiet_if names {param!r}, which is not a param")
        if raw.get("inline") and (raw.get("spends") or runs_on != ["container"] or raw.get("turn")):
            raise ManifestError(f"{where}: an inline command runs in the container, spends nothing and takes no turn")
        timeout = raw.get("timeout")
        if raw.get("role") is not None and raw["role"] not in ROLES:
            raise ManifestError(f"{where}: role must be one of {sorted(ROLES)}")
        if raw.get("turn") and "host" not in runs_on:
            raise ManifestError(f"{where}: only host routines run in a turn")
        aliases = list(raw.get("aliases", []))
        if not all(isinstance(a, str) and a.count(".") == 1 and all(a.split(".")) for a in aliases):
            raise ManifestError(f"{where}: aliases must be full names like module.command")
        params = _fields(f"{where}.params", raw.get("params"))
        if raw.get("deterministic") and any(p.get("digest") in ("git", "none") for p in params.values()):
            raise ManifestError(f"{where}: a deterministic command is cached by its inputs' content, so none of its "
                                "params may use digest = \"git\" or \"none\"")
        module.commands.append(Command(
            module=name, name=raw["name"], summary=raw.get("summary", ""), handler=raw["handler"],
            version=str(raw.get("version", module.version)), deterministic=bool(raw.get("deterministic", False)),
            spends=bool(raw.get("spends", False)), quote=raw.get("quote"), runs_on=runs_on,
            requires=_requires(where, raw.get("requires")), params=params,
            result=_fields(f"{where}.result", raw.get("result")), resources=resources,
            timeout=types.parse_duration(timeout) if timeout is not None else None,
            cancel_grace=types.parse_duration(raw.get("cancel_grace", 30)),
            schedulable=bool(raw.get("schedulable", not raw.get("spends", False))),
            turn=bool(raw.get("turn", False)), role=raw.get("role"), aliases=aliases,
            inline=bool(raw.get("inline", False))))
    return module


def _path_manifests(roots: list[Path]):
    for root in roots:
        if not root.is_dir():
            continue
        for module_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            for package_dir in sorted(module_dir.glob("wb_*")):
                manifest = package_dir / "module.toml"
                if manifest.is_file():
                    yield module_dir, package_dir.name, manifest


def discover(extra_paths: list[Path] | None = None) -> list[Module]:
    """Modules from the ``wb.modules`` entry points and from module folders on ``WB_MODULE_PATH``.
    A module found both ways is taken once (the path wins, for development)."""
    found: dict[str, Module] = {}
    roots = list(extra_paths or [])
    roots += [Path(p) for p in os.environ.get("WB_MODULE_PATH", "").split(os.pathsep) if p]
    for module_dir, package, manifest in _path_manifests(roots):
        if str(module_dir) not in sys.path:
            sys.path.insert(0, str(module_dir))
        module = parse(manifest, package)
        if module.name in found:
            raise ManifestError(f"module {module.name} found twice: {found[module.name].directory} and {manifest.parent}")
        found[module.name] = module
    for entry in importlib.metadata.entry_points(group="wb.modules"):
        spec = importlib.util.find_spec(entry.value)
        if spec is None or not spec.submodule_search_locations:
            continue
        manifest = Path(list(spec.submodule_search_locations)[0]) / "module.toml"
        if manifest.is_file():
            module = parse(manifest, entry.value)
            found.setdefault(module.name, module)
    return [found[name] for name in sorted(found)]

