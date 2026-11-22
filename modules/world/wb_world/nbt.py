"""Named Binary Tag, the game's save format: a bounded reader, a canonical encoding and a JSON view.

Every value keeps its tag type through a small subclass of the matching Python type (an ``Int`` is an
``int``), so a comparison can tell an Int from a Long and the canonical encoding can be written again.
"""

from __future__ import annotations

import math
import struct
import zlib

MAX_DEPTH = 512          # the game's own nesting limit
MAX_NODES = 4_000_000    # values in one document; a chunk holds thousands
MAX_BYTES = 64 << 20     # one inflated document


class NbtError(ValueError):
    """Bytes that are not well-formed NBT, or that exceed this reader's bounds."""


class Byte(int):
    kind = 1


class Short(int):
    kind = 2


class Int(int):
    kind = 3


class Long(int):
    kind = 4


class Float(float):
    kind = 5


class Double(float):
    kind = 6


class ByteArray(bytes):
    kind = 7


class String(str):
    kind = 8


class List(list):
    kind = 9

    def __init__(self, items=(), item_kind: int = 0):
        super().__init__(items)
        self.item_kind = item_kind


class Compound(dict):
    kind = 10


class IntArray(tuple):
    kind = 11


class LongArray(tuple):
    kind = 12


_SCALARS = {1: (Byte, ">b", 1), 2: (Short, ">h", 2), 3: (Int, ">i", 4), 4: (Long, ">q", 8),
            5: (Float, ">f", 4), 6: (Double, ">d", 8)}
_ARRAYS = {11: (IntArray, "i", 4), 12: (LongArray, "q", 8)}
# The fewest bytes one list item of each type takes, so a forged item count is caught before allocating.
_ITEM_BYTES = {0: 0, 1: 1, 2: 2, 3: 4, 4: 8, 5: 4, 6: 8, 7: 4, 8: 2, 9: 5, 10: 1, 11: 4, 12: 4}


def inflate(data: bytes, limit: int = MAX_BYTES) -> bytes:
    """A gzip or zlib stream (its header tells which), refused past ``limit`` bytes or when cut short."""
    engine = zlib.decompressobj(32 + zlib.MAX_WBITS)
    try:
        out = engine.decompress(data, limit + 1)
    except zlib.error as error:
        raise NbtError(f"is not gzip or zlib data ({error})") from None
    if len(out) > limit:
        raise NbtError(f"inflates past {limit >> 20} MiB")
    if not engine.eof:
        raise NbtError("ends inside its compressed data")
    return out


def parse(data: bytes) -> Compound:
    """The root compound of one document; its name is dropped (saves leave it empty)."""
    reader = _Reader(data)
    kind = reader.unpack(">B", 1)
    if kind != Compound.kind:
        raise NbtError(f"has tag type {kind} at its root, not a compound")
    reader.string()
    return reader.value(Compound.kind, 0)


class _Reader:
    def __init__(self, data: bytes):
        self.data, self.at, self.nodes = data, 0, 0

    def take(self, size: int) -> bytes:
        end = self.at + size
        if end > len(self.data):
            raise NbtError("ends inside a value")
        chunk, self.at = self.data[self.at:end], end
        return chunk

    def unpack(self, fmt: str, size: int):
        if self.at + size > len(self.data):
            raise NbtError("ends inside a value")
        value, = struct.unpack_from(fmt, self.data, self.at)
        self.at += size
        return value

    def count(self, item_bytes: int) -> int:
        count = self.unpack(">i", 4)
        if count < 0 or count * item_bytes > len(self.data) - self.at:
            raise NbtError(f"declares {count} items, more than the data holds")
        return count

    def string(self) -> str:
        raw = self.take(self.unpack(">H", 2))
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:  # Java's modified UTF-8: NUL as C0 80, characters past U+FFFF as surrogates
            return raw.decode("utf-8", "replace")

    def value(self, kind: int, depth: int):
        # One Python frame per nesting level (lists and compounds are read inline), so the game's depth of
        # 512 stays inside the interpreter's recursion limit.
        self.nodes += 1
        if depth > MAX_DEPTH or self.nodes > MAX_NODES:
            raise NbtError(f"nests deeper than {MAX_DEPTH} or holds more than {MAX_NODES} values")
        if kind in _SCALARS:
            cls, fmt, size = _SCALARS[kind]
            return cls(self.unpack(fmt, size))
        if kind == String.kind:
            return String(self.string())
        if kind == ByteArray.kind:
            return ByteArray(self.take(self.count(1)))
        if kind in _ARRAYS:
            cls, code, size = _ARRAYS[kind]
            count = self.count(size)
            return cls(struct.unpack(f">{count}{code}", self.take(count * size)))
        if kind == List.kind:
            item = self.unpack(">B", 1)
            count = self.count(_ITEM_BYTES.get(item, 1))
            if item == 0 and count:
                raise NbtError("holds a list of end tags")
            if item in _SCALARS:  # a run of numbers in one unpack
                cls, fmt, size = _SCALARS[item]
                self.nodes += count
                return List([cls(v) for v in struct.unpack(f">{count}{fmt[1]}", self.take(count * size))], item)
            return List([self.value(item, depth + 1) for _ in range(count)], item)
        if kind == Compound.kind:
            out = Compound()
            while (tag := self.unpack(">B", 1)) != 0:
                name = self.string()
                out[name] = self.value(tag, depth + 1)  # a repeated key keeps the last value, as the game does
            return out
        raise NbtError(f"holds an unknown tag type {kind}")


def canonical(value) -> bytes:
    """The value's NBT encoding with compound keys sorted, so equal values encode to equal bytes."""
    out = bytearray()
    _encode(value, out)
    return bytes(out)


def _encode(value, out: bytearray) -> None:
    kind = value.kind
    if kind in _SCALARS:
        out += struct.pack(_SCALARS[kind][1], value)
    elif kind == ByteArray.kind:
        out += struct.pack(">i", len(value)) + value
    elif kind == String.kind:
        _name(value, out)
    elif kind == List.kind:
        out += struct.pack(">Bi", value.item_kind, len(value))
        for item in value:
            _encode(item, out)
    elif kind == Compound.kind:
        for key in sorted(value):
            out.append(value[key].kind)
            _name(key, out)
            _encode(value[key], out)
        out.append(0)
    else:
        code = _ARRAYS[kind][1]
        out += struct.pack(f">i{len(value)}{code}", len(value), *value)


def _name(text: str, out: bytearray) -> None:
    raw = text.encode("utf-8")
    out += struct.pack(">H", len(raw)) + raw


def to_json(value):
    """A plain JSON view of a value: numbers, strings, lists and objects; tag types are not kept."""
    if isinstance(value, dict):
        return {key: to_json(item) for key, item in value.items()}
    if isinstance(value, bytes):
        return list(struct.unpack(f">{len(value)}b", value))
    if isinstance(value, (list, tuple)):
        return [to_json(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    return value
