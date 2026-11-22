"""Build minimal JVM class files for tests, so no compiler or third-party JAR is needed."""

from __future__ import annotations

import io
import struct
import zipfile


class Pool:
    def __init__(self) -> None:
        self.entries: list[bytes] = []
        self.index: dict[tuple, int] = {}

    def _add(self, key: tuple, data: bytes) -> int:
        if key not in self.index:
            self.entries.append(data)
            self.index[key] = len(self.entries)
        return self.index[key]

    def utf8(self, text: str) -> int:
        raw = text.encode("utf-8")
        return self._add(("utf8", text), b"\x01" + struct.pack(">H", len(raw)) + raw)

    def cls(self, name: str) -> int:
        return self._add(("class", name), b"\x07" + struct.pack(">H", self.utf8(name)))

    def string(self, text: str) -> int:
        return self._add(("string", text), b"\x08" + struct.pack(">H", self.utf8(text)))

    def integer(self, value: int) -> int:
        return self._add(("int", value), b"\x03" + struct.pack(">i", value))

    def nat(self, name: str, descriptor: str) -> int:
        data = b"\x0c" + struct.pack(">HH", self.utf8(name), self.utf8(descriptor))
        return self._add(("nat", name, descriptor), data)

    def methodref(self, owner: str, name: str, descriptor: str = "()V") -> int:
        data = b"\x0a" + struct.pack(">HH", self.cls(owner), self.nat(name, descriptor))
        return self._add(("method", owner, name, descriptor), data)


def build(
    pool: Pool,
    name: str = "test/Sample",
    code: bytes = b"\xb1",
    major: int = 52,
    minor: int = 0,
) -> bytes:
    """A class with one static method ``run`` whose body is ``code`` (default: return)."""
    this_index = pool.cls(name)
    super_index = pool.cls("java/lang/Object")
    method_name, descriptor, code_name = pool.utf8("run"), pool.utf8("()V"), pool.utf8("Code")
    code_attribute = struct.pack(">HHI", 4, 4, len(code)) + code + struct.pack(">HH", 0, 0)
    method = struct.pack(">HHHH", 0x0009, method_name, descriptor, 1)
    method += struct.pack(">HI", code_name, len(code_attribute)) + code_attribute
    out = b"\xca\xfe\xba\xbe" + struct.pack(">HHH", minor, major, len(pool.entries) + 1)
    out += b"".join(pool.entries)
    out += struct.pack(">HHHHH", 0x0021, this_index, super_index, 0, 0)
    out += struct.pack(">H", 1) + method + struct.pack(">H", 0)
    return out


def jar(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()
