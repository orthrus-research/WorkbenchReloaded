"""Reference blocks for a resource pack, and a mod's blocks sorted by shape, for assets.blockstates.

From the vanilla 1.12.2 client JAR (and a mod's resources, for a reference in its namespace), each reference block's
blockstate, block models and item model are copied into the pack's namespace under the block's new name. Vanilla
geometry is inherited, not copied: a copied model is a child of the vanilla model that names its textures. Every
distinct texture becomes one texture slot: ``blocks/<name>`` when the block has one, else ``blocks/<name>_<variable>``,
alternates of one variable told apart by their model. The pack is complete but for one PNG per slot at
``assets/<namespace>/textures/<slot>.png``, and ``slots.json`` lists every slot with what it must be.

A reference is ``ref``, ``name=ref`` or ``name=ref[property=value,...]`` (a property kept at one value and dropped, as a
slab keeps half=bottom); ``ref`` is a vanilla block (``oak_stairs``) or ``namespace:block``. A catalog (TOML or JSON)
groups references into types:

    [types.stairs]
    about = "a stair block"                         # other plain keys are kept in slots.json too
    roles = { "*" = { owner = "material" }, side = { tint = true } }   # per texture variable; see ROLE
    [[types.stairs.blocks]]
    name = "stairs"
    ref = "oak_stairs"
    select = { }                                    # optional: properties kept at one value
    item = "oak_stairs"                             # optional: the item model to copy; "" for none

A mod's blocks are sorted by the shape of their blockstate: its format (variants, multipart or forge), its
properties, and the template each model it names is built on (the first model along the parents that names no
texture of its own). A block with the shape of a reference block takes the reference's type.
"""

from __future__ import annotations

import copy
import json
import re
import zipfile
from collections import Counter
from pathlib import Path

ROLE = {"owner": "block", "alpha": "opaque", "tint": False, "animated": False}
ROLE_KEYS = set(ROLE)
REFERENCE = re.compile(r"^(?:([a-z0-9_.-]+)=)?([a-z0-9_.-]+(?::[a-z0-9_./-]+)?)(?:\[([^\]]*)\])?$")
NAME = re.compile(r"^[a-z0-9_.-]+$")
TYPE_KEYS = {"blocks", "roles"}
BLOCK_KEYS = {"name", "ref", "select", "item"}


class BlockError(ValueError):
    pass


def split(ref: str, default: str = "minecraft") -> tuple[str, str]:
    return tuple(ref.split(":", 1)) if ":" in ref else (default, ref)  # type: ignore[return-value]


def full(texture: str) -> str:
    return texture if ":" in texture else f"minecraft:{texture}"


def model_location(ref: str, kind: str = "block") -> tuple[str, str]:
    """(namespace, path) of a model as a blockstate (``oak_stairs``) or a parent (``block/stairs``) names it."""
    namespace, path = split(ref)
    if not path.startswith(("block/", "item/", "builtin/")):
        path = f"{kind}/{path}"
    return namespace, f"models/{path}.json"


def dumps(data) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


