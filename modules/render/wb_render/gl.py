"""A GL probe report and the gate a card must pass, for render.gl-gate.

The probe report (``wb/gl-probe/v1``, JSON) describes one GL context on one machine:

    {
      "schema": "wb/gl-probe/v1",
      "machine": "free text: a host name or a rental",           optional
      "os": "linux",                                             optional: linux, windows or macos
      "vendor": "NVIDIA Corporation",                            GL_VENDOR
      "renderer": "NVIDIA GeForce RTX 3060/PCIe/SSE2",           GL_RENDERER
      "version": "4.6.0 NVIDIA 580.95.05",                       GL_VERSION of the context the probe made
      "profile": "compatibility",                                that context's profile: compatibility or core
      "core_version": "4.6.0 NVIDIA 580.95.05",                  optional: the highest core profile version
      "memory_mib": 12288,                                       optional: dedicated video memory
      "driver": "580.95.05",                                     optional
      "extensions": ["GL_ARB_bindless_texture", "..."],         the context's extensions
      "limits": {"GL_MAX_TEXTURE_SIZE": "32768"}                 optional: implementation limits
    }

A ``glxinfo`` dump (``glxinfo``, ``glxinfo -B``, ``glxinfo -l``; a tag before each line, such as ``PROBE glx``, is
allowed) is read into the same report.

A gate (TOML or JSON, or ``[modules.render.gates.NAME]``):

    min_version = "4.6"                  # the lowest GL version accepted
    profile = "compatibility"            # compatibility, core or any
    vendors = ["NVIDIA"]                 # GL_VENDOR must contain one of these (no case); empty: any vendor
    min_memory_mib = 8192                # the least dedicated memory
    extensions = ["GL_ARB_bindless_texture", "GL_NV_mesh_shader|GL_EXT_mesh_shader"]   # each needed; a|b: either
    os_specific = ["GL_EXT_memory_object_win32", "..."]   # never required: present on one operating system only
"""

from __future__ import annotations

import json
import re
from pathlib import Path

SCHEMA = "wb/gl-probe/v1"
# Interop handles exist on one operating system only by design, so a gate never requires them.
OS_SPECIFIC = ["GL_EXT_memory_object_win32", "GL_EXT_semaphore_win32", "GL_EXT_memory_object_fd",
               "GL_EXT_semaphore_fd", "GL_WIN_swap_hint", "WGL_EXT_swap_control"]
GATE_KEYS = {"min_version", "profile", "vendors", "min_memory_mib", "extensions", "os_specific"}
PROFILES = ("compatibility", "core", "any")
EXTENSION = re.compile(r"\b(?:GL|WGL)_[A-Za-z0-9]+_\w+\b")
LIMIT = re.compile(r"\b(GL_MAX_[A-Z0-9_]+|GL_[A-Z0-9_]+_ALIGNMENT)\s*=\s*(.+?)\s*$")


def version_of(text: str | None) -> tuple[int, int] | None:
    match = re.search(r"(\d+)\.(\d+)", text or "")
    return (int(match[1]), int(match[2])) if match else None


def _after(line: str, key: str) -> str | None:
    index = line.find(key)
    return line[index + len(key):].strip() if index >= 0 else None


def from_glxinfo(text: str) -> dict:
    report: dict = {"schema": SCHEMA, "extensions": [], "limits": {}, "source": "glxinfo"}
    extensions: list[str] = []
    in_extensions = False
    for raw in text.splitlines():
        line = raw.rstrip()
        fields = [("OpenGL vendor string:", "vendor"), ("OpenGL renderer string:", "renderer"),
                  ("OpenGL core profile version string:", "core_version"), ("OpenGL version string:", "version"),
                  ("OpenGL profile mask:", "profile")]
        matched = False
        for key, name in fields:
            value = _after(line, key)
            if value is not None:
                if name == "profile":
                    value = "compatibility" if "compatibility" in value else "core" if "core" in value else value
                report.setdefault(name, value)
                matched = True
                break
        if matched:
            in_extensions = False
            continue
        memory = _after(line, "Dedicated video memory:")
        if memory is None and "memory_mib" not in report:
            memory = _after(line, "Video memory:")
        if memory is not None:
            found = re.search(r"(\d+)\s*MB", memory)
            if found and "memory_mib" not in report:
                report["memory_mib"] = int(found[1])
            continue
        if re.search(r"OpenGL (?:core profile |ES profile )?extensions:", line):
            # the ES context's extensions are not the desktop context's
            in_extensions = "ES profile" not in line
            continue
        if line.rstrip().endswith(":") or "string:" in line:
            in_extensions = False
        limit = LIMIT.search(line)
        if limit:
            report["limits"][limit[1]] = limit[2]
            continue
        if in_extensions:
            extensions += [name for name in EXTENSION.findall(line) if not name.startswith("GLX_")]
    report["extensions"] = sorted(set(extensions))
    if not report["limits"]:
        del report["limits"]
    return report


