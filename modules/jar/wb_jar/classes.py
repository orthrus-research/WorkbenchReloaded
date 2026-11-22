"""What a class file declares and calls, read without loading or running it.

``orthrus_tools.classfile`` decodes the constant pool for the survey and the audit. This reader fills the
same ``ClassFile`` in one pass and adds what a static analysis of mods needs: the superclass and
interfaces, each field and method with its descriptor and annotations, each method's bytecode, the class's
own annotations, and what every ``invokedynamic`` binds. As there, malformed input never raises: whatever
was read before the damage is kept, and ``complete`` says whether the whole class was read.
"""

from __future__ import annotations

import struct
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_SRC = Path(__file__).resolve().parents[2] / "tools" / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from orthrus_tools import classfile  # noqa: E402

ACC_PRIVATE, ACC_STATIC, ACC_INTERFACE = 0x0002, 0x0008, 0x0200
_ANNOTATIONS = {"RuntimeVisibleAnnotations": True, "RuntimeInvisibleAnnotations": False}
INVOKES = {0xB6: "virtual", 0xB7: "special", 0xB8: "static", 0xB9: "interface"}
_INVOKEDYNAMIC = 0xBA
_NESTING = 32  # annotations nest no deeper than this; deeper input is damage


def _sizes() -> list[int]:
    """Operand bytes after each opcode; -1 for an opcode that is not an instruction, -2 for the
    variable-length ones (tableswitch, lookupswitch, wide)."""
    sizes = [0] * 0xCA + [-1] * (256 - 0xCA)
    for op in (0x10, 0x12, *range(0x15, 0x1A), *range(0x36, 0x3B), 0xA9, 0xBC):
        sizes[op] = 1
    for op in (0x11, 0x13, 0x14, 0x84, *range(0x99, 0xA9), *range(0xB2, 0xB9), 0xBB, 0xBD, 0xC0, 0xC1,
               0xC6, 0xC7):
        sizes[op] = 2
    sizes[0xC5] = 3
    for op in (0xB9, 0xBA, 0xC8, 0xC9):
        sizes[op] = 4
    sizes[0xAA] = sizes[0xAB] = sizes[0xC4] = -2
    return sizes


_SIZES = _sizes()
# Instructions whose operand is a constant pool index (ldc's is one byte).
_POOL_OPERANDS = {0x12, 0x13, 0x14, *range(0xB2, 0xBC), 0xBD, 0xC0, 0xC1, 0xC5}


def instructions(code: bytes) -> Iterator[tuple[int, int]]:
    """(opcode, constant pool index) of every instruction in a method body that names a pool entry.
    A damaged body ends the walk where the damage starts."""
    position, end = 0, len(code)
    while position < end:
        op = code[position]
        size = _SIZES[op]
        if size >= 0:
            if position + 1 + size > end:
                return
            if op in _POOL_OPERANDS:
                yield op, code[position + 1] if op == 0x12 else (code[position + 1] << 8) | code[position + 2]
            position += 1 + size
        elif op in (0xAA, 0xAB):  # tableswitch, lookupswitch: padded to a multiple of four
            base = (position + 4) & ~3
            if base + (12 if op == 0xAA else 8) > end:
                return
            if op == 0xAA:
                low, high = struct.unpack_from(">ii", code, base + 4)
                if high < low:
                    return
                position = base + 12 + 4 * (high - low + 1)
            else:
                pairs = struct.unpack_from(">i", code, base + 4)[0]
                if pairs < 0:
                    return
                position = base + 8 + 8 * pairs
        elif op == 0xC4:  # wide: iinc takes two more bytes than the loads and stores
            if position + 1 >= end:
                return
            position += 6 if code[position + 1] == 0x84 else 4
        else:
            return


@dataclass
class Annotation:
    type: str
    """A field descriptor, such as ``Lorg/spongepowered/asm/mixin/Mixin;``."""
    values: dict[str, Any] = field(default_factory=dict)
    """Element name -> value: str, int, bool, float, a class literal as its descriptor, an enum as
    ``{"enum": type, "value": name}``, a nested Annotation or a list of these."""
    visible: bool = True

    def get(self, name: str, default: Any = None) -> Any:
        return self.values.get(name, default)

    def strings(self, name: str) -> list[str]:
        """A string (or class literal) element, or an array of them, as a list."""
        value = self.values.get(name)
        if value is None:
            return []
        return [str(v) for v in value] if isinstance(value, list) else [str(value)]

    def nested(self, name: str) -> list[Annotation]:
        """A nested annotation element, or an array of them, as a list."""
        value = self.values.get(name)
        items = value if isinstance(value, list) else [value]
        return [v for v in items if isinstance(v, Annotation)]

    def as_json(self) -> dict:
        return {"type": self.type, "values": {k: _json(v) for k, v in self.values.items()}}


def _json(value: Any) -> Any:
    if isinstance(value, Annotation):
        return value.as_json()
    if isinstance(value, list):
        return [_json(v) for v in value]
    return value