class Resources:
    """Files under assets/: the minecraft namespace from the client JAR, any other from the mod's resources."""

    def __init__(self, client_jar: Path, mod: Path | None = None):
        try:
            self.jar = zipfile.ZipFile(client_jar)
        except (OSError, zipfile.BadZipFile) as error:
            raise BlockError(f"{Path(client_jar).name} is not a readable client JAR: {error}") from None
        if not any(n.startswith("assets/minecraft/blockstates/") for n in self.jar.namelist()):
            raise BlockError(f"{Path(client_jar).name} has no assets/minecraft/blockstates: not a client JAR")
        self.mod_zip, self.mod_dir = None, None
        if mod is not None:
            mod = Path(mod)
            if mod.is_file():
                try:
                    self.mod_zip = zipfile.ZipFile(mod)
                except zipfile.BadZipFile as error:
                    raise BlockError(f"{mod.name} is neither a folder nor a readable JAR: {error}") from None
            else:
                for candidate in (mod, mod / "src" / "main" / "resources"):
                    if (candidate / "assets").is_dir():
                        self.mod_dir = candidate
                        break
                else:
                    raise BlockError(f"{mod} holds no assets folder")
        self._json: dict = {}

    def read(self, namespace: str, path: str) -> bytes | None:
        name = f"assets/{namespace}/{path}"
        archive = self.jar if namespace == "minecraft" else self.mod_zip
        if archive is not None:
            try:
                return archive.read(name)
            except KeyError:
                return None
        if namespace != "minecraft" and self.mod_dir is not None:
            file = self.mod_dir / name
            return file.read_bytes() if file.is_file() else None
        return None

    def json(self, namespace: str, path: str):
        key = (namespace, path)
        if key not in self._json:
            raw = self.read(namespace, path)
            try:
                self._json[key] = None if raw is None else json.loads(raw.decode("utf-8-sig"))
            except ValueError as error:
                raise BlockError(f"assets/{namespace}/{path} is not valid JSON: {error}") from None
        return copy.deepcopy(self._json[key])

    def blockstate(self, ref: str) -> dict:
        namespace, name = split(ref)
        state = self.json(namespace, f"blockstates/{name}.json")
        if state is None:
            where = "the client JAR" if namespace == "minecraft" else "the mod's resources"
            raise BlockError(f"{ref}: no blockstate in {where}")
        return state

    def model(self, ref: str, kind: str = "block"):
        return self.json(*model_location(ref, kind))

    def mcmeta(self, texture: str):
        namespace, path = split(full(texture))
        return self.json(namespace, f"textures/{path}.png.mcmeta")

    def has_texture(self, texture: str) -> bool:
        namespace, path = split(full(texture))
        return self.read(namespace, f"textures/{path}.png") is not None

    def mod_blockstates(self) -> list[tuple[str, str]]:
        """(namespace, block) of every blockstate in the mod's resources."""
        if self.mod_zip is not None:
            names = self.mod_zip.namelist()
        elif self.mod_dir is not None:
            names = [p.relative_to(self.mod_dir).as_posix() for p in (self.mod_dir / "assets").rglob("*.json")]
        else:
            names = []
        out = []
        for name in names:
            match = re.fullmatch(r"assets/([^/]+)/blockstates/([^/]+)\.json", name)
            if match and match[1] != "minecraft":
                out.append((match[1], match[2]))
        return sorted(out)


# -- reading blockstates ---------------------------------------------------------------------------
def _is_table(value) -> bool:
    """A Forge property table (value -> entry), told from a single entry as Forge's loader tells it: by whether
    its first value is an object."""
    return isinstance(value, dict) and bool(value) and isinstance(next(iter(value.values())), dict)


def entries(state: dict):
    """Every variant entry of a blockstate: variants, multipart parts, Forge defaults and property tables."""
    forge = bool(state.get("forge_marker"))
    for value in (state.get("variants") or {}).values():
        if forge and _is_table(value):
            for sub in value.values():
                yield from (e for e in (sub if isinstance(sub, list) else [sub]) if isinstance(e, dict))
        else:
            yield from (e for e in (value if isinstance(value, list) else [value]) if isinstance(e, dict))
    for part in state.get("multipart") or []:
        apply = part.get("apply")
        yield from (e for e in (apply if isinstance(apply, list) else [apply]) if isinstance(e, dict))
    if isinstance(state.get("defaults"), dict):
        yield state["defaults"]


def properties(state: dict) -> list[str]:
    names = set()
    forge = bool(state.get("forge_marker"))
    for key, value in (state.get("variants") or {}).items():
        if forge and _is_table(value):
            names.add(key)
        else:
            names |= {pair.split("=", 1)[0] for pair in key.split(",") if "=" in pair}
    for part in state.get("multipart") or []:
        when = part.get("when") or {}
        for clause in when.get("OR", [when]):
            names |= set(clause)
    return sorted(names)


def model_refs(state: dict) -> list[str]:
    return list(dict.fromkeys(e["model"] for e in entries(state) if isinstance(e.get("model"), str)))


def form(state: dict) -> str:
    return "forge" if state.get("forge_marker") else "multipart" if "multipart" in state else "variants"


