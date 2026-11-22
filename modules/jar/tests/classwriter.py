"""Write class files with members, annotations and code for tests, on the tools module's test ``Pool``.

The classes need no compiler, so the readers' tests run on any machine; javac-built classes are tested
beside them where a JDK is found.
"""

from __future__ import annotations

import importlib.util
import struct
from dataclasses import dataclass, field
from pathlib import Path

_BUILDER = Path(__file__).resolve().parents[2] / "tools" / "tests" / "classbuilder.py"
_SPEC = importlib.util.spec_from_file_location("wb_test_classbuilder", _BUILDER)
classbuilder = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(classbuilder)
jar = classbuilder.jar

INVOKEVIRTUAL, INVOKESPECIAL, INVOKESTATIC, INVOKEINTERFACE = 0xB6, 0xB7, 0xB8, 0xB9
GETFIELD, RETURN = 0xB4, b"\xb1"


class Pool(classbuilder.Pool):
    def fieldref(self, owner: str, name: str, descriptor: str) -> int:
        data = b"\x09" + struct.pack(">HH", self.cls(owner), self.nat(name, descriptor))
        return self._add(("field", owner, name, descriptor), data)

    def imethodref(self, owner: str, name: str, descriptor: str) -> int:
        data = b"\x0b" + struct.pack(">HH", self.cls(owner), self.nat(name, descriptor))
        return self._add(("imethod", owner, name, descriptor), data)

    def handle(self, kind: int, reference: int) -> int:
        return self._add(("handle", kind, reference), b"\x0f" + struct.pack(">BH", kind, reference))

    def indy(self, bootstrap: int, name: str, descriptor: str) -> int:
        data = b"\x12" + struct.pack(">HH", bootstrap, self.nat(name, descriptor))
        return self._add(("indy", bootstrap, name, descriptor), data)


class Cls(str):
    """A class literal element value, as its descriptor."""


@dataclass
class Enum:
    type: str
    name: str


@dataclass
class Ann:
    type: str
    values: dict = field(default_factory=dict)
    visible: bool = False


def _element(pool: Pool, value) -> bytes:
    if isinstance(value, bool):
        return b"Z" + struct.pack(">H", pool.integer(int(value)))
    if isinstance(value, int):
        return b"I" + struct.pack(">H", pool.integer(value))
    if isinstance(value, Cls):
        return b"c" + struct.pack(">H", pool.utf8(str(value)))
    if isinstance(value, str):
        return b"s" + struct.pack(">H", pool.utf8(value))
    if isinstance(value, Enum):
        return b"e" + struct.pack(">HH", pool.utf8(value.type), pool.utf8(value.name))
    if isinstance(value, Ann):
        return b"@" + _annotation(pool, value)
    return b"[" + struct.pack(">H", len(value)) + b"".join(_element(pool, v) for v in value)


def _annotation(pool: Pool, annotation: Ann) -> bytes:
    out = struct.pack(">HH", pool.utf8(annotation.type), len(annotation.values))
    for name, value in annotation.values.items():
        out += struct.pack(">H", pool.utf8(name)) + _element(pool, value)
    return out


def _attributes(pool: Pool, annotations, code: bytes | None = None) -> list[bytes]:
    out = []
    for name, visible in (("RuntimeVisibleAnnotations", True), ("RuntimeInvisibleAnnotations", False)):
        group = [a for a in annotations if a.visible == visible]
        if group:
            body = struct.pack(">H", len(group)) + b"".join(_annotation(pool, a) for a in group)
            out.append(struct.pack(">HI", pool.utf8(name), len(body)) + body)
    if code is not None:
        body = struct.pack(">HHI", 8, 8, len(code)) + code + struct.pack(">HH", 0, 0)
        out.append(struct.pack(">HI", pool.utf8("Code"), len(body)) + body)
    return out


