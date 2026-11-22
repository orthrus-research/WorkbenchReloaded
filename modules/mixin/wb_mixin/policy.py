"""Loader policies for ``mixin.doctor``: what a loader's Mixin accepts, and how loudly each check reports.

A policy is a table of the keys below. It is named under ``[modules.mixin.loaders.<name>]``, read from a TOML
or JSON file, or given as the command's params, each laid over the one before (``severity`` key by key).
Workbench ships none: a loader's facts are config.

    compatibility_levels = ["JAVA_8..JAVA_25"]   # the levels the loader's Mixin defines: names or ranges
    mixin_version = "0.8.7"                      # the loader's Mixin: no minVersion may be above it
    max_class_version = 69                       # the highest class-file version the runtime reads (Java 25)
    required_fields = ["compatibilityLevel", "minVersion"]   # keys every configuration must set
    required_may_target_missing = false          # true: the loader skips a required configuration's Mixin
                                                 # whose target is missing instead of stopping the game
    injection_namespaces = ["MIXINEXTRAS"]       # the namespaced injection points the loader provides
    remapped_packages = ["net/minecraft/"]       # targets whose names a refmap maps (default: every target)

    [severity]                                   # per check (CHECKS): error, warning or off
    registration = "error"

A key left out is not checked: without ``compatibility_levels`` any ``JAVA_<n>`` passes, without
``mixin_version`` only the form of ``minVersion`` is checked, without ``max_class_version`` no class is too
new, without ``injection_namespaces`` a namespaced injection point is not checked. An error is a finding, a
warning is reported in the report and the log, and a check that is off is skipped.
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# Each check with its default severity and what it reports (wb_mixin/doctor.py says how).
CHECKS = {
    "parse": ("error", "a JAR or configuration is unreadable, not JSON or not a configuration, or a key has "
                       "the wrong type"),
    "fields": ("error", "a key the policy requires is not set"),
    "classes": ("error", "no package, or a listed Mixin is not in the JAR or its package, carries no @Mixin, or "
                         "is listed twice"),
    "refmap": ("error", "the refmap is not packaged or not readable, or none is named while the Mixins remap "
                        "selectors"),
    "compatibility": ("error", "compatibilityLevel is not a level, or not one the loader defines"),
    "coverage": ("error", "compatibilityLevel's class-file version is below a Mixin class's"),
    "class-version": ("error", "a Mixin or plugin class is above the highest class-file version the runtime reads"),
    "min-version": ("error", "minVersion is not a version Mixin reads, or is above the loader's Mixin version"),
    "plugin": ("error", "the configuration plugin class is not in the JARs or on the class path"),
    "targets": ("error", "a @Mixin target is not on the class path (a warning where the loader only skips the Mixin)"),
    "injection-points": ("error", "an @At names no point Mixin defines, a namespace the loader lacks, or a class "
                                  "that is not there"),
    "duplicates": ("error", "a configuration name is in more than one of the JARs"),
    "registration": ("warning", "no manifest key, loader entry or class names the configuration, so nothing "
                                "registers it"),
}
SEVERITIES = ("error", "warning", "off")
KEYS = {"compatibility_levels", "mixin_version", "max_class_version", "required_fields", "required_may_target_missing",
        "injection_namespaces", "remapped_packages", "severity"}
SHORT = 32767  # a VersionNumber part is a Java short
_LEVEL = re.compile(r"JAVA_([1-9][0-9]*)")  # Mixin looks a level up by its enum name: JAVA_8, never JAVA_08
_RANGE = re.compile(r"JAVA_([1-9][0-9]*)\s*\.\.\s*JAVA_([1-9][0-9]*)")
_VERSION = re.compile(r"([0-9]{1,5})(?:\.([0-9]{1,5})(?:\.([0-9]{1,5})(?:\.([0-9]{1,5}))?)?)?(-[a-zA-Z0-9_\-]+)?")
_NAMESPACE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def class_version(level: int) -> int:
    """The class-file version that compatibility level ``JAVA_<level>`` covers: JAVA_8 52, JAVA_25 69."""
    return level + 44


def java(major: int) -> str:
    """The Java release of a class-file version: 52 is Java 8, 70 is Java 26."""
    return f"Java 1.{major - 44}" if major < 49 else f"Java {major - 44}"


def level_of(text: str) -> int | None:
    """``JAVA_<n>`` as Mixin reads a level (trimmed, upper case) -> n; None when it is not one."""
    match = _LEVEL.fullmatch(text.strip().upper())
    return int(match.group(1)) if match else None


def version(text: str) -> tuple[tuple[int, int, int, int] | None, str | None]:
    """A version as Mixin's ``VersionNumber`` reads it: one to four numeric parts (missing ones are 0) and an
    optional ``-suffix`` that does not count. None and ``malformed`` (Mixin reads it as no version) or
    ``overflow`` (a part above 32767, which Mixin refuses) when it cannot."""
    match = _VERSION.fullmatch(text)
    if not match:
        return None, "malformed"
    parts = tuple(int(p) if p else 0 for p in match.groups()[:4])
    if any(p > SHORT for p in parts):
        return None, "overflow"
    return parts, None  # type: ignore[return-value]


def span(levels) -> str:
    """Levels as compact ranges: JAVA_8..JAVA_25, or JAVA_8, JAVA_11..JAVA_17."""
    ordered, runs = sorted(levels), []
    for level in ordered:
        if runs and level == runs[-1][1] + 1:
            runs[-1][1] = level
        else:
            runs.append([level, level])
    return ", ".join(f"JAVA_{a}" if a == b else f"JAVA_{a}..JAVA_{b}" for a, b in runs) or "none"


def _strings(value, where: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise ValueError(f"{where} must be a list of names")
    return [v.strip() for v in value]


def _levels(value, where: str) -> frozenset[int]:
    out: set[int] = set()
    for item in _strings(value, where):
        text = item.upper()
        single, run = _LEVEL.fullmatch(text), _RANGE.fullmatch(text)
        if single:
            out.add(int(single.group(1)))
        elif run and int(run.group(1)) <= int(run.group(2)):
            out.update(range(int(run.group(1)), int(run.group(2)) + 1))
        else:
            raise ValueError(f"{where}: {item!r} is not a level (JAVA_17) or a range (JAVA_8..JAVA_25)")
    if not out:
        raise ValueError(f"{where}: name at least one level")
    return frozenset(out)


def check(table, where: str) -> dict:
    """``table`` checked as a policy, its values normalised; a ValueError names ``where`` and the key."""
    if not isinstance(table, dict):
        raise ValueError(f"{where}: a policy must be a table")
    unknown = set(table) - KEYS
    if unknown:
        raise ValueError(f"{where}: unknown keys {sorted(unknown)} (known: {', '.join(sorted(KEYS))})")
    out: dict = {}
    for key, value in table.items():
        at = f"{where}: {key}"
        if key == "compatibility_levels":
            out[key] = _levels(value, at)
        elif key == "mixin_version":
            if not isinstance(value, str) or version(value.split("+", 1)[0].strip())[0] is None:
                raise ValueError(f"{at} must be a version string such as \"0.8.7\"")
            out[key] = value.strip()
        elif key == "max_class_version":
            if isinstance(value, bool) or not isinstance(value, int) or value < 45:
                raise ValueError(f"{at} must be a class-file version, such as 69 (Java 25)")
            out[key] = value
        elif key == "required_fields":
            out[key] = tuple(dict.fromkeys(_strings(value, at)))
        elif key == "required_may_target_missing":
            if not isinstance(value, bool):
                raise ValueError(f"{at} must be true or false")
            out[key] = value
        elif key == "injection_namespaces":
            names = _strings(value, at)
            if not all(_NAMESPACE.fullmatch(n) for n in names):
                raise ValueError(f"{at} must name namespaces such as MIXINEXTRAS")
            out[key] = frozenset(n.upper() for n in names)
        elif key == "remapped_packages":
            out[key] = tuple(p.replace(".", "/").rstrip("/") + "/" for p in _strings(value, at))
        elif key == "severity":
            if not isinstance(value, dict):
                raise ValueError(f"{at} must be a table of check = error, warning or off")
            for name, level in value.items():
                if name not in CHECKS:
                    raise ValueError(f"{at}: unknown check {name!r} (known: {', '.join(CHECKS)})")
                if level not in SEVERITIES:
                    raise ValueError(f"{at}.{name} must be one of {', '.join(SEVERITIES)}")
            out[key] = dict(value)
    return out


def read(path: Path) -> dict:
    """A policy file's table: JSON for a .json file, TOML otherwise."""
    text = Path(path).read_text(encoding="utf-8-sig")
    try:
        data = json.loads(text) if Path(path).suffix.lower() == ".json" else tomllib.loads(text)
    except (ValueError, tomllib.TOMLDecodeError) as error:
        raise ValueError(f"{Path(path).name}: {error}") from None
    return data