def select(state: dict, chosen: dict) -> dict:
    """The blockstate with each chosen property kept at its value and dropped."""
    if not chosen:
        return copy.deepcopy(state)
    if state.get("forge_marker"):
        raise BlockError("a property can be kept at one value only in a vanilla-format blockstate")
    out = copy.deepcopy(state)
    if "variants" in out:
        kept = {}
        for key, value in out["variants"].items():
            pairs = dict(p.split("=", 1) for p in key.split(",") if "=" in p)
            if any(pairs.get(k) not in (None, v) for k, v in chosen.items()):
                continue
            rest = ",".join(f"{k}={v}" for k, v in pairs.items() if k not in chosen)
            kept[rest or ("normal" if pairs else key)] = value
        out["variants"] = kept
    if "multipart" in out:
        parts = []
        for part in out["multipart"]:
            when = part.get("when")
            if when is None:
                parts.append(part)
                continue
            matching = []
            for clause in when.get("OR", [when]):
                if any(k in clause and v not in str(clause[k]).split("|") for k, v in chosen.items()):
                    continue
                matching.append({k: v for k, v in clause.items() if k not in chosen})
            if not matching:
                continue
            new = dict(part)
            matching = [c for c in matching if c]
            if not matching:
                new.pop("when")
            else:
                new["when"] = {"OR": matching} if "OR" in when else matching[0]
            parts.append(new)
        out["multipart"] = parts
    return out


def alternates(state: dict) -> int:
    return max([len(v) for v in (state.get("variants") or {}).values() if isinstance(v, list)] or [1])


# -- copying a reference ---------------------------------------------------------------------------
def model_suffixes(refs: list[str]) -> dict[str, str]:
    """Each model's name with the words every model of the block shares taken out."""
    words = {r: split(r)[1].rsplit("/", 1)[-1].split("_") for r in refs}
    common = set.intersection(*(set(w) for w in words.values())) if len(refs) > 1 else set(next(iter(words.values()), []))
    out, used = {}, set()
    for ref in refs:
        suffix = "_".join(w for w in words[ref] if w not in common)
        name, n = suffix, 2
        while name in used:
            name, n = f"{suffix}_{n}", n + 1
        used.add(name)
        out[ref] = name
    return out


def name_slots(name: str, uses: list[tuple[str, str, str]], suffix_of: dict[str, str]) -> dict[str, dict]:
    """One slot per distinct texture: the block's name alone if it has one texture, else by the variable that first
    uses it; alternates of one variable are told apart by their model's suffix, then by number."""
    order, first = [], {}
    for value, variable, where in uses:
        if value not in first:
            first[value] = (variable, where)
            order.append(value)

    def folder(value: str) -> str:
        return "items" if split(value)[1].startswith("items/") else "blocks"

    def variables(value: str) -> list[str]:
        return sorted({v for texture, v, _ in uses if texture == value})

    if len(order) == 1:
        return {f"{folder(order[0])}/{name}": {"source": order[0], "vars": variables(order[0])}}
    candidates = {value: first[value][0] for value in order}
    counts = Counter(candidates.values())
    several = len({first[value][0] for value in order}) > 1
    for value in order:
        variable, where = first[value]
        if counts[candidates[value]] > 1:
            candidates[value] = "_".join(x for x in (suffix_of.get(where, ""), variable if several else "") if x) or variable
    final = Counter(candidates.values())
    seen: dict[str, int] = {}
    out = {}
    for value in order:
        candidate = candidates[value]
        if final[candidate] > 1:
            seen[candidate] = seen.get(candidate, -1) + 1
            candidate = f"{candidate}_{seen[candidate]}"
        out[f"{folder(value)}/{name}_{candidate}"] = {"source": value, "vars": variables(value)}
    return out


def _inherit(ref: str, model: dict) -> dict:
    """A vanilla model with geometry as a child of itself: the copy names textures, the game keeps the geometry."""
    namespace, path = model_location(ref)
    if namespace == "minecraft" and "elements" in model and "parent" not in model:
        return {"parent": path.removeprefix("models/").removesuffix(".json"), "textures": model.get("textures", {})}
    return model


def _name_inherited(model: dict, resources: Resources) -> dict:
    """The model naming, after its own, every concrete texture its parents supply (the nearest definition of a
    variable wins), so each becomes a slot rather than staying the reference's texture."""
    own = dict(model.get("textures") or {})
    seen, inherited = set(own), {}
    parent, depth = model.get("parent"), 0
    while parent and depth < 16 and not split(parent)[1].startswith("builtin/"):
        found = resources.model(parent)
        if found is None:
            break
        for variable, value in (found.get("textures") or {}).items():
            if variable in seen:
                continue
            seen.add(variable)
            if isinstance(value, str) and not value.startswith("#"):
                inherited[variable] = value
        parent, depth = found.get("parent"), depth + 1
    if inherited:
        model["textures"] = {**own, **inherited}
    return model


