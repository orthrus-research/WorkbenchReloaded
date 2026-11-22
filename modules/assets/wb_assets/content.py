"""Content assets from one manifest of blocks, items and families, for assets.generate.

The manifest (TOML or JSON) names a namespace and what to make under it:

    namespace = "examplemod"
    keep = ["blockstates/hand_made_*.json"]       # files in the managed folders the manifest does not own

    [lang]                                        # optional
    file = "en_us.lang"                           # .lang (key=value lines) or .json
    block = "tile.{namespace}.{id}.name"          # the key of a block's name
    item = "item.{namespace}.{id}.name"           # the key of an item's name
    entries = { "itemGroup.examplemod" = "Example Mod" }   # more entries, as written

    [[block]]
    id = "basalt"
    name = "Basalt"
    shape = "cube"                                # see SHAPES; cube when not given
    textures = { all = "blocks/basalt" }          # a texture without a namespace is the manifest's

    [[block]]
    id = "rock"
    textures = { all = "blocks/rock_{n}" }
    alternates = 3                                # three models, one chosen by position; {n} is 0, 1, 2

    [[block]]
    id = "spike"
    shape = "cross"
    properties = { direction = ["up", "down"], size = ["small", "large"] }
    textures = { cross = "blocks/spike_{direction}_{size}" }   # a property in a texture: a model per value

    [[item]]
    id = "seed"
    name = "Seed"
    textures = { layer0 = "items/seed" }          # parent = "item/generated" unless given

    [[family]]                                    # blocks and items made for every subject
    subjects = { ash = "Ash", elm = { name = "Elm", grain = "rough" } }   # id = name, or a table of name and more
    [[family.block]]
    id = "log_{subject}"                          # {subject}, {name} and a subject's other keys fill every string
    name = "{name} Log"
    shape = "log"
    textures = { side = "blocks/{subject}_bark", end = "blocks/{subject}_end" }

Shapes and the textures they take:

    cube     all           block/cube_all
    column   side, end     block/cube_column
    log      side, end     block/cube_column on axis=x|y|z (turned), the side all round on axis=none
    cross    cross         block/cross; the item is drawn from the texture
    plane    surface       a flat face at `height` (default 0.1) seen from above and below; the item is drawn
    model    any           `parent` (e.g. block/cube_bottom_top) with the textures given

A block's item model is its block model (``item = "block"``, the default for solid shapes), drawn from a texture
(``item = "generated"``, the default for cross and plane, or ``item = { layer0 = "..." }``), or none
(``item = "none"``). Blockstates are vanilla variants keyed by properties in name order, as the game names a state.
The manifest owns ``blockstates/``, ``models/block/`` and ``models/item/`` under its namespace, and its lang file.
"""

from __future__ import annotations

import fnmatch
import itertools
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

SHAPES = {"cube": ("all",), "column": ("side", "end"), "log": ("side", "end"), "cross": ("cross",),
          "plane": ("surface",), "model": ()}
PARENTS = {"cube": "block/cube_all", "column": "block/cube_column", "log": "block/cube_column", "cross": "block/cross"}
DRAWN = {"cross": "cross", "plane": "surface"}  # shapes whose item is drawn from this texture
TOP_KEYS = {"namespace", "lang", "keep", "block", "item", "family"}
LANG_KEYS = {"file", "block", "item", "entries"}
BLOCK_KEYS = {"id", "name", "shape", "textures", "properties", "alternates", "item", "parent", "height"}
ITEM_KEYS = {"id", "name", "textures", "parent"}
FAMILY_KEYS = {"subjects", "block", "item"}
ID = re.compile(r"^[a-z0-9_.-]+$")
PROPERTY = re.compile(r"^[a-z0-9_]+$")
PLACEHOLDER = re.compile(r"\{(\w+)\}")
AXES = {"x": {"x": 90, "y": 90}, "y": {}, "z": {"x": 90}}
OWNED = ("blockstates/", "models/block/", "models/item/")
TEXT = (".json", ".lang", ".mcmeta", ".txt")


class ManifestError(ValueError):
    pass


