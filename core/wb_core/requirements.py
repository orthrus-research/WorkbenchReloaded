"""Requirement checks (D8). Static kinds decide at startup whether a command is active; volatile kinds
(network, disk, host) are reported at startup but only fail a job when checked again just before it runs.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from .manifest import VOLATILE, Command, Module
from .settings import Settings

_JDK_NAME = re.compile(r"^(?:jdk-?|temurin-?|zulu-?)?(\d+)(?:[.\-+_][\w.+\-]*)?$", re.I)


def find_jdks(settings: Settings) -> dict[str, str]:
    """JDKs by key: "26", "25", "8", "27-ea", plus exact folder names. Looks in [core.jdks].root, in
    ``WB_JDKS`` (a JSON object of key -> home) and, outside a container, in ``~/.jdks``."""
    found: dict[str, str] = {}
    roots = [Path(settings.core["jdks"]["root"])]
    if not Path("/.dockerenv").exists():
        roots.append(Path.home() / ".jdks")
    for root in roots:
        if not root.is_dir():
            continue
        for home in sorted(root.iterdir()):
            java = home / "bin" / ("java.exe" if os.name == "nt" else "java")
            if not java.exists():
                continue
            name = home.name
            found.setdefault(name, str(home))
            match = _JDK_NAME.match(name)
            if match:
                key = match.group(1) + ("-ea" if "ea" in name.lower().split(match.group(1), 1)[1] else "")
                found.setdefault(key, str(home))
    extra = os.environ.get("WB_JDKS")
    if extra:
        found.update(json.loads(extra))
    return found


@dataclass
class Reason:
    kind: str
    name: str
    detail: str
    fix: str = ""

    def as_dict(self):
        return {"kind": self.kind, "name": self.name, "detail": self.detail, "fix": self.fix}


@dataclass
class Activation:
    command: Command
    active: bool
    reasons: list[Reason] = field(default_factory=list)
    warnings: list[Reason] = field(default_factory=list)


def merged_requires(module: Module, command: Command) -> dict:
    merged: dict = {}
    for source in (module.requires, command.requires):
        for kind, value in source.items():
            if isinstance(value, list):
                merged.setdefault(kind, [])
                merged[kind] = list(dict.fromkeys(merged[kind] + value))
            else:
                merged[kind] = value
    return merged


class Checker:
    def __init__(self, settings: Settings, jdks: dict[str, str] | None = None, host_probe=None,
                 active_modules: set[str] | None = None):
        self.settings = settings
        self.jdks = jdks if jdks is not None else find_jdks(settings)
        self.host_probe = host_probe
        self.active_modules = active_modules or set()

    def check(self, kind: str, value, module: Module) -> list[Reason]:
        method = getattr(self, "_" + kind)
        return method(value, module)

    def _secrets(self, names, module):
        reasons = []
        for name in names:
            path = self.settings.secret_path(name)
            if path is None or not path.is_file():
                reasons.append(Reason("secrets", name, f"secret {name} is not mounted",
                                      f"add [secrets.{name}] host = '<file>' to the config and restart"))
        return reasons

    def _binary(self, names, module):
        return [Reason("binary", n, f"{n} is not on PATH", f"install {n} in the image") for n in names
                if shutil.which(n) is None]

    def _jdk(self, versions, module):
        return [Reason("jdk", str(v), f"JDK {v} is not available", "add it to image/pins.toml")
                for v in versions if str(v) not in self.jdks]

    def _config(self, keys, module):
        reasons = []
        for key in keys:
            value = self.settings.config.get(key if "." in key else f"modules.{module.name}.{key}")
            if value in (None, "", [], {}):
                reasons.append(Reason("config", key, f"config key {key} is not set", f"set {key} in the config"))
        return reasons

    def _module(self, names, module):
        return [Reason("module", n, f"module {n} is not active", f"see `wb ls {n}`") for n in names
                if n not in self.active_modules]

    def _network(self, targets, module):
        reasons = []
        for target in targets:
            host, _, port = str(target).rpartition(":")
            try:
                with socket.create_connection((host or target, int(port or 443)), timeout=3):
                    pass
            except OSError as error:
                reasons.append(Reason("network", str(target), f"{target} is not reachable: {error}",
                                      "check the network and the container's DNS"))
        return reasons

    def _disk(self, gigabytes, module):
        folder = self.settings.state_dir
        folder.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(folder).free / 1e9
        if free < float(gigabytes):
            return [Reason("disk", f"{gigabytes} GB", f"{free:.1f} GB free in the state volume",
                           "run `wb prune` or free space")]
        return []

    def _host(self, value, module):
        if not self.settings.core["host"]["enabled"]:
            return [Reason("host", "wb-host", "the host runner is disabled", "set core.host.enabled = true")]
        probe = self.host_probe or probe_host
        error = probe(self.settings)
        return [Reason("host", "wb-host", error, "start it with `wb host serve` on Windows")] if error else []

    def _lease(self, value, module):
        return []

    def activation(self, module: Module, command: Command, *, volatile: bool) -> Activation:
        reasons, warnings = [], []
        requires = merged_requires(module, command)
        if "host" in command.runs_on and command.runs_on == ["host"]:
            requires.setdefault("host", True)
        for kind, value in requires.items():
            if (kind in VOLATILE) != volatile and kind in VOLATILE:
                continue
            found = self.check(kind, value, module)
            if kind in VOLATILE:
                warnings.extend(found)
            else:
                reasons.extend(found)
        return Activation(command=command, active=not reasons, reasons=reasons, warnings=warnings)

    def volatile(self, module: Module, command: Command) -> list[Reason]:
        requires = merged_requires(module, command)
        if command.runs_on == ["host"]:
            requires.setdefault("host", True)
        out = []
        for kind in ("network", "disk", "host"):
            if kind in requires:
                out.extend(self.check(kind, requires[kind], module))
        return out


def probe_host(settings: Settings) -> str | None:
    """None when wb-host answers its health check with the token; else why not."""
    url = settings.core["host"]["url"].rstrip("/") + "/v1/health"
    token_path = settings.secret_path(settings.core["host"]["token_secret"])
    request = urllib.request.Request(url)
    if token_path and token_path.is_file():
        request.add_header("Authorization", "Bearer " + token_path.read_text(encoding="ascii").strip())
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            json.loads(response.read() or b"{}")
        return None
    except Exception as error:  # noqa: BLE001
        return f"wb-host does not answer at {url}: {error}"
