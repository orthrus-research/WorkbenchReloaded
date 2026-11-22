"""Frames captured along a path, checked without the game: what each file holds, which path frame it shows, and how
the pictures change from one frame to the next.

A frame's place on the path comes from its name (naming): index, the last number in the name is the path frame
(frame-0001.png); ms, the last number is a time in milliseconds; timestamp, a date and time to the second, with
_N for several in one second (2026-11-21_12.30.05_2.png); order, nothing (the sorted names are the capture order);
or a regular expression with named groups index, ms or seconds. Frames with numbers map by number (offset is the
number of the path's first frame), frames with only times by time from the first, others by order.

Two frames are the same picture when their image data is the same: a PNG's IHDR, PLTE, tRNS and IDAT data (its
other chunks, a time stamp among them, left out), a JPEG's whole file. Motion continuity compares each frame with
the one before by their mean absolute difference (0-255, on grey thumbnails), which needs NumPy and Pillow.
"""

from __future__ import annotations

import datetime
import hashlib
import re
import statistics
import struct
import zlib
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .path import Read
from .tour import Finding

IMAGES = {".png", ".jpg", ".jpeg"}
SIGNATURE = b"\x89PNG\r\n\x1a\n"
LAST_NUMBER = re.compile(r"(\d+)(?!.*\d)")
STAMP = re.compile(r"(\d{4})-(\d{2})-(\d{2})[_ T](\d{2})[.:-](\d{2})[.:-](\d{2})(?:[.,](\d{1,6}))?(?:_(\d+))?")
NAMINGS = ("index", "ms", "timestamp", "order")
WINDOW = 5     # frames either side whose differences make a frame's usual change
SAME = 1e-6    # poses closer than this (blocks, degrees) are one pose


@dataclass
class Frame:
    name: str
    path: Path
    index: int | None = None
    time: float | None = None          # seconds, from the name
    suffix: int = 0                    # timestamp: the _N of several in one second
    digest: str | None = None          # the image data's SHA-256; None when the file cannot be read
    size: tuple[int, int] | None = None
    problem: str | None = None
    unnamed: bool = False              # the name does not say what the naming needs, so the frame has no place
    frame: int | None = None           # the path frame it shows
    difference: float | None = None    # mean absolute difference from the frame before
    flag: str = ""                     # cut, gap, still or spike: how the change from the frame before reads
    repeats: str | None = None         # the frame before, when the picture is the same
    stuck: bool = False                # it repeats the frame before while the camera moves

    def describe(self, route: Read | None) -> dict:
        pose = route.poses[self.frame] if route and self.frame is not None and self.frame < route.frames else None
        return {"file": self.name, "frame": self.frame, "index": self.index,
                "time": round(self.time, 4) if self.time is not None else None, "sha256": self.digest,
                "size": list(self.size) if self.size else None, "problem": self.problem,
                "segment": pose["segment"] if pose else None, "phase": pose["phase"] if pose else None,
                "difference": round(self.difference, 4) if self.difference is not None else None,
                "flag": self.flag or None, "repeats": self.repeats, "stuck": self.stuck}


def png(data: bytes) -> tuple[tuple[int, int], str]:
    """A PNG's size and the SHA-256 of its image data; ValueError when it is not a whole PNG."""
    if data[:8] != SIGNATURE:
        raise ValueError("not a PNG: no signature")
    offset, size, meta, stream, ended = 8, None, [], hashlib.sha256(), False
    while offset + 12 <= len(data):
        length, kind = struct.unpack_from(">I", data, offset)[0], data[offset + 4:offset + 8]
        end = offset + 12 + length
        if end > len(data):
            raise ValueError(f"truncated in its {kind.decode('latin-1')} chunk")
        payload = data[offset + 8:offset + 8 + length]
        if zlib.crc32(kind + payload) & 0xFFFFFFFF != struct.unpack_from(">I", data, end - 4)[0]:
            raise ValueError(f"its {kind.decode('latin-1')} chunk fails its CRC")
        if offset == 8 and kind != b"IHDR":
            raise ValueError("does not start with IHDR")
        if kind == b"IHDR":
            size = struct.unpack_from(">II", payload)
        if kind == b"IDAT":
            stream.update(payload)
        elif kind in (b"IHDR", b"PLTE", b"tRNS"):
            meta.append(kind + payload)
        if kind == b"IEND":
            ended = True
            break
        offset = end
    if not ended or size is None:
        raise ValueError("ends before IEND: truncated")
    return size, hashlib.sha256(b"".join(meta) + stream.digest()).hexdigest()


