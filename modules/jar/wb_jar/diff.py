"""Why two archives of one build differ, entry by entry: which entries, their order, their metadata, their content
(with the cheapest explanation: line endings, JSON order, line order) or only how they were compressed.

The comparison two builds of one commit need when their digests differ: on two machines, two OSes or two JDK builds.
"""

from __future__ import annotations

import hashlib
import json
import struct
import zipfile
from pathlib import Path

META = ("date_time", "external_attr", "create_system", "compress_type", "extra", "comment")


def _read(path: Path) -> tuple[list[str], dict[str, tuple[zipfile.ZipInfo, bytes, bytes]]]:
    """Entry names in archive order, and each entry's info, content and compressed bytes."""
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        return [i.filename for i in infos], {i.filename: (i, archive.read(i), _packed(archive, i)) for i in infos}


def _packed(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    """An entry's bytes as stored, after its local header."""
    archive.fp.seek(info.header_offset)
    header = archive.fp.read(30)
    name_length, extra_length = struct.unpack("<HH", header[26:30])
    archive.fp.seek(info.header_offset + 30 + name_length + extra_length)
    return archive.fp.read(info.compress_size)


def explain(left: bytes, right: bytes) -> str:
    """How two contents of one entry differ, the cheapest explanation first."""
    if left.replace(b"\r\n", b"\n") == right.replace(b"\r\n", b"\n"):
        return "line endings"
    try:
        if json.loads(left) == json.loads(right):
            return "JSON order or layout"
    except (ValueError, UnicodeDecodeError):
        pass
    if sorted(left.splitlines()) == sorted(right.splitlines()):
        return "line order"
    return "content"


def _first_difference(left: bytes, right: bytes) -> int:
    return next((i for i, (x, y) in enumerate(zip(left, right)) if x != y), min(len(left), len(right)))


def _entry(name: str, a: tuple, b: tuple) -> dict | None:
    (info_a, data_a, packed_a), (info_b, data_b, packed_b) = a, b
    if data_a != data_b:
        at = _first_difference(data_a, data_b)
        return {"name": name, "kind": explain(data_a, data_b), "sizes": [len(data_a), len(data_b)],
                "firstDifference": at, "a": data_a[max(0, at - 24):at + 40].decode("utf-8", "replace"),
                "b": data_b[max(0, at - 24):at + 40].decode("utf-8", "replace")}
    fields = [f for f in META if getattr(info_a, f) != getattr(info_b, f)]
    if fields:
        return {"name": name, "kind": "metadata", "fields": {f: [str(getattr(info_a, f)), str(getattr(info_b, f))]
                                                             for f in fields}}
    if packed_a != packed_b:
        return {"name": name, "kind": "compression", "compressedSizes": [info_a.compress_size, info_b.compress_size]}
    return None


def compare(a: Path, b: Path) -> dict:
    names_a, entries_a = _read(a)
    names_b, entries_b = _read(b)
    common = [n for n in names_a if n in entries_b]
    differing = [d for d in (_entry(n, entries_a[n], entries_b[n]) for n in common) if d]
    kinds: dict[str, int] = {}
    for item in differing:
        kinds[item["kind"]] = kinds.get(item["kind"], 0) + 1
    digests = [hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in (a, b)]
    return {
        "a": {"sha256": digests[0], "entries": len(names_a)},
        "b": {"sha256": digests[1], "entries": len(names_b)},
        "identical": digests[0] == digests[1],
        "onlyInA": [n for n in names_a if n not in entries_b],
        "onlyInB": [n for n in names_b if n not in entries_a],
        "orderDiffers": common != [n for n in names_b if n in entries_a],
        "kinds": kinds,
        "entries": differing,
    }
