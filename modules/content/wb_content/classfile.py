"""Just enough of the class file format to read what a mod JAR declares about itself: the constant pool, the
class's annotations (``@Mod(modid = ...)``), and the strings a static initializer passes to a constructor
(how GroovyScript declares the mod names it answers to under ``mods.``)."""

from __future__ import annotations

import struct

MODS = ("Lnet/minecraftforge/fml/common/Mod;", "Lnet/neoforged/fml/common/Mod;")
_SIZES = {3: 4, 4: 4, 5: 8, 6: 8, 7: 2, 8: 2, 9: 4, 10: 4, 11: 4, 12: 4, 15: 3, 16: 2, 17: 4, 18: 4, 19: 2, 20: 2}
_OPERANDS = bytearray(256)
for _op in (0x10, 0x12, *range(0x15, 0x1A), *range(0x36, 0x3B), 0xA9, 0xBC):
    _OPERANDS[_op] = 1
for _op in (0x11, 0x13, 0x14, 0x84, *range(0x99, 0xA9), *range(0xB2, 0xB9), 0xBB, 0xBD, 0xC0, 0xC1, 0xC6, 0xC7):
    _OPERANDS[_op] = 2
_OPERANDS[0xC5] = 3
for _op in (0xB9, 0xBA, 0xC8, 0xC9):
    _OPERANDS[_op] = 4
NEW, LDC, LDC_W, INVOKESPECIAL = 0xBB, 0x12, 0x13, 0xB7


class ClassError(ValueError):
    pass