def jpeg(data: bytes) -> tuple[tuple[int, int] | None, str]:
    """A JPEG's size (from its frame header) and the SHA-256 of the file; ValueError when it is not a whole JPEG."""
    if data[:2] != b"\xff\xd8":
        raise ValueError("not a JPEG: no SOI marker")
    if not data.rstrip(b"\0").endswith(b"\xff\xd9"):
        raise ValueError("ends before its EOI marker: truncated")
    size, at = None, 2
    while at + 4 <= len(data) and size is None:
        if data[at] != 0xFF:
            raise ValueError("a marker is broken")
        marker = data[at + 1]
        if marker == 0xFF:
            at += 1
            continue
        if 0xD0 <= marker <= 0xD7 or marker == 0x01:
            at += 2
            continue
        length = struct.unpack_from(">H", data, at + 2)[0]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC) and at + 9 <= len(data):
            height, width = struct.unpack_from(">HH", data, at + 5)
            size = (width, height)
        at += 2 + length
    return size, hashlib.sha256(data).hexdigest()


def _named(frame: Frame, naming: str, pattern: re.Pattern | None) -> str | None:
    """Reads a frame's number or time from its name; a problem when the name does not say what it should."""
    stem = Path(frame.name).stem
    if pattern is not None:
        match = pattern.search(frame.name)
        if not match:
            return f"the name does not match {pattern.pattern}"
        groups = match.groupdict()
        try:
            if groups.get("index") is not None:
                frame.index = int(groups["index"])
            if groups.get("ms") is not None:
                frame.time = float(groups["ms"]) / 1000
            elif groups.get("seconds") is not None:
                frame.time = float(groups["seconds"])
        except ValueError:
            return "the pattern's groups are not numbers"
        return None
    if naming in ("index", "ms"):
        match = LAST_NUMBER.search(stem)
        if not match:
            return "the name has no number"
        if naming == "index":
            frame.index = int(match.group(1))
        else:
            frame.time = int(match.group(1)) / 1000
    elif naming == "timestamp":
        match = STAMP.search(stem)
        if not match:
            return "the name has no date and time (2026-11-21_12.30.05)"
        year, month, day, hour, minute, second = (int(v) for v in match.groups()[:6])
        try:
            moment = datetime.datetime(year, month, day, hour, minute, second, tzinfo=datetime.timezone.utc)
        except ValueError:
            return "the name's date and time is not a real one"
        fraction = match.group(7)
        frame.time = moment.timestamp() + (int(fraction) / 10 ** len(fraction) if fraction else 0.0)
        frame.suffix = int(match.group(8) or 0)
    return None


def _ranges(numbers: list[int]) -> list[tuple[int, int]]:
    out: list[list[int]] = []
    for n in sorted(numbers):
        if out and n == out[-1][1] + 1:
            out[-1][1] = n
        else:
            out.append([n, n])
    return [(a, b) for a, b in out]


def _span(a: int, b: int) -> str:
    return f"path frame {a}" if a == b else f"path frames {a}-{b}"


def _still(route: Read | None, a: int | None, b: int | None) -> bool:
    """Whether the route holds the camera in one pose from path frame a to path frame b."""
    if route is None or a is None or b is None or not (0 <= a < route.frames and 0 <= b < route.frames):
        return False
    first, second = route.poses[a], route.poses[b]
    return all(abs(float(first[key]) - float(second[key])) <= SAME for key in ("x", "y", "z", "yaw", "pitch"))


def _cut_between(route: Read | None, a: int | None, b: int | None) -> bool:
    if route is None or a is None or b is None:
        return False
    return any(route.poses[f]["phase"] == "cut" for f in range(max(a + 1, 0), min(b, route.frames - 1) + 1))


@dataclass
class Checked:
    frames: list[Frame]
    findings: list[Finding]
    mapping: str
    expected: int | None
    missing: list[int]
    duplicates: int
    spikes: int
    continuity: str                    # checked, or why not
    pace: dict | None
    segments: list[dict] = field(default_factory=list)


