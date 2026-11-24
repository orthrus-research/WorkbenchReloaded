"""Read a Workbench config folder: a root ``workbench.toml`` plus TOML files in subdirectories.

A file's path is its namespace: ``modules/vast/rent.toml`` fills ``[modules.vast.rent]``. Files merge
in order of namespace depth and the deeper value wins, key by key; tables merge, scalars and arrays
replace. Every leaf value remembers the file and line that set it.

A machine's folder may lie over shared folders, its **layers**: ``[core.config] layers = [...]`` in the
folder's own ``workbench.toml`` lists them (relative to the folder or absolute), so a team keeps its
common config in one versioned folder and each machine adds only its paths and secrets. Layers merge
first, in the order listed, each the same way a folder does; the machine folder merges last and wins.
A layer cannot list layers of its own. In the container, where the host paths do not exist,
``WB_CONFIG_LAYERS`` names the mounted layers instead.

Standard library only: the client reads config to generate the compose file, and the daemon reads the
same folder and then checks it against the core's and the modules' schemas.
"""

from __future__ import annotations

import fnmatch
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROOT_FILE = "workbench.toml"
SKIP_DIRS = {"run", ".generated"}
LAYERS_ENV = "WB_CONFIG_LAYERS"


class ConfigError(Exception):
    """A config folder that cannot be read: the message names the file and, where known, the line."""


@dataclass
class Config:
    folder: Path | None
    values: dict = field(default_factory=dict)
    origins: dict[str, str] = field(default_factory=dict)  # "a.b.c" -> "path:line"
    layers: list[Path] = field(default_factory=list)       # shared folders under ``folder``, in merge order

    def get(self, dotted: str, default=None):
        node = self.values
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def table(self, dotted: str) -> dict:
        value = self.get(dotted, {})
        return value if isinstance(value, dict) else {}

    def origin(self, dotted: str) -> str | None:
        return self.origins.get(dotted)


def default_folder() -> Path:
    explicit = os.environ.get("WB_CONFIG")
    if explicit:
        return Path(explicit)
    return Path.home() / ".config" / "wb"


def namespace_of(folder: Path, path: Path) -> tuple[str, ...]:
    relative = path.relative_to(folder)
    if relative.as_posix() == ROOT_FILE:
        return ()
    parts = list(relative.parts)
    parts[-1] = parts[-1][: -len(".toml")]
    return tuple(parts)


def config_files(folder: Path, skip: tuple[Path, ...] = ()) -> list[Path]:
    found = []
    skipped = {p.resolve() for p in skip}
    for directory, subdirs, files in os.walk(folder):
        subdirs[:] = sorted(d for d in subdirs if not d.startswith(".") and not (
            Path(directory) == folder and d in SKIP_DIRS) and (Path(directory) / d).resolve() not in skipped)
        for name in sorted(files):
            if name.endswith(".toml") and not name.startswith("."):
                found.append(Path(directory) / name)
    return found


_HEADER = re.compile(r"^\s*\[\[?\s*([^\]]+?)\s*\]\]?\s*(#.*)?$")
_KEY = re.compile(r"^\s*([A-Za-z0-9_\-\.\"' ]+?)\s*=")


def _split_key(text: str) -> list[str]:
    parts, current, quote = [], "", None
    for char in text:
        if quote:
            if char == quote:
                quote = None
            else:
                current += char
        elif char in "\"'":
            quote = char
        elif char == ".":
            parts.append(current.strip())
            current = ""
        else:
            current += char
    parts.append(current.strip())
    return [p for p in parts if p != ""]


def key_lines(text: str) -> dict[tuple[str, ...], int]:
    """Line numbers of the keys a TOML text sets, by key path within the file. Best effort: values
    inside inline tables or multi-line arrays are attributed to the line their key starts on."""
    lines: dict[tuple[str, ...], int] = {}
    table: tuple[str, ...] = ()
    in_multiline = None
    for number, line in enumerate(text.splitlines(), start=1):
        if in_multiline:
            if in_multiline in line:
                in_multiline = None
            continue
        header = _HEADER.match(line)
        if header and not line.lstrip().startswith("#"):
            table = tuple(_split_key(header.group(1)))
            lines.setdefault(table, number)
            continue
        key = _KEY.match(line)
        if key and not line.lstrip().startswith("#"):
            path = table + tuple(_split_key(key.group(1)))
            lines.setdefault(path, number)
            for marker in ('"""', "'''"):
                rest = line.split("=", 1)[1]
                if rest.count(marker) == 1:
                    in_multiline = marker
    return lines


def _walk_leaves(value, prefix: tuple[str, ...]):
    if isinstance(value, dict) and value:
        for key, inner in value.items():
            yield from _walk_leaves(inner, prefix + (key,))
    else:
        yield prefix, value


