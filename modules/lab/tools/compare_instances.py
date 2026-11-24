"""Compare an instance lab.build made with one the old route prepared from the same launch file: the proof of phase 7
that both routes build the same game.

    python modules/lab/tools/compare_instances.py --new <instance> --old <old instance> --old-tools <folder>
        [--cache <runtime cache>] [--json <report.json>]

``--new`` is a folder with instance.json; ``--old`` one with the old route's standalone.json; ``--old-tools`` the
folder holding the old route's lab_process.py, whose ``argv`` gives the old route's command lines (it is run on a copy
of the manifest pointed at a scratch folder, so nothing is written into the old instance); ``--cache`` the runtime
cache the new instance's ``{cache}`` stands for, which the class-path check reads (without it, that check is
skipped). Nothing is started and neither instance is changed.

Three comparisons, each printed with what differs and, for the differences this tool knows, why:

- files: every file of both instance folders by relative path and SHA-256, a file found under another name with the
  same bytes reported as renamed; and the client's libraries and client JAR by their path under the cache and their
  SHA-1;
- the server's and client's command lines token by token, after the old route's instance folder, runtime cache and
  JDK home are written as ``{instance}``, ``{cache}`` and ``{jdk:N}`` and path separators as ``/``; the class path is
  compared entry by entry, and where only its order differs, the JARs are searched for entries (classes, resources,
  service files) found in more than one: only those make the order matter, and each is reported with the JAR that
  serves it first in either order;
- the player name and the class-path order, named on their own because the proof names them.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib
import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path

# The files the routes keep differently by design, and why. Matched on the relative path.
EXPECTED = {
    "standalone.json": "the old route's manifest, with absolute paths; the new route's receipt is instance.json, "
                       "with paths relative to {instance}, {cache} and {jdk:N}",
    "installer.log": "the installer's log; the new route keeps it in the job's out folder, not in the instance",
    "client/client-runtime.json": "the old client runtime with absolute paths; instance.json's runtime and client "
                                  "command carry it",
    "client/resolved-components.json": "the old copy of the launcher components; instance.json names each "
                                       "component by SHA-256",
    "instance.json": "the new route's receipt",
    ".wb-instance.json": "the instance store's marker (template and hard links)",
    "launch.json": "the launch file the instance was built from, by its bytes",
    "server/eula.txt": "the EULA is accepted only when the request says so (lab.build eula=true); the old route "
                       "always wrote it",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def tree(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): sha256(p) for p in sorted(root.rglob("*")) if p.is_file()}


def compare_trees(old: dict[str, str], new: dict[str, str], same_folder: bool = True) -> dict:
    equal = sorted(p for p in old.keys() & new.keys() if old[p] == new[p])
    differ = sorted(p for p in old.keys() & new.keys() if old[p] != new[p])
    only_old = sorted(old.keys() - new.keys())
    only_new = sorted(new.keys() - old.keys())
    renamed = []
    for path in list(only_old):
        twins = [p for p in only_new
                 if new[p] == old[path] and (not same_folder or Path(p).parent == Path(path).parent)]
        if len(twins) == 1:
            renamed.append({"old": path, "new": twins[0], "sha256": old[path]})
            only_old.remove(path)
            only_new.remove(twins[0])
    return {"equal": equal, "differ": differ, "only_old": only_old, "only_new": only_new, "renamed": renamed}


def old_commands(old: Path, manifest: dict, tools: Path) -> dict[str, list[str]]:
    """The old route's command lines for its instance, made by its own argv on a copy pointed at a scratch folder, then
    with the properties the product's play adds (its launch's JVM properties first, the lab's loader flags removed)."""
    sys.path.insert(0, str(tools))
    process = importlib.import_module("lab_process")
    out = {}
    with tempfile.TemporaryDirectory() as scratch:
        mirror = copy.deepcopy(manifest)
        for side in ("server", "client"):
            mirror[side] = str(Path(scratch) / side)
            (Path(scratch) / side).mkdir()
        # argv looks for the installer's argument file in the server folder: the mirror has it where the old one does
        version = manifest.get("runtimeVersion") or manifest["release"]["tag_name"]
        relative = Path("libraries/com/cleanroommc/cleanroom") / version / ("win_args.txt" if os.name == "nt"
                                                                           else "unix_args.txt")
        if (Path(manifest["server"]) / relative).is_file():
            (Path(scratch) / "server" / relative).parent.mkdir(parents=True)
            (Path(scratch) / "server" / relative).write_text("")
        for side, client in (("server", False), ("client", True)):
            argv = [a for a in process.argv(mirror, client) if not a.startswith("-Dcubiclab.")]
            properties = manifest.get(f"{side}Properties") or {}
            argv[1:1] = [f"-D{k}={v}" for k, v in properties.items()]
            out[side] = [a.replace(str(Path(scratch)), str(old)) for a in argv]
    return out


