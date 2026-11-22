"""Prove that a production (reobfuscated) JAR's Mixins were remapped, from the JAR's bytes alone.

A valid refmap does not prove that the packaged classes were renamed: the injection selectors go through
the refmap, but a shadowed or overwritten member, and every reference to one, must already carry its
production name in the class file. Each check below reports a member still under its development name:

- **refmap**: each configuration's refmap is packaged and readable; no entry maps a selector to a
  development name, or to another name than the mappings give; a member that keeps its native name
  (``native``) has no entry; ``expect`` pins entries.
- **member**: every member the mappings rename in a class of the JAR is declared under its production
  name (the annotation processor's mapping of the Mixins' own shadow and overwrite members).
- **shadow**: no ``@Shadow`` or ``@Overwrite`` member of a Mixin is declared under a development name of
  its target's member.
- **reference**: no class refers to a member by a development name.

Development and production names come from the mapping files and from the refmaps' own pairs. A
reference through a subclass of the member's owner is not matched (the hierarchy is not read).
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from wb_jar import classes, mixins
from wb_jar.archive import Archive

from .mappings import Mappings

KEPT = 200  # failures and unverified entries kept per check; the counts are whole


def _dotted(name: str) -> str:
    return name.replace("/", ".")


class Proof:
    def __init__(self) -> None:
        self.failures: list[dict] = []
        self.counts: Counter = Counter()
        self.checked: Counter = Counter()
        self.unverified: list[str] = []

    def fail(self, check: str, where: str, detail: str) -> None:
        self.counts[check] += 1
        if self.counts[check] <= KEPT:
            self.failures.append({"check": check, "where": where, "detail": detail})


def prove(archive: Archive, mappings: Mappings, *, refmap_name: str | None = None, environment: str | None = None,
          owners: list[str] = (), expect: dict[str, dict[str, str]] | None = None,
          native: list[str] = ()) -> dict[str, Any]:
    proof = Proof()
    configs, errors = mixins.configs(archive)
    for error in errors:
        proof.fail("config", error.split(":", 1)[0], error)
    names = [refmap_name] if refmap_name else list(dict.fromkeys(c.refmap for c in configs if c.refmap))
    tables: dict[str, dict[str, dict[str, str]]] = {}
    for name in names:
        if name not in archive.entries:
            proof.fail("refmap", name, "the refmap is not packaged")
            continue
        table, error = mixins.refmap(archive, name, environment)
        if error:
            proof.fail("refmap", name, error)
        else:
            tables[name] = table
    refmap_of = {}
    for config in configs:
        for _, name in config.classes():
            refmap_of[name] = refmap_name or config.refmap
    merged: dict[str, dict[str, str]] = {}
    for table in tables.values():
        for mixin, entries in table.items():
            for key, value in entries.items():
                merged.setdefault(mixin, {}).setdefault(key, value)

    infos: dict[str, classes.ClassInfo] = {}
    for entry, info in archive.each_class():
        if info.this_class and not entry.startswith("META-INF/versions/"):
            infos.setdefault(info.this_class, info)

    # What is renamed, in production names: (owner, development name, descriptor) -> production name.
    methods: dict[tuple[str, str, str], str] = {}
    fields: dict[tuple[str, str], str] = {}
    for (owner, name, descriptor), production in mappings.methods.items():
        if production != name:
            methods.setdefault((mappings.owner(owner), name, mappings.descriptor(descriptor)), production)
    for (owner, name), production in mappings.fields.items():
        if production != name:
            fields.setdefault((mappings.owner(owner), name), production)
    for entries in merged.values():
        for key, value in entries.items():
            _, k_name, _ = mixins.selector(key)
            v_owner, v_name, v_descriptor = mixins.selector(value)
            if v_owner and k_name and k_name != v_name and k_name not in mixins.CONSTRUCTORS:
                if v_descriptor.startswith("("):
                    methods.setdefault((v_owner, k_name, v_descriptor), v_name)
                else:
                    fields.setdefault((v_owner, k_name), v_name)
    production_methods = {(o, p, d) for (o, _, d), p in methods.items()}
    production_fields = {(o, p) for (o, _), p in fields.items()}
    production_classes = set(mappings.classes.values())
    simple_native = {n.strip() for n in native if "::" not in n}
    owned_native = {(classes.internal(n.split("::", 1)[0]), n.split("::", 1)[1].strip()) for n in native if "::" in n}

    def is_native(owner: str, name: str) -> bool:
        return name in simple_native or (owner, name) in owned_native

    _refmaps(proof, tables, methods, fields, production_classes, mappings, is_native)
    _expectations(proof, merged, expect or {})
    _members(proof, infos, mappings, [o.replace(".", "/") for o in owners])
    _shadows(proof, infos, tables, refmap_of, merged, methods, fields, production_methods, production_fields,
             is_native)
    _references(proof, infos, methods, fields)
    if not (proof.checked["refmap"] + proof.checked["members"] + proof.checked["shadows"] + proof.checked["expect"]):
        proof.fail("nothing", archive.label, "nothing to prove: no refmap entry, renamed member, shadow or "
                                             "overwrite was found to check")
    return {"schema": "wb.mixin.remap-proof/1", "jar": archive.label, "configs": [c.as_json() for c in configs],
            "refmaps": sorted(tables), "environment": environment, "mappings": len(mappings),
            "checked": dict(proof.checked), "proved": not proof.counts, "failures": proof.failures,
            "failure_counts": dict(proof.counts), "unverified": proof.unverified[:KEPT]}


def _refmaps(proof: Proof, tables, methods, fields, production_classes, mappings: Mappings, is_native) -> None:
    for refmap_name, table in sorted(tables.items()):
        for mixin, entries in sorted(table.items()):
            for key, value in sorted(entries.items()):
                proof.checked["refmap"] += 1
                where = f"{_dotted(mixin)}: {key}"
                k_owner, k_name, k_descriptor = mixins.selector(key)
                v_owner, v_name, v_descriptor = mixins.selector(value)
                owner = v_owner or k_owner
                if is_native(owner, k_name):
                    proof.fail("native", where, f"keeps its native name, but {refmap_name} maps it to {value}")
                    continue
                if v_owner or v_descriptor:  # a member
                    if v_descriptor.startswith("("):
                        renamed = methods.get((v_owner, v_name, v_descriptor))
                        expected = methods.get((v_owner, k_name, v_descriptor))
                    else:
                        renamed = fields.get((v_owner, v_name))
                        expected = fields.get((v_owner, k_name))
                    if renamed:
                        proof.fail("refmap", where, f"maps it to {value}, a development name ({renamed} in production)")
                    elif expected and expected != v_name:
                        proof.fail("refmap", where, f"maps it to {value}; the mappings name it {expected}")
                    elif k_name == v_name and k_name not in mixins.CONSTRUCTORS:
                        proof.unverified.append(f"{where}: kept as {v_name}, which no mapping renames")
                elif "/" in value or "." in value:  # a class
                    dev = classes.internal(value)
                    if dev in mappings.classes and dev not in production_classes:
                        proof.fail("refmap", where, f"maps it to {value}, a development class name "
                                                    f"({_dotted(mappings.classes[dev])} in production)")


def _expectations(proof: Proof, merged: dict, expect: dict) -> None:
    for mixin, wanted in sorted(expect.items()):
        name = classes.internal(mixin)
        for key, value in sorted(wanted.items()):
            proof.checked["expect"] += 1
            actual = merged.get(name, {}).get(key)
            where = f"{_dotted(name)}: {key}"
            if not value:
                if actual is not None:
                    proof.fail("expect", where, f"must keep its native name, but the refmap maps it to {actual}")
            elif actual != value:
                proof.fail("expect", where, f"expected {value}, the refmap has {actual or 'no entry'}")


def _members(proof: Proof, infos: dict, mappings: Mappings, owners: list[str]) -> None:
    def required(name: str) -> bool:
        return any(name.startswith(prefix) for prefix in owners)

    missing: set[str] = set()
    entries = [((owner, name, None), production) for (owner, name), production in mappings.fields.items()]
    entries += [((owner, name, descriptor), production) for (owner, name, descriptor), production in mappings.methods.items()]
    for (owner, name, descriptor), production in sorted(entries, key=lambda e: (e[0][0], e[0][1], e[0][2] or "")):
        target = mappings.owner(owner)
        info = infos.get(target)
        if info is None:
            if required(target) and target not in missing:
                missing.add(target)
                proof.fail("member", _dotted(target), "the mappings rename its members, but the class is not in the JAR")
            continue
        if production == name:
            continue
        proof.checked["members"] += 1
        if descriptor is None:
            declared = {f.name for f in info.declared_fields}
            where = f"{_dotted(target)}#{name}"
            done = production in declared
            dev = name in declared
        else:
            mapped = mappings.descriptor(descriptor)
            declared = {(m.name, m.descriptor) for m in info.declared_methods}
            where = f"{_dotted(target)}::{name}{descriptor}"
            done = (production, mapped) in declared
            dev = (name, mapped) in declared or (name, descriptor) in declared
        if not done:
            proof.fail("member", where, f"still under its development name, not {production}" if dev
                       else f"declared under neither {name} nor {production}")


def _shadows(proof: Proof, infos, tables, refmap_of, merged, methods, fields, production_methods, production_fields,
             is_native) -> None:
    for name, info in sorted(infos.items()):
        table = tables.get(refmap_of.get(name), merged)
        mixin = mixins.mixin_of(info, table)
        if mixin is None or not mixin.remap:
            continue
        for member in [*info.declared_methods, *info.declared_fields]:
            annotation = member.annotation(mixins.SHADOW, mixins.OVERWRITE)
            if annotation is None or annotation.get("remap", True) is False:
                continue
            kind = "shadows" if annotation.type == mixins.SHADOW else "overwrites"
            prefix = annotation.get("prefix", "shadow$") if annotation.type == mixins.SHADOW else ""
            shown = member.name[len(prefix):] if prefix and member.name.startswith(prefix) else member.name
            is_method = member.descriptor.startswith("(")
            where = f"{_dotted(name)}.{member.name}"
            verified = False
            # The targets' members, then the Mixin's own (the annotation processor's mapping names those;
            # one still under its development name is a member failure already).
            for target in [*mixin.targets, name]:
                if is_method:
                    renamed = methods.get((target, shown, member.descriptor))
                    known = (target, member.name if target == name else shown, member.descriptor) in production_methods
                else:
                    renamed = fields.get((target, shown))
                    known = (target, member.name if target == name else shown) in production_fields
                if renamed and target != name:
                    proof.fail("shadow", where, f"{kind} {_dotted(target)}.{shown} under its development name "
                                                f"({renamed} in production)")
                verified = verified or bool(renamed) or known
            if verified:
                proof.checked["shadows"] += 1
            elif not any(is_native(t, shown) for t in mixin.targets):
                proof.unverified.append(f"{where}: {kind} {shown}, which no mapping names")


def _references(proof: Proof, infos, methods, fields) -> None:
    for name, info in sorted(infos.items()):
        seen = set()
        for index in info.member_refs:
            owner, member, descriptor = info.ref(index)
            proof.checked["references"] += 1
            renamed = methods.get((owner, member, descriptor)) if descriptor.startswith("(") else fields.get((owner, member))
            if renamed and (owner, member, descriptor) not in seen:
                seen.add((owner, member, descriptor))
                proof.fail("reference", _dotted(name), f"refers to {_dotted(owner)}.{member}"
                                                       f"{descriptor if descriptor.startswith('(') else ''} by its "
                                                       f"development name ({renamed} in production)")
