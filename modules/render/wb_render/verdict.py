"""Whether a shader pack really loaded, from a game log, for render.load-verdict.

Captures alone prove nothing: a pack that fails can leave the game rendering without it. The verdict reads the log's
evidence instead. A pattern set says what that evidence looks like for one loader:

    [fatal]                    # regular expressions, matched without case; any match refuses the run
    fallback = ['...']         # the loader fell back to rendering without the pack
    load_failure = ['...']     # the pack could not be read
    pipeline_failure = ['...']
    compile_failure = ['...']  # a stage did not compile or a program did not link
    directive_failure = ['...']
    missing_option = ['...']   # a menu or profile names an option the pack does not declare
    [markers]                  # regular expressions with named groups; '' turns a marker off
    using = '(?P<pack>...)'    # the pack the loader activated
    profile = '(?P<profile>...)'
    loaded = '(?P<pack>...) (?P<dimension>...) (?P<ms>...)'   # a completed load, per dimension
    capture = '(?P<file>...)'  # a capture (screenshot) the run saved
    error = '...'              # other error lines: listed, never refusing
    ignore = ['...']           # lines to skip entirely (known-harmless noise)

The built-in ``optifine`` set covers OptiFine-format loaders; a set named in ``[modules.render.loaders.NAME]`` is laid
over it, and a patterns file over that: a ``fatal`` kind or a marker given replaces the one beneath, other keys stay.
"""

from __future__ import annotations

import binascii
import copy
import gzip
import hashlib
import re
import struct
from pathlib import Path

OPTIFINE = {
    "fatal": {
        "fallback": [r"fall(?:ing)? back to (?:normal|vanilla|default) rendering",
                     r"without shaders because the shader\s*pack", r"shaders are disabled"],
        "load_failure": [r"(?:failed|unable) to load (?:the )?shader\s*pack", r"shader\s*pack could not be loaded"],
        "pipeline_failure": [r"(?:unable|failed|error) (?:to |while )?(?:create|creating|construct|constructing|"
                             r"initiali[sz]e|initiali[sz]ing) (?:the )?(?:shader )?pipeline"],
        "compile_failure": [r"(?:error|failed|unable) (?:to )?(?:compil(?:e|ing)|link(?:ing)?).{0,100}(?:shader|program)",
                            r"(?:shader|program).{0,100}(?:compilation|link).{0,30}(?:fail|error)",
                            r"^\s*ERROR:\s*\d+:\d+:"],
        "directive_failure": [r"failed to (?:process|parse) (?:the )?const\s*directive"],
        "missing_option": [r"unable to resolve shader\s*pack option"],
    },
    "markers": {
        "using": r"\b(?:Using|Loaded) shader\s*pack:\s*(?P<pack>.+?)\s*$",
        "profile": r"\]: Profile:\s*(?P<profile>[^\r\n(]+?)\s*(?:\(|$)",
        "loaded": r"Total shader\s*pack load time for '(?P<pack>[^']+)' in dimension '(?P<dimension>[^']+)':\s*"
                  r"(?P<ms>[\d.]+)\s*ms",
        "capture": r"Saved screenshot as (?P<file>\S+?\.png)\b",
        "error": r"/(?:ERROR|FATAL)\]",
    },
    "ignore": [],
}
BUILTIN = {"optifine": OPTIFINE}
TOP_KEYS = {"fatal", "markers", "ignore"}
MARKERS = {"using": {"pack"}, "profile": {"profile"}, "loaded": {"pack", "dimension"}, "capture": {"file"}, "error": set()}
CAPTURE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_. -]*\.png$", re.IGNORECASE)


def _check(table: dict, where: str):
    if not isinstance(table, dict):
        raise ValueError(f"{where} must be a table")
    unknown = set(table) - TOP_KEYS
    if unknown:
        raise ValueError(f"{where}: unknown keys {sorted(unknown)} (known: {', '.join(sorted(TOP_KEYS))})")
    for kind, patterns in (table.get("fatal") or {}).items():
        if not isinstance(patterns, list) or not all(isinstance(p, str) for p in patterns):
            raise ValueError(f"{where}: fatal.{kind} must be a list of regular expressions")
    for name, pattern in (table.get("markers") or {}).items():
        if name not in MARKERS:
            raise ValueError(f"{where}: unknown marker {name} (known: {', '.join(sorted(MARKERS))})")
        if not isinstance(pattern, str):
            raise ValueError(f"{where}: markers.{name} must be a regular expression")
    ignore = table.get("ignore", [])
    if not isinstance(ignore, list) or not all(isinstance(p, str) for p in ignore):
        raise ValueError(f"{where}: ignore must be a list of regular expressions")


