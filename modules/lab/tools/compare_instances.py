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
  same bytes reported as renamed, and the server's properties compared again with the old route's fixed port written
  where the new template has its default (a session writes its own port); and the client's libraries and client JAR by
  their path under the cache and their SHA-1;
- the server's and client's command lines token by token, after the old route's instance folder, runtime cache and
  JDK home are written as ``{instance}``, ``{cache}`` and ``{jdk:N}`` and path separators as ``/``; the class path is
  compared entry by entry, and where only its order differs, the JARs are searched for entries (classes, resources,
  service files) found in more than one: only those make the order matter, and each is reported with the JAR that
  serves it first in either order;
- the player name and the class-path order, named on their own because the proof names them.

The proof of phase 8, that one launch file built on the host and in the container gives the same instance tree apart
from natives, compares two instances lab.build made:

    python modules/lab/tools/compare_instances.py --host <instance> --container <instance | instance .tar>
        [--json <report.json>]

Every file of both by relative path and SHA-256. A difference is explained when it is a native (under
``client/natives``, or a .dll, .so, .dylib or .jnilib: each system's own), a text file whose bytes are equal once
CRLF is written as LF (the builder writing a generated file in its system's line endings), or the receipt and the
store's marker, which name their system, paths and template by design; the receipts' file lists are compared too,
apart from natives. Anything else is unexplained, and the exit code is 1.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib
import json
import os
import re
import sys
import tarfile
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


PROPERTIES = "server/server.properties"


def same_but_port(old: Path, new: Path, files: dict, manifest: dict, receipt: dict) -> list[str]:
    """The server's properties, when they differ only by the port: the old route fixed one per instance, the new
    template has its default and a session writes its own. Such a file moves from ``differ`` to the returned list."""
    if PROPERTIES not in files["differ"] or not manifest.get("port"):
        return []
    default = (receipt.get("session") or {}).get("default_port")
    text = (new / PROPERTIES).read_bytes().replace(f"server-port={default}".encode(),
                                                   f"server-port={manifest['port']}".encode(), 1)
    if hashlib.sha256(text).hexdigest() != sha256(old / PROPERTIES):
        return []
    files["differ"].remove(PROPERTIES)
    return [PROPERTIES]


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


# --- two instances lab.build made, on two systems --------------------------------------------------------------------

NATIVE_SUFFIXES = (".dll", ".so", ".dylib", ".jnilib")
# A JAR of natives for one system, by its classifier: lwjgl-3.4.3-natives-windows.jar, netty's -linux-x86_64.jar.
NATIVE_JAR = re.compile(r"-natives-(windows|linux|macos|osx)[^/]*\.jar$"
                        r"|-(windows|linux|osx|macos)-(x86_64|x86|aarch_64|aarch64|arm64)\.jar$")
# The files that name their own system, paths and template by design.
BY_SYSTEM = {
    "instance.json": "the receipt: it names the system, the template's key and each side's command line",
    ".wb-instance.json": "the instance store's marker: the template's folder and key on this machine",
    "server/run.bat": "the installer's start script for Windows",
    "server/run.sh": "the installer's start script for Linux and macOS",
}
# The installer's argument files: the same libraries but each system's natives.
ARGUMENTS = re.compile(r"(^|/)(win|unix)_args\.txt$")
TOKEN = re.compile(r"[\s;:]+")


def is_native(path: str) -> bool:
    return (path.startswith("client/natives/") or "/natives/" in path or path.lower().endswith(NATIVE_SUFFIXES)
            or bool(NATIVE_JAR.search(path.replace("\\", "/"))))


def without_natives(data: bytes) -> list[str]:
    """A text file's tokens with path separators as /, the native JARs it names left out."""
    text = data.decode("utf-8", "replace").replace("\\", "/")
    return [token for token in TOKEN.split(text) if token and not NATIVE_JAR.search(token)]


def instance_files(source: Path) -> dict[str, bytes]:
    """An instance's files by relative path, read from its folder or from a tar of it (one top folder)."""
    if source.is_dir():
        return {p.relative_to(source).as_posix(): p.read_bytes() for p in sorted(source.rglob("*")) if p.is_file()}
    out = {}
    with tarfile.open(source) as archive:
        for member in archive.getmembers():
            if not (member.isfile() or member.islnk()):
                continue
            name = member.name.split("/", 1)[1] if "/" in member.name else member.name
            out[name] = archive.extractfile(member).read()
    return out


def receipt_files(receipt: dict) -> dict[str, str]:
    """The receipt's file list (path relative to a root, SHA-256)."""
    return {path: (entry.get("sha256") if isinstance(entry, dict) else entry)
            for path, entry in (receipt.get("files") or {}).items()}


def classify(path: str, host: bytes | None, container: bytes | None) -> tuple[str, str] | None:
    """Why a file differs between the systems, or None when nothing explains it."""
    if is_native(path):
        return "natives", "each system's own"
    if path in BY_SYSTEM:
        return "by_system", BY_SYSTEM[path]
    if host is None or container is None:
        return None
    if host.replace(b"\r\n", b"\n") == container.replace(b"\r\n", b"\n"):
        return "line_endings", "written in the building system's line endings"
    if ARGUMENTS.search(path) and without_natives(host) == without_natives(container):
        return "arguments", "the installer's argument file: the same entries but each system's native JARs"
    return None


def compare_peers(host: dict[str, bytes], container: dict[str, bytes]) -> dict:
    digest = {side: {path: hashlib.sha256(data).hexdigest() for path, data in files.items()}
              for side, files in (("host", host), ("container", container))}
    trees = compare_trees(digest["host"], digest["container"], same_folder=False)
    report = {"files": [len(host), len(container)], "equal": len(trees["equal"]), "natives": [], "line_endings": [],
              "arguments": [], "by_system": {}, "unexplained": []}
    why = {}

    def place(path: str, a: bytes | None, b: bytes | None, missing: str | None = None):
        found = classify(path, a, b)
        if found is None:
            report["unexplained"].append({"path": path, "why": missing or "differs"})
            return
        kind, reason = found
        why[path] = kind
        if kind == "by_system":
            report["by_system"][path] = reason
        else:
            report[kind].append(path)

    for path in trees["differ"]:
        place(path, host[path], container[path])
    for key, side in (("only_old", "host"), ("only_new", "container")):
        for path in trees[key]:
            place(path, None, None, f"only on the {side}")
    for item in trees["renamed"]:
        place(item["old"], None, None, f"the same bytes are {item['new']} in the container")
    receipts = {}
    for side, files in (("host", host), ("container", container)):
        try:
            receipts[side] = json.loads(files["instance.json"])
        except (KeyError, ValueError):
            receipts[side] = {}
    listed = compare_trees(receipt_files(receipts["host"]), receipt_files(receipts["container"]))
    explained, jdk = [], []
    for path in listed["differ"] + listed["only_old"] + listed["only_new"]:
        inside = path.removeprefix("{instance}/")
        if path.startswith("{jdk:"):
            jdk.append(path)  # the receipt's JDK is the launch's build for each system: its own files
        elif is_native(path) or (inside != path and inside in why):
            explained.append(path)
        else:
            report["unexplained"].append({"path": f"instance.json files: {path}",
                                          "why": "the receipts list it differently"})
    report["receipt"] = {"system": [receipts["host"].get("system"), receipts["container"].get("system")],
                         "key": [receipts["host"].get("key"), receipts["container"].get("key")],
                         "files_equal": len(listed["equal"]), "files_differ": len(listed["differ"]),
                         "only_host": len(listed["only_old"]), "only_container": len(listed["only_new"]),
                         "explained": explained, "jdk": jdk}
    report["natives"].sort()
    report["same_apart_from_natives"] = not report["unexplained"]
    return report


def render_peers(report: dict) -> str:
    receipt = report["receipt"]
    lines = [f"files: {report['files'][0]} on the host, {report['files'][1]} in the container; {report['equal']} equal "
             f"by SHA-256; {len(report['natives'])} natives (each system's own); {len(report['line_endings'])} equal "
             f"but for line endings; {len(report['arguments'])} argument files equal but for natives; "
             f"{len(report['by_system'])} that name their system; "
             f"{len(report['unexplained'])} unexplained"]
    lines += [f"  native: {path}" for path in report["natives"]]
    lines += [f"  line endings only: {path} (written in the building system's line endings)"
              for path in report["line_endings"]]
    lines += [f"  arguments: {path} (the same entries but each system's native JARs)" for path in report["arguments"]]
    lines += [f"  by system: {path} -- {why}" for path, why in report["by_system"].items()]
    lines += [f"  UNEXPLAINED: {item['path']} ({item['why']})" for item in report["unexplained"]]
    lines.append(f"receipts: systems {receipt['system'][0]} and {receipt['system'][1]}, templates {receipt['key'][0]} "
                 f"and {receipt['key'][1]}; {receipt['files_equal']} listed files equal, {receipt['files_differ']} "
                 f"differ, {receipt['only_host']} only on the host, {receipt['only_container']} only in the "
                 f"container: {len(receipt['explained'])} of them natives or the files above, {len(receipt['jdk'])} "
                 "the JDK's own files for each system")
    lines.append("the same instance tree apart from natives" if report["same_apart_from_natives"]
                 else "NOT the same instance tree apart from natives")
    return "\n".join(lines)


def main_peers(args) -> int:
    report = compare_peers(instance_files(args.host), instance_files(args.container))
    report.update(host=str(args.host), container=str(args.container))
    print(render_peers(report))
    if args.json:
        args.json.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    return 0 if report["same_apart_from_natives"] else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--new", type=Path)
    parser.add_argument("--old", type=Path)
    parser.add_argument("--old-tools", type=Path)
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--host", type=Path, help="an instance lab.build made on the host")
    parser.add_argument("--container", type=Path, help="the same launch's instance built in the container (or a tar)")
    parser.add_argument("--json", type=Path)
    args = parser.parse_args(argv)
    if args.host or args.container:
        if not (args.host and args.container):
            parser.error("--host and --container go together")
        return main_peers(args)
    if not (args.new and args.old and args.old_tools):
        parser.error("--new, --old and --old-tools go together (or --host and --container)")
    receipt = json.loads((args.new / "instance.json").read_text(encoding="utf-8"))
    manifest = json.loads((args.old / "standalone.json").read_text(encoding="utf-8"))
    report = {"new": str(args.new), "old": str(args.old)}

    # files in the instance folders
    files = compare_trees(tree(args.old), tree(args.new))
    files["expected"] = {p: EXPECTED[p] for p in files["only_old"] + files["only_new"] + files["differ"]
                         if p in EXPECTED}
    files["port"] = same_but_port(args.old, args.new, files, manifest, receipt)
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
    lines = [f"files: {len(files['equal'])} equal, {len(files.get('port') or [])} equal but the port, "
             f"{len(files['differ'])} differ, {len(files['renamed'])} renamed, "
             f"{len(files['only_old'])} only in the old instance, {len(files['only_new'])} only in the new one"]
    for key, label in (("differ", "differs"), ("only_old", "only old"), ("only_new", "only new")):
        for path in files[key]:
            lines.append(f"  {label}: {path}" + (f" -- {files['expected'][path]}" if path in files["expected"] else ""))
    for item in files["renamed"]:
        lines.append(f"  renamed: {item['old']} -> {item['new']} (same SHA-256 {item['sha256'][:12]})")
    for path in files.get("port") or []:
        lines.append(f"  equal with the old route's port written in: {path} (the template keeps its default port; a "
                     "session writes its own)")
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