def _origin_for(path: tuple[str, ...], lines: dict, display: str) -> str:
    probe = path
    while probe:
        if probe in lines:
            return f"{display}:{lines[probe]}"
        probe = probe[:-1]
    return display


def merge_into(target: dict, namespace: tuple[str, ...], data: dict) -> None:
    node = target
    for part in namespace:
        existing = node.get(part)
        if not isinstance(existing, dict):
            existing = {}
            node[part] = existing
        node = existing
    _deep_merge(node, data)


def _deep_merge(node: dict, data: dict) -> None:
    for key, value in data.items():
        if isinstance(value, dict) and isinstance(node.get(key), dict):
            _deep_merge(node[key], value)
        else:
            node[key] = value


def _listed_layers(data: dict):
    return ((data.get("core") or {}).get("config") or {}).get("layers")


def layer_folders(folder: Path) -> list[Path]:
    """The shared folders ``folder`` lies over: ``WB_CONFIG_LAYERS`` (os.pathsep-separated) when set, else
    ``[core.config] layers`` in the folder's own workbench.toml, relative paths from the folder."""
    explicit = os.environ.get(LAYERS_ENV)
    if explicit is not None:
        return [Path(p) for p in explicit.split(os.pathsep) if p]
    root = folder / ROOT_FILE
    if not root.is_file():
        return []
    try:
        listed = _listed_layers(tomllib.loads(root.read_text(encoding="utf-8")))
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{ROOT_FILE}: {error}") from None
    if listed is None:
        return []
    if not isinstance(listed, list) or not all(isinstance(entry, str) for entry in listed):
        raise ConfigError(f"{ROOT_FILE}: core.config.layers must be a list of folder paths")
    out = []
    for entry in listed:
        path = Path(os.path.expanduser(entry))
        out.append((path if path.is_absolute() else folder / path).resolve())
    return out