def normalise(argv: list[str], roots: list[tuple[str, str]]) -> list[str]:
    """Each root written as its placeholder (the longest first), separators as /."""
    out = []
    for token in argv:
        token = token.replace("\\", "/")
        for path, name in sorted(roots, key=lambda r: -len(r[0])):
            token = token.replace(path.replace("\\", "/").rstrip("/"), name)
        out.append(token)
    return out


def classpath_of(argv: list[str]) -> list[str] | None:
    if "-cp" not in argv:
        return None
    return argv[argv.index("-cp") + 1].split(";" if ";" in argv[argv.index("-cp") + 1] else os.pathsep)


def overlapping_classes(jars: list[Path]) -> dict[str, list[str]]:
    """Entries (classes, resources outside META-INF, service files) found in more than one of ``jars``."""
    owners: dict[str, list[str]] = {}
    for jar in jars:
        try:
            with zipfile.ZipFile(jar) as archive:
                names = {n for n in archive.namelist() if not n.endswith("/") and n != "module-info.class"
                         and (not n.startswith("META-INF/") or n.startswith("META-INF/services/"))}
        except (OSError, zipfile.BadZipFile):
            continue
        for name in names:
            owners.setdefault(name, []).append(jar.name)
    return {name: jars for name, jars in sorted(owners.items()) if len(jars) > 1}


def _without_classpath(argv: list[str]) -> list[str]:
    if "-cp" not in argv:
        return list(argv)
    at = argv.index("-cp") + 1
    return argv[:at] + ["<classpath>"] + argv[at + 1:]


def compare_commands(old: list[str], new: list[str], cache: Path | None, moved: dict[str, str]) -> dict:
    """``moved``: class-path entries the cache keeps at another path with the same bytes, old to new."""
    report = {"equal": old == new, "tokens": [len(old), len(new)], "differences": []}
    old_cp, new_cp = classpath_of(old), classpath_of(new)
    if old_cp is not None and new_cp is not None:
        old_cp = [moved.get(entry, entry) for entry in old_cp]
        report["classpath"] = {"entries": [len(old_cp), len(new_cp)], "moved": moved,
                               "same_entries": sorted(old_cp) == sorted(new_cp),
                               "same_order": old_cp == new_cp,
                               "only_old": sorted(set(old_cp) - set(new_cp)),
                               "only_new": sorted(set(new_cp) - set(old_cp))}
        if report["classpath"]["same_entries"] and not report["classpath"]["same_order"]:
            first = next(i for i, (a, b) in enumerate(zip(old_cp, new_cp)) if a != b)
            report["classpath"]["first_moved"] = {"index": first, "old": old_cp[first], "new": new_cp[first]}
            if cache is not None:
                # The order decides only which JAR serves an entry that more than one holds: the first one.
                jars = [cache / entry.removeprefix("{cache}/") for entry in new_cp if entry.startswith("{cache}/")]
                overlap = overlapping_classes(jars)
                rank = {"old": {Path(e).name: i for i, e in enumerate(old_cp)},
                        "new": {Path(e).name: i for i, e in enumerate(new_cp)}}
                first = {name: {order: min(owners, key=rank[order].__getitem__) for order in rank}
                         for name, owners in overlap.items()}
                report["classpath"]["entries_in_two_jars"] = len(overlap)
                report["classpath"]["overlap_sample"] = {name: {"jars": overlap[name], **first[name]}
                                                         for name in list(overlap)[:10]}
                report["classpath"]["served_by_another_jar"] = sorted(n for n, f in first.items()
                                                                      if f["old"] != f["new"])
    a, b = _without_classpath(old), _without_classpath(new)  # the class path is compared entry by entry above
    for index in range(max(len(a), len(b))):
        x = a[index] if index < len(a) else None
        y = b[index] if index < len(b) else None
        if x != y:
            report["differences"].append({"index": index, "old": x, "new": y})
    report["equal_besides_classpath"] = not report["differences"]
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--new", type=Path, required=True)
    parser.add_argument("--old", type=Path, required=True)
    parser.add_argument("--old-tools", type=Path, required=True)
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args(argv)
    receipt = json.loads((args.new / "instance.json").read_text(encoding="utf-8"))
    manifest = json.loads((args.old / "standalone.json").read_text(encoding="utf-8"))
    report = {"new": str(args.new), "old": str(args.old)}

    # files in the instance folders
    files = compare_trees(tree(args.old), tree(args.new))
    files["expected"] = {p: EXPECTED[p] for p in files["only_old"] + files["only_new"] + files["differ"]
                         if p in EXPECTED}
    report["files"] = files

    # the client runtime in the caches: by path under the cache and SHA-1
    runtime = manifest["clientRuntime"]
    old_cache = Path(runtime["assets"]).parent
    old_libraries = {Path(l["path"]).relative_to(old_cache).as_posix(): l["sha1"] for l in runtime["libraries"]}
    old_libraries[Path(runtime["minecraft"]).relative_to(old_cache).as_posix()] = runtime["minecraft_sha1"]
    new_libraries = {k.removeprefix("{cache}/"): v["sha1"] for k, v in receipt["files"].items()
                     if k.startswith("{cache}/libraries/")}
    cache = compare_trees(old_libraries, new_libraries, same_folder=False)
    report["cache"] = {"equal": len(cache["equal"]), "differ": cache["differ"], "only_old": cache["only_old"],
                       "only_new": cache["only_new"], "renamed": cache["renamed"],
                       "assets": {"old": Path(runtime["assets"]).relative_to(old_cache).as_posix(),
                                  "old_index": runtime["assetIndex"], "new_index": receipt["runtime"]["asset_index"]}}

    # the command lines
    major = str(next(iter(receipt["jdk"])))
    old_java_home = Path(manifest["java"]).parent.parent
    roots = [(str(args.old), "{instance}"), (str(old_cache), "{cache}"), (str(old_java_home), f"{{jdk:{major}}}")]
    olds = old_commands(args.old, manifest, args.old_tools)
    report["commands"] = {}
    for side in ("server", "client"):
        old = normalise(olds[side], roots)
        # The port is a session's input: the old route's instance fixed one, so compare with it filled in.
        port = str(manifest.get("port", ""))
        new = [t.replace("\\", "/").replace("{port}", port) for t in receipt[side]["command"]]
        moved = {"{cache}/" + item["old"]: "{cache}/" + item["new"] for item in cache["renamed"]}
        report["commands"][side] = compare_commands(old, new, args.cache, moved)
        report["commands"][side]["old"] = old
        report["commands"][side]["new"] = new
    client_old, client_new = report["commands"]["client"]["old"], report["commands"]["client"]["new"]
    name = lambda argv: argv[argv.index("--username") + 1] if "--username" in argv else None
    report["player"] = {"old": name(client_old), "new": name(client_new), "equal": name(client_old) == name(client_new)}

    text = render(report)
    print(text)
    if args.json:
        args.json.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    return 0


