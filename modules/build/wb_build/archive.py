"""build.archive: a file tree packed into a ZIP whose bytes depend on the files' paths and contents alone.

Entries are the tree's regular files in sorted path order, each with one timestamp, mode 0644 and the same
compression; the archive has no folders, comments or extra fields. Beside ``<name>.zip`` go ``<name>.zip.sha256`` and
the manifest ``<name>.json`` (``wb.archive.v1``): every file's path, size and SHA-256, the digest of the set (sorted
``<path>\\0<sha256>\\n`` lines), and what the bytes were made with (a deflated archive is byte-identical wherever
Python and zlib are the same versions; a stored one needs only the same Python). Packing refuses links and special
files (exclude them), names that cannot exist on every system, and an output whose name already holds other bytes:
a rebuild of the same tree is harmless, a different archive under an existing name is never written.

A variant is another archive, ``<name>-<variant>.zip``, of the same files with ``#define`` values frozen in named
text files: a number replaces the value of ``#define NAME <number>`` (one of the choices when its comment lists them,
``// [1 2 3]``), and ``on``/``off`` uncomments or comments out a valueless ``#define NAME``. Each name must be defined
exactly once in its file (include guards, named by ``#ifndef``, do not count).
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import platform
import re
import stat
import zipfile
import zlib
from pathlib import Path

from wb_core.module import Context, EnvironmentProblem, ParamError, sha256_file

from . import checkout as checkouts
from .files import copy_verified, set_digest, sidecar_text, write_atomic

SCHEMA = "wb.archive.v1"
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,99}")
MODE = 0o100644  # a regular file, rw-r--r--
RESERVED = re.compile(r"(?i)(con|prn|aux|nul|com[0-9]|lpt[0-9])(\..*)?")
FORBIDDEN = set('\\:<>"|?*')
DEFINE = re.compile(r"(?P<indent>[ \t]*)(?P<off>//[ \t]*)?#define[ \t]+(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
                    r"(?P<rest>[^\r\n]*)(?P<eol>\r\n|\n|\r)?")
GUARD = re.compile(r"(?m)^[ \t]*#ifndef[ \t]+([A-Za-z_][A-Za-z0-9_]*)[ \t]*\r?$")
NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
CHOICES = re.compile(r"\[([^\]]*)\]")
SYMBOL = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


# ---------------------------------------------------------------------------------------------
# which files

def _glob(pattern: str, what: str) -> re.Pattern:
    """A glob over POSIX paths: ``**`` crosses folders, ``*`` and ``?`` stay within one."""
    text = pattern.replace("\\", "/").strip()
    text = text[2:] if text.startswith("./") else text
    text = text.rstrip("/")
    if not text or text.startswith("/") or ":" in text or ".." in text.split("/"):
        raise ParamError(f"{what}: {pattern!r} must be a glob relative to root")
    regex, i = "", 0
    while i < len(text):
        if text.startswith("**/", i):
            regex, i = regex + "(?:.*/)?", i + 3
        elif text.startswith("**", i):
            regex, i = regex + ".*", i + 2
        elif text[i] == "*":
            regex, i = regex + "[^/]*", i + 1
        elif text[i] == "?":
            regex, i = regex + "[^/]", i + 1
        else:
            regex, i = regex + re.escape(text[i]), i + 1
    return re.compile(regex)


def _matches(path: str, patterns: list[re.Pattern]) -> bool:
    """Whether a glob matches the path or a folder above it (``exclude = ["build"]`` leaves out all of build/)."""
    parts = path.split("/")
    prefixes = ["/".join(parts[:n]) for n in range(1, len(parts) + 1)]
    return any(p.fullmatch(prefix) for p in patterns for prefix in prefixes)


def _linked(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)  # a reparse point


def _collect(root: Path, include: list[re.Pattern], exclude: list[re.Pattern]) -> tuple[list[str], list[str]]:
    """The files to pack (sorted POSIX paths relative to root), and the links and special files that would be."""
    files, refused = [], []
    for folder, dirs, names in os.walk(root):
        here = Path(folder)
        prefix = here.relative_to(root).as_posix()
        prefix = "" if prefix == "." else prefix + "/"
        walk = []
        for name in sorted(dirs):
            relative = prefix + name
            if name == ".git" or _matches(relative, exclude):
                continue
            if _linked(here / name):
                refused.append(relative)
            else:
                walk.append(name)
        dirs[:] = walk
        for name in sorted(names):
            relative = prefix + name
            if name == ".git" or not _matches(relative, include) or _matches(relative, exclude):
                continue
            path = here / name
            if _linked(path) or not stat.S_ISREG(path.lstat().st_mode):
                refused.append(relative)
            else:
                files.append(relative)
    return sorted(files), sorted(refused)


def unportable(paths: list[str]) -> list[str]:
    """Why some of these archive paths could not be extracted on every system: reserved or forbidden names, and names
    (or folder spellings) that differ only in case."""
    problems, files, folders = [], {}, {}
    for path in paths:
        parts = path.split("/")
        bad = next((p for p in parts if p in ("", ".", "..") or p[-1] in ". " or RESERVED.fullmatch(p)
                    or any(c in FORBIDDEN or ord(c) < 32 for c in p)), None)
        if bad is not None:
            problems.append(f"{path}: {bad!r} cannot be a file name on every system")
        key = path.casefold()
        if key in files:
            problems.append(f"{path} and {files[key]} differ only in case")
        files.setdefault(key, path)
        for n in range(1, len(parts)):
            folder = "/".join(parts[:n])
            spelled = folders.setdefault(folder.casefold(), folder)
            if spelled != folder:
                problems.append(f"{path}: its folder {folder} is also spelled {spelled}")
                break
    return problems


# ---------------------------------------------------------------------------------------------
# variants

def freeze_defines(text: str, values: dict[str, str], where: str) -> str:
    """``text`` with each ``#define`` named in ``values`` frozen to its value (a number, on or off)."""
    lines = text.splitlines(keepends=True)
    guards = set(GUARD.findall(text))
    found: dict[str, list[int]] = {}
    for index, line in enumerate(lines):
        match = DEFINE.fullmatch(line)
        if match and match["name"] not in guards:
            found.setdefault(match["name"], []).append(index)
    for key, value in values.items():
        places = found.get(key, [])
        if len(places) != 1:
            raise ParamError(f"{where}: {key} is defined {len(places)} times, not once" if places
                             else f"{where}: no #define {key}")
        match = DEFINE.fullmatch(lines[places[0]])
        body, slashes, comment = match["rest"].partition("//")
        body, eol = body.strip(), match["eol"] or ""
        choices = CHOICES.search(comment)
        if value in ("on", "off"):
            if body or choices:
                raise ParamError(f"{where}: {key} holds a value, so it takes a number, not {value}")
            line = match["indent"] + ("// " if value == "off" else "") + "#define " + key + match["rest"] + eol
        else:
            if match["off"] or not NUMBER.fullmatch(body):
                raise ParamError(f"{where}: {key} is a switch, so it takes on or off, not {value}")
            if choices and value not in choices[1].split():
                raise ParamError(f"{where}: {key} takes one of {choices[1].strip()}, not {value}")
            line = match["indent"] + "#define " + key + " " + value + (" //" + comment if slashes else "") + eol
        lines[places[0]] = line
    return "".join(lines)


