"""content.scripts: a pack's GroovyScript folder, read statically (no Groovy runtime).

What runs, and in what order, follows GroovyScript's own rules (runconfig.py), in every configuration the
pack has: each packmode on each side. Each file is tokenized (lexer.py) and read (syntax.py) for what it does:

- the ids it declares: ore dictionary entries, recipe names, content (items, blocks, fluids) and ids a pack's
  rules name; an id declared twice where both declarations run in one configuration is a duplicate;
- recipe additions and removals, by registry (mod.registry: minecraft.crafting, thermalexpansion.pulverizer),
  per file and per mod, as written (a call in a loop counts once); and builders never registered;
- the mods it refers to (mods.<name>, item('mod:...') and the other object mappers); a reference inside a
  check that the mod is loaded, or in a file whose MODS_LOADED names it, is guarded;
- the scripts it depends on (imports, static imports and classes of its own package), for import cycles.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from fnmatch import fnmatchcase
from pathlib import Path

from . import logs, runconfig, syntax
from .graph import components, cycle_in
from .lexer import NAME, tokenize
from .mods import Mods
from .rules import Rules
from .runconfig import SIDES, STAGES

MAX_BYTES = 16 * 2**20
VANILLA = frozenset({"minecraft", "mc", "vanilla"})  # the names of GroovyScript's minecraft container
GLOBALS = {"crafting": "crafting", "furnace": "furnace", "oredict": "ore_dict", "ore_dict": "ore_dict",
           "content": "content", "inworldcrafting": "in_world_crafting", "in_world_crafting": "in_world_crafting"}
RECIPES = frozenset({"crafting", "furnace", "in_world_crafting"})  # minecraft's registries of recipes
MAPPERS = frozenset({"item", "block", "blockstate", "enchantment", "potion", "potionType", "sound", "entity",
                     "biome", "profession", "resource"})  # object mappers whose argument is mod:name
REGISTERS = frozenset({"register", "buildAndRegister"})


def snake(name: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).lower()


def operation(method: str, terminal: str) -> str | None:
    """What a registry call does: add, remove, replace (both), unregistered (a builder never registered)."""
    if method.startswith("remove") or terminal.startswith("remove"):
        return "remove"
    if method.startswith("replace"):
        return "replace"
    if method.startswith("add"):
        return "add"
    if method.lower().endswith("builder"):
        return "add" if terminal in REGISTERS else "unregistered"
    return None


class Script:
    def __init__(self, path: str, size: int):
        self.path, self.size = path, size
        self.lines = 0
        self.source: syntax.Source | None = None
        self.problems: list[dict] = []  # what cannot be read: unclosed strings and brackets
        self.preprocessors: list[runconfig.Preprocessor] = []
        self.flagged: list[dict] = []  # preprocessor comments not read, arguments GroovyScript rejects
        self.classes: list[str] = []  # the classes it declares, qualified
        self.declared: list[tuple[str, str, int]] = []  # kind, id, line
        self.operations: list[tuple[str, str, str, int]] = []  # mod, registry, add|remove, line
        self.unregistered: list[tuple[str, int]] = []
        self.references: list[tuple[str, str, int, bool]] = []  # mod, how, line, guarded
        self.malformed: list[tuple[str, int]] = []
        self.oredict: Counter = Counter()

    def mods_loaded(self) -> set[str]:
        return {a for p in self.preprocessors if p.name == "MODS_LOADED" for a in p.args}


class Analysis:
    def __init__(self, root: Path, *, packmode: str, side: str, file_system: str, mods: Mods | None,
                 platform: list[str], rules: Rules, cancel=lambda: None):
        self.root, self.side, self.file_system = root, side, file_system
        self.requested_packmode = packmode
        self.mods, self.rules, self.cancel = mods, rules, cancel
        self.platform = list(platform)
        self.findings: list[str] = []
        self.notes: list[str] = []
        self.containers = {**(mods.aliases if mods else {}), **rules.aliases}

    # -- the whole analysis -------------------------------------------------------------------------------
    def run(self, log: Path | None) -> dict:
        try:
            self.config = runconfig.load(self.root / "runConfig.json")
        except (ValueError, OSError) as error:
            self.findings.append(f"runConfig.json cannot be read, so GroovyScript runs nothing: {error}")
            return {"runConfig": None, "error": str(error)}
        self.namespace = runconfig.namespace(self.config)
        self.inventory = self._inventory()
        self.scripts = {path: self._read(path) for path in self.inventory}
        self._statics()
        for script in self.scripts.values():
            self.cancel()
            if script.source is not None:
                self._facts(script)
        self._configurations()
        orders = {fs: [(stage, s) for stage in STAGES for s in runconfig.stage_files(self.root, self.config, stage, fs)]
                  for fs in runconfig.FILE_SYSTEMS}
        self.entries = orders[self.file_system]
        self._runs()
        report = {"runConfig": self._config_report(), "selected": {
            "packmode": self.packmode, "side": self.side, "fileSystem": self.file_system},
            "configurations": [self.label(c) for c in self.configurations]}
        report["loaderPaths"] = self._loader_paths()
        report["order"] = self._order_report()
        report["otherFileSystems"] = self._compare(orders)
        report["imports"] = self._imports()
        report["files"] = self._files_report()
        report["unconfigured"] = sorted(set(self.inventory) - self.configured - self.on_demand)
        report["onDemand"] = sorted(self.on_demand - self.configured)
        report["preprocessors"] = self._preprocessor_problems()
        report["syntax"] = self._syntax_problems()
        report["duplicates"] = self._duplicates()
        report["recipes"] = self._recipes()
        running = [s for s in self.scripts.values() if self.runs_in(s.path, self.selected)]
        report["declared"] = dict(sorted(Counter(kind for s in running for kind, _, _ in s.declared).items()))
        report["references"] = self._references()
        report["log"] = self._log(log) if log else None
        if report["unconfigured"]:
            self.notes.append(f"{_many(len(report['unconfigured']), 'file')} no loader runs and no script imports, "
                              f"the first: {report['unconfigured'][0]}")
        report["notes"] = self.notes
        return report

    @staticmethod
    def label(configuration: tuple[str | None, str]) -> str:
        mode, side = configuration
        return f"{mode}/{side}" if mode else side

    # -- reading ---------------------------------------------------------------------------------------------
    def _inventory(self) -> list[str]:
        return [path.relative_to(self.root).as_posix() for path in runconfig.walk(self.root, self.config.debug)]

    def _read(self, path: str) -> Script:
        file = self.root / path
        script = Script(path, file.stat().st_size)
        if script.size > MAX_BYTES:
            script.problems.append({"line": 0, "detail": f"over {MAX_BYTES} bytes, not read"})
            return script
        text = file.read_bytes().decode("utf-8", errors="replace").removeprefix("﻿")
        script.lines = text.count("\n") + 1
        tokens, problems = tokenize(text)
        script.source = syntax.read(tokens)
        script.problems = [{"line": p.line, "detail": p.message} for p in problems] + \
                          [{"line": line, "detail": message} for line, message in script.source.problems]
        script.preprocessors, script.flagged = runconfig.preprocessors(text)
        script.flagged += runconfig.check(script.preprocessors, self.config)
        package = script.source.package
        script.classes = [f"{package}.{name}" if package else name for name, _ in script.source.types]
        return script

    def _statics(self) -> None:
        """Fields a class of the folder aliases (static MIXER = mods.gregtech.mixer), for the files that import
        it, statically or not, or share its package."""
        by_class = {}
        for script in self.scripts.values():
            if script.source is not None:
                for name in script.classes:
                    by_class[name] = script.source.aliases
        for script in self.scripts.values():
            source = script.source
            if source is None:
                continue
            package = source.package
            for name, aliases in by_class.items():
                if name.rsplit(".", 1)[0] == package or ("." not in name and not package):
                    source.statics.setdefault(name.rsplit(".", 1)[-1], aliases)
            for imp in source.imports:
                if imp.static:
                    owner, member = (imp.name, None) if imp.star or "." not in imp.name else imp.name.rsplit(".", 1)
                    fields = by_class.get(owner, {})
                    for field_name, path in fields.items():
                        if member is None or field_name == member:
                            source.aliases.setdefault(imp.alias or field_name, path)
                elif imp.name in by_class:
                    source.statics.setdefault(imp.alias or imp.name.rsplit(".", 1)[-1], by_class[imp.name])

    def container(self, name: str) -> str:
        """The mod a name under mods. stands for."""
        if name.lower() in VANILLA:
            return "minecraft"
        return self.containers.get(name, name)

    def _target(self, name: tuple[str, ...]) -> tuple[str, str, str] | None:
        """(mod, registry, method) of a call on a registry: mods.te.pulverizer.add, crafting.addShaped."""
        if len(name) >= 3 and name[0] == "mods":
            registry = ".".join(snake(part) for part in name[2:-1])
            return self.container(name[1]), registry, name[-1]
        if len(name) >= 2 and name[0].lower() in GLOBALS:
            inner = "".join("." + snake(part) for part in name[1:-1])
            return "minecraft", GLOBALS[name[0].lower()] + inner, name[-1]
        return None

    def _facts(self, script: Script) -> None:
        source, tokens = script.source, script.source.tokens
        loaded = script.mods_loaded()
        for k in range(len(tokens) - 2):
            if tokens[k].text == "mods" and tokens[k].kind == NAME and tokens[k + 1].text in syntax.DOTS and \
                    tokens[k + 2].kind == NAME and (k == 0 or tokens[k - 1].text not in syntax.DOTS):
                if k + 4 < len(tokens) and tokens[k + 3].text in syntax.DOTS and tokens[k + 4].text in syntax.GUARDS:
                    continue  # mods.x.isLoaded() asks; it does not use the mod
                self._reference(script, self.container(tokens[k + 2].text), f"mods.{tokens[k + 2].text}", k, loaded)
        for call in source.calls:
            if call.chained:
                continue
            chain = source.chain(call)
            name = call.name if call.constructs else source.resolve(call.name)
            qualified = ".".join(name)
            self._ids(script, call, qualified)
            for step in chain[1:]:
                self._ids(script, step, qualified + "." + ".".join(step.name))
            if call.constructs:
                continue
            if len(call.name) == 1:
                self._mapper(script, call, loaded)
                if call.name == ("ore",) and len(chain) > 1 and call.args:
                    self._ore(script, source.literal(call.args[0]), chain[1].name[-1], chain[1].args, call.line)
            target = self._target(name)
            if target is None:
                continue
            mod, registry, method = target
            key = f"{mod}.{registry}"
            if (mod, registry) == ("minecraft", "ore_dict"):
                if call.args:
                    self._ore(script, source.literal(call.args[0]), method, call.args[1:], call.line)
                continue
            if (mod, registry) == ("minecraft", "content"):
                made = re.fullmatch(r"(?:create|register)([A-Z]\w*)", method)
                value = self._id_value(source, call.args[0]) if made and call.args else None
                if value:
                    script.declared.append((made.group(1).lower(), self._location(value), call.line))
                continue
            if not registry or (mod == "minecraft" and registry not in RECIPES):
                continue
            if any(fnmatchcase(key, glob) for glob in self.rules.not_recipes):
                continue
            kind = operation(method, chain[-1].name[-1])
            if kind == "unregistered" and any(step.closure for step in chain):  # .with { ... }: look inside
                kind = "add" if any(t.text in REGISTERS for step in chain if step.closure
                                    for t in tokens[step.closure[0]:step.closure[1]]) else None
            if kind == "unregistered":
                if source.starts_statement(call.start):
                    script.unregistered.append((key, call.line))
                continue
            if kind is None:
                continue
            for op in ("add", "remove") if kind == "replace" else (kind,):
                script.operations.append((mod, key, op, call.line))
            if kind == "add":
                named = source.literal(call.args[0]) if key == "minecraft.crafting" and call.args and \
                    method in ("addShaped", "addShapeless") else None
                named = named or next((self._id_value(source, step.args[0]) for step in chain[1:]
                                       if step.name == ("name",) and step.args), None)
                if named:
                    script.declared.append(("recipe", f"{key} {self._location(named)}", call.line))

    def _ore(self, script: Script, ore: str | None, method: str, args: list, line: int) -> None:
        if method.startswith("remove"):
            script.oredict["remove"] += 1
            return
        if method != "add":
            return
        script.oredict["add"] += 1
        for span in args:
            stack = self._stack(script.source, span)
            if ore and stack:
                script.declared.append(("ore dictionary entry", f"{ore} {stack}", line))

    def _stack(self, source: syntax.Source, span: tuple[int, int]) -> str | None:
        """An ingredient as an id: item('a:b') and item('a:b', 0) are a:b:0; ore('x') is ore:x."""
        call = source.inner(span)
        if call is None or len(call.name) != 1 or not call.args:
            return None
        value = source.literal(call.args[0])
        if value is None:
            return None
        if call.name == ("ore",):
            return f"ore:{value}"
        if call.name != ("item",):
            return None
        parts = value.split(":")
        if len(parts) == 2:
            meta = source.integer(call.args[1]) if len(call.args) > 1 else 0
            if meta is None:
                meta = source.literal(call.args[1]) if len(call.args) > 1 else None
            return f"{value}:{meta}" if meta is not None else None
        return value if len(parts) == 3 else None

    def _id_value(self, source: syntax.Source, span: tuple[int, int]) -> str | None:
        """An id argument: a literal, a number, or literals one call wraps whole: resource('ns', 'path') is
        ns:path, and a helper's prefix('name') is name."""
        literal = source.literal(span)
        if literal is not None:
            return literal
        number = source.integer(span)
        if number is not None:
            return str(number)
        call = source.inner(span)
        if call is not None and call.end == span[1] - 1 and call.closure is None:
            parts = [source.literal(a) for a in call.args]
            if parts and all(p is not None for p in parts):
                return ":".join(parts)
        return None

    def _location(self, value: str) -> str:
        value = value.strip().lower()
        return value if ":" in value else f"{self.namespace}:{value}"

    def _ids(self, script: Script, call: syntax.Call, qualified: str) -> None:
        last = qualified.rsplit(".", 1)[-1]
        for rule in self.rules.ids:
            if (fnmatchcase(qualified, rule.call) or fnmatchcase(last, rule.call)) and rule.arg < len(call.args):
                value = self._id_value(script.source, call.args[rule.arg])
                if value:
                    script.declared.append((rule.kind, value, call.line))

    def _mapper(self, script: Script, call: syntax.Call, loaded: set[str]) -> None:
        name = call.name[0]
        if name in self.rules.mappers:
            self._reference(script, self.rules.mappers[name], f"{name}(...)", call.start, loaded)
            return
        if name not in MAPPERS or not call.args:
            return
        value = script.source.literal(call.args[0])
        if value is None:
            return
        if ":" in value:
            space = value.split(":", 1)[0]
            if space:
                self._reference(script, space, f"{name}('{value}')", call.start, loaded)
        elif name == "item" and len(call.args) <= 2:
            script.malformed.append((f"item('{value}')", call.line))

    def _reference(self, script: Script, mod: str, how: str, index: int, loaded: set[str]) -> None:
        guards = {self.container(m) for m in script.source.guarded(index)} | loaded
        script.references.append((mod, how, script.source.tokens[index].line, mod in guards))

    # -- what runs -------------------------------------------------------------------------------------------
    def _configurations(self) -> None:
        modes = list(self.config.packmodes)
        if not modes and self.config.integrate_packmode_mod:
            modes = sorted({runconfig.packmode_name(a) for s in self.scripts.values() for p in s.preprocessors
                            if p.name == "PACKMODE" for a in p.args})
        if self.requested_packmode:
            wanted = runconfig.packmode_name(self.requested_packmode)
            if not self.config.integrate_packmode_mod and wanted not in self.config.packmodes:
                raise ValueError(f"packmode {self.requested_packmode} is not one runConfig.json defines "
                                 f"({', '.join(self.config.packmodes) or 'none'})")
            self.packmode = wanted
            if wanted not in modes:
                modes.append(wanted)
        elif modes:
            self.packmode = self.config.default_packmode if self.config.default_packmode in modes else modes[0]
        else:
            self.packmode = None
        self.configurations = [(mode, side) for mode in (modes or [None]) for side in SIDES]
        self.selected = (self.packmode, self.side)

    def _runs(self) -> None:
        loaded = set(self.mods.ids) if self.mods is not None else None
        self.runs: dict[tuple[str, str], dict] = {}
        for stage, chosen in self.entries:
            script = self.scripts[chosen.file]
            self.runs[(stage, chosen.file)] = {c: runconfig.allows(
                script.preprocessors, packmode=c[0], side=c[1], debug=self.config.debug, mods=loaded)
                for c in self.configurations}
        self.by_file: dict[str, list[dict]] = defaultdict(list)
        self.stages: dict[str, list[str]] = defaultdict(list)
        for (stage, file), allowed in self.runs.items():
            self.by_file[file].append(allowed)
            self.stages[file].append(stage)
        self.configured = set(self.by_file)
        self.active = {file for file, rows in self.by_file.items() if any(ok for r in rows for ok, _ in r.values())}
        self.on_demand: set[str] = set()  # set by _imports: scripts the active ones import

    def runs_in(self, file: str, configuration: tuple) -> bool:
        return any(allowed[configuration][0] for allowed in self.by_file.get(file, ()))

    def _active_scripts(self) -> list[Script]:
        return [self.scripts[f] for f in sorted(self.active | self.on_demand)]

    # -- report sections ----------------------------------------------------------------------------------
    def _config_report(self) -> dict:
        config = self.config
        for problem in config.problems:
            if problem["kind"] in ("lenient", "default"):
                self.notes.append(problem["detail"])
            else:
                self.findings.append(f"runConfig.json: {problem['detail']}")
        return {"packId": config.pack_id, "packName": config.pack_name, "debug": config.debug,
                "loaders": config.loaders, "packmodes": config.packmodes, "defaultPackmode": config.default_packmode,
                "integratePackmodeMod": config.integrate_packmode_mod, "problems": config.problems}

    def _loader_paths(self) -> list[dict]:
        out = []
        separator_case = runconfig.FILE_SYSTEMS[self.file_system][1]
        for stage in STAGES:
            for entry in self.config.loaders.get(stage, []):
                found, how = runconfig.locate(self.root, entry, separator_case)
                strict, strict_how = runconfig.locate(self.root, entry, True)
                count = sum(1 for s, chosen in self.entries if s == stage and chosen.via == entry)
                row = {"loader": stage, "path": entry, "found": how, "files": count}
                out.append(row)
                if how == "outside":
                    self.findings.append(f"runConfig.json: loader {stage} path '{entry}' leaves the groovy folder "
                                         "(not analysed)")
                elif strict_how == "case":
                    actual = runconfig.locate(self.root, entry, False)[0]
                    shown = actual.relative_to(self.root).as_posix() if actual else entry
                    self.findings.append(f"runConfig.json: loader {stage} path '{entry}' matches '{shown}' only on a "
                                         "case-insensitive file system: on Linux (a dedicated server) it runs nothing")
                elif how == "missing":
                    self.notes.append(f"loader {stage} path '{entry}' does not exist")
        return out

    def _order_report(self) -> list[dict]:
        out = []
        for stage, chosen in self.entries:
            ok, why = self.runs[(stage, chosen.file)][self.selected]
            out.append({"loader": stage, "file": chosen.file, "via": chosen.via, "runs": ok, "why": why})
        return out

    def _compare(self, orders: dict) -> dict:
        mine = [(s, c.file) for s, c in orders[self.file_system]]
        out = {}
        for fs, order in orders.items():
            if fs == self.file_system:
                continue
            theirs = [(s, c.file) for s, c in order]
            if theirs == mine:
                out[fs] = {"same": True}
                continue
            first = next((i for i, (a, b) in enumerate(zip(mine, theirs)) if a != b), min(len(mine), len(theirs)))
            out[fs] = {"same": False, "at": first, "here": mine[first][1] if first < len(mine) else None,
                       "there": theirs[first][1] if first < len(theirs) else None, "files": len(theirs)}
            self.notes.append(f"on {fs} the load order differs from {self.file_system} at position {first + 1}: "
                              f"{out[fs]['there']} instead of {out[fs]['here']}")
        return out

    def _imports(self) -> dict:
        """Edges between scripts (a script imports or uses a class another declares), cycles among the scripts
        that run and those they import, and imports of the folder's own packages that name no class."""
        declared = {name: s.path for s in self.scripts.values() for name in s.classes}
        by_path = {logs.class_name(path): path for path in self.scripts}
        roots = {name.split(".")[0] for name in [*declared, *by_path] if "." in name}
        packages: dict[str, dict[str, str]] = defaultdict(dict)
        for name, path in declared.items():
            package, _, simple = name.rpartition(".")
            packages[package][simple] = path
        edges: dict[str, set[str]] = defaultdict(set)
        basis: dict[tuple[str, str], str] = {}
        unresolved: list[dict] = []
        for script in self.scripts.values():
            source = script.source
            if source is None:
                continue
            names = {t.text for t in source.tokens if t.kind == NAME}

            def link(target: str | None, why: str, script=script) -> None:
                if target and target != script.path:
                    edges[script.path].add(target)
                    basis.setdefault((script.path, target), why)

            for imp in source.imports:
                owner = imp.name if (imp.star and imp.static) or not imp.static else imp.name.rsplit(".", 1)[0]
                if imp.star and not imp.static:
                    for simple, path in packages.get(imp.name, {}).items():
                        if simple in names:
                            link(path, f"import {imp.name}.*")
                    continue
                target = declared.get(owner) or by_path.get(owner)
                if target:
                    link(target, f"import {imp.name}")
                elif owner.split(".")[0] in roots:
                    unresolved.append({"file": script.path, "line": imp.line, "import": imp.name})
            for simple, path in packages.get(source.package, {}).items():
                if simple in names and path != script.path:
                    link(path, f"{simple}, a class of package {source.package or '(default)'}")
        reach = set(self.active)
        frontier = list(reach)
        while frontier:
            current = frontier.pop()
            for target in edges.get(current, ()):
                if target not in reach:
                    reach.add(target)
                    frontier.append(target)
        self.on_demand = reach - self.active
        live = {k: sorted(v & reach) for k, v in edges.items() if k in reach}
        cycles = []
        for component in components(sorted(reach), live):
            path = cycle_in(component, live)
            cycles.append({"files": component, "cycle": path, "via": [basis[(a, b)] for a, b in zip(path, path[1:])]})
            self.findings.append(f"import cycle: {' -> '.join(path)}")
        for row in unresolved:
            if row["file"] in reach:
                self.findings.append(f"{row['file']}:{row['line']}: imports {row['import']}, which no script of the "
                                     "folder declares")
        return {"edges": [[a, b, basis[(a, b)]] for a in sorted(edges) for b in sorted(edges[a])],
                "cycles": cycles, "unresolved": [r for r in unresolved if r["file"] in reach]}

    def _files_report(self) -> dict:
        out = {}
        for path, script in self.scripts.items():
            stages = self.stages.get(path, [])
            runs_in = [self.label(c) for c in self.configurations if self.runs_in(path, c)]
            row = {"lines": script.lines, "bytes": script.size, "loaders": stages, "runsIn": runs_in,
                   "package": script.source.package if script.source else "", "classes": script.classes,
                   "imports": [i.name + (".*" if i.star else "") for i in script.source.imports] if script.source
                   else [], "preprocessors": [{"name": p.name, "args": p.args, "line": p.line}
                                              for p in script.preprocessors],
                   "additions": sum(1 for o in script.operations if o[2] == "add"),
                   "removals": sum(1 for o in script.operations if o[2] == "remove")}
            out[path] = row
        return out

    def _relevant(self) -> list[Script]:
        return [self.scripts[f] for f in sorted(self.configured | self.on_demand)]

    def _preprocessor_problems(self) -> list[dict]:
        out = []
        loaded = {m.casefold(): m for m in self.mods.ids} if self.mods is not None else {}
        for script in self._relevant():
            for problem in script.flagged:
                out.append({"file": script.path, **problem})
                self.findings.append(f"{script.path}:{problem['line']}: {problem['detail']}")
            for prep in script.preprocessors:
                for arg in prep.args if prep.name == "MODS_LOADED" else []:
                    if self.mods is not None and arg not in self.mods.ids and arg.casefold() in loaded:
                        detail = (f"MODS_LOADED names {arg}, but the mod's id is {loaded[arg.casefold()]}: ids are "
                                  "matched exactly, so the file never runs")
                        out.append({"file": script.path, "line": prep.line, "kind": "mods-loaded", "detail": detail})
                        self.findings.append(f"{script.path}:{prep.line}: {detail}")
        return out

    def _syntax_problems(self) -> list[dict]:
        out = []
        for script in self._relevant():
            for problem in script.problems:
                out.append({"file": script.path, **problem})
            if script.problems:
                first = script.problems[0]
                more = f" ({len(script.problems) - 1} more)" if len(script.problems) > 1 else ""
                self.findings.append(f"{script.path}:{first['line']}: {first['detail']}: Groovy cannot compile "
                                     f"it{more}")
        return out

    def _duplicates(self) -> list[dict]:
        groups: dict[tuple[str, str], list[tuple[str, int]]] = defaultdict(list)
        for path in sorted(self.active):
            for kind, key, line in self.scripts[path].declared:
                groups[(kind, key)].append((path, line))
        out = []
        for (kind, key), places in sorted(groups.items()):
            if len(places) < 2:
                continue
            clash = [c for c in self.configurations if sum(1 for f, _ in places if self.runs_in(f, c)) >= 2]
            if not clash:
                continue
            rows = []
            for file, line in places:
                where = [c for c in clash if self.runs_in(file, c)]
                if where:
                    rows.append({"file": file, "line": line, "configurations": [self.label(c) for c in where]})
            out.append({"kind": kind, "id": key, "places": rows, "configurations": [self.label(c) for c in clash]})
            shown = ", ".join(f"{r['file']}:{r['line']}" + ("" if len(r["configurations"]) == len(clash) else
                                                           f" in {self.brief(r['configurations'])}") for r in rows[:4])
            more = ", ..." if len(rows) > 4 else ""
            self.findings.append(f"{kind} {key} is declared {len(rows)} times ({shown}{more})"
                                 + ("" if len(clash) == len(self.configurations) else
                                    f", together in {self.brief([self.label(c) for c in clash])}"))
        return out

    def brief(self, labels: list[str]) -> str:
        """Configurations named shortly: a packmode on both sides by the packmode alone."""
        out = []
        for label in labels:
            mode = label.split("/")[0] if "/" in label else None
            if mode and all(f"{mode}/{side}" in labels for side in SIDES):
                if mode not in out:
                    out.append(mode)
            else:
                out.append(label)
        return ", ".join(out)

    def _recipes(self) -> dict:
        """Additions and removals as written in the files that run in the selected configuration; builders never
        registered wherever they run."""
        by_file: dict[str, dict] = {}
        by_mod: dict[str, Counter] = defaultdict(Counter)
        by_registry: dict[str, Counter] = defaultdict(Counter)
        unregistered = []
        oredict: Counter = Counter()
        for script in [self.scripts[f] for f in sorted(self.active)]:
            if script.operations and self.runs_in(script.path, self.selected):
                counted: dict[str, Counter] = {"add": Counter(), "remove": Counter()}
                for mod, key, op, _ in script.operations:
                    counted[op][key] += 1
                    by_mod[mod][op] += 1
                    by_registry[key][op] += 1
                by_file[script.path] = {op: dict(sorted(c.items())) for op, c in counted.items() if c}
            for key, line in script.unregistered:
                unregistered.append({"file": script.path, "line": line, "registry": key})
                self.findings.append(f"{script.path}:{line}: a {key} recipe builder is never registered (no register() "
                                     "or buildAndRegister()): it adds nothing")
            if self.runs_in(script.path, self.selected):
                oredict += script.oredict
        return {"configuration": self.label(self.selected), "additions": sum(c["add"] for c in by_mod.values()),
                "removals": sum(c["remove"] for c in by_mod.values()),
                "byFile": by_file, "byMod": {m: dict(c) for m, c in sorted(by_mod.items())},
                "byRegistry": {k: dict(c) for k, c in sorted(by_registry.items())},
                "unregistered": unregistered, "oreDict": dict(oredict)}

    def _references(self) -> dict:
        referenced: Counter = Counter()
        missing: dict[str, list[dict]] = defaultdict(list)
        malformed = []
        checked = self.mods is not None
        known = {m.casefold() for m in (self.mods.ids if self.mods else {})} | \
            {m.casefold() for m in self.platform} | {self.namespace, "minecraft"}
        for script in self._active_scripts():
            for mod, how, line, guarded in script.references:
                referenced[mod] += 1
                if checked and not guarded and mod.casefold() not in known:
                    missing[mod].append({"file": script.path, "line": line, "via": how})
            for how, line in script.malformed:
                malformed.append({"file": script.path, "line": line, "via": how})
                self.findings.append(f"{script.path}:{line}: {how} names no mod: GroovyScript's item() needs mod:name")
        for mod, places in sorted(missing.items()):
            first = places[0]
            files = len({p["file"] for p in places})
            self.findings.append(f"{first['file']}:{first['line']}: refers to mod {mod} ({first['via']}), which the "
                                 f"mod list lacks ({_many(len(places), 'reference')} in {_many(files, 'file')}, none "
                                 "guarded by a mod-loaded check)")
        return {"checked": checked, "modList": self.mods.describe() if self.mods else None,
                "mods": dict(sorted(referenced.items())),
                "missing": [{"mod": m, "references": p} for m, p in sorted(missing.items())], "malformed": malformed}

    def _log(self, path: Path) -> dict:
        resolve = logs.Resolver(list(self.scripts), self.root.name)
        try:
            entries, ran, count = logs.read(path, resolve)
        except (OSError, EOFError) as error:
            raise ValueError(f"log: cannot read {path.name}: {error}") from None
        by_file: dict[str, Counter] = defaultdict(Counter)
        errors, warnings = [], []
        for entry in entries:
            row = {"line": entry.number, "level": entry.level, "message": entry.message[:300],
                   "at": [{"file": f, "line": line} for f, line in entry.places], "unplaced": entry.unplaced}
            if entry.places:
                by_file[entry.places[0][0]][entry.level] += 1
            if not entry.groovy:
                continue
            (errors if entry.level in ("ERROR", "FATAL") else warnings).append(row)
            if entry.level in ("ERROR", "FATAL"):
                where = entry.place() or "no script named"
                self.findings.append(f"log line {entry.number}: {entry.level.lower()} at {where}: "
                                     f"{_clip(entry.message)}")
        predicted = [(stage, file) for stage, chosen in self.entries for file in [chosen.file]
                     if self.runs[(stage, file)][self.selected][0]]
        executed = {"ran": [{"loader": s, "file": f} for s, f in ran]}
        if ran:
            same = [f for _, f in ran] == [f for _, f in predicted]
            executed["matchesOrder"] = same
            if not same:
                at = next((i for i, (a, b) in enumerate(zip(ran, predicted)) if a[1] != b[1]),
                          min(len(ran), len(predicted)))
                executed["differsAt"] = at
                self.notes.append(f"the log ran scripts in another order or set than predicted for "
                                  f"{self.label(self.selected)}, from position {at + 1}: other packmode, side or mods?")
        return {"file": path.name, "lines": count, "errors": errors, "warnings": warnings,
                "other": {"errors": sum(1 for e in entries if not e.groovy and e.level in ("ERROR", "FATAL")),
                          "warnings": sum(1 for e in entries if not e.groovy and e.level == "WARN")},
                "byFile": {f: dict(c) for f, c in sorted(by_file.items())}, **executed}


def _many(count: int, noun: str) -> str:
    return f"1 {noun}" if count == 1 else f"{count} {noun}s"


def _clip(text: str, limit: int = 160) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 3] + "..."
