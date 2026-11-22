"""Who a stack frame belongs to, the same way in every profile command.

A map comes as a flat ``{prefix: owner}`` object or as a spec::

    {"owners": {"com.example.render.": "Renderer", "com.google.": null},
     "engine": ["Game"],                    # owners whose time below another owner's frame is done for it
     "tokens": {"render": "Renderer"},      # merged handler names that carry a token -> owner
     "wrappers": ["\\.Timer\\.measure$"],   # frames that only wrap work (regular expressions)
     "waits": {"\\.Display\\.sync$": "idle"}}  # frames near the leaf that mean the thread is waiting

A frame (``package.Class.method``) is owned by the longest prefix of the map it starts with; an owner of
null or "" makes the prefix transparent. Without a matching prefix, the Java runtime's packages are
transparent and any other frame is owned by its package root (the first two package names), so unmapped
code still shows where it lives. Wrapper frames are transparent. A handler that Mixin merged into another
class (``handler$abc000$name`` and its kin) is owned by the first token its name contains, else it is an
unnamed merged handler. A stack is charged to its first owned frame from the leaf.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

RUNTIME = ("java.", "javax.", "jdk.", "sun.", "com.sun.")
RUNTIME_OWNER = "(java runtime)"
UNNAMED_HANDLER = "(merged handler)"
HANDLER = re.compile(r"^(?:handler|redirect|modify|localvar|constant|wrapOperation|wrapWithCondition|"
                     r"modifyExpressionValue|modifyReturnValue|modifyArg|modifyArgs|modifyVariable|"
                     r"modifyConstant|wrapMethod|inject)\$[0-9a-z]+\$")
SPEC_KEYS = {"owners", "groups", "engine", "tokens", "wrappers", "waits"}
WAIT_DEPTH = 8  # waits are recognised in this many frames from the leaf


@dataclass
class Spec:
    prefixes: dict[str, str | None] = field(default_factory=dict)
    engine: list[str] = field(default_factory=list)
    tokens: dict[str, str] = field(default_factory=dict)
    wrappers: list[str] = field(default_factory=list)
    waits: dict[str, str] = field(default_factory=dict)

    @classmethod
    def merge(cls, *sources) -> Spec:
        """One spec from several maps (flat or spec form); later ones add to and override earlier ones."""
        spec = cls()
        for source in sources:
            if source in (None, {}):
                continue
            if not isinstance(source, dict):
                raise ValueError("a map is a JSON object")
            if not set(source) & SPEC_KEYS:
                source = {"owners": source}
            unknown = set(source) - SPEC_KEYS
            if unknown:
                raise ValueError(f"unknown map keys {sorted(unknown)} (known: {sorted(SPEC_KEYS)})")
            for key in ("owners", "groups"):
                for prefix, owner in _object(source.get(key), key).items():
                    if owner is not None and not isinstance(owner, str):
                        raise ValueError(f"the owner of {prefix!r} must be a string or null")
                    spec.prefixes[prefix] = owner or None
            engine = source.get("engine") or []
            if not isinstance(engine, list) or not all(isinstance(e, str) for e in engine):
                raise ValueError("engine is a list of owner names")
            spec.engine += [e for e in engine if e not in spec.engine]
            for token, owner in _object(source.get("tokens"), "tokens").items():
                spec.tokens[token.lower()] = str(owner)
            wrappers = source.get("wrappers") or []
            if not isinstance(wrappers, list):
                raise ValueError("wrappers is a list of regular expressions")
            spec.wrappers += [_pattern(w) for w in wrappers if w not in spec.wrappers]
            for pattern, label in _object(source.get("waits"), "waits").items():
                spec.waits[_pattern(pattern)] = str(label)
        return spec

    def describe(self) -> dict:
        return {"owners": dict(sorted(self.prefixes.items())), "engine": self.engine, "tokens": self.tokens,
                "wrappers": self.wrappers, "waits": self.waits}


def _object(value, key: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{key} is an object")
    return value


def _pattern(text) -> str:
    try:
        re.compile(str(text))
    except re.error as error:
        raise ValueError(f"not a regular expression: {text!r} ({error})") from None
    return str(text)


@dataclass(frozen=True)
class Charge:
    """Where one stack's cost goes: its owner and that owner's frame nearest the leaf; for an engine owner,
    the owner it worked for (the nearest non-engine owner toward the root) and that owner's frame; and every
    owner on the stack."""
    owner: str
    method: str
    behalf: str | None = None
    behalf_method: str = ""
    present: frozenset[str] = frozenset()


def package_root(method: str) -> str:
    parts = method.split(".")[:-2]  # drop the class and the method
    return ".".join(parts[:2]) if parts else "(default package)"


class Owners:
    def __init__(self, spec: Spec | None = None):
        self.spec = spec or Spec()
        self.prefixes = sorted(self.spec.prefixes.items(), key=lambda item: -len(item[0]))
        self.engine = set(self.spec.engine)
        self.tokens = sorted(self.spec.tokens.items(), key=lambda item: -len(item[0]))
        self.wrappers = re.compile("|".join(f"(?:{w})" for w in self.spec.wrappers)) if self.spec.wrappers else None
        self.waits = [(re.compile(p), label) for p, label in self.spec.waits.items()]
        self._owners: dict[str, str | None] = {}

    def owner(self, method: str) -> str | None:
        """The owner of one frame, None when it is transparent."""
        if method in self._owners:
            return self._owners[method]
        found = self._owner(method)
        self._owners[method] = found
        return found

    def _owner(self, method: str) -> str | None:
        if self.wrappers is not None and self.wrappers.search(method):
            return None
        name = method.rsplit(".", 1)[-1]
        handler = HANDLER.match(name)
        if handler:
            carried = name[handler.end():].lower()
            return next((owner for token, owner in self.tokens if token in carried), UNNAMED_HANDLER)
        for prefix, owner in self.prefixes:
            if method.startswith(prefix):
                return owner
        if method.startswith(RUNTIME):
            return None
        return package_root(method)

    def single(self, method: str) -> str:
        """The owner of a lone method (a compilation's): the runtime's when it is transparent."""
        return self.owner(method) or RUNTIME_OWNER

    def charge(self, frames: Sequence[str]) -> Charge:
        for frame in frames[:WAIT_DEPTH]:
            for pattern, label in self.waits:
                if pattern.search(frame):
                    return Charge(label, frame)
        owned = [(frame, owner) for frame in frames if (owner := self.owner(frame)) is not None]
        if not owned:
            return Charge(RUNTIME_OWNER, frames[0] if frames else "(no stack)")
        frame, owner = owned[0]
        behalf, behalf_method = None, ""
        if owner in self.engine:
            for other_frame, other in owned[1:]:
                if other not in self.engine and other != UNNAMED_HANDLER:
                    behalf, behalf_method = other, other_frame
                    break
        present = frozenset(o for _, o in owned if o not in self.engine and o != UNNAMED_HANDLER)
        return Charge(owner, frame, behalf, behalf_method, present)


def thread_group(name: str) -> str:
    """A thread's group: its name with each run of digits as #, so a pool's threads count together."""
    return re.sub(r"\d+", "#", name).strip() or name