def copy_block(spec: dict, resources: Resources, namespace: str) -> dict:
    """The reference block renamed into ``namespace``: blockstate, models, item model, slots and their .mcmeta."""
    name, ref = spec["name"], spec["ref"]
    state = select(resources.blockstate(ref), spec.get("select") or {})
    refs = [r for r in model_refs(state) if r != "forge:fluid"]
    suffixes = model_suffixes(refs)
    models: dict[str, dict] = {}
    renamed: dict[str, str] = {}
    for r in refs:
        model = resources.model(r)
        if model is None:
            raise BlockError(f"{ref}: its model {r} is not there")
        new = name + (f"_{suffixes[r]}" if suffixes[r] else "")
        renamed[r] = new
        models[new] = _name_inherited(_inherit(r, model), resources)
    parents: dict[str, str] = {}
    for new, model in list(models.items()):  # a parent outside vanilla is copied too, under the block's name
        parent = model.get("parent")
        if parent and split(parent)[0] != "minecraft":
            if parent not in parents:
                copied = resources.model(parent)
                if copied is None:
                    raise BlockError(f"{ref}: the parent {parent} is not there")
                if copied.get("parent") and split(copied["parent"])[0] != "minecraft":
                    raise BlockError(f"{ref}: the parent {parent} has a parent outside vanilla")
                parents[parent] = f"{name}_parent" + (f"_{len(parents) + 1}" if parents else "")
                models[parents[parent]] = copied
            model["parent"] = f"{namespace}:block/{parents[parent]}"
    item = None
    item_ref = spec.get("item", ref)
    if item_ref:
        item_namespace, item_name = split(item_ref)
        item = resources.json(item_namespace, f"models/item/{item_name}.json")
    if item is not None:
        parent = item.get("parent", "")
        parent_path = split(parent)[1] if parent else ""
        if parent and not parent_path.startswith(("item/", "builtin/")):
            parent_namespace = split(parent)[0]
            target = parent_path.removeprefix("block/")
            key = next((r for r in renamed if split(r)[0] == parent_namespace
                        and split(r)[1].removeprefix("block/") == target), None)
            if key is None:
                # an item drawn from a block model the blockstate never uses (a fence's inventory model)
                extra = resources.model(parent)
                if extra is None:
                    raise BlockError(f"{ref}: the item's parent {parent} is not there")
                common = set.intersection(*(set(split(r)[1].rsplit("/", 1)[-1].split("_")) for r in renamed)) \
                    if renamed else set()
                suffix = "_".join(w for w in target.split("_") if w not in common) or "item"
                new = f"{name}_{suffix}"
                while new in models:
                    new += "_item"
                key = parent
                renamed[key] = new
                models[new] = _name_inherited(_inherit(parent, extra), resources)
            item["parent"] = f"{namespace}:block/{renamed[key]}"
    uses: list[tuple[str, str, str]] = []

    def note(value, variable: str, where: str):
        if isinstance(value, str) and not value.startswith("#"):
            uses.append((full(value), variable, where))

    for new, model in models.items():
        for variable, value in (model.get("textures") or {}).items():
            if variable != "particle":
                note(value, variable, new)
    for entry in entries(state):
        for variable, value in (entry.get("textures") or {}).items():
            note(value, variable, "")
    if item is not None:
        for variable, value in (item.get("textures") or {}).items():
            note(value, variable, "item")
    for model in models.values():  # a particle of its own is a slot too
        note((model.get("textures") or {}).get("particle"), "particle", "")
    suffix_of = {new: new[len(name) + 1:] if new != name else "" for new in models}
    slots = name_slots(name, uses, suffix_of)
    lookup = {info["source"]: f"{namespace}:{slot}" for slot, info in slots.items()}

    def rewrite(textures: dict) -> dict:
        return {k: lookup[full(v)] if isinstance(v, str) and not v.startswith("#") else v for k, v in textures.items()}

    for model in models.values():
        if "textures" in model:
            model["textures"] = rewrite(model["textures"])
    for entry in entries(state):
        if isinstance(entry.get("model"), str) and entry["model"] in renamed:
            entry["model"] = f"{namespace}:{renamed[entry['model']]}"
        if "textures" in entry:
            entry["textures"] = rewrite(entry["textures"])
    if item is not None and "textures" in item:
        item["textures"] = rewrite(item["textures"])
    mcmeta = {slot: meta for slot, info in slots.items() if (meta := resources.mcmeta(info["source"])) is not None}
    return {"name": name, "ref": ref, "select": spec.get("select") or None, "blockstate": state, "models": models,
            "item": item, "slots": slots, "mcmeta": mcmeta}


