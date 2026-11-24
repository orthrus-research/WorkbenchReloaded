"""The closed set of value types used by command parameters, results and config schemas (spec S4).

A field spec is a small table: ``{ type = "money", unit = "USD/h", default = 0.30 }``. Supported keys:
type, default, optional, min, max, pattern, values (enum), items (list), unit, summary, and digest (path.in only):
how a job records the input, ``content`` (the default: a SHA-256 over every file), ``git`` (a checkout the
command reads through git, recorded by its HEAD commit; the command reports the commit or tree it read) or ``none``
(a folder the command inventories and records itself, such as hundreds of GiB of old instances: recorded by its
path only, so nothing is read through the mount before the job starts).

An object may say what it holds: ``fields``, a table of field specs for a table with fixed keys (unknown keys are
refused, defaults filled in), or ``entries``, one field spec every value of a table keyed by name must match (a map,
such as projects by name or variables by name). A value deeper in is named by its dotted path in an error.
"""

from __future__ import annotations

import re

TYPES = {"string", "int", "float", "bool", "enum", "list", "object", "money", "duration", "sha256",
         "path.in", "path.out", "any"}
SPEC_KEYS = {"type", "default", "optional", "min", "max", "pattern", "values", "items", "unit", "summary", "digest",
             "fields", "entries"}
DIGESTS = {"content", "git", "none"}
_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|m|h|d)?\s*$")
_UNITS = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400, None: 1}
_SHA = re.compile(r"^(sha256:)?[0-9a-f]{64}$")


class ValueError_(Exception):
    """A value that does not match its spec; ``field`` names it."""

    def __init__(self, field: str, message: str):
        super().__init__(f"{field}: {message}")
        self.field = field
        self.detail = message


def normalise_spec(name: str, spec) -> dict:
    if isinstance(spec, str):
        spec = {"type": spec}
    if not isinstance(spec, dict):
        raise ValueError_(name, "a field spec must be a table or a type name")
    unknown = set(spec) - SPEC_KEYS
    if unknown:
        raise ValueError_(name, f"unknown spec keys {sorted(unknown)}")
    kind = spec.get("type", "string")
    if kind not in TYPES:
        raise ValueError_(name, f"unknown type {kind!r}")
    if kind == "enum" and not spec.get("values"):
        raise ValueError_(name, "an enum needs values")
    if "digest" in spec and (kind != "path.in" or spec["digest"] not in DIGESTS):
        raise ValueError_(name, f"digest is for a path.in, one of {sorted(DIGESTS)}")
    spec = dict(spec, type=kind)
    if ("fields" in spec or "entries" in spec) and kind != "object":
        raise ValueError_(name, "fields and entries are for an object")
    if "fields" in spec and "entries" in spec:
        raise ValueError_(name, "an object has fixed fields or entries by name, not both")
    if "fields" in spec:
        if not isinstance(spec["fields"], dict):
            raise ValueError_(name, "fields must be a table of field specs")
        spec["fields"] = {key: normalise_spec(f"{name}.{key}", value) for key, value in spec["fields"].items()}
    if "entries" in spec:
        spec["entries"] = normalise_spec(f"{name}.*", spec["entries"])
    return spec


def parse_duration(value) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    match = _DURATION.match(str(value))
    if not match:
        raise ValueError(f"not a duration: {value!r} (use 30s, 90m, 2h)")
    return float(match.group(1)) * _UNITS[match.group(2)]


def coerce(name: str, spec: dict, value):
    """Checks and converts one value (CLI strings included); returns the canonical value."""
    kind = spec["type"]
    try:
        if kind == "any":
            out = value
        elif kind in ("string", "path.in", "path.out"):
            if not isinstance(value, str):
                raise ValueError("expected a string")
            out = value
        elif kind == "int":
            if isinstance(value, bool):
                raise ValueError("expected an integer")
            out = int(value) if not isinstance(value, float) or value.is_integer() else None
            if out is None:
                raise ValueError("expected an integer")
        elif kind in ("float", "money"):
            if isinstance(value, bool):
                raise ValueError("expected a number")
            out = float(value)
        elif kind == "bool":
            if isinstance(value, bool):
                out = value
            elif str(value).lower() in ("true", "yes", "1", "on"):
                out = True
            elif str(value).lower() in ("false", "no", "0", "off"):
                out = False
            else:
                raise ValueError("expected true or false")
        elif kind == "enum":
            if value not in spec["values"]:
                raise ValueError(f"expected one of {spec['values']}")
            out = value
        elif kind == "duration":
            out = parse_duration(value)
        elif kind == "sha256":
            if not isinstance(value, str) or not _SHA.match(value.lower()):
                raise ValueError("expected a SHA-256 hex digest")
            out = value.lower().removeprefix("sha256:")
        elif kind == "list":
            if isinstance(value, str):
                value = [v for v in (p.strip() for p in value.split(",")) if v]
            if not isinstance(value, list):
                raise ValueError("expected a list")
            item = spec.get("items")
            if item and item != "any":
                item_spec = normalise_spec(name, item if isinstance(item, dict) else {"type": item})
                out = [coerce(f"{name}[{i}]", item_spec, v) for i, v in enumerate(value)]
            else:
                out = list(value)
        elif kind == "object":
            if not isinstance(value, dict):
                raise ValueError("expected an object")
            out = _object(name, spec, value)
        else:  # pragma: no cover - TYPES is closed
            raise ValueError(f"unsupported type {kind}")
    except (TypeError, ValueError) as error:
        raise ValueError_(name, str(error)) from None
    if kind in ("int", "float", "money", "duration"):
        low, high = spec.get("min"), spec.get("max")
        if kind == "duration":
            low = parse_duration(low) if low is not None else None
            high = parse_duration(high) if high is not None else None
        if low is not None and out < low:
            raise ValueError_(name, f"{value} is below the minimum {spec['min']}")
        if high is not None and out > high:
            raise ValueError_(name, f"{value} is above the maximum {spec['max']}")
    if kind in ("string",) and spec.get("pattern") and not re.search(spec["pattern"], out):
        raise ValueError_(name, f"{out!r} does not match {spec['pattern']}")
    return out