def check(folder: Path, route: Read | None, naming: str = "index", pattern: str = "", offset: int = 0,
          fps: float = 0.0, spike: float = 4.0, floor: float = 2.0, tolerance: float = 0.1,
          continuity: bool = True, scale: int = 160, step=None) -> Checked:
    """Checks a folder of frames (against a route, when given); step(phase, done, total) hears of progress."""
    step = step or (lambda phase, done, total: None)
    compiled = None
    if pattern:
        try:
            compiled = re.compile(pattern)
        except re.error as error:
            raise ValueError(f"pattern: {error}") from None
        if not set(compiled.groupindex) & {"index", "ms", "seconds"}:
            raise ValueError("pattern: name a group index, ms or seconds, as (?P<index>\\d+)")
    elif naming not in NAMINGS:
        raise ValueError(f"naming is one of {', '.join(NAMINGS)}")
    files = sorted(p for p in Path(folder).iterdir() if p.is_file() and p.suffix.lower() in IMAGES)
    if not files:
        raise ValueError(f"{folder} holds no PNG or JPEG frames")
    fps = fps or (route.fps if route else 20.0)
    findings: list[Finding] = []
    frames = [Frame(p.name, p) for p in files]
    for done, frame in enumerate(frames, 1):
        step("reading", done, len(frames))
        try:
            data = frame.path.read_bytes()
            frame.size, frame.digest = png(data) if frame.path.suffix.lower() == ".png" else jpeg(data)
        except ValueError as error:
            frame.problem = str(error)
            findings.append(Finding("frame", f"{frame.name} cannot be read: {error}"))
        problem = _named(frame, naming, compiled)
        if problem:
            findings.append(Finding("name", f"{frame.name}: {problem}, so it shows no known path frame"))
            frame.problem, frame.unnamed = frame.problem or problem, True
    sizes = Counter(frame.size for frame in frames if frame.size)
    if len(sizes) > 1:
        usual = sizes.most_common(1)[0][0]
        for frame in frames:
            if frame.size and frame.size != usual:
                findings.append(Finding("size", f"{frame.name} is {frame.size[0]}x{frame.size[1]}; the other frames "
                                                f"are {usual[0]}x{usual[1]}"))
    # -- which path frame each shows ---------------------------------------------------------------------------
    named = [f for f in frames if not f.unnamed]
    if not named:
        raise ValueError(f"no frame name in {folder} says which path frame it shows (naming {pattern or naming})")
    if any(f.index is not None for f in named):
        mapping = "index"
        placed = sorted((f for f in named if f.index is not None), key=lambda f: (f.index, f.name))
        for frame in placed:
            frame.frame = frame.index - offset
    elif any(f.time is not None for f in named):
        mapping = "time"
        placed = sorted((f for f in named if f.time is not None), key=lambda f: (f.time, f.suffix, f.name))
        start = placed[0].time
        for frame in placed:
            frame.frame = round((frame.time - start) * fps)
    else:
        mapping = "order"
        placed = sorted(named, key=lambda f: f.name)
        for position, frame in enumerate(placed):
            frame.frame = position
    shown: dict[int, list[Frame]] = {}
    for frame in placed:
        shown.setdefault(frame.frame, []).append(frame)
    for number, many in sorted(shown.items()):
        if len(many) > 1:
            findings.append(Finding("duplicate", f"{_span(number, number)} has {len(many)} captures: "
                                                 f"{', '.join(f.name for f in many[:4])}"))
    expected = route.frames if route else None
    if route:
        outside = [f for f in placed if not 0 <= f.frame < route.frames]
        for frame in outside[:5]:
            findings.append(Finding("count", f"{frame.name} shows path frame {frame.frame}, outside the path's "
                                             f"0-{route.frames - 1}"))
        if len(outside) > 5:
            findings.append(Finding("count", f"{len(outside) - 5} more frames fall outside the path"))
        missing = [n for n in range(route.frames) if n not in shown]
        if len(placed) != route.frames:
            findings.append(Finding("count", f"{len(placed)} frames were captured; the path has {route.frames}"))
    elif mapping != "order" and placed:
        low, high = min(shown), max(shown)
        missing = [n for n in range(low, high + 1) if n not in shown]
    else:
        missing = []
    for a, b in _ranges(missing):
        where = ""
        if route:
            labels = list(dict.fromkeys(route.segments[route.poses[n]["segment"]]["label"] for n in range(a, b + 1)))
            where = f", in segment {', '.join(labels[:3])}"
        count = b - a + 1
        findings.append(Finding("missing", f"{_span(a, b)} {'has' if count == 1 else 'have'} no capture "
                                           f"({count} frame{'s' if count > 1 else ''}{where})"))
    # -- the same picture twice -------------------------------------------------------------------------------
    order = [frame for frame in placed if frame.digest]
    duplicates = 0
    for before, frame in zip(order, order[1:]):
        if frame.digest == before.digest:
            frame.repeats = before.name
            if not _still(route, before.frame, frame.frame):
                frame.stuck = True
                duplicates += 1
                moving = "while the camera moves" if route else "(no route says the camera holds)"
                findings.append(Finding("repeat", f"{frame.name} repeats {before.name} {moving}"))
    # -- pace ---------------------------------------------------------------------------------------------------
    pace = None
    timed = [frame for frame in placed if frame.time is not None]
    if len(timed) > 1:
        intervals = [b.time - a.time for a, b in zip(timed, timed[1:])]
        steps = [max(b.frame - a.frame, 1) for a, b in zip(timed, timed[1:])]
        per_frame = [i / s for i, s in zip(intervals, steps)]
        median = statistics.median(per_frame)
        pace = {"expectedMs": round(1000 / fps, 3), "medianMs": round(median * 1000, 3),
                "maxMs": round(max(intervals) * 1000, 3), "ratio": round(median * fps, 4) if median else None}
        if mapping == "index" and median > 0 and abs(median * fps - 1) > tolerance:
            findings.append(Finding("pace", f"frames came every {median * 1000:.1f} ms ({1 / median:.2f} a second); "
                                            f"the path runs {fps:g} a second"))
    # -- motion continuity --------------------------------------------------------------------------------------
    spikes, state = 0, "not asked for"
    if continuity:
        state = _differences(order, scale, step)
    if state == "checked":
        for before, frame in zip(order, order[1:]):
            if frame.difference is None:
                continue
            if _cut_between(route, before.frame, frame.frame):
                frame.flag = "cut"
            elif before.frame is not None and frame.frame is not None and frame.frame - before.frame > 1:
                frame.flag = "gap"
            elif _still(route, before.frame, frame.frame):
                frame.flag = "still"
        values = [(frame, frame.difference) for frame in order[1:] if frame.difference is not None]
        for position, (frame, value) in enumerate(values):
            if frame.flag in ("cut", "gap"):
                continue
            around = values[max(0, position - WINDOW):position] + values[position + 1:position + 1 + WINDOW]
            near = [v for f, v in around if f.flag not in ("cut", "gap")]
            usual = statistics.median(near) if near else 0.0
            if value > floor and value > spike * usual:
                holds = frame.flag == "still"
                frame.flag = "spike"
                spikes += 1
                findings.append(Finding("continuity", f"{frame.name} (path frame {frame.frame}) changes {value:.1f} "
                                                      f"from the frame before against a usual {usual:.1f} nearby"
                                                      + (" while the camera holds" if holds else "")))
    return Checked(frames, findings, mapping, expected, missing, duplicates, spikes, state, pace,
                   _segments(route, placed, missing))


