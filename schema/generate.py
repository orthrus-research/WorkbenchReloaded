"""Generates every SDK's envelope type from schema/envelope.schema.json (D24), so the clients cannot drift.

    python schema/generate.py           write the generated files
    python schema/generate.py --check   exit 1 if any generated file is stale

Outputs:
    sdk/python/wb/types.py                                    TypedDicts
    sdk/node/src/envelope.ts                                  TypeScript interfaces
    sdk/java/src/main/java/cc/orthrus/wb/Envelope.java        typed accessors over the parsed JSON
    sdk/powershell/Wb/WbEnvelope.ps1                          a PowerShell class
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / "schema" / "envelope.schema.json").read_text(encoding="utf-8"))
PROPS = SCHEMA["properties"]
REQUIRED = set(SCHEMA.get("required", []))
HEADER = "generated from schema/envelope.schema.json by schema/generate.py; do not edit"


def kinds(spec: dict) -> list[str]:
    if "const" in spec:
        return ["string"]
    if "enum" in spec:
        values = spec["enum"]
        out = sorted({"null" if v is None else "integer" if isinstance(v, int) else "string" for v in values})
        return out
    kind = spec.get("type", "any")
    return kind if isinstance(kind, list) else [kind]


def nullable(spec: dict) -> bool:
    return "null" in kinds(spec)


def camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(p.title() for p in rest)


# -- Python ---------------------------------------------------------------------------------------
def py_type(spec: dict) -> str:
    base = [k for k in kinds(spec) if k != "null"]
    if "enum" in spec and all(isinstance(v, str) or v is None for v in spec["enum"]):
        literal = "Literal[" + ", ".join(json.dumps(v) for v in spec["enum"] if v is not None) + "]"
        return literal + (" | None" if nullable(spec) else "")
    mapping = {"string": "str", "integer": "int", "number": "float", "boolean": "bool", "object": "dict",
               "array": "list", "any": "Any"}
    text = " | ".join(mapping.get(k, "Any") for k in base) or "Any"
    if spec.get("type") == "array" and "items" in spec and spec["items"].get("type") == "string":
        text = "list[str]"
    return text + (" | None" if nullable(spec) else "")


def python() -> str:
    lines = [f'"""The wb/result/v1 envelope as TypedDicts ({HEADER})."""', "", "from __future__ import annotations",
             "", "from typing import Any, Literal, TypedDict", "", "", "class EnvelopeDict(TypedDict, total=False):",
             f'    """{SCHEMA.get("description", "")}"""']
    for name, spec in PROPS.items():
        lines.append(f"    {name}: {py_type(spec)}")
    lines += ["", "", f"SCHEMA_ID = {json.dumps(SCHEMA['$id'])}", f"FIELDS = {sorted(PROPS)!r}", ""]
    return "\n".join(lines)


# -- TypeScript -----------------------------------------------------------------------------------
def ts_type(spec: dict) -> str:
    if "const" in spec:
        return json.dumps(spec["const"])
    if "enum" in spec:
        return " | ".join("null" if v is None else json.dumps(v) for v in spec["enum"])
    out = []
    for kind in kinds(spec):
        if kind == "array":
            items = spec.get("items", {})
            out.append(f"Array<{ts_type(items) if items else 'unknown'}>")
        elif kind == "object":
            props = spec.get("properties")
            if props:
                fields = "; ".join(f"{json.dumps(k)}?: {ts_type(v)}" for k, v in props.items())
                out.append("{ " + fields + "; [key: string]: unknown }")
            else:
                out.append("Record<string, unknown>")
        else:
            out.append({"string": "string", "integer": "number", "number": "number", "boolean": "boolean",
                        "null": "null", "any": "unknown"}.get(kind, "unknown"))
    return " | ".join(out) or "unknown"


def typescript() -> str:
    lines = [f"// {HEADER}", "", f"/** {SCHEMA.get('description', '')} */", "export interface Envelope {"]
    for name, spec in PROPS.items():
        optional = "" if name in REQUIRED else "?"
        lines.append(f"  {json.dumps(name)}{optional}: {ts_type(spec)};")
    lines += ["}", "", f"export const SCHEMA_ID = {json.dumps(SCHEMA['$id'])};", ""]
    return "\n".join(lines)


# -- Java -----------------------------------------------------------------------------------------
def java_type(spec: dict) -> tuple[str, str]:
    base = [k for k in kinds(spec) if k != "null"]
    kind = base[0] if base else "any"
    if kind == "integer":
        return "Long", "asLong"
    if kind == "number":
        return "Double", "asDouble"
    if kind == "boolean":
        return "Boolean", "asBoolean"
    if kind == "string":
        return "String", "asString"
    if kind == "array":
        return "java.util.List<Object>", "asList"
    if kind == "object":
        return "java.util.Map<String, Object>", "asMap"
    return "Object", "raw"


def java() -> str:
    lines = [f"// {HEADER}", "package cc.orthrus.wb;", "", "import java.util.Map;", "",
             f"/** {SCHEMA.get('description', '')} */", "public final class Envelope {",
             "    private final Map<String, Object> raw;", "",
             "    public Envelope(Map<String, Object> raw) {", "        this.raw = raw;", "    }", "",
             "    public Map<String, Object> raw() {", "        return raw;", "    }", ""]
    for name, spec in PROPS.items():
        kind, accessor = java_type(spec)
        lines += [f"    /** {spec.get('description', name)} */",
                  f"    public {kind} {camel(name)}() {{", f"        return Json.{accessor}(raw.get(\"{name}\"));", "    }", ""]
    lines += ["    public boolean settled() {", "        return \"settled\".equals(state());", "    }", "",
              "    public int exitCode() {", "        String state = state();",
              "        if (\"settled\".equals(state)) return code() == null ? 3 : code().intValue();",
              "        if (\"cancelled\".equals(state) || \"interrupted\".equals(state)) return 4;",
              "        if (\"awaiting_approval\".equals(state)) return 6;", "        return 5;", "    }", "",
              f"    public static final String SCHEMA_ID = \"{SCHEMA['$id']}\";", "}", ""]
    return "\n".join(lines)


# -- PowerShell -----------------------------------------------------------------------------------
def ps_type(spec: dict) -> str:
    base = [k for k in kinds(spec) if k != "null"]
    kind = base[0] if base else "any"
    return {"string": "[string]", "integer": "[Nullable[long]]", "number": "[Nullable[double]]",
            "boolean": "[Nullable[bool]]", "array": "[object[]]", "object": "[object]"}.get(kind, "[object]")


def powershell() -> str:
    lines = [f"# {HEADER}", "class WbEnvelope {"]
    for name, spec in PROPS.items():
        lines.append(f"    {ps_type(spec)} ${name}")
    lines += ["", "    WbEnvelope([object] $raw) {", "        foreach ($name in $raw.PSObject.Properties.Name) {",
              "            if ($this.PSObject.Properties.Name -contains $name) { $this.$name = $raw.$name }",
              "        }", "    }", "", "    [int] ExitCode() {",
              "        if ($this.state -eq 'settled') { if ($null -eq $this.code) { return 3 } return [int]$this.code }",
              "        if ($this.state -in @('cancelled', 'interrupted')) { return 4 }",
              "        if ($this.state -eq 'awaiting_approval') { return 6 }", "        return 5", "    }", "}", ""]
    return "\r\n".join(lines)


OUTPUTS = {
    ROOT / "sdk" / "python" / "wb" / "types.py": python,
    ROOT / "sdk" / "node" / "src" / "envelope.ts": typescript,
    ROOT / "sdk" / "java" / "src" / "main" / "java" / "cc" / "orthrus" / "wb" / "Envelope.java": java,
    ROOT / "sdk" / "powershell" / "Wb" / "WbEnvelope.ps1": powershell,
}


def main(argv=None) -> int:
    check = "--check" in (argv or sys.argv[1:])
    stale = []
    for path, build in OUTPUTS.items():
        text = build()
        if check:
            if not path.exists() or path.read_text(encoding="utf-8").replace("\r\n", "\n") != text.replace("\r\n", "\n"):
                stale.append(str(path.relative_to(ROOT)))
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        print(f"wrote {path.relative_to(ROOT)}")
    if stale:
        print("stale: " + ", ".join(stale) + " (run python schema/generate.py)")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