def _variants(raw, files: set[str]) -> dict[str, dict[str, dict[str, str]]]:
    if not isinstance(raw, dict):
        raise ParamError("variants: an object {variant: {file: {NAME: value}}}")
    plan = {}
    for variant, edits in sorted(raw.items()):
        if not NAME.fullmatch(str(variant)):
            raise ParamError(f"variant {variant!r}: a name of letters, digits and . _ + -")
        if not isinstance(edits, dict) or not edits:
            raise ParamError(f"variant {variant}: name the files and the values to freeze in them")
        plan[variant] = {}
        for path, values in sorted(edits.items()):
            if path not in files:
                raise ParamError(f"variant {variant}: {path} is not a file of the archive")
            if not isinstance(values, dict) or not values:
                raise ParamError(f"variant {variant}: {path}: give the values to freeze, {{NAME: value}}")
            fixed = {}
            for key, value in values.items():
                text = ("on" if value else "off") if isinstance(value, bool) else str(value)
                if not SYMBOL.fullmatch(str(key)) or not (NUMBER.fullmatch(text) or text in ("on", "off")):
                    raise ParamError(f"variant {variant}: {path}: {key} = {value!r}; a value is a number, on or off")
                fixed[str(key)] = text
            plan[variant][path] = dict(sorted(fixed.items()))
    return plan