@dataclass
class Manifest:
    namespace: str
    blocks: list[dict] = field(default_factory=list)
    items: list[dict] = field(default_factory=list)
    lang_file: str = "en_us.lang"
    lang_block: str = "tile.{namespace}.{id}.name"
    lang_item: str = "item.{namespace}.{id}.name"
    lang_entries: dict = field(default_factory=dict)
    keep: list[str] = field(default_factory=list)

    @property
    def base(self) -> str:
        return f"assets/{self.namespace}"

    def owns(self, relative: str, lang: bool) -> bool:
        """Whether a file (relative to the folder holding assets/) is the manifest's to make or remove."""
        if not relative.startswith(self.base + "/"):
            return False
        inner = relative[len(self.base) + 1:]
        if any(fnmatch.fnmatchcase(inner, pattern) for pattern in self.keep):
            return False
        return inner.startswith(OWNED) or (lang and inner == f"lang/{self.lang_file}")


def fill(text: str, values: dict, where: str, strict: bool = True) -> str:
    def replace(match):
        if match[1] in values:
            return str(values[match[1]])
        if strict:
            raise ManifestError(f"{where}: {{{match[1]}}} names nothing")
        return match[0]
    return PLACEHOLDER.sub(replace, text)


def _filled(value, values: dict, where: str):
    """A family member with the subject's values filled into every string; other placeholders are kept."""
    if isinstance(value, str):
        return fill(value, values, where, strict=False)
    if isinstance(value, list):
        return [_filled(v, values, where) for v in value]
    if isinstance(value, dict):
        return {k: _filled(v, values, where) for k, v in value.items()}
    return value


def _table(value, where: str) -> dict:
    if not isinstance(value, dict):
        raise ManifestError(f"{where} must be a table")
    return value


def _unknown(table: dict, known: set, where: str):
    extra = set(table) - known
    if extra:
        raise ManifestError(f"{where}: unknown keys {sorted(extra)} (known: {', '.join(sorted(known))})")


def _texture(value, where: str) -> str:
    if not isinstance(value, str) or not value or value.startswith("#"):
        raise ManifestError(f"{where} must name a texture")
    return value


def _check_block(block: dict, where: str) -> dict:
    _unknown(block, BLOCK_KEYS, where)
    block_id = block.get("id")
    if not isinstance(block_id, str) or not ID.match(block_id):
        raise ManifestError(f"{where}: id must be lowercase letters, digits, '_', '.' or '-' (got {block_id!r})")
    where = f"block {block_id}"
    shape = block.setdefault("shape", "cube")
    if shape not in SHAPES:
        raise ManifestError(f"{where}: shape must be one of {', '.join(SHAPES)}")
    if "name" in block and (not isinstance(block["name"], str) or PLACEHOLDER.search(block["name"])):
        raise ManifestError(f"{where}: name must be text with nothing left to fill")
    textures = _table(block.get("textures", {}), f"{where}.textures")
    if shape == "model":
        if not isinstance(block.get("parent"), str) or not block["parent"]:
            raise ManifestError(f"{where}: the model shape needs parent (e.g. block/cube_bottom_top)")
        if not textures:
            raise ManifestError(f"{where}: the model shape needs textures")
    else:
        wanted = set(SHAPES[shape])
        if set(textures) != wanted:
            raise ManifestError(f"{where}: {shape} takes the textures {', '.join(SHAPES[shape])} "
                                f"(got {', '.join(sorted(textures)) or 'none'})")
        if "parent" in block:
            raise ManifestError(f"{where}: parent belongs to the model shape")
    for variable, value in textures.items():
        _texture(value, f"{where}.textures.{variable}")
    properties = _table(block.get("properties", {}), f"{where}.properties")
    clean = {}
    for name, values in properties.items():
        if not PROPERTY.match(name) or (shape == "log" and name == "axis"):
            raise ManifestError(f"{where}: property {name!r} is not allowed here" if name == "axis" else
                                f"{where}: a property name is lowercase letters, digits and '_'")
        if not isinstance(values, list) or not values:
            raise ManifestError(f"{where}.properties.{name} must list its values")
        clean[name] = [("true" if v else "false") if isinstance(v, bool) else str(v) for v in values]
        if len(set(clean[name])) != len(clean[name]):
            raise ManifestError(f"{where}.properties.{name} lists a value twice")
    block["properties"] = clean
    alternates = block.setdefault("alternates", 1)
    if isinstance(alternates, bool) or not isinstance(alternates, int) or not 1 <= alternates <= 64:
        raise ManifestError(f"{where}: alternates must be a whole number from 1 to 64")
    if alternates > 1 and not any("{n}" in t for t in textures.values()):
        raise ManifestError(f"{where}: {alternates} alternates need {{n}} in a texture, or they are all the same")
    known = {"n", *clean}
    for variable, value in textures.items():
        for name in PLACEHOLDER.findall(value):
            if name not in known:
                raise ManifestError(f"{where}.textures.{variable}: {{{name}}} names nothing")
    item = block.get("item", "generated" if shape in DRAWN else "block")
    if isinstance(item, dict):
        _unknown(item, {"parent", "layer0", "layer1", "layer2", "layer3", "layer4"}, f"{where}.item")
        if not any(k.startswith("layer") for k in item):
            raise ManifestError(f"{where}.item needs layer0")
    elif item not in ("block", "generated", "none"):
        raise ManifestError(f"{where}: item must be block, generated, none, or a table of layers")
    if item == "generated" and shape not in DRAWN:
        raise ManifestError(f"{where}: item = generated draws from a texture; give item = {{ layer0 = ... }}")
    block["item"] = item
    if "height" in block:
        if shape != "plane":
            raise ManifestError(f"{where}: height belongs to the plane shape")
        if isinstance(block["height"], bool) or not isinstance(block["height"], (int, float)) \
                or not 0 <= block["height"] <= 16:
            raise ManifestError(f"{where}: height must be from 0 to 16")
    return block


