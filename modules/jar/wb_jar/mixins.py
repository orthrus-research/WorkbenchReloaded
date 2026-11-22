"""What a JAR's Mixin configurations and Mixin classes declare, read from the archive alone.

A configuration is a JSON resource with a ``package`` and mixin lists (``mixins``, ``client``, ``server``):
every such JSON at the JAR's root, and every resource the JAR's metadata names as one (the manifest's
``MixinConfigs``, ``mixins`` in ``fabric.mod.json``, ``[[mixins]]`` in ``mods.toml``). Its refmap
(``refmap``), where packaged, maps the development names the annotations use to the runtime's; a selector
it does not map is used as written. A handler's targets are what its annotation names, not proof that it
applies: ``mixin.apply`` applies configurations.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from typing import Any

from . import classes
from .archive import Archive

MIXIN = "Lorg/spongepowered/asm/mixin/Mixin;"
OVERWRITE = "Lorg/spongepowered/asm/mixin/Overwrite;"
SHADOW = "Lorg/spongepowered/asm/mixin/Shadow;"
ACCESSORS = {"Lorg/spongepowered/asm/mixin/gen/Accessor;": "accessor",
             "Lorg/spongepowered/asm/mixin/gen/Invoker;": "invoker"}
# Injector annotations, Mixin's and MixinExtras', by the kind of handler each makes.
HANDLERS = {
    "Lorg/spongepowered/asm/mixin/injection/Inject;": "inject",
    "Lorg/spongepowered/asm/mixin/injection/Redirect;": "redirect",
    "Lorg/spongepowered/asm/mixin/injection/ModifyArg;": "modifyArg",
    "Lorg/spongepowered/asm/mixin/injection/ModifyArgs;": "modifyArgs",
    "Lorg/spongepowered/asm/mixin/injection/ModifyVariable;": "modifyVariable",
    "Lorg/spongepowered/asm/mixin/injection/ModifyConstant;": "modifyConstant",
    "Lcom/llamalad7/mixinextras/injector/wrapoperation/WrapOperation;": "wrapOperation",
    "Lcom/llamalad7/mixinextras/injector/v2/WrapWithCondition;": "wrapWithCondition",
    "Lcom/llamalad7/mixinextras/injector/WrapWithCondition;": "wrapWithCondition",
    "Lcom/llamalad7/mixinextras/injector/ModifyExpressionValue;": "modifyExpressionValue",
    "Lcom/llamalad7/mixinextras/injector/ModifyReturnValue;": "modifyReturnValue",
    "Lcom/llamalad7/mixinextras/injector/ModifyReceiver;": "modifyReceiver",
    "Lcom/llamalad7/mixinextras/injector/wrapmethod/WrapMethod;": "wrapMethod",
}
SIDES = ("mixins", "client", "server")
CONSTRUCTORS = {"<init>", "<clinit>"}
_QUANTIFIER = re.compile(r"\{[^}]*\}$")


@dataclass
class Config:
    name: str
    package: str
    """Internal form, such as ``com/example/mixins``."""
    lists: dict[str, list[str]]
    refmap: str | None = None
    plugin: str | None = None
    required: bool = False
    priority: int | None = None

    def classes(self) -> list[tuple[str, str]]:
        """(side, internal class name) of each Mixin the configuration lists."""
        prefix = self.package + "/" if self.package else ""
        return [(side, prefix + entry.replace(".", "/")) for side in SIDES for entry in self.lists.get(side, [])]

    def as_json(self) -> dict:
        return {"name": self.name, "package": self.package, "refmap": self.refmap, "plugin": self.plugin,
                "required": self.required, "priority": self.priority,
                "mixins": {side: len(entries) for side, entries in self.lists.items()}}


def is_config(value: Any) -> bool:
    return (isinstance(value, dict) and isinstance(value.get("package"), str)
            and any(isinstance(value.get(side), list) for side in SIDES))


def declared(archive: Archive) -> list[str]:
    """The Mixin configurations the JAR's metadata names: the manifest's, Fabric's and the mods.toml's."""
    named = [n.strip() for n in archive.manifest().get("MixinConfigs", "").split(",") if n.strip()]
    if "fabric.mod.json" in archive.entries:
        fabric, _ = archive.json("fabric.mod.json")
        items = fabric.get("mixins") if isinstance(fabric, dict) else None
        for item in items if isinstance(items, list) else []:
            config = item.get("config") if isinstance(item, dict) else item
            if isinstance(config, str):
                named.append(config)
    for name in ("META-INF/mods.toml", "META-INF/neoforge.mods.toml"):
        data = archive.read(name, 2**20)
        try:
            table = tomllib.loads(data.decode("utf-8", "replace")) if data else {}
        except tomllib.TOMLDecodeError:
            continue
        named += [m["config"] for m in table.get("mixins") or [] if isinstance(m, dict) and isinstance(m.get("config"), str)]
    return list(dict.fromkeys(named))


def configs(archive: Archive) -> tuple[list[Config], list[str]]:
    """The archive's Mixin configurations, and what is wrong with the ones its metadata names."""
    named = declared(archive)
    candidates = list(dict.fromkeys(named + sorted(n for n in archive.names if "/" not in n and n.endswith(".json"))))
    found, errors = [], []
    for name in candidates:
        value, error = archive.json(name)
        if not is_config(value):
            if name in named:
                errors.append(f"{name}: the JAR's metadata names it as a Mixin configuration, but it is "
                              + (error or "not one"))
            continue
        lists = {side: [e for e in value[side] if isinstance(e, str)] for side in SIDES
                 if isinstance(value.get(side), list)}
        found.append(Config(
            name=name, package=value["package"].replace(".", "/").strip("/"), lists=lists,
            refmap=value["refmap"] if isinstance(value.get("refmap"), str) else None,
            plugin=value["plugin"] if isinstance(value.get("plugin"), str) else None,
            required=value.get("required") is True,
            priority=value["priority"] if isinstance(value.get("priority"), int) else None))
    return found, errors