def ignored_patterns(folder: Path) -> list[str]:
    """Files in a config folder that hold other tools' settings rather than Workbench config: ``[core.config] ignore``
    in the folder's own workbench.toml, glob patterns relative to the folder. They travel with the folder (and its
    mount in the container) but are never read as config, so a tool can keep its machine settings beside ours."""
    root = folder / ROOT_FILE
    if not root.is_file():
        return []
    try:
        data = tomllib.loads(root.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{ROOT_FILE}: {error}") from None
    listed = ((data.get("core") or {}).get("config") or {}).get("ignore") or []
    if not isinstance(listed, list) or not all(isinstance(entry, str) for entry in listed):
        raise ConfigError(f"{ROOT_FILE}: core.config.ignore must be a list of file patterns")
    return listed


def _merge_folder(config: Config, folder: Path, prefix: str, *, layer: bool, skip: tuple[Path, ...] = ()):
    entries = []
    seen: dict[tuple[str, ...], Path] = {}
    ignore = ignored_patterns(folder)
    for path in config_files(folder, skip):
        relative = path.relative_to(folder).as_posix()
        if any(fnmatch.fnmatchcase(relative, pattern) for pattern in ignore):
            continue
        namespace = namespace_of(folder, path)
        folded = tuple(part.lower() for part in namespace)
        if folded in seen:
            raise ConfigError(f"{path} and {seen[folded]} both fill [{'.'.join(namespace) or 'root'}]")
        seen[folded] = path
        entries.append((len(namespace), prefix + path.relative_to(folder).as_posix(), namespace, path))
    entries.sort(key=lambda e: (e[0], e[1]))
    for _depth, display, namespace, path in entries:
        text = path.read_text(encoding="utf-8")
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as error:
            raise ConfigError(f"{display}: {error}") from None
        if layer and not namespace and _listed_layers(data) is not None:
            raise ConfigError(f"{display}: a layer cannot list layers; only the machine folder's "
                              f"{ROOT_FILE} does")
        merge_into(config.values, namespace, data)
        lines = key_lines(text)
        for leaf, _value in _walk_leaves(data, ()):
            config.origins[".".join(namespace + leaf)] = _origin_for(leaf, lines, display)


def load(folder: Path | str | None = None, *, required: bool = False) -> Config:
    """Merge a config folder over its layers. A missing folder is an empty config unless ``required``."""
    folder = Path(folder) if folder is not None else default_folder()
    if not folder.is_dir():
        if required:
            raise ConfigError(f"config folder {folder} does not exist")
        return Config(folder=None)
    layers = layer_folders(folder)
    config = Config(folder=folder, layers=layers)
    for layer in layers:
        if not layer.is_dir():
            raise ConfigError(f"config layer {layer} (core.config.layers) does not exist")
        _merge_folder(config, layer, f"{layer.name}/", layer=True)
    _merge_folder(config, folder, "", layer=False, skip=tuple(layers))
    return config


def root_mount(name: str, root: dict) -> str:
    """Where a [paths.roots] entry is mounted in the container: its ``container`` key, else ``/mnt/<name>``.
    The compose generator and the daemon both use this, so the mount and the translation always agree."""
    return root.get("container") or f"/mnt/{name}"


# [paths]: each folder named once, in host form; the container spelling is derived (/mnt/<root>).
#   out       the read-write root, mounted as the root named ``out``; the folders below live in it
#   releases  where wb deploy keeps releases (WB_RELEASES still wins, for tests and a second checkout)
#   backup    where wb backup copies what cannot be rebuilt
#   roots     the other roots the container may read (ro) or write (rw)
OUT_ROOT = "out"
# The folders Workbench keeps under [paths] out, by the [modules.ci] key that named each before (the pin cache was
# also [modules.toolchain] pin_cache): each is named once, by out.
OUT_FOLDERS = {"store": ("store",), "records": ("ci", "records"), "pin_cache": ("cache", "pins")}
# The keys each replaces, read for one more release with a warning when set.
OLD_FOLDER_KEYS = {"store": ("modules.ci.store",), "records": ("modules.ci.records",),
                   "pin_cache": ("modules.ci.pin_cache", "modules.toolchain.pin_cache")}


def roots(config: Config | None) -> dict[str, dict]:
    """[paths.roots], with [paths] out as the rw root named ``out``: each {host, mode, and container when the old
    spelling gave one}. Naming the out root twice is refused."""
    paths = config.table("paths") if config is not None else {}
    found = {name: dict(root) if isinstance(root, dict) else root for name, root in (paths.get("roots") or {}).items()}
    out = paths.get("out")
    if out:
        if OUT_ROOT in found:
            where = config.origin(f"paths.roots.{OUT_ROOT}.host") or config.origin("paths.out")
            raise ConfigError(f"{where + ': ' if where else ''}[paths] out and [paths.roots.{OUT_ROOT}] both name the "
                              f"out root; keep [paths] out and remove [paths.roots.{OUT_ROOT}]")
        found[OUT_ROOT] = {"host": str(out), "mode": "rw"}
    return found


def out_root(config: Config | None) -> str | None:
    """The out root as the host names it: [paths] out, else the old [paths.roots.out] host; None without one."""
    root = roots(config).get(OUT_ROOT)
    return str(root["host"]) if root and root.get("host") else None


def folders(config: Config | None, container: bool) -> dict[str, str]:
    """The folders under out by key (store, records, pin_cache), as the container (``container``) or the host names
    them. An old key that is set still wins for this release; a folder neither names is left out (its default is
    under the daemon's state)."""
    found: dict[str, str] = {}
    root = roots(config).get(OUT_ROOT)
    for key, parts in OUT_FOLDERS.items():
        old = next((str(config.get(name)) for name in OLD_FOLDER_KEYS[key] if config is not None
                    and config.get(name)), None)
        if old:
            found[key] = old
        elif root is not None and config is not None and config.get("paths.out"):
            base = root_mount(OUT_ROOT, root) if container else str(root["host"])
            found[key] = "/".join([base.rstrip("/"), *parts]) if container else os.path.join(base, *parts)
    return found


def deprecations(config: Config | None) -> list[str]:
    """The old path keys this config still sets, each with what replaced it: they are read, with this warning, for
    one more release."""
    if config is None:
        return []
    warnings = []

    def where(dotted: str) -> str:
        origin = config.origin(dotted)
        return f"{origin}: " if origin else ""

    for name, root in (config.table("paths").get("roots") or {}).items():
        if not isinstance(root, dict):
            continue
        if name == OUT_ROOT and not config.get("paths.out"):
            warnings.append(f"{where(f'paths.roots.{name}.host')}[paths.roots.{name}] is the old spelling of the out "
                            f"root: write [paths] out = {str(root.get('host', ''))!r} instead (read for one more "
                            "release)")
        if root.get("container"):
            warnings.append(f"{where(f'paths.roots.{name}.container')}paths.roots.{name}.container: container paths "
                            f"are derived (/mnt/{name}); remove the key (read for one more release)")
    for key, names in OLD_FOLDER_KEYS.items():
        for dotted in names:
            if config.get(dotted):
                below = "/".join(OUT_FOLDERS[key])
                warnings.append(f"{where(dotted)}{dotted}: this folder is <out>/{below}, named once by [paths] out; "
                                "remove the key (read for one more release)")
    return warnings


def flatten(values: dict, prefix: str = "") -> dict[str, object]:
    out = {}
    for key, value in values.items():
        dotted = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict) and value:
            out.update(flatten(value, dotted))
        else:
            out[dotted] = value
    return out