def rules(loader: str, configured: dict, overlay: dict | None = None) -> dict:
    """The pattern set for ``loader`` with any patterns file laid over it, compiled. Raises ValueError."""
    if loader in BUILTIN:
        merged = copy.deepcopy(BUILTIN[loader])
    elif loader in configured:
        merged = copy.deepcopy(OPTIFINE)
    else:
        known = sorted(set(BUILTIN) | set(configured))
        raise ValueError(f"no loader pattern set {loader!r} (known: {', '.join(known)})")
    layers = ([(configured[loader], f"[modules.render.loaders.{loader}]")] if loader in configured else []) + \
             ([(overlay, "the patterns file")] if overlay is not None else [])
    for table, where in layers:
        _check(table, where)
        merged["fatal"].update(table.get("fatal") or {})
        merged["markers"].update(table.get("markers") or {})
        if "ignore" in table:
            merged["ignore"] = list(table["ignore"])
    compiled = {"fatal": {}, "markers": {}, "ignore": []}
    try:
        for kind, patterns in merged["fatal"].items():
            if patterns:
                compiled["fatal"][kind] = re.compile("|".join(f"(?:{p})" for p in patterns), re.IGNORECASE)
        for name, pattern in merged["markers"].items():
            if not pattern:
                continue
            regex = re.compile(pattern)
            missing = MARKERS[name] - set(regex.groupindex)
            if missing:
                raise ValueError(f"marker {name} needs the named group(s) {', '.join(sorted(missing))}")
            compiled["markers"][name] = regex
        compiled["ignore"] = [re.compile(p) for p in merged["ignore"]]
    except re.error as error:
        raise ValueError(f"a pattern does not compile: {error}") from None
    return compiled


def _pack_name(name: str) -> str:
    name = name.strip()
    return name[:-4] if name.lower().endswith(".zip") else name


def evaluate(lines, rules: dict, *, pack: str = "", profile: str = "", dimensions=(), min_captures: int = 0) -> dict:
    """The verdict over the log's lines: ``passed`` and the ``reasons`` it was refused, with the evidence."""
    fatal, profiles, active, loads, captures, errors = [], [], [], [], [], []
    markers = rules["markers"]
    for number, line in enumerate(lines, 1):
        line = line.rstrip("\r\n")
        if any(pattern.search(line) for pattern in rules["ignore"]):
            continue
        kinds = [kind for kind, pattern in rules["fatal"].items() if pattern.search(line)]
        if kinds:
            fatal.append({"line": number, "kinds": kinds, "text": line[:1000]})
        elif "error" in markers and markers["error"].search(line):
            errors.append({"line": number, "text": line[:1000]})
        if "profile" in markers and (match := markers["profile"].search(line)):
            profiles.append({"line": number, "profile": match["profile"].strip()})
        if "using" in markers and (match := markers["using"].search(line)):
            active.append({"line": number, "pack": match["pack"].strip()})
        if "loaded" in markers and (match := markers["loaded"].search(line)):
            entry = {"line": number, "pack": match["pack"].strip(), "dimension": match["dimension"].strip()}
            if "ms" in match.groupdict() and match["ms"]:
                entry["milliseconds"] = float(match["ms"])
            loads.append(entry)
        if "capture" in markers and (match := markers["capture"].search(line)):
            captures.append({"line": number, "file": match["file"]})

    def ours(name: str) -> bool:
        return not pack or _pack_name(name) == _pack_name(pack)

    reasons = []
    by_kind: dict[str, list[dict]] = {}
    for item in fatal:
        for kind in item["kinds"]:
            by_kind.setdefault(kind, []).append(item)
    for kind, items in by_kind.items():
        first = items[0]
        reasons.append(f"{len(items)} {kind.replace('_', ' ')} line(s), first at line {first['line']}: "
                       f"{first['text'].strip()[:160]}")
    activated = [item for item in active if ours(item["pack"])]
    if not activated:
        reasons.append(f"the log never names {pack!r} as the active shader pack" if pack else
                       "the log never names an active shader pack")
    if active and not ours(active[-1]["pack"]):
        reasons.append(f"the last active shader pack is {active[-1]['pack']!r}, not {pack!r}")
    if profile and not any(item["profile"] == profile for item in profiles):
        seen = sorted({item["profile"] for item in profiles})
        reasons.append(f"the log never selects the profile {profile!r}" + (f" (it selects {', '.join(seen)})" if seen else ""))
    completed = [item for item in loads if ours(item["pack"])]
    for dimension in dimensions:
        if not any(item["dimension"] == dimension for item in completed):
            reasons.append(f"no completed load of the pack in {dimension}")
    # Where the pack counts as loaded: its completed loads when the loader logs them, else its activation.
    anchors = completed if loads else activated
    if len(captures) < min_captures:
        reasons.append(f"{len(captures)} capture(s), fewer than {min_captures}")
    early = [c for c in captures if not any(a["line"] < c["line"] for a in anchors)]
    if early:
        reasons.append(f"{len(early)} capture(s) precede any load of the pack, first {early[0]['file']} at line "
                       f"{early[0]['line']}")
    return {"passed": not reasons, "reasons": reasons, "pack": pack, "profile": profile,
            "dimensions": list(dimensions), "min_captures": min_captures, "fatal": fatal, "profiles": profiles,
            "active": active, "loads": loads, "captures": captures, "other_errors": errors,
            "load_marker": "loaded" if loads else "using"}