def reference(text: str) -> dict:
    """``ref``, ``name=ref`` or ``name=ref[property=value,...]`` as a reference spec."""
    match = REFERENCE.match(text.strip())
    if not match:
        raise BlockError(f"not a reference block: {text!r} (use ref, name=ref or name=ref[property=value])")
    chosen = {}
    for pair in (match[3] or "").split(","):
        if pair.strip():
            key, separator, value = pair.partition("=")
            if not separator:
                raise BlockError(f"{text}: {pair!r} is not property=value")
            chosen[key.strip()] = value.strip()
    return {"name": match[1] or split(match[2])[1].rsplit("/", 1)[-1], "ref": match[2], "select": chosen}


def catalog_types(data: dict) -> dict[str, dict]:
    types = data.get("types")
    if not isinstance(types, dict) or not types:
        raise BlockError("the catalog needs [types.NAME] tables")
    out = {}
    for type_name, spec in types.items():
        if not isinstance(spec, dict) or not isinstance(spec.get("blocks"), list) or not spec["blocks"]:
            raise BlockError(f"types.{type_name} needs blocks = [{{ name = ..., ref = ... }}]")
        roles = spec.get("roles", {})
        if not isinstance(roles, dict) or not all(isinstance(r, dict) and set(r) <= ROLE_KEYS for r in roles.values()):
            raise BlockError(f"types.{type_name}.roles: each role is a table of {', '.join(sorted(ROLE_KEYS))}")
        about = {k: v for k, v in spec.items() if k not in TYPE_KEYS}
        if not all(isinstance(v, (str, int, float, bool)) for v in about.values()):
            raise BlockError(f"types.{type_name}: besides blocks and roles, keys hold plain values")
        blocks = []
        for block in spec["blocks"]:
            if not isinstance(block, dict) or set(block) - BLOCK_KEYS or "ref" not in block:
                raise BlockError(f"types.{type_name}.blocks: each block is {{ name, ref, select, item }} with a ref")
            parsed = reference(block["ref"])
            blocks.append({"name": block.get("name", parsed["name"]), "ref": parsed["ref"],
                           "select": block.get("select", parsed["select"]) or {}, **({"item": block["item"]}
                                                                                   if "item" in block else {})})
        out[type_name] = {"about": about, "roles": roles, "blocks": blocks}
    return out


def role_of(roles: dict, variables: list[str], animated: bool) -> dict:
    out = dict(ROLE, animated=animated)
    for variable in variables:
        out.update(roles.get("*", {}))
        out.update(roles.get(variable, {}))
    return out


def title(name: str) -> str:
    return " ".join(word.capitalize() for word in name.replace(".", "_").replace("-", "_").split("_") if word)


