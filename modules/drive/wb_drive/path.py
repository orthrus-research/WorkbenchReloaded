"""Camera paths: poses at a frame rate through keyframes, and the route format that holds them (wb/route/v1).

Keyframes come as a JSON or TOML list, or a table with keyframes (a tour's views serve as well). A keyframe is a
table of

    x, y, z, yaw, pitch    a pose, as in a tour (yaw 0 faces south, 90 west; pitch 90 looks straight down)
    label                  optional: names it in segments and findings (default k0, k1, ...)
    seconds                optional: the time to travel to it from the keyframe before; by default the time the
                           distance takes at the speed, or the turn at the turn speed, whichever is longer
    hold                   optional: seconds to rest at it; a keyframe with a hold is a stop
    cut                    optional: true jumps to it from the keyframe before in one frame (a camera cut)

The path rests at its first and last keyframes and at every stop. Ease pacing leaves and reaches a stop over the
ramp, a smooth (cosine) change of speed; constant pacing moves at full speed at once. Between stops the path
keeps each segment's own pace: its length at the speed, or its turn at the turn speed. Positions follow a
centripetal Catmull-Rom curve through the keyframes (or straight lines between them), at an even speed along each
segment; yaw and pitch follow monotone Hermite splines (or straight lines), so a turn never overshoots a keyframe, and
yaw takes the shorter way round from one keyframe to the next.

A route (wb/route/v1) is a table of

    format, fps, interpolation, pacing, speed, turnSpeed, ramp    how it was made
    frames, seconds, length    poses, the time from the first to the last, blocks travelled (cuts left out)
    keyframes                  the keyframes as read (yaw as travelled, the shorter way round)
    segments                   in order, each frame in exactly one: {index, kind (hold, move or cut), label, from,
                               to, first, last, frames, seconds, length, turn}
    poses                      one a frame: {frame, t, x, y, z, yaw, pitch, segment, phase (hold, move or cut)}

Pose yaw is continuous: it does not wrap at 180, so a player turned to it frame by frame never spins round.
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass, field

from .tour import VIEW_KEYS, Finding, number, normal_yaw, tidy

FORMAT = "wb/route/v1"
KEYFRAME_KEYS = {"label", "x", "y", "z", "yaw", "pitch", "seconds", "hold", "cut"}
EPSILON = 1e-9


class PathError(ValueError):
    """Keyframes (or a route) that cannot be read as the format says."""


@dataclass
class Keyframe:
    index: int
    label: str
    pos: tuple[float, float, float]
    yaw: float              # as travelled: the shorter way round from the keyframe before
    pitch: float
    seconds: float | None
    hold: float | None
    cut: bool

    def describe(self) -> dict:
        out = {"label": self.label, "x": tidy(self.pos[0]), "y": tidy(self.pos[1]), "z": tidy(self.pos[2]),
               "yaw": tidy(self.yaw, 3), "pitch": tidy(self.pitch, 3)}
        for key in ("seconds", "hold"):
            if getattr(self, key) is not None:
                out[key] = getattr(self, key)
        if self.cut:
            out["cut"] = True
        return out


def keyframes(document) -> list[Keyframe]:
    """Keyframes from a list, a table with keyframes, or a tour (its views)."""
    known = KEYFRAME_KEYS
    if isinstance(document, dict) and isinstance(document.get("keyframes"), list):
        raw = document["keyframes"]
    elif isinstance(document, dict) and isinstance(document.get("views"), list):
        raw, known = document["views"], KEYFRAME_KEYS | VIEW_KEYS
    elif isinstance(document, list):
        raw = document
        if any(isinstance(item, dict) and set(item) & (VIEW_KEYS - KEYFRAME_KEYS) for item in raw):
            known = KEYFRAME_KEYS | VIEW_KEYS  # a tour as a bare list of views
    else:
        raise PathError("keyframes are a list, or a table with keyframes (or a tour's views)")
    if not raw:
        raise PathError("there are no keyframes")
    out: list[Keyframe] = []
    for index, item in enumerate(raw):
        where = f"keyframe {index}"
        if not isinstance(item, dict):
            raise PathError(f"{where} is not a table")
        unknown = sorted(k for k in set(item) - known if not str(k).startswith("x-"))
        if unknown:
            raise PathError(f"{where} has unknown keys {', '.join(map(str, unknown))} (known: "
                            f"{', '.join(sorted(KEYFRAME_KEYS))})")
        values = {}
        for key in ("x", "y", "z", "yaw", "pitch"):
            if not number(item.get(key)):
                raise PathError(f"{where} needs {key} as a number")
            values[key] = float(item[key])
        if not -90 <= values["pitch"] <= 90:
            raise PathError(f"{where} has pitch {values['pitch']:g}, outside -90..90")
        label = item.get("label", f"k{index}")
        if not isinstance(label, str) or not label:
            raise PathError(f"{where} has a label that is not a name")
        seconds, hold, cut = item.get("seconds"), item.get("hold"), item.get("cut", False)
        if seconds is not None and (not number(seconds) or seconds <= 0):
            raise PathError(f"{where} has seconds {seconds!r}: a time above 0")
        if hold is not None and (not number(hold) or hold < 0):
            raise PathError(f"{where} has hold {hold!r}: a time of at least 0")
        if not isinstance(cut, bool):
            raise PathError(f"{where} has cut {cut!r}: true or false")
        if cut and index == 0:
            raise PathError("the first keyframe cannot be a cut: there is nothing to cut from")
        if cut and seconds is not None:
            raise PathError(f"{where} is a cut, which takes no time: it cannot say seconds")
        yaw = values["yaw"] if not out else out[-1].yaw + normal_yaw(values["yaw"] - out[-1].yaw)
        out.append(Keyframe(index, label, (values["x"], values["y"], values["z"]), yaw, values["pitch"],
                            float(seconds) if seconds is not None else None,
                            float(hold) if hold is not None else None, cut))
    return out


# -- curves ----------------------------------------------------------------------------------------------------------
def _lerp(a, b, w):
    return tuple(x + (y - x) * w for x, y in zip(a, b))


def _reflect(edge, inner):
    return tuple(2 * e - i for e, i in zip(edge, inner))


def _catmull_rom(p0, p1, p2, p3, u: float):
    """The centripetal Catmull-Rom curve from p1 (u = 0) to p2 (u = 1)."""
    def knot(a, b):
        return max(math.dist(a, b) ** 0.5, 1e-6)
    t0, t1 = 0.0, knot(p0, p1)
    t2 = t1 + knot(p1, p2)
    t3 = t2 + knot(p2, p3)
    t = t1 + (t2 - t1) * u

    def mix(a, b, ta, tb):
        return _lerp(a, b, (t - ta) / (tb - ta))
    a1, a2, a3 = mix(p0, p1, t0, t1), mix(p1, p2, t1, t2), mix(p2, p3, t2, t3)
    return mix(mix(a1, a2, t0, t2), mix(a2, a3, t1, t3), t1, t2)


def _hermite(y1: float, y2: float, m1: float, m2: float, q: float) -> float:
    q2, q3 = q * q, q * q * q
    return (2 * q3 - 3 * q2 + 1) * y1 + (q3 - 2 * q2 + q) * m1 + (-2 * q3 + 3 * q2) * y2 + (q3 - q2) * m2


def _tangents(values: list[float], spans: list[float]) -> list[float]:
    """Monotone slopes (a unit a second) at each keyframe of a piece, for spans of the given seconds: zero where the
    values turn back or stand still, the harmonic mean of the slopes either side elsewhere."""
    if len(values) < 2:
        return [0.0] * len(values)
    slopes = [(b - a) / h if h > EPSILON else 0.0 for a, b, h in zip(values, values[1:], spans)]
    out = [slopes[0]]
    for left, right in zip(slopes, slopes[1:]):
        out.append(0.0 if left * right <= 0 else 2 * left * right / (left + right))
    out.append(slopes[-1])
    return out


class Segment:
    """The move from one keyframe to the next: a curve through space measured along its length, and turns."""

    def __init__(self, keys: list[Keyframe], i: int, piece: range, smooth: bool, speed: float, turn_speed: float):
        self.i, self.a, self.b = i, keys[i], keys[i + 1]
        p1, p2 = self.a.pos, self.b.pos
        self.still = math.dist(p1, p2) < 1e-6
        if smooth and not self.still:
            p0 = keys[i - 1].pos if i - 1 in piece else _reflect(p1, p2)
            p3 = keys[i + 2].pos if i + 2 in piece else _reflect(p2, p1)
            p0 = p0 if math.dist(p0, p1) > 1e-6 else _reflect(p1, p2)
            p3 = p3 if math.dist(p3, p2) > 1e-6 else _reflect(p2, p1)
            self.curve = lambda u: _catmull_rom(p0, p1, p2, p3, u)
            count = max(32, min(4096, 8 * math.ceil(math.dist(p1, p2) + 1)))
        else:
            self.curve = lambda u: _lerp(p1, p2, u)
            count = 1
        points = [self.curve(j / count) for j in range(count + 1)]
        self.cumulative = [0.0]
        for a, b in zip(points, points[1:]):
            self.cumulative.append(self.cumulative[-1] + math.dist(a, b))
        self.count = count
        self.length = 0.0 if self.still else self.cumulative[-1]
        self.turn = max(abs(self.b.yaw - self.a.yaw), abs(self.b.pitch - self.a.pitch))
        planned = max(self.length / speed, self.turn / turn_speed)
        self.seconds = self.b.seconds if self.b.seconds is not None else planned
        self.yaw_slopes = self.pitch_slopes = (0.0, 0.0)

    def position(self, q: float):
        if self.still:
            return self.a.pos
        if self.count == 1:
            return self.curve(q)
        s = q * self.length
        j = min(max(bisect.bisect_right(self.cumulative, s) - 1, 0), self.count - 1)
        step = self.cumulative[j + 1] - self.cumulative[j]
        fraction = (s - self.cumulative[j]) / step if step > EPSILON else 0.0
        return self.curve((j + min(max(fraction, 0.0), 1.0)) / self.count)

    def rotation(self, q: float) -> tuple[float, float]:
        h = self.seconds
        yaw = _hermite(self.a.yaw, self.b.yaw, self.yaw_slopes[0] * h, self.yaw_slopes[1] * h, q)
        pitch = _hermite(self.a.pitch, self.b.pitch, self.pitch_slopes[0] * h, self.pitch_slopes[1] * h, q)
        return yaw, min(90.0, max(-90.0, pitch))


# -- the route ------------------------------------------------------------------------------------------------------
@dataclass
class Span:
    """A segment of the route as it reports it: a hold at a keyframe, a move between two, or a cut."""
    index: int
    kind: str
    label: str
    source: str
    target: str
    length: float = 0.0
    turn: float = 0.0
    planned: float = 0.0
    first: int | None = None
    last: int | None = None
    frames: int = 0

    def describe(self, fps: float) -> dict:
        return {"index": self.index, "kind": self.kind, "label": self.label, "from": self.source, "to": self.target,
                "first": self.first, "last": self.last, "frames": self.frames,
                "seconds": round(self.frames / fps, 4), "planned": round(self.planned, 4),
                "length": round(self.length, 3), "turn": round(self.turn, 3)}


@dataclass
class Route:
    fps: float
    interpolation: str
    pacing: str
    speed: float
    turn_speed: float
    ramp: float
    keys: list[Keyframe]
    spans: list[Span] = field(default_factory=list)
    poses: list[tuple] = field(default_factory=list)   # (x, y, z, yaw, pitch, span index, phase)

    @property
    def seconds(self) -> float:
        return (len(self.poses) - 1) / self.fps if self.poses else 0.0

    @property
    def length(self) -> float:
        return sum(span.length for span in self.spans if span.kind == "move")

    def emit(self, pos, yaw: float, pitch: float, span: Span, phase: str) -> None:
        frame = len(self.poses)
        self.poses.append((*pos, yaw, pitch, span.index, phase))
        span.first = frame if span.first is None else span.first
        span.last = frame
        span.frames += 1

    def document(self) -> dict:
        return {"format": FORMAT, "fps": self.fps, "interpolation": self.interpolation, "pacing": self.pacing,
                "speed": self.speed, "turnSpeed": self.turn_speed, "ramp": self.ramp, "frames": len(self.poses),
                "seconds": round(self.seconds, 4), "length": round(self.length, 3),
                "keyframes": [key.describe() for key in self.keys],
                "segments": [span.describe(self.fps) for span in self.spans],
                "poses": [{"frame": frame, "t": round(frame / self.fps, 4), "x": tidy(p[0], 4), "y": tidy(p[1], 4),
                           "z": tidy(p[2], 4), "yaw": tidy(p[3], 3), "pitch": tidy(p[4], 3), "segment": p[5],
                           "phase": p[6]} for frame, p in enumerate(self.poses)]}


def _ramped(t: float, ramp: float) -> float:
    """Nominal time covered by t seconds of a cosine ramp from rest that reaches full pace after ramp seconds."""
    return t / 2 - ramp / (2 * math.pi) * math.sin(math.pi * t / ramp)


def build(keys: list[Keyframe], fps: float, interpolation: str = "catmull-rom", pacing: str = "ease",
          speed: float = 4.0, turn_speed: float = 30.0, ramp: float = 1.0) -> Route:
    route = Route(fps, interpolation, pacing, speed, turn_speed, ramp, keys)
    last = len(keys) - 1
    starts = [0] + [i for i in range(1, len(keys)) if keys[i].cut] + [len(keys)]
    pieces = [range(a, b) for a, b in zip(starts, starts[1:])]
    segments: dict[int, Segment] = {}
    for piece in pieces:
        for i in list(piece)[:-1]:
            segments[i] = Segment(keys, i, piece, interpolation == "catmull-rom", speed, turn_speed)
        indices = list(piece)
        spans = [segments[i].seconds for i in indices[:-1]]
        for axis in ("yaw", "pitch"):
            slopes = _tangents([getattr(keys[i], axis) for i in indices], spans)
            for n, i in enumerate(indices[:-1]):
                setattr(segments[i], f"{axis}_slopes", (slopes[n], slopes[n + 1]))

    def stop(k: int) -> bool:
        return k in (0, last) or keys[k].hold is not None

    def hold(k: int, include_start: bool = False) -> None:
        frames = round((keys[k].hold or 0.0) * fps) + (1 if include_start else 0)
        if frames <= 0:
            return
        span = Span(len(route.spans), "hold", keys[k].label, keys[k].label, keys[k].label,
                    planned=keys[k].hold or 0.0)
        route.spans.append(span)
        for _ in range(frames):
            route.emit(keys[k].pos, keys[k].yaw, keys[k].pitch, span, "hold")

    hold(0, include_start=True)
    k = 0
    while k < last:
        if keys[k + 1].cut:
            a, b = keys[k], keys[k + 1]
            span = Span(len(route.spans), "cut", f"{a.label}-{b.label}", a.label, b.label,
                        length=math.dist(a.pos, b.pos), turn=max(abs(b.yaw - a.yaw), abs(b.pitch - a.pitch)))
            route.spans.append(span)
            route.emit(b.pos, b.yaw, b.pitch, span, "cut")
            k += 1
            hold(k)
            continue
        end = k + 1
        while end < last and not stop(end) and not keys[end + 1].cut:
            end += 1
        _run(route, [segments[i] for i in range(k, end)], pacing == "ease" and stop(k),
             pacing == "ease" and stop(end))
        k = end
        hold(k)
    return route


def _run(route: Route, run: list[Segment], ease_in: bool, ease_out: bool) -> None:
    """Samples a run of segments between two places the path does not pass at full pace."""
    spans = []
    for segment in run:
        span = Span(len(route.spans), "move", f"{segment.a.label}-{segment.b.label}", segment.a.label,
                    segment.b.label, length=segment.length, turn=segment.turn, planned=segment.seconds)
        route.spans.append(span)
        spans.append(span)
    durations = [segment.seconds for segment in run]
    nominal = sum(durations)
    if nominal <= EPSILON:
        return
    ramps = int(ease_in) + int(ease_out)
    ramp = 0.0 if ramps == 0 else min(route.ramp, nominal if ramps == 2 else 2 * nominal)
    total = nominal + ramp * ramps / 2
    starts = [0.0]
    for duration in durations[:-1]:
        starts.append(starts[-1] + duration)

    def timing(t: float) -> float:
        if ramp and ease_in and t < ramp:
            return _ramped(t, ramp)
        if ramp and ease_out and t > total - ramp:
            return nominal - _ramped(total - t, ramp)
        return t - (ramp / 2 if ease_in else 0.0)

    frames = max(1, math.ceil(total * route.fps - 1e-6))
    for j in range(1, frames + 1):
        tau = nominal if j == frames else min(max(timing(total * j / frames), 0.0), nominal)
        n = min(max(bisect.bisect_left(starts, tau) - 1, 0), len(run) - 1)  # a frame on a keyframe arrives there
        while durations[n] <= EPSILON and n > 0:
            n -= 1
        segment = run[n]
        q = (tau - starts[n]) / durations[n] if durations[n] > EPSILON else 1.0
        q = min(max(q, 0.0), 1.0)
        yaw, pitch = segment.rotation(q)
        route.emit(segment.position(q), yaw, pitch, spans[n], "move")


# -- limits ---------------------------------------------------------------------------------------------------------
def motion(route: Route) -> list[dict]:
    """Per frame: speed (blocks a second), yaw and pitch rates (degrees a second) and acceleration (blocks a second
    squared) into it from the frame before; None into a cut and, for acceleration, out of one."""
    fps, rows, velocity = route.fps, [], (0.0, 0.0, 0.0)
    for frame, pose in enumerate(route.poses):
        if frame == 0:
            rows.append({"speed": 0.0, "yaw": 0.0, "pitch": 0.0, "accel": 0.0})
            continue
        before = route.poses[frame - 1]
        if pose[6] == "cut":
            rows.append({"speed": None, "yaw": None, "pitch": None, "accel": None})
            velocity = None
            continue
        step = tuple((a - b) * fps for a, b in zip(pose[:3], before[:3]))
        accel = None if velocity is None else math.dist(step, velocity) * fps
        rows.append({"speed": math.hypot(*step), "yaw": abs(pose[3] - before[3]) * fps,
                     "pitch": abs(pose[4] - before[4]) * fps, "accel": accel})
        velocity = step
    if len(route.poses) > 1 and route.poses[-1][6] != "cut" and velocity is not None:
        rows[-1]["stop"] = math.hypot(*velocity) * fps  # coming to rest after the last frame
    return rows


def check(route: Route, limits: dict[str, float]) -> tuple[list[Finding], dict]:
    """Findings where the path breaks a limit (max_speed, max_turn, max_accel, max_seconds; 0 or None leaves one
    unchecked), one per stretch of frames over it within a segment; and the path's peaks."""
    rows = motion(route)
    peaks = {"speed": 0.0, "turn": 0.0, "accel": 0.0}
    for row in rows:
        if row["speed"] is not None:
            peaks["speed"] = max(peaks["speed"], row["speed"])
            peaks["turn"] = max(peaks["turn"], row["yaw"], row["pitch"])
        if row["accel"] is not None:
            peaks["accel"] = max(peaks["accel"], row["accel"], row.get("stop", 0.0))
    findings = []
    measures = (("max_speed", "speed", "blocks/s", lambda r: r["speed"]),
                ("max_turn", "turn", "degrees/s", lambda r: None if r["yaw"] is None else max(r["yaw"], r["pitch"])),
                ("max_accel", "accel", "blocks/s2",
                 lambda r: None if r["accel"] is None else max(r["accel"], r.get("stop", 0.0))))
    for key, rule, unit, value in measures:
        limit = limits.get(key) or 0.0
        if not limit:
            continue
        stretch = None
        for frame, row in enumerate(rows + [None]):
            over = row is not None and value(row) is not None and value(row) > limit + 1e-9
            span = route.poses[frame][5] if row is not None else None
            if stretch and (not over or span != stretch["span"]):
                findings.append(_over(route, rule, unit, limit, stretch))
                stretch = None
            if over:
                if stretch is None:
                    stretch = {"span": span, "first": frame, "last": frame, "peak": value(row), "at": frame}
                else:
                    stretch["last"] = frame
                    if value(row) > stretch["peak"]:
                        stretch["peak"], stretch["at"] = value(row), frame
    longest = limits.get("max_seconds") or 0.0
    if longest and route.seconds > longest + 1e-9:
        findings.append(Finding("length", f"the path takes {route.seconds:,.2f} s; its limit is {longest:,.2f} s"))
    return findings, {key: round(value, 4) for key, value in peaks.items()}