def read_lines(path: Path):
    """The log's lines; a .gz log is read through gzip."""
    path = Path(path)
    opener = gzip.open if path.suffix.lower() == ".gz" else open
    with opener(path, "rt", encoding="utf-8", errors="replace", newline="") as handle:
        yield from handle


def png_structure(path: Path) -> dict:
    """Checks a PNG's framing: signature, IHDR, every chunk's length and CRC, IDAT and a final IEND. Pixels are not
    decoded. Raises ValueError."""
    data = Path(path).read_bytes()
    if len(data) < 33 or data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("no PNG signature")
    offset, header, saw_data = 8, None, False
    while offset < len(data):
        if offset + 12 > len(data):
            raise ValueError("a chunk header is cut short")
        size = struct.unpack_from(">I", data, offset)[0]
        kind = data[offset + 4:offset + 8]
        end = offset + 12 + size
        if size > 0x7FFFFFFF or end > len(data):
            raise ValueError("a chunk runs past the end of the file")
        if binascii.crc32(data[offset + 4:offset + 8 + size]) & 0xFFFFFFFF != struct.unpack_from(">I", data, offset + 8 + size)[0]:
            raise ValueError(f"the {kind.decode('latin-1')} chunk fails its CRC")
        if header is None:
            if kind != b"IHDR" or size != 13:
                raise ValueError("the first chunk is not IHDR")
            width, height, depth, colour = struct.unpack_from(">IIBB", data, offset + 8)
            if not width or not height:
                raise ValueError("a zero dimension")
            header = {"width": width, "height": height, "bit_depth": depth, "colour_type": colour}
        if kind == b"IDAT":
            saw_data = True
        if kind == b"IEND":
            if end != len(data):
                raise ValueError("bytes follow IEND")
            if not saw_data:
                raise ValueError("no IDAT chunk")
            return {**header, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        offset = end
    raise ValueError("no IEND chunk")


def check_captures(result: dict, folder: Path) -> list[str]:
    """Each logged capture is a plain file name present in ``folder`` and a well-formed PNG."""
    problems = []
    root = Path(folder).resolve()
    for capture in result["captures"]:
        name = capture["file"]
        if not CAPTURE_NAME.match(name) or Path(name).name != name:
            problems.append(f"the log names an unsafe capture file {name!r}")
            continue
        path = (root / name).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            problems.append(f"capture {name} is not in the screenshots folder")
            continue
        try:
            capture.update(png_structure(path))
        except (OSError, ValueError) as error:
            problems.append(f"capture {name} is not a well-formed PNG: {error}")
    return problems