def read_report(path: Path) -> dict:
    """A probe report from JSON, or from a glxinfo dump. Raises ValueError when it is neither."""
    text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    if text.lstrip().startswith("{"):
        try:
            report = json.loads(text)
        except ValueError as error:
            raise ValueError(f"{Path(path).name}: not valid JSON: {error}") from None
        if report.get("schema", SCHEMA) != SCHEMA:
            raise ValueError(f"{Path(path).name}: schema {report.get('schema')!r}, expected {SCHEMA}")
    elif "OpenGL" in text:
        report = from_glxinfo(text)
    else:
        raise ValueError(f"{Path(path).name} is neither a {SCHEMA} report nor a glxinfo dump")
    if not isinstance(report.get("version"), str) or not report["version"]:
        raise ValueError(f"{Path(path).name}: the report names no GL version")
    extensions = report.get("extensions", [])
    if not isinstance(extensions, list) or not all(isinstance(e, str) for e in extensions):
        raise ValueError(f"{Path(path).name}: extensions must be a list of names")
    report["extensions"] = sorted(set(extensions))
    if "memory_mib" in report and not isinstance(report["memory_mib"], (int, float)):
        raise ValueError(f"{Path(path).name}: memory_mib must be a number")
    return report


def check_gate(gate: dict, where: str) -> dict:
    unknown = set(gate) - GATE_KEYS
    if unknown:
        raise ValueError(f"{where}: unknown keys {sorted(unknown)} (known: {', '.join(sorted(GATE_KEYS))})")
    if "min_version" in gate and version_of(str(gate["min_version"])) is None:
        raise ValueError(f"{where}: min_version must look like 4.6")
    if "profile" in gate and gate["profile"] not in PROFILES:
        raise ValueError(f"{where}: profile must be one of {', '.join(PROFILES)}")
    for key in ("vendors", "extensions", "os_specific"):
        if key in gate and (not isinstance(gate[key], list) or not all(isinstance(v, str) for v in gate[key])):
            raise ValueError(f"{where}: {key} must be a list of strings")
    if "min_memory_mib" in gate and (isinstance(gate["min_memory_mib"], bool)
                                     or not isinstance(gate["min_memory_mib"], (int, float))):
        raise ValueError(f"{where}: min_memory_mib must be a number")
    return gate


def judge(report: dict, gate: dict) -> list[dict]:
    """Each check of the gate: {check, passed, measured, required}."""
    checks = []
    have = set(report.get("extensions", []))
    profile = gate.get("profile", "any")
    if "min_version" in gate:
        wanted = version_of(str(gate["min_version"]))
        text = report.get("core_version") if profile == "core" and report.get("core_version") else report.get("version")
        got = version_of(text)
        es = "OpenGL ES" in (text or "")
        checks.append({"check": f"OpenGL {wanted[0]}.{wanted[1]} or later", "passed": bool(got and not es and got >= wanted),
                       "measured": text, "required": gate["min_version"]})
    if profile != "any":
        if profile == "core":
            passed = bool(report.get("core_version")) or report.get("profile") == "core"
        else:
            passed = report.get("profile") == "compatibility"
        checks.append({"check": f"a {profile} profile context", "passed": passed,
                       "measured": report.get("profile"), "required": profile})
    if gate.get("vendors"):
        vendor = report.get("vendor") or ""
        checks.append({"check": "vendor " + " or ".join(gate["vendors"]),
                       "passed": any(v.lower() in vendor.lower() for v in gate["vendors"]),
                       "measured": vendor, "required": gate["vendors"]})
    if gate.get("min_memory_mib"):
        memory = report.get("memory_mib")
        checks.append({"check": f"{gate['min_memory_mib']:,} MiB or more", "passed": memory is not None
                       and memory >= gate["min_memory_mib"], "measured": memory, "required": gate["min_memory_mib"]})
    skipped = set(gate.get("os_specific", OS_SPECIFIC))
    for entry in gate.get("extensions", []):
        alternatives = [name.strip() for name in entry.split("|") if name.strip()]
        wanted = [name for name in alternatives if name not in skipped]
        if not wanted:
            continue
        present = [name for name in wanted if name in have]
        checks.append({"check": " or ".join(wanted), "passed": bool(present), "measured": present or None,
                       "required": wanted})
    return checks


def compare(report: dict, reference: dict, gate: dict) -> dict:
    """How the report differs from a reference card's: the gate's extensions first, then every other."""
    skipped = set(gate.get("os_specific", OS_SPECIFIC))
    named = [n.strip() for entry in gate.get("extensions", []) for n in entry.split("|") if n.strip()]
    have, ref = set(report.get("extensions", [])), set(reference.get("extensions", []))
    limits, ref_limits = report.get("limits") or {}, reference.get("limits") or {}
    return {
        "reference": {k: reference.get(k) for k in ("machine", "vendor", "renderer", "version") if reference.get(k)},
        "named_missing": [n for n in named if n in ref and n not in have and n not in skipped],
        "named_extra": [n for n in named if n not in ref and n in have and n not in skipped],
        "other_missing": sorted(e for e in ref - have if e not in named and e not in skipped),
        "other_extra": sorted(e for e in have - ref if e not in named and e not in skipped),
        "limits_differ": {k: {"reference": ref_limits[k], "here": v} for k, v in sorted(limits.items())
                          if k in ref_limits and ref_limits[k] != v},
    }