class ClassWriter:
    def __init__(self, name: str, super_name: str = "java/lang/Object", interfaces=(), access: int = 0x0021,
                 major: int = 52):
        self.pool = Pool()
        self.name, self.super_name, self.interfaces = name, super_name, list(interfaces)
        self.access, self.major = access, major
        self.fields: list[tuple] = []
        self.methods: list[tuple] = []
        self.annotations: list[Ann] = []
        self.bootstrap: list[tuple[int, list[int]]] = []

    def annotate(self, *annotations: Ann) -> ClassWriter:
        self.annotations += annotations
        return self

    def field(self, name: str, descriptor: str, access: int = 0x0002, annotations=()) -> ClassWriter:
        self.fields.append((access, name, descriptor, list(annotations), None))
        return self

    def method(self, name: str, descriptor: str, code: bytes | None = RETURN, access: int = 0x0001,
               annotations=()) -> ClassWriter:
        self.methods.append((access, name, descriptor, list(annotations), code))
        return self

    def invoke(self, opcode: int, owner: str, name: str, descriptor: str) -> bytes:
        """One invoke instruction; invokeinterface takes an InterfaceMethodref and its count bytes."""
        if opcode == INVOKEINTERFACE:
            return bytes([opcode]) + struct.pack(">H", self.pool.imethodref(owner, name, descriptor)) + b"\x01\x00"
        return bytes([opcode]) + struct.pack(">H", self.pool.methodref(owner, name, descriptor))

    def get_field(self, owner: str, name: str, descriptor: str) -> bytes:
        return bytes([GETFIELD]) + struct.pack(">H", self.pool.fieldref(owner, name, descriptor))

    def method_reference(self, owner: str, name: str, descriptor: str) -> bytes:
        """An invokedynamic whose bootstrap binds ``owner.name`` as a method handle (``Owner::name``)."""
        factory = self.pool.handle(6, self.pool.methodref(
            "java/lang/invoke/LambdaMetafactory", "metafactory",
            "(Ljava/lang/invoke/MethodHandles$Lookup;Ljava/lang/String;Ljava/lang/invoke/MethodType;"
            "Ljava/lang/invoke/MethodType;Ljava/lang/invoke/MethodHandle;Ljava/lang/invoke/MethodType;)"
            "Ljava/lang/invoke/CallSite;"))
        target = self.pool.handle(5, self.pool.methodref(owner, name, descriptor))
        self.bootstrap.append((factory, [target]))
        site = self.pool.indy(len(self.bootstrap) - 1, "run", "()Ljava/lang/Runnable;")
        return b"\xba" + struct.pack(">H", site) + b"\x00\x00"

    def build(self) -> bytes:
        this, parent = self.pool.cls(self.name), self.pool.cls(self.super_name) if self.super_name else 0
        interfaces = [self.pool.cls(i) for i in self.interfaces]
        members = []
        for group in (self.fields, self.methods):
            encoded = []
            for access, name, descriptor, marks, code in group:
                attributes = _attributes(self.pool, marks, code)
                encoded.append(struct.pack(">HHHH", access, self.pool.utf8(name), self.pool.utf8(descriptor),
                                           len(attributes)) + b"".join(attributes))
            members.append(struct.pack(">H", len(encoded)) + b"".join(encoded))
        attributes = _attributes(self.pool, self.annotations)
        if self.bootstrap:
            body = struct.pack(">H", len(self.bootstrap)) + b"".join(
                struct.pack(">HH", ref, len(args)) + b"".join(struct.pack(">H", a) for a in args)
                for ref, args in self.bootstrap)
            attributes.append(struct.pack(">HI", self.pool.utf8("BootstrapMethods"), len(body)) + body)
        out = b"\xca\xfe\xba\xbe" + struct.pack(">HHH", 0, self.major, len(self.pool.entries) + 1)
        out += b"".join(self.pool.entries)
        out += struct.pack(">HHHH", self.access, this, parent, len(interfaces))
        out += b"".join(struct.pack(">H", i) for i in interfaces)
        out += b"".join(members) + struct.pack(">H", len(attributes)) + b"".join(attributes)
        return out
