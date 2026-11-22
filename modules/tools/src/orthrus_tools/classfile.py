"""Read the constant pool and method bodies of a JVM class file without loading or running it.

Only what the survey and audit tools need is decoded: the version, this class's name, UTF-8
strings, class names, member references, string and integer constants and each method's
``Code`` attribute. Malformed input never raises; it yields
whatever was read before the damage, so one bad class cannot stop a survey of thousands.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

MAGIC = b"\xca\xfe\xba\xbe"

# Constant pool tags and the bytes each entry occupies after its tag (None: variable or special).
_FIXED_SIZES = {3: 4, 4: 4, 5: 8, 6: 8, 7: 2, 8: 2, 9: 4, 10: 4, 11: 4, 12: 4, 15: 3, 16: 2}
_FIXED_SIZES.update({17: 4, 18: 4, 19: 2, 20: 2})


@dataclass
class ClassFile:
    utf8: dict[int, str] = field(default_factory=dict)
    classes: dict[int, int] = field(default_factory=dict)
    name_and_type: dict[int, int] = field(default_factory=dict)
    member_refs: dict[int, tuple[int, int]] = field(default_factory=dict)
    integers: dict[int, int] = field(default_factory=dict)
    method_bodies: list[tuple[str, bytes]] = field(default_factory=list)
    string_literals: set[int] = field(default_factory=set)
    """Pool indexes of the UTF-8 entries that ``String`` constants (``ldc`` literals) use."""
    minor: int = 0
    major: int = 0
    this_class: str = ""
    """This class's internal name, like ``a/b/C``; empty when the header was not reached."""
    complete: bool = False

    @property
    def strings(self) -> set[str]:
        return set(self.utf8.values())

    @property
    def class_names(self) -> set[str]:
        return {self.utf8.get(index, "") for index in self.classes.values()}

    def member(self, pool_index: int) -> tuple[str, str]:
        """(owner internal name, member name) of a Fieldref, Methodref or InterfaceMethodref."""
        class_index, nat_index = self.member_refs[pool_index]
        owner = self.utf8.get(self.classes.get(class_index, 0), "")
        name = self.utf8.get(self.name_and_type.get(nat_index, 0), "")
        return owner, name

    @property
    def methods(self) -> set[tuple[str, str]]:
        return {self.member(index) for index in self.member_refs}


def parse(data: bytes) -> ClassFile:
    """Decode what can be decoded; ``complete`` says whether the whole structure was read."""
    result = ClassFile()
    if data[:4] != MAGIC or len(data) < 10:
        return result
    result.minor, result.major = struct.unpack_from(">HH", data, 4)
    try:
        count = struct.unpack_from(">H", data, 8)[0]
        position, index = 10, 1
        while index < count:
            tag = data[position]
            if tag == 1:
                length = struct.unpack_from(">H", data, position + 1)[0]
                if position + 3 + length > len(data):
                    return result
                raw = data[position + 3 : position + 3 + length]
                result.utf8[index] = raw.decode("utf-8", "replace")
                position += 3 + length
            elif tag in _FIXED_SIZES:
                body = position + 1
                if tag == 3:
                    result.integers[index] = struct.unpack_from(">i", data, body)[0]
                elif tag == 7:
                    result.classes[index] = struct.unpack_from(">H", data, body)[0]
                elif tag == 8:
                    result.string_literals.add(struct.unpack_from(">H", data, body)[0])
                elif tag in (9, 10, 11):
                    result.member_refs[index] = struct.unpack_from(">HH", data, body)
                elif tag == 12:
                    result.name_and_type[index] = struct.unpack_from(">H", data, body)[0]
                position = body + _FIXED_SIZES[tag]
                if tag in (5, 6):  # long and double take two pool slots
                    index += 1
            else:
                return result
            index += 1
        # access flags, this class, super class, then the interfaces table
        this_index = struct.unpack_from(">H", data, position + 2)[0]
        result.this_class = result.utf8.get(result.classes.get(this_index, 0), "")
        position += 6
        position += 2 + 2 * struct.unpack_from(">H", data, position)[0]
        for members in ("fields", "methods"):
            member_count = struct.unpack_from(">H", data, position)[0]
            position += 2
            for _ in range(member_count):
                name_index = struct.unpack_from(">H", data, position + 2)[0]
                attribute_count = struct.unpack_from(">H", data, position + 6)[0]
                position += 8
                for _ in range(attribute_count):
                    attribute_name, length = struct.unpack_from(">HI", data, position)
                    if position + 6 + length > len(data):
                        return result
                    body_bytes = data[position + 6 : position + 6 + length]
                    position += 6 + length
                    if members == "methods" and result.utf8.get(attribute_name) == "Code":
                        result.method_bodies.append((result.utf8.get(name_index, ""), body_bytes))
        # the class's own attributes: their count must be present for the file to be whole
        struct.unpack_from(">H", data, position)
        result.complete = True
    except (IndexError, struct.error):
        pass
    return result