@dataclass
class Member:
    access: int
    name: str
    descriptor: str
    annotations: list[Annotation] = field(default_factory=list)
    code: bytes = b""
    """A method's bytecode; empty for a field and for an abstract or native method."""

    def annotation(self, *types: str) -> Annotation | None:
        return next((a for a in self.annotations if a.type in types), None)

    @property
    def static(self) -> bool:
        return bool(self.access & ACC_STATIC)

    @property
    def private(self) -> bool:
        return bool(self.access & ACC_PRIVATE)


@dataclass
class ClassInfo(classfile.ClassFile):
    """A ``classfile.ClassFile`` (its pool maps filled the same way) with the declarations added."""

    access: int = 0
    super_name: str = ""
    interfaces: list[str] = field(default_factory=list)
    declared_fields: list[Member] = field(default_factory=list)
    declared_methods: list[Member] = field(default_factory=list)
    annotations: list[Annotation] = field(default_factory=list)
    descriptors: dict[int, int] = field(default_factory=dict)
    """NameAndType pool index -> its descriptor's UTF-8 index."""
    numbers: dict[int, float] = field(default_factory=dict)
    """Float, long and double constants by pool index."""
    handles: dict[int, int] = field(default_factory=dict)
    """MethodHandle pool index -> the member reference it points at."""
    dynamics: dict[int, int] = field(default_factory=dict)
    """InvokeDynamic pool index -> its bootstrap method's index."""
    bootstrap: list[list[int]] = field(default_factory=list)
    """Each bootstrap method's static arguments, as pool indexes."""

    @property
    def interface(self) -> bool:
        return bool(self.access & ACC_INTERFACE)

    def ref(self, index: int) -> tuple[str, str, str]:
        """(owner, name, descriptor) of a Fieldref, Methodref or InterfaceMethodref."""
        owner, name = self.member(index)
        nat = self.member_refs[index][1]
        return owner, name, self.utf8.get(self.descriptors.get(nat, 0), "")

    def annotation(self, *types: str) -> Annotation | None:
        return next((a for a in self.annotations if a.type in types), None)

    def method(self, name: str, descriptor: str | None = None) -> Member | None:
        return next((m for m in self.declared_methods if m.name == name
                     and (descriptor is None or m.descriptor == descriptor)), None)

    def calls(self, method: Member) -> Iterator[tuple[str, str, str, str]]:
        """(how, owner, name, descriptor) of each method a body invokes, or binds as a method handle
        through ``invokedynamic`` (a method reference or a lambda's body): how is virtual, special,
        static, interface or handle."""
        for op, index in instructions(method.code):
            if op in INVOKES:
                if index in self.member_refs:
                    yield (INVOKES[op], *self.ref(index))
            elif op == _INVOKEDYNAMIC:
                bootstrap = self.dynamics.get(index)
                if bootstrap is not None and bootstrap < len(self.bootstrap):
                    for argument in self.bootstrap[bootstrap]:
                        target = self.handles.get(argument)
                        if target in self.member_refs:
                            yield ("handle", *self.ref(target))


def read(data: bytes) -> ClassInfo:
    """Decode a class file; see the module docstring."""
    info = ClassInfo()
    if data[:4] != classfile.MAGIC or len(data) < 10:
        return info
    info.minor, info.major = struct.unpack_from(">HH", data, 4)
    try:
        position = _pool(info, data)
        info.access, this_index, super_index, count = struct.unpack_from(">HHHH", data, position)
        info.this_class = info.utf8.get(info.classes.get(this_index, 0), "")
        info.super_name = info.utf8.get(info.classes.get(super_index, 0), "") if super_index else ""
        position += 8
        info.interfaces = [info.utf8.get(info.classes.get(i, 0), "")
                           for i in struct.unpack_from(f">{count}H", data, position)]
        position += 2 * count
        for kind, members in (("field", info.declared_fields), ("method", info.declared_methods)):
            (count,) = struct.unpack_from(">H", data, position)
            position += 2
            for _ in range(count):
                access, name, descriptor, attributes = struct.unpack_from(">HHHH", data, position)
                member = Member(access, info.utf8.get(name, ""), info.utf8.get(descriptor, ""))
                members.append(member)
                position = _attributes(info, data, position + 8, attributes, kind, member)
        (attributes,) = struct.unpack_from(">H", data, position)
        _attributes(info, data, position + 2, attributes, "class", None)
        info.complete = True
    except (IndexError, struct.error, ValueError):
        pass
    return info