def _check_item(item: dict, where: str) -> dict:
    _unknown(item, ITEM_KEYS, where)
    item_id = item.get("id")
    if not isinstance(item_id, str) or not ID.match(item_id):
        raise ManifestError(f"{where}: id must be lowercase letters, digits, '_', '.' or '-' (got {item_id!r})")
    where = f"item {item_id}"
    textures = _table(item.get("textures", {}), f"{where}.textures")
    if not textures:
        raise ManifestError(f"{where} needs textures (layer0 = ...)")
    for variable, value in textures.items():
        if PLACEHOLDER.search(_texture(value, f"{where}.textures.{variable}")):
            raise ManifestError(f"{where}.textures.{variable}: nothing fills {value!r}")
    if "name" in item and (not isinstance(item["name"], str) or PLACEHOLDER.search(item["name"])):
        raise ManifestError(f"{where}: name must be text with nothing left to fill")
    if "parent" in item and not isinstance(item["parent"], str):
        raise ManifestError(f"{where}: parent must name a model")
    return item


def load(data: dict) -> Manifest:
    """The manifest checked and its families expanded. Raises ManifestError."""
    _table(data, "the manifest")
    _unknown(data, TOP_KEYS, "the manifest")
    namespace = data.get("namespace")
    if not isinstance(namespace, str) or not ID.match(namespace):
        raise ManifestError("namespace must be the mod id: lowercase letters, digits, '_', '.' or '-'")
    lang = _table(data.get("lang", {}), "lang")
    _unknown(lang, LANG_KEYS, "lang")
    manifest = Manifest(namespace, lang_file=lang.get("file", "en_us.lang"),
                        lang_block=lang.get("block", "tile.{namespace}.{id}.name"),
                        lang_item=lang.get("item", "item.{namespace}.{id}.name"),
                        lang_entries=dict(_table(lang.get("entries", {}), "lang.entries")))
    if not re.fullmatch(r"[A-Za-z0-9_]+\.(lang|json)", manifest.lang_file):
        raise ManifestError("lang.file must be a file name ending .lang or .json (e.g. en_us.lang)")
    keep = data.get("keep", [])
    if not isinstance(keep, list) or not all(isinstance(k, str) for k in keep):
        raise ManifestError("keep must be a list of glob patterns")
    manifest.keep = keep
    blocks, items = list(data.get("block", [])), list(data.get("item", []))
    for index, family in enumerate(data.get("family", [])):
        where = f"family {index + 1}"
        _table(family, where)
        _unknown(family, FAMILY_KEYS, where)
        subjects = _table(family.get("subjects"), f"{where}.subjects") if "subjects" in family else {}
        if not subjects:
            raise ManifestError(f"{where} needs subjects")
        if not family.get("block") and not family.get("item"):
            raise ManifestError(f"{where} makes nothing: give [[family.block]] or [[family.item]]")
        for subject, value in subjects.items():
            values = {"name": value} if isinstance(value, str) else dict(_table(value, f"{where}.subjects.{subject}"))
            if not isinstance(values.get("name"), str):
                raise ManifestError(f"{where}.subjects.{subject} needs a name")
            values["subject"] = subject
            blocks += [_filled(dict(member), values, where) for member in family.get("block", [])]
            items += [_filled(dict(member), values, where) for member in family.get("item", [])]
    manifest.blocks = [_check_block(dict(b), f"block {i + 1}") for i, b in enumerate(blocks)]
    manifest.items = [_check_item(dict(t), f"item {i + 1}") for i, t in enumerate(items)]
    for kind, entries in (("block", manifest.blocks), ("item", manifest.items)):
        seen: set[str] = set()
        for entry in entries:
            if entry["id"] in seen:
                raise ManifestError(f"{kind} {entry['id']} is defined twice")
            seen.add(entry["id"])
    return manifest