def render(report: dict) -> str:
    files, cache = report["files"], report["cache"]
    lines = [f"files: {len(files['equal'])} equal, {len(files['differ'])} differ, {len(files['renamed'])} renamed, "
             f"{len(files['only_old'])} only in the old instance, {len(files['only_new'])} only in the new one"]
    for key, label in (("differ", "differs"), ("only_old", "only old"), ("only_new", "only new")):
        for path in files[key]:
            lines.append(f"  {label}: {path}" + (f" -- {files['expected'][path]}" if path in files["expected"] else ""))
    for item in files["renamed"]:
        lines.append(f"  renamed: {item['old']} -> {item['new']} (same SHA-256 {item['sha256'][:12]})")
    lines.append(f"cache: {cache['equal']} client files equal by path and SHA-1, {len(cache['differ'])} differ, "
                 f"{len(cache['renamed'])} at another path, {len(cache['only_old'])} only old, "
                 f"{len(cache['only_new'])} only new")
    for item in cache["renamed"]:
        lines.append(f"  moved: {item['old']} -> {item['new']}")
    for key in ("differ", "only_old", "only_new"):
        for path in cache[key]:
            lines.append(f"  {key}: {path}")
    for side in ("server", "client"):
        c = report["commands"][side]
        lines.append(f"{side} command: {c['tokens'][0]} and {c['tokens'][1]} tokens, "
                     + ("equal" if c["equal"] else f"{len(c['differences'])} token(s) differ besides the class path"
                        if c["differences"] else "equal besides the class path"))
        for d in c["differences"]:
            lines.append(f"  [{d['index']}] old {d['old']!r}  new {d['new']!r}")
        cp = c.get("classpath")
        if cp:
            for before, after in cp["moved"].items():
                lines.append(f"  class path: {before} is {after} in the new cache (same SHA-1)")
            lines.append(f"  class path: {cp['entries'][0]} and {cp['entries'][1]} entries, "
                         f"{'the same' if cp['same_entries'] else 'different'} entries, "
                         f"{'the same order' if cp['same_order'] else 'another order'}")
            if "first_moved" in cp:
                lines.append(f"    first moved at {cp['first_moved']['index']}: old {cp['first_moved']['old']}, "
                             f"new {cp['first_moved']['new']}")
            if "entries_in_two_jars" in cp:
                lines.append(f"    entries found in more than one JAR: {cp['entries_in_two_jars']}; served by another "
                             f"JAR in the new order: {len(cp['served_by_another_jar'])}")
                for entry, item in cp["overlap_sample"].items():
                    lines.append(f"      {entry}: {', '.join(item['jars'])} "
                                 f"(first: old {item['old']}, new {item['new']})")
                for entry in cp["served_by_another_jar"][:10]:
                    lines.append(f"      served by another JAR: {entry}")
    p = report["player"]
    lines.append(f"player: old {p['old']}, new {p['new']} ({'equal' if p['equal'] else 'different'})")
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