class ClassFile:
    def __init__(self, data: bytes):
        if data[:4] != b"\xca\xfe\xba\xbe":
            raise ClassError("not a class file")
        self.data = data
        self.pool: list = [None]
        at = 10
        count = struct.unpack_from(">H", data, 8)[0]
        try:
            while len(self.pool) < count:
                tag = data[at]
                if tag == 1:
                    length = struct.unpack_from(">H", data, at + 1)[0]
                    self.pool.append((1, data[at + 3:at + 3 + length].decode("utf-8", "replace")))
                    at += 3 + length
                    continue
                size = _SIZES[tag]
                self.pool.append((tag, data[at + 1:at + 1 + size]))
                at += 1 + size
                if tag in (5, 6):
                    self.pool.append(None)
            at += 6
            interfaces = struct.unpack_from(">H", data, at)[0]
            at += 2 + 2 * interfaces
            self.fields, at = self._members(at)
            self.methods, at = self._members(at)
            self.attributes, at = self._attributes(at)
        except (KeyError, IndexError, struct.error) as error:
            raise ClassError(f"malformed class file: {error}") from None

    def _attributes(self, at: int) -> tuple[list[tuple[str, bytes]], int]:
        count = struct.unpack_from(">H", self.data, at)[0]
        at += 2
        out = []
        for _ in range(count):
            name, length = struct.unpack_from(">HI", self.data, at)
            out.append((self.utf8(name), self.data[at + 6:at + 6 + length]))
            at += 6 + length
        return out, at

    def _members(self, at: int) -> tuple[list[tuple[str, str, list]], int]:
        count = struct.unpack_from(">H", self.data, at)[0]
        at += 2
        out = []
        for _ in range(count):
            _, name, descriptor = struct.unpack_from(">HHH", self.data, at)
            attributes, at = self._attributes(at + 6)
            out.append((self.utf8(name), self.utf8(descriptor), attributes))
        return out, at

    def utf8(self, index: int) -> str:
        entry = self.pool[index] if 0 < index < len(self.pool) else None
        if not entry or entry[0] != 1:
            raise ClassError(f"constant {index} is not text")
        return entry[1]

    def string(self, index: int) -> str | None:
        entry = self.pool[index] if 0 < index < len(self.pool) else None
        if not entry or entry[0] != 8:
            return None
        return self.utf8(struct.unpack(">H", entry[1])[0])

    def class_name(self, index: int) -> str:
        entry = self.pool[index]
        return self.utf8(struct.unpack(">H", entry[1])[0]) if entry and entry[0] == 7 else ""

    def member(self, index: int) -> tuple[str, str]:
        """A method or field reference's owner class and name."""
        entry = self.pool[index]
        if not entry or entry[0] not in (9, 10, 11):
            return "", ""
        owner, pair = struct.unpack(">HH", entry[1])
        name = struct.unpack(">HH", self.pool[pair][1])[0]
        return self.class_name(owner), self.utf8(name)

    # -- annotations ---------------------------------------------------------------------------------
    def annotations(self) -> list[tuple[str, dict]]:
        """The class's annotations (visible and invisible): type descriptor and its string/number values."""
        out = []
        for name, body in self.attributes:
            if name in ("RuntimeVisibleAnnotations", "RuntimeInvisibleAnnotations"):
                count = struct.unpack_from(">H", body, 0)[0]
                at = 2
                for _ in range(count):
                    annotation, at = self._annotation(body, at)
                    out.append(annotation)
        return out

    def _annotation(self, body: bytes, at: int) -> tuple[tuple[str, dict], int]:
        kind, pairs = struct.unpack_from(">HH", body, at)
        at += 4
        values = {}
        for _ in range(pairs):
            name = struct.unpack_from(">H", body, at)[0]
            value, at = self._element(body, at + 2)
            values[self.utf8(name)] = value
        return (self.utf8(kind), values), at

    def _element(self, body: bytes, at: int):
        tag = chr(body[at])
        at += 1
        if tag == "s":
            return self.utf8(struct.unpack_from(">H", body, at)[0]), at + 2
        if tag in "BCDFIJSZc":
            return None, at + 2
        if tag == "e":
            return None, at + 4
        if tag == "@":
            return self._annotation(body, at)
        if tag == "[":
            count = struct.unpack_from(">H", body, at)[0]
            at += 2
            items = []
            for _ in range(count):
                item, at = self._element(body, at)
                items.append(item)
            return items, at
        raise ClassError(f"unknown element value tag {tag!r}")

    # -- code ----------------------------------------------------------------------------------------------
    def code(self, method: str) -> bytes | None:
        for name, _, attributes in self.methods:
            if name == method:
                for attribute, body in attributes:
                    if attribute == "Code":
                        length = struct.unpack_from(">I", body, 4)[0]
                        return body[8:8 + length]
        return None

    def constructed(self, method: str, owner_suffix: str) -> list[list[str]]:
        """For each ``new X(...)`` in a method whose class ends with ``owner_suffix``: the string constants loaded
        between the ``new`` and the constructor call, in order."""
        code = self.code(method)
        if code is None:
            return []
        out, open_ = [], []
        at = 0
        while at < len(code):
            op = code[at]
            if op == 0xAA or op == 0xAB:  # tableswitch, lookupswitch: aligned operands
                pad = (4 - (at + 1) % 4) % 4
                base = at + 1 + pad
                if op == 0xAA:
                    low, high = struct.unpack_from(">ii", code, base + 4)
                    at = base + 12 + 4 * (high - low + 1)
                else:
                    pairs = struct.unpack_from(">i", code, base + 4)[0]
                    at = base + 8 + 8 * pairs
                continue
            if op == 0xC4:  # wide
                at += 6 if code[at + 1] == 0x84 else 4
                continue
            if op == NEW and self.class_name(struct.unpack_from(">H", code, at + 1)[0]).endswith(owner_suffix):
                open_.append([])
            elif op in (LDC, LDC_W) and open_:
                index = code[at + 1] if op == LDC else struct.unpack_from(">H", code, at + 1)[0]
                text = self.string(index)
                if text is not None:
                    open_[-1].append(text)
            elif op == INVOKESPECIAL and open_:
                owner, name = self.member(struct.unpack_from(">H", code, at + 1)[0])
                if name == "<init>" and owner.endswith(owner_suffix):
                    out.append(open_.pop())
            at += 1 + _OPERANDS[op]
        return out


def mod_ids(data: bytes) -> list[str]:
    """The mod ids a class's @Mod annotation declares (modid, or value on newer loaders)."""
    if not any(kind.encode() in data for kind in MODS):
        return []
    out = []
    for kind, values in ClassFile(data).annotations():
        if kind in MODS:
            modid = values.get("modid", values.get("value"))
            if isinstance(modid, str) and modid:
                out.append(modid)
    return out