def _ref(namespace: str, texture: str) -> str:
    return texture if ":" in texture else f"{namespace}:{texture}"


def _text(data) -> bytes:
    return (json.dumps(data, indent=2) + "\n").encode("utf-8")


def _model(shape: str, textures: dict, block: dict) -> dict:
    if shape == "model":
        return {"parent": block["parent"], "textures": textures}
    if shape == "plane":
        height = block.get("height", 0.1)
        face = {"uv": [0, 0, 16, 16], "texture": "#surface"}
        return {"ambientocclusion": False, "textures": {"particle": textures["surface"], "surface": textures["surface"]},
                "elements": [{"from": [0, height, 0], "to": [16, height, 16], "faces": {"up": face, "down": face}}]}
    return {"parent": PARENTS[shape], "textures": textures}


def block_assets(namespace: str, block: dict) -> tuple[dict, dict[str, dict], dict | None]:
    """A block's blockstate, its block models by name, and its item model."""
    block_id, shape, textures = block["id"], block["shape"], block["textures"]
    properties, alternates = block["properties"], block["alternates"]
    names = sorted(properties)
    varying = [p for p in names if any("{" + p + "}" in t for t in textures.values())]
    models: dict[str, dict] = {}
    variants: dict[str, list[dict]] = {}
    first: tuple[str, dict] | None = None
    for combination in itertools.product(*(properties[p] for p in names)):
        values = dict(zip(names, combination))
        stem = block_id + "".join(f"_{values[p]}" for p in varying)
        entries = []
        for n in range(alternates):
            name = stem + (f"_{n}" if alternates > 1 else "")
            filled = {var: _ref(namespace, fill(t, {**values, "n": n}, f"block {block_id}")) for var, t in textures.items()}
            if name not in models:
                models[name] = _model(shape, filled, block)
                if shape == "log":
                    models[f"{name}_bark"] = {"parent": "block/cube_all", "textures": {"all": filled["side"]}}
            first = first or (name, filled)
            entries.append(name)
        if shape == "log":
            for axis in ("x", "y", "z", "none"):
                keyed = {**values, "axis": axis}
                key = ",".join(f"{p}={keyed[p]}" for p in sorted(keyed))
                variants[key] = [{"model": f"{namespace}:{e}_bark"} if axis == "none" else
                                 {"model": f"{namespace}:{e}", **AXES[axis]} for e in entries]
        else:
            variants[",".join(f"{p}={values[p]}" for p in names) or "normal"] = [{"model": f"{namespace}:{e}"}
                                                                                 for e in entries]
    state = {"variants": {key: entries[0] if len(entries) == 1 else entries for key, entries in variants.items()}}
    model_name, filled = first
    item = block["item"]
    if item == "none":
        item_model = None
    elif item == "block":
        item_model = {"parent": f"{namespace}:block/{model_name}"}
    elif item == "generated":
        item_model = {"parent": "item/generated", "textures": {"layer0": filled[DRAWN[shape]]}}
    else:
        item_model = {"parent": item.get("parent", "item/generated"),
                      "textures": {k: _ref(namespace, _texture(v, f"block {block_id}.item.{k}"))
                                   for k, v in sorted(item.items()) if k.startswith("layer")}}
    return state, models, item_model