def _object(name: str, spec: dict, value: dict) -> dict:
    """An object's ``fields`` or ``entries`` checked; an error names the value by its dotted path."""
    if spec.get("fields") is not None:
        fields = {key: normalise_spec(key, field) for key, field in spec["fields"].items()}
        try:
            return validate(fields, value, where="keys")
        except ValueError_ as error:
            raise ValueError_(f"{name}.{error.field}", error.detail) from None
    if spec.get("entries") is not None:
        entry = normalise_spec(name, spec["entries"])
        return {key: coerce(f"{name}.{key}", entry, item) for key, item in value.items()}
    return value


def validate(fields: dict[str, dict], values: dict, *, where: str = "params") -> dict:
    """Checks ``values`` against ``fields``: unknown keys and missing required ones are errors.
    Returns the values with defaults filled in."""
    out = {}
    unknown = set(values) - set(fields)
    if unknown:
        raise ValueError_(sorted(unknown)[0], f"unknown {where[:-1] if where.endswith('s') else where} "
                                             f"(known: {', '.join(sorted(fields)) or 'none'})")
    for name, spec in fields.items():
        if name in values and values[name] is not None:
            out[name] = coerce(name, spec, values[name])
        elif "default" in spec:
            out[name] = coerce(name, spec, spec["default"]) if spec["default"] is not None else None
        elif spec.get("optional"):
            continue
        else:
            raise ValueError_(name, "required")
    return out


def json_schema(spec: dict) -> dict:
    kind = spec["type"]
    base: dict
    if kind in ("string",):
        base = {"type": "string"}
        if spec.get("pattern"):
            base["pattern"] = spec["pattern"]
    elif kind in ("path.in", "path.out"):
        base = {"type": "string", "format": kind}
    elif kind == "int":
        base = {"type": "integer"}
    elif kind in ("float", "money"):
        base = {"type": "number"}
        if kind == "money":
            base["format"] = "money"
    elif kind == "bool":
        base = {"type": "boolean"}
    elif kind == "enum":
        base = {"enum": list(spec["values"])}
    elif kind == "duration":
        base = {"oneOf": [{"type": "number"}, {"type": "string", "pattern": _DURATION.pattern}],
                "format": "duration"}
    elif kind == "sha256":
        base = {"type": "string", "pattern": _SHA.pattern}
    elif kind == "list":
        item = spec.get("items")
        base = {"type": "array"}
        if item and item != "any":
            base["items"] = json_schema(normalise_spec("item", item if isinstance(item, dict) else {"type": item}))
    elif kind == "object":
        if spec.get("fields") is not None:
            base = object_schema({k: normalise_spec(k, f) for k, f in spec["fields"].items()})
        elif spec.get("entries") is not None:
            base = {"type": "object", "additionalProperties": json_schema(normalise_spec("entry", spec["entries"]))}
        else:
            base = {"type": "object"}
    else:
        base = {}
    for key in ("min", "max"):
        if key in spec and kind != "duration":
            base["minimum" if key == "min" else "maximum"] = spec[key]
    if "default" in spec:
        base["default"] = spec["default"]
    if spec.get("summary"):
        base["description"] = spec["summary"]
    if spec.get("unit"):
        base["x-unit"] = spec["unit"]
    return base


def object_schema(fields: dict[str, dict]) -> dict:
    required = [n for n, s in fields.items() if "default" not in s and not s.get("optional")]
    schema = {"type": "object", "additionalProperties": False,
              "properties": {n: json_schema(s) for n, s in fields.items()}}
    if required:
        schema["required"] = required
    return schema
