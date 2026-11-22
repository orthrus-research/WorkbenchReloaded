"""Rules a pack brings for what its scripts mean, from a set under ``[modules.content.rulesets.<name>]`` and a
TOML or JSON file laid over it. Every key is optional:

    [aliases]                 names under mods. that stand for a mod id, beyond those GroovyScript's JAR declares
    te = "thermalexpansion"

    [mappers]                 object mappers a mod adds, such as metaitem('...'): a reference through one needs it
    metaitem = "gregtech"

    not_recipes = ["jei.*"]   registries (mod.registry globs) whose additions and removals are not recipes

    [[ids]]                   calls that declare an id: a second declaration of the same id is a duplicate
    call = "*.materialBuilder"  # a glob over the call's dotted name (mods. aliases resolved) or over its last part
    arg = 1                     # the argument that holds the id, from 0
    kind = "material"           # the family the id belongs to: ids of different kinds never clash
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class IdRule:
    call: str
    arg: int
    kind: str


@dataclass
class Rules:
    aliases: dict[str, str] = field(default_factory=dict)
    mappers: dict[str, str] = field(default_factory=dict)
    not_recipes: list[str] = field(default_factory=list)
    ids: list[IdRule] = field(default_factory=list)

    def over(self, other: "Rules") -> "Rules":
        """These rules with ``other`` laid over them."""
        return Rules({**self.aliases, **other.aliases}, {**self.mappers, **other.mappers},
                     self.not_recipes + [g for g in other.not_recipes if g not in self.not_recipes],
                     self.ids + other.ids)


def parse(table, where: str) -> Rules:
    """Rules from a table; ValueError names what is wrong."""
    if not isinstance(table, dict):
        raise ValueError(f"{where}: rules must be a table")
    unknown = set(table) - {"aliases", "mappers", "not_recipes", "ids"}
    if unknown:
        raise ValueError(f"{where}: unknown keys {sorted(unknown)} (known: aliases, mappers, not_recipes, ids)")
    out = Rules()
    for key in ("aliases", "mappers"):
        value = table.get(key, {})
        if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str) and v
                                                 for k, v in value.items()):
            raise ValueError(f"{where}: {key} must map names to mod ids")
        setattr(out, key, dict(value))
    globs = table.get("not_recipes", [])
    if not isinstance(globs, list) or not all(isinstance(g, str) for g in globs):
        raise ValueError(f"{where}: not_recipes must be a list of mod.registry globs")
    out.not_recipes = list(globs)
    ids = table.get("ids", [])
    if not isinstance(ids, list):
        raise ValueError(f"{where}: ids must be a list of tables")
    for index, rule in enumerate(ids):
        if not isinstance(rule, dict) or set(rule) - {"call", "arg", "kind"} or not isinstance(rule.get("call"), str) \
                or not isinstance(rule.get("kind"), str) or not isinstance(rule.get("arg", 0), int) \
                or isinstance(rule.get("arg", 0), bool) or rule.get("arg", 0) < 0:
            raise ValueError(f"{where}: ids[{index}] needs call (a glob), kind (a name) and arg (an index from 0)")
        out.ids.append(IdRule(rule["call"], rule.get("arg", 0), rule["kind"]))
    return out


def read_file(path: Path) -> dict:
    """A TOML or JSON table from a file (by its suffix; TOML, then JSON, when it has neither)."""
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() == ".json":
        return json.loads(text)
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        if path.suffix.lower() == ".toml":
            raise
        return json.loads(text)