def render(manifest: Manifest) -> dict[str, bytes]:
    """Every file the manifest makes, by path relative to the folder holding assets/."""
    namespace, base = manifest.namespace, manifest.base
    files: dict[str, bytes] = {}
    lang: dict[str, str] = {}
    made_by: dict[str, str] = {}

    def put(path: str, data, owner: str):
        if path in files:
            raise ManifestError(f"{owner} and {made_by[path]} both make {path}")
        files[path], made_by[path] = _text(data), owner

    for block in manifest.blocks:
        state, models, item = block_assets(namespace, block)
        owner = f"block {block['id']}"
        put(f"{base}/blockstates/{block['id']}.json", state, owner)
        for name, model in models.items():
            put(f"{base}/models/block/{name}.json", model, owner)
        if item is not None:
            put(f"{base}/models/item/{block['id']}.json", item, owner)
        if block.get("name"):
            lang[fill(manifest.lang_block, {"namespace": namespace, "id": block["id"]}, "lang.block")] = block["name"]
    for item in manifest.items:
        owner = f"item {item['id']}"
        put(f"{base}/models/item/{item['id']}.json",
            {"parent": item.get("parent", "item/generated"),
             "textures": {k: _ref(namespace, v) for k, v in item["textures"].items()}}, owner)
        if item.get("name"):
            lang[fill(manifest.lang_item, {"namespace": namespace, "id": item["id"]}, "lang.item")] = item["name"]
    lang.update({str(k): str(v) for k, v in manifest.lang_entries.items()})
    if lang:
        ordered = dict(sorted(lang.items()))
        path = f"{base}/lang/{manifest.lang_file}"
        files[path] = ((json.dumps(ordered, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
                       if manifest.lang_file.endswith(".json") else
                       "".join(f"{k}={v}\n" for k, v in ordered.items()).encode("utf-8"))
    return files


def _same(path: Path, data: bytes) -> bool:
    have = path.read_bytes()
    if path.suffix.lower() in TEXT:  # a checkout with CRLF line ends still matches
        return have.replace(b"\r\n", b"\n") == data.replace(b"\r\n", b"\n")
    return have == data


def present(manifest: Manifest, root: Path, lang: bool) -> set[str]:
    folder = root / manifest.base
    if not folder.is_dir():
        return set()
    return {relative for path in folder.rglob("*") if path.is_file()
            and manifest.owns(relative := path.relative_to(root).as_posix(), lang)}


def compare(manifest: Manifest, files: dict[str, bytes], root: Path) -> dict[str, list[str]]:
    lang = any(path.startswith(f"{manifest.base}/lang/") for path in files)
    missing = sorted(path for path in files if not (root / path).is_file())
    stale = sorted(path for path in files if (root / path).is_file() and not _same(root / path, files[path]))
    orphaned = sorted(present(manifest, root, lang) - files.keys())
    return {"stale": stale, "missing": missing, "orphaned": orphaned}


def write(manifest: Manifest, files: dict[str, bytes], root: Path) -> dict[str, list[str]]:
    """Writes what differs and removes what the manifest owns but no longer makes."""
    lang = any(path.startswith(f"{manifest.base}/lang/") for path in files)
    written, unchanged = [], []
    for path, data in sorted(files.items()):
        target = root / path
        if target.is_file() and target.read_bytes() == data:
            unchanged.append(path)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        written.append(path)
    removed = sorted(present(manifest, root, lang) - files.keys())
    for path in removed:
        (root / path).unlink()
    return {"written": written, "unchanged": unchanged, "removed": removed}