def refmap(archive: Archive, name: str, environment: str | None = None) -> tuple[dict[str, dict[str, str]], str | None]:
    """A refmap's mappings, ``{mixin internal name: {selector as written: as the runtime knows it}}``:
    the default ones, or those of a ``data`` environment (``searge``, ``notch``, ...)."""
    value, error = archive.json(name)
    if error:
        return {}, f"refmap {name}: {error}"
    if not isinstance(value, dict):
        return {}, f"refmap {name}: not a refmap"
    table = value.get("mappings") if environment is None else (value.get("data") or {}).get(environment)
    if not isinstance(table, dict):
        return {}, f"refmap {name}: no {environment + ' ' if environment else ''}mappings"
    return {mixin: {k: v for k, v in entries.items() if isinstance(v, str)}
            for mixin, entries in table.items() if isinstance(entries, dict)}, None


def selector(text: str) -> tuple[str, str, str]:
    """(owner, name, descriptor) of a target selector: ``Lowner;name(desc)ret``, ``Lowner;name:desc``,
    ``name(desc)``, ``name``. Missing parts are empty; a quantifier such as ``{,1}`` is dropped."""
    rest, owner = text.strip(), ""
    if rest.startswith("L") and ";" in rest:
        owner, rest = rest[1 : rest.index(";")], rest[rest.index(";") + 1 :]
    if "(" in rest:
        name, descriptor = rest[: rest.index("(")], rest[rest.index("(") :]
    elif ":" in rest:
        name, descriptor = rest.split(":", 1)
    else:
        name, descriptor = rest, ""
    return owner, _QUANTIFIER.sub("", name).rstrip("*"), _QUANTIFIER.sub("", descriptor)


@dataclass
class Handler:
    kind: str
    handler: str
    """The handler method's own name."""
    methods: list[str]
    """The target methods its annotation names, through the refmap."""
    at: list[str] = field(default_factory=list)
    """Its injection points' targets (an ``@At``'s ``target``), through the refmap."""
    ordinals: list[int] = field(default_factory=list)
    """Each target's ``ordinal``: which occurrence it hooks, -1 for every one."""
    points: list[str] = field(default_factory=list)
    """Its injection points' kinds: HEAD, RETURN, INVOKE, FIELD, ..."""

    def as_json(self) -> dict:
        return {"kind": self.kind, "handler": self.handler, "methods": self.methods, "at": self.at,
                "ordinals": self.ordinals, "points": self.points}


@dataclass
class Mixin:
    name: str
    targets: list[str]
    priority: int = 1000
    remap: bool = True
    handlers: list[Handler] = field(default_factory=list)
    overwrites: list[tuple[str, str]] = field(default_factory=list)
    """(name, descriptor) of each ``@Overwrite`` method, as this JAR declares it."""
    shadows: list[classes.Member] = field(default_factory=list)
    accessors: list[tuple[str, str]] = field(default_factory=list)
    """(accessor or invoker, the member it names) of each generated accessor."""

    def as_json(self) -> dict:
        return {"name": self.name, "targets": self.targets, "priority": self.priority, "remap": self.remap,
                "handlers": [h.as_json() for h in self.handlers],
                "overwrites": [name + descriptor for name, descriptor in self.overwrites],
                "shadows": [m.name + (m.descriptor if m.descriptor.startswith("(") else ":" + m.descriptor)
                            for m in self.shadows],
                "accessors": [f"{kind} {member}" for kind, member in self.accessors]}


def mixin_of(info: classes.ClassInfo, mappings: dict[str, dict[str, str]]) -> Mixin | None:
    """The Mixin a class declares, with its selectors mapped through ``mappings`` (a refmap's); None
    when the class carries no ``@Mixin``."""
    annotation = info.annotation(MIXIN)
    if annotation is None:
        return None
    own = mappings.get(info.this_class, {})

    def mapped(text: str) -> str:
        return own.get(text, text)

    targets = [classes.descriptor_class(v) for v in annotation.strings("value")]
    targets += [classes.internal(mapped(t)) for t in annotation.strings("targets")]
    priority = annotation.get("priority")
    mixin = Mixin(info.this_class, list(dict.fromkeys(t for t in targets if t)),
                  priority if isinstance(priority, int) else 1000, annotation.get("remap", True) is not False)
    for method in info.declared_methods:
        for a in method.annotations:
            if a.type == OVERWRITE:
                mixin.overwrites.append((method.name, method.descriptor))
            elif a.type in HANDLERS:
                handler = Handler(HANDLERS[a.type], method.name, [mapped(m) for m in a.strings("method")])
                for point in a.nested("at"):
                    handler.points += point.strings("value")
                    ordinal = point.get("ordinal")
                    for target in point.strings("target"):
                        handler.at.append(mapped(target))
                        handler.ordinals.append(ordinal if isinstance(ordinal, int) else -1)
                mixin.handlers.append(handler)
            elif a.type in ACCESSORS:
                mixin.accessors.append((ACCESSORS[a.type], mapped(a.strings("value")[0]) if a.strings("value")
                                        else method.name))
            elif a.type == SHADOW:
                mixin.shadows.append(method)
    mixin.shadows += [f for f in info.declared_fields if f.annotation(SHADOW)]
    return mixin