def pack_files(types: dict[str, dict], resources: Resources, namespace: str, description: str) -> tuple[dict, dict]:
    """Every file of the pack (path -> text) and the slot listing."""
    files = {"pack.mcmeta": dumps({"pack": {"pack_format": 3, "description": description}})}
    catalogue, lang, owners = {}, [], {}
    for type_name, spec in types.items():
        blocks = []
        for block_spec in spec["blocks"]:
            if not NAME.match(block_spec["name"]):
                raise BlockError(f"{block_spec['name']!r}: a block name is lowercase letters, digits, '_', '.' or '-'")
            try:
                block = copy_block(block_spec, resources, namespace)
            except BlockError as error:
                raise BlockError(f"type {type_name}: {error}") from None
            name = block["name"]
            paths = [f"assets/{namespace}/blockstates/{name}.json"]
            paths += [f"assets/{namespace}/models/block/{m}.json" for m in block["models"]]
            paths += [f"assets/{namespace}/models/item/{name}.json"] if block["item"] is not None else []
            for path in paths:
                if path in owners:
                    raise BlockError(f"{block_spec['ref']} as {name} and {owners[path]} both make {path}")
                owners[path] = f"{block_spec['ref']} as {name}"
            files[paths[0]] = dumps(block["blockstate"])
            for model_name, model in sorted(block["models"].items()):
                files[f"assets/{namespace}/models/block/{model_name}.json"] = dumps(model)
            if block["item"] is not None:
                files[f"assets/{namespace}/models/item/{name}.json"] = dumps(block["item"])
            for slot, meta in sorted(block["mcmeta"].items()):
                files[f"assets/{namespace}/textures/{slot}.png.mcmeta"] = dumps(meta)
            lang.append(f"tile.{namespace}.{name}.name={title(name)}")
            state = block["blockstate"]
            blocks.append({
                "block": f"{namespace}:{name}", "reference": block["ref"], "select": block["select"],
                "format": form(state), "properties": properties(state),
                "blockstate": paths[0], "models": sorted(paths[1:1 + len(block["models"])]),
                "item": f"assets/{namespace}/models/item/{name}.json" if block["item"] is not None else None,
                "lang": f"tile.{namespace}.{name}.name",
                "slots": {f"{namespace}:{slot}": {**role_of(spec["roles"], info["vars"],
                                                           "animation" in (block["mcmeta"].get(slot) or {})),
                                                  "vars": info["vars"], "source": info["source"],
                                                  "mcmeta": slot in block["mcmeta"]}
                          for slot, info in block["slots"].items()}})
        catalogue[type_name] = {**spec["about"], "blocks": blocks}
    files[f"assets/{namespace}/lang/en_us.lang"] = "\n".join(sorted(lang)) + "\n"
    listing = {
        "schema": "wb/slots/v1", "namespace": namespace,
        "about": ("Every texture slot of the pack, by block type: the PNG a complete pack supplies at "
                  "assets/<namespace>/textures/<slot>.png, 16 px wide (an animated slot is a strip of 16 x 16 frames "
                  "with its .mcmeta). owner: block (its own), material (the base block's, reused), host (another "
                  "block's, borrowed) or type (one shared by every block of the type). alpha: the transparency the "
                  "block's render layer allows. tint: coloured in code. source: the reference texture."),
        "types": catalogue}
    files["slots.json"] = dumps(listing)
    return files, listing


# -- checking the pack -----------------------------------------------------------------------------
def verify(files: dict[str, str], resources: Resources, namespace: str, listing: dict) -> list[str]:
    """Every model the pack names resolves, every face's texture ends at a listed slot, and every slot is used."""
    problems: list[str] = []

    def load(ref: str, kind: str = "block"):
        model_namespace, path = model_location(ref, kind)
        if model_namespace == namespace:
            text = files.get(f"assets/{namespace}/{path}")
            return json.loads(text) if text else None
        return resources.json(model_namespace, path)

    def used(model: dict, where: str) -> set[str]:
        chain, textures, elements = [], {}, None
        while model is not None and len(chain) < 16:
            chain.append(model)
            if elements is None and "elements" in model:
                elements = model["elements"]
            parent = model.get("parent")
            if not parent or split(parent)[1].startswith("builtin/"):
                break
            model = load(parent)
            if model is None:
                problems.append(f"{where}: the parent {parent} is not there")
                return set()
        for link in reversed(chain):
            textures.update(link.get("textures") or {})

        def resolve(value):
            hops = 0
            while isinstance(value, str) and value.startswith("#") and hops < 8:
                value, hops = textures.get(value[1:]), hops + 1
            return value

        out = set()
        for element in elements or []:
            for face in (element.get("faces") or {}).values():
                value = resolve(face.get("texture", ""))
                if not isinstance(value, str) or value.startswith("#"):
                    problems.append(f"{where}: a face's texture {face.get('texture')} resolves to nothing")
                else:
                    out.add(full(value))
        for variable in [v for v in textures if v.startswith("layer")] + ["particle"]:
            value = resolve(textures.get(variable))
            if isinstance(value, str) and not value.startswith("#"):
                out.add(full(value))
        return out

    listed = {slot for t in listing["types"].values() for b in t["blocks"] for slot in b["slots"]}
    referenced: set[str] = set()
    for t in listing["types"].values():
        for block in t["blocks"]:
            state = json.loads(files[block["blockstate"]])
            name = block["block"].split(":", 1)[1]
            for entry in entries(state):
                referenced |= {full(v) for v in (entry.get("textures") or {}).values()
                               if isinstance(v, str) and not v.startswith("#")}
                ref = entry.get("model")
                if not isinstance(ref, str) or ref == "forge:fluid":
                    continue
                model = load(ref)
                if model is None:
                    problems.append(f"{block['blockstate']}: the model {ref} is not there")
                    continue
                if entry.get("textures"):
                    model = {**model, "textures": {**(model.get("textures") or {}), **entry["textures"]}}
                referenced |= used(model, f"{name}'s model {ref}")
            if block["item"]:
                referenced |= used(json.loads(files[block["item"]]), f"{name}'s item")
    for ref in sorted(referenced):
        if split(ref)[0] != namespace:
            problems.append(f"a texture outside the pack is still named: {ref}")
        elif ref not in listed:
            problems.append(f"{ref} is used but not listed in slots.json")
    for slot in sorted(listed - referenced):
        problems.append(f"{slot} is listed in slots.json but no model uses it")
    return problems