def _over(route: Route, rule: str, unit: str, limit: float, stretch: dict) -> Finding:
    span = route.spans[stretch["span"]]
    frames = (f"frame {stretch['first']}" if stretch["first"] == stretch["last"]
              else f"frames {stretch['first']}-{stretch['last']}")
    return Finding(rule, f"segment {span.label} reaches {stretch['peak']:,.2f} {unit} at frame {stretch['at']}, over "
                         f"the limit of {limit:g} ({frames})")


# -- reading a route -------------------------------------------------------------------------------------------------
@dataclass
class Read:
    fps: float
    frames: int
    segments: list[dict]
    poses: list[dict]


def read(document) -> Read:
    """A route as drive.path writes it, checked enough to compare captures with."""
    if not isinstance(document, dict) or document.get("format") != FORMAT:
        raise PathError(f"a route is a table with format {FORMAT!r}")
    fps, poses, segments = document.get("fps"), document.get("poses"), document.get("segments")
    if not number(fps) or fps <= 0 or not isinstance(poses, list) or not isinstance(segments, list):
        raise PathError("a route needs fps, poses and segments")
    for index, pose in enumerate(poses):
        if not isinstance(pose, dict) or pose.get("frame") != index or not isinstance(pose.get("segment"), int) \
                or not 0 <= pose["segment"] < len(segments):
            raise PathError(f"route pose {index} is not frame {index} of a known segment")
        if not all(number(pose.get(key)) for key in ("x", "y", "z", "yaw", "pitch")) \
                or pose.get("phase") not in ("hold", "move", "cut"):
            raise PathError(f"route pose {index} needs x, y, z, yaw, pitch and a phase (hold, move or cut)")
    if not all(isinstance(segment, dict) for segment in segments):
        raise PathError("route segments are tables")
    return Read(float(fps), len(poses), segments, poses)