# ---------------------------------------------------------------------------------------------
# packing

def _moment(text: str) -> tuple[int, int, int, int, int, int]:
    try:
        moment = datetime.datetime.fromisoformat(text)
    except ValueError:
        raise ParamError(f"timestamp {text!r} is not an ISO date and time (1980-01-01T00:00:00)") from None
    if moment.tzinfo is not None:
        moment = moment.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    if not 1980 <= moment.year <= 2107:
        raise ParamError("timestamp: a ZIP holds times from 1980 to 2107")
    return moment.year, moment.month, moment.day, moment.hour, moment.minute, moment.second // 2 * 2  # 2 s steps


def _pack(target: Path, root: Path, entries: list[str], digests: dict[str, str], replaced: dict[str, bytes],
          when: tuple, method: int) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    level = 9 if method == zipfile.ZIP_DEFLATED else None
    with zipfile.ZipFile(target, "w", allowZip64=True) as bundle:
        for relative in entries:
            if relative in replaced:
                data = replaced[relative]
            else:
                data = (root / relative).read_bytes()
                if hashlib.sha256(data).hexdigest() != digests[relative]:
                    raise EnvironmentProblem(f"{relative} changed while it was packed; nothing was written")
            info = zipfile.ZipInfo(relative, when)
            info.create_system = 3  # Unix, so readers take the mode
            info.external_attr = MODE << 16
            bundle.writestr(info, data, compress_type=method, compresslevel=level)


def _record(name: str, path: Path, entries: list[str], digests: dict[str, str], sizes: dict[str, int],
            replaced: dict[str, bytes], when: tuple, compression: str) -> dict:
    files = []
    for relative in entries:
        data = replaced.get(relative)
        files.append({"path": relative, "bytes": len(data) if data is not None else sizes[relative],
                      "sha256": hashlib.sha256(data).hexdigest() if data is not None else digests[relative]})
    made_with = {"python": platform.python_version()}
    if compression == "deflate":
        made_with["zlib"] = zlib.ZLIB_RUNTIME_VERSION
    return {"schema": SCHEMA, "archive": name, "sha256": sha256_file(path), "bytes": path.stat().st_size,
            "tree_sha256": set_digest({f["path"]: f["sha256"] for f in files}), "files": files,
            "entries": {"timestamp": "%04d-%02d-%02dT%02d:%02d:%02d" % when, "mode": "0644",
                        "compression": "deflate, level 9" if compression == "deflate" else "stored"},
            "made_with": made_with}


def archive(ctx: Context, root: str, name: str, include: list, exclude: list, tracked: bool, timestamp: str,
            compression: str, variants: dict):
    base = Path(root)
    if not base.is_dir():
        raise ParamError(f"{ctx.host_path(base)} is not a folder")
    name = name[:-4] if name.lower().endswith(".zip") else name
    if not NAME.fullmatch(name):
        raise ParamError(f"name {name!r}: letters, digits and . _ + -, at most 100")
    when = _moment(timestamp)
    method = zipfile.ZIP_DEFLATED if compression == "deflate" else zipfile.ZIP_STORED
    wanted = [_glob(g, "include") for g in include]
    unwanted = [_glob(g, "exclude") for g in exclude]
    ctx.progress("collect", msg=ctx.host_path(base))
    entries, refused = _collect(base, wanted, unwanted)
    if tracked:
        known = checkouts.tracked(ctx, base)
        entries = [e for e in entries if e in known]
        # A link git does not track, with nothing tracked below it, would not be packed anyway.
        refused = [r for r in refused if r in known or any(k.startswith(r + "/") for k in known)]
    problems = [f"{r} is a link or a special file; an archive holds regular files only (exclude it)"
                for r in refused] + unportable(entries)
    if problems:
        for problem in problems:
            ctx.finding(problem)
        ctx.log.warning("nothing was packed")
        return {"archive": None, "sha256": None, "files": 0, "refused": problems}
    if not entries:
        raise ParamError("nothing to pack: no file of root matches include and escapes exclude")
    plan = _variants(variants, set(entries))
    digests = {e: sha256_file(base / e) for e in entries}
    sizes = {e: (base / e).stat().st_size for e in entries}
    builds = [(f"{name}.zip", None, {})]
    for variant, edits in plan.items():
        replaced = {}
        for path, values in edits.items():
            data = (base / path).read_bytes()
            if hashlib.sha256(data).hexdigest() != digests[path]:
                raise EnvironmentProblem(f"{path} changed while it was packed; nothing was written")
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                raise ParamError(f"variant {variant}: {path} is not UTF-8 text") from None
            replaced[path] = freeze_defines(text, values, f"variant {variant}: {path}").encode("utf-8")
        builds.append((f"{name}-{variant}.zip", variant, replaced))
    try:
        return _publish(ctx, base, entries, digests, sizes, plan, builds, when, method, compression)
    finally:
        for zip_name, _, _ in builds:
            (ctx.work / zip_name).unlink(missing_ok=True)


