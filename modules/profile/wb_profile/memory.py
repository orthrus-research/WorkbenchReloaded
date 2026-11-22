"""profile.memory: a JVM's memory by owner class, from its Native Memory Tracking summary
(``jcmd PID VM.native_memory summary``) joined with its memory map (``jcmd PID System.map``, JDK 21+).

NMT knows what the JVM itself reserved and allocated; the map knows every mapping of the process, and tags
those NMT knows (JAVAHEAP, CODE, STACK-..., META ...). Untagged private memory beyond the JVM's own malloc
belongs to native libraries and drivers loaded beside it. Linux maps give each mapping's resident size,
so the accounting is of resident memory; Windows maps give committed regions, so it is of committed
private memory, against the process's commit charge. Without a map only NMT's classes are known.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import PurePosixPath, PureWindowsPath

MIB = 1024 * 1024
UNITS = {"B": 1, "KB": 1024, "MB": MIB, "GB": 1024 * MIB, "K": 1024, "M": MIB, "G": 1024 * MIB}
CLASSES = ("heap", "JVM internals", "code cache", "thread stacks", "direct buffers",
           "native libraries and drivers", "images and mapped files", "unaccounted")

_SIZE = r"(\d+)\s*([KMG]?B)"
_TOTAL = re.compile(rf"^Total:\s+reserved={_SIZE},\s+committed={_SIZE}", re.M)
_MALLOC_TOTAL = re.compile(rf"^\s+malloc:\s+{_SIZE}", re.M)
_MMAP_TOTAL = re.compile(rf"^\s+mmap:\s+reserved={_SIZE},\s+committed={_SIZE}", re.M)
_CATEGORY = re.compile(rf"^-\s*(?P<name>[A-Za-z][\w ]*?)\s+\(reserved={_SIZE},\s+committed={_SIZE}")
_MALLOC = re.compile(rf"\(malloc={_SIZE}")
_ARENA = re.compile(rf"\(arena={_SIZE}")
_MMAP = re.compile(rf"\(mmap:\s+reserved={_SIZE},\s+committed={_SIZE}")
_STACK = re.compile(rf"\(stack:\s+reserved={_SIZE},\s+committed={_SIZE}")
_THREADS = re.compile(r"\(threads? #(\d+)\)")

_LINUX_ROW = re.compile(r"^0x(?P<start>[0-9a-f]+)\s*-\s*0x(?P<end>[0-9a-f]+)\s+(?P<size>\d+)\s+"
                        r"(?P<prot>[r-][w-][x-][ps])\s+(?P<rest>.*)$")
_WINDOWS_ROW = re.compile(r"^0x(?P<start>[0-9a-f]+)-0x(?P<end>[0-9a-f]+)\s+(?P<size>\d+)\s+(?P<prot>\S+)\s+"
                          r"(?P<state>[cr])-(?P<type>pvt|img|map)\s+(?P<offset>\S+)\s*(?P<info>.*)$")
_TAG = re.compile(r"^(?:STACK-\S+|[A-Z][A-Z0-9]+)(?:,(?:STACK-\S+|[A-Z][A-Z0-9]+))*$")
_FOOTER_SIZE = r"(\d+(?:\.\d+)?)\s*([KMG]?B?)"
_COMMIT_CHARGE = re.compile(rf"commit charge \(\"private bytes\"\):\s*{_FOOTER_SIZE}")
_WORKING_SET = re.compile(rf"WorkingSet \(physical memory assigned to process\):\s*{_FOOTER_SIZE}")
_LINUX_RSS = re.compile(r"^\s*rss:\s+(\d+)", re.M)
HEAP_TAGS, CODE_TAGS = {"JAVAHEAP"}, {"CODE"}
ANONYMOUS = {"", "-", "[heap]"}


def _bytes(value: str, unit: str) -> int:
    return int(float(value) * UNITS.get((unit or "B").upper(), 1))


def nmt(text: str) -> dict:
    """Bytes from an NMT summary: total reserved and committed, malloc and mmap totals, and each category's
    committed, malloc, mmap and (for Thread) stack bytes."""
    if "not enabled" in text and "Native memory tracking" in text:
        raise ValueError("native memory tracking is not enabled in that JVM (start it with "
                         "-XX:NativeMemoryTracking=summary)")
    total = _TOTAL.search(text)
    if not total:
        raise ValueError("not a Native Memory Tracking summary (VM.native_memory summary)")
    categories: dict[str, dict] = {}
    current = None
    for line in text.splitlines():
        head = _CATEGORY.match(line)
        if head:
            current = categories.setdefault(head["name"].strip(), {
                "reserved": _bytes(head[2], head[3]), "committed": _bytes(head[4], head[5]),
                "malloc": 0, "arena": 0, "mmapCommitted": 0})
            continue
        stripped = line.strip()
        if current is None or not stripped.startswith("("):
            if stripped and not line.startswith(" "):
                current = None  # past the category's lines
            continue
        for pattern, key in ((_MALLOC, "malloc"), (_ARENA, "arena")):
            match = pattern.search(line)
            if match:
                current[key] = _bytes(match[1], match[2])
        match = _MMAP.search(line)
        if match:
            current["mmapCommitted"] = _bytes(match[3], match[4])
        match = _STACK.search(line)
        if match:
            current["stackCommitted"] = _bytes(match[3], match[4])
        match = _THREADS.search(line)
        if match:
            current["threads"] = int(match[1])
    malloc = _MALLOC_TOTAL.search(text[total.end():])
    mmap = _MMAP_TOTAL.search(text[total.end():])
    summed = sum(c["malloc"] + c["arena"] for c in categories.values())
    return {"reserved": _bytes(total[1], total[2]), "committed": _bytes(total[3], total[4]),
            "malloc": _bytes(malloc[1], malloc[2]) if malloc else summed,
            "mmapCommitted": _bytes(mmap[3], mmap[4]) if mmap else None,
            "categories": categories}


def system_map(text: str) -> dict:
    """The mappings of a ``System.map`` listing (Linux or Windows form) and its footer totals."""
    mappings, style = [], None
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("0x"):
            continue
        row = _WINDOWS_ROW.match(line)
        if row:
            style = style or "windows"
            info = row["info"].strip()
            first, _, rest = info.partition(" ")
            tags = first.split(",") if first and _TAG.match(first) else []
            mappings.append({"size": int(row["size"]), "prot": row["prot"], "committed": row["state"] == "c",
                             "type": row["type"], "tags": tags, "file": (rest if tags else info).strip()})
            continue
        row = _LINUX_ROW.match(line)
        if row:
            style = style or "linux"
            mappings.append(dict(_linux_rest(row["rest"]), size=int(row["size"]), prot=row["prot"]))
    if not mappings:
        raise ValueError("not a System.map listing (jcmd PID System.map)")
    footer = {}
    if style == "windows":
        for key, pattern in (("commitCharge", _COMMIT_CHARGE), ("workingSet", _WORKING_SET)):
            match = pattern.search(text)
            if match:
                footer[key] = _bytes(match[1], match[2] or "B")
    else:
        match = _LINUX_RSS.search(text)
        if match:
            footer["rss"] = int(match[1])
    return {"style": style, "mappings": mappings, "footer": footer}


def _linux_rest(rest: str) -> dict:
    """After the protection: rss, hugetlb, page size and notes (JDK 23+), then the VM info and the file."""
    tokens = rest.split()
    out = {"rss": None, "tags": [], "file": ""}
    if len(tokens) >= 4 and tokens[0].isdigit() and tokens[1].isdigit() and re.match(r"^\d+[KMG]$", tokens[2]):
        out["rss"] = int(tokens[0])
        tokens = tokens[4:]  # rss, hugetlb, page size, notes
    elif tokens and tokens[0].startswith("0x"):
        tokens = tokens[1:]  # an offset, in the older form
    if tokens and _TAG.match(tokens[0]):
        out["tags"] = tokens[0].split(",")
        tokens = tokens[1:]
    elif tokens and tokens[0] == "-":
        tokens = tokens[1:]
    out["file"] = " ".join(tokens)
    return out


def _measure(mapping: dict, style: str) -> int:
    if style == "linux":
        if mapping["rss"] is not None:
            return mapping["rss"]
        return mapping["size"] if mapping["prot"][:3] != "---" else 0
    return mapping["size"] if mapping["committed"] else 0


def _name(path: str) -> str:
    return (PureWindowsPath(path) if "\\" in path else PurePosixPath(path)).name or path


def account(summary: dict, mapped: dict | None = None, top: int = 8) -> dict:
    """Owner classes, in bytes. With a map: tagged mappings by tag, NMT's malloc split out of untagged
    private memory, and what is left of the process total unaccounted. Without one: NMT's view only."""
    categories = summary["categories"]
    committed = lambda name: categories.get(name, {}).get("committed", 0)  # noqa: E731
    stack = categories.get("Thread", {}).get("stackCommitted", 0)
    other = committed("Other")
    largest = sorted(categories.items(), key=lambda kv: -kv[1]["committed"])
    out = {"nmt": {"committed": summary["committed"], "reserved": summary["reserved"], "malloc": summary["malloc"],
                   "categories": {n: c["committed"] for n, c in largest},
                   "threads": categories.get("Thread", {}).get("threads")}}
    if mapped is None:
        classes = {"heap": committed("Java Heap"), "code cache": committed("Code"), "thread stacks": stack,
                   "direct buffers": other}
        classes["JVM internals"] = summary["committed"] - sum(classes.values())
        classes.update({"native libraries and drivers": None, "images and mapped files": None, "unaccounted": None})
        out.update(basis="committed (NMT only)", classes=_ordered(classes))
        return out
    style = mapped["style"]
    tagged, images = Counter(), Counter()
    anonymous = files = stacks = 0
    write_combined = 0
    for m in mapped["mappings"]:
        size = _measure(m, style)
        if not size:
            continue
        if style == "windows" and "W" in m["prot"]:
            write_combined += size
        if m["tags"]:
            tag = m["tags"][0]
            if tag.startswith("STACK"):
                stacks += size
            else:
                tagged[tag] += size
        elif style == "windows":
            if m["type"] == "pvt":
                anonymous += size
            elif m["type"] == "img" and "w" in m["prot"].lower():
                files += size  # writable image pages are private copies
                images[_name(m["file"]) or "(image)"] += size
        elif m["file"] in ANONYMOUS or m["file"].startswith("[anon"):
            anonymous += size
        elif m["file"] == "[stack]":
            stacks += size
        else:
            files += size
            images[_name(m["file"])] += size
    code_malloc = categories.get("Code", {}).get("malloc", 0)
    jvm_malloc = summary["malloc"]
    classes = {"heap": sum(tagged[t] for t in HEAP_TAGS), "code cache": sum(tagged[t] for t in CODE_TAGS) + code_malloc,
               "thread stacks": stacks, "direct buffers": other,
               "JVM internals": sum(v for t, v in tagged.items() if t not in HEAP_TAGS | CODE_TAGS)
               + max(0, jvm_malloc - other - code_malloc),
               "native libraries and drivers": max(0, anonymous - jvm_malloc), "images and mapped files": files}
    footer = mapped["footer"]
    total = footer.get("rss") if style == "linux" else footer.get("commitCharge")
    classes["unaccounted"] = total - sum(classes.values()) if total is not None else None
    out.update(basis="resident (Linux map)" if style == "linux" else "committed private (Windows map)",
               classes=_ordered(classes), processTotal=total,
               map={"tags": dict(tagged.most_common()), "untaggedPrivate": anonymous,
                    "largestFiles": dict(images.most_common(top)), "mappings": len(mapped["mappings"])})
    if style == "windows":
        out["map"]["writeCombined"] = write_combined
        if footer.get("workingSet") is not None:
            out["map"]["workingSet"] = footer["workingSet"]
    return out


def _ordered(classes: dict) -> list[dict]:
    return [{"owner": name, "bytes": classes[name],
             "mib": round(classes[name] / MIB, 1) if classes[name] is not None else None} for name in CLASSES]


def summary_line(accounting: dict) -> str:
    parts = [f"{c['owner']} {c['mib']:,.0f} MiB" for c in accounting["classes"]
             if c["mib"] is not None and (c["owner"] != "unaccounted" or abs(c["mib"]) >= 1)]
    return f"{', '.join(parts)} ({accounting['basis']})"