# -- sorting a mod's blocks ------------------------------------------------------------------------
def template(ref: str, resources: Resources) -> str:
    """The model a block's model is built on: the first along its parents that names no texture of its own."""
    current, location = ref, model_location(ref)
    for _ in range(16):
        model = resources.json(*location)
        if model is None:
            return "missing"
        own = any(isinstance(v, str) and not v.startswith("#") for v in (model.get("textures") or {}).values())
        if not own:
            namespace, path = location
            name = path.removeprefix("models/").removesuffix(".json")
            return name if namespace == "minecraft" else f"{namespace}:{name}"
        parent = model.get("parent")
        if not parent or split(parent)[1].startswith("builtin/"):
            return "elements" if "elements" in model else parent or "none"
        current, location = parent, model_location(parent)
    return "too deep"


def shape(state: dict, resources: Resources) -> tuple:
    return form(state), tuple(properties(state)), tuple(sorted({template(r, resources) for r in model_refs(state)
                                                                if r != "forge:fluid"}))


def _textures_of(state: dict, namespace: str, block: str, resources: Resources) -> set[str]:
    found = set()
    for entry in entries(state):
        found |= {full(v) for v in (entry.get("textures") or {}).values() if isinstance(v, str) and not v.startswith("#")}
    refs = model_refs(state)
    item = f"{namespace}:item/{block}"
    for ref in refs + [item]:
        location = model_location(ref)
        for _ in range(16):
            model = resources.json(*location)
            if model is None:
                break
            found |= {full(v) for v in (model.get("textures") or {}).values() if isinstance(v, str)
                      and not v.startswith("#")}
            parent = model.get("parent")
            if not parent or split(parent)[1].startswith("builtin/"):
                break
            location = model_location(parent)
    return found


def classify(resources: Resources, types: dict[str, dict]) -> dict:
    exemplars: dict[tuple, str] = {}
    for type_name, spec in types.items():
        for block in spec["blocks"]:
            exemplars.setdefault(shape(resources.blockstate(block["ref"]), resources), type_name)
    blocks, groups = {}, {}
    for namespace, block in resources.mod_blockstates():
        state = resources.blockstate(f"{namespace}:{block}")
        found = shape(state, resources)
        textures = _textures_of(state, namespace, block, resources)
        record = {"type": exemplars.get(found), "alternates": alternates(state), "textures": sorted(textures)}
        missing = sorted(t for t in textures if split(t)[0] != "minecraft" and not resources.has_texture(t))
        if missing:
            record["missing"] = missing
        blocks[f"{namespace}:{block}"] = record
        groups.setdefault(found, []).append(f"{namespace}:{block}")
    users = Counter(t for record in blocks.values() for t in record["textures"])
    counts = Counter(record["type"] or "(no type)" for record in blocks.values())
    return {"schema": "wb/block-shapes/v1",
            "counts": dict(counts.most_common()),
            "shapes": [{"format": f, "properties": list(p), "models": list(m), "type": exemplars.get((f, p, m)),
                        "blocks": names} for (f, p, m), names in sorted(groups.items(), key=lambda kv: -len(kv[1]))],
            "unmatched": sorted(name for name, record in blocks.items() if record["type"] is None),
            "shared_textures": {t: n for t, n in sorted(users.items()) if n > 1},
            "blocks": blocks}