def _differences(order: list[Frame], scale: int, step) -> str:
    """Each frame's mean absolute difference from the one before, on grey thumbnails scale pixels wide."""
    try:
        import numpy
        from PIL import Image
    except ImportError as error:
        return f"needs NumPy and Pillow ({error.name} is missing)"
    resample = getattr(getattr(Image, "Resampling", Image), "BILINEAR")
    before = None
    for done, frame in enumerate(order, 1):
        step("comparing", done, len(order))
        try:
            with Image.open(frame.path) as image:
                grey = image.convert("L")
                height = max(1, round(scale * grey.height / grey.width))
                pixels = numpy.asarray(grey.resize((scale, height), resample), dtype=numpy.float32)
        except (OSError, ValueError):
            before = None
            continue
        if before is not None and before.shape == pixels.shape:
            frame.difference = float(numpy.abs(pixels - before).mean())
        before = pixels
    return "checked"


def _segments(route: Read | None, placed: list[Frame], missing: list[int]) -> list[dict]:
    """A summary a segment: the frames the path has and the capture holds, what is missing, repeated while the
    camera moves or jumps, the usual and the largest change, and the time between frames."""
    if not route:
        return [_summary(0, "all", None, None, placed, len(missing))]
    groups: dict[int, list[Frame]] = {}
    for frame in placed:
        if frame.frame is not None and 0 <= frame.frame < route.frames:
            groups.setdefault(route.poses[frame.frame]["segment"], []).append(frame)
    lost = Counter(route.poses[n]["segment"] for n in missing)
    return [_summary(index, segment.get("label", str(index)), segment.get("kind"), segment.get("frames"),
                     groups.get(index, []), lost[index]) for index, segment in enumerate(route.segments)]


def _summary(index: int, label: str, kind: str | None, planned, frames: list[Frame], lost: int) -> dict:
    changes = [f.difference for f in frames if f.difference is not None and f.flag not in ("cut", "gap")]
    times = [f.time for f in frames if f.time is not None]
    gaps = [b - a for a, b in zip(times, times[1:])]
    return {"index": index, "label": label, "kind": kind, "frames": planned, "captured": len(frames),
            "missing": lost, "repeats": sum(1 for f in frames if f.stuck),
            "spikes": sum(1 for f in frames if f.flag == "spike"),
            "difference": {"mean": round(statistics.fmean(changes), 4) if changes else None,
                           "max": round(max(changes), 4) if changes else None},
            "intervalMs": {"mean": round(statistics.fmean(gaps) * 1000, 3) if gaps else None,
                           "max": round(max(gaps) * 1000, 3) if gaps else None}}