def _publish(ctx: Context, base: Path, entries: list[str], digests: dict, sizes: dict, plan: dict, builds: list,
             when: tuple, method: int, compression: str) -> dict:
    """Packs each archive in the work folder, then writes them and their manifests and sidecars to the out folder,
    unless one of those names already holds other bytes there."""
    records = []
    for zip_name, variant, replaced in builds:
        ctx.check_cancel()
        ctx.progress("pack", msg=zip_name)
        temporary = ctx.work / zip_name
        _pack(temporary, base, entries, digests, replaced, when, method)
        record = _record(zip_name, temporary, entries, digests, sizes, replaced, when, compression)
        if variant is not None:
            first = records[0]
            record["variant"] = {"name": variant, "of": first["archive"], "of_sha256": first["sha256"],
                                 "values": plan[variant],
                                 "modified": [{"path": p, "before": digests[p],
                                               "after": hashlib.sha256(replaced[p]).hexdigest()}
                                              for p in sorted(replaced)]}
        records.append(record)
    writes = []  # (final path, a file to copy or bytes to write, its SHA-256)
    for record in records:
        zip_name = record["archive"]
        manifest = (json.dumps(record, indent=2, sort_keys=True) + "\n").encode("utf-8")
        writes.append((ctx.out / zip_name, ctx.work / zip_name, record["sha256"]))
        writes.append((ctx.out / (zip_name + ".sha256"), sidecar_text(zip_name, record["sha256"]), None))
        writes.append((ctx.out / (zip_name[:-4] + ".json"), manifest, None))
    conflicts = []
    for final, content, digest in writes:
        if not final.exists():
            continue
        want = digest or hashlib.sha256(content).hexdigest()
        if not final.is_file() or sha256_file(final) != want:
            conflicts.append(final.name)
    if conflicts:
        for conflict in conflicts:
            ctx.finding(f"{conflict} already exists with other bytes: an archive's name never changes meaning "
                        "(use a new name or out folder)")
        ctx.log.warning("nothing was written")
        return {"archive": None, "sha256": records[0]["sha256"], "files": len(entries), "refused": conflicts}
    written, present = [], []
    for final, content, digest in writes:
        if final.exists():
            present.append(final.name)
            continue
        if digest:
            copy_verified(content, final, digest)
        else:
            write_atomic(final, content)
        written.append(final.name)
    for record in records:
        ctx.output(record["archive"], ctx.out / record["archive"])
        ctx.output(record["archive"][:-4] + ".json", ctx.out / (record["archive"][:-4] + ".json"))
    first = records[0]
    ctx.measure("archive_bytes", first["bytes"], "B")
    ctx.summary(f"{first['archive']}: {len(entries)} file(s), {first['bytes']} bytes, SHA-256 {first['sha256'][:12]}"
                + (f"; {len(records) - 1} variant(s)" if len(records) > 1 else "")
                + (" (already there)" if not written else ""))
    return {"archive": ctx.host_path(ctx.out / first["archive"]), "sha256": first["sha256"], "bytes": first["bytes"],
            "files": len(entries), "tree_sha256": first["tree_sha256"],
            "variants": [{"name": r["variant"]["name"], "archive": ctx.host_path(ctx.out / r["archive"]),
                          "sha256": r["sha256"]} for r in records[1:]],
            "written": written, "present": present}