def _pool(info: ClassInfo, data: bytes) -> int:
    (count,) = struct.unpack_from(">H", data, 8)
    position, index = 10, 1
    while index < count:
        tag = data[position]
        if tag == 1:
            (length,) = struct.unpack_from(">H", data, position + 1)
            end = position + 3 + length
            if end > len(data):
                raise IndexError("constant pool runs past the end")
            info.utf8[index] = data[position + 3 : end].decode("utf-8", "replace")
            position = end
            index += 1
            continue
        size = classfile._FIXED_SIZES.get(tag)  # the pool entry sizes the survey's reader uses
        if size is None:
            raise ValueError(f"unknown constant pool tag {tag}")
        body = position + 1
        if tag == 3:
            info.integers[index] = struct.unpack_from(">i", data, body)[0]
        elif tag in (4, 5, 6):
            info.numbers[index] = struct.unpack_from({4: ">f", 5: ">q", 6: ">d"}[tag], data, body)[0]
        elif tag == 7:
            info.classes[index] = struct.unpack_from(">H", data, body)[0]
        elif tag == 8:
            info.string_literals.add(struct.unpack_from(">H", data, body)[0])
        elif tag in (9, 10, 11):
            info.member_refs[index] = struct.unpack_from(">HH", data, body)
        elif tag == 12:
            info.name_and_type[index], info.descriptors[index] = struct.unpack_from(">HH", data, body)
        elif tag == 15:
            info.handles[index] = struct.unpack_from(">H", data, body + 1)[0]
        elif tag == 18:
            info.dynamics[index] = struct.unpack_from(">H", data, body)[0]
        position = body + size
        index += 2 if tag in (5, 6) else 1  # a long or a double takes two pool slots
    return position


def _attributes(info: ClassInfo, data: bytes, position: int, count: int, kind: str, member: Member | None) -> int:
    for _ in range(count):
        name_index, length = struct.unpack_from(">HI", data, position)
        start, end = position + 6, position + 6 + length
        if end > len(data):
            raise IndexError("attribute runs past the end")
        name = info.utf8.get(name_index, "")
        if name in _ANNOTATIONS:
            found = _annotations(info, data[start:end], _ANNOTATIONS[name])
            (member.annotations if member is not None else info.annotations).extend(found)
        elif kind == "method" and name == "Code":
            info.method_bodies.append((member.name, data[start:end]))
            (code_length,) = struct.unpack_from(">I", data, start + 4)
            member.code = data[start + 8 : min(end, start + 8 + code_length)]
        elif kind == "class" and name == "BootstrapMethods":
            (methods,) = struct.unpack_from(">H", data, start)
            at = start + 2
            for _ in range(methods):
                _, arguments = struct.unpack_from(">HH", data, at)
                info.bootstrap.append(list(struct.unpack_from(f">{arguments}H", data, at + 4)))
                at += 4 + 2 * arguments
        position = end
    return position


def _annotations(info: ClassInfo, body: bytes, visible: bool) -> list[Annotation]:
    (count,) = struct.unpack_from(">H", body, 0)
    position, found = 2, []
    for _ in range(count):
        annotation, position = _annotation(info, body, position, visible, 0)
        found.append(annotation)
    return found


def _annotation(info: ClassInfo, body: bytes, position: int, visible: bool, depth: int) -> tuple[Annotation, int]:
    if depth > _NESTING:
        raise ValueError("annotations nest too deeply")
    type_index, pairs = struct.unpack_from(">HH", body, position)
    position += 4
    values: dict[str, Any] = {}
    for _ in range(pairs):
        (name_index,) = struct.unpack_from(">H", body, position)
        values[info.utf8.get(name_index, "")], position = _element(info, body, position + 2, visible, depth)
    return Annotation(info.utf8.get(type_index, ""), values, visible), position


def _element(info: ClassInfo, body: bytes, position: int, visible: bool, depth: int) -> tuple[Any, int]:
    tag = chr(body[position])
    position += 1
    if tag == "@":
        return _annotation(info, body, position, visible, depth + 1)
    if tag == "[":
        (count,) = struct.unpack_from(">H", body, position)
        position += 2
        items = []
        for _ in range(count):
            item, position = _element(info, body, position, visible, depth + 1)
            items.append(item)
        return items, position
    if tag == "e":
        type_index, name_index = struct.unpack_from(">HH", body, position)
        return {"enum": info.utf8.get(type_index, ""), "value": info.utf8.get(name_index, "")}, position + 4
    (index,) = struct.unpack_from(">H", body, position)
    position += 2
    if tag in "sc":  # a string, or a class literal as its descriptor
        return info.utf8.get(index, ""), position
    if tag == "Z":
        return bool(info.integers.get(index, 0)), position
    if tag in "BCIS":
        return info.integers.get(index), position
    if tag in "DFJ":
        return info.numbers.get(index), position
    raise ValueError(f"unknown annotation element tag {tag!r}")


def descriptor_class(descriptor: str) -> str:
    """``Lnet/minecraft/X;`` -> ``net/minecraft/X``; anything else unchanged."""
    return descriptor[1:-1] if descriptor.startswith("L") and descriptor.endswith(";") else descriptor


def internal(name: str) -> str:
    """A class name as given (dotted, internal, or a descriptor) in internal form."""
    name = name.strip()
    return descriptor_class(name).replace(".", "/")


def parameters(descriptor: str) -> list[str]:
    """The parameter types of a method descriptor, each as a field descriptor."""
    out, position = [], 1
    if not descriptor.startswith("("):
        return out
    try:
        while descriptor[position] != ")":
            start = position
            while descriptor[position] == "[":
                position += 1
            if descriptor[position] == "L":
                position = descriptor.index(";", position)
            position += 1
            out.append(descriptor[start:position])
    except (IndexError, ValueError):  # a malformed descriptor: the parameters read so far
        pass
    return out
