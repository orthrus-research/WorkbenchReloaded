"""Profiles for render.validate: the macro sets every stage is compiled under, read from a TOML or JSON file.

    defines = ["MC_VERSION 11202", "MC_GL_VERSION 460"]   # every profile: the macros the loader itself defines
    pack_profiles = false                                 # true: also one profile per profile.NAME of the pack

    [profile.low]
    defines = ["QUALITY 1"]                 # more macros: NAME, NAME VALUE or NAME=VALUE, inserted after #version
    options = { SHADOWS = false, SHADOW_RES = "1024" }   # option values, set where each option is declared
    pack_profile = "LOW"                    # start from the values the pack's own profile.LOW sets
    exclude = ["dh_*"]                      # programs (name or folder/name, glob) this profile does not compile
    only = []                               # when given, the only programs it compiles

Without a file there is one profile, ``default``, with no macros and the pack's default option values.
JSON takes the same shape (``{"defines": [...], "profile": {"low": {...}}}``).
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .pack import Pack, macro, token

PROFILE_KEYS = {"defines", "options", "pack_profile", "exclude", "only"}
TOP_KEYS = {"defines", "pack_profiles", "profile", "profiles"}
NAME = re.compile(r"^[A-Za-z0-9_.-]+$")


@dataclass
class Profile:
    name: str
    defines: list[tuple[str, str | None]] = field(default_factory=list)
    options: dict = field(default_factory=dict)
    pack_profile: str | None = None
    only: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    values: dict = field(default_factory=dict)   # the option values in effect, once resolved against a pack

    def as_dict(self) -> dict:
        return {"name": self.name, "defines": [f"{n} {v}" if v else n for n, v in self.defines],
                "options": self.values, "pack_profile": self.pack_profile, "only": self.only, "exclude": self.exclude}


def read_table(path: Path) -> dict:
    text = Path(path).read_text(encoding="utf-8-sig")
    try:
        data = json.loads(text) if Path(path).suffix.lower() == ".json" else tomllib.loads(text)
    except (ValueError, tomllib.TOMLDecodeError) as error:
        raise ValueError(f"{Path(path).name}: {error}") from None
    if not isinstance(data, dict):
        raise ValueError(f"{Path(path).name}: the file must hold a table")
    return data


def _strings(value, where: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, (str, int, float)) and not isinstance(v, bool)
                                              for v in value):
        raise ValueError(f"{where} must be a list of strings")
    return [str(v) for v in value]


def _macros(value, where: str) -> list[tuple[str, str | None]]:
    out = []
    for item in _strings(value, where):
        try:
            out.append(macro(item))
        except ValueError as error:
            raise ValueError(f"{where}: {error}") from None
    return out


@dataclass
class Spec:
    profiles: list[Profile]
    common: list[tuple[str, str | None]] = field(default_factory=list)
    pack_profiles: bool = False   # also one profile per profile.NAME the pack declares


def parse(data: dict | None) -> Spec:
    """The profiles a file names (not yet checked against a pack)."""
    if not data:
        return Spec([Profile("default")])
    unknown = set(data) - TOP_KEYS
    if unknown:
        raise ValueError(f"unknown keys {sorted(unknown)} (known: {', '.join(sorted(TOP_KEYS))})")
    common = _macros(data.get("defines", []), "defines")
    table = data.get("profile", data.get("profiles", {}))
    if not isinstance(table, dict):
        raise ValueError("[profile] must be a table of profiles")
    profiles = []
    for name, spec in table.items():
        where = f"profile.{name}"
        if not NAME.match(name):
            raise ValueError(f"{where}: a profile name is letters, digits, '.', '_' or '-'")
        if not isinstance(spec, dict):
            raise ValueError(f"{where} must be a table")
        unknown = set(spec) - PROFILE_KEYS
        if unknown:
            raise ValueError(f"{where}: unknown keys {sorted(unknown)} (known: {', '.join(sorted(PROFILE_KEYS))})")
        options = spec.get("options", {})
        if not isinstance(options, dict):
            raise ValueError(f"{where}.options must be a table of option values")
        pack_profile = spec.get("pack_profile")
        if pack_profile is not None and not isinstance(pack_profile, str):
            raise ValueError(f"{where}.pack_profile must be a profile name")
        profiles.append(Profile(name, common + _macros(spec.get("defines", []), f"{where}.defines"),
                                {k: token(v) for k, v in options.items()}, pack_profile,
                                _strings(spec.get("only", []), f"{where}.only"),
                                _strings(spec.get("exclude", []), f"{where}.exclude")))
    wants_pack = data.get("pack_profiles", False)
    if not isinstance(wants_pack, bool):
        raise ValueError("pack_profiles must be true or false")
    if not profiles and not wants_pack:
        profiles.append(Profile("default", common))
    for profile in profiles:
        profile.defines = list(dict.fromkeys(profile.defines))
    return Spec(profiles, common, wants_pack)


def resolve(pack: Pack, spec: Spec) -> list[Profile]:
    """Each profile's option values against the pack; raises ValueError naming what does not fit the pack."""
    props, _ = pack.properties("shaders.properties", pack.environment({}, spec.common))
    declared = pack.pack_profiles(props)
    out = list(spec.profiles)
    if spec.pack_profiles:
        names = {p.name for p in out}
        out += [Profile(name, list(spec.common), pack_profile=name) for name in declared if name not in names]
        if not out:
            raise ValueError("pack_profiles is set, but the pack's shaders.properties declares no profile.NAME")
    for profile in out:
        values: dict = {}
        if profile.pack_profile:
            if profile.pack_profile not in declared:
                raise ValueError(f"profile {profile.name}: the pack has no profile.{profile.pack_profile}")
            values.update(pack.resolve_profile(declared, profile.pack_profile))
        values.update(profile.options)
        wrong = pack.check_values(profile.options, f"profile {profile.name}")
        if wrong:
            raise ValueError("; ".join(wrong))
        profile.values = values
    return out