@dataclass
class Policy:
    levels: frozenset[int] | None = None
    mixin_version: str | None = None
    max_class_version: int | None = None
    required_fields: tuple[str, ...] = ()
    required_may_target_missing: bool = False
    injection_namespaces: frozenset[str] | None = None
    remapped_packages: tuple[str, ...] | None = None
    severity: dict[str, str] = field(default_factory=dict)
    sources: list[str] = field(default_factory=list)

    def level(self, check_name: str) -> str:
        """The severity of a check under this policy."""
        return self.severity.get(check_name, CHECKS[check_name][0])

    @property
    def version_parts(self) -> tuple[int, int, int, int] | None:
        return version(self.mixin_version.split("+", 1)[0].strip())[0] if self.mixin_version else None

    def remapped(self, name: str) -> bool:
        """Whether a refmap maps the names of class ``name`` (internal form) at runtime."""
        return self.remapped_packages is None or any(name.startswith(p) for p in self.remapped_packages)

    def as_json(self) -> dict:
        return {"compatibility_levels": span(self.levels) if self.levels is not None else None,
                "mixin_version": self.mixin_version, "max_class_version": self.max_class_version,
                "required_fields": list(self.required_fields),
                "required_may_target_missing": self.required_may_target_missing,
                "injection_namespaces": sorted(self.injection_namespaces) if self.injection_namespaces is not None
                else None,
                "remapped_packages": list(self.remapped_packages) if self.remapped_packages is not None else None,
                "severity": {name: self.level(name) for name in CHECKS}, "sources": self.sources}


def resolve(layers: list[tuple[str, dict]]) -> Policy:
    """The policy of ``(source, checked table)`` layers, each laid over the ones before it."""
    merged: dict = {}
    severity: dict[str, str] = {}
    for _, table in layers:
        severity.update(table.get("severity", {}))
        merged.update({k: v for k, v in table.items() if k != "severity"})
    return Policy(levels=merged.get("compatibility_levels"), mixin_version=merged.get("mixin_version"),
                  max_class_version=merged.get("max_class_version"),
                  required_fields=merged.get("required_fields", ()),
                  required_may_target_missing=merged.get("required_may_target_missing", False),
                  injection_namespaces=merged.get("injection_namespaces"),
                  remapped_packages=merged.get("remapped_packages"), severity=severity,
                  sources=[source for source, _ in layers])
